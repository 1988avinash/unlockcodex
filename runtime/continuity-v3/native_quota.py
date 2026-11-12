"""Read quota through the installed native app-server, with memory-only auth.

No thread is started, no inference is submitted, and no real auth file is read.
Normal operation uses only the fixed native official backend. Test overrides are
loopback-only and reject non-synthetic credentials. Errors never include native
stderr, tokens, RPC parameters, or backend response text.
"""
import ctypes
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit


class NativeQuotaError(RuntimeError):
    pass


def _check_private_path(path):
    if '..' in path.parts:
        raise NativeQuotaError('invalid_isolation_root')
    for item in [path,*path.parents]:
        if item.is_symlink() or item.exists() and getattr(item.stat(),'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise NativeQuotaError('isolation_reparse_path_rejected')


def qualified_executable(explicit=None):
    """Select the packaged Desktop runtime, never an arbitrary PATH executable."""
    candidate=explicit or os.environ.get('CODEX_APP_RUNTIME_CLI_PATH')
    if candidate is None:
        candidates=list(Path('C:/Program Files/WindowsApps').glob('OpenAI.Codex_*/app/resources/codex.exe'))
        if not candidates and os.name=='nt':
            ps=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
            try:
                loc=subprocess.run([str(ps),'-NoProfile','-NonInteractive','-Command',
                    '(Get-AppxPackage OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1 -ExpandProperty InstallLocation)'],
                    capture_output=True,text=True,timeout=8,creationflags=subprocess.CREATE_NO_WINDOW,check=True).stdout.strip()
                if loc:
                    exe=Path(loc)/'app/resources/codex.exe'
                    if exe.is_file():candidates=[exe]
            except (OSError,subprocess.SubprocessError):
                pass
        if not candidates: raise NativeQuotaError('bundled_runtime_unavailable')
        def version(path):
            match=re.search(r'OpenAI\.Codex_([0-9.]+)_',str(path),re.I)
            return tuple(int(n) for n in match[1].split('.')) if match else ()
        candidate=max(candidates,key=version)
    path=Path(candidate).resolve()
    pattern=r'C:/Program Files/WindowsApps/OpenAI\.Codex_[0-9.]+_(?:x64|arm64)__[A-Za-z0-9]+/app/resources/codex\.exe'
    if not re.fullmatch(pattern,path.as_posix(),re.I) or not path.is_file():
        raise NativeQuotaError('unqualified_runtime_path')
    return path


def windows_boot_epoch():
    """Process-independent Windows boot identity; never a per-process nonce."""
    if os.name!='nt': raise NativeQuotaError('windows_boot_epoch_requires_windows')
    executable=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-Command',
            '(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString("o")'],
            capture_output=True,text=True,timeout=8,creationflags=subprocess.CREATE_NO_WINDOW,check=True)
    except (OSError,subprocess.SubprocessError):
        raise NativeQuotaError('boot_epoch_unavailable') from None
    value=result.stdout.strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z',value):
        raise NativeQuotaError('invalid_boot_epoch')
    return 'windows:'+value


class _OwnedJob:
    """Close one owned Windows job to terminate its complete process tree."""
    def __init__(self, process):
        self.handle=None
        self.process=process
        self.lock=threading.Lock()
        self.closed=False
        self.close_done=threading.Event()
        if os.name!='nt': return
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_=[('ProcessTime',ctypes.c_int64),('JobTime',ctypes.c_int64),('Flags',wintypes.DWORD),
                ('MinWorking',ctypes.c_size_t),('MaxWorking',ctypes.c_size_t),('ActiveLimit',wintypes.DWORD),
                ('Affinity',ctypes.c_size_t),('Priority',wintypes.DWORD),('Scheduling',wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_uint64) for name in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
        class Extended(ctypes.Structure):
            _fields_=[('Basic',Basic),('IO',IO),('ProcessMemory',ctypes.c_size_t),('JobMemory',ctypes.c_size_t),
                ('PeakProcessMemory',ctypes.c_size_t),('PeakJobMemory',ctypes.c_size_t)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype=wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        kernel.SetInformationJobObject.restype=wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype=wintypes.BOOL
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.CloseHandle.restype=wintypes.BOOL
        kernel.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT]
        kernel.TerminateJobObject.restype=wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype=wintypes.BOOL
        handle=kernel.CreateJobObjectW(None,None)
        if not handle: raise NativeQuotaError('owned_process_job_unavailable')
        limits=Extended(); limits.Basic.Flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle,int(process._handle)):
            kernel.CloseHandle(handle)
            raise NativeQuotaError('owned_process_assignment_failed')
        self.handle,self.kernel=handle,kernel

    def close(self):
        with self.lock:
            already_closing=self.closed
            if not already_closing:
                self.closed=True
                handle,self.handle=self.handle,None
        if already_closing:
            self.close_done.wait(4)
            return
        try:
            if handle is not None:
                from ctypes import wintypes
                class Accounting(ctypes.Structure):
                    _fields_=[('User',ctypes.c_int64),('Kernel',ctypes.c_int64),
                        ('PeriodUser',ctypes.c_int64),('PeriodKernel',ctypes.c_int64),
                        ('Faults',wintypes.DWORD),('Total',wintypes.DWORD),
                        ('Active',wintypes.DWORD),('Terminated',wintypes.DWORD)]
                self.kernel.TerminateJobObject(handle,1)
                until=time.monotonic()+3
                while time.monotonic()<until:
                    accounting=Accounting()
                    if not self.kernel.QueryInformationJobObject(handle,1,ctypes.byref(accounting),ctypes.sizeof(accounting),None) or accounting.Active==0:
                        break
                    time.sleep(.01)
                self.kernel.CloseHandle(handle)
            elif os.name!='nt':
                try: os.killpg(self.process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
        finally:
            self.close_done.set()


def _strict_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError()
            result[key]=value
        return result
    def invalid(value): raise ValueError()
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)


