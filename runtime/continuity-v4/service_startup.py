"""Pinned, hidden, idempotent startup operator. No OAuth or inference actions."""
import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import types
import tempfile

class StartupBlocked(RuntimeError):pass

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def no_links(path):
    path=Path(path).absolute()
    if '..' in path.parts:raise StartupBlocked('Parent-relative path rejected')
    for item in [path,*path.parents]:
        if item.is_symlink() or item.exists() and getattr(item.stat(),'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise StartupBlocked('Reparse path rejected')
    return path

def strict_json(raw):
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise StartupBlocked('Duplicate JSON key')
            out[key]=value
        return out
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda value:(_ for _ in ()).throw(StartupBlocked('Non-finite JSON')))

def _pinned(pin):
    if not isinstance(pin,dict) or set(pin)!={'path','sha256'} or not re.fullmatch('[0-9a-f]{64}',pin['sha256']):raise StartupBlocked('Exact file pin required')
    path=no_links(pin['path'])
    if not path.is_file() or sha(path)!=pin['sha256']:raise StartupBlocked('Pinned file hash changed')
    return path

def load_bundle(path,expected_sha256):
    raw=no_links(path).read_bytes()
    if len(raw)>65536 or hashlib.sha256(raw).hexdigest()!=expected_sha256:raise StartupBlocked('Launch manifest hash changed')
    manifest=strict_json(raw)
    if set(manifest)!={'version','helper','service','config','pythonw'} or manifest['version']!=1:raise StartupBlocked('Exact launch manifest required')
    paths={k:_pinned(manifest[k]) for k in ('helper','service','config','pythonw')}
    if paths['helper']!=Path(__file__).absolute():raise StartupBlocked('Wrong reviewed startup helper')
    # Compile the already verified bytes directly: never load a stale .pyc.
    module=types.ModuleType('continuity_verified_startup_service');module.__file__=str(paths['service'])
    source=paths['service'].read_bytes()
    if hashlib.sha256(source).hexdigest()!=manifest['service']['sha256']:raise StartupBlocked('Service changed before import')
    exec(compile(source,str(paths['service']),'exec'),module.__dict__)
    config=module.read_config(paths['config'],manifest['config']['sha256']);pins=module.verify_config(config)
    if pins['config_sha256']!=manifest['config']['sha256']:raise StartupBlocked('Noncanonical service configuration')
    if paths['service']!=Path(config['package_root']).absolute()/'continuity-v4/production_service_v4.py':raise StartupBlocked('Service path does not belong to configured package')
    if paths['pythonw']!=Path(config['python_runtime']['path']).absolute().with_name('pythonw.exe'):raise StartupBlocked('Reviewed hidden Python must belong to service runtime')
    return {'manifest':manifest,'manifest_path':str(no_links(path)),'manifest_sha256':expected_sha256,'config':config,'pins':pins,'service':module,'config_path':str(paths['config'])}

