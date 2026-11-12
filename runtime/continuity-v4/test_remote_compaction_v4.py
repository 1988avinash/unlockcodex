"""Remote-v2 compaction at the agreed local HTTP seam; no paid requests."""
import json
import hashlib
from http.server import BaseHTTPRequestHandler
import unittest
import test_multichat_v4 as fixtures
CHAT_A=fixtures.CHAT_A

USER={'type':'message','role':'user','content':[{'type':'input_text','text':'Keep task facts and approval constraints.'}]}
TRIGGER={'type':'compaction_trigger'}
COMPACT={'type':'compaction','id':'compact-native','encrypted_content':'SYNTHETIC_OPENAI_OPAQUE',
    'metadata':{'turn_id':'11111111-1111-4111-8111-111111111111'}}


class CompactUpstream(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append({'body':body,'headers':dict(self.headers)})
        output=[COMPACT] if body['input'][-1].get('type')=='compaction_trigger' else [
            {'type':'message','id':'ordinary-openai','role':'assistant','content':[{'type':'output_text','text':'Normal answer'}]}]
        if getattr(self.server,'bad_shape',False):output.append(dict(COMPACT,id='second-compact'))
        if getattr(self.server,'stream_tool',False):
            events=[{'type':'response.created','response':{'id':'stream-response','model':body['model'],'output':[]}},
                {'type':'response.output_item.added','item':{'type':'function_call','id':'INJECTED_TOOL','call_id':'bad-call','name':'run_tool','arguments':'{}'}},
                {'type':'response.completed','response':{'id':'stream-response','model':body['model'],'status':'completed','output':[COMPACT]}}]
            raw=''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in events).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw);return
        raw=json.dumps({'id':'actual-response','model':'WRONG' if getattr(self.server,'bad_result',False) else body['model'],'status':'completed','output':output}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)


