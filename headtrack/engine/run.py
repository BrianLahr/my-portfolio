"""Head-tracked, two-source real-time listening demonstration. Run with --help."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
import math
import os
import queue
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import torch
from scipy.signal import resample_poly
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                              QFormLayout, QHBoxLayout, QLabel, QMainWindow,
                              QPushButton, QVBoxLayout, QWidget)

from tcn_stream import FS, HOP, FFT, NeuralStream, load_model, verify_streaming
from spatial import KEMARRenderer

DEVICE_NAME = 'Tiresias_DK'
CHAR_UUID = '12345678-1234-5678-1234-56789abcdef1'
ATTENTION_SIGMA_DEG = 20.0
ATTENTION_BOOST_DB = 10.0


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def attention_db(azimuth: float) -> float:
    """João's Gaussian attention in dB for a source at 1 m."""
    return ATTENTION_BOOST_DB * math.exp(-0.5 * (azimuth / ATTENTION_SIGMA_DEG) ** 2)


def level_dbfs(signal: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(signal, dtype=np.float64))))
    return 20.0 * math.log10(max(rms, 1e-12))


def aar_core_from_repo(repo: Path):
    # Use João's unchanged module directly from the adjacent repository.
    sys.path.insert(0, str(repo.resolve()))
    import aar_core  # type: ignore
    return aar_core


class Orientation:
    def __init__(self, core, device_name: str, uuid: str):
        self.core, self.device_name, self.uuid = core, device_name, uuid
        self.lock = threading.Lock()
        self.raw = None
        self.tare = None
        self.time = 0.
        self.status = 'Buscando IMU'
        self.manual_yaw = 0.
        self.manual = False
        self.stop = threading.Event()

    def notification(self, _sender, data: bytearray):
        if len(data) != 16:
            return
        q = struct.unpack('<ffff', data)  # exactly the format from João's main.py
        norm = math.sqrt(sum(x * x for x in q))
        if not 0.5 < norm < 1.5 or not all(math.isfinite(x) for x in q):
            return
        q = tuple(x / norm for x in q)
        with self.lock:
            self.raw = q
            self.time = time.monotonic()
            if self.tare is None:
                self.tare = self.core.quaternion_conjugate(q)

    def zero(self):
        with self.lock:
            if self.raw is not None:
                self.tare = self.core.quaternion_conjugate(self.raw)

    def snapshot(self):
        with self.lock:
            if self.manual:
                return self.core.euler_to_quaternion(0., 0., self.manual_yaw), 'yaw manual'
            if self.raw is None or self.tare is None:
                return (1., 0., 0., 0.), 'IMU sem dados recentes: cabeça neutra'
            q = self.core.quaternion_multiply(self.tare, self.raw)
            if time.monotonic() - self.time > 1.0:
                return q, 'IMU sem dados recentes: orientação congelada'
            return q, self.status

    async def _run(self):
        from bleak import BleakClient, BleakScanner
        while not self.stop.is_set():
            try:
                with self.lock:
                    self.status = 'Buscando IMU'
                devices = await BleakScanner.discover(timeout=5.0)
                found = next((d for d in devices if d.name and self.device_name in d.name), None)
                if found is None:
                    with self.lock:
                        self.status = f'{self.device_name} não encontrado'
                    await asyncio.sleep(2.)
                    continue
                async with BleakClient(found) as client:
                    await client.start_notify(self.uuid, self.notification)
                    with self.lock:
                        self.raw, self.tare = None, None
                        self.status = 'IMU conectado; aponte para frente e clique Calibrar'
                    while client.is_connected and not self.stop.is_set():
                        await asyncio.sleep(.2)
            except Exception as exc:
                with self.lock:
                    self.status = f'BLE: {type(exc).__name__}: {exc}'
                await asyncio.sleep(2.)

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True,
                         name='imu-ble').start()