def stage_manifest(destination,*,config_path,config_sha256,service_path,service_sha256,pythonw_path,pythonw_sha256):
    target=no_links(destination)
    manifest={'version':1,'helper':{'path':str(Path(__file__).absolute()),'sha256':sha(__file__)},
        'service':{'path':str(no_links(service_path)),'sha256':service_sha256},
        'config':{'path':str(no_links(config_path)),'sha256':config_sha256},
        'pythonw':{'path':str(no_links(pythonw_path)),'sha256':pythonw_sha256}}
    for pin in (manifest[k] for k in ('helper','service','config','pythonw')):_pinned(pin)
    raw=encoded(manifest)
    with target.open('xb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
    digest=hashlib.sha256(raw).hexdigest();load_bundle(target,digest)
    return {'manifest_path':str(target),'manifest_sha256':digest,'registered':False,'service_started':False}

def read_health(config):
    if config['host']!='127.0.0.1':raise StartupBlocked('Loopback health only')
    # Windows may take about two seconds to report an actively refused local
    # socket. Distinguish that definite absence from a genuinely unknown timeout.
    connection=http.client.HTTPConnection('127.0.0.1',config['port'],timeout=5)
    try:
        try:connection.connect()
        except ConnectionRefusedError:return None
        connection.sock.settimeout(2)
        connection.request('GET','/'+config['capability']+'/health',headers={'Connection':'close'})
        response=connection.getresponse()
        try:
            if response.status!=200:raise StartupBlocked('Unexpected health status; redirects are not followed')
            raw=response.read(65537)
            if len(raw)>65536:raise StartupBlocked('Health response exceeds bound')
            return strict_json(raw)
        finally:response.close()
    except (TimeoutError,OSError,http.client.HTTPException) as exc:raise StartupBlocked('Loopback health unavailable; do not launch over unknown listener') from exc
    finally:connection.close()

def powershell(script,value):
    executable=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-Command',script],input=encoded(value),capture_output=True,timeout=8,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if result.returncode:raise StartupBlocked('Windows ownership operation failed')
    return strict_json(result.stdout.decode('utf-8-sig'))

def process_owner(pid,bundle):
    if type(pid) is not int or pid<=0:raise StartupBlocked('Invalid process identity')
    return bundle['service'].verified_service_process(pid,bundle['config'],bundle['config_path'],
        bundle['pins']['config_sha256'],expected=bundle.get('expected_process_identity'))

def current_user_sid():
    """Read the current process token; optional launch environment is irrelevant."""
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);security=ctypes.WinDLL('advapi32',use_last_error=True)
    kernel.GetCurrentProcess.restype=w.HANDLE;kernel.CloseHandle.argtypes=[w.HANDLE];kernel.LocalFree.argtypes=[w.HLOCAL]
    security.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)];security.OpenProcessToken.restype=w.BOOL
    security.GetTokenInformation.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD)];security.GetTokenInformation.restype=w.BOOL
    security.ConvertSidToStringSidW.argtypes=[ctypes.c_void_p,ctypes.POINTER(w.LPWSTR)];security.ConvertSidToStringSidW.restype=w.BOOL
    token=w.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(),8,ctypes.byref(token)):raise StartupBlocked('Current Windows token unavailable')
    try:
        size=w.DWORD();security.GetTokenInformation(token,1,None,0,ctypes.byref(size))
        if not 0<size.value<=65536:raise StartupBlocked('Current Windows SID size invalid')
        raw=ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token,1,raw,size,ctypes.byref(size)):raise StartupBlocked('Current Windows SID unavailable')
        sid_pointer=ctypes.c_void_p.from_buffer(raw).value;text=w.LPWSTR()
        if not security.ConvertSidToStringSidW(sid_pointer,ctypes.byref(text)):raise StartupBlocked('Current Windows SID conversion failed')
        try:return text.value
        finally:kernel.LocalFree(text)
    finally:kernel.CloseHandle(token)

def mutex_name(user_sid,port):
    if not isinstance(user_sid,str) or not re.fullmatch(r'S-1-(?:\d+-)*\d+',user_sid) or not (port=='registration' or type(port) is int and 1<=port<=65535):
        raise StartupBlocked('Verified Windows SID and port required')
    return 'Local\\CodexContinuityStart-'+hashlib.sha256((user_sid+':'+str(port)).encode()).hexdigest()

@contextmanager
def journal_lock(path,*,timeout=45):
    """Windows byte-range lock also serializes the same journal across sessions."""
    import msvcrt
    path=no_links(path);stream=path.open('a+b');acquired=False
    try:
        if stream.seek(0,2)==0:stream.write(b'0');stream.flush()
        deadline=time.monotonic()+timeout
        while True:
            stream.seek(0)
            try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);acquired=True;break
            except OSError:
                if time.monotonic()>=deadline:raise StartupBlocked('Another session holds the startup journal lock')
                time.sleep(.1)
        yield
    finally:
        if acquired:stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
        stream.close()

@contextmanager
def startup_mutex(bundle):
    if os.name!='nt':raise StartupBlocked('Windows current-user startup only')
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR];kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.ReleaseMutex.argtypes=[wintypes.HANDLE];kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateMutexW(None,False,mutex_name(current_user_sid(),bundle['config']['port']))
    if not handle:raise StartupBlocked('Startup mutex unavailable')
    acquired=False
    try:
        result=kernel.WaitForSingleObject(handle,45000)
        if result not in (0,0x80):raise StartupBlocked('Another startup helper is active')
        acquired=True
        if bundle['config']['port']=='registration':
            yield;return
        service=bundle['service']
        state=Path(bundle['config']['state_dir']);journal_root=state.with_name(state.name+'-startup')
        service.private_state(journal_root,bundle['config']['account_id'])
        with journal_lock(journal_root/'startup.lock'):yield
    finally:
        if acquired:kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)

