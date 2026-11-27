"""Inference-only, bit-compatible architecture and streaming path from the EARS KEMAR notebook."""
from __future__ import annotations

import os
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

FS = 44100
FFT = 512
HOP = 256
DELAY = FFT - HOP


class CausalBlock(nn.Module):
    def __init__(self, channels: int, dilation: int):
        super().__init__()
        self.dilation = dilation
        self.context = 2 * dilation
        self.expand = nn.Conv1d(channels, channels, 1)
        self.depth = nn.Conv1d(channels, channels, 3, dilation=dilation, groups=channels)
        self.project = nn.Conv1d(channels, channels, 1)
        self.norm = nn.LayerNorm(channels)
        self.act = nn.PReLU(channels)

    def _post(self, q: torch.Tensor) -> torch.Tensor:
        q = self.project(self.act(self.depth(q)))
        return self.norm(q.transpose(1, 2)).transpose(1, 2)

    def step(self, z: torch.Tensor, cache: torch.Tensor | None):
        q = self.expand(z)
        if cache is None:
            cache = q.new_zeros(q.shape[0], q.shape[1], self.context)
        sequence = torch.cat((cache, q), dim=-1)
        return z + self._post(sequence), sequence[:, :, -self.context:]

    def forward(self, z: torch.Tensor):
        q = self.expand(z)
        return z + self._post(F.pad(q, (self.context, 0)))


