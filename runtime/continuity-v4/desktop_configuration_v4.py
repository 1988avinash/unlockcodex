"""Stage and atomically activate two Desktop routing keys with exact rollback.

Preserves every unrelated setting and native auth/history file. This module is
inert until the host explicitly calls stage/apply/rollback after peer review.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib
from urllib.parse import urlsplit

KEYS={'openai_base_url','model_catalog_json'}


def sha(raw):return hashlib.sha256(raw).hexdigest()


def atomic_write(path,raw):
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        f.write(raw);f.flush();os.fsync(f.fileno());temporary=Path(f.name)
    os.replace(temporary,path)


def _strip_routing_header(raw):
    lines=raw.removeprefix(b'\xef\xbb\xbf').decode('utf-8').splitlines(keepends=True)
    kept=[]
    for line in lines:
        stripped=line.strip()
        if stripped=='# Codex continuity route; exact original configuration retained in activation backup.':
            continue
        if any(stripped.startswith(k) and '=' in stripped and stripped.split('=',1)[0].strip()==k for k in KEYS):
            continue
        kept.append(line)
    while kept and not kept[0].strip():
        kept.pop(0)
    return ''.join(kept).encode('utf-8')


def stage(*,config_path,model_cache_path,service_config_path,directory,allow_replace=False):
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=False)
    config_path=Path(config_path).resolve();raw=config_path.read_bytes();current=tomllib.loads(raw.decode('utf-8-sig'))
    if not allow_replace and any(key in current for key in KEYS):raise ValueError('Existing routing/catalog requires explicit migration')
    base_raw=_strip_routing_header(raw) if allow_replace else raw.removeprefix(b'\xef\xbb\xbf')
    base_current={k:v for k,v in current.items() if k not in KEYS}
    service_raw=Path(service_config_path).read_bytes();service=json.loads(service_raw)
    if service['host']!='127.0.0.1':raise ValueError('Only local reviewed service routing is allowed')
    url=f"http://127.0.0.1:{service['port']}/{service['capability']}"
    models=json.loads(Path(model_cache_path).read_text(encoding='utf-8'))['models']
    admitted=set(service['openai_models']);existing={m['slug']:m for m in models}
    if not admitted<=existing.keys():raise ValueError('Configured model missing from native catalog')
    plan_aliases={s['native_alias'] for s in service['route_specs']}|set(service['recovery_aliases'])
    default=current.get('model')
    allowed_defaults=admitted|plan_aliases if allow_replace else admitted
    if default is not None and default not in allowed_defaults:raise ValueError('Current model is not qualified')
    template_slug=default if default in admitted else service['recovery_aliases'].get(default,'gpt-6.1-sol' if 'gpt-6.1-sol' in admitted else sorted(admitted)[0])
    output=copy.deepcopy(models)
    for model in output:
        # Preserve each qualified OpenAI model's upstream context_window and
        # max_context_window when present (defaulting to 131072 only when omitted).
        # Instructions, host tools, and mandatory reviewer capabilities stay intact.
        if model['slug'] in admitted:
            model['use_responses_lite']=False
            model.setdefault('context_window',131072)
            model.setdefault('max_context_window',131072)
        elif model.get('visibility')=='list':model['visibility']='hide'
    for spec in service['route_specs']:
        proof_path=Path(spec['qualification_ref'])
        proof_raw=proof_path.read_bytes()
        if sha(proof_raw)!=service['qualification_files'][str(proof_path)]:raise ValueError('Plan qualification changed')
        proof=json.loads(proof_raw)
        profile=proof.get('catalog_metadata')
        allowed={'context_window','max_context_window','effective_context_window_percent','auto_compact_token_limit',
            'input_modalities','supports_image_detail_original','supported_reasoning_levels','default_reasoning_level',
            'supports_search_tool','web_search_tool_type','supports_reasoning_effort_updates','priority'}
        if not isinstance(profile,dict) or set(profile)-allowed:raise ValueError('Reviewed plan catalog metadata required')
        if 'priority' in profile and (type(profile['priority']) is not int or not 1<=profile['priority']<=100):
            raise ValueError('Bounded integer catalog priority required')
        if not {'context_window','max_context_window','input_modalities'}<=profile.keys():raise ValueError('Plan context/modality qualification required')
        if (any(type(profile[k]) is not int for k in ('context_window','max_context_window'))
                or not 8192<=profile['context_window']<=profile['max_context_window']<=2*1024*1024):
            raise ValueError('Bounded reviewed context window required')
        if 'effective_context_window_percent' in profile and (type(profile['effective_context_window_percent']) is not int or not 50<=profile['effective_context_window_percent']<=100):
            raise ValueError('Bounded effective context window percent required')
        if 'auto_compact_token_limit' in profile and profile['auto_compact_token_limit'] is not None and (type(profile['auto_compact_token_limit']) is not int or not 8192<=profile['auto_compact_token_limit']<=profile['context_window']):
            raise ValueError('Bounded auto compact token limit required')
        modalities=profile['input_modalities']
        if not isinstance(modalities,list) or not modalities or any(v not in ('text','image') for v in modalities):
            raise ValueError('Qualified native input modalities required')
        for key in ('supports_image_detail_original','supports_search_tool','supports_reasoning_effort_updates'):
            if key in profile and type(profile[key]) is not bool:raise ValueError('Explicit model capability required')
        fallback=copy.deepcopy(existing[template_slug])
        fallback.update(profile)
        fallback.update(slug=spec['native_alias'],display_name=spec['display_name'],visibility='list',
            description='Existing '+spec['display_name']+'. Same chat with visible history and an outgoing-model handoff.',
            use_responses_lite=spec['protocol_profile']=='responses-lite')
        # Safety flags and tool execution stay native; vendor convenience defaults
        # may not disable an existing mandatory reviewer or enable broader actions.
        for key in ('node_repl_auto_review_required','requires_sandboxed_review','auto_review_model_override','tool_mode'):
            if key in existing[template_slug]:fallback[key]=existing[template_slug][key]
        output.append(fallback)
    for alias,actual in service['recovery_aliases'].items():
        item=copy.deepcopy(existing[actual]);item.update(slug=alias,
            display_name='Return to ChatGPT — '+item.get('display_name',actual),use_responses_lite=False,
            description='Return this chat to included ChatGPT allowance when the recovery margin is available.')
        item.setdefault('context_window',131072)
        item.setdefault('max_context_window',131072)
        output.append(item)
    if len({m['slug'] for m in output})!=len(output):raise ValueError('Catalog alias collision')
    catalog=directory/'catalog.json';catalog_raw=json.dumps({'models':output},ensure_ascii=False,indent=2).encode()
    catalog.write_bytes(catalog_raw)
    prefix=('# Codex continuity route; exact original configuration retained in activation backup.\n'
        'openai_base_url = '+json.dumps(url)+'\nmodel_catalog_json = '+json.dumps(catalog.as_posix())+'\n\n').encode()
    candidate=prefix+base_raw
    parsed=tomllib.loads(candidate.decode())
    if {k:v for k,v in parsed.items() if k not in KEYS}!=base_current:raise ValueError('Unrelated settings changed')
    (directory/'original-config.toml').write_bytes(raw);(directory/'candidate-config.toml').write_bytes(candidate)
    manifest={'config_path':str(config_path),'original_sha256':sha(raw),'candidate_sha256':sha(candidate),
        'catalog_sha256':sha(catalog_raw),'service_config_path':str(Path(service_config_path).resolve()),
        'service_config_sha256':sha(service_raw),'route_url':url,'auth_modified':False,'histories_modified':False}
    (directory/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return manifest


def apply(directory,health):
    directory=Path(directory).resolve();manifest=json.loads((directory/'manifest.json').read_text())
    if health.get('mode')!='verified_multichat_continuity_service' or health.get('config_sha256')!=manifest['service_config_sha256']:
        raise ValueError('Matching live service health is required')
    config=Path(manifest['config_path']);candidate=(directory/'candidate-config.toml').read_bytes()
    if sha(config.read_bytes())!=manifest['original_sha256'] or sha(candidate)!=manifest['candidate_sha256']:
        raise ValueError('Configuration changed since staging')
    if sha((directory/'catalog.json').read_bytes())!=manifest['catalog_sha256']:
        raise ValueError('Catalog changed since review')
    if sha(Path(manifest['service_config_path']).read_bytes())!=manifest['service_config_sha256']:
        raise ValueError('Service configuration changed since review')
    atomic_write(config,candidate)
    if sha(config.read_bytes())!=manifest['candidate_sha256']:raise ValueError('Configuration readback failed')
    (directory/'applied.json').write_text(json.dumps({'config_sha256':sha(candidate),'service_pid':health['pid']}),encoding='utf-8')


def rollback(directory):
    directory=Path(directory).resolve();manifest=json.loads((directory/'manifest.json').read_text())
    config=Path(manifest['config_path']);original=(directory/'original-config.toml').read_bytes()
    if sha(config.read_bytes())!=manifest['candidate_sha256'] or sha(original)!=manifest['original_sha256']:
        raise ValueError('Configuration changed; refusing to overwrite concurrent work')
    atomic_write(config,original)
    return {'restored_sha256':sha(config.read_bytes()),'restart_required':True}
