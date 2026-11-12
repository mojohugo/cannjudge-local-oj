"""Upload trusted worker files through the existing SSH alias. Never submits online."""
from pathlib import Path
import argparse
import json
import shlex
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from oj.config import load_settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',help='Named backend in config/backends.json; does not switch the live service')
    args=parser.parse_args()
    settings = load_settings()
    path = ROOT/'config/backends.json'
    if not path.is_file():
        parser.error('Copy config/backends.example.json to config/backends.json and configure your worker first')
    backends=json.loads(path.read_text(encoding='utf-8'))
    name = args.backend or settings.ssh_host
    if name not in backends:
        parser.error('Backend is not configured: '+name)
    backend=backends[name]
    required = {'ssh_host','root','jobs','lock','execution_user','isolation','env_script'}
    if required - backend.keys():
        parser.error('Backend lacks fields: '+', '.join(sorted(required-backend.keys())))
    if backend['isolation'] not in {'separate_uid','trusted_user'}:
        parser.error('isolation must be separate_uid or trusted_user')
    host=backend['ssh_host']
    stage = backend['root']+'.install-' + uuid.uuid4().hex
    ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', host]
    subprocess.run(ssh + ['mkdir -m 755 ' + shlex.quote(stage)], check=True)
    paths = [str(p) for p in (ROOT / 'remote').iterdir() if p.name != '__pycache__']
    subprocess.run(['scp', '-r', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', *paths, host + ':' + stage + '/'], check=True)
    subprocess.run(['scp', '-r', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', str(ROOT / 'problems'), host + ':' + stage + '/'], check=True)
    if backend:
        with tempfile.TemporaryDirectory(prefix='oj-worker-config-') as local:
            config_path=Path(local)/'worker-config.json'
            config_path.write_text(json.dumps(backend,ensure_ascii=False,indent=2),encoding='utf-8')
            subprocess.run(['scp','-q',str(config_path),host+':'+stage+'/worker-config.json'],check=True)
        # Use the administrator's CANN environment; do not guess a SDK location.
        subprocess.run(ssh+['cp '+shlex.quote(backend['env_script'])+' '+shlex.quote(stage+'/env.sh')],check=True)
        from oj.problem import problem
        for identity,data in backend.get('datasets',{}).items():
            problem(identity)  # Validate ID against the installed problem registry.
            data_root=backend['root']+'/problems/'+identity
            subprocess.run(ssh+['mkdir -p '+shlex.quote(data_root)],check=True)
            source=subprocess.Popen(['ssh','-T',data['ssh_host'],
                'tar -C '+shlex.quote(data['path'])+' -czf - .'],stdout=subprocess.PIPE)
            target=subprocess.Popen(ssh+['mkdir -p '+shlex.quote(data_root+'/data')+' && tar -xzf - -C '+shlex.quote(data_root+'/data')+' --no-same-owner'],stdin=source.stdout)
            source.stdout.close();target.wait();source.wait()
            if source.returncode or target.returncode:raise RuntimeError('Dataset transfer failed')
        if backend['isolation']=='trusted_user':
            subprocess.run(ssh+['python3 '+shlex.quote(stage+'/install_user.py')],check=True)
            subprocess.run([sys.executable,str(ROOT/'scripts/enroll_ssh.py'),'--backend',args.backend or settings.ssh_host],check=True)
        else:
            subprocess.run(ssh+['python3 '+shlex.quote(stage+'/bootstrap.py')],check=True)
    else:
        subprocess.run(ssh + ['python3 ' + stage + '/bootstrap.py'], check=True)
    print('Worker installed. Existing optimization source and results were not modified.')


if __name__ == '__main__':
    main()
