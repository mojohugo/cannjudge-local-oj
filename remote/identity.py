"""Persistent worker identity; signs fresh challenges and this SSH host's keys."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from host import ROOT, CONFIG

KEY = ROOT / '.worker-identity.key'


def private_key(create=False):
    if create and not KEY.exists():
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        with os.fdopen(os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(raw)
    return Ed25519PrivateKey.from_private_bytes(KEY.read_bytes())


def main():
    if len(sys.argv) != 2:
        raise ValueError('Expected --enroll or a fresh 64-character hex nonce')
    nonce = sys.argv[1]
    enroll = nonce == '--enroll'
    if not enroll and not re.fullmatch('[0-9a-f]{64}', nonce):
        raise ValueError('Invalid challenge')
    key = private_key(create=enroll)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if enroll:
        print(json.dumps(dict(version=1, root=str(ROOT), public_key=base64.b64encode(public).decode())))
        return
    host_keys = []
    paths = CONFIG.get('ssh_host_public_keys', ['/etc/ssh/ssh_host_rsa_key.pub', '/etc/ssh/ssh_host_ed25519_key.pub'])
    for name in paths:
        path = Path(name)
        if path.is_file():
            kind, value, *_ = path.read_text().split()
            if kind.startswith(('ssh-', 'ecdsa-')):
                host_keys.append([kind, value])
    if not host_keys:
        raise RuntimeError('No configured SSH host public key is readable')
    stat = Path('/proc/1/stat').read_text().rsplit(')', 1)[1].split()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + stat[19]
    payload = dict(version=1, nonce=nonce, root=str(ROOT), host_keys=host_keys, boot_id=boot,
                   server_id=hashlib.sha256(public).hexdigest())
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    print(json.dumps(dict(payload=payload, signature=base64.b64encode(key.sign(encoded)).decode())))


if __name__ == '__main__':
    main()
