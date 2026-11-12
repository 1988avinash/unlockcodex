"""Native lite compaction qualification at local HTTP seam; no cloud work."""
import hashlib
import json
from pathlib import Path
import threading
import unittest
from http.server import ThreadingHTTPServer
import test_remote_compaction_v4 as remote
import test_multichat_v4 as fixtures
from route_registry import RouteSpec,RouteRegistry

DECL={'type':'additional_tools','id':'native-tools','role':'developer','tools':[]}
LITE={'x-openai-internal-codex-responses-lite':'true'}


class LiteSSEUpstream(remote.CompactUpstream):
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append({'body':body,'headers':dict(self.headers)})
        result={'id':'native-lite-compact','model':body['model'],'status':'completed','output':[remote.COMPACT]}
        events=[{'type':'response.created','response':dict(result,status='in_progress',output=[])},
            {'type':'response.output_item.added','output_index':0,'item':dict(remote.COMPACT)},
            {'type':'response.compaction.compacting'},
            {'type':'response.output_item.done','output_index':0,'item':dict(remote.COMPACT)},
            {'type':'response.completed','response':result}]
        raw=b''.join(('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n').encode() for e in events)
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)


class LiteCompactionTests(unittest.TestCase):
    setUp=fixtures.MultiChatTests.setUp
    tearDown=fixtures.MultiChatTests.tearDown
    start=fixtures.MultiChatTests.start
    stop=fixtures.MultiChatTests.stop
    send=fixtures.MultiChatTests.send
    enable=remote.RemoteCompactionTests.enable

    def configure(self,enabled=True):
        spec=RouteSpec('mimo','mimo','mimo-v2.6-flash','tp-mimo-flash',
            'https://token-plan-sgp.xiaomimimo.com/v1/responses','responses-lite',True,False,'native synthetic shape','MiMo')
        upstream=ThreadingHTTPServer(('127.0.0.1',0),fixtures.Upstream)
        upstream.route='mimo';upstream.calls=[];upstream.barrier=None
        threading.Thread(target=upstream.serve_forever,daemon=True).start();self.upstreams['mimo']=upstream
        self.kwargs.update(state_dir=str(Path(self.tmp.name)/'lite-scoped'),allow_paid_lite_compaction=enabled,
            route_registry=RouteRegistry([*self.kwargs['route_registry'].routes,spec],auto_fallback_id='minimax'))
        self.kwargs['test_upstreams']['mimo']=f'http://127.0.0.1:{upstream.server_port}/responses'
        self.enable()
        self.assertEqual(self.send(fixtures.CHAT_A,'account-a','start',[remote.USER])[0],200)

    def compact(self,items=None,headers=LITE,model='tp-mimo-flash'):
        return self.send(fixtures.CHAT_A,'account-a','compact',items if items is not None else [DECL,remote.USER,remote.TRIGGER],
            extra_headers=headers,model=model)

    def test_lite_paid_compaction_preserves_protocol_original_receipt_and_sticky_plan(self):
        self.configure()
        self.assertEqual(self.send(fixtures.CHAT_A,'account-a','select MiMo',[DECL,remote.USER],model='tp-mimo-flash',extra_headers=LITE)[0],200)
        self.upstreams['openai'].RequestHandlerClass=LiteSSEUpstream
        marker=len(self.events)
        pairs=[{'type':'function_call','name':'inspect','call_id':'older-full','arguments':'{}'},
            {'type':'function_call_output','call_id':'older-full','output':'Already completed'},
            {'type':'function_call','namespace':'functions','name':'inspect','call_id':'newer-lite','arguments':'{}'},
            {'type':'function_call_output','call_id':'newer-lite','output':'Already completed too'}]
        items=[DECL,remote.USER,*pairs,remote.TRIGGER]
        status,raw,headers=self.compact(items);self.assertEqual(status,200,raw)
        self.assertIn(b'response.completed',raw);self.assertNotIn(b'function_call',raw)
        posted=self.upstreams['openai'].calls[-1]
        self.assertEqual(posted['body']['model'],'gpt-test')
        self.assertEqual(posted['body']['input'],items)
        self.assertEqual(posted['headers']['X-OpenAI-Internal-Codex-Responses-Lite'],'true')
        self.assertEqual(self.events[marker][1:3],('notice','paid_compaction'))
        self.assertEqual(self.events[marker+1][2]['provider'],'mimo')
        scope=self.server.registry.get('account-a',fixtures.CHAT_A)
        with scope.store.transaction() as db:
            saved=db.execute('SELECT decoded FROM requests WHERE id=?',(headers['X-Continuity-Request-ID'],)).fetchone()[0]
        expected=json.dumps({'model':'tp-mimo-flash','input':items}).encode()
        self.assertEqual(saved,expected)
        self.assertEqual(hashlib.sha256(saved).hexdigest(),hashlib.sha256(expected).hexdigest())
        with scope.policy._transaction() as db:self.assertEqual(db.execute('SELECT mode FROM policy').fetchone()[0],'mimo')
        replay=dict(remote.COMPACT);replay.pop('metadata')
        self.assertEqual(self.send(fixtures.CHAT_A,'account-a','after compact',[DECL,replay,remote.USER],model='tp-mimo-flash',extra_headers=LITE)[0],200)
        sent=json.dumps(self.upstreams['mimo'].calls[-1]['body'])
        self.assertIn('Bounded outgoing-model task handoff',sent);self.assertNotIn('SYNTHETIC_OPENAI_OPAQUE',sent)

    def test_disabled_or_mismatched_lite_shape_rejects_before_paid_work(self):
        self.configure(enabled=False)
        self.assertEqual(self.compact()[0],409)
        self.server.allow_paid_lite_compaction=True
        cases=[([DECL,remote.USER,remote.TRIGGER],{},'tp-mimo-flash'),
            ([DECL,remote.USER,remote.TRIGGER],LITE,'gpt-test'),
            ([remote.USER,remote.TRIGGER],LITE,'tp-mimo-flash'),
            ([remote.USER,dict(DECL,role='user'),remote.TRIGGER],LITE,'tp-mimo-flash'),
            ([DECL,{'type':'configuration_update','reasoning':{'effort':'low'}},remote.TRIGGER],LITE,'tp-mimo-flash'),
            ([DECL,{'type':'function_call','name':'read','call_id':'unclosed','arguments':'{}'},remote.TRIGGER],LITE,'tp-mimo-flash')]
        for items,headers,model in cases:self.assertEqual(self.compact(items,headers,model)[0],409)
        self.assertEqual(len(self.upstreams['openai'].calls),1);self.assertEqual(self.events,[])

    def test_lite_compaction_never_delivers_tool_output_and_latches_uncertainty(self):
        self.configure();self.upstreams['openai'].stream_tool=True
        status,raw,_=self.compact()
        self.assertNotIn(b'INJECTED_TOOL',raw);self.assertNotIn(b'response.completed',raw)
        self.assertNotEqual(self.compact()[0],200)
        self.assertEqual(len(self.upstreams['openai'].calls),2)

    def test_full_compaction_does_not_gain_lite_header(self):
        self.configure()
        self.assertEqual(self.send(fixtures.CHAT_A,'account-a','full compact',[remote.USER,remote.TRIGGER])[0],200)
        self.assertNotIn('X-OpenAI-Internal-Codex-Responses-Lite',self.upstreams['openai'].calls[-1]['headers'])


if __name__=='__main__':unittest.main()
