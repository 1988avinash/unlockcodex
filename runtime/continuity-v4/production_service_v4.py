"""Verified, explicit production service startup. Importing this module does nothing.

Preparation writes only a requested configuration file. Activation/autostart is
separate. No OAuth file is read; native request credentials stay in memory.
"""
import argparse
import csv
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import types
from urllib.parse import urlsplit
import uuid

REQUIRED = {'continuity-v4/route_registry.py', 'continuity-v4/transport_v4.py', 'continuity-v4/production_service_v4.py', 'continuity-v3/native_quota.py', 'continuity-v4/paid_compaction_policy_v4.py', 'continuity-v2/protocol_codec.py', 'continuity-v4/handoff_executor_v4.py', 'continuity-v4/protocol_profiles_v4.py', 'continuity-v2/quota_policy.py', 'continuity-v2/handoff.py', 'continuity-v4/route_policy.py', 'continuity-v2/handoff_service.py', 'continuity-v4/history_v4.py', 'continuity-v4/native_review_gateway_v4.py', 'continuity-v4/production_adapters_v4.py', 'continuity-v3/desktop_notifier.py', 'continuity-v3/Notify-Continuity.ps1', 'continuity-v4/gateway_v4.py'}
REQUIRED.update({'continuity-v4/agy_bridge.py','continuity-v4/agy_runtime.py','continuity-v4/agy_executor.py'})
REQUIRED.add('continuity-v4/handoff_normalization.py')
REQUIRED.add('continuity-v4/process_identity.py')
REQUIRED.add('continuity-v4/credentials.py')
# zstandard comes from the pinned virtualenv; provider keys from the in-package
# DPAPI store. No executable code outside the release and its virtualenv is loaded.
EXTERNAL_REQUIRED = set()
FIELDS = {'version','package_root','state_dir','host','port','account_id','capability','openai_models',
    'recovery_aliases','cold_routes','registered_chats','enroll_new_chats','allow_incomplete_visible_rebase','allow_paid_compaction','allow_native_review','allow_paid_lite_compaction',
    'native_runtime','python_runtime','files','external_files','route_specs','auto_fallback_id','qualification_files'}


