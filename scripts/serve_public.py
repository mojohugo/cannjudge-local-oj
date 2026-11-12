"""Start the OJ and a temporary HTTPS tunnel together (no account required)."""
import argparse
import json
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oj.config import load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    migration = ROOT/'var/remote-deployment.json'
    if migration.is_file():
        entry=json.loads(migration.read_text(encoding='utf-8'))
        print('OJ and MCP now run on the configured remote server: '+entry['public_url'])
        print('MCP URL: '+entry['mcp_url'])
        print('No local evaluation service or additional tunnel was started.')
        return
    settings = load_settings()
    local = f'http://127.0.0.1:{args.port}'
    try:
        with socket.create_connection(('127.0.0.1', args.port), timeout=2):
            print(f'Port {args.port} is already in use. Keep the running service, or close its window first.')
            if settings.public_origin:
                print('Configured MCP URL (check that its tunnel is still running): '+settings.public_origin+'/mcp')
            return
    except OSError:
        pass
    bundled = ROOT/'tools/cloudflared.exe'
    executable = bundled if sys.platform=='win32' and bundled.is_file() else shutil.which('cloudflared')
    if not executable:
        raise SystemExit('Install cloudflared in PATH (or tools/cloudflared.exe on Windows)')
    tunnel = server = None
    stamp = time.strftime('%Y%m%d-%H%M%S')
    with (settings.state_dir/f'cloudflared-{stamp}.log').open('w',encoding='utf-8') as tunnel_log, \
            (settings.state_dir/f'web-{stamp}.log').open('w',encoding='utf-8') as server_log:
        try:
            tunnel = subprocess.Popen([str(executable),'tunnel','--url',local,'--protocol','http2',
                '--no-autoupdate'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                text=True,encoding='utf-8',errors='replace')
            events = queue.Queue(maxsize=256)

            def read_tunnel():
                for line in tunnel.stdout:
                    tunnel_log.write(line)
                    tunnel_log.flush()
                    try:
                        events.put_nowait(line)
                    except queue.Full:
                        pass

            threading.Thread(target=read_tunnel,daemon=True).start()
            origin, connected = None, False
            deadline = time.monotonic()+120
            print('Connecting HTTPS tunnel...',flush=True)
            while time.monotonic() < deadline and tunnel.poll() is None:
                try:
                    line = events.get(timeout=1)
                except queue.Empty:
                    continue
                found = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com\b',line)
                if found:
                    origin = found.group(0)
                connected |= 'Registered tunnel connection' in line
                if origin and connected:
                    break
            if not (origin and connected):
                raise RuntimeError('Tunnel did not connect; inspect var/cloudflared-'+stamp+'.log')
            config = settings.state_dir/'config.json'
            raw = config.read_bytes()
            values = json.loads(raw)
            old_host = urlparse(values.get('public_origin') or '').hostname
            hosts = [h for h in values['allowed_hosts'] if h != old_host or h in ('localhost','127.0.0.1','[::1]')]
            values.update(public_origin=origin,local_origin=local,
                          allowed_hosts=list(dict.fromkeys(hosts+[urlparse(origin).hostname])))
            (settings.state_dir/'config.before-public-start.json').write_bytes(raw)
            temporary = config.with_suffix('.tmp')
            temporary.write_text(json.dumps(values,indent=2)+'\n',encoding='utf-8')
            temporary.replace(config)
            server = subprocess.Popen([sys.executable,'-X','utf8','-m','uvicorn','oj.app:create_app',
                '--factory','--host','127.0.0.1','--port',str(args.port),'--workers','1',
                '--no-proxy-headers','--no-access-log'],cwd=ROOT,stdout=server_log,stderr=subprocess.STDOUT)
            (settings.state_dir/'server.pid').write_text(str(server.pid))
            ready = False
            for _ in range(30):
                if server.poll() is not None:
                    break
                try:
                    with urlopen(local+'/api/health',timeout=2) as response:
                        ready = response.status == 200
                    if ready:
                        break
                except OSError:
                    time.sleep(1)
            if not ready:
                raise RuntimeError('OJ startup failed; inspect var/web-'+stamp+'.log')
            entry = dict(origin=origin,mcp_url=origin+'/mcp',local_url=local,
                         server_pid=server.pid,tunnel_pid=tunnel.pid,temporary=True)
            (settings.state_dir/'public-mcp.json').write_text(json.dumps(entry,indent=2)+'\n')
            (settings.state_dir/'public-mcp-url.txt').write_text(origin+'/mcp\n')
            print('MCP URL: '+origin+'/mcp',flush=True)
            print('Auth: OAuth / DCR / oj:access. Credentials: var/admin-access.txt',flush=True)
            print('Keep this window open. Restarting the tunnel changes its URL; reconnect in ChatGPT.',flush=True)
            while tunnel.poll() is None and server.poll() is None:
                time.sleep(2)
            raise RuntimeError('OJ or tunnel exited; inspect the logs. The tunnel URL may no longer work.')
        except KeyboardInterrupt:
            print('Stopping OJ and public tunnel.')
        finally:
            for child in (server,tunnel):
                if child and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()


if __name__ == '__main__':
    main()