class RemoteCompactionTests(unittest.TestCase):
    setUp=fixtures.MultiChatTests.setUp
    tearDown=fixtures.MultiChatTests.tearDown
    start=fixtures.MultiChatTests.start
    stop=fixtures.MultiChatTests.stop
    send=fixtures.MultiChatTests.send

    def enable(self):
        self.stop()
        original=self.kwargs['callback_factory']
        def factory(account,chat,headers,context):
            callbacks=original(account,chat,headers,context)
            def preflight(ident,raw):
                if getattr(self,'preflight_block',False):raise ValueError('Synthetic oversized handoff')
                route=context['last_completed_route']() or {'provider':'openai','model':'gpt-test'}
                outgoing=context['outgoing_history'](raw,route['provider'])
                return {'request_sha256':ident,'checkpoint_source_sha256':'0'*64,
                    'outgoing_request_sha256':hashlib.sha256(outgoing).hexdigest(),'outgoing_request_bytes':len(outgoing),
                    'provider':route['provider'],'model':route['model']}
            callbacks['checkpoint_callback'].preflight=preflight;return callbacks
        def evidence(account,headers):
            from paid_compaction_policy_v4 import CompactionSnapshot
            from quota_policy import Snapshot
            return CompactionSnapshot(Snapshot(account,self.now,'test-boot',True,True,self.remaining[CHAT_A],'synthetic'),
                getattr(self,'spend_control',False),None,True,True,'synthetic')
        factory.compaction_snapshot=evidence
        self.kwargs['callback_factory']=factory
        self.kwargs['allow_paid_compaction']=True
        self.upstreams['openai'].RequestHandlerClass=CompactUpstream
        self.start()

    def test_unqualified_remote_compaction_stops_before_any_upstream(self):
        status,_,_=self.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
        self.assertEqual(status,409)
        self.assertEqual(self.upstreams['openai'].calls,[])

    def test_sticky_minimax_compacts_on_openai_then_uses_bounded_summary_with_native_metadata_dropped(self):
        self.enable()
        self.assertEqual(self.send(CHAT_A,'account-a','initial',[USER])[0],200)
        self.remaining[CHAT_A]=9
        self.assertEqual(self.send(CHAT_A,'account-a','switch')[0],200)
        foreign={'type':'reasoning','id':'reason-minimax','summary':[{'type':'summary_text','text':'Foreign visible fact'}],
            'encrypted_content':'FOREIGN_MINIMAX'}
        old={'type':'message','role':'assistant','content':[{'type':'output_text','text':'OLD_LONG_CONTEXT_'+'x'*200000}]}
        marker=len(self.events)
        status,raw,_=self.send(CHAT_A,'account-a','compact',[USER,old,foreign,TRIGGER],model='tp-minimax-m31')
        self.assertEqual(status,200,raw)
        call=self.upstreams['openai'].calls[-1]['body']
        self.assertEqual(call['model'],'gpt-test');self.assertEqual(call['input'][-1],TRIGGER)
        self.assertIn('OLD_LONG_CONTEXT_',json.dumps(call))
        self.assertIn('Foreign visible fact',json.dumps(call));self.assertNotIn('FOREIGN_MINIMAX',json.dumps(call))
        self.assertEqual(self.contexts[CHAT_A]['last_completed_route']()['provider'],'minimax')
        events=self.events[marker:]
        self.assertEqual(events[0][1],'notice');self.assertEqual(events[1][1],'checkpoint')
        replay=dict(COMPACT);replay.pop('metadata')
        self.stop();self.start()
        status,raw,_=self.send(CHAT_A,'account-a','next',[replay,USER])
        self.assertEqual(status,200,raw)
        following=json.dumps(self.upstreams['minimax'].calls[-1]['body'])
        self.assertNotIn('OLD_LONG_CONTEXT_',following)
        self.assertNotIn('SYNTHETIC_OPENAI_OPAQUE',following)
        self.assertIn('Task handoff for '+CHAT_A,following)
        self.assertLess(len(following),5000)

    def test_missing_credit_warning_delivery_stops_checkpoint_and_inference(self):
        self.enable();self.stop();original=self.kwargs['callback_factory']
        def factory(*args):
            callbacks=original(*args);callbacks['notifier']=lambda event:'';return callbacks
        factory.compaction_snapshot=original.compaction_snapshot;self.kwargs['callback_factory']=factory;self.start()
        status,raw,_=self.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
        self.assertEqual(status,409,raw)
        self.assertEqual(self.upstreams['openai'].calls,[])
        self.assertFalse(any(e[1]=='checkpoint' for e in self.events))

    def test_spend_control_blocks_even_with_explicit_paid_compaction_enabled(self):
        self.enable();self.spend_control=True
        status,raw,_=self.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
        self.assertEqual(status,409,raw);self.assertEqual(self.upstreams['openai'].calls,[])
        self.assertEqual(self.events,[])

    def test_uncertain_compaction_blocks_new_request_and_restart_without_retry(self):
        self.enable();self.upstreams['openai'].bad_result=True
        status,raw,_=self.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
        self.assertEqual(status,502,raw)
        self.stop();self.start()
        self.assertEqual(self.send(CHAT_A,'account-a','different ordinary turn')[0],409)
        self.assertEqual(self.send(CHAT_A,'account-a','retry',[USER,TRIGGER])[0],409)
        self.assertEqual(len(self.upstreams['openai'].calls),1)

    def test_checkpoint_structured_history_strips_only_terminal_trigger_but_keeps_original_binding(self):
        self.enable();self.stop();original=self.kwargs['callback_factory'];seen=[]
        def factory(account,chat,headers,context):
            callbacks=original(account,chat,headers,context);checkpoint=callbacks['checkpoint_callback']
            def wrapped(ident,raw):
                prepared=context['outgoing_history'](raw,'openai')
                seen.append((json.loads(raw)['input'],json.loads(prepared)['input']))
                return checkpoint(ident,raw)
            wrapped.preflight=checkpoint.preflight
            callbacks['checkpoint_callback']=wrapped;return callbacks
        factory.compaction_snapshot=original.compaction_snapshot;self.kwargs['callback_factory']=factory;self.start()
        status,raw,_=self.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
        self.assertEqual(status,200,raw)
        self.assertEqual(seen,[([USER,TRIGGER],[USER])])

    def test_spend_control_is_rechecked_after_outgoing_handoff_before_compact_post(self):
        self.enable();self.stop();original=self.kwargs['callback_factory']
        def factory(*args):
            callbacks=original(*args);checkpoint=callbacks['checkpoint_callback']
            def wrapped(ident,raw):
                handoff=checkpoint(ident,raw);self.spend_control=True;self.now+=20;return handoff
            wrapped.preflight=checkpoint.preflight
            callbacks['checkpoint_callback']=wrapped;return callbacks
        factory.compaction_snapshot=original.compaction_snapshot;self.kwargs['callback_factory']=factory;self.start()
        status,raw,_=self.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
        self.assertNotEqual(status,200,raw)
        self.assertEqual(self.upstreams['openai'].calls,[])
        self.assertEqual(self.send(CHAT_A,'account-a','different ordinary turn')[0],409)

    def test_invalid_actual_compaction_shape_is_never_delivered_as_completed(self):
        self.enable();self.upstreams['openai'].bad_shape=True
        status,raw,_=self.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
        self.assertEqual(status,502,raw)
        self.assertEqual(self.server.registry.get('account-a',CHAT_A).store.bounded_summaries(),{})
        self.assertEqual(self.send(CHAT_A,'account-a','different ordinary turn')[0],409)
        self.assertEqual(len(self.upstreams['openai'].calls),1)

    def test_oversized_handoff_preflight_does_not_reserve_or_send_paid_work(self):
        self.enable();self.preflight_block=True
        status,raw,_=self.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
        self.assertNotEqual(status,200,raw);self.assertEqual(self.events,[])
        self.assertEqual(self.upstreams['openai'].calls,[])
        self.assertEqual(self.send(CHAT_A,'account-a','ordinary distinct turn')[0],200)

    def test_compaction_stream_rejects_tool_items_before_delivery(self):
        self.enable();self.upstreams['openai'].stream_tool=True
        status,raw,_=self.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
        self.assertNotIn(b'INJECTED_TOOL',raw)
        self.assertNotIn(b'response.completed',raw)
        self.assertEqual(self.send(CHAT_A,'account-a','different ordinary turn')[0],409)