def _health_matches(health,bundle):
    if not isinstance(health,dict):raise StartupBlocked('Unexpected loopback service')
    expected={'mode':'verified_multichat_continuity_service','architecture_version':4,'port':bundle['config']['port'],**bundle['pins']}
    if any(health.get(k)!=v for k,v in expected.items()) or type(health.get('pid')) is not int or health['pid']<=0:
        raise StartupBlocked('Existing service identity/configuration differs; no duplicate launch')
    return health['pid']

def validate_health_process(health,bundle,proof):
    """Bind health to independently observed native ownership, never a truthy flag."""
    pid=_health_matches(health,bundle)
    if not isinstance(proof,dict) or proof.get('child',{}).get('pid')!=pid or not proof.get('wrapper'):
        raise StartupBlocked('Verified wrapper and health child proof required')
    reported=health.get('process_identity')
    if not isinstance(reported,dict) or any(reported.get(k)!=proof[k] for k in ('wrapper','child')):
        raise StartupBlocked('Health process evidence differs from independently observed pair')
    bundle['service'].process_module(bundle['config']).validate_self_job(proof,reported.get('job',{}))
    return proof

def ensure_running(bundle,*,health_reader,process_owner,launch,mutex,clock=time.monotonic,sleeper=time.sleep,timeout=120,read_pending=lambda:None,write_pending=lambda value:None):
    if not 1<=timeout<=120:raise StartupBlocked('Bounded startup wait required')
    with mutex(bundle):
        pending=read_pending();pid=None;status='started';expected=None;retirement=None
        if pending is not None:
            if pending.get('config_sha256')!=bundle['pins']['config_sha256'] or type(pending.get('pid')) is not int:raise StartupBlocked('Unreconciled prior startup record')
            expected=pending.get('process_identity')
            if 'service' in bundle and not isinstance(expected,dict):raise StartupBlocked('Prior startup lacks process birth evidence')
            if expected is not None:
                key='child' if pending.get('status')=='healthy' else 'wrapper'
                if pending['pid']!=expected.get(key,{}).get('pid'):raise StartupBlocked('Journal PID differs from recorded process identity')
        def owned(actual):
            proof=process_owner(actual,dict(bundle,expected_process_identity=expected))
            if isinstance(proof,dict) and proof.get('original_pair_gone') is True:
                if pending.get('status')!='healthy' or proof.get('recorded_identity')!=expected or not all(k in expected for k in ('wrapper','child')):
                    raise StartupBlocked('Only a complete healthy prior identity can be retired')
                return proof
            if isinstance(proof,dict):
                for key in ('wrapper','child'):
                    if key in (expected or {}) and proof.get(key)!=expected[key]:raise StartupBlocked('Recorded process birth/identity changed')
            return proof
        def health_owner(actual):
            proof=owned(actual)
            if isinstance(proof,dict):
                if proof.get('child',{}).get('pid')!=actual:raise StartupBlocked('Health must belong to verified base child')
                if pid is not None and pid not in (actual,proof.get('wrapper',{}).get('pid')):raise StartupBlocked('Health belongs to a different launch')
                if 'service' in bundle:
                    validate_health_process(health,bundle,proof)
            elif proof is not True or pid is not None and actual!=pid:
                raise StartupBlocked('Started service PID does not match health')
            return proof
        health=health_reader(bundle['config'])
        if health is not None:
            actual=_health_matches(health,bundle)
            pid=pending['pid'] if pending is not None else None
            proof=health_owner(actual)
            return {'status':'already_running','pid':actual,'config_sha256':bundle['pins']['config_sha256'],
                **({'process_identity':proof} if isinstance(proof,dict) else {})}
        if pending is not None:
            ownership=owned(pending['pid'])
            if ownership is False:raise StartupBlocked('Prior startup PID changed ownership; reconcile before launch')
            if isinstance(ownership,dict) and ownership.get('original_pair_gone') is True:
                retirement=ownership;status='restarted_after_verified_exit'
            elif ownership is None:raise StartupBlocked('Prior startup exit is not fully reconciled')
            else:pid=pending['pid'];status='recovered_pending_start'
        if pid is None:
            # Reserve before process creation. A helper crash between launch and
            # PID persistence remains uncertain and cannot launch a duplicate.
            write_pending({'pid':None,'config_sha256':bundle['pins']['config_sha256'],'status':'launching',
                **({'previous_journal':pending,'previous_exit_evidence':retirement} if retirement is not None else {})})
            started=launch(bundle);pid=started.get('pid')
            if type(pid) is not int or pid<=0:raise StartupBlocked('Owned launch did not return a PID')
            expected=started.get('process_identity')
            if expected is not None and expected.get('wrapper',{}).get('pid')!=pid:raise StartupBlocked('Launch wrapper identity differs')
            write_pending({'pid':pid,'config_sha256':bundle['pins']['config_sha256'],'status':'pending',
                **({'process_identity':expected} if expected is not None else {})})
        deadline=clock()+timeout
        while clock()<deadline:
            health=health_reader(bundle['config'])
            if health is not None:
                actual=_health_matches(health,bundle)
                proof=health_owner(actual)
                result={'status':status,'pid':actual,'config_sha256':bundle['pins']['config_sha256'],
                    **({'process_identity':proof} if isinstance(proof,dict) else {})}
                write_pending(dict(result,status='healthy'));return result
            sleeper(.2)
        raise StartupBlocked('Startup outcome uncertain; owned PID retained, no duplicate launch or automatic stop')