class ServiceBlocked(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',',':'), allow_nan=False).encode()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def no_links(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ServiceBlocked('Path traversal is not allowed')
    for parent in (path, *path.parents):
        if parent.is_symlink() or parent.exists() and getattr(parent.stat(),'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ServiceBlocked('Reparse paths are not allowed')
    return path


def package_files(root):
    """Only runtime code locations; never recursively scan histories/state."""
    root = no_links(root)
    paths = set()
    for folder in ('continuity-v2','continuity-v3','continuity-v4'):
        for path in (root/folder).iterdir():
            no_links(path)
            if path.is_dir() and path.name!='__pycache__':
                raise ServiceBlocked('Runtime source folders must be flat; unexpected import package directory')
            if path.is_file() and path.suffix.lower()=='.pyc':
                raise ServiceBlocked('Unpinned sourceless bytecode is not allowed')
            if path.is_file() and path.suffix.lower() in ('.py','.ps1','.pyd','.dll'):
                paths.add(path.relative_to(root).as_posix())
    deps = root/'desktop-route-v2/deps'
    for path in deps.rglob('*'):
        if '__pycache__' in path.parts:
            continue
        if path.is_file() and path.suffix.lower()=='.pyc':raise ServiceBlocked('Unpinned sourceless bytecode is not allowed')
        if path.is_file() and path.suffix.lower() in ('.py','.pyd','.dll'):
            no_links(path)
            paths.add(path.relative_to(root).as_posix())
    if not REQUIRED <= paths:
        raise ServiceBlocked('Required reviewed runtime files are missing')
    return paths


def _pin(path):
    path = no_links(path)
    if not path.is_file():
        raise ServiceBlocked('Pinned artifact is unavailable')
    return {'path':str(path), 'sha256':sha256(path)}


def dependency_files(python_runtime):
    """Exhaustive installed-library pins; no bytecode, credentials or user data."""
    libraries=no_links(Path(python_runtime).parent.parent/'Lib/site-packages')
    if not libraries.is_dir():raise ServiceBlocked('Reviewed virtualenv dependency directory unavailable')
    paths={str(no_links(path)) for path in EXTERNAL_REQUIRED}
    paths.update(str(path) for path in python_runtime_layout(python_runtime)['files'])
    for path in libraries.rglob('*'):
        if '__pycache__' in path.parts:continue
        if path.is_file() and path.suffix.lower()=='.pyc':raise ServiceBlocked('Unpinned sourceless dependency bytecode is not allowed')
        no_links(path)
        if path.is_file():paths.add(str(path))
        if len(paths)>20000:raise ServiceBlocked('Installed dependency manifest exceeds bound')
    return paths


def python_runtime_layout(python_runtime):
    """Pin the venv redirector selector and the interpreter it actually loads."""
    wrapper=no_links(python_runtime);selector=no_links(wrapper.parent.parent/'pyvenv.cfg')
    raw=selector.read_bytes()
    if len(raw)>16384:raise ServiceBlocked('Python selector exceeds bound')
    values={}
    for line in raw.decode('utf-8-sig').splitlines():
        if not line.strip():continue
        key,separator,value=line.partition('=');key=key.strip();value=value.strip()
        if not separator or key in values:raise ServiceBlocked('Invalid Python selector')
        values[key]=value
    home=no_links(values['home']);base=no_links(values['executable'])
    if base!=home/'python.exe' or wrapper.name.lower()!='python.exe' or not values.get('version','').startswith('3.11.'):
        raise ServiceBlocked('Unqualified Python redirector layout')
    files={selector,base,home/'pythonw.exe',home/'python311.dll',home/'python3.dll',home/'vcruntime140.dll',home/'vcruntime140_1.dll'}
    if any(not no_links(path).is_file() for path in files):raise ServiceBlocked('Python runtime dependency unavailable')
    return {'selector':selector,'base':base,'files':files}


def process_module(config):
    path=Path(config['package_root'])/'continuity-v4/process_identity.py';raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=config['files']['continuity-v4/process_identity.py']:
        raise ServiceBlocked('Process identity helper changed')
    module=types.ModuleType('verified_continuity_process_identity');module.__file__=str(path)
    exec(compile(raw,str(path),'exec'),module.__dict__);return module


def python_base_pin(config):
    layout=python_runtime_layout(config['python_runtime']['path'])
    for path in layout['files']:
        if config['external_files'].get(str(path))!=sha256(path):raise ServiceBlocked('Python selector/base runtime pin changed')
    base=layout['base'];return {'path':str(base),'sha256':config['external_files'][str(base)]}


def service_arguments(config,config_path,expected_sha256):
    return ['-I','-S','-B',str(Path(config['package_root'])/'continuity-v4/production_service_v4.py'),
        '--config',str(no_links(config_path)),'--config-sha256',expected_sha256]


def original_process_state(record,identity):
    """Observe an exact historical birth without adopting or touching a reused PID."""
    if type(record.get('pid')) is not int or record['pid']<=0 or type(record.get('creation_filetime')) is not int or record['creation_filetime']<=0:
        raise ServiceBlocked('Recorded native process birth required')
    kernel=identity.kernel();handle=kernel.OpenProcess(0x1000|0x100000,False,record['pid'])
    if not handle:
        error=ctypes.get_last_error()
        if error==87:return {'state':'absent','pid':record['pid'],'recorded_creation_filetime':record['creation_filetime']}
        raise ServiceBlocked('Original process exit cannot be established')
    try:
        birth=identity.creation_filetime(kernel,handle)
        if birth==record['creation_filetime']:
            from ctypes import wintypes
            kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.WaitForSingleObject.restype=wintypes.DWORD
            wait=kernel.WaitForSingleObject(handle,0)
            if wait not in (0,258):raise ServiceBlocked('Original process termination query failed')
            state='same_birth_exited' if wait==0 else 'same_birth_alive'
        else:state='different_birth'
    finally:kernel.CloseHandle(handle)
    return {'state':state,
        'pid':record['pid'],'recorded_creation_filetime':record['creation_filetime'],'observed_creation_filetime':birth}


def verified_service_process(pid,config,config_path,expected_sha256,*,expected=None):
    """Observe a pinned wrapper or its exact base-Python child, never guess PID."""
    identity=process_module(config);base=python_base_pin(config);identity.verify_pin(config['python_runtime'])
    caller=identity.process_identity(os.getpid())
    if expected and all(key in expected for key in ('wrapper','child')):
        if any(expected[key].get('user_sid')!=caller['user_sid'] for key in ('wrapper','child')):
            raise ServiceBlocked('Recorded service belongs to a different user')
        states={key:original_process_state(expected[key],identity) for key in ('wrapper','child')}
        gone=[value['state']!='same_birth_alive' for value in states.values()]
        if all(gone):return {'original_pair_gone':True,'recorded_identity':expected,'exit_evidence':states}
        if any(gone):raise ServiceBlocked('Original service pair is only partially alive; reconcile before launch')
    try:value=identity.process_identity(pid)
    except identity.IdentityBlocked as error:
        if str(error)!='Expected process exited':raise
        # A live wrapper with a missing recorded child is not a clean shutdown.
        wrapper=(expected or {}).get('wrapper',{})
        if wrapper.get('pid') and wrapper['pid']!=pid:
            try:identity.process_identity(wrapper['pid'])
            except identity.IdentityBlocked as absent:
                if str(absent)!='Expected process exited':raise
            else:raise ServiceBlocked('Recorded wrapper remains alive without its child')
        return None
    args=service_arguments(config,config_path,expected_sha256)
    if identity.path_key(value['executable'])==identity.path_key(config['python_runtime']['path']):
        wrapper=value;proof={'wrapper':wrapper}
        if (wrapper['argv'][1:]!=args or identity.path_key(wrapper['argv'][0])!=identity.path_key(config['python_runtime']['path'])
                or wrapper['user_sid']!=caller['user_sid'] or wrapper['session_id']!=caller['session_id']):
            raise ServiceBlocked('Wrapper arguments or interactive owner changed')
    else:
        wrapper=identity.process_identity(value['parent'])
        proof=identity.validate_pair(wrapper,value,config['python_runtime'],base,args,caller['user_sid'],caller['session_id'])
    if identity.process_in_any_job(wrapper['pid'],expected_creation_filetime=wrapper['creation_filetime']):
        raise ServiceBlocked('Service wrapper remains in an ancestor job')
    for key in ('wrapper','child'):
        if key in (expected or {}) and proof.get(key)!=expected[key]:raise ServiceBlocked('Recorded process birth/identity changed')
    if identity.process_identity(value['pid'])!=value or identity.process_identity(wrapper['pid'])!=wrapper:
        raise ServiceBlocked('Service process changed during verification')
    return proof


def reject_credential_shadows(workflow_root,libraries):
    """The existing adapter prepends this root; forbid dependency collisions."""
    names=set(sys.stdlib_module_names)|{Path(name).stem for name in REQUIRED}
    namespaces=set()
    for item in no_links(libraries).iterdir():
        if item.is_dir() and item.name.isidentifier() and item.name!='__pycache__':
            names.add(item.name)
            if not any((item/name).is_file() for name in ('__init__.py','__init__.pyc')) and not any(item.glob('__init__*.pyd')):
                namespaces.add(item.name)
        elif item.suffix.lower() in ('.py','.pyc','.pyd'):names.add(item.name.split('.')[0])
    for item in no_links(workflow_root).iterdir():
        stem=item.name if item.is_dir() else item.name.split('.')[0]
        package=item.is_dir() and (stem in namespaces or any((item/name).is_file() for name in ('__init__.py','__init__.pyc')) or any(item.glob('__init__*.pyd')))
        if stem in names and (package or item.is_file() and item.suffix.lower() in ('.py','.pyc','.pyd','.dll')):
            raise ServiceBlocked('Credential adapter import root shadows a pinned dependency')


def prepare_config(path, *, package_root, state_dir, account_id, openai_models, port,
                   native_runtime, python_runtime, external_files, recovery_aliases=None,
                   cold_routes=None, registered_chats=(), enroll_new_chats=False,
                   allow_incomplete_visible_rebase=False, allow_paid_compaction=False, allow_native_review=False, allow_paid_lite_compaction=False,
                   route_specs=(),auto_fallback_id=None,qualification_files=(),capability=None,default_cold_model=None):
    """Write a new reviewed candidate; never launch it or edit Desktop settings.

    external_files is the host-reviewed executable dependency manifest, including
    installed credential adapter/library code. It must not contain secret files.
    """
    root = no_links(package_root)
    config = {'version':4,'route_specs':list(route_specs),'auto_fallback_id':auto_fallback_id,
        'qualification_files':{str(no_links(item)):sha256(item) for item in qualification_files}, 'package_root':str(root), 'state_dir':str(no_links(state_dir)),
        'host':'127.0.0.1', 'port':port, 'account_id':account_id, 'capability':capability if capability is not None else secrets.token_urlsafe(32),
        'openai_models':sorted(openai_models), 'recovery_aliases':dict(recovery_aliases or {}),
        'cold_routes':list(cold_routes or []), 'registered_chats':list(registered_chats),
        'enroll_new_chats':enroll_new_chats, 'allow_incomplete_visible_rebase':allow_incomplete_visible_rebase,
        'allow_paid_compaction':allow_paid_compaction,
        'allow_native_review':allow_native_review,'allow_paid_lite_compaction':allow_paid_lite_compaction,
        'native_runtime':_pin(native_runtime), 'python_runtime':_pin(python_runtime),
        'files':{name:sha256(no_links(root/name)) for name in sorted(package_files(root))},
        'external_files':{str(no_links(item)):sha256(item) for item in external_files}}
    # Optional: the OpenAI model assumed to have written a chat first seen on a plan model.
    if default_cold_model is not None:config['default_cold_model']=default_cold_model
    verify_config(config)
    target = no_links(path)
    with target.open('xb') as stream:
        stream.write(encoded(config));stream.flush();os.fsync(stream.fileno())
    return {'config_path':str(target), 'config_sha256':sha256(target),
        'manifest_sha256':hashlib.sha256(encoded(config['files'])).hexdigest()}


def verify_routes(config):
    """Validate plan declarations and immutable host-reviewed qualification bytes."""
    specs=config['route_specs'];pins=config['qualification_files']
    if not isinstance(specs,list) or not 1<=len(specs)<=40 or not isinstance(pins,dict):
        raise ServiceBlocked('Bounded qualified plan routes required')
    fields={'route_id','provider_id','actual_model','native_alias','endpoint','protocol_profile',
        'display_name','supports_native_tools','auto_fallback_qualified','qualification_ref'}
    ids=set();aliases=set();references=set();automatic=[];endpoints={}
    for spec in specs:
        if not isinstance(spec,dict) or set(spec)!=fields:raise ServiceBlocked('Exact route schema required')
        if any(not isinstance(spec[k],str) for k in fields-{'supports_native_tools','auto_fallback_qualified'}):
            raise ServiceBlocked('Route text fields must be strings')
        if (not re.fullmatch('[a-z0-9][a-z0-9:_-]{0,127}',spec['route_id']) or spec['route_id'] in ids|{'openai','pause'}
                or not re.fullmatch('tp-[a-z0-9-]{1,120}',spec['native_alias']) or spec['native_alias'] in aliases):
            raise ServiceBlocked('Unique route and native alias identities required')
        # Providers are the user's own configuration: any HTTPS Responses endpoint,
        # exactly one per provider id so a key is never sent to a second host.
        target=urlsplit(spec['endpoint'])
        if (not re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}',spec['provider_id']) or spec['provider_id']=='openai'
                or endpoints.setdefault(spec['provider_id'],spec['endpoint'])!=spec['endpoint']
                or not (spec['endpoint']=='agy:session-only-structured' if spec['provider_id']=='antigravity' else
                    target.scheme=='https' and target.hostname and not (target.username or target.password or target.query or target.fragment))):
            raise ServiceBlocked('One HTTPS Responses endpoint per provider id required')
        if (spec['protocol_profile'] not in ('responses-full','responses-lite','agy-structured')
                or (spec['provider_id']=='antigravity')!=(spec['protocol_profile']=='agy-structured')):
            raise ServiceBlocked('Provider wire profile mismatch')
        if (spec['supports_native_tools'] is not True or type(spec['auto_fallback_qualified']) is not bool
                or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}',spec['actual_model'])
                or not 0<len(spec['display_name'])<=200):raise ServiceBlocked('Qualified native model capabilities required')
        ref=str(no_links(spec['qualification_ref']));references.add(ref)
        if ref not in pins or sha256(ref)!=pins[ref]:raise ServiceBlocked('Plan qualification evidence changed')
        proof=json.loads(Path(ref).read_text(encoding='utf-8'))
        if (proof.get('qualified_for_native') is not True or any(proof.get(k)!=spec[k] for k in
                ('provider_id','actual_model','protocol_profile','auto_fallback_qualified'))):
            raise ServiceBlocked('Plan qualification does not cover this route')
        if (config['allow_paid_lite_compaction'] and spec['protocol_profile']=='responses-lite'
                and proof.get('paid_lite_compaction_qualified') is not True):
            raise ServiceBlocked('Live lite compaction qualification is required')
        ids.add(spec['route_id']);aliases.add(spec['native_alias'])
        if spec['auto_fallback_qualified']:automatic.append(spec['route_id'])
    if set(pins)!=references or config['auto_fallback_id'] not in automatic:
        raise ServiceBlocked('Exact qualification set and automatic fallback required')
    if aliases & (set(config['openai_models'])|set(config['recovery_aliases'])):
        raise ServiceBlocked('Ambiguous native plan alias')


