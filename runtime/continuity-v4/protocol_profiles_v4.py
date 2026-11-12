"""Validate the native selected model's protocol; never convert full to lite."""
import json


def prepare_native(raw, spec):
    body=json.loads(raw)
    items=body.get('input')
    controls=[(n,item) for n,item in enumerate(items) if isinstance(item,dict) and item.get('type')=='additional_tools'] if isinstance(items,list) else []
    if spec.protocol_profile=='responses-lite':
        if not controls or 'tools' in body:
            raise ValueError('Selected plan requires actual native Responses lite serialization')
        for _,item in controls:
            if item.get('role')!='developer' or not isinstance(item.get('tools'),list):
                raise ValueError('Invalid native lite tool declaration')
        if len(controls)>1:
            identities=[item.get('id') for _,item in controls]
            if any(not isinstance(value,str) or not value for value in identities) or len(set(identities))!=len(identities):
                raise ValueError('Multiple native tool declarations need distinct stable identities')
        if any(item.get('type')=='configuration_update' for item in items):
            raise ValueError('Full configuration updates cannot be silently translated to lite')
    elif spec.protocol_profile=='responses-full':
        if controls:raise ValueError('Full route cannot consume native lite declarations')
    else:
        raise ValueError('Protocol requires an explicitly qualified preparer')
    body['model']=spec.actual_model
    body.pop('client_metadata',None);body.pop('prompt_cache_key',None)
    body.pop('access_programs',None)  # OpenAI-only control
    if spec.provider_id=='mimo':
        inc=body.get('include')
        if isinstance(inc,list):
            filtered=[x for x in inc if x!='output.encrypted_content']
            if filtered:body['include']=filtered
            else:body.pop('include',None)
    reasoning=body.get('reasoning')
    if isinstance(reasoning,dict):
        effort=reasoning.get('effort')
        if spec.provider_id=='minimax' and effort in ('none','minimal','ultra','persistent'):
            reasoning['effort']={'none':'low','minimal':'low','ultra':'max','persistent':'max'}[effort]
        elif spec.provider_id=='mimo' and effort in ('minimal','xhigh','max','ultra','persistent'):
            reasoning['effort']='low' if effort=='minimal' else 'high'
        elif spec.provider_id=='alibaba':
            if spec.actual_model=='glm-5.3' and effort in ('none','minimal'):
                reasoning['effort']='low'
            elif spec.actual_model=='qwen3.6-flash' and effort in ('minimal','high','xhigh','max','ultra','persistent'):
                reasoning['effort']='low' if effort=='minimal' else 'medium'
            elif effort in ('minimal','max','ultra','persistent'):
                reasoning['effort']='low' if effort=='minimal' else 'xhigh'
        elif spec.provider_id not in ('minimax','mimo') and effort in ('none','minimal','xhigh','max','ultra','persistent'):
            # Untuned provider: stay within the low/medium/high set every Responses implementation accepts.
            reasoning['effort']='low' if effort in ('none','minimal') else 'high'
    return json.dumps(body,separators=(',',':'),allow_nan=False).encode()
