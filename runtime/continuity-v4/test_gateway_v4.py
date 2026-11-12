import http.client
from dataclasses import replace
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT.parent/'continuity-v2'),str(ROOT.parent/'desktop-route-v2/deps')]
from gateway_v4 import create_server
from route_registry import RouteSpec,RouteRegistry
from quota_policy import Snapshot

CHAT='11111111-1111-4111-8111-111111111111'
CAP='synthetic-v4-capability-'+'x'*32


class Upstream(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((body,dict(self.headers)))
        raw=json.dumps({'id':self.server.route+'-response','model':body['model'],'status':'completed',
            'output':[{'type':'message','id':self.server.route+'-message','role':'assistant','content':[{'type':'output_text','text':'Provider result'}]}]}).encode()
        if getattr(self.server,'sse',False):
            result=json.loads(raw)
            events=[{'type':'response.created','response':{'id':result['id'],'model':result['model']}},
                {'type':'response.completed','response':result}]
            raw=b''.join(b'id: 1\nretry: 1000\n: provider metadata\ndata: '+json.dumps(e).encode()+b'\n\n' for e in events)
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw);return
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)


class GatewayV4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.remaining=80;self.now=100.;self.events=[];self.servers={}
        for route in ('openai','minimax-plan','mimo-plan'):
            server=ThreadingHTTPServer(('127.0.0.1',0),Upstream);server.calls=[];server.route=route
            threading.Thread(target=server.serve_forever,daemon=True).start();self.servers[route]=server
        self.routes=RouteRegistry([
            RouteSpec(route_id='minimax-plan',provider_id='minimax',actual_model='MiniMax-M3.1-Flash',native_alias='tp-minimax-m31',
                endpoint='https://api.minimax.io/v1/responses',protocol_profile='responses-full',supports_native_tools=True,
                auto_fallback_qualified=True,qualification_ref='synthetic full fixture',display_name='MiniMax Plan'),
            RouteSpec(route_id='mimo-plan',provider_id='mimo',actual_model='mimo-v2-flash',native_alias='tp-mimo-flash',
                endpoint='https://token-plan-sgp.xiaomimimo.com/v1/responses',protocol_profile='responses-lite',supports_native_tools=True,
                auto_fallback_qualified=False,qualification_ref='synthetic lite fixture',display_name='MiMo Plan')],auto_fallback_id='minimax-plan')
        def factory(account,chat,headers,context):
            self.context=context
            def notice(event):self.events.append(('notice',event['kind']));return 'synthetic-shown'
            def checkpoint(ident,raw):
                previous=context['last_completed_route']() or {'provider':'openai','model':'gpt-test'}
                self.events.append(('handoff',previous['provider'],previous['model']))
                return {'request_sha256':ident,'checkpoint_id':'synthetic-checkpoint','checkpoint_source_sha256':'0'*64,
                    'checkpoint_turn_id':'synthetic-turn','source_provider':previous['provider'],'source_model':previous['model'],
                    'stale':False,'requires_reconciliation':True,'text':'Preserve original task facts'}
            return {'snapshot_reader':lambda:Snapshot(account,self.now,'v4-test',True,True,self.remaining,'synthetic'),
                'notifier':notice,'checkpoint_callback':checkpoint,'recovery_reader':lambda:False}
        self.kwargs=dict(state_dir=self.tmp.name,capability=CAP,allowed_accounts={'account'},registered_chats={('account',CHAT)},
            clock_epoch='v4-test',clock=lambda:self.now,callback_factory=factory,openai_models={'gpt-test'},route_registry=self.routes,
            credential_reader=lambda spec:'SECRET_'+spec.provider_id,allow_test_upstreams=True,
            test_upstreams={k:f'http://127.0.0.1:{v.server_port}/responses' for k,v in self.servers.items()},timeout=3)
        self.start()
    def start(self):
        self.gateway=create_server(**self.kwargs);self.worker=threading.Thread(target=self.gateway.serve_forever,daemon=True);self.worker.start()
    def stop(self):self.gateway.shutdown();self.gateway.server_close();self.worker.join()
    def tearDown(self):
        self.stop()
        for server in self.servers.values():server.shutdown();server.server_close()
        self.tmp.cleanup()
    def send(self,model,text,lite=False,chat=CHAT,items=None):
        items=items if items is not None else [{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}]
        if lite:items.insert(0,{'type':'additional_tools','role':'developer','tools':[]})
        body={'model':model,'input':items}
        conn=http.client.HTTPConnection(*self.gateway.server_address,timeout=5)
        conn.request('POST','/'+CAP+'/responses',json.dumps(body),{'thread-id':chat,'session-id':chat,
            'ChatGPT-Account-ID':'account','Authorization':'Bearer OPENAI_ONLY'})
        response=conn.getresponse();result=(response.status,response.read());conn.close();return result
    def test_manual_plan_alias_dispatches_exact_route_and_stays_selected_after_restart(self):
        self.assertEqual(self.send('gpt-test','initial')[0],200)
        self.assertEqual(self.send('tp-minimax-m31','manual plan')[0],200)
        self.stop();self.start()
        self.assertEqual(self.send('gpt-test','continue selected plan')[0],200)
        self.assertEqual(len(self.servers['openai'].calls),1)
        self.assertEqual(len(self.servers['minimax-plan'].calls),2)
        self.assertEqual(self.servers['minimax-plan'].calls[-1][1]['Authorization'],'Bearer SECRET_minimax')
        self.assertEqual(self.context['last_completed_route']()['provider'],'minimax')
        self.assertEqual(self.context['last_completed_route']()['route_id'],'minimax-plan')

    def test_lite_requires_native_declaration_before_warning_or_reservation(self):
        status,_=self.send('tp-mimo-flash','wrong full serialization')
        self.assertEqual(status,409);self.assertEqual(self.events,[])
        self.assertEqual(self.servers['mimo-plan'].calls,[])
        scope=self.gateway.registry.get('account',CHAT)
        with scope.policy._transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)

    def test_manual_lite_plan_exact_credentials_and_provider_history(self):
        self.assertEqual(self.send('gpt-test','initial')[0],200)
        self.assertEqual(self.send('tp-mimo-flash','use native lite',lite=True)[0],200)
        body,headers=self.servers['mimo-plan'].calls[0]
        self.assertEqual(body['model'],'mimo-v2-flash')
        self.assertEqual(body['input'][0]['type'],'additional_tools')
        self.assertEqual(headers['Authorization'],'Bearer SECRET_mimo')
        self.assertNotIn('ChatGPT-Account-ID',headers)
        self.assertEqual(headers['X-OpenAI-Internal-Codex-Responses-Lite'],'true')
        scope=self.gateway.registry.get('account',CHAT)
        # Wait for the durable terminal state after consuming the network bytes.
        for _ in range(100):
            latest=scope.store.latest_completed_route()
            if latest and latest['provider']=='mimo':break
            threading.Event().wait(.01)
        self.assertEqual(latest['provider'],'mimo')
        responses,_=scope.store.history()
        self.assertEqual(responses[-1].provider,'mimo')
        self.assertEqual(self.send('tp-mimo-flash','next lite',lite=True)[0],200)
        self.assertEqual([e for e in self.events if e[0]=='handoff'],[('handoff','openai','gpt-test')])
        self.assertIn('Earlier checkpoint',json.dumps(self.servers['mimo-plan'].calls[-1][0]))

    def test_automatic_low_allowance_uses_only_qualified_default(self):
        self.remaining=5
        self.assertEqual(self.send('gpt-test','low quota')[0],200)
        self.assertEqual(len(self.servers['minimax-plan'].calls),1)
        self.assertEqual(self.servers['mimo-plan'].calls,[])
        self.assertEqual(self.events[0],('notice','switch'))

    def test_unknown_alias_never_dispatches(self):
        self.assertEqual(self.send('tp-unqualified','unknown')[0],400)
        self.assertEqual(self.events,[])
        self.assertTrue(all(not s.calls for s in self.servers.values()))

    def test_actual_route_switches_only_generate_handoff_and_recovery_includes_old_plan(self):
        self.stop()
        route=RouteSpec('alibaba-plan','alibaba','qwen3.8-max','tp-alibaba-qwen',
            'https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/responses','responses-full',True,False,'synthetic full','Alibaba')
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream);upstream.calls=[];upstream.route=route.route_id
        threading.Thread(target=upstream.serve_forever,daemon=True).start();self.servers[route.route_id]=upstream
        self.kwargs.update(state_dir=str(Path(self.tmp.name)/'switch-lifecycle'),
            route_registry=RouteRegistry([*self.routes.routes,route],auto_fallback_id='minimax-plan'),
            recovery_aliases={'return-gpt':'gpt-test'})
        self.kwargs['test_upstreams'][route.route_id]=f'http://127.0.0.1:{upstream.server_port}/responses';self.start()
        handoffs=lambda:[e for e in self.events if e[0]=='handoff']
        self.assertEqual(self.send('gpt-test','initial')[0],200);self.assertEqual(handoffs(),[])
        self.assertEqual(self.send('tp-mimo-flash','switch',lite=True)[0],200)
        self.assertEqual(len(handoffs()),1)
        pair=[{'type':'function_call','call_id':'synthetic-closed','name':'inspect','namespace':'functions','arguments':'{}'},
            {'type':'function_call_output','call_id':'synthetic-closed','output':'Completed inspection; never repeat.'}]
        self.assertEqual(self.send('tp-mimo-flash','tool continuation',lite=True,items=pair.copy())[0],200)
        self.assertEqual(len(handoffs()),1)
        sent=self.servers['mimo-plan'].calls[-1][0]['input']
        self.assertEqual(sent[0]['type'],'additional_tools')
        self.assertIn('earlier checkpoint',json.dumps(sent[1]).lower())
        self.assertEqual(sent[-2:],pair)
        self.assertEqual(self.send('tp-alibaba-qwen','switch again')[0],200);self.assertEqual(len(handoffs()),2)
        self.now+=1
        self.assertEqual(self.send('return-gpt','recover')[0],200)
        self.assertEqual([e[1] for e in handoffs()],['openai','mimo','alibaba'])
        self.assertIn('Preserve original task facts',json.dumps(self.servers['openai'].calls[-1][0]))

    def test_sse_metadata_preserved_without_reconnection(self):
        self.servers['minimax-plan'].sse=True
        status,raw=self.send('tp-minimax-m31','stream with metadata')
        self.assertEqual(status,200)
        self.assertIn(b'id: 1\nretry: 1000',raw)
        self.assertIn(b'response.completed',raw)
        self.assertEqual(len(self.servers['minimax-plan'].calls),1)

    def test_changed_route_model_cannot_reinterpret_existing_history(self):
        self.assertEqual(self.send('gpt-test','initial')[0],200)
        changed=RouteRegistry([replace(self.routes.routes[0],actual_model='Different-Model'),self.routes.routes[1]],auto_fallback_id='minimax-plan')
        with self.assertRaisesRegex(ValueError,'Route identities changed'):
            create_server(**dict(self.kwargs,route_registry=changed))

    def test_structured_executor_gets_handoff_and_immutable_original_chat_scope(self):
        self.stop()
        other='22222222-2222-4222-8222-222222222222'
        spec=RouteSpec('agy','antigravity','gemini-3.8-flash-low','tp-agy-flash','agy:session-only-structured',
            'agy-structured',True,False,'synthetic structured fixture','Signed-in Gemini')
        routes=RouteRegistry([*self.routes.routes,spec],auto_fallback_id='minimax-plan')
        from protocol_profiles_v4 import prepare_native
        calls=[]
        def prepare(raw,route):
            source=json.loads(raw)
            self.assertEqual(source['model'],route.actual_model)
            return json.dumps({'model':route.actual_model,'source':source,'schema':{'type':'object'}}).encode()
        def execute(route,raw,context):
            with self.assertRaises(TypeError):context['thread_id']='changed'
            self.assertEqual(context['account_id'],'account');self.assertIn(context['thread_id'],(CHAT,other))
            self.assertFalse(Path(context['evidence_dir']).exists())
            self.assertEqual(Path(context['evidence_dir']).name,context['request_id'])
            packet=json.loads(raw);self.assertIn('Saved task handoff',json.dumps(packet['source']['input']))
            calls.append(dict(context))
            return json.dumps({'id':'resp-agy','model':route.actual_model,'status':'completed','output':[]}).encode()
        self.kwargs.update(state_dir=str(Path(self.tmp.name)/'structured-successor'),route_registry=routes,
            registered_chats={('account',CHAT),('account',other)},
            protocol_preparers={'responses-full':prepare_native,'responses-lite':prepare_native,'agy-structured':prepare},
            route_executors={'agy-structured':execute})
        self.start()
        self.assertEqual(self.send('tp-agy-flash','structured request')[0],200)
        self.assertEqual(len(calls),1)
        self.assertTrue(all(not s.calls for s in self.servers.values()))
        self.assertNotEqual(self.send('tp-agy-flash','structured request')[0],200)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.send('tp-agy-flash','structured request',chat=other)[0],200)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0]['request_id'],calls[1]['request_id'])
        self.assertNotEqual(calls[0]['evidence_dir'],calls[1]['evidence_dir'])


if __name__=='__main__':unittest.main()
