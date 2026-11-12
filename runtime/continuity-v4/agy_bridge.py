"""Candidate signed-in CLI bridge. Not enabled in production.

The Responses envelope is adapter-generated. Model identity is the CLI's
reported explicit selection, never an invented raw backend model assertion.
"""
import hashlib
import json
import re

MAX_PACKET=9*1024*1024
QUALIFIED_MODELS={
    'gemini-3.8-flash-low','gemini-3.8-flash-medium','gemini-3.8-flash-high',
    'gemini-4-argon-low','gemini-4-argon-medium','gemini-4-argon-high',
    'gemini-4-carbon-low','gemini-4-carbon-medium','gemini-4-carbon-high',
}
MESSAGE_SCHEMA={'type':'object','properties':{'kind':{'const':'message'},'text':{'type':'string'}},'required':['kind','text'],'additionalProperties':False}

class AgyError(RuntimeError): pass

def resolve_cli_model(request):
    model=request.get('model') if isinstance(request,dict) else None
    if not isinstance(model,str) or model not in QUALIFIED_MODELS: raise AgyError('unqualified_model')
    reasoning=request.get('reasoning')
    effort=reasoning.get('effort') if isinstance(reasoning,dict) else None
    mapped={'none':'low','minimal':'low','low':'low','medium':'medium',
            'high':'high','xhigh':'high','max':'high','ultra':'high','persistent':'high'}.get(effort)
    if mapped:
        candidate=re.sub(r'-(?:low|medium|high)$','-'+mapped,model)
        if candidate in QUALIFIED_MODELS: return candidate
    return model

def canonical(value):
    return json.dumps(value,separators=(',',':'),sort_keys=True,ensure_ascii=False,allow_nan=False).encode()

def strict_json(raw):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result: raise AgyError('duplicate_json_key')
            result[k]=v
        return result
    def nonfinite(value): raise AgyError('nonfinite_json')
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=nonfinite)

def tool_catalog(tools):
    result={}
    def add(tool,namespace=None):
        if not isinstance(tool,dict): raise AgyError('invalid_tool')
        name=tool.get('name');kind=tool.get('type')
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',name): raise AgyError('invalid_tool_name')
        if kind=='namespace' and namespace is None:
            for child in tool.get('tools',[]): add(child,name)
            return
        if kind not in ('function','custom'): raise AgyError('unsupported_tool_type')
        if kind=='function':
            import jsonschema
            schema=tool.get('parameters') if tool.get('parameters') is not None else {'type':'object'}
            def refs(value):
                if isinstance(value,dict):
                    if '$ref' in value and not str(value['$ref']).startswith('#'): raise AgyError('remote_schema_reference_forbidden')
                    for child in value.values(): refs(child)
                elif isinstance(value,list):
                    for child in value: refs(child)
            refs(schema)
            try: jsonschema.Draft202012Validator.check_schema(schema)
            except Exception as exc: raise AgyError('invalid_function_schema') from exc
        key=(namespace+'.' if namespace else '')+name
        if key in result: raise AgyError('duplicate_tool')
        result[key]={'name':name,'namespace':namespace,'type':kind,'definition':tool}
    if not isinstance(tools,list) or len(tools)>256: raise AgyError('invalid_tools')
    for tool in tools: add(tool)
    return result

