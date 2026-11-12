"""Owned, one-shot AGY startup or synthetic inference diagnostic; no retries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'continuity-v3'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'continuity-v4'))
from native_quota import _OwnedJob,_check_private_path
from production_service_v4 import private_state
from agy_bridge import AgyError,decode_result,validate_event

EXE=Path(os.environ.get('LOCALAPPDATA',''))/'agy/bin/agy.exe'
EXE_SHA256='fe9eb2f3884ca6cd630c57eed117580819341bce4b87d2c40b441ddd01cb8f2a'
MODEL='gemini-3.8-flash-low'
SCHEMA={'type':'object','properties':{'kind':{'const':'message'},'text':{'type':'string'}},'required':['kind','text'],'additionalProperties':False}
SETTINGS={'modelProvider':'','useG1Credits':False,'toolPermission':'strict','agentMode':'plan','allowNonWorkspaceAccess':False,
          'permissions':{'allow':[],'ask':[],'deny':[x+'(*)' for x in ['command','read_file','write_file','read_url','execute_url','mcp','unsandboxed']]},
          'disableSlashCommands':True,'statusLine':False,'title':False,'notifications':False}

def write(path,value):
    with path.open('x',encoding='utf-8') as out: json.dump(value,out,ensure_ascii=False,indent=2)

def stage(folder,schema=SCHEMA,*,home=None):
    home=Path.home() if home is None else Path(home); global_config=home/'.gemini/config'
    # Hooks are executed before permission checks. Do not start if present.
    if (global_config/'hooks.json').exists(): raise AgyError('global_hooks_require_review')
    cfg=json.loads((global_config/'config.json').read_text(encoding='utf-8-sig'))
    plugins=set(cfg.get('plugins',{}))
    for root in [global_config/'plugins',home/'.gemini/antigravity-cli/plugins']:
        if root.is_dir(): plugins.update(p.name for p in root.iterdir() if p.is_dir())
    servers=json.loads((global_config/'mcp_config.json').read_text(encoding='utf-8-sig')).get('mcpServers',{})
    # Win32 CreateProcess cannot use an arbitrarily long current directory.
    # Durable evidence remains in the exact account/chat/request scope.
    owner=Path(tempfile.mkdtemp(prefix='codex-agy-')).absolute()
    private_state(owner,'agy-native-workspace')
    workspace=owner/'workspace'; workspace.mkdir(); (workspace/'.git').mkdir()
    config=workspace/'.agents'; config.mkdir()
    write(config/'plugins.json',{'entries':[],'exclude':sorted(plugins)})
    write(config/'mcp_config.json',{'mcpServers':{name:{'disabled':True} for name in servers}})
    write(folder/'settings.json',SETTINGS); write(folder/'schema.json',schema)
    return workspace,{'excluded_plugins':sorted(plugins),'disabled_mcp_names':sorted(servers),'global_hooks_file_absent':True,'owned_temporary_workspace':str(workspace)}

def release_workspace(workspace):
    root=Path(workspace).absolute().parent
    temp_root=Path(tempfile.gettempdir()).resolve()
    if root.parent.resolve()!=temp_root or not root.name.startswith('codex-agy-') or Path(workspace).name!='workspace':
        raise AgyError('temporary_workspace_ownership_mismatch')
    _check_private_path(root)
    for path in root.rglob('*'): _check_private_path(path)
    shutil.rmtree(root)

def run(folder,live=False,timeout=90,prepared=None,scope=None):
    if hashlib.sha256(EXE.read_bytes()).hexdigest()!=EXE_SHA256: raise AgyError('native_executable_changed_requires_requalification')
    if not 1<=timeout<=180: raise AgyError('bounded_timeout_required')
    if prepared is not None:
        from agy_bridge import strict_json,prepare_body,resolve_cli_model
        packet=strict_json(prepared)
        if prepare_body(packet['source'])!=prepared: raise AgyError('prepared_packet_not_canonical')
        model=packet['model']; cli_model=resolve_cli_model(packet['source']); schema=packet['schema']; tools=packet['tools']
    else: model=MODEL;cli_model=MODEL;schema=SCHEMA;tools=[]
    folder=Path(folder).resolve(); folder.mkdir(parents=True,exist_ok=False)
    workspace,isolation=stage(folder,schema)
    receipt={'logical_id':folder.name,'status':'reserved','live_prompt_authorized':live,'model':model,'cli_model':cli_model,
             'executable_sha256':hashlib.sha256(EXE.read_bytes()).hexdigest(),'isolation':isolation}
    if scope is not None: receipt['scope']=dict(scope)
    write(folder/'reservation.json',receipt)
    if prepared is not None:
        (folder/'prepared-request.json').write_bytes(prepared)
    print_timeout=f'{max(75,min(int(timeout)-15,165))}s'
    args=[str(EXE),'--input-format','stream-json','--output-format','stream-json','--model',cli_model,
          '--settings',str(folder/'settings.json'),'--json-schema',str(folder/'schema.json'),
          '--disable-slash-commands','--print-timeout',print_timeout,'--log-file',str(folder/'native.log')]
    environment={k:v for k,v in os.environ.items() if k.upper() not in {'GEMINI_API_KEY','GOOGLE_API_KEY','GOOGLE_APPLICATION_CREDENTIALS','GOOGLE_GENAI_USE_VERTEXAI','ANTHROPIC_API_KEY','OPENAI_API_KEY'}}
    events=[]; buffers={'stdout':bytearray(),'stderr':bytearray()}; incoming=queue.Queue(maxsize=128); process=None; owned=None; reader_threads=[]
    prompt_sent=False; started=time.monotonic()
    def reader(name,stream):
        try:
            while True:
                line=stream.readline(262145)
                if not line: break
                incoming.put((name,line))
        finally: incoming.put((name,None))
    try:
        process=subprocess.Popen(args,cwd=workspace,env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0,
            start_new_session=os.name!='nt')
        owned=_OwnedJob(process)
        for name,stream in [('stdout',process.stdout),('stderr',process.stderr)]:
            thread=threading.Thread(target=reader,args=(name,stream),daemon=True); thread.start(); reader_threads.append(thread)
        closed=set()
        while len(closed)<2:
            if time.monotonic()-started>timeout: raise AgyError('native_timeout_uncertain' if prompt_sent else 'startup_timeout')
            try: name,line=incoming.get(timeout=.2)
            except queue.Empty: continue
            if line is None: closed.add(name); continue
            buffers[name].extend(line)
            if len(line)>1024*1024 or len(buffers[name])>8*1024*1024: raise AgyError('native_output_limit')
            if name=='stderr': continue
            event=json.loads(line); events.append(event)
            validate_event(event,tools)
            if event.get('event')=='init':
                initial=event.get('init',{})
                if initial.get('model')!=cli_model or initial.get('permission_mode')!='strict' or initial.get('json_schema')!=schema: raise AgyError('native_selection_or_permissions_mismatch')
                # call_mcp_tool is a built-in dispatcher, not evidence of a loaded
                # external server. Never infer a tool-free runtime from this.
                if any('mcp' in str(t).lower() and t!='call_mcp_tool' for t in initial.get('tools',[])):
                    raise AgyError('external_mcp_exposure_not_isolated')
                log=(folder/'native.log').read_text(encoding='utf-8',errors='replace')
                hook_counts=re.findall(r'loaded (\d+) named hooks from (\d+) hooks.json file\(s\)',log)
                if not hook_counts or any(pair!=('0','0') for pair in hook_counts):
                    raise AgyError('zero_runtime_hooks_unconfirmed')
                if live:
                    prompt=packet['prompt'] if prepared is not None else 'This is a pure structured-output diagnostic for final completion metadata. Do not execute any tool, subagent, file, command or web/MCP action. The sole allowed native control is finish to finalize the schema object. Call finish with kind message and text SYNTHETIC_AGY_FINAL_OK; do not emit a preliminary prose answer.'
                    process.stdin.write((json.dumps({'event':'user','message':{'content':prompt}})+'\n').encode()); process.stdin.flush(); prompt_sent=True
                process.stdin.close()
        code=process.wait(timeout=3)
        if code!=0: raise AgyError('native_exit_'+str(code))
        if live:
            import jsonschema
            final=[event['result'] for event in events if event.get('event')=='result']
            if len(final)!=1: raise AgyError('missing_or_multiple_native_results')
            jsonschema.Draft202012Validator(schema).validate(final[0].get('structured_output'))
            response=decode_result(events,cli_model,tools)
            if cli_model!=model:
                response['adapter_evidence']['cli_selected_model']=cli_model
                response['model']=model
            if prepared is None and response['output'][0]['content'][0]['text']!='SYNTHETIC_AGY_FINAL_OK': raise AgyError('diagnostic_marker_mismatch')
            write(folder/'translated-response.json',response)
        receipt.update(status='completed' if live else 'startup_completed',returncode=code)
    except Exception as exc:
        receipt.update(status='uncertain' if prompt_sent else 'startup_failed',error=str(exc),error_type=type(exc).__name__)
    finally:
        if owned: owned.close()
        elif process and process.poll() is None: process.kill()
        if process:
            try: process.wait(timeout=4)
            except subprocess.TimeoutExpired: receipt['owned_process_exit_unconfirmed']=True
        for thread in reader_threads: thread.join(timeout=.5)
        # Preserve terminal output already read by the subprocess reader even
        # when the validator aborted; do not infer completion from this drain.
        while not incoming.empty():
            name,line=incoming.get_nowait()
            if line is not None and len(buffers[name])+len(line)<=8*1024*1024:
                buffers[name].extend(line)
        for name,data in buffers.items(): (folder/(name+'.ndjson' if name=='stdout' else name+'.txt')).write_bytes(data)
        receipt.update(prompt_sent=prompt_sent,elapsed_seconds=time.monotonic()-started,event_count=len(events))
        log=(folder/'native.log').read_text(encoding='utf-8',errors='replace') if (folder/'native.log').exists() else ''
        receipt['native_model_request_log_count']=sum('streamGenerateContent' in line for line in log.splitlines())
        receipt['native_request_count_is_log_observation_not_billing_receipt']=True
        if not receipt.get('owned_process_exit_unconfirmed'):
            try:release_workspace(workspace);receipt['owned_workspace_removed']=True
            except Exception as exc:receipt['owned_workspace_removed']=False;receipt['workspace_cleanup_error']=type(exc).__name__
        write(folder/'receipt.json',receipt)
    return receipt