class HistoryCaptureBounds(unittest.TestCase):
    def test_materialized_compaction_capture_matches_eight_mib_request_and_sixteen_mib_response_bounds(self):
        from history_v4 import capture_compaction,translate_request,HistoryBlocked
        before=json.dumps({'input':[{'type':'message','role':'user','content':[{'type':'input_text','text':'x'*(1100*1024)}]},TRIGGER]}).encode()
        item=dict(COMPACT,encrypted_content='opaque'+'x'*(1100*1024))
        after=json.dumps({'output':[item]}).encode()
        record=capture_compaction(before,after,chat_id=CHAT_A,source_provider='openai')
        replay=json.dumps({'input':[item]}).encode()
        result=translate_request(replay,chat_id=CHAT_A,source_provider='openai',target_provider='openai',
            disclosure_acknowledged=True,compaction_records=[record],max_bytes=8*1024*1024)
        self.assertEqual(json.loads(result.request_bytes)['input'],[item])
        with self.assertRaises(HistoryBlocked):
            capture_compaction(b' '*(8*1024*1024+1),after,chat_id=CHAT_A,source_provider='openai')
        with self.assertRaises(HistoryBlocked):
            capture_compaction(before,b' '*(16*1024*1024+1),chat_id=CHAT_A,source_provider='openai')


if __name__=='__main__':unittest.main()
