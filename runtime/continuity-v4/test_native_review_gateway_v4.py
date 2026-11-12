from contextlib import closing
import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT.parent/'continuity-v2'),str(ROOT.parent/'desktop-route-v2/deps')]
from native_review_gateway_v4 import qualify_native_review,ReviewBlocked,NativeReviewPolicy,install_native_review,MODEL
from paid_compaction_policy_v4 import parse_compaction_snapshot
from quota_policy import QuotaPolicy

PARENT='11111111-1111-4111-8111-111111111111'
CHILD='22222222-2222-4222-8222-222222222222'
ACCOUNT='synthetic-account'
CAP='synthetic-review-capability-'+'c'*32


def fixture():
    metadata={'thread_id':CHILD,'session_id':PARENT,'parent_thread_id':PARENT,'turn_id':'44444444-4444-4444-8444-444444444444',
        'parent_turn_id':'33333333-3333-4333-8333-333333333333','root_turn_id':'33333333-3333-4333-8333-333333333333','subagent_kind':'guardian',
        'thread_source':'guardian_review','turn_trigger':'guardian_review','request_kind':'turn','model':MODEL}
    encoded=json.dumps(metadata)
    client={'thread_id':CHILD,'session_id':PARENT,'x-codex-parent-thread-id':PARENT,
        'turn_id':'44444444-4444-4444-8444-444444444444','parent_turn_id':'33333333-3333-4333-8333-333333333333','root_turn_id':'33333333-3333-4333-8333-333333333333',
        'x-openai-subagent':'guardian','x-codex-turn-metadata':encoded,'parent_response_id':'parent-response'}
    headers={'thread-id':CHILD,'session-id':PARENT,'x-codex-parent-thread-id':PARENT,
        'x-client-request-id':CHILD,'x-openai-subagent':'guardian','x-codex-guardian':'reviewer',
        'x-openai-internal-codex-responses-lite':'true','x-codex-turn-metadata':encoded,
        'chatgpt-account-id':ACCOUNT,'authorization':'Bearer synthetic-native','content-type':'application/json'}
    body={'model':MODEL,'stream':True,'store':False,'tool_choice':'auto','parallel_tool_calls':True,
        'client_metadata':client,'input':[{'type':'additional_tools','role':'developer','tools':[]},
            {'type':'compaction','encrypted_content':'synthetic-owner-opaque'},
            {'type':'message','role':'user','content':[{'type':'input_text','text':'Review this synthetic action without changing approval policy'}]}]}
    return json.dumps(body,separators=(',',':')).encode(),headers


def quota():
    return {'accountId':ACCOUNT,'ordinaryUsageAllowed':False,'rateLimitsByLimitId':{'codex':{
        'limitId':'codex','spendControlReached':False,'rateLimitReachedType':'rate_limit_reached',
        'primary':{'usedPercent':100,'windowDurationMins':10080},
        'credits':{'hasCredits':True,'unlimited':False}}}}


class ReviewQualificationTests(unittest.TestCase):
    def test_actual_native_shape_preserves_opaque_lite_body(self):
        raw,headers=fixture();before=bytes(raw)
        result=qualify_native_review(raw,headers)
        self.assertEqual(result['child'],CHILD);self.assertEqual(result['parent'],PARENT)
        self.assertEqual(result['body']['input'][1]['encrypted_content'],'synthetic-owner-opaque')
        self.assertEqual(raw,before)
    def test_hidden_model_alone_and_false_lineage_are_rejected(self):
        raw,headers=fixture()
        for key,value in [('x-codex-guardian','classifier'),('x-openai-subagent',''),
            ('x-openai-internal-codex-responses-lite','false'),('session-id',CHILD),('x-client-request-id',PARENT)]:
            altered=dict(headers);altered[key]=value
            with self.assertRaises(ReviewBlocked):qualify_native_review(raw,altered)
        body=json.loads(raw);body['client_metadata']['parent_turn_id']='forged'
        with self.assertRaises(ReviewBlocked):qualify_native_review(json.dumps(body).encode(),headers)
    def test_guardian_warning_has_distinct_purpose_in_shared_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ordinary=QuotaPolicy(Path(tmp)/'quota.sqlite3',account_id=ACCOUNT,thread_id=CHILD,clock_epoch='boot')
            policy=NativeReviewPolicy(ordinary,clock=lambda:100.)
            evidence=parse_compaction_snapshot(quota(),account_id=ACCOUNT,observed_monotonic=100.,clock_epoch='boot')
            events=[];ident='a'*64
            result=policy.admit_review(ident,evidence,now=100.,notifier=lambda event:events.append(event) or 'rendered')
            self.assertEqual(result.route,'openai');self.assertEqual(events[0]['kind'],'native_safety_review')
            self.assertIn('safety reviewer',events[0]['text']);self.assertIn('credits',events[0]['text'])
            self.assertEqual(policy.check_dispatch(ident,evidence,now=100.).route,'openai')
            with ordinary._transaction() as db:
                self.assertEqual(db.execute('SELECT kind FROM warnings').fetchone()[0],'native_safety_review')
                self.assertEqual(db.execute('SELECT mode FROM policy').fetchone()[0],'openai')


