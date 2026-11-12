import json,unittest,tempfile
from pathlib import Path
from handoff_executor_v4 import prepare_body,ExecutorFailed
from production_adapters_v4 import ProductionAdapters,AdapterBlocked
from route_registry import RouteSpec,RouteRegistry

def registry():
    return RouteRegistry([
        RouteSpec('minimax','minimax','MiniMax-M3.1-Flash','tp-minimax-m31','https://api.minimax.io/v1/responses','responses-full',True,True,'test-receipt','MiniMax'),
        RouteSpec('mimo','mimo','mimo-v2.6-flash','tp-mimo-flash','https://token-plan-sgp.xiaomimimo.com/v1/responses','responses-lite',True,False,'test-receipt','MiMo'),
        RouteSpec('alibaba','alibaba','qwen3.8-max','tp-alibaba-qwen','https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/responses','responses-full',True,False,'test-receipt','Alibaba'),
    ],auto_fallback_id='minimax')

class AdditionalHandoffs(unittest.TestCase):
    def test_new_plans_use_complete_visible_packet_and_no_native_tool_execution(self):
        for provider in ('mimo','alibaba'):
            packet='Exact task packet: keep existing file x, never retry UNKNOWN request42, next action inspect receipt.'
            body=json.loads(prepare_body({'provider':provider,'model':'qualified-model','prompt':packet,'tools':[],
                'tool_choice':'none','max_output_tokens':6000,'native_history':[{'type':'additional_tools'},
                    {'type':'reasoning','encrypted_content':'opaque'},{'type':'custom_tool_call','input':'unsafe'}]}))
            self.assertEqual(body['instructions'],packet);self.assertEqual(body['tools'],[])
            self.assertEqual(body['tool_choice'],'none');self.assertEqual([x['type'] for x in body['input']],['message'])
            self.assertNotIn('opaque',json.dumps(body));self.assertNotIn('unsafe',json.dumps(body))
    def test_old_model_receipt_uses_exact_provider_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory=ProductionAdapters(state_root=tmp,openai_models={'gpt-test'},clock_epoch='boot',route_registry=registry())
            for provider,model in [('mimo','mimo-v2.6-flash'),('alibaba','qwen3.8-max')]:
                actual=factory.validate_initial_route('a','chat',{'model':'tp-minimax-m31'},lambda:{
                    'route_id':provider,'provider':provider,'model':model,'request_sha256':'a'*64})
                self.assertEqual(actual['provider'],provider);self.assertEqual(actual['model'],model)
            with self.assertRaises(AdapterBlocked):
                factory.validate_initial_route('a','chat',{'model':'gpt-test'},lambda:{'provider':'mimo','model':'unqualified','request_sha256':'a'*64})
    def test_outgoing_encoder_is_bounded_before_dispatch(self):
        with self.assertRaises(ExecutorFailed):prepare_body({'provider':'alibaba','model':'qwen3.8-max','prompt':'x'*1100001,'tools':[],'tool_choice':'none'})

if __name__=='__main__':unittest.main()