class NativeQuotaReader:
    def __init__(self, *, state_root, executable=None, timeout=20, max_line_bytes=262144,
                 test_command=None, test_backend=None, test_ca_path=None, allow_test_mode=False):
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 0<timeout<=60:
            raise NativeQuotaError('invalid_timeout')
        if type(max_line_bytes) is not int or not 1024<=max_line_bytes<=1048576:
            raise NativeQuotaError('invalid_protocol_bound')
        self.test_mode=any(value is not None for value in (test_command,test_backend,test_ca_path))
        if self.test_mode and allow_test_mode is not True:
            raise NativeQuotaError('test_overrides_require_explicit_opt_in')
        if test_backend is not None:
            url=urlsplit(test_backend)
            if url.scheme not in ('http','https') or url.hostname not in ('127.0.0.1','localhost','::1') or url.username or url.password or url.query or url.fragment:
                raise NativeQuotaError('test_backend_must_be_loopback')
        if test_ca_path and (not test_backend or urlsplit(test_backend).scheme!='https'):
            raise NativeQuotaError('test_ca_requires_loopback_https')
        if test_command is not None:
            if not isinstance(test_command,(list,tuple)) or not test_command or any(not isinstance(x,str) or '\x00' in x for x in test_command):
                raise NativeQuotaError('invalid_test_command')
            self.command=list(test_command)
        else:
            self.command=[str(qualified_executable(executable)),'app-server','--stdio']
        self.root=Path(state_root).absolute()
        _check_private_path(self.root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.timeout,self.max_line_bytes=timeout,max_line_bytes
        self.test_backend,self.test_ca_path=test_backend,test_ca_path

    def _environment(self, home):
        allowed={'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','COMSPEC','SYSTEMDRIVE'}
        env={k:v for k,v in os.environ.items() if k.upper() in allowed}
        for name,folder in [('USERPROFILE','profile'),('APPDATA','appdata'),('LOCALAPPDATA','localappdata')]:
            path=home/folder; path.mkdir(); env[name]=str(path)
        env.update(CODEX_HOME=str(home),RUST_LOG='off')
        if self.test_ca_path:
            env['CODEX_CA_CERTIFICATE']=str(Path(self.test_ca_path).resolve())
            env['SSL_CERT_FILE']=env['CODEX_CA_CERTIFICATE']
        return env

    @contextmanager
    def _isolated_home(self):
        home=Path(tempfile.mkdtemp(prefix='quota-',dir=self.root)).resolve()
        if not home.is_relative_to(self.root.resolve()):
            raise NativeQuotaError('isolation_path_escape')
        try:
            yield home
        finally:
            # A terminated Windows child may release its cwd handle slightly after
            # the job reports zero active processes. Retry only this verified owned
            # directory, never a computed global cleanup target.
            deadline=time.monotonic()+2
            while True:
                _check_private_path(home)
                if not home.resolve().is_relative_to(self.root.resolve()):
                    raise NativeQuotaError('isolation_cleanup_path_escape')
                try:
                    shutil.rmtree(home)
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    if time.monotonic()>=deadline:
                        raise NativeQuotaError('isolation_cleanup_failed') from None
                    time.sleep(.03)

    def read(self, bearer_token, account_id):
        if not isinstance(bearer_token,str) or not 0<len(bearer_token)<=65536 or any(c.isspace() or c=='\x00' for c in bearer_token):
            raise NativeQuotaError('invalid_memory_token')
        if not isinstance(account_id,str) or not 0<len(account_id)<=256 or any(c.isspace() or c=='\x00' for c in account_id):
            raise NativeQuotaError('invalid_account_identity')
        if self.test_mode and not (bearer_token=='synthetic-token-only' or bearer_token.endswith('.synthetic-signature')):
            raise NativeQuotaError('test_mode_rejects_real_credentials')
        _check_private_path(self.root)
        with self._isolated_home() as home:
            config='''cli_auth_credentials_store = "ephemeral"
check_for_update_on_startup = false
web_search = "disabled"
analytics.enabled = false
feedback.enabled = false
[mcp_servers]
[features]
multi_agent = false
plugins = false
remote_plugin = false
plugin_discovery_prefetch = false
plugin_sharing = false
skill_search = false
skill_mcp_dependency_install = false
skip_host_skill_discovery = true
omit_skill_descriptions = true
'''
            if self.test_backend:
                config='chatgpt_base_url = '+json.dumps(self.test_backend)+'\n'+config
            (home/'config.toml').write_text(config,encoding='utf-8')
            return self._read_owned_process(home,bearer_token,account_id)

    def _read_owned_process(self, home, token, account_id):
        process=None; job=None; reader=None; watchdog=None
        messages=queue.Queue(maxsize=32); protocol_failed=threading.Event()
        deadline=time.monotonic()+self.timeout
        try:
            options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
            process=subprocess.Popen(self.command,cwd=home,env=self._environment(home),stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,**options)
            job=_OwnedJob(process)
            def expire():
                job.close()
            watchdog=threading.Timer(max(.001,deadline-time.monotonic()),expire)
            watchdog.daemon=True;watchdog.start()
            def receive():
                try:
                    while True:
                        line=process.stdout.readline(self.max_line_bytes+1)
                        if not line: return
                        if len(line)>self.max_line_bytes or not line.endswith(b'\n'):
                            protocol_failed.set(); return
                        value=_strict_json(line)
                        if not isinstance(value,dict): protocol_failed.set(); return
                        messages.put_nowait(value)
                except (ValueError,UnicodeError,RecursionError,queue.Full,OSError):
                    protocol_failed.set()
            reader=threading.Thread(target=receive,daemon=True);reader.start()
            def send(value):
                process.stdin.write(json.dumps(value,separators=(',',':')).encode()+b'\n')
                process.stdin.flush()
            def rpc(ident,method,params):
                send({'id':ident,'method':method,'params':params})
                while time.monotonic()<deadline:
                    if protocol_failed.is_set(): raise NativeQuotaError('invalid_or_excessive_native_protocol')
                    try: value=messages.get(timeout=min(.1,max(.001,deadline-time.monotonic())))
                    except queue.Empty:
                        if process.poll() is not None: raise NativeQuotaError('native_process_exited')
                        continue
                    if 'method' in value and 'id' in value:
                        # A refresh or other server request cannot cause credential retrieval.
                        raise NativeQuotaError('native_requested_unsupported_auth_or_action')
                    if value.get('id')==ident:
                        if 'error' in value or not isinstance(value.get('result'),dict):
                            raise NativeQuotaError('native_rpc_failed')
                        return value['result']
                raise NativeQuotaError('native_quota_timeout')
            rpc(1,'initialize',{'clientInfo':{'name':'codex_continuity_quota_reader','version':'1.0'},
                'capabilities':{'experimentalApi':True}})
            send({'method':'initialized'})
            rpc(2,'account/login/start',{'type':'chatgptAuthTokens','accessToken':token,'chatgptAccountId':account_id})
            result=rpc(3,'account/rateLimits/read',{})
            if result.get('accountId')!=account_id:
                raise NativeQuotaError('native_quota_account_mismatch')
            if 'ordinaryUsageAllowed' not in result or 'rateLimitsByLimitId' not in result:
                raise NativeQuotaError('native_quota_snapshot_incomplete')
            return result
        except NativeQuotaError:
            raise
        except (OSError,ValueError,subprocess.SubprocessError):
            raise NativeQuotaError('native_quota_transport_failed') from None
        finally:
            if watchdog:
                watchdog.cancel()
            if job: job.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: pass
                for stream in (process.stdin,process.stdout):
                    if stream:
                        try: stream.close()
                        except OSError: pass
            if reader: reader.join(timeout=1)