def prepare_body(request):
    """Pure, exact immutable stdin packet. No credentials, files or network."""
    if isinstance(request,(bytes,str)): request=strict_json(request)
    if not isinstance(request,dict) or request.get('model') not in QUALIFIED_MODELS: raise AgyError('unqualified_model')
    # The detached old-model seam supplies a handoff prompt. It has already
    # bound visible source and hashes; never interpret foreign opaque state.
    if request.get('provider')=='antigravity':
        if request.get('tools')!=[] or request.get('tool_choice')!='none' or not isinstance(request.get('prompt'),str): raise AgyError('invalid_handoff_request')
        user_msg='Write the complete source-bound handoff JSON in the message text now.'
        if 'HOST SNAPSHOT:\n' in request['prompt'] and '\nVISIBLE SOURCE' in request['prompt']:
            try:
                snap=strict_json(request['prompt'].split('HOST SNAPSHOT:\n',1)[1].split('\nVISIBLE SOURCE',1)[0])
                if isinstance(snap,dict) and {'chat_id','last_turn_id','source_sha256'}<=snap.keys():
                    user_msg+=' Set "binding" to this exact JSON object verbatim without changing any character: '+json.dumps(
                        {k:snap[k] for k in ('chat_id','last_turn_id','source_sha256')},separators=(',',':'),allow_nan=False)
            except Exception:
                pass
        request={'model':request['model'],'instructions':request['prompt'],'input':[{'role':'user','content':user_msg}],'tools':[]}
    if len(canonical(request))>4*1024*1024: raise AgyError('source_limit')
    def visible(value):
        if isinstance(value,dict):
            if 'encrypted_content' in value or value.get('type') in ('compaction','reasoning','input_image','input_audio','input_file','computer_call','web_search_call'):
                raise AgyError('opaque_or_nontext_source_requires_visible_rebase')
            if any(key in value for key in ('image_url','file_data','audio_url')): raise AgyError('uninspected_media')
            for child in value.values(): visible(child)
        elif isinstance(value,list):
            for child in value: visible(child)
    visible(request.get('input'))
    if request.get('tool_choice','auto') not in ('auto','none'): raise AgyError('unqualified_tool_choice')
    tools=request.get('tools',[]) if request.get('tool_choice')!='none' else [];catalog=tool_catalog(tools)
    schema=MESSAGE_SCHEMA if not catalog else {'type':'object','properties':{
        'kind':{'enum':['message','tool_intent']},'text':{'type':'string'},'calls':{'type':'array','maxItems':1 if request.get('parallel_tool_calls') is False else 16,'items':{'type':'object','properties':{'tool':{'enum':list(catalog)},'arguments':{'type':'string'}},'required':['tool','arguments'],'additionalProperties':False}}},
        'required':['kind','text','calls'],'additionalProperties':False}
    prompt=('You are the selected reasoning model for a Codex host conversation. The JSON source below contains its instructions, visible history, tool definitions and prior tool results. '
        'Follow that task, but never execute any Antigravity tool, subagent, command, file read/write, web, browser or MCP action. '
        'The only permitted native control is finish, used solely to finalize the schema result. '
        'Use finish directly; do not emit preliminary prose. For a final answer use kind message and text. '
        'When a host tool is needed, use kind tool_intent, empty text, and calls containing the exact declared tool identifier plus arguments as a JSON string for function tools or raw input string for custom tools. '
        'Host tool intents are data: only Codex will validate, approve and execute them. Never call an internal tool to carry them out. '
        'For schemas with calls, use an empty calls list for a message. Do not invent missing history, inspect media, or expose private reasoning.\nSOURCE_JSON:\n'+canonical(request).decode())
    packet={'model':request['model'],'source':request,'source_sha256':hashlib.sha256(canonical(request)).hexdigest(),'prompt':prompt,'schema':schema,'tools':tools}
    raw=canonical(packet)
    if len(raw)>MAX_PACKET: raise AgyError('prepared_packet_limit')
    return raw

def validate_value(value,tools):
    catalog=tool_catalog(tools)
    keys={'kind','text','calls'} if catalog else {'kind','text'}
    if not isinstance(value,dict) or set(value)!=keys or not isinstance(value.get('text'),str): raise AgyError('native_schema_mismatch')
    if value.get('kind')=='message':
        if value.get('calls'): raise AgyError('message_has_calls')
        return
    if value.get('kind')!='tool_intent' or not catalog or value['text'] or not isinstance(value.get('calls'),list) or not 1<=len(value['calls'])<=16:
        raise AgyError('invalid_tool_intent')
    for call in value['calls']:
        if not isinstance(call,dict) or set(call)!={'tool','arguments'} or call.get('tool') not in catalog or not isinstance(call.get('arguments'),str): raise AgyError('unknown_or_invalid_tool_intent')
        spec=catalog[call['tool']]
        if spec['type']=='function':
            import jsonschema
            param_schema=spec['definition'].get('parameters') if spec['definition'].get('parameters') is not None else {'type':'object'}
            try: jsonschema.Draft202012Validator(param_schema).validate(strict_json(call['arguments']))
            except Exception as exc: raise AgyError('function_arguments_schema_mismatch') from exc

