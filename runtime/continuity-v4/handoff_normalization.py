"""Losslessly demote malformed model claims; never manufacture completion proof."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from handoff import validate_handoff,HandoffRejected,_parse,_canonical,MAX_HANDOFF

CLAIMS=('changed_files','completed_actions','active_jobs','uncertain_outcomes')


def normalize_response(response,snapshot,*,evidence_dir):
    if (not isinstance(response,dict) or response.get('status')!='completed'
            or response.get('model')!=snapshot['source_model'] or not isinstance(response.get('id'),str) or not response['id']):
        raise HandoffRejected('Completed actual outgoing-model response required')
    output=response.get('output');texts=[]
    if not isinstance(output,list) or not output:raise HandoffRejected('No outgoing handoff output')
    for item in output:
        if not isinstance(item,dict):raise HandoffRejected('Invalid output item')
        if item.get('type')=='reasoning':continue
        if item.get('type')!='message' or item.get('role')!='assistant':raise HandoffRejected('Handoff attempted tool output')
        if not isinstance(item.get('content'),list):raise HandoffRejected('Invalid handoff message')
        for part in item['content']:
            if not isinstance(part,dict) or part.get('type')!='output_text' or not isinstance(part.get('text'),str):
                raise HandoffRejected('Unsupported handoff content')
            texts.append(part['text'])
    raw=''.join(texts).encode()
    if len(raw)>MAX_HANDOFF:raise HandoffRejected('Original handoff exceeds bound')
    # Accept only one exact JSON code fence, without prose or nested fences.
    # Original bytes remain evidence; inner JSON still uses every strict gate.
    fenced=re.fullmatch(rb'```json\r?\n([\s\S]*)\r?\n```',raw)
    unwrapped=fenced is not None and b'```' not in fenced.group(1)
    inner=fenced.group(1) if unwrapped else raw
    value=_parse(inner)
    try:
        validate_handoff(inner,snapshot)
        if not unwrapped:return response
        valid=True
    except HandoffRejected:valid=False
    if not isinstance(value,dict):raise HandoffRejected('Handoff object required')
    probe=copy.deepcopy(value)
    for field in (() if valid else CLAIMS):
        if not isinstance(value.get(field),list) or len(value[field])>100:
            raise HandoffRejected('Malformed claim container cannot be normalized')
        probe[field]=[]
    # Binding, objective, intent, known gaps, text arrays and exact top-level
    # keys must already pass. Only individual claim records are recoverable.
    validate_handoff(_canonical(probe),snapshot)
    changes=[]
    for field in (() if valid else CLAIMS):
        kept=[]
        for index,item in enumerate(value[field]):
            candidate=copy.deepcopy(probe);candidate[field]=[item]
            try:validate_handoff(_canonical(candidate),snapshot);kept.append(item)
            except HandoffRejected:
                content=_canonical(item)
                note=('UNVERIFIED outgoing-model claim from '+field+'['+str(index)+']; '
                    'not completion evidence or replay permission; reconcile against current history: '+content.decode())
                if len(note)>4000:raise HandoffRejected('Lossless claim note exceeds bound; refusing truncation')
                probe['known_gaps'].append(note)
                changes.append({'field':field,'index':index,'canonical_value_sha256':hashlib.sha256(content).hexdigest()})
        probe[field]=kept
    normalized=_canonical(probe);validate_handoff(normalized,snapshot)
    if not changes and not unwrapped:raise HandoffRejected('Unqualified handoff failure cannot be normalized')
    result=copy.deepcopy(response)
    result['output']=[{'type':'message','role':'assistant','status':'completed',
        'content':[{'type':'output_text','text':normalized.decode()}]}]
    receipt={'normalization':('exact_json_fence_removed' if not changes else 'malformed_claim_records_demoted_to_unverified_context'),
        'exact_json_fence_removed':unwrapped,
        'original_response_sha256':hashlib.sha256(_canonical(response)).hexdigest(),
        'original_handoff_sha256':hashlib.sha256(raw).hexdigest(),'normalized_handoff_sha256':hashlib.sha256(normalized).hexdigest(),
        'binding':value['binding'],'changes':changes,'facts_verified':False,'model_requeried':False,
        'bindings_modified':False,'content_truncated':False}
    directory=Path(evidence_dir)
    for name,payload in [('original-handoff-response.json',_canonical(response)),('handoff-normalization.json',_canonical(receipt))]:
        with (directory/name).open('xb') as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
    result['handoff_normalization']=receipt
    return result
