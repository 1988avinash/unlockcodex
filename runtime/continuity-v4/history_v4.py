"""Transport-only history adapter. Never modifies a canonical chat or decrypts state.

Public bytes-in/bytes-out seam; the caller owns routing, durable receipts and the
trusted chat identity. Preserve original_bytes before dispatching request_bytes.
"""
from dataclasses import dataclass
import hashlib
import json
import re

DEFAULT_MAX_BYTES = 1048576
MAX_TOOL_DESCRIPTION_UNITS = 950000


def _desc_units(text):
    return max(len(text), len(text.encode('utf-16-le')) // 2)


def _bound_description_text(desc, limit=MAX_TOOL_DESCRIPTION_UNITS):
    if not isinstance(desc, str) or _desc_units(desc) <= limit:
        return desc, False
    candidate = desc
    if '\n### ' in desc:
        parts = desc.split('\n### ')
        header, sections = parts[0], parts[1:]
        kept = []
        omitted_apps = 0
        for sec in sections:
            heading = sec.split('\n', 1)[0].strip()
            if heading.startswith('`mcp__codex_apps__') or heading.startswith('mcp__codex_apps__'):
                omitted_apps += 1
            else:
                kept.append(sec)
        if omitted_apps:
            note = (
                '\n\n[Deferred mcp__codex_apps__* declarations omitted to fit upstream '
                '1,048,576-character tool description limit; all nested tools remain '
                'callable on tools.* and searchable via ALL_TOOLS.]'
            )
            candidate = header + ''.join('\n### ' + sec for sec in kept) + note
    if _desc_units(candidate) > limit:
        suffix = (
            '\n\n[Additional nested tool declarations omitted to fit upstream '
            '1,048,576-character schema limit; all nested tools remain callable on '
            'tools.* and searchable via ALL_TOOLS.]'
        )
        budget = max(0, limit - _desc_units(suffix))
        prefix = candidate[:budget]
        while prefix and _desc_units(prefix) > budget:
            excess = _desc_units(prefix) - budget
            prefix = prefix[:-max(1, excess)]
        cut = prefix.rfind('\n### ')
        if cut < len(prefix) // 2:
            cut = prefix.rfind('\n')
        if cut >= len(prefix) // 2:
            prefix = prefix[:cut]
        candidate = prefix + suffix
    return candidate, candidate != desc


def _bound_tool_entry(tool, limit):
    if not isinstance(tool, dict):
        return tool, False
    changed = False
    updated = tool
    desc = tool.get('description')
    if isinstance(desc, str):
        bounded, did_bound = _bound_description_text(desc, limit)
        if did_bound:
            updated = dict(updated, description=bounded)
            changed = True
    nested = updated.get('tools')
    if isinstance(nested, list):
        nested_out = []
        nested_changed = False
        for child in nested:
            bounded_child, child_changed = _bound_tool_entry(child, limit)
            nested_out.append(bounded_child)
            nested_changed |= child_changed
        if nested_changed:
            if updated is tool:
                updated = dict(tool)
            updated['tools'] = nested_out
            changed = True
    return updated, changed


def bound_tool_descriptions(body, *, limit=MAX_TOOL_DESCRIPTION_UNITS):
    """Bound oversized tool description strings (>950k units) without mutating shared dicts."""
    if not isinstance(body, dict):
        return False
    changed = False
    tools = body.get('tools')
    if isinstance(tools, list):
        tools_out = []
        tools_changed = False
        for tool in tools:
            bounded_tool, tool_changed = _bound_tool_entry(tool, limit)
            tools_out.append(bounded_tool)
            tools_changed |= tool_changed
        if tools_changed:
            body['tools'] = tools_out
            changed = True
    items = body.get('input')
    if isinstance(items, list):
        items_out = []
        items_changed = False
        for item in items:
            if isinstance(item, dict) and item.get('type') == 'additional_tools' and isinstance(item.get('tools'), list):
                decl_tools = []
                decl_changed = False
                for tool in item['tools']:
                    bounded_tool, tool_changed = _bound_tool_entry(tool, limit)
                    decl_tools.append(bounded_tool)
                    decl_changed |= tool_changed
                if decl_changed:
                    item = dict(item, tools=decl_tools)
                    items_changed = True
            items_out.append(item)
        if items_changed:
            body['input'] = items_out
            changed = True
    return changed


def _parse(raw, max_bytes=DEFAULT_MAX_BYTES):
    if not isinstance(raw,bytes) or len(raw)>max_bytes:
        raise HistoryBlocked('History size exceeds the configured bound')
    def object_pairs(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        value = json.loads(raw,object_pairs_hook=object_pairs)
        json.dumps(value,allow_nan=False)
        if not isinstance(value,dict):
            raise ValueError('Requires JSON object')
        return value
    except (ValueError,TypeError,UnicodeError,RecursionError) as exc:
        raise HistoryBlocked('Invalid or ambiguous history JSON') from exc


def _request_input(value):
    items = value.get('input')
    if not isinstance(items,(list,str)) or isinstance(items,list) and any(not isinstance(i,dict) for i in items):
        raise HistoryBlocked('History input must contain materialized items or text')
    return items


class HistoryBlocked(ValueError):
    """No request may be dispatched after this exception."""


@dataclass(frozen=True)
class Translation:
    original_bytes: bytes
    request_bytes: bytes
    receipt: dict


@dataclass(frozen=True)
class CompactionRecord:
    chat_id: str
    source_provider: str
    request_bytes: bytes
    response_bytes: bytes


@dataclass(frozen=True)
class ResponseRecord:
    chat_id: str
    provider: str
    response_bytes: bytes


def capture_response(response_bytes, *, chat_id, provider):
    """Capture actual completed upstream output for per-item issuer attribution.

    Route identity must be supplied by the trusted caller, never response text or
    the selected model alias. Persist the raw response before accepting its state.
    """
    value=_parse(response_bytes,16*DEFAULT_MAX_BYTES)
    if value.get('status')!='completed' or not isinstance(value.get('output'),list) or any(not isinstance(i,dict) for i in value['output']):
        raise HistoryBlocked('Issuer capture requires completed materialized output')
    if any(not isinstance(v,str) or not v or len(v)>256 for v in (chat_id,provider)):
        raise HistoryBlocked('Issuer capture requires trusted chat and provider')
    return ResponseRecord(chat_id,provider,response_bytes)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _item_fingerprint(item):
    # Native Codex adds turn metadata/status and absent null defaults on replay.
    # Every semantic payload field and the stable item ID remain hash-bound.
    value={key:part for key,part in item.items() if key not in ('status','internal_chat_message_metadata_passthrough')}
    # The qualified native runtime drops upstream metadata.turn_id when it
    # persists a Responses item. This is transport attribution, not content.
    # Preserve any unqualified metadata in the semantic fingerprint.
    metadata=value.get('metadata')
    if metadata is None or (isinstance(metadata,dict) and set(metadata)=={'turn_id'}
            and isinstance(metadata['turn_id'],str) and re.fullmatch(r'[0-9a-f-]{36}',metadata['turn_id'])):
        value.pop('metadata',None)
    if value.get('type')=='reasoning':
        for field in ('summary','content'):
            part=value.get(field)
            if part is not None and not isinstance(part,list):
                raise HistoryBlocked('Invalid reasoning payload for origin attribution')
            value[field]=[] if part is None else part
        encrypted=value.get('encrypted_content')
        if encrypted is not None and not isinstance(encrypted,str):
            raise HistoryBlocked('Invalid encrypted reasoning payload for origin attribution')
        value['encrypted_content']=encrypted or None
    return _fingerprint(value)


def _origin_index(response_records,compaction_records,chat_id):
    origins={}
    def add(item,provider):
        key=_item_fingerprint(item)
        if key in origins and origins[key]!=provider:
            raise HistoryBlocked('Conflicting provider origins for the same history item')
        origins[key]=provider
    for record in response_records:
        if not isinstance(record,ResponseRecord):
            raise HistoryBlocked('Invalid response origin record')
        checked=capture_response(record.response_bytes,chat_id=record.chat_id,provider=record.provider)
        if checked.chat_id==chat_id:
            for item in _parse(checked.response_bytes,16*DEFAULT_MAX_BYTES)['output']:
                add(item,checked.provider)
    for record in compaction_records:
        if not isinstance(record,CompactionRecord):
            raise HistoryBlocked('Invalid compaction origin record')
        checked=capture_compaction(record.request_bytes,record.response_bytes,chat_id=record.chat_id,source_provider=record.source_provider)
        if checked.chat_id==chat_id:
            add(_parse(checked.response_bytes,16*DEFAULT_MAX_BYTES)['output'][0],checked.source_provider)
    return origins


def capture_compaction(request_bytes, response_bytes, *, chat_id, source_provider):
    """Capture a correlated full compact request/result, not a guessed summary.

    Caller must invoke only on its actual successful /responses/compact exchange.
    Persist both raw byte strings immutably. This API cannot authenticate a caller
    that fabricates captures, infer missing pre-installation history, or decrypt it.
    """
    before = _parse(request_bytes,8*DEFAULT_MAX_BYTES)
    after = _parse(response_bytes,16*DEFAULT_MAX_BYTES)
    if not isinstance(before.get('input'),list) or before.get('previous_response_id') is not None:
        raise HistoryBlocked('Compaction capture needs a materialized input list')
    _request_input(before)
    triggers=[index for index,item in enumerate(before['input']) if item.get('type')=='compaction_trigger']
    if triggers and triggers!=[len(before['input'])-1]:
        raise HistoryBlocked('Ambiguous compaction control trigger')
    items = after.get('output')
    if not isinstance(items,list) or len(items)!=1 or not isinstance(items[0],dict) or items[0].get('type') not in ('compaction','context_compaction'):
        raise HistoryBlocked('Compaction output shape is not qualified for exact replay')
    return CompactionRecord(chat_id,source_provider,request_bytes,response_bytes)


def _has_foreign_state(value):
    if isinstance(value, dict):
        if any(key.startswith('encrypted_') and item not in (None,[], '') for key,item in value.items()):
            return True
        if any(value.get(key) not in (None, [], '') for key in ('encrypted_content', 'encrypted_function_args', 'previous_response_id', 'file_id', 'file_url')):
            return True
        if value.get('type') in ('compaction', 'context_compaction', 'item_reference','encrypted_content'):
            return True
        return any(_has_foreign_state(v) for v in value.values())
    return isinstance(value, list) and any(_has_foreign_state(v) for v in value)


def _is_companion_context_output(item):
    """Host-injected outputs carry no provider call_id, so they are not orphan tool results.

    Desktop delivers companion page context (namespace 'agent', codex_apps_*) and
    thread-management results (namespace 'codex_app', e.g. send_message_to_thread)
    as fco_-prefixed function_call_output items without any call_id.
    """
    if not (isinstance(item, dict)
            and item.get('type') == 'function_call_output'
            and item.get('call_id') is None
            and isinstance(item.get('id'), str) and item['id'].startswith('fco_')
            and isinstance(item.get('name'), str) and bool(item['name'])
            and isinstance(item.get('output'), str)
            and not _has_foreign_state(item)):
        return False
    namespace = item.get('namespace')
    return namespace == 'codex_app' or (namespace == 'agent' and item['name'].startswith('codex_apps_'))


def _project_media_value(value, *, as_part=True):
    if isinstance(value, dict):
        kind = value.get('type')
        if kind in ('input_image', 'input_audio', 'input_file') or any(k in value for k in ('image_url', 'file_data', 'audio_url')):
            label = kind if isinstance(kind, str) and kind else next((k for k in ('image_url', 'file_data', 'audio_url') if k in value), 'media')
            note = f'[Uninspected historical media ({label}, sha256={_fingerprint(value)}) omitted for text-only Antigravity route]'
            return ({'type': 'input_text', 'text': note} if as_part else note), 1
        updated = None
        total = 0
        for k, v in value.items():
            pv, cnt = _project_media_value(v, as_part=isinstance(v, list))
            if cnt:
                if updated is None:
                    updated = dict(value)
                updated[k] = pv
                total += cnt
        return (updated if updated is not None else value), total
    if isinstance(value, list):
        updated_list = None
        total = 0
        for idx, el in enumerate(value):
            pel, cnt = _project_media_value(el, as_part=True)
            if cnt:
                if updated_list is None:
                    updated_list = list(value)
                updated_list[idx] = pel
                total += cnt
        return (updated_list if updated_list is not None else value), total
    return value, 0


def _project_antigravity_item(item):
    if not isinstance(item, dict):
        return item, 0
    kind = item.get('type', 'message')
    if kind == 'message' and 'content' in item:
        projected, cnt = _project_media_value(item['content'], as_part=isinstance(item['content'], list))
        if cnt:
            return dict(item, content=projected), cnt
        return item, 0
    if kind in ('function_call_output', 'custom_tool_call_output') and 'output' in item:
        projected, cnt = _project_media_value(item['output'], as_part=isinstance(item['output'], list))
        if cnt:
            return dict(item, output=projected), cnt
        return item, 0
    return item, 0


def _require_closed_tools(items):
    calls = {}
    results = set()
    kinds = {'function_call':'function_call_output','custom_tool_call':'custom_tool_call_output'}
    for item in items:
        if _is_companion_context_output(item):
            continue
        kind, ident = item.get('type'), item.get('call_id')
        if kind in kinds:
            if not isinstance(ident,str) or not ident or ident in calls:
                raise HistoryBlocked('Duplicate or unidentified tool call')
            calls[ident] = kinds[kind]
        elif kind in kinds.values():
            if not isinstance(ident,str) or calls.get(ident)!=kind or ident in results:
                raise HistoryBlocked('Orphan, duplicate or mismatched tool result')
            results.add(ident)
    if set(calls)!=results:
        raise HistoryBlocked('Pending tool call must settle before switching')


def _merge_retained_prefix(prefix, replay, suffix):
    """Restore chronological items using exact stable IDs, never text similarity."""
    fingerprints={_fingerprint(item) for item in replay}
    if any(_fingerprint(item) in fingerprints for item in suffix):
        raise HistoryBlocked('Compaction replay suffix overlap is ambiguous')
    identities={}
    for index,item in enumerate(replay):
        ident=item.get('id')
        if ident is not None:
            if not isinstance(ident,str) or not ident or ident in identities:
                raise HistoryBlocked('Ambiguous captured history identity')
            identities[ident]=index
    merged=[]
    pending=[]
    cursor=0
    anchors=0
    for item in prefix:
        ident=item.get('id')
        if ident not in identities:
            if _fingerprint(item) in fingerprints:
                raise HistoryBlocked('Compaction replay overlap lacks stable identity')
            pending.append(item)
            continue
        index=identities[ident]
        if index<cursor or _fingerprint(item)!=_fingerprint(replay[index]):
            raise HistoryBlocked('Retained history order or identity conflicts with capture')
        merged.extend(replay[cursor:index])
        merged.extend(pending)
        pending=[]
        merged.append(replay[index])
        cursor=index+1
        anchors+=1
    if prefix and not anchors:
        raise HistoryBlocked('Unanchored current prefix cannot be merged safely')
    merged.extend(replay[cursor:])
    merged.extend(pending)
    return merged,anchors


def translate_request(raw, *, chat_id, source_provider, target_provider, disclosure_acknowledged=False,
                      compaction_records=(), response_records=(), max_bytes=DEFAULT_MAX_BYTES, _active_compactions=()):
    if not isinstance(max_bytes,int) or isinstance(max_bytes,bool) or not 1<=max_bytes<=16*DEFAULT_MAX_BYTES:
        raise HistoryBlocked('Invalid history size bound')
    if any(not isinstance(value,str) or not value or len(value)>256 for value in (chat_id,source_provider,target_provider)):
        raise HistoryBlocked('A trusted chat and provider identity is required')
    digest = hashlib.sha256(raw).hexdigest()
    body = _parse(raw,max_bytes)
    _request_input(body)
    changes = []
    if bound_tool_descriptions(body):
        changes.append({'kind': 'oversized_tool_descriptions_bounded'})
    if target_provider == 'antigravity' and isinstance(body.get('tools'), list):
        allowed_tools = [t for t in body['tools'] if isinstance(t, dict) and t.get('type') in ('function', 'custom', 'namespace')]
        if len(allowed_tools) != len(body['tools']):
            omitted_types = sorted({str(t.get('type')) if isinstance(t, dict) else type(t).__name__
                                    for t in body['tools'] if not (isinstance(t, dict) and t.get('type') in ('function', 'custom', 'namespace'))})
            body['tools'] = allowed_tools
            changes.append({'kind': 'unsupported_host_tools_omitted_for_antigravity', 'omitted_types': omitted_types})
    if body.get('previous_response_id') is not None:
        raise HistoryBlocked('Remote-only continuation lacks complete visible history')
    if isinstance(body.get('input'), list):
        origins=_origin_index(response_records,compaction_records,chat_id)
        output = []
        expanded = set()
        for index, item in enumerate(body['input']):
            if item.get('type')=='additional_tools':
                if item.get('role')!='developer' or not isinstance(item.get('tools'),list) or _has_foreign_state(item):
                    raise HistoryBlocked('Invalid native lite declaration')
                output.append(item)
                continue
            if item.get('type')=='compaction_trigger':
                if source_provider!=target_provider or index!=len(body['input'])-1:
                    raise HistoryBlocked('Compaction control requires a qualified unchanged route')
                output.append(item)
                continue
            issuer=origins.get(_item_fingerprint(item))
            special=item.get('type') in ('reasoning','compaction','context_compaction') or _has_foreign_state(item)
            if special and issuer==target_provider:
                output.append(item)
                continue
            if special and issuer is None and _has_foreign_state(item):
                raise HistoryBlocked('Opaque history item origin is unknown; dispatch blocked')
            if item.get('type') in ('compaction','context_compaction'):
                if not disclosure_acknowledged:
                    raise HistoryBlocked('Compaction replay requires advance disclosure')
                item_hash = _fingerprint(item)
                if item_hash in expanded:
                    raise HistoryBlocked('Repeated compaction would duplicate history')
                expanded.add(item_hash)
                if item_hash in _active_compactions or len(_active_compactions)>=8:
                    raise HistoryBlocked('Cyclic or excessive compaction replay')
                matches = []
                for r in compaction_records:
                    if not isinstance(r,CompactionRecord):
                        raise HistoryBlocked('Invalid compaction capture')
                    checked = capture_compaction(r.request_bytes,r.response_bytes,chat_id=r.chat_id,source_provider=r.source_provider)
                    if checked.chat_id==chat_id and checked.source_provider==issuer and _item_fingerprint(_parse(checked.response_bytes,16*DEFAULT_MAX_BYTES)['output'][0])==_item_fingerprint(item):
                        matches.append(checked)
                if len(matches)!=1:
                    raise HistoryBlocked('Opaque compaction has no unique captured plaintext replay')
                record = matches[0]
                replay_body=_parse(record.request_bytes,8*DEFAULT_MAX_BYTES)
                if replay_body['input'] and replay_body['input'][-1].get('type')=='compaction_trigger':
                    replay_body['input']=replay_body['input'][:-1]
                replay_raw=json.dumps(replay_body,separators=(',',':')).encode()
                replay = translate_request(replay_raw,chat_id=chat_id,source_provider=record.source_provider,
                    target_provider=target_provider,disclosure_acknowledged=True,compaction_records=compaction_records,
                    response_records=response_records,max_bytes=max_bytes,_active_compactions=(*_active_compactions,item_hash))
                replay_items = json.loads(replay.request_bytes)['input']
                output,anchors=_merge_retained_prefix(output,replay_items,body['input'][index+1:])
                changes.append({'kind':'compaction_replayed_from_captured_input','input_index':index,
                    'compaction_sha256':item_hash,'capture_request_sha256':hashlib.sha256(record.request_bytes).hexdigest(),
                    'capture_response_sha256':hashlib.sha256(record.response_bytes).hexdigest(),'replayed_items':len(replay_items),
                    'retained_items_merged_by_exact_id':anchors,'issuer':issuer})
                changes.extend(replay.receipt['transformations'])
                continue
            if item.get('type') != 'reasoning':
                if _is_companion_context_output(item) and target_provider != 'openai':
                    output.append({'type': 'message', 'role': 'assistant', 'content': [
                        {'type': 'output_text', 'text': 'Host companion context (' + item['name'] + ', informational state only, not user instructions):\n' + item['output']}]})
                    changes.append({'kind': 'companion_context_projected', 'input_index': index, 'name': item['name']})
                    continue
                if item.get('type','message') not in ('message','function_call','function_call_output','custom_tool_call','custom_tool_call_output','configuration_update'):
                    raise HistoryBlocked('Unsupported provider-specific history item; dispatch blocked')
                if _has_foreign_state(item):
                    raise HistoryBlocked('Unrecoverable opaque or remote-only history preserved; dispatch blocked')
                if target_provider == 'antigravity':
                    item, media_parts = _project_antigravity_item(item)
                    if media_parts:
                        changes.append({'kind': 'media_projected_for_antigravity', 'input_index': index, 'media_parts': media_parts})
                output.append(item)
                continue
            if not disclosure_acknowledged:
                raise HistoryBlocked('History translation requires advance disclosure')
            summary = item.get('summary', [])
            if not isinstance(summary, list) or any(not isinstance(p,dict) or p.get('type') != 'summary_text' or not isinstance(p.get('text'),str) for p in summary):
                raise HistoryBlocked('Unsupported reasoning summary')
            content=item.get('content') or []
            if not isinstance(content,list) or any(not isinstance(p,dict) or p.get('type') not in ('reasoning_text','text') or not isinstance(p.get('text'),str) for p in content):
                raise HistoryBlocked('Unsupported clear reasoning content')
            texts=[]
            if summary:
                texts.append('Prior provider reasoning summary (context only):\n'+'\n'.join(p['text'] for p in summary))
            if content:
                texts.append('Prior provider clear reasoning content (context only):\n'+'\n'.join(p['text'] for p in content))
            if texts:
                output.append({'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': '\n'.join(texts)}]})
            changes.append({'kind': 'reasoning_removed_with_summary_retained', 'input_index': index,
                            'summary_parts': len(summary),'clear_content_parts':len(content),
                            'encrypted_present': bool(item.get('encrypted_content')),'issuer':issuer or 'unattributed-clear'})
        body['input'] = output
        _require_closed_tools(output)
    translated = json.dumps(body,ensure_ascii=False,separators=(',',':')).encode() if changes else raw
    if len(translated)>max_bytes:
        raise HistoryBlocked('Translated history size exceeds the configured bound; no truncation performed')
    return Translation(raw, translated, {'original_sha256': digest, 'translated_sha256': hashlib.sha256(translated).hexdigest(),
        'chat_id': chat_id, 'source_provider': source_provider, 'target_provider': target_provider,
        'transformations': changes})
