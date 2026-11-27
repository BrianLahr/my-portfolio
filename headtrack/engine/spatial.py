"""Dynamic KEMAR FIR renderer; the source coordinates and quaternion convention follow aar_core."""
from __future__ import annotations

import hashlib
from pathlib import Path

import h5py
import numpy as np
from scipy.fft import next_fast_len, rfft, irfft

from tcn_stream import FS, HOP

KEMAR_MD5 = '6ebef4950764f0e6c4bde77a6fbd5fd4'


def _str(x) -> str:
    return x.decode() if isinstance(x, bytes) else str(x)


class KEMARRenderer:
    def __init__(self, sofa_path: str, *, verify_md5: bool = True):
        path = Path(sofa_path)
        if verify_md5:
            digest = hashlib.md5()
            with path.open('rb') as file:
                for chunk in iter(lambda: file.read(1 << 20), b''):
                    digest.update(chunk)
            if digest.hexdigest() != KEMAR_MD5:
                raise ValueError(f'O SOFA difere do KEMAR_s.sofa usado no treino: MD5 {digest.hexdigest()}')
        with h5py.File(path, 'r') as h:
            p = h['SourcePosition']
            if 'spherical' not in _str(p.attrs.get('Type', '')).lower() or \
                    'degree' not in _str(p.attrs.get('Units', '')).lower():
                raise ValueError('SOFA: SourcePosition deve ser spherical/degree')
            positions = np.asarray(p)
            rec = np.squeeze(np.asarray(h['ReceiverPosition']))
            if rec.shape != (2, 3):
                raise ValueError('SOFA: esperados dois receptores com coordenadas cartesianas')
            left, right = int(np.argmax(rec[:, 1])), int(np.argmin(rec[:, 1]))
            if not rec[left, 1] > 0 > rec[right, 1]:
                raise ValueError('SOFA: não foi possível identificar ouvido esquerdo e direito')
            if int(np.asarray(h['Data.SamplingRate']).ravel()[0]) != FS:
                raise ValueError('SOFA: taxa diferente de 44100 Hz')
            indices = np.flatnonzero(np.abs(positions[:, 1]) <= 1.01)
            if len(indices) < 24:
                raise ValueError('SOFA: cobertura horizontal insuficiente')
            self.angles = np.mod(positions[indices, 0], 360.)
            order = np.argsort(self.angles)
            indices, self.angles = indices[order], self.angles[order]
            if np.min(np.diff(self.angles)) < 0.1:
                raise ValueError('SOFA: azimutes horizontais duplicados')
            ir = np.stack([np.asarray(h['Data.IR'][int(i)], dtype=np.float32)
                           for i in indices])[:, [left, right], :]
            if 'Data.Delay' in h:
                ds = h['Data.Delay']
                delay = np.asarray(ds)
                if delay.ndim != 2 or delay.shape[1] != 2 or delay.shape[0] not in (1, len(positions)):
                    raise ValueError('SOFA: formato de Data.Delay desconhecido')
                delay = (delay[indices] if len(delay) > 1 else np.repeat(delay, len(indices), axis=0))[:, [left, right]]
                units = _str(ds.attrs.get('Units', 'sample')).lower()
                if 'second' in units:
                    delay *= FS
                elif 'sample' not in units:
                    raise ValueError(f'SOFA: unidade de Data.Delay não reconhecida: {units}')
                # Match the notebook's +30/-30 delay differences, then add the
                # same offset to every direction so all FIRs remain causal.
                # The offset is a fixed extra render latency, independent of
                # ear and azimuth; it does not change ITD or relative timing.
                a = int(np.argmin(np.abs((self.angles - 30. + 180.) % 360. - 180.)))
                b = int(np.argmin(np.abs((self.angles + 30. + 180.) % 360. - 180.)))
                delay = delay - min(delay[a].min(), delay[b].min())
                delay -= min(0.0, float(np.min(delay)))
                if np.min(delay) < -1e-6:
                    raise ValueError('SOFA: Data.Delay requer atrasos negativos para outros azimutes')
                if np.max(delay) > 5 * FS:
                    raise ValueError('SOFA: atraso irreal (>5 s)')
                if np.any(delay):
                    extra = int(np.ceil(np.max(delay))) + 2
                    base = np.arange(ir.shape[-1])
                    shifted = np.empty((len(indices), 2, ir.shape[-1] + extra), dtype=np.float32)
                    for i in range(len(indices)):
                        for m in range(2):
                            shifted[i, m] = np.interp(np.arange(shifted.shape[-1]) - delay[i, m],
                                                       base, ir[i, m], left=0., right=0.)
                    ir = shifted
        self.length = ir.shape[-1]
        self.fft_length = next_fast_len(HOP + 2 * (self.length - 1))
        self.h_freq = rfft(ir, n=self.fft_length, axis=-1)
        self.history = np.zeros((2, self.length - 1), dtype=np.float32)
        self.last_h = [None, None]

    def _bracket(self, az: float) -> tuple[int, int, float]:
        az = az % 360.
        upper = int(np.searchsorted(self.angles, az))
        hi, lo = upper % len(self.angles), (upper - 1) % len(self.angles)
        low, high = self.angles[lo], self.angles[hi]
        query = az
        if high <= low:
            high += 360.
            if query < low:
                query += 360.
        return lo, hi, float((query - low) / (high - low))

    def push(self, sources: tuple[np.ndarray, np.ndarray], azimuths: tuple[float, float]):
        output = []
        for n, (chunk, az) in enumerate(zip(sources, azimuths)):
            if chunk.shape != (HOP,):
                raise ValueError('Fonte deve ter 256 amostras mono')
            frame = np.concatenate((self.history[n], chunk))
            self.history[n] = frame[HOP:].copy()
            lo, hi, fraction = self._bracket(az)
            h = (1. - fraction) * self.h_freq[lo] + fraction * self.h_freq[hi]
            x = rfft(frame, n=self.fft_length)[None, :]
            result = irfft(x * h,
                           n=self.fft_length, axis=-1)
            result = result[:, self.length - 1:self.length - 1 + HOP].T
            # A static orientation has the same filter every block; avoid a
            # second inverse FFT for an identical crossfade target.
            if self.last_h[n] is not None and not np.array_equal(h, self.last_h[n]):
                old = irfft(x * self.last_h[n], n=self.fft_length, axis=-1)
                old = old[:, self.length - 1:self.length - 1 + HOP].T
                ramp = np.linspace(0., 1., HOP, dtype=np.float32)[:, None]
                result = (1. - ramp) * old + ramp * result
            self.last_h[n] = h
            output.append(result.astype(np.float32))
        return tuple(output)
