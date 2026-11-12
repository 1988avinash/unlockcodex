"""Stage one current-user Startup shortcut; explicit ownership-CAS activation only.

Existing entries are never replaced. Updating requires uninstalling the exact
owned predecessor first; its immutable staged link remains the rollback source.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import stat

class RegistrationBlocked(RuntimeError):pass
NAME='Codex Continuity Guard.lnk'
def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(v):return hashlib.sha256(v).hexdigest()

def no_links(path):
    path=Path(path).absolute()
    if '..' in path.parts:raise RegistrationBlocked('Parent-relative path rejected')
    for item in (path,*path.parents):
        if item.is_symlink() or item.exists() and getattr(item.stat(),'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RegistrationBlocked('Reparse path rejected')
    return path

def _ownership(plan):
    path=Path(plan['ownership_receipt'])
    if not path.exists():return None
    value=json.loads(path.read_bytes())
    if value.get('plan_sha256')!=digest(encoded(plan)) or value.get('target')!=plan['target']:
        raise RegistrationBlocked('Ownership receipt differs; no startup mutation')
    return value

def _save_owner(plan,status):
    path=Path(plan['ownership_receipt']);value={'purpose':plan['purpose'],'plan_sha256':digest(encoded(plan)),
        'target':plan['target'],'installed_sha256':plan['staged_sha256'],'status':status}
    with tempfile.NamedTemporaryFile(mode='wb',dir=path.parent,delete=False) as out:
        out.write(encoded(value));out.flush();os.fsync(out.fileno());temporary=out.name
    os.replace(temporary,path)
    return value

def install(plan,*,backend,verify):
    verify(plan)
    with backend.lock():
        data=Path(plan['staged_link']).read_bytes()
        if digest(data)!=plan['staged_sha256']:raise RegistrationBlocked('Staged shortcut changed')
        owner=_ownership(plan);existing=backend.read(plan['target'])
        if existing is not None:
            if existing==data and owner and owner['status'] in ('installing','installed'):
                _save_owner(plan,'installed');return {'status':'already_installed','service_started':False}
            raise RegistrationBlocked('Startup entry exists without matching ownership; untouched')
        _save_owner(plan,'installing')
        backend.create_exclusive(plan['target'],data)
        try:
            if backend.read(plan['target'])!=data:raise RegistrationBlocked('Installed shortcut readback differs')
            _save_owner(plan,'installed')
        except Exception:
            # Roll back only the exact bytes this transaction created.
            backend.remove_exact(plan['target'],data)
            raise
        return {'status':'installed','service_started':False}

def uninstall(plan,*,backend,verify):
    verify(plan)
    with backend.lock():
        owner=_ownership(plan)
        if owner is None:raise RegistrationBlocked('No ownership receipt; untouched')
        existing=backend.read(plan['target'])
        if existing is None:
            _save_owner(plan,'removed');return {'status':'already_absent','service_stopped':False}
        expected=Path(plan['staged_link']).read_bytes()
        if digest(expected)!=plan['staged_sha256'] or existing!=expected:raise RegistrationBlocked('Startup entry changed; untouched')
        backend.remove_exact(plan['target'],expected);_save_owner(plan,'removed')
        return {'status':'removed','service_stopped':False}

class WindowsStartupBackend:
    def __init__(self,helper):self.helper=helper
    def directory(self):
        value=self.helper.powershell("@{path=[Environment]::GetFolderPath('Startup')}|ConvertTo-Json -Compress",{})
        path=self.helper.no_links(value['path'])
        if not path.is_dir():raise RegistrationBlocked('Current-user Startup folder unavailable')
        return path
    def lock(self):return self.helper.startup_mutex({'config':{'port':'registration'}})
    def read(self,path):
        path=self.helper.no_links(path)
        if not path.exists():return None
        data=path.read_bytes()
        if len(data)>1024*1024:raise RegistrationBlocked('Shortcut exceeds bound')
        return data
    def create_exclusive(self,path,data):
        path=self.helper.no_links(path)
        with tempfile.NamedTemporaryFile(mode='wb',dir=path.parent,suffix='.tmp',delete=False) as out:
            out.write(data);out.flush();os.fsync(out.fileno());temporary=Path(out.name)
        try:
            if os.name!='nt':raise RegistrationBlocked('Windows atomic no-overwrite rename required')
            os.rename(temporary,path)  # On Windows destination existence fails atomically.
        finally:
            if temporary.exists():temporary.unlink()
    def remove_exact(self,path,expected):
        delete_exact_file(self.helper.no_links(path),expected)
    def shortcut(self,path,*,target,arguments,cwd):
        script="$s=[Console]::In.ReadToEnd()|ConvertFrom-Json; $w=New-Object -ComObject WScript.Shell; $l=$w.CreateShortcut($s.path); $l.TargetPath=$s.target; $l.Arguments=$s.arguments; $l.WorkingDirectory=$s.cwd; $l.WindowStyle=7; $l.Description='Verified current-user Codex Continuity Guard'; $l.Save(); $r=$w.CreateShortcut($s.path); @{target=$r.TargetPath;arguments=$r.Arguments;cwd=$r.WorkingDirectory}|ConvertTo-Json -Compress"
        result=self.helper.powershell(script,{'path':str(path),'target':target,'arguments':arguments,'cwd':cwd})
        if result!={'target':target,'arguments':arguments,'cwd':cwd}:raise RegistrationBlocked('Shortcut COM readback differs')

def delete_exact_file(path,expected):
    """Windows compare-and-delete on one locked file handle; no replacement race."""
    if os.name!='nt':raise RegistrationBlocked('Windows ownership-CAS deletion required')
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.ReadFile.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p];kernel.ReadFile.restype=wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD];kernel.SetFileInformationByHandle.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(Path(path).absolute()),0x80000000|0x10000,1,None,3,0x00200000|0x80,None)
    if handle==ctypes.c_void_p(-1).value:raise RegistrationBlocked('Cannot exclusively own startup entry')
    try:
        data=bytearray();buffer=ctypes.create_string_buffer(65536);count=wintypes.DWORD()
        while True:
            if not kernel.ReadFile(handle,buffer,65536,ctypes.byref(count),None):raise RegistrationBlocked('Owned entry read failed')
            if not count.value:break
            data.extend(buffer.raw[:count.value])
            if len(data)>1024*1024:raise RegistrationBlocked('Owned entry exceeds bound')
        if bytes(data)!=expected:raise RegistrationBlocked('Startup entry changed; untouched')
        delete=wintypes.BOOLEAN(1)
        if not kernel.SetFileInformationByHandle(handle,4,ctypes.byref(delete),ctypes.sizeof(delete)):
            raise RegistrationBlocked('Owned entry could not be removed')
    finally:kernel.CloseHandle(handle)

def stage_registration(bundle,directory,*,helper,backend=None):
    backend=backend or WindowsStartupBackend(helper)
    stage=helper.no_links(directory);stage.mkdir(parents=True,exist_ok=False)
    bundle['service'].private_state(stage,bundle['config']['account_id'])
    target=backend.directory()/NAME
    if backend.read(target) is not None:raise RegistrationBlocked('Existing startup entry must be reconciled/uninstalled explicitly first')
    manifest=bundle['manifest'];staged=stage/NAME
    args=subprocess.list2cmdline(['-I','-S','-B',manifest['helper']['path'],'--manifest',bundle['manifest_path'],'--manifest-sha256',bundle['manifest_sha256']])
    backend.shortcut(staged,target=manifest['pythonw']['path'],arguments=args,cwd=str(Path(manifest['helper']['path']).parent))
    plan={'purpose':'codex_continuity_startup','version':1,'target':str(target),'staged_link':str(staged),'staged_sha256':digest(staged.read_bytes()),
        'ownership_receipt':str(stage/'ownership.json'),'launch_manifest':bundle['manifest_path'],'launch_sha256':bundle['manifest_sha256'],'helper':manifest['helper']}
    raw=encoded(plan);path=stage/'registration-plan.json'
    with path.open('xb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
    return {'plan_path':str(path),'plan_sha256':digest(raw),'registered':False,'service_started':False}

def activate(plan_path,expected_sha256,*,action):
    if action not in ('install','uninstall'):raise RegistrationBlocked('Explicit install or uninstall required')
    raw=no_links(plan_path).read_bytes()
    if len(raw)>65536 or digest(raw)!=expected_sha256:raise RegistrationBlocked('Reviewed registration plan changed')
    plan=json.loads(raw)
    if set(plan)!={'purpose','version','target','staged_link','staged_sha256','ownership_receipt','launch_manifest','launch_sha256','helper'}:
        raise RegistrationBlocked('Exact registration plan required')
    pin=plan['helper'];source=no_links(pin['path']).read_bytes()
    if digest(source)!=pin['sha256']:raise RegistrationBlocked('Reviewed startup helper changed')
    helper=types.ModuleType('reviewed_startup_operator');helper.__file__=pin['path'];exec(compile(source,pin['path'],'exec'),helper.__dict__)
    backend=WindowsStartupBackend(helper)
    def verify(value):
        if value.get('purpose')!='codex_continuity_startup' or value.get('version')!=1:raise RegistrationBlocked('Wrong registration purpose')
        if str(backend.directory()/NAME)!=value['target']:raise RegistrationBlocked('Not the exact current-user startup entry')
        for field in ('staged_link','ownership_receipt'):helper.no_links(value[field])
        # Removal needs the reviewed ownership proof, not a still-runnable service.
        # This permits safe rollback after service/config drift without loading it.
        if action=='install':
            bundle=helper.load_bundle(value['launch_manifest'],value['launch_sha256'])
            if bundle['manifest']['helper']!=value['helper']:raise RegistrationBlocked('Helper manifest mismatch')
    if action=='install':return install(plan,backend=backend,verify=verify)
    if action=='uninstall':return uninstall(plan,backend=backend,verify=verify)
    raise RegistrationBlocked('Explicit install or uninstall required')

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--plan',required=True);parser.add_argument('--plan-sha256',required=True)
    parser.add_argument('--action',choices=['install','uninstall'],required=True);args=parser.parse_args()
    print(json.dumps(activate(args.plan,args.plan_sha256,action=args.action)))
