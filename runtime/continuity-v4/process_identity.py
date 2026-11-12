"""Exact Windows venv wrapper/child ownership; no launch or process mutation."""
import ctypes,datetime,hashlib,json,os,subprocess,time
from ctypes import wintypes as w
from pathlib import Path

class IdentityBlocked(ValueError):pass
def path_key(path):return os.path.normcase(str(Path(path).resolve()))
def verify_pin(pin):
    if not isinstance(pin,dict) or set(pin)!={'path','sha256'}:raise IdentityBlocked('Exact executable pin required')
    if hashlib.sha256(Path(pin['path']).read_bytes()).hexdigest()!=pin['sha256']:raise IdentityBlocked('Executable pin changed')
def argv(command):
    shell=ctypes.WinDLL('shell32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    shell.CommandLineToArgvW.argtypes=[w.LPCWSTR,ctypes.POINTER(ctypes.c_int)];shell.CommandLineToArgvW.restype=ctypes.POINTER(w.LPWSTR)
    kernel.LocalFree.argtypes=[w.HLOCAL];kernel.LocalFree.restype=w.HLOCAL
    count=ctypes.c_int();ptr=shell.CommandLineToArgvW(command,ctypes.byref(count))
    if not ptr:raise IdentityBlocked('Unable to parse native command line')
    try:return [ptr[i] for i in range(count.value)]
    finally:kernel.LocalFree(ptr)
def process_identity(pid):
    if type(pid) is not int or pid<=0:raise IdentityBlocked('Positive native PID required')
    ps=("$p=Get-CimInstance Win32_Process -Filter 'ProcessId="+str(pid)+"';if($p){$o=Invoke-CimMethod -InputObject $p -MethodName GetOwnerSid;"
        "[pscustomobject]@{pid=$p.ProcessId;parent=$p.ParentProcessId;executable=$p.ExecutablePath;command=$p.CommandLine;"
        "created=$p.CreationDate.ToUniversalTime().ToString('o');user_sid=$o.Sid;session_id=$p.SessionId}|ConvertTo-Json -Compress}")
    exe=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    result=subprocess.run([str(exe),'-NoProfile','-NonInteractive','-Command',ps],capture_output=True,text=True,encoding='utf-8',timeout=10,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    if not result.stdout.strip():raise IdentityBlocked('Expected process exited')
    value=json.loads(result.stdout);value['argv']=argv(value['command'])
    k=kernel();handle=k.OpenProcess(0x1000,False,pid)
    if not handle:raise IdentityBlocked('Process exited before birth binding')
    try:birth=creation_filetime(k,handle)
    finally:k.CloseHandle(handle)
    iso=(datetime.datetime(1601,1,1,tzinfo=datetime.timezone.utc)+datetime.timedelta(microseconds=birth//10)).strftime('%Y-%m-%dT%H:%M:%S.%f')+'0Z'
    if value['created']!=iso:raise IdentityBlocked('PID birth changed between CIM and native handle')
    value['creation_filetime']=birth;return value
def kernel():
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
    k.CloseHandle.argtypes=[w.HANDLE];k.CloseHandle.restype=w.BOOL
    k.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL)];k.IsProcessInJob.restype=w.BOOL
    k.QueryInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD)];k.QueryInformationJobObject.restype=w.BOOL
    k.GetProcessTimes.argtypes=[w.HANDLE,ctypes.POINTER(w.FILETIME),ctypes.POINTER(w.FILETIME),ctypes.POINTER(w.FILETIME),ctypes.POINTER(w.FILETIME)];k.GetProcessTimes.restype=w.BOOL
    return k