class CausalSpatialTCN(nn.Module):
    def __init__(self, n_freq: int = FFT // 2 + 1, feat_bins: int = 64, channels: int = 128):
        super().__init__()
        self.n_freq = n_freq
        self.feat_bins = feat_bins
        self.input = nn.Linear(5 * feat_bins, channels)
        self.blocks = nn.ModuleList([CausalBlock(channels, 2 ** i) for _ in range(2) for i in range(5)])
        self.head = nn.Linear(channels, 8 * n_freq)
        nn.init.zeros_(self.head.weight)
        nn.init.zeros_(self.head.bias)

    def features(self, x: torch.Tensor):
        assert x.ndim == 4 and x.shape[-2:] == (self.n_freq, 2)
        b, t, nf, _ = x.shape
        mag = x.abs()
        pair = x[..., 0] * x[..., 1].conj()
        phase = pair / (pair.abs() + 1e-7)
        ild = (torch.log((mag[..., 0] + 1e-6) / (mag[..., 1] + 1e-6)).clamp(-4, 4) / 4)
        f = torch.stack((torch.log1p(100 * mag[..., 0]), torch.log1p(100 * mag[..., 1]),
                         phase.real, phase.imag, ild), dim=2)
        f = F.interpolate(f.reshape(b * t, 5, nf), size=self.feat_bins,
                          mode='linear', align_corners=False)
        return f.reshape(b, t, 5 * self.feat_bins)

    def _images(self, x: torch.Tensor, z: torch.Tensor):
        b, t, nf, _ = x.shape
        q = self.head(z.transpose(1, 2)).reshape(b, t, nf, 2, 2, 2)
        eye = torch.eye(2, device=x.device, dtype=q.dtype).reshape(1, 1, 1, 2, 2)
        h = torch.complex(0.5 * eye + 1.5 * torch.tanh(q[..., 0]),
                          1.5 * torch.tanh(q[..., 1]))
        ya = torch.einsum('btfem,btfm->btfe', h, x)
        return ya, x - ya

    def forward(self, x: torch.Tensor):
        z = self.input(self.features(x)).transpose(1, 2)
        for block in self.blocks:
            z = block(z)
        return self._images(x, z)

    def step(self, x: torch.Tensor, state=None):
        assert x.shape[1] == 1
        z = self.input(self.features(x)).transpose(1, 2)
        if state is None:
            state = [None] * len(self.blocks)
        updated = []
        for block, cache in zip(self.blocks, state):
            z, cache = block.step(z, cache)
            updated.append(cache)
        ya, yb = self._images(x, z)
        return ya, yb, updated


def load_model(checkpoint: str, device: str = 'cpu') -> CausalSpatialTCN:
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    package = torch.load(checkpoint, map_location='cpu', weights_only=True)
    for key, expected in [('architecture', 'spatial_tcn_v1'), ('fs', FS), ('fft', FFT)]:
        if package.get(key) != expected:
            raise ValueError(f'Checkpoint incompatível: {key}={package.get(key)!r}; esperado {expected!r}')
    model = CausalSpatialTCN()
    model.load_state_dict(package['state_dict'], strict=True)
    return model.to(device).eval()


class NeuralStream:
    """Returns [samples, sources, ears]; waveform delay HOP plus one scheduler HOP."""
    def __init__(self, net: CausalSpatialTCN, device: str = 'cpu'):
        self.net = net
        self.device = device
        self.win = torch.as_tensor(np.sqrt(np.hanning(FFT + 1)[:-1].copy()),
                                   dtype=torch.float32, device=device)
        self.hist = torch.zeros(DELAY, 2, device=device)
        self.ola = torch.zeros(FFT, 2, 2, device=device)
        self.state = None

    @torch.inference_mode()
    def push(self, block: np.ndarray) -> np.ndarray:
        if block.shape != (HOP, 2):
            raise ValueError('Bloco precisa ter exatamente 256 amostras x 2 canais')
        v = torch.as_tensor(block, dtype=torch.float32, device=self.device)
        frame = torch.cat((self.hist, v), dim=0)
        self.hist = frame[HOP:].clone()
        spectrum = torch.fft.rfft(frame * self.win[:, None], dim=0)[None, None, :, :]
        ya, yb, self.state = self.net.step(spectrum, self.state)
        spec = torch.stack((ya[0, 0], yb[0, 0]), dim=1)  # [frequency, source, ear]
        samples = torch.fft.irfft(spec, n=FFT, dim=0) * self.win[:, None, None]
        self.ola = self.ola + samples
        out = self.ola[:HOP].clone()
        self.ola = torch.cat((self.ola[HOP:], torch.zeros_like(self.ola[:HOP])), dim=0)
        return out.cpu().numpy()


def verify_streaming(net: CausalSpatialTCN, device: str):
    """Check checkpoint step equivalence, mixture consistency and block delay."""
    with torch.inference_mode():
        generator = torch.Generator(device=device).manual_seed(179)
        real = torch.randn(1, 12, FFT // 2 + 1, 2, generator=generator, device=device)
        imag = torch.randn(1, 12, FFT // 2 + 1, 2, generator=generator, device=device)
        frames = torch.complex(real, imag)
        full_a, full_b = net(frames)
        state = None
        aa, bb = [], []
        for t in range(frames.shape[1]):
            a, b, state = net.step(frames[:, t:t + 1], state)
            aa.append(a)
            bb.append(b)
        if not torch.allclose(full_a, torch.cat(aa, dim=1), atol=1e-4, rtol=1e-4) or \
                not torch.allclose(full_b, torch.cat(bb, dim=1), atol=1e-4, rtol=1e-4):
            raise RuntimeError('TCN: forward e step não coincidem')
        signal = np.zeros((16 * HOP, 2), dtype=np.float32)
        signal[4 * HOP, 0] = 0.1
        signal[5 * HOP + 37, 1] = 0.07
        stream = NeuralStream(net, device)
        output = np.concatenate([stream.push(signal[i:i + HOP])
                                 for i in range(0, len(signal), HOP)], axis=0)
        if output.shape != (len(signal), 2, 2):
            raise RuntimeError(f'TCN/STFT: dimensões de saída inesperadas: {output.shape}')
        reconstructed = output.sum(axis=1)  # sum over sources, retain L/R ears
        impulse = int(np.argmax(abs(reconstructed[:, 0])))
        error = np.max(abs(reconstructed[4 * HOP:, :] -
                           np.pad(signal, ((HOP, 0), (0, 0)))[:len(signal)][4 * HOP:]))
        if impulse - 4 * HOP != HOP or error > 2e-5:
            raise RuntimeError(f'TCN/STFT: atraso {impulse - 4 * HOP} samples; erro {error}')