class ReviewUpstream(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        wire=self.rfile.read(int(self.headers['Content-Length']))
        self.server.calls.append((wire,{k.lower():v for k,v in self.headers.items()}))
        model='wrong-model' if self.server.wrong_model else MODEL
        message={'type':'message','role':'assistant','content':[{'type':'output_text',
            'text':'{"risk_level":"high","user_authorization":"low","outcome":"deny","rationale":"Synthetic denied action"}'}]}
        response={'id':'review-response','model':model,'status':'completed','output':[message]}
        events=[{'type':'response.created','response':dict(response,status='in_progress',output=[])},
            {'type':'response.output_item.done','output_index':0,'item':message},
            {'type':'response.completed','response':response}]
        raw=b''.join(('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n').encode() for e in events)
        self.server.response_wire=raw
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


class ReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        from gateway_v4 import create_server
        from production_adapters_v4 import ProductionAdapters
        from route_registry import RouteSpec,RouteRegistry
        routes=RouteRegistry([RouteSpec('minimax','minimax','MiniMax-M3.1-Flash','tp-minimax-m31','https://api.minimax.io/v1/responses','responses-full',True,True,'synthetic full')],auto_fallback_id='minimax')
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.events=[]
        self.upstream=ThreadingHTTPServer(('127.0.0.1',0),ReviewUpstream);self.upstream.calls=[];self.upstream.wrong_model=False
        self.upthread=threading.Thread(target=self.upstream.serve_forever,daemon=True);self.upthread.start()
        class Reader:
            def read(self,token,account):return quota()
        factory=ProductionAdapters(state_root=self.root,openai_models={'gpt-test'},clock_epoch='boot',clock=lambda:100.,
            native_reader=Reader(),notifier_factory=lambda path:lambda event:self.events.append(event) or 'synthetic-shown',
            executor_factory=lambda **kwargs:(_ for _ in ()).throw(AssertionError('No guardian handoff permitted')))
        self.server=create_server(state_dir=self.root/'gateway',capability=CAP,allowed_accounts={ACCOUNT},registered_chats={(ACCOUNT,PARENT)},
            clock_epoch='boot',clock=lambda:100.,callback_factory=factory,openai_models={'gpt-test'},enroll_new_chats=True,timeout=4,route_registry=routes,credential_reader=lambda spec:'synthetic-unused')
        parent=self.server.registry.get(ACCOUNT,PARENT)
        body={'model':'gpt-test','input':[],'client_metadata':{'turn_id':'33333333-3333-4333-8333-333333333333','root_turn_id':'33333333-3333-4333-8333-333333333333'}}
        raw=json.dumps(body).encode();ident=hashlib.sha256(raw).hexdigest()
        parent.store.reserve(ident,'openai','gpt-test',raw,raw);parent.store.prepared(ident,raw,None)
        response=json.dumps({'id':'parent-response','model':'gpt-test','status':'completed','output':[]}).encode()
        parent.store.completed_upstream(ident,response,response,200);parent.store.state(ident,'completed')
        with parent.policy._transaction() as db:
            db.execute('INSERT INTO attempts VALUES(?,?,?,?)',(parent.policy.scope,'parent-still-active','reserved','openai'))
            db.execute("UPDATE policy SET mode='minimax'")
        install_native_review(self.server,enabled=True,test_endpoint=f'http://127.0.0.1:{self.upstream.server_port}/responses',allow_test_endpoint=True)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join()
        self.upstream.shutdown();self.upstream.server_close();self.upthread.join();self.tmp.cleanup()
    def send(self,raw=None,headers=None):
        if raw is None:raw,headers=fixture()
        connection=http.client.HTTPConnection(*self.server.server_address,timeout=5)
        try:
            connection.request('POST','/'+CAP+'/v1/responses',raw,headers)
            response=connection.getresponse();return response.status,response.read()
        finally:connection.close()
    def test_passthrough_preserves_wire_lite_headers_and_native_deny(self):
        raw,headers=fixture();status,body=self.send(raw,headers)
        self.assertEqual(status,200,body)
        self.assertEqual(self.upstream.calls[0][0],raw)
        for key in ('session-id','thread-id','x-codex-parent-thread-id','x-openai-subagent','x-codex-guardian','x-openai-internal-codex-responses-lite'):
            self.assertEqual(self.upstream.calls[0][1][key],headers[key])
        self.assertEqual(body,self.upstream.response_wire)
        self.assertIn(b'"outcome"',body.replace(b'\\"',b'"'))
        self.assertEqual([event['kind'] for event in self.events],['native_safety_review'])
        with self.server.registry.get(ACCOUNT,PARENT).policy._transaction() as db:
            self.assertEqual(db.execute('SELECT mode FROM policy').fetchone()[0],'minimax')
        self.assertNotEqual(self.send(raw,headers)[0],200)
        self.assertEqual(len(self.upstream.calls),1)
    def test_wrong_actual_model_is_uncertain_and_never_retried(self):
        self.upstream.wrong_model=True
        self.assertEqual(self.send()[0],502)
        self.assertNotEqual(self.send()[0],200)
        self.assertEqual(len(self.upstream.calls),1)
        scope=self.server.registry.get(ACCOUNT,CHILD)
        with scope.policy._transaction() as db:self.assertEqual(db.execute('SELECT state FROM attempts').fetchone()[0],'uncertain')
    def test_compressed_native_lite_wire_is_forwarded_unchanged(self):
        import zstandard
        raw,headers=fixture();wire=zstandard.ZstdCompressor().compress(raw);headers['content-encoding']='zstd'
        self.assertEqual(self.send(wire,headers)[0],200)
        self.assertEqual(self.upstream.calls[0][0],wire)
        self.assertEqual(self.upstream.calls[0][1]['content-encoding'],'zstd')
    def test_native_fake_jwt_requires_exact_test_fingerprint(self):
        raw,headers=fixture()
        token='eyJhbGciOiJub25lIn0.eyJzdWIiOiJzeW50aGV0aWMifQ.synthetic-signature'
        headers['authorization']='Bearer '+token
        self.assertEqual(self.send(raw,headers)[0],403)
        self.assertEqual(self.upstream.calls,[])
        self.server.native_review_test_token_sha256=hashlib.sha256(token.encode()).hexdigest()
        self.assertEqual(self.send(raw,headers)[0],200)
        self.assertEqual(self.upstream.calls[0][1]['authorization'],headers['authorization'])
    def test_spend_control_blocks_review_before_warning_or_network(self):
        changed=quota();changed['rateLimitsByLimitId']['codex']['spendControlReached']=True
        self.server.callback_factory.reader.read=lambda token,account:changed
        status,body=self.send()
        self.assertEqual(status,409);self.assertIn(b'spend_control',body)
        self.assertEqual(self.events,[]);self.assertEqual(self.upstream.calls,[])
    def test_forged_parent_response_never_notifies_or_dispatches(self):
        raw,headers=fixture();body=json.loads(raw);body['client_metadata']['parent_response_id']='not-recorded'
        self.assertEqual(self.send(json.dumps(body).encode(),headers)[0],403)
        self.assertEqual(self.upstream.calls,[]);self.assertEqual(self.events,[])


if __name__=='__main__':unittest.main()
