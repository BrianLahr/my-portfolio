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
    parser.add_argument('--demo-dir', type=Path, required=True,
                        help='Pasta extraída imu_binaural_demo (contém run.py)')
    parser.add_argument('--joao-repo', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--sofa', type=Path, required=True)
    parser.add_argument('--source-a', type=Path)
    parser.add_argument('--source-b', type=Path)
    parser.add_argument('--device', choices=('cpu', 'cuda:0'), default='cpu')
    parser.add_argument('--output-device')
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
    from run import Engine, Orientation, aar_core_from_repo

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
                    if not engine.running.is_set():
                        await ws.send(json.dumps({'type':'error','message':'Processamento de áudio interrompido'}))
                        return
                    await ws.send(json.dumps({
                        'type':'status',
                        'compute_ms':data['compute_ms'],
                        'max_ms':data['max_ms'],
                        'deadline_ms':1000*HOP/FS,
                        'deadline_misses':data['deadline_misses'],
                        'underflows':data['underflows'],
                        'sensor_age_ms':(time.monotonic()-last_sensor[0])*1000
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
                        dry = float(data['raw_gain'])
                        wet = float(data['enh_gain'])
                        target = int(data['target'])
                        if not all(math.isfinite(v) for v in (yaw,dry,wet)) or not (-180 <= yaw <= 180 and 0 <= dry <= 1 and 0 <= wet <= 1 and target in (0,1,2)):
                            continue
                        with orientation.lock:
                            orientation.manual_yaw = yaw
                        engine.raw_on = data['raw_on'] is True
                        engine.raw_gain = dry
                        engine.enh_gain = wet
                        engine.target = target
                        last_sensor[0] = time.monotonic()
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
