"""Candidate agent-backed signed-in Gemini executor; never executes host tools."""
import hashlib
import json
from pathlib import Path

from agy_bridge import AgyError,canonical,prepare_body,strict_json
from agy_runtime import run

class AgyExecutor:
    def __init__(self,*,model,evidence_dir,timeout=90,runner=run,scope=None):
        self.model=model;self.evidence_dir=Path(evidence_dir);self.timeout=timeout;self.runner=runner;self.scope=scope
    def __call__(self,request):
        prepared=prepare_body(request)
        return self.execute_prepared(prepared)
    def execute_prepared(self,prepared):
        packet=strict_json(prepared)
        if packet.get('model')!=self.model or prepare_body(packet['source'])!=prepared: raise AgyError('prepared_model_or_source_mismatch')
        # The runner owns atomic exclusive evidence-directory creation. An
        # existing reserved/completed/uncertain logical request never replays.
        receipt=self.runner(self.evidence_dir,live=True,timeout=self.timeout,prepared=prepared,scope=self.scope)
        if receipt.get('status')!='completed': raise AgyError('native_run_'+str(receipt.get('status')))
        response=strict_json((self.evidence_dir/'translated-response.json').read_bytes())
        if response.get('model')!=self.model or response.get('status')!='completed': raise AgyError('native_result_mismatch')
        response['adapter_evidence'].update(source_sha256=packet['source_sha256'],prepared_sha256=hashlib.sha256(prepared).hexdigest(),
            native_model_request_log_count=receipt.get('native_model_request_log_count'))
        return response

def factory(*,provider,model=None,token=None,account_id=None,thread_id=None,timeout=90,evidence_dir=None,**kwargs):
    if provider!='antigravity' or evidence_dir is None: raise AgyError('explicit_signed_in_route_and_evidence_required')
    # Old-model factory learns the exact selected model at its request seam.
    def execute(request):
        selected=request.get('model')
        if model is not None and selected!=model: raise AgyError('selected_model_mismatch')
        scope={'account_id':account_id,'thread_id':thread_id,'request_id':Path(evidence_dir).name}
        return AgyExecutor(model=selected,evidence_dir=evidence_dir,timeout=timeout,scope=scope)(request)
    return execute

def protocol_preparer(raw,route):
    body=strict_json(raw)
    if route.provider_id!='antigravity' or body.get('model')!=route.actual_model: raise AgyError('route_model_mismatch')
    return prepare_body(body)

def route_executor(*,timeout=90):
    def execute(route,prepared,scope_context):
        if route.provider_id!='antigravity': raise AgyError('route_provider_mismatch')
        if set(scope_context)!={'account_id','thread_id','request_id','evidence_dir'} or any(not isinstance(scope_context[k],str) or not scope_context[k] for k in ('account_id','thread_id','request_id')):
            raise AgyError('explicit_host_scope_required')
        scope={k:scope_context[k] for k in ('account_id','thread_id','request_id')}
        response=AgyExecutor(model=route.actual_model,evidence_dir=scope_context['evidence_dir'],timeout=timeout,scope=scope).execute_prepared(prepared)
        response['adapter_evidence']['scope']={k:scope_context[k] for k in ('account_id','thread_id','request_id')}
        return canonical(response)
    return execute