def start_verified(manifest_path,expected_sha256,*,timeout=120):
    bundle=load_bundle(manifest_path,expected_sha256)
    if os.name!='nt' or Path(sys.executable).resolve()!=Path(bundle['manifest']['pythonw']['path']).resolve():
        raise StartupBlocked('Launch through the reviewed hidden Python executable')
    service=bundle['service'];identity=service.process_module(bundle['config'])
    base=Path(service.python_base_pin(bundle['config'])['path']).with_name('pythonw.exe')
    base_pin={'path':str(base),'sha256':bundle['config']['external_files'][str(base)]}
    current=identity.process_identity(os.getpid())
    expected_args=['-I','-S','-B',str(Path(__file__).absolute()),'--manifest',str(no_links(manifest_path)),'--manifest-sha256',expected_sha256]
    if '--timeout' in sys.argv:expected_args+=['--timeout',str(timeout)]
    identity.require_detached_venv_self(bundle['manifest']['pythonw'],base_pin,expected_args=expected_args,
        user_sid=current['user_sid'],session_id=current['session_id'])
    state=Path(bundle['config']['state_dir']);journal_root=state.with_name(state.name+'-startup')
    def journal():
        bundle['service'].private_state(journal_root,bundle['config']['account_id'])
        return no_links(journal_root/'owned-startup.json')
    def read_pending():
        path=journal()
        return strict_json(path.read_bytes()) if path.exists() else None
    def write_pending(value):
        path=journal()
        if path.exists():
            previous=path.read_bytes();history=no_links(path.parent/'history');history.mkdir(exist_ok=True)
            archive=no_links(history/(hashlib.sha256(previous).hexdigest()+'.json'))
            if archive.exists():
                if archive.read_bytes()!=previous:raise StartupBlocked('Immutable startup journal archive differs')
            else:
                with archive.open('xb') as old:old.write(previous);old.flush();os.fsync(old.fileno())
        with tempfile.NamedTemporaryFile(mode='wb',dir=path.parent,delete=False) as out:
            out.write(encoded(value));out.flush();os.fsync(out.fileno());temporary=Path(out.name)
        no_links(path);os.replace(temporary,path)
    return ensure_running(bundle,health_reader=read_health,process_owner=process_owner,
        launch=lambda b:b['service'].launch_hidden(b['config_path'],b['pins']['config_sha256']),
        mutex=startup_mutex,read_pending=read_pending,write_pending=write_pending,timeout=timeout)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--manifest',required=True);parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--check',action='store_true');parser.add_argument('--timeout',type=int,default=120);args=parser.parse_args()
    if args.check:
        checked=load_bundle(args.manifest,args.manifest_sha256);result={'verified':True,'config_sha256':checked['pins']['config_sha256'],'service_started':False}
    else:result=start_verified(args.manifest,args.manifest_sha256,timeout=args.timeout)
    if sys.stdout is not None:print(json.dumps(result))