def verify_config(config, *, test_artifacts=False):
    """No executable imports, credential reads, state writes or sockets here."""
    if not isinstance(config,dict) or set(config)-{'default_cold_model'}!=FIELDS or config['version']!=4:
        raise ServiceBlocked('Exact service configuration schema required')
    if config.get('default_cold_model') is not None and config['default_cold_model'] not in config['openai_models']:
        raise ServiceBlocked('Default cold model must be a qualified OpenAI model')
    if config['host']!='127.0.0.1' or type(config['port']) is not int or not 1024<=config['port']<=65535:
        raise ServiceBlocked('Stable nonprivileged loopback port required')
    if not isinstance(config['account_id'],str) or not 0<len(config['account_id'])<=256:
        raise ServiceBlocked('Exactly one configured account is required')
    if not isinstance(config['capability'],str) or not re.fullmatch('[A-Za-z0-9_-]{32,128}',config['capability']):
        raise ServiceBlocked('Random local service capability required')
    models = config['openai_models']
    if not isinstance(models,list) or not models or len(set(models))!=len(models) or any(not isinstance(m,str) or not m.startswith('gpt-') for m in models):
        raise ServiceBlocked('Exact qualified model allowset required')
    aliases = config['recovery_aliases']
    if not isinstance(aliases,dict) or any(not isinstance(k,str) or k in models or k=='tp-minimax-m31' or v not in models for k,v in aliases.items()):
        raise ServiceBlocked('Qualified recovery aliases required')
    if any(type(config[key]) is not bool for key in ('enroll_new_chats','allow_incomplete_visible_rebase','allow_paid_compaction','allow_native_review','allow_paid_lite_compaction')):
        raise ServiceBlocked('Enrollment and visible rebase must be explicit')
    if config['allow_paid_lite_compaction'] and not config['allow_paid_compaction']:
        raise ServiceBlocked('Lite compaction requires the reviewed paid compaction gate')
    chats = config['registered_chats']
    if not isinstance(chats,list) or len(chats)!=len(set(chats)):
        raise ServiceBlocked('Explicit unique registered chats required')
    try:
        if any(str(uuid.UUID(chat))!=chat or uuid.UUID(chat).int==0 for chat in chats):raise ValueError()
    except (TypeError,ValueError,AttributeError):
        raise ServiceBlocked('Native registered chat identities required') from None
    if not isinstance(config['cold_routes'],list):
        raise ServiceBlocked('Explicit cold route list required')
    seen = set()
    for item in config['cold_routes']:
        if not isinstance(item,dict) or set(item)!={'account_id','thread_id','route'} or item['account_id']!=config['account_id'] or item['thread_id'] not in chats or item['thread_id'] in seen:
            raise ServiceBlocked('Cold routes must bind unique registered account/chats')
        seen.add(item['thread_id'])
        route = item['route']
        if not isinstance(route,dict) or set(route)!={'provider','model','last_completed_turn_id','snapshot_sha256','evidence_ref'}:
            raise ServiceBlocked('Cold route snapshot evidence required')
        if route['provider']!='openai' or route['model'] not in models or not isinstance(route['snapshot_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',route['snapshot_sha256']):
            raise ServiceBlocked('Cold route old model or snapshot is unqualified')
        if not all(isinstance(route[k],str) and 0<len(route[k])<=1000 for k in ('last_completed_turn_id','evidence_ref')):
            raise ServiceBlocked('Cold route turn/evidence references missing')
    verify_routes(config)
    root = no_links(config['package_root']);state = no_links(config['state_dir'])
    if not root.is_dir() or state==root or state in root.parents:
        raise ServiceBlocked('Private state must be distinct from source roots')
    if not isinstance(config['files'],dict) or set(config['files'])!=package_files(root):
        raise ServiceBlocked('Exact code/dependency manifest membership changed')
    for relative, expected in config['files'].items():
        path = no_links(root/relative)
        if not path.is_relative_to(root) or not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected) or sha256(path)!=expected:
            raise ServiceBlocked('Reviewed code/dependency hash mismatch')
    external = config['external_files']
    required_external=({str(Path(p).absolute()) for p in EXTERNAL_REQUIRED} if test_artifacts
        else dependency_files(config['python_runtime']['path']))
    if not isinstance(external,dict) or set(external)!=required_external:
        raise ServiceBlocked('Exact installed credential/library dependency membership must be pinned')
    for path, expected in external.items():
        candidate = no_links(path)
        if candidate.name.lower() in ('auth.json','secrets.json','.env'):
            raise ServiceBlocked('Only reviewed dependency artifacts belong in the manifest')
        if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected) or sha256(candidate)!=expected:
            raise ServiceBlocked('External dependency hash mismatch')
    for key in ('native_runtime','python_runtime'):
        pin = config[key]
        if not isinstance(pin,dict) or set(pin)!={'path','sha256'} or sha256(no_links(pin['path']))!=pin['sha256']:
            raise ServiceBlocked('Pinned executable hash mismatch')
    native = Path(config['native_runtime']['path']).as_posix()
    if not test_artifacts and not re.fullmatch(r'C:/Program Files/WindowsApps/OpenAI\.Codex_[0-9.]+_(?:x64|arm64)__[A-Za-z0-9]+/app/resources/codex\.exe',native,re.I):
        raise ServiceBlocked('Qualified bundled native runtime required')
    return {'config_sha256':hashlib.sha256(encoded(config)).hexdigest(),
        'manifest_sha256':hashlib.sha256(encoded(config['files'])).hexdigest(),
        'native_runtime_sha256':config['native_runtime']['sha256']}


