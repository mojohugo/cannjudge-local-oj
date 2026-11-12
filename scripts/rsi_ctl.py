"""Local RSI control client. JSON on stdin; credentials never enter agent output."""
import json
import os
from pathlib import Path
import sys
import urllib.request


def main():
    body=json.load(sys.stdin)
    if os.environ.get('RSI_TASK_ID'):
        body['actor_task_id']=os.environ['RSI_TASK_ID']
    key=Path('/etc/cann-oj-rsi/token').read_text().strip()
    request=urllib.request.Request('http://127.0.0.1:8776/rpc',data=json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=30) as response:
        print(json.dumps(json.load(response),ensure_ascii=False))


if __name__=='__main__':main()
