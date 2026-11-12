"""Bounded native media handoff admission and echoed-input transport."""
import json,threading,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import handoff_executor_v4 as executor
from handoff import MAX_SOURCE,MAX_HANDOFF,validate_handoff,HandoffRejected,make_snapshot
from production_adapters_v4 import MAX_NATIVE,MINIMAX_MODEL,AdapterBlocked
from test_production_adapters_v4 import AdapterTests
from route_registry import RouteRegistry,RouteSpec

class NativeHandoffBounds(unittest.TestCase):
    def request(self,provider='openai',size=3_100_000):
        return {'provider':provider,'model':'synthetic-model','tools':[],'tool_choice':'none','max_output_tokens':6000,
            'prompt':'Source-bound handoff only. Preserve the two original images.',
            'native_history':[{'type':'message','role':'user','content':[
                {'type':'input_image','image_url':'data:image/png;base64,'+'A'*(size//2)},
                {'type':'input_image','image_url':'data:image/png;base64,'+'B'*(size//2)}]}]}
    def test_native_providers_keep_two_images_at_3_1mb_but_reject_over_4mib(self):
        for provider in ('openai','minimax'):
            with self.subTest(provider=provider):
                request=self.request(provider);raw=executor.prepare_body(request);body=json.loads(raw)
                self.assertGreater(len(raw),3_100_000);self.assertLessEqual(len(raw),4*1024*1024)
                self.assertEqual(body['input'][0],request['native_history'][0])
                with self.assertRaisesRegex(executor.ExecutorFailed,'input exceeds'):
                    executor.prepare_body(self.request(provider,4*1024*1024))
    def test_shared_limits_keep_other_plans_and_content_bounds_unchanged(self):
        self.assertEqual(executor.request_byte_limit('openai'),4*1024*1024)
        self.assertEqual(executor.request_byte_limit('minimax'),4*1024*1024)
        for provider in ('mimo','alibaba','antigravity'):
            self.assertEqual(executor.request_byte_limit(provider),2*1024*1024)
        with self.assertRaises(executor.ExecutorFailed):executor.request_byte_limit('unqualified')
        for provider in ('mimo','alibaba'):
            request=self.request(provider);request['prompt']='\u2603'*350000
            with self.assertRaisesRegex(executor.ExecutorFailed,'input exceeds'):executor.prepare_body(request)
        self.assertEqual((MAX_SOURCE,MAX_HANDOFF,MAX_NATIVE),(1024*1024,128*1024,8*1024*1024))
        snapshot=make_snapshot(chat_id='c',source_provider='openai',source_model='gpt-test',last_turn_id='t',captured_at=1,visible_source=b'x')
        with self.assertRaisesRegex(HandoffRejected,'size bound'):
            validate_handoff(json.dumps({'known_gaps':['x'*(128*1024)]}).encode(),snapshot)
    def test_actual_adapter_preflight_uses_same_native_request_limit_without_inference(self):
        fixture=AdapterTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        callback=fixture.callbacks()['checkpoint_callback']
        request=self.request();ident,raw=fixture.request(extra=request['native_history'])
        proof=callback.preflight(ident,raw)
        self.assertGreater(proof['outgoing_request_bytes'],3_100_000)
        self.assertLessEqual(proof['outgoing_request_bytes'],executor.request_byte_limit('openai'))
        self.assertEqual((fixture.calls,fixture.credentials,fixture.reader.calls),([],[],[]))
        fixture.factory.provider_preflight_encoders['openai']=lambda request:b'x'*(4*1024*1024+1)
        with self.assertRaisesRegex(AdapterBlocked,'Bounded exact checkpoint encoding'):
            callback.preflight(ident,raw)
        fixture.route={'provider':'minimax','model':MINIMAX_MODEL}
        callback=fixture.callbacks(thread='minimax-chat')['checkpoint_callback']
        proof=callback.preflight(*fixture.request(text='MiniMax images',extra=request['native_history']))
        self.assertGreater(proof['outgoing_request_bytes'],3_100_000)
        self.assertLessEqual(proof['outgoing_request_bytes'],executor.request_byte_limit('minimax'))
    def test_other_provider_encoder_cannot_borrow_native_4mib_allowance(self):
        fixture=AdapterTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.factory.route_registry=RouteRegistry([
            RouteSpec('minimax','minimax',MINIMAX_MODEL,'tp-minimax','https://api.minimax.io/v1/responses','responses-full',True,True,'synthetic'),
            RouteSpec('mimo','mimo','mimo-test','tp-mimo','https://token-plan-sgp.xiaomimimo.com/v1/responses','responses-lite',True,False,'synthetic')],auto_fallback_id='minimax')
        fixture.route={'provider':'mimo','model':'mimo-test'}
        fixture.factory.provider_preflight_encoders['mimo']=lambda request:b'x'*(2*1024*1024+1)
        callback=fixture.callbacks()['checkpoint_callback']
        with self.assertRaisesRegex(AdapterBlocked,'Bounded exact checkpoint encoding'):callback.preflight(*fixture.request())
        self.assertEqual((fixture.calls,fixture.credentials,fixture.reader.calls),([],[],[]))
    def test_two_large_input_echoes_fit_but_generated_handoff_bound_is_unchanged(self):
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));calls.append(body)
                response={'id':'r','model':body['model'],'status':'completed','instructions':'E'*(4*1024*1024+10000),
                    'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'{}'}]}]}
                events=[{'type':'response.created','response':dict(response,status='in_progress',output=[])},
                    {'type':'response.completed','response':response}]
                wire=b''.join(b'data: '+json.dumps(e).encode()+b'\n\n' for e in events)
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)))
                self.end_headers();self.wfile.write(wire)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            call=executor.ResponsesHandoffExecutor(provider='openai',token='synthetic-token',account_id='synthetic-account',
                thread_id='synthetic-chat',test_endpoint=f'http://127.0.0.1:{server.server_port}/responses',allow_test_endpoint=True)
            result=call(self.request(size=20));self.assertEqual(result['status'],'completed');self.assertEqual(len(calls),1)
            self.assertEqual(MAX_HANDOFF,128*1024)
            self.assertEqual((executor.MAX_LINE,executor.MAX_TRANSPORT),(6*1024*1024,12*1024*1024))
        finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