def load_track(path: Path):
    try:
        x, rate = sf.read(path, dtype='float32')
    except Exception as exc:
        raise RuntimeError(f'Não pude abrir {path}. Se MP3 falhar, converta-o para WAV PCM com ffmpeg: '
                           f'ffmpeg -i "{path}" -ar 44100 -ac 1 "{path.with_suffix(".wav")}"') from exc
    if x.ndim == 2:
        x = x.mean(axis=1)
    if rate != FS:
        import math as _math
        g = _math.gcd(rate, FS)
        x = resample_poly(x, FS // g, rate // g).astype(np.float32)
    if len(x) < FS:
        raise ValueError(f'Fonte curta: {path}')
    rms = np.sqrt(np.mean(np.square(x, dtype=np.float64)) + 1e-12)
    return (x * (.03 / rms)).astype(np.float32)


def first_active_window(track: np.ndarray, *, window_seconds: float = 2.) -> int:
    """Find an early sustained speech window without changing track amplitude."""
    width = round(window_seconds * FS)
    if len(track) <= width:
        return 0
    step = FS // 2
    starts = np.arange(0, min(len(track) - width, 30 * FS) + 1, step)
    energy = np.concatenate(([0.], np.cumsum(np.square(track[:starts[-1] + width], dtype=np.float64))))
    levels = np.sqrt((energy[starts + width] - energy[starts]) / width)
    eligible = np.flatnonzero(levels >= max(.008, .9 * float(levels.max())))
    return int(starts[eligible[0]]) if len(eligible) else 0


class Engine:
    def __init__(self, args, orientation: Orientation, core):
        self.core, self.orientation = core, orientation
        self.block_hops = int(getattr(args, 'block_hops', 1))
        if self.block_hops not in (1, 2):
            raise ValueError('block_hops deve ser 1 ou 2')
        self.renderer = KEMARRenderer(str(args.sofa))
        net = load_model(str(args.checkpoint), args.device)
        verify_streaming(net, args.device)
        self.stream = NeuralStream(net, args.device)
        self.audio = (load_track(args.source_a), load_track(args.source_b))
        self.start_positions = [first_active_window(track) for track in self.audio]
        self.positions = self.start_positions.copy()
        self.world_az = (30., -30.)
        self.sources = tuple(np.array([math.cos(math.radians(a)),
                                       math.sin(math.radians(a)), 0.], dtype=np.float32)
                             for a in self.world_az)
        self.raw_on = False
        self.target = 1  # 0=nearest source, 1=A, 2=B
        self.raw_gain = db_to_linear(-12.)
        self.enh_gain = 1.
        self.buffer_blocks = int(getattr(args, 'buffer_blocks', 16))
        if not 3 <= self.buffer_blocks <= 128:
            raise ValueError('buffer_blocks deve estar entre 3 e 128')
        self.audio_latency = getattr(args, 'audio_latency', 'high')
        self.queue: queue.Queue[tuple[np.ndarray, np.ndarray]] = queue.Queue(maxsize=self.buffer_blocks)
        self.running = threading.Event()
        self.running.set()
        self.errors: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.stats = {'compute_ms': 0., 'max_ms': 0., 'render_ms': 0., 'tcn_ms': 0.,
                      'blocks_rendered': 0, 'deadline_misses': 0, 'underflows': 0,
                      'stream_flags': 0, 'clip_blocks': 0, 'dry_dbfs': -240.,
                      'wet_dbfs': -240., 'out_dbfs': -240., 'audio_latency_ms': 0.,
                      'attention_db': 0., 'focus': 'A', 'source_az': (30., -30.), 'imu': ''}
        self.compute_times = deque(maxlen=512)
        self.prev_weight = 1.
        self.prev_attention = db_to_linear(attention_db(30.))
        self.extra_delay = np.zeros((self.block_hops * HOP, 2), dtype=np.float32)
        self.stream_handle = None
        self.worker = threading.Thread(target=self._worker, name='dsp-worker', daemon=True)
        if args.device == 'cpu' and getattr(args, 'auto_threads', True):
            candidates = [n for n in (1, 2, 4) if n <= (os.cpu_count() or 1)]
            trials = {}
            for n in candidates:
                torch.set_num_threads(n)
                trials[n] = self.check_scene(.5, report=False)
            best = min(trials, key=trials.get)
            torch.set_num_threads(best)
            print('CPU, p95 da cena por número de threads: ' +
                  ', '.join(f'{n}: {ms:.2f} ms' for n, ms in trials.items()) +
                  f'; usando {best} thread(s).', flush=True)
        self.check_scene(float(getattr(args, 'self_test_seconds', 2.)))

    def check_scene(self, seconds: float, *, report: bool = True) -> float:
        """Offline sanity check with the actual voices and fixed training geometry."""
        if seconds <= 0:
            return 0.
        if not math.isfinite(seconds) or seconds > 30:
            raise ValueError('self_test_seconds deve estar entre 0 e 30')
        count = max(12, math.ceil(seconds * FS / (self.block_hops * HOP)))
        positions = self.start_positions.copy()
        stream = NeuralStream(self.stream.net, self.stream.device)
        truth, mixed, predicted, elapsed = [], [], [], []
        try:
            for _ in range(count):
                start = time.perf_counter()
                rendered_a, rendered_b = [], []
                for _hop in range(self.block_hops):
                    chunks = []
                    for j, track in enumerate(self.audio):
                        chunks.append(track[(np.arange(HOP) + positions[j]) % len(track)])
                        positions[j] = (positions[j] + HOP) % len(track)
                    a, b = self.renderer.push(tuple(chunks), self.world_az)
                    rendered_a.append(a)
                    rendered_b.append(b)
                a, b = np.concatenate(rendered_a), np.concatenate(rendered_b)
                mix = a + b
                estimate = stream.push(mix)
                elapsed.append((time.perf_counter() - start) * 1000.)
                truth.append(np.stack((a, b), axis=1))
                mixed.append(mix)
                predicted.append(estimate)
        finally:
            # The listening experiment always begins at sample zero with clean FIR state.
            self.renderer.history.fill(0.)
            self.renderer.last_h = [None, None]
        y = np.concatenate(predicted, axis=0)
        clean = np.concatenate(truth, axis=0)
        x = np.concatenate(mixed, axis=0)
        begin, end = FFT, len(x) - HOP
        estimate, reference, baseline = y[HOP + begin:HOP + end], clean[begin:end], x[begin:end]
        error = float(np.max(np.abs(estimate.sum(axis=1) - baseline)))
        if error > 2e-5:
            raise RuntimeError(f'Teste de cena: mistura reconstruída diverge: {error:.3g}')
        p95 = float(np.percentile(elapsed[10:], 95))
        if not report:
            return p95

        def si_sdr(estimate_1d, truth_1d):
            e = estimate_1d.astype(np.float64) - float(np.mean(estimate_1d))
            s = truth_1d.astype(np.float64) - float(np.mean(truth_1d))
            energy = float(np.dot(s, s))
            if energy < 1e-10:
                return float('nan')
            p = np.dot(e, s) / energy * s
            return 10. * math.log10((np.dot(p, p) + 1e-20) /
                                    (np.dot(e - p, e - p) + 1e-20))

        print('\nDiagnóstico das vozes do João em +30°/−30° (trecho ativo, sem métrica de paper):', flush=True)
        for source in (0, 1):
            scores = []
            for ear in (0, 1):
                scores.append(si_sdr(estimate[:, source, ear], reference[:, source, ear]) -
                              si_sdr(baseline[:, ear], reference[:, source, ear]))
            raw = self.audio[source][(np.arange(len(x)) + self.start_positions[source]) % len(self.audio[source])]
            target_level = level_dbfs(reference[:, source, :])
            print(f'  Fonte {"AB"[source]}: início {self.start_positions[source]/FS:.1f} s; '
                  f'RMS seco {level_dbfs(raw):.1f} dBFS; alvo KEMAR {target_level:.1f} dBFS; '
                  f'ΔSI-SDR L/R {scores[0]:+.1f}/{scores[1]:+.1f} dB', flush=True)
            if target_level < -60:
                print(f'  AVISO: Fonte {"AB"[source]} abaixo de −60 dBFS neste trecho: '
                      'o ΔSI-SDR não garante audibilidade.', flush=True)
        print(f'  Consistência {error:.2g}; DSP no teste p95 {p95:.2f} ms '
              f'(prazo {1000*self.block_hops*HOP/FS:.2f} ms).', flush=True)
        if p95 >= 1000*self.block_hops*HOP/FS:
            print('  AVISO: p95 acima do prazo; a escuta contínua neste PC ainda não está demonstrada.', flush=True)
        return p95

    def _chunk(self, i):
        x = self.audio[i]
        indices = (np.arange(HOP) + self.positions[i]) % len(x)
        self.positions[i] = (self.positions[i] + HOP) % len(x)
        return x[indices]

    def _worker(self):
        try:
            while self.running.is_set():
                if self.queue.full():
                    time.sleep(.001)
                    continue
                t = time.perf_counter()
                q, imu = self.orientation.snapshot()
                az = tuple(float(self.core.source_relative_angle(q, p.copy()))
                           for p in self.sources)
                rendered_a, rendered_b = [], []
                for _hop in range(self.block_hops):
                    a, b = self.renderer.push((self._chunk(0), self._chunk(1)), az)
                    rendered_a.append(a)
                    rendered_b.append(b)
                a, b = np.concatenate(rendered_a), np.concatenate(rendered_b)
                mix = a + b
                rendered = time.perf_counter()
                enhanced = self.stream.push(mix)  # [block_hops*256, 2 sources, 2 ears]
                inferred = time.perf_counter()
                if self.target == 1:
                    w, focus = 1., 0
                elif self.target == 2:
                    w, focus = 0., 1
                else:
                    # A Gaussian average at yaw=0 mixes both sources equally.
                    # Select the nearest stem instead; tie resolves to A.
                    focus = 0 if abs(az[0]) <= abs(az[1]) else 1
                    w = 1. if focus == 0 else 0.
                w = .75 * self.prev_weight + .25 * w
                ramp = np.linspace(self.prev_weight, w, self.block_hops*HOP, dtype=np.float32)[:, None]
                self.prev_weight = w
                # Gaussian 0..+10 dB boost from João, applied only to the
                # selected neural image. The two virtual sources are at 1 m.
                boost_db = attention_db(az[focus])
                boost = .75 * self.prev_attention + .25 * db_to_linear(boost_db)
                boost_ramp = np.linspace(self.prev_attention, boost, self.block_hops*HOP, dtype=np.float32)[:, None]
                self.prev_attention = boost
                selected = boost_ramp * (ramp * enhanced[:, 0, :] +
                                         (1. - ramp) * enhanced[:, 1, :])
                # The notebook has a 256-sample waveform offset; a real captured
                # block cannot be played before it finishes. Add one processing batch
                # to emulate capture/scheduling delay relative to direct acoustic hearing.
                delayed = self.extra_delay
                self.extra_delay = selected.astype(np.float32)
                took = (time.perf_counter() - t) * 1000.
                with self.lock:
                    self.stats['compute_ms'] = took
                    self.stats['max_ms'] = max(self.stats['max_ms'], took)
                    self.stats['deadline_misses'] += took > (1000 * self.block_hops * HOP / FS)
                    self.stats['render_ms'] = (rendered - t) * 1000.
                    self.stats['tcn_ms'] = (inferred - rendered) * 1000.
                    self.stats['blocks_rendered'] += 1  # processing batches, not callbacks
                    self.stats['attention_db'] = boost_db
                    self.stats['focus'] = 'AB'[focus]
                    self.stats['source_az'] = az
                    self.stats['imu'] = imu
                    self.compute_times.append(took)
                for offset in range(0, len(mix), HOP):
                    self.queue.put((mix[offset:offset+HOP], delayed[offset:offset+HOP]), timeout=.2)
        except Exception as exc:
            self.errors.put(f'{type(exc).__name__}: {exc}')
            self.running.clear()

    def _callback(self, outdata, frames, _time_info, status):
        if status:
            with self.lock:
                self.stats['stream_flags'] += 1
        if frames != HOP:
            with self.lock:
                self.stats['underflows'] += 1
            outdata.fill(0)
            return
        try:
            mix, separated = self.queue.get_nowait()
        except queue.Empty:
            with self.lock:
                self.stats['underflows'] += 1
            outdata.fill(0)
            return
        # No neural inference, I/O, Qt calls or BLE operations in PortAudio callback.
        wet = self.enh_gain * separated
        dry = self.raw_gain * mix if self.raw_on else 0.
        output = wet + dry
        clipped = bool(np.max(np.abs(output)) > .95)
        np.clip(output, -.95, .95, out=outdata)
        with self.lock:
            self.stats['dry_dbfs'] = level_dbfs(mix) + 20 * math.log10(max(self.raw_gain, 1e-12)) if self.raw_on else -240.
            self.stats['wet_dbfs'] = level_dbfs(wet)
            self.stats['out_dbfs'] = level_dbfs(outdata)
            self.stats['clip_blocks'] += clipped

    def start(self, output_device=None):
        self.worker.start()
        deadline = time.monotonic() + 10.
        while self.queue.qsize() < min(12, self.buffer_blocks - 1) and self.running.is_set() and time.monotonic() < deadline:
            time.sleep(.005)
        if not self.errors.empty():
            raise RuntimeError(self.errors.get())
        self.stream_handle = sd.OutputStream(samplerate=FS, channels=2, dtype='float32',
                                             blocksize=HOP, latency=self.audio_latency,
                                             device=output_device, callback=self._callback)
        self.stream_handle.start()
        with self.lock:
            self.stats['audio_latency_ms'] = 1000. * self.stream_handle.latency

    def stop(self):
        self.running.clear()
        if self.stream_handle is not None:
            self.stream_handle.stop()
            self.stream_handle.close()
            self.stream_handle = None
        self.worker.join(timeout=2.)


class Window(QMainWindow):
    def __init__(self, engine: Engine, orientation: Orientation):
        super().__init__()
        self.engine, self.orientation = engine, orientation
        self.setWindowTitle('Fala binaural | KEMAR + TCN + IMU Tiresias')
        root = QWidget()
        vertical = QVBoxLayout(root)
        self.info = QLabel('')
        self.info.setWordWrap(True)
        vertical.addWidget(self.info)
        self.raw = QCheckBox('Ouvir fala original misturada junto com o realce')
        self.raw.setChecked(False)
        self.raw.toggled.connect(lambda x: setattr(engine, 'raw_on', bool(x)))
        vertical.addWidget(self.raw)
        form = QFormLayout()
        selector = QComboBox()
        selector.addItems(['Voz à frente (IMU)', 'Fonte A (+30° no início)', 'Fonte B (−30° no início)'])
        selector.setCurrentIndex(1)
        selector.currentIndexChanged.connect(lambda i: setattr(engine, 'target', i))
        form.addRow('Fonte separada', selector)
        self.manual = QCheckBox('Testar yaw sem IMU')
        self.manual.toggled.connect(lambda x: setattr(orientation, 'manual', bool(x)))
        form.addRow(self.manual)
        yaw = QDoubleSpinBox()
        yaw.setRange(-180., 180.)
        yaw.setSuffix('°')
        yaw.valueChanged.connect(lambda v: setattr(orientation, 'manual_yaw', v))
        form.addRow('Yaw manual', yaw)
        dry = QDoubleSpinBox()
        dry.setRange(-36., 6.)
        dry.setSingleStep(1.)
        dry.setSuffix(' dB')
        dry.setValue(-12.)
        dry.valueChanged.connect(lambda v: setattr(engine, 'raw_gain', db_to_linear(v)))
        form.addRow('Ganho misturado', dry)
        wet = QDoubleSpinBox()
        wet.setRange(-36., 6.)
        wet.setSingleStep(1.)
        wet.setSuffix(' dB')
        wet.setValue(0.)
        wet.valueChanged.connect(lambda v: setattr(engine, 'enh_gain', db_to_linear(v)))
        form.addRow('Ganho separado', wet)
        vertical.addLayout(form)
        buttons = QHBoxLayout()
        tare = QPushButton('Calibrar frente (IMU)')
        tare.clicked.connect(orientation.zero)
        buttons.addWidget(tare)
        vertical.addLayout(buttons)
        self.setCentralWidget(root)
        self.resize(640, 340)
        timer = QTimer(self)
        timer.timeout.connect(self.update_status)
        timer.start(200)
        self.timer = timer

    def update_status(self):
        if not self.engine.errors.empty():
            self.info.setText('Erro no processamento: ' + self.engine.errors.get())
            return
        with self.engine.lock:
            stats = self.engine.stats.copy()
        az_a, az_b = stats['source_az']
        far = abs(az_a - 30.) > 15. or abs(az_b + 30.) > 15.
        warning = ('ATENÇÃO: geometria fora do treino (+30°/−30°): '
                   'identidade e qualidade da separação não validadas.' if far else
                   'Geometria próxima da condição de treino.')
        self.info.setText(
            f"IMU: {stats['imu']} | azimutes A/B: {az_a:+.1f}° / {az_b:+.1f}°\n"
            f"{warning}\n"
            f"DSP último/máximo: {stats['compute_ms']:.2f}/{stats['max_ms']:.2f} ms "
            f"(prazo {1000 * self.engine.block_hops * HOP / FS:.2f} ms); ultrapassagens: {stats['deadline_misses']}; "
            f"blocos sem áudio: {stats['underflows']}; flags do driver: {stats['stream_flags']}\n"
            f"Foco {stats['focus']} (+{stats['attention_db']:.1f} dB gaussiano); "
            f"saída {stats['out_dbfs']:.1f} dBFS; blocos com corte: {stats['clip_blocks']}\n"
            f"Separado vs. direto: {(self.engine.block_hops+1)*HOP} amostras = "
            f"{1000 * (self.engine.block_hops+1)*HOP / FS:.2f} ms "
            f"programados; buffers de áudio/IMU acrescentam atraso físico. Use fones.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--joao-repo', type=Path, required=True,
                        help='Pasta clonada de joaocolombari/yaw-walk-with-me')
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--sofa', type=Path, required=True)
    parser.add_argument('--source-a', type=Path, default=None)
    parser.add_argument('--source-b', type=Path, default=None)
    parser.add_argument('--device', choices=('cpu', 'cuda:0'), default='cpu',
                        help='Use CPU primeiro para evitar jitter de transferências GPU')
    parser.add_argument('--output-device', default=None,
                        help='Nome/índice da saída; veja python -m sounddevice')
    parser.add_argument('--buffer-blocks', type=int, default=16,
                        help='Fila de blocos de 256 amostras; 16 = até 93 ms de fila')
    parser.add_argument('--block-hops', type=int, choices=(1, 2), default=1,
                        help='1 ou 2 hops de 256 amostras por chamada TCN; 2 acrescenta 5,80 ms ao atraso relativo')
    parser.add_argument('--audio-latency', choices=('low','high'), default='high')
    parser.add_argument('--self-test-seconds', type=float, default=2.,
                        help='Diagnóstico offline curto antes de iniciar a escuta; 0 desativa')
    parser.add_argument('--no-auto-threads', action='store_false', dest='auto_threads',
                        help='Não compara 1/2/4 threads no início')
    parser.add_argument('--ble-name', default=DEVICE_NAME)
    parser.add_argument('--ble-uuid', default=CHAR_UUID)
    args = parser.parse_args()
    args.source_a = args.source_a or args.joao_repo / 'laura.mp3'
    args.source_b = args.source_b or args.joao_repo / 'cooper.mp3'
    for path in (args.joao_repo / 'aar_core.py', args.checkpoint, args.sofa,
                 args.source_a, args.source_b):
        if not path.is_file():
            parser.error(f'Arquivo não encontrado: {path}')
    core = aar_core_from_repo(args.joao_repo)
    orientation = Orientation(core, args.ble_name, args.ble_uuid)
    app = QApplication.instance() or QApplication(sys.argv)
    engine = Engine(args, orientation, core)
    try:
        output = (int(args.output_device) if args.output_device is not None and
                  args.output_device.isdecimal() else args.output_device)
        engine.start(output)
        orientation.start()
        window = Window(engine, orientation)
        window.show()
        app.exec()
    finally:
        orientation.stop.set()
        engine.stop()


if __name__ == '__main__':
    main()
