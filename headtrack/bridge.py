"""HeadTrack WebSocket bridge: mobile orientation, local Python TCN/audio.

Run: python bridge.py --demo-dir PATH --joao-repo PATH --checkpoint PATH --sofa PATH
Start cloudflared separately: cloudflared tunnel --url http://localhost:8765
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import math
import secrets
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from websockets.asyncio.server import serve

HOP = 256
FS = 44100


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo-dir', type=Path,
                        default=Path(__file__).resolve().parent / 'engine',
                        help='Pasta do motor Python; por padrão headtrack/engine do clone')
    parser.add_argument('--joao-repo', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--sofa', type=Path, required=True)
    parser.add_argument('--source-a', type=Path)
    parser.add_argument('--source-b', type=Path)
    parser.add_argument('--device', choices=('cpu', 'cuda:0'), default='cpu')
    parser.add_argument('--output-device')
    parser.add_argument('--buffer-blocks', type=int, default=16,
                        help='Fila em blocos de 256 amostras (3..128; padrão 16)')
    parser.add_argument('--block-hops', type=int, choices=(1, 2), default=1,
                        help='2 agrupa duas janelas na TCN (prazo 11,61 ms; +5,80 ms de atraso relativo)')
    parser.add_argument('--audio-latency', choices=('low', 'high'), default='high')
    parser.add_argument('--self-test-seconds', type=float, default=2.,
                        help='Diagnóstico offline das vozes antes de iniciar o áudio')
    parser.add_argument('--no-auto-threads', action='store_false', dest='auto_threads',
                        help='Não compara 1/2/4 threads antes do áudio')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--origin', default='https://brianlahr.netlify.app',
                        help='Origem HTTPS exata da página que acessará o WebSocket')
    args = parser.parse_args()
    if not (args.demo_dir / 'run.py').is_file():
        parser.error('Em --demo-dir, indique a pasta extraída que contém run.py')
    for path in (args.joao_repo / 'aar_core.py', args.checkpoint, args.sofa):
        if not path.is_file():
            parser.error('Arquivo não encontrado: ' + str(path))
    sys.path.insert(0, str(args.demo_dir.resolve()))
    from run import Engine, Orientation, aar_core_from_repo, db_to_linear

    args.source_a = args.source_a or args.joao_repo / 'laura.mp3'
    args.source_b = args.source_b or args.joao_repo / 'cooper.mp3'
    core = aar_core_from_repo(args.joao_repo)
    orientation = Orientation(core, 'web-phone', 'unused')
    # Web controls the yaw; do not start a BLE scan.
    with orientation.lock:
        orientation.manual = True
        orientation.manual_yaw = 0.
    engine = Engine(args, orientation, core)
    engine.raw_on = False
    engine.enh_gain = 0.
    output = (int(args.output_device) if args.output_device and
              args.output_device.isdecimal() else args.output_device)
    engine.start(output)
    token = secrets.token_urlsafe(32)
    last_sensor = [time.monotonic()]
    last_control = [time.monotonic()]
    guard = asyncio.Lock()

    async def handle(ws):
        if ws.request.path != '/ws':
            await ws.close(code=1008, reason='path')
            return
        try:
            auth = json.loads(await asyncio.wait_for(ws.recv(), 8))
            if not isinstance(auth, dict) or auth.get('type') != 'auth' or not isinstance(auth.get('token'), str) or not hmac.compare_digest(auth['token'], token):
                await ws.close(code=1008, reason='invalid token')
                return
        except (ValueError, TypeError, asyncio.TimeoutError):
            await ws.close(code=1008, reason='invalid auth')
            return

        # One active controller: reject a second device instead of allowing races.
        if guard.locked():
            await ws.close(code=1008, reason='controller already connected')
            return
        async with guard:
            await ws.send(json.dumps({'type':'ready'}))
            async def status():
                while True:
                    await asyncio.sleep(.5)
                    with engine.lock:
                        data = engine.stats.copy()
                        timings = list(engine.compute_times)
                    timings.sort()
                    p95 = timings[min(len(timings)-1, int(.95*len(timings)))] if timings else 0.
                    if not engine.running.is_set():
                        await ws.send(json.dumps({'type':'error','message':'Processamento de áudio interrompido'}))
                        return
                    await ws.send(json.dumps({
                        'type':'status',
                        'compute_ms':data['compute_ms'],
                        'p95_ms':p95,
                        'max_ms':data['max_ms'],
                        'render_ms':data['render_ms'],
                        'tcn_ms':data['tcn_ms'],
                        'deadline_ms':1000*engine.block_hops*HOP/FS,
                        'relative_delay_ms':1000*(engine.block_hops+1)*HOP/FS,
                        'block_hops':engine.block_hops,
                        'blocks_rendered':data['blocks_rendered'],
                        'deadline_misses':data['deadline_misses'],
                        'underflows':data['underflows'],
                        'stream_flags':data['stream_flags'],
                        'queue_blocks':engine.queue.qsize(),
                        'audio_latency_ms':data['audio_latency_ms'],
                        'dry_dbfs':data['dry_dbfs'],
                        'wet_dbfs':data['wet_dbfs'],
                        'out_dbfs':data['out_dbfs'],
                        'clip_blocks':data['clip_blocks'],
                        'focus':data['focus'],
                        'attention_db':data['attention_db'],
                        'sensor_age_ms':(time.monotonic()-last_sensor[0])*1000,
                        'control_age_ms':(time.monotonic()-last_control[0])*1000
                    }))
            status_task = asyncio.create_task(status())
            try:
                async for payload in ws:
                    if not isinstance(payload, str) or len(payload) > 1024:
                        await ws.close(code=1009, reason='oversize')
                        break
                    try:
                        data = json.loads(payload)
                        if not isinstance(data, dict) or data.get('type') != 'control':
                            continue
                        yaw = float(data['yaw'])
                        if 'raw_gain_db' in data and 'enh_gain_db' in data:
                            dry_db = float(data['raw_gain_db'])
                            wet_db = float(data['enh_gain_db'])
                            if not all(math.isfinite(v) and -36 <= v <= 6 for v in (dry_db, wet_db)):
                                continue
                            dry, wet = db_to_linear(dry_db), db_to_linear(wet_db)
                        else:
                            # Old cached web page during deployment transition.
                            dry, wet = float(data['raw_gain']), float(data['enh_gain'])
                        target = int(data['target'])
                        if not all(math.isfinite(v) for v in (yaw,dry,wet)) or not (-180 <= yaw <= 180 and 0 <= dry <= db_to_linear(6) and 0 <= wet <= db_to_linear(6) and target in (0,1,2)):
                            continue
                        with orientation.lock:
                            orientation.manual_yaw = yaw
                        engine.raw_on = data['raw_on'] is True
                        engine.raw_gain = dry
                        engine.enh_gain = wet
                        engine.target = target
                        now = time.monotonic()
                        last_control[0] = now
                        sensor_age = float(data.get('sensor_age_ms', 0.))
                        if math.isfinite(sensor_age) and 0 <= sensor_age <= 600000:
                            last_sensor[0] = now - sensor_age / 1000.
                    except (KeyError, ValueError, TypeError):
                        continue
            finally:
                status_task.cancel()
                # Keep last orientation on disconnect; mute audio until user reconnects.
                engine.raw_on = False
                engine.enh_gain = 0.

    print('\nÁudio no PC iniciado. Porta local:', args.port, flush=True)
    print('CÓDIGO DA SESSÃO (digite somente no celular):', token, flush=True)
    print('Abra OUTRO PowerShell: cloudflared tunnel --url http://localhost:' + str(args.port), flush=True)
    print('Cole a URL https://…trycloudflare.com e o código em https://brianlahr.netlify.app/headtrack/', flush=True)
    print('Não publique o código da sessão. Ctrl+C encerra o áudio e o servidor.\n', flush=True)
    try:
        async with serve(handle, '127.0.0.1', args.port, origins=[args.origin],
                         max_size=2048, compression=None):
            await asyncio.Future()
    finally:
        engine.stop()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