def creation_filetime(k,handle):
    values=[w.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(handle,*[ctypes.byref(v) for v in values]):raise IdentityBlocked('Native process birth query failed')
    return (values[0].dwHighDateTime<<32)|values[0].dwLowDateTime
def process_in_any_job(pid,*,expected_creation_filetime=None):
    k=kernel();handle=k.OpenProcess(0x1000,False,int(pid))
    if not handle:raise IdentityBlocked('Cannot open exact process for job query')
    try:
        if expected_creation_filetime is not None and creation_filetime(k,handle)!=expected_creation_filetime:raise IdentityBlocked('Job query PID was reused')
        value=w.BOOL()
        if not k.IsProcessInJob(handle,None,ctypes.byref(value)):raise IdentityBlocked('Native job query failed')
        return bool(value.value)
    finally:k.CloseHandle(handle)
def current_job_snapshot(*,expected_self_creation=None,expected_parent_creation=None):
    k=kernel();pid=os.getpid();parent=os.getppid()
    result={'pid':pid,'parent_pid':parent,
        'in_any_job':process_in_any_job(pid,expected_creation_filetime=expected_self_creation),
        'parent_in_any_job':process_in_any_job(parent,expected_creation_filetime=expected_parent_creation)}
    if not result['in_any_job']:return result
    class Limits(ctypes.Structure):
        _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',w.DWORD),
            ('minimum_working_set',ctypes.c_size_t),('maximum_working_set',ctypes.c_size_t),('active_process_limit',w.DWORD),
            ('affinity',ctypes.c_size_t),('priority_class',w.DWORD),('scheduling_class',w.DWORD)]
    limits=Limits();returned=w.DWORD()
    if not k.QueryInformationJobObject(None,2,ctypes.byref(limits),ctypes.sizeof(limits),ctypes.byref(returned)):raise IdentityBlocked('Cannot qualify own venv job limits')
    raw=ctypes.create_string_buffer(65536)
    if not k.QueryInformationJobObject(None,3,raw,len(raw),ctypes.byref(returned)):raise IdentityBlocked('Cannot qualify own venv job membership')
    assigned=w.DWORD.from_buffer(raw,0).value;count=w.DWORD.from_buffer(raw,4).value
    if count>(len(raw)-8)//ctypes.sizeof(ctypes.c_size_t):raise IdentityBlocked('Invalid own job member count')
    return dict(result,immediate_job_limit_flags=limits.flags,immediate_job_assigned_count=assigned,
        immediate_job_pids=list((ctypes.c_size_t*count).from_buffer(raw,8)))
def validate_pair(wrapper,child,wrapper_pin,base_pin,expected_args,user_sid,session_id):
    """Pure identity checks; callers separately hash pins and query job handles."""
    if not isinstance(expected_args,list) or not expected_args or not all(isinstance(s,str) for s in expected_args):raise IdentityBlocked('Exact argument vector required')
    if child.get('parent')!=wrapper.get('pid') or child.get('pid')==wrapper.get('pid'):raise IdentityBlocked('Wrong venv parent/child lineage')
    if not isinstance(user_sid,str) or not user_sid.startswith('S-1-') or type(session_id) is not int or session_id<1:raise IdentityBlocked('Expected interactive user/session required')
    for value,pin in ((wrapper,wrapper_pin),(child,base_pin)):
        if path_key(value['executable'])!=path_key(pin['path']):raise IdentityBlocked('Wrong wrapper/base executable')
        args=value.get('argv')
        if not args or path_key(args[0])!=path_key(pin['path']) or args[1:]!=expected_args:raise IdentityBlocked('Exact wrapper/base argument binding failed')
        if value.get('user_sid')!=user_sid or value.get('session_id')!=session_id:raise IdentityBlocked('Wrong user/session ownership')
        if not isinstance(value.get('created'),str) or not value['created'].endswith('Z'):raise IdentityBlocked('Native process birth evidence required')
    if wrapper['created']>child['created']:raise IdentityBlocked('Wrapper cannot be younger than child')
    return {'wrapper':wrapper,'child':child,'user_sid':user_sid,'session_id':session_id}
def validate_self_job(proof,job):
    if job.get('pid')!=proof['child']['pid'] or job.get('parent_pid')!=proof['wrapper']['pid']:raise IdentityBlocked('Job identity disagrees with process lineage')
    if job.get('parent_in_any_job') is not False:raise IdentityBlocked('Venv wrapper remains in an ancestor job')
    if (job.get('in_any_job') is not True or job.get('immediate_job_limit_flags')!=0x3000
        or job.get('immediate_job_assigned_count')!=1 or job.get('immediate_job_pids')!=[proof['child']['pid']]):
        raise IdentityBlocked('Only qualified self-owned venv child job is permitted')
    return dict(proof,job=job)
def require_detached_venv_self(wrapper_pin,base_pin,*,expected_args,user_sid,session_id):
    verify_pin(wrapper_pin);verify_pin(base_pin)
    child=process_identity(os.getpid());wrapper=process_identity(os.getppid())
    proof=validate_pair(wrapper,child,wrapper_pin,base_pin,expected_args,user_sid,session_id)
    proof=validate_self_job(proof,current_job_snapshot(expected_self_creation=child['creation_filetime'],expected_parent_creation=wrapper['creation_filetime']))
    if process_identity(child['pid'])!=child or process_identity(wrapper['pid'])!=wrapper:raise IdentityBlocked('Process identity changed during qualification')
    return dict(proof,qualified_at=time.time(),scope='exact detached wrapper plus own venv child job',app_exit_survival_tested=False)