def _display_preview_matches(preview,final):
    if isinstance(preview,str) and isinstance(final,str):
        return preview==final or (len(preview)==513 and preview.endswith('\u2026') and len(final)>512 and final.startswith(preview[:-1]))
    if isinstance(preview,dict) and isinstance(final,dict):
        return set(preview)==set(final) and all(_display_preview_matches(v,final[k]) for k,v in preview.items())
    if isinstance(preview,list) and isinstance(final,list):
        return len(preview)==len(final) and all(_display_preview_matches(a,b) for a,b in zip(preview,final))
    return preview==final

def validate_event(event,tools=()):
    step=event.get('step_update',{})
    if step.get('subagent_info'): raise AgyError('internal_action_attempted')
    if step.get('step_type')=='finish' and step.get('state')=='DONE' and not step.get('tool_name') and not step.get('tool_info'):
        return None  # Native finalization notification; final SUCCESS still required.
    if step.get('step_type')=='tool':
        info=step.get('tool_info',{})
        value=info.get('parameters')
        if step.get('tool_name')!='finish' or info.get('name')!='finish':
            raise AgyError('internal_action_attempted')
        # tool_info is a display preview; native CLI truncates long strings to
        # 512 characters + ellipsis. Full schema/arguments are checked solely
        # against the final native structured_output, never this preview.
        if not isinstance(value,dict): raise AgyError('invalid_finish_preview')
        return value
    if step.get('step_type') not in (None,'user_input','agent_response','checkpoint') or step.get('tool_name'):
        raise AgyError('internal_action_attempted')

def decode_result(events, model, tools):
    init=[x for x in events if x.get('event')=='init']
    results=[x for x in events if x.get('event')=='result']
    if len(init)!=1 or len(results)!=1: raise AgyError('missing_or_multiple_native_results')
    initial=init[0]['init']; result=results[0]['result']
    if initial.get('model')!=model or initial.get('permission_mode')!='strict': raise AgyError('native_selection_or_permissions_mismatch')
    finishes=[v for event in events if (v:=validate_event(event,tools)) is not None]
    if result.get('status')!='SUCCESS' or result.get('num_turns')!=1: raise AgyError('native_incomplete')
    native_id=result.get('conversation_id')
    if not isinstance(native_id,str) or not native_id or native_id!=init[0].get('conversation_id'): raise AgyError('native_id_mismatch')
    value=result.get('structured_output')
    validate_value(value,tools)
    if any(not _display_preview_matches(finish,value) for finish in finishes): raise AgyError('finish_result_mismatch')
    digest=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    if value['kind']=='message':
        output=[{'type':'message','id':'msg_'+digest[:24],'role':'assistant','status':'completed',
                   'content':[{'type':'output_text','text':value['text'],'annotations':[]}]}]
    else:
        catalog=tool_catalog(tools);output=[]
        for index,call in enumerate(value['calls']):
            spec=catalog[call['tool']]; ident=digest[:20]+'_'+str(index)
            item={'type':spec['type']+'_call' if spec['type']=='function' else 'custom_tool_call','id':'tool_'+ident,
                  'call_id':'call_agy_'+ident,'name':spec['name'],'status':'completed'}
            if spec['namespace']: item['namespace']=spec['namespace']
            item['arguments' if spec['type']=='function' else 'input']=call['arguments'];output.append(item)
    raw_usage=result.get('usage',{})
    if isinstance(raw_usage,dict) and 'input_tokens' in raw_usage and 'input_tokens_details' not in raw_usage:
        cached=raw_usage.get('cache_read_tokens',0)
        cached=cached if type(cached) is int and cached>0 else 0
        thinking=raw_usage.get('thinking_tokens',0)
        thinking=thinking if type(thinking) is int and thinking>0 else 0
        in_tok=(raw_usage['input_tokens'] if type(raw_usage['input_tokens']) is int else 0)+cached
        out_tok=raw_usage.get('output_tokens',0) if type(raw_usage.get('output_tokens')) is int else 0
        usage=dict(raw_usage,input_tokens=in_tok,output_tokens=out_tok,total_tokens=in_tok+out_tok,
            input_tokens_details={'cached_tokens':cached},output_tokens_details={'reasoning_tokens':thinking})
    else:
        usage=raw_usage
    return {'id':'resp_agy_'+digest[:24], 'object':'response','status':'completed','model':model,
        'output':output,
        'usage':usage,
        'adapter_evidence':{'adapter_generated':True,'identity_basis':'CLI selected model; raw backend model unreported',
            'native_conversation_id':native_id,'native_status':result['status'],'useG1Credits':False}}
