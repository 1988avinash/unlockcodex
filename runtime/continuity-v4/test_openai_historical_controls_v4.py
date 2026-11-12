"""Full native historical controls must not be mistaken for current lite mode."""
import copy,json,http.client,unittest
from test_multichat_v4 import MultiChatTests,CHAT_A,CAP


class OpenAIHistoricalControls(unittest.TestCase):
    def setUp(self):
        self.fixture=MultiChatTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.control={'type':'additional_tools','id':'saved-tools','role':'developer','tools':[
            {'type':'namespace','name':'functions','tools':[{'type':'function','name':'lookup','parameters':{'type':'object'}}]}]}
        self.compact={'type':'compaction','id':'old-compact','encrypted_content':'OWN_NATIVE_OPAQUE'}
        self.body={'model':'gpt-test','tools':[], 'input':[
            {'type':'message','role':'user','content':[{'type':'input_text','text':'Prior task'}]},
            self.control,self.compact,{'type':'message','role':'user','content':[{'type':'input_text','text':'Continue'}]}]}
    def send(self,body,headers=None):
        h={'thread-id':CHAT_A,'session-id':CHAT_A,'ChatGPT-Account-ID':'account-a','Authorization':'Bearer TOKEN_'+CHAT_A}
        h.update(headers or {})
        c=http.client.HTTPConnection(*self.fixture.server.server_address,timeout=6)
        c.request('POST','/'+CAP+'/responses',json.dumps(body),h)
        r=c.getresponse();result=(r.status,r.read());c.close();return result
    def test_full_saved_controls_reach_original_openai_unchanged(self):
        self.assertEqual(self.send(self.body)[0],200)
        self.assertEqual(self.fixture.upstreams['openai'].calls[-1]['body'],self.body)
        self.assertEqual(self.fixture.upstreams['minimax'].calls,[])
    def test_changed_moved_injected_control_and_current_lite_reject_before_dispatch(self):
        self.assertEqual(self.send(self.body)[0],200)
        for kind in ('changed','moved','duplicated','unanchored','lite_header','no_full_tools'):
            body=copy.deepcopy(self.body);headers={}
            body['input'][-1]['content'][0]['text']=kind
            if kind=='changed':body['input'][1]['tools'][0]['name']='new_namespace'
            if kind=='moved':body['input'][0],body['input'][1]=body['input'][1],body['input'][0]
            if kind=='duplicated':body['input'].insert(1,copy.deepcopy(body['input'][1]))
            if kind=='unanchored':body['input'].pop(2)
            if kind=='lite_header':headers['x-openai-internal-codex-responses-lite']='true'
            if kind=='no_full_tools':body.pop('tools')
            with self.subTest(kind=kind):
                self.assertEqual(self.send(body,headers)[0],409)
                self.assertEqual(len(self.fixture.upstreams['openai'].calls),1)
                self.assertEqual(self.fixture.upstreams['minimax'].calls,[])
    def test_fallback_then_recovery_retains_own_controls_and_adapts_foreign_state(self):
        self.assertEqual(self.send(self.body)[0],200)
        self.fixture.remaining[CHAT_A]=9
        body=copy.deepcopy(self.body);body['input'][-1]['content'][0]['text']='Switch now'
        self.assertEqual(self.send(body)[0],200)
        self.assertNotIn(self.control,self.fixture.upstreams['minimax'].calls[-1]['body']['input'])
        self.fixture.now+=1;self.fixture.remaining[CHAT_A]=80;self.fixture.recover=True
        foreign={'type':'reasoning','id':'reason-minimax','summary':[{'type':'summary_text','text':'Foreign visible fact'}],'encrypted_content':'FOREIGN_MINIMAX'}
        body['input'].extend([foreign,{'type':'message','role':'user','content':[{'type':'input_text','text':'Return now'}]}])
        self.assertEqual(self.send(body)[0],200)
        self.assertEqual(len(self.fixture.upstreams['openai'].calls),2)
        returned=self.fixture.upstreams['openai'].calls[-1]['body']
        self.assertIn(self.control,returned['input']);self.assertIn(self.compact,returned['input'])
        self.assertEqual(returned['tools'],self.body['tools'])
        self.assertNotIn('FOREIGN_MINIMAX',json.dumps(returned))
        self.assertIn('Foreign visible fact',json.dumps(returned))
        self.assertEqual(len([event for event in self.fixture.events if event[1]=='checkpoint']),2)

if __name__=='__main__':unittest.main()