def read_config(path, expected_sha256):
    raw = no_links(path).read_bytes()
    if len(raw)>4*1024*1024 or not re.fullmatch('[0-9a-f]{64}',expected_sha256) or hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ServiceBlocked('Approved configuration hash mismatch')
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise ServiceBlocked('Duplicate configuration key')
            out[key]=value
        return out
    return json.loads(raw,object_pairs_hook=pairs)


def private_state(path,account_id):
    path = no_links(path);path.mkdir(parents=True,exist_ok=True)
    if os.name!='nt':
        raise ServiceBlocked('Production service requires Windows ACL qualification')
    system = Path(os.environ.get('SystemRoot','C:/Windows'))/'System32'
    who = subprocess.run([str(system/'whoami.exe'),'/user','/fo','csv','/nh'],capture_output=True,text=True,timeout=10,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    rows=list(csv.reader(who.stdout.splitlines()))
    if len(rows)!=1 or len(rows[0])!=2 or not re.fullmatch('S-1-[0-9-]+',rows[0][1]):
        raise ServiceBlocked('Current Windows user SID unavailable')
    sid=rows[0][1]
    marker=path/'continuity-state-owner.json'
    owner={'purpose':'codex-continuity-private-state','version':1,'account_id':account_id,'windows_sid':sid,'path':str(path)}
    descendants=list(path.rglob('*'))
    if len(descendants)>100000:raise ServiceBlocked('Owned state audit exceeds bound')
    for item in descendants:
        no_links(item)
        if item.is_file() and item.stat().st_nlink!=1:
            raise ServiceBlocked('Hard-linked state files cannot be privately repermissioned')
    if descendants and (not marker.is_file() or marker.read_bytes()!=encoded(owner)):
        raise ServiceBlocked('Existing state is not owned by this exact service/account/user')
    # Set a complete protected DACL rather than retaining old explicit grants.
    # Apply separately to every audited descendant, including protected files.
    set_private_acl(path,sid)
    for item in descendants:set_private_acl(item,sid)
    if not marker.exists():
        with marker.open('xb') as stream:
            stream.write(encoded(owner));stream.flush();os.fsync(stream.fileno())
    return path


def set_private_acl(path,sid):
    import ctypes
    from ctypes import wintypes
    path=no_links(path)
    descriptor=ctypes.c_void_p()
    advapi=ctypes.WinDLL('advapi32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    convert=advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
    convert.restype=wintypes.BOOL
    apply=advapi.SetFileSecurityW
    apply.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p];apply.restype=wintypes.BOOL
    kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    sddl='D:P(A;OICI;FA;;;'+sid+')' if path.is_dir() else 'D:P(A;;FA;;;'+sid+')'
    if not convert(sddl,1,ctypes.byref(descriptor),None):raise ServiceBlocked('Private Windows DACL construction failed')
    try:
        if not apply(str(path),0x00000004|0x80000000,descriptor):
            raise ServiceBlocked('Private Windows DACL application failed')
    finally:kernel.LocalFree(descriptor)


def serve(config, expected_config_hash, config_path):
    pins=verify_config(config)
    if pins['config_sha256']!=expected_config_hash:
        raise ServiceBlocked('Configuration must use canonical reviewed bytes')
    if Path(sys.executable).resolve()!=Path(config['python_runtime']['path']).resolve() or sha256(sys.executable)!=config['python_runtime']['sha256']:
        raise ServiceBlocked('Service must run the reviewed Python executable')
    identity=process_module(config);current=identity.process_identity(os.getpid())
    process_proof=identity.require_detached_venv_self(config['python_runtime'],python_base_pin(config),
        expected_args=service_arguments(config,config_path,expected_config_hash),user_sid=current['user_sid'],session_id=current['session_id'])
    root=Path(config['package_root']);state=private_state(config['state_dir'],config['account_id'])
    # Isolated CLI (-I) plus a new nonexistent cache prefix prevents stale .pyc
    # modules from superseding verified source. No bytecode is written.
    sys.dont_write_bytecode=True
    sys.pycache_prefix=str(state/('no-bytecode-'+secrets.token_hex(16)))
    os.environ['PYTHON_ZSTANDARD_IMPORT_POLICY']='cext'
    sys.path[:0]=[str(root/'continuity-v4'),str(root/'continuity-v3'),str(root/'continuity-v2'),str(root/'desktop-route-v2/deps'),
        str(Path(config['python_runtime']['path']).parent.parent/'Lib/site-packages')]
    import credentials
    credentials.DIRECTORY=state.parent/'secrets'
    from native_quota import NativeQuotaReader, windows_boot_epoch
    from production_adapters_v4 import ProductionAdapters, _credential
    from gateway_v4 import create_server
    from route_registry import RouteRegistry,RouteSpec
    from native_review_gateway_v4 import install_native_review
    from protocol_profiles_v4 import prepare_native
    from agy_bridge import prepare_body as prepare_agy_checkpoint
    from agy_executor import factory as agy_factory, protocol_preparer as prepare_agy_route, route_executor as agy_route_executor
    epoch=windows_boot_epoch()
    routes=RouteRegistry([RouteSpec(**v) for v in config['route_specs']],auto_fallback_id=config['auto_fallback_id'])
    default_cold=((config.get('default_cold_model') or ('gpt-6.1-sol' if 'gpt-6.1-sol' in config['openai_models'] else sorted(config['openai_models'])[0]))
        if (config['enroll_new_chats'] and config['allow_incomplete_visible_rebase']) else None)
    factory=ProductionAdapters(state_root=state,route_registry=routes,openai_models=set(config['openai_models']),
        default_cold_openai_model=default_cold,max_visible_source_bytes=4*1024*1024,
        provider_executor_factories={'antigravity':agy_factory},provider_preflight_encoders={'antigravity':prepare_agy_checkpoint},
        recovery_aliases=config['recovery_aliases'],clock_epoch=epoch,
        native_reader=NativeQuotaReader(state_root=state/'quota-isolation',executable=config['native_runtime']['path']),
        cold_routes={(item['account_id'],item['thread_id']):item['route'] for item in config['cold_routes']})
    server=create_server(route_registry=routes,credential_reader=lambda spec:_credential(spec.provider_id),state_dir=state/'gateway',capability=config['capability'],allowed_accounts={config['account_id']},
        protocol_preparers={'responses-full':prepare_native,'responses-lite':prepare_native,'agy-structured':prepare_agy_route},
        route_executors={'agy-structured':agy_route_executor(timeout=180)},
        registered_chats={(config['account_id'],chat) for chat in config['registered_chats']},clock_epoch=epoch,
        clock=time.monotonic,callback_factory=factory,openai_models=set(config['openai_models']),
        recovery_aliases=config['recovery_aliases'],enroll_new_chats=config['enroll_new_chats'],
        allow_incomplete_visible_rebase=config['allow_incomplete_visible_rebase'],allow_paid_compaction=config['allow_paid_compaction'],allow_paid_lite_compaction=config['allow_paid_lite_compaction'],
        host=config['host'],port=config['port'],timeout=300)
    install_native_review(server,enabled=config['allow_native_review'])
    health={'mode':'verified_multichat_continuity_service','pid':os.getpid(),'port':server.server_port,
        'clock_epoch':epoch,'architecture_version':4,'automatic_post_retries':False,'allow_paid_compaction':config['allow_paid_compaction'],
        'allow_native_review':config['allow_native_review'],'allow_paid_lite_compaction':config['allow_paid_lite_compaction'],
        'process_identity':process_proof,**pins}
    install_health(server,health)
    receipt=state/'service-running.json'
    with tempfile.NamedTemporaryFile(mode='wb',dir=state,delete=False) as output:
        output.write(encoded(health));output.flush();os.fsync(output.fileno());temporary=output.name
    os.replace(temporary,receipt)
    try:server.serve_forever(poll_interval=.25)
    finally:server.server_close()


def install_health(server,health):
    """Add pinned startup evidence to the existing capability-bound health URL."""
    health=dict(health)
    parent=server.RequestHandlerClass
    class VerifiedHandler(parent):
        def do_GET(self):
            if self.path=='/'+self.server.capability+'/health':
                if not self.local_allowed():self.reply(403,'Local native access only');return
                self.reply(200,health);return
            super().do_GET()
    server.RequestHandlerClass=VerifiedHandler


def launch_hidden(config_path, expected_sha256):
    """Explicit caller action only. Breakaway failure is reported, never hidden."""
    config=read_config(config_path,expected_sha256);verify_config(config)
    if os.name!='nt':raise ServiceBlocked('Hidden detached launch requires Windows')
    python_base_pin(config)
    script=Path(config['package_root'])/'continuity-v4/production_service_v4.py'
    flags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_BREAKAWAY_FROM_JOB
    env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','COMSPEC','SYSTEMDRIVE','USERPROFILE','APPDATA','LOCALAPPDATA'}}
    state=private_state(config['state_dir'],config['account_id']);logs=state/('launch-'+secrets.token_hex(16));logs.mkdir()
    with (logs/'stdout.log').open('xb') as stdout,(logs/'stderr.log').open('xb') as stderr:
        proc=subprocess.Popen([config['python_runtime']['path'],*service_arguments(config,config_path,expected_sha256)],
            cwd=script.parent,env=env,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,close_fds=True,creationflags=flags)
    proof=verified_service_process(proc.pid,config,config_path,expected_sha256)
    if proof is None or 'wrapper' not in proof:raise ServiceBlocked('Launched wrapper exited before ownership persistence')
    return {'pid':proc.pid,'config_sha256':expected_sha256,'health_verified':False,'process_identity':proof,'logs':str(logs)}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--config-sha256',required=True)
    args=parser.parse_args()
    serve(read_config(args.config,args.config_sha256),args.config_sha256,args.config)
