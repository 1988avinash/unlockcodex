"""Real Windows warning window; receipt means rendered, not read/acknowledged."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time


class NoticeFailed(RuntimeError):pass


def validate_receipt(value,*,event_id,nonce,pid):
    if not isinstance(value,dict) or value.get('id')!=event_id or value.get('nonce')!=nonce:
        raise NoticeFailed('Warning receipt identity mismatch')
    if value.get('status')!='shown' or value.get('pid')!=pid or type(value.get('window_handle')) is not int or value['window_handle']==0:
        raise NoticeFailed('No rendered Windows warning receipt')
    if value.get('user_acknowledged') is not False:
        raise NoticeFailed('Render delivery cannot claim user acknowledgement')
    return value


class DesktopNotifier:
    def __init__(self,directory,*,timeout=12):
        self.root=Path(directory).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.timeout=timeout
        self.children=[]
        self.script=Path(__file__).with_name('Notify-Continuity.ps1')
        self.shell=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'

    def __call__(self,event):
        if not isinstance(event,dict) or not isinstance(event.get('id'),str) or len(event['id'])!=64:
            raise NoticeFailed('Bounded notice identity required')
        if any(c not in '0123456789abcdef' for c in event['id']):raise NoticeFailed('Invalid notice ID')
        if not isinstance(event.get('text'),str) or not 0<len(event['text'])<=3000:raise NoticeFailed('Bounded notice text required')
        nonce=secrets.token_hex(16)
        folder=self.root/(event['id'][:16]+'-'+nonce);folder.mkdir()
        body=dict(event,nonce=nonce)
        input_path=folder/'notice.json'
        input_path.write_text(json.dumps(body,ensure_ascii=False),encoding='utf-8')
        with (folder/'helper.log').open('wb') as log:
            proc=subprocess.Popen([str(self.shell),'-NoProfile','-STA','-File',str(self.script),'-InputPath',str(input_path)],
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(proc)
        deadline=time.monotonic()+self.timeout
        receipt_path=folder/'shown.json'
        while time.monotonic()<deadline:
            if receipt_path.is_file():
                try:value=json.loads(receipt_path.read_text(encoding='utf-8'))
                except (ValueError,OSError):time.sleep(.05);continue
                validate_receipt(value,event_id=event['id'],nonce=nonce,pid=proc.pid)
                return str(receipt_path)
            if proc.poll() is not None:raise NoticeFailed('Windows warning helper exited without render receipt')
            time.sleep(.05)
        proc.terminate();proc.wait(timeout=5)
        raise NoticeFailed('Windows warning did not render before deadline')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--test-dir',required=True);args=parser.parse_args()
    text='TEST ONLY — Codex continuity warning.\n\nThis checks that a warning is visible before switching Token Plans. No model switch, restart or paid request is happening.\n\nThis window closes automatically after 15 seconds.'
    notifier=DesktopNotifier(args.test_dir)
    receipt=notifier({'id':hashlib.sha256(('visibility-test:'+str(time.time_ns())).encode()).hexdigest(),'kind':'visibility_test','text':text})
    print(json.dumps({'receipt':receipt,'status':'rendered','user_seen':'unverified'}),flush=True)
    for child in notifier.children:child.wait(timeout=20)
