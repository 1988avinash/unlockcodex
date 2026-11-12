from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from gateway_v4 import create_server
from quota_policy import Snapshot
from route_registry import RouteSpec,RouteRegistry

CHAT_A='fcba0057-466a-73b9-b5c4-a46244e34911'
CHAT_B='91fddd12-ef85-7da0-ac33-c749b226a276'
CHAT_C='2cd70c78-f57f-7918-a504-153325f4e37a'
CAP='synthetic_multichat_capability_01234567890123456789'


class Upstream(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append({'body':body,'headers':dict(self.headers)})
        if 'force uncertain response' in json.dumps(body):
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"incomplete":true}');return
        if self.server.barrier:self.server.barrier.wait(timeout=3)
        output=[{'type':'message','id':'msg-'+str(len(self.server.calls)),'role':'assistant',
            'content':[{'type':'output_text','text':'Synthetic answer'}]}]
        if self.server.route=='minimax':
            output=[{'type':'reasoning','id':'reason-minimax','summary':[{'type':'summary_text','text':'Foreign visible fact'}],
                'encrypted_content':'FOREIGN_MINIMAX'}]
        raw=json.dumps({'id':'resp-'+str(len(self.server.calls)),'status':'completed','model':body['model'],'output':output}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)


class MultiChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=100.;self.remaining={CHAT_A:50,CHAT_B:50,CHAT_C:50}
        self.recover=False;self.events=[];self.contexts={};self.upstreams={}
        for route in ('openai','minimax'):
            s=ThreadingHTTPServer(('127.0.0.1',0),Upstream);s.route=route;s.calls=[];s.barrier=None
            threading.Thread(target=s.serve_forever,daemon=True).start();self.upstreams[route]=s
        def factory(account,chat,headers,context):
            self.contexts[chat]=context
            token=headers['authorization']
            def notice(event):self.events.append((chat,'notice',event['kind'],token));return 'visible:'+chat
            def checkpoint(ident,raw):
                prior=context['last_completed_route']()
                self.events.append((chat,'checkpoint',prior,token))
                return {'request_sha256':ident,'checkpoint_id':'fresh-'+chat,'source_provider':prior['provider'] if prior else 'openai',
                    'source_model':prior['model'] if prior else 'gpt-test','checkpoint_source_sha256':'0'*64,
                    'checkpoint_turn_id':'turn-original','text':'Task handoff for '+chat,'stale':False,'requires_reconciliation':True}
            return {'snapshot_reader':lambda:Snapshot(account,self.now,'test-boot',True,True,self.remaining[chat],'trusted'),
                'notifier':notice,'checkpoint_callback':checkpoint,'recovery_reader':lambda:self.recover}
        self.kwargs=dict(state_dir=self.tmp.name,capability=CAP,allowed_accounts={'account-a','account-b'},
            registered_chats={('account-a',CHAT_A),('account-b',CHAT_B)},clock_epoch='test-boot',clock=lambda:self.now,
            callback_factory=factory,openai_models={'gpt-test'},timeout=3,
            allow_incomplete_visible_rebase=True,allow_test_upstreams=True,credential_reader=lambda spec:'MINIMAX_ONLY',
            route_registry=RouteRegistry([RouteSpec('minimax','minimax','MiniMax-M3.1-Flash','tp-minimax-m31','https://api.minimax.io/v1/responses','responses-full',True,True,'synthetic fixture')],auto_fallback_id='minimax'),
            test_upstreams={k:f'http://127.0.0.1:{s.server_port}/responses' for k,s in self.upstreams.items()})
        self.start()
    def start(self):
        self.server=create_server(**self.kwargs)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
    def stop(self):
        self.server.shutdown();self.server.server_close();self.worker.join()
    def tearDown(self):
        self.stop()
        for s in self.upstreams.values():s.shutdown();s.server_close()
        self.tmp.cleanup()
    def send(self,chat,account,text,items=None,extra_headers=None,model='gpt-test'):
        body={'model':model,'input':items if items is not None else [{'type':'message','role':'user',
            'content':[{'type':'input_text','text':text}]}]}
        headers={'thread-id':chat,'session-id':chat,'ChatGPT-Account-ID':account,'Authorization':'Bearer TOKEN_'+chat}
        if extra_headers:headers.update(extra_headers)
        c=http.client.HTTPConnection(*self.server.server_address,timeout=6)
        c.request('POST','/'+CAP+'/responses',json.dumps(body),headers)
        r=c.getresponse();result=(r.status,r.read(),dict(r.getheaders()));c.close();return result

    def test_concurrent_chats_keep_auth_history_and_stores_isolated(self):
        self.upstreams['openai'].barrier=threading.Barrier(2)
        with ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(self.send,c,a,'ONLY_'+c) for c,a in ((CHAT_A,'account-a'),(CHAT_B,'account-b'))]
            results=[f.result() for f in futures]
            self.assertEqual([result[0] for result in results],[200,200])
        # Delivery precedes durable settlement. Observe the public receipt,
        # without relabelling upstream_complete as completed in runtime code.
        for (chat,account),result in zip(((CHAT_A,'account-a'),(CHAT_B,'account-b')),results):
            deadline=time.monotonic()+2
            while True:
                conn=http.client.HTTPConnection(*self.server.server_address,timeout=2)
                conn.request('GET','/'+CAP+'/requests/'+result[2]['X-Continuity-Request-ID'],headers={
                    'thread-id':chat,'session-id':chat,'ChatGPT-Account-ID':account,'Authorization':'Bearer TOKEN_'+chat})
                response=conn.getresponse();receipt=json.loads(response.read());conn.close()
                self.assertEqual(response.status,200)
                if receipt['state']=='completed':break
                self.assertLess(time.monotonic(),deadline,'Receipt did not durably settle')
                time.sleep(.01)
        calls=self.upstreams['openai'].calls
        for call in calls:
            chat=call['headers']['Thread-Id']
            self.assertEqual(call['headers']['Authorization'],'Bearer TOKEN_'+chat)
            self.assertIn('ONLY_'+chat,json.dumps(call['body']))
            other=CHAT_B if chat==CHAT_A else CHAT_A
            self.assertNotIn(other,json.dumps(call['body']))
        self.assertNotEqual(self.contexts[CHAT_A]['state_dir'],self.contexts[CHAT_B]['state_dir'])
        self.assertEqual(self.contexts[CHAT_A]['last_completed_route']()['provider'],'openai')

    def test_nonregistered_account_mismatch_and_invalid_uuid_reject(self):
        for chat,account in ((CHAT_C,'account-a'),(CHAT_A,'account-b'),(CHAT_A,'unknown-account'),('not-a-uuid','account-a')):
            self.assertEqual(self.send(chat,account,'must block')[0],403)
        self.assertEqual(self.upstreams['openai'].calls,[])

    def test_old_opaque_baseline_fallback_and_return_are_destination_bound(self):
        old={'type':'compaction','id':'old-compact','encrypted_content':'OLD_UNATTRIBUTED'}
        user={'type':'message','role':'user','content':[{'type':'input_text','text':'Original user constraint'}]}
        self.assertEqual(self.send(CHAT_A,'account-a','old',[old,user])[0],200)
        self.assertIn('OLD_UNATTRIBUTED',json.dumps(self.upstreams['openai'].calls[-1]['body']))
        self.remaining[CHAT_A]=9
        self.assertEqual(self.send(CHAT_A,'account-a','switch',[old,user,{'type':'message','role':'user','content':[{'type':'input_text','text':'Switch now'}]}])[0],200)
        mini=self.upstreams['minimax'].calls[-1]
        self.assertNotIn('OLD_UNATTRIBUTED',json.dumps(mini['body']))
        self.assertEqual(mini['headers']['Authorization'],'Bearer MINIMAX_ONLY')
        self.assertIn((CHAT_A,'notice','incomplete_hidden_history','Bearer TOKEN_'+CHAT_A),self.events)
        boundary=[e for e in self.events if e[1]=='checkpoint']
        self.stop();self.start()
        self.assertEqual(self.send(CHAT_A,'account-a','continued',[old,user,{'type':'message','role':'user','content':[{'type':'input_text','text':'Do next step only'}]}])[0],200)
        self.assertEqual([e for e in self.events if e[1]=='checkpoint'],boundary)
        reused=self.upstreams['minimax'].calls[-1]['body']['input']
        self.assertIn('Earlier checkpoint',json.dumps(reused[0]))
        self.assertIn('Do next step only',json.dumps(reused[-1]))
        foreign={'type':'reasoning','id':'reason-minimax','summary':[{'type':'summary_text','text':'Foreign visible fact'}],
            'encrypted_content':'FOREIGN_MINIMAX'}
        self.assertEqual(self.send(CHAT_A,'account-a','own state retained',[old,user,foreign])[0],200)
        self.assertIn('FOREIGN_MINIMAX',json.dumps(self.upstreams['minimax'].calls[-1]['body']))
        self.assertEqual([e for e in self.events if e[1]=='checkpoint'],boundary)
        self.stop();self.start();self.now+=1;self.remaining[CHAT_A]=80;self.recover=True
        self.assertEqual(self.send(CHAT_A,'account-a','return',[old,user,foreign,{'type':'message','role':'user','content':[{'type':'input_text','text':'Return now'}]}])[0],200)
        returned=json.dumps(self.upstreams['openai'].calls[-1]['body'])
        self.assertIn('OLD_UNATTRIBUTED',returned);self.assertIn('Foreign visible fact',returned)
        self.assertNotIn('FOREIGN_MINIMAX',returned)
        self.assertEqual(self.contexts[CHAT_A]['last_completed_route']()['provider'],'openai')

    def test_unknown_new_or_altered_opaque_does_not_join_baseline(self):
        first={'type':'reasoning','id':'old','summary':[],'encrypted_content':'original'}
        self.assertEqual(self.send(CHAT_A,'account-a','old',[first])[0],200)
        altered=dict(first,encrypted_content='changed')
        self.assertNotEqual(self.send(CHAT_A,'account-a','altered',[altered])[0],200)
        self.assertEqual(len(self.upstreams['openai'].calls),1)

    def test_altered_unknown_opaque_is_rejected_even_with_fallback_rebase_enabled(self):
        first={'type':'compaction','id':'old','encrypted_content':'original'}
        self.assertEqual(self.send(CHAT_A,'account-a','old',[first])[0],200)
        self.remaining[CHAT_A]=9
        self.assertNotEqual(self.send(CHAT_A,'account-a','altered',[dict(first,encrypted_content='tampered')])[0],200)
        self.assertEqual(self.upstreams['minimax'].calls,[])

    def test_known_foreign_plain_tool_pairs_can_return_to_openai(self):
        self.assertEqual(self.send(CHAT_A,'account-a','initialize')[0],200)
        context=self.contexts[CHAT_A]
        scope=self.server.registry.get('account-a',CHAT_A)
        call={'type':'function_call','id':'foreign-call','call_id':'call-stable','name':'test_tool','arguments':'{}'}
        raw=json.dumps({'id':'captured-tool','status':'completed','model':'MiniMax-M3.1-Flash','output':[call]}).encode()
        scope.store.reserve('captured-known-tool','minimax','MiniMax-M3.1-Flash',b'{}',b'{}')
        scope.store.prepared('captured-known-tool',b'{"input":[]}',None)
        scope.store.completed_upstream('captured-known-tool',raw,raw,200);scope.store.state('captured-known-tool','completed')
        result={'type':'function_call_output','call_id':'call-stable','output':'Tool result unchanged'}
        self.assertEqual(self.send(CHAT_A,'account-a','tool return',[call,result])[0],200)
        expected=dict(call);expected.pop('id')
        self.assertEqual(self.upstreams['openai'].calls[-1]['body']['input'][:2],[expected,result])

    def test_known_foreign_message_id_is_omitted_on_return_without_inventing_id(self):
        self.assertEqual(self.send(CHAT_A,'account-a','initialize')[0],200)
        scope=self.server.registry.get('account-a',CHAT_A)
        item={'type':'message','id':'074256d2_msg_1','status':'completed','role':'assistant',
            'content':[{'type':'output_text','text':'Foreign task fact remains exact'}]}
        raw=json.dumps({'id':'foreign-response','status':'completed','model':'MiniMax-M3.1-Flash','output':[item]}).encode()
        scope.store.reserve('foreign-message-proof','minimax','MiniMax-M3.1-Flash',b'{}',b'{}')
        scope.store.prepared('foreign-message-proof',b'{"input":[]}',None)
        scope.store.completed_upstream('foreign-message-proof',raw,raw,200);scope.store.state('foreign-message-proof','completed')
        native=dict(item,internal_chat_message_metadata_passthrough={})
        self.assertEqual(self.send(CHAT_A,'account-a','return',[native])[0],200)
        expected=dict(native);expected.pop('id')
        self.assertEqual(self.upstreams['openai'].calls[-1]['body']['input'][:1],[expected])

    def test_explicit_new_chat_enrollment_is_scoped_and_persists(self):
        self.stop();self.kwargs['enroll_new_chats']=True;self.start()
        self.assertEqual(self.send(CHAT_C,'account-a','enroll')[0],200)
        self.stop();self.kwargs['enroll_new_chats']=False;self.start()
        self.assertEqual(self.send(CHAT_C,'account-a','persisted')[0],200)
        self.assertEqual(self.send(CHAT_C,'account-b','account swap')[0],403)

    def test_uncertain_chat_latches_across_restart_without_blocking_other_chat(self):
        self.assertEqual(self.send(CHAT_A,'account-a','force uncertain response')[0],502)
        self.stop();self.start()
        self.assertEqual(self.send(CHAT_A,'account-a','must stop after restart')[0],409)
        self.assertEqual(self.send(CHAT_B,'account-b','independent chat continues')[0],200)
        self.assertEqual(len(self.upstreams['openai'].calls),2)

    def test_receipts_require_matching_chat_scope_and_do_not_expose_auth(self):
        status,_,headers=self.send(CHAT_A,'account-a','only A request')
        self.assertEqual(status,200);ident=headers['X-Continuity-Request-ID']
        for chat,account,expected in ((CHAT_A,'account-a',200),(CHAT_B,'account-b',404)):
            c=http.client.HTTPConnection(*self.server.server_address,timeout=4)
            c.request('GET','/'+CAP+'/requests/'+ident,headers={'thread-id':chat,'session-id':chat,
                'ChatGPT-Account-ID':account,'Authorization':'Bearer TOKEN_'+chat})
            response=c.getresponse();raw=response.read();c.close()
            self.assertEqual(response.status,expected)
            self.assertNotIn(b'TOKEN_',raw);self.assertNotIn(b'only A request',raw)

    def test_durable_baseline_stores_original_hash_without_inventing_provider_origin(self):
        import hashlib
        from contextlib import closing
        import sqlite3
        item={'type':'reasoning','id':'legacy','summary':[],'encrypted_content':'UNATTRIBUTED'}
        self.assertEqual(self.send(CHAT_A,'account-a','original',[item])[0],200)
        store=self.server.registry.get('account-a',CHAT_A).store
        with closing(sqlite3.connect(store.path)) as db:
            sha,raw,destination,attribution=db.execute('SELECT request_sha256,raw,destination,attribution FROM baseline').fetchone()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),sha)
        self.assertEqual((destination,attribution),('openai','unknown'))
        self.assertIn(b'UNATTRIBUTED',raw)
        self.assertTrue(all(b'UNATTRIBUTED' not in record.response_bytes for record in store.history()[0]))

    def test_native_recovery_alias_is_explicit_warned_and_maps_actual_model(self):
        self.stop();self.kwargs['recovery_aliases']={'return-to-openai':'gpt-test'};self.start()
        self.remaining[CHAT_A]=9
        self.assertEqual(self.send(CHAT_A,'account-a','initial fallback')[0],200)
        self.now+=1;self.remaining[CHAT_A]=80
        self.assertEqual(self.send(CHAT_A,'account-a','ordinary model remains sticky')[2]['X-Continuity-Route'],'minimax')
        status,raw,headers=self.send(CHAT_A,'account-a','explicit recovery',model='return-to-openai')
        self.assertEqual(status,200);self.assertEqual(headers['X-Continuity-Route'],'openai')
        self.assertEqual(json.loads(raw)['model'],'gpt-test')
        self.assertEqual(self.upstreams['openai'].calls[-1]['body']['model'],'gpt-test')
        self.assertIn((CHAT_A,'notice','recovery','Bearer TOKEN_'+CHAT_A),self.events)
        from contextlib import closing
        import sqlite3
        store=self.server.registry.get('account-a',CHAT_A).store
        with closing(sqlite3.connect(store.path)) as db:
            decoded,forwarded=db.execute('SELECT decoded,forwarded FROM requests WHERE id=?',(headers['X-Continuity-Request-ID'],)).fetchone()
        self.assertEqual(json.loads(decoded)['model'],'return-to-openai')
        self.assertEqual(json.loads(forwarded)['model'],'gpt-test')

    def test_recovery_alias_never_overrides_insufficient_allowance_or_accepts_spoof(self):
        self.stop();self.kwargs['recovery_aliases']={'return-to-openai':'gpt-test'};self.start()
        self.remaining[CHAT_A]=9
        self.assertEqual(self.send(CHAT_A,'account-a','low quota recovery',model='return-to-openai')[2]['X-Continuity-Route'],'minimax')
        self.assertEqual(self.upstreams['openai'].calls,[])
        self.assertEqual(self.send(CHAT_A,'account-a','spoof',model='unregistered-recovery-alias')[0],400)
        self.assertEqual(len(self.upstreams['minimax'].calls),1)

    def test_recovery_alias_configuration_requires_distinct_alias_and_qualified_target(self):
        for aliases in ({'gpt-test':'gpt-test'},{'tp-minimax-m31':'gpt-test'},
                        {'return-to-openai':'unqualified-model'}):
            with self.subTest(aliases=aliases),self.assertRaises(ValueError):
                create_server(**dict(self.kwargs,recovery_aliases=aliases))

    def test_outgoing_history_preserves_openai_baseline_and_prepares_minimax_context(self):
        from history_v4 import HistoryBlocked
        old={'type':'compaction','id':'legacy','encrypted_content':'ORIGINAL_OPENAI_DESTINATION'}
        user={'type':'message','role':'user','content':[{'type':'input_text','text':'Visible user goal'}]}
        self.assertEqual(self.send(CHAT_A,'account-a','baseline',[old,user])[0],200)
        outgoing=self.contexts[CHAT_A]['outgoing_history']
        raw=json.dumps({'model':'gpt-test','input':[old,user]}).encode()
        self.assertEqual(outgoing(raw,'openai'),raw)
        prepared=json.loads(outgoing(raw,'minimax'))
        self.assertIn('Visible user goal',json.dumps(prepared))
        self.assertNotIn('ORIGINAL_OPENAI_DESTINATION',json.dumps(prepared))
        changed=json.dumps({'model':'gpt-test','input':[dict(old,encrypted_content='ALTERED')]}).encode()
        with self.assertRaises(HistoryBlocked):outgoing(changed,'openai')
        with self.assertRaises(HistoryBlocked):outgoing(changed,'minimax')
        with self.assertRaises(ValueError):outgoing(raw,'unqualified-provider')

    def test_outgoing_history_adapts_known_foreign_reasoning_and_is_chat_scoped(self):
        self.remaining[CHAT_A]=9
        self.assertEqual(self.send(CHAT_A,'account-a','create foreign reasoning')[0],200)
        self.assertEqual(self.send(CHAT_B,'account-b','independent baseline')[0],200)
        foreign={'type':'reasoning','id':'reason-minimax','summary':[{'type':'summary_text','text':'Foreign visible fact'}],
            'encrypted_content':'FOREIGN_MINIMAX'}
        raw=json.dumps({'model':'gpt-test','input':[foreign]}).encode()
        prepared=self.contexts[CHAT_A]['outgoing_history'](raw,'openai')
        self.assertNotIn(b'FOREIGN_MINIMAX',prepared);self.assertIn(b'Foreign visible fact',prepared)
        from history_v4 import HistoryBlocked
        with self.assertRaises(HistoryBlocked):self.contexts[CHAT_B]['outgoing_history'](raw,'openai')


if __name__=='__main__':unittest.main()
