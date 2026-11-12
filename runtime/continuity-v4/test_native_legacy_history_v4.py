"""Actual installed-wire shapes with adversarial synthetic content only."""
import copy,hashlib,json,tempfile,unittest
from email.message import Message
from pathlib import Path
from gateway_v4 import Registry,outgoing_history,scoped_visible_rebase,HistoryBlocked,qualify_compaction_protocol
from route_registry import RouteRegistry,RouteSpec
from history_v4 import capture_response
from protocol_profiles_v4 import prepare_native

class NativeLegacyHistory(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        routes=RouteRegistry([RouteSpec('minimax','minimax','MiniMax-M3.1-Flash','tp-minimax-m31',
            'https://api.minimax.io/v1/responses','responses-full',True,True,'synthetic')],auto_fallback_id='minimax')
        self.scope=Registry(Path(self.tmp.name),{'account'},[('account','fcba0057-466a-73b9-b5c4-a46244e34911')],False,'boot',routes).get('account','fcba0057-466a-73b9-b5c4-a46244e34911')
        self.peer={'type':'agent_message','id':'peer1','author':'/root/peer','recipient':'/root',
            'content':[{'type':'input_text','text':'Exact peer claim; do not rerun job.'},
                       {'type':'encrypted_content','encrypted_content':'PEER_OPAQUE'}],
            'internal_chat_message_metadata_passthrough':{'opaque':'META'}}
        self.call={'type':'function_call','id':'fc1','call_id':'call1','name':'lookup','namespace':'functions',
            'arguments':'{"query":"original"}','status':'completed','encrypted_content':'CALL_OPAQUE',
            'internal_chat_message_metadata_passthrough':{'opaque':'META'}}
        self.result={'type':'function_call_output','call_id':'call1','output':'Existing result'}
    def enroll(self,items):
        raw=json.dumps({'model':'gpt-test','tools':[],'input':items}).encode()
        self.scope.store.ensure_baseline(raw,[],[]);return raw
    def test_exact_peer_and_closed_call_visible_rebase_preserves_original(self):
        raw=self.enroll([self.peer,self.call,self.result])
        self.assertEqual(outgoing_history(self.scope,raw,'openai',allow_incomplete_visible_rebase=True),raw)
        translated,receipt=scoped_visible_rebase(self.scope,raw,'minimax',[],[])
        body=json.loads(translated.request_bytes);message,call,result=body['input']
        self.assertEqual(translated.original_bytes,raw)
        self.assertEqual(message['role'],'assistant');text=message['content'][0]['text']
        for value in ('/root/peer','/root','Exact peer claim; do not rerun job.'):
            self.assertIn(value,text)
        self.assertIn('not verified',text)
        for key in ('id','call_id','name','namespace','arguments','status'):self.assertEqual(call[key],self.call[key])
        self.assertEqual(result,self.result)
        self.assertNotIn('PEER_OPAQUE',translated.request_bytes.decode());self.assertNotIn('CALL_OPAQUE',translated.request_bytes.decode())
        self.assertEqual(len(receipt['baseline_opaque_sha256']),2)
        self.assertEqual(receipt['legacy_items_rebased'],{'agent_message':1,'function_call':1})
    def test_unknown_fields_parts_changed_opaque_and_pending_calls_fail(self):
        raw=self.enroll([self.peer,self.call,self.result])
        cases=[]
        for changed in ('unknown_field','unknown_content','changed_opaque','pending','invalid_arguments'):
            body=json.loads(raw)
            if changed=='unknown_field':body['input'][0]['remote_reference']='unqualified'
            if changed=='unknown_content':body['input'][0]['content'].append({'type':'file_reference','file_id':'x'})
            if changed=='changed_opaque':body['input'][0]['content'][1]['encrypted_content']='ALTERED'
            if changed=='pending':body['input'].pop()
            if changed=='invalid_arguments':body['input'][1]['arguments']='{bad'
            cases.append((changed,json.dumps(body).encode()))
        for name,changed in cases:
            with self.subTest(name=name),self.assertRaises(HistoryBlocked):scoped_visible_rebase(self.scope,changed,'minimax',[],[])
    def historical(self):
        declaration={'type':'additional_tools','id':'old-tools','role':'developer','tools':[
            {'type':'namespace','name':'old_functions','description':'Prior schema','tools':[{'type':'function','name':'prior','parameters':{'type':'object'}}]}]}
        compact={'type':'compaction','id':'compact-old','encrypted_content':'OLD_COMPACT'}
        lead={'type':'message','role':'user','content':[{'type':'input_text','text':'Prior task'}]}
        raw=self.enroll([lead,declaration,compact,self.peer,self.call,self.result])
        return raw,declaration,compact
    def test_historical_schema_is_inert_at_exact_position_and_current_tools_unchanged(self):
        raw,declaration,compact=self.historical()
        result,receipt=scoped_visible_rebase(self.scope,raw,'minimax',[],[])
        body=json.loads(result.request_bytes)
        self.assertEqual(body['tools'],[])
        text=body['input'][1]['content'][0]['text']
        self.assertIn('inert context only',text)
        self.assertEqual(json.loads(text.split('Original declaration JSON:\n',1)[1]),declaration)
        self.assertEqual(len(receipt['historical_declarations']),1)
        self.assertEqual(outgoing_history(self.scope,raw,'openai',allow_incomplete_visible_rebase=True),raw)
        request=json.loads(raw);request['input'].append({'type':'compaction_trigger'})
        self.assertFalse(qualify_compaction_protocol(self.scope,request,None,Message(),False))
        self.assertEqual(request['input'][1],declaration)
    def test_multiple_lite_controls_preserve_exact_native_position_and_incremental_definitions(self):
        raw,declaration,compact=self.historical();body=json.loads(raw);body.pop('tools')
        current={'type':'additional_tools','id':'current-tools','role':'developer','tools':[]}
        body['input'].insert(0,current)
        result,_=scoped_visible_rebase(self.scope,json.dumps(body).encode(),'minimax',[],[])
        self.assertEqual(json.loads(result.request_bytes)['input'][0],current)
        self.assertEqual(json.loads(result.request_bytes)['input'][2],declaration)
        selected=RouteSpec('mimo','mimo','mimo-v2.6-flash','tp-mimo-flash','https://token-plan-sgp.xiaomimimo.com/v1/responses','responses-lite',True,False,'fixture')
        headers=Message();headers['x-openai-internal-codex-responses-lite']='true'
        body['input'].append({'type':'compaction_trigger'})
        original=copy.deepcopy(body)
        self.assertTrue(qualify_compaction_protocol(self.scope,body,selected,headers,True))
        self.assertEqual(body,original)
        prepared=json.loads(prepare_native(result.request_bytes,selected))
        self.assertEqual([i for i in prepared['input'] if i.get('type')=='additional_tools'],[current,declaration])
        with self.assertRaises(HistoryBlocked):qualify_compaction_protocol(self.scope,body,selected,headers,False)
    def test_new_changed_malformed_or_unanchored_historical_controls_reject(self):
        raw,_,_=self.historical()
        for kind in ('changed','injected','unanchored','malformed'):
            body=json.loads(raw)
            if kind=='changed':body['input'][1]['tools'][0]['description']='changed'
            if kind=='injected':body['input'].append(copy.deepcopy(body['input'][1]))
            if kind=='unanchored':body['input'].pop(2)
            if kind=='malformed':body['input'][1]['unknown']='x'
            with self.subTest(kind=kind),self.assertRaises(HistoryBlocked):
                scoped_visible_rebase(self.scope,json.dumps(body).encode(),'minimax',[],[])
    def test_new_peer_observation_is_authorized_scoped_durable_and_not_an_issuer_claim(self):
        initial=self.enroll([{'type':'message','role':'user','content':[]}])
        body=json.loads(initial);body['input'].append(self.peer);raw=json.dumps(body).encode()
        with self.assertRaises(HistoryBlocked):self.scope.store.validate_opaque_origins(raw,[],[])
        self.scope.store.validate_opaque_origins(raw,[],[],allow_host_peer=True)
        translated,receipt=scoped_visible_rebase(self.scope,raw,'minimax',[],[])
        self.assertEqual(len(receipt['host_observed_peer_sha256']),1)
        self.assertTrue(receipt['host_observation_is_not_sender_or_provider_verification'])
        self.assertIn('Claimed attribution',translated.request_bytes.decode())
        self.assertEqual(outgoing_history(self.scope,raw,'openai',allow_incomplete_visible_rebase=True),raw)
        for field in ('author','recipient','ciphertext','text','unknown'):
            changed=copy.deepcopy(body)
            if field=='ciphertext':changed['input'][-1]['content'][1]['encrypted_content']='ALTERED'
            elif field=='text':changed['input'][-1]['content'][0]['text']='Different text'
            elif field=='unknown':changed['input'][-1]['unqualified']='Different semantic field'
            else:changed['input'][-1][field]='different'
            with self.subTest(field=field),self.assertRaises(HistoryBlocked):
                self.scope.store.validate_opaque_origins(json.dumps(changed).encode(),[],[],allow_host_peer=True)
        newer=copy.deepcopy(self.peer);newer['id']='peer2';body['input'].append(newer);newraw=json.dumps(body).encode()
        self.scope.store.validate_opaque_origins(newraw,[],[],allow_host_peer=True)
        _,next_receipt=scoped_visible_rebase(self.scope,newraw,'minimax',[],[])
        self.assertNotEqual(receipt['baseline_opaque_sha256'],next_receipt['baseline_opaque_sha256'])
        self.assertEqual(self.scope.store.history(),([],[]))
    def test_only_native_metadata_changes_preserve_semantic_identity_and_raw_observations(self):
        raw=self.enroll([self.peer,self.call,self.result])
        self.scope.store.validate_opaque_origins(raw,[],[],allow_host_peer=True)
        _,prior=scoped_visible_rebase(self.scope,raw,'minimax',[],[])
        body=json.loads(raw)
        for item in body['input'][:2]:
            item['internal_chat_message_metadata_passthrough']={'turn_id':'native-turn','content_item_metadata':[{'provenance':{}}]}
        changed=json.dumps(body).encode()
        self.scope.store.validate_opaque_origins(changed,[],[],allow_host_peer=True)
        result,receipt=scoped_visible_rebase(self.scope,changed,'minimax',[],[])
        self.assertEqual(prior['baseline_opaque_sha256'],receipt['baseline_opaque_sha256'])
        self.assertNotEqual(prior['omitted_item_raw_sha256'],receipt['omitted_item_raw_sha256'])
        self.assertEqual(outgoing_history(self.scope,changed,'openai',allow_incomplete_visible_rebase=True),changed)
        with self.scope.store.transaction() as db:
            observations=db.execute('SELECT COUNT(*) FROM native_peer_observations').fetchone()[0]
            sources={r[0] for r in db.execute('SELECT raw FROM native_peer_requests')}
        self.assertEqual(observations,2);self.assertEqual(sources,{raw,changed})
        self.assertEqual(json.loads(result.request_bytes)['input'][1]['arguments'],self.call['arguments'])
    def test_known_provider_peer_uses_response_origin_not_host_observation(self):
        self.enroll([])
        record=capture_response(json.dumps({'status':'completed','output':[self.peer]}).encode(),chat_id=self.scope.thread_id,provider='openai')
        raw=json.dumps({'model':'gpt-test','input':[self.peer]}).encode()
        self.scope.store.validate_opaque_origins(raw,[record],[],allow_host_peer=True)
        self.assertEqual(self.scope.store.native_peer_hashes(),set())
        result,receipt=scoped_visible_rebase(self.scope,raw,'minimax',[record],[])
        self.assertIn('Exact peer claim',result.request_bytes.decode());self.assertEqual(receipt['host_observed_peer_sha256'],[])
    def test_new_peer_unknown_shape_and_no_readable_content_do_not_enroll(self):
        self.enroll([])
        for kind in ('opaque_only','unknown_part','unknown_field','missing_id','empty_content'):
            item=copy.deepcopy(self.peer)
            if kind=='opaque_only':item['content']=item['content'][1:]
            if kind=='unknown_part':item['content'].append({'type':'file_reference','file_id':'x'})
            if kind=='unknown_field':item['unqualified']='x'
            if kind=='missing_id':item.pop('id')
            if kind=='empty_content':item['content']=[]
            with self.subTest(kind=kind),self.assertRaises(HistoryBlocked):
                self.scope.store.validate_opaque_origins(json.dumps({'input':[item]}).encode(),[],[],allow_host_peer=True)
        self.assertEqual(self.scope.store.native_peer_hashes(),set())
    def test_new_peer_invalidates_handoff_coverage_once_then_same_history_reuses(self):
        from test_multichat_v4 import MultiChatTests,CHAT_A
        fixture=MultiChatTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        user={'type':'message','role':'user','content':[{'type':'input_text','text':'Initial task'}]}
        self.assertEqual(fixture.send(CHAT_A,'account-a','initial',[user])[0],200)
        fixture.remaining[CHAT_A]=9
        peer=copy.deepcopy(self.peer)
        self.assertEqual(fixture.send(CHAT_A,'account-a','switch',[user,peer])[0],200)
        checkpoints=lambda:len([event for event in fixture.events if event[1]=='checkpoint'])
        self.assertEqual(checkpoints(),1)
        next_user=copy.deepcopy(user);next_user['content'][0]['text']='New user continuation'
        self.assertEqual(fixture.send(CHAT_A,'account-a','same',[user,peer,next_user])[0],200)
        self.assertEqual(checkpoints(),1)
        newer=copy.deepcopy(peer);newer['id']='peer-after-switch';newer['content'][0]['text']='New peer completion claim'
        self.assertEqual(fixture.send(CHAT_A,'account-a','new peer',[user,peer,next_user,newer])[0],200)
        self.assertEqual(checkpoints(),2)
        next_user['content'][0]['text']='Another user continuation'
        self.assertEqual(fixture.send(CHAT_A,'account-a','reuse',[user,peer,next_user,newer])[0],200)
        self.assertEqual(checkpoints(),2)

    def test_peer_registration_requires_exact_item_membership_in_source(self):
        raw=self.enroll([])
        with self.assertRaisesRegex(HistoryBlocked,'membership'):
            self.scope.store.record_native_peer(self.peer,raw)
        changed=copy.deepcopy(self.peer)
        changed['internal_chat_message_metadata_passthrough']={'different_native_metadata':True}
        with self.assertRaisesRegex(HistoryBlocked,'membership'):
            self.scope.store.record_native_peer(self.peer,json.dumps({'input':[changed]}).encode())
        self.assertEqual(self.scope.store.native_peer_hashes(),set())

    def test_peer_readback_rejects_valid_hashed_request_without_observed_item(self):
        raw=self.enroll([self.peer])
        self.scope.store.validate_opaque_origins(raw,[],[],allow_host_peer=True)
        other=json.dumps({'model':'gpt-test','input':[]}).encode();key=hashlib.sha256(other).hexdigest()
        with self.scope.store.transaction() as db:
            db.execute('INSERT INTO native_peer_requests VALUES(?,?)',(key,other))
            db.execute('UPDATE native_peer_items SET request_sha256=?',(key,))
            db.execute('UPDATE native_peer_observations SET request_sha256=?',(key,))
        with self.assertRaisesRegex(HistoryBlocked,'membership'):
            self.scope.store.native_peer_hashes()

if __name__=='__main__':unittest.main()
