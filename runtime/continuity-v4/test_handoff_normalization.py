import copy,json,tempfile,unittest
from pathlib import Path
from production_adapters_v4 import canonical
from handoff import make_snapshot,validate_handoff,HandoffRejected
from handoff_normalization import normalize_response

class DemotionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.directory=Path(self.temp.name)
        self.snapshot=make_snapshot(chat_id='chat',source_provider='mimo',source_model='mimo-test',last_turn_id='turn',captured_at=1,visible_source=b'source')
        self.value={'binding':{k:self.snapshot[k] for k in ('chat_id','last_turn_id','source_sha256')},'objective':'Continue task','user_intent':'Continue same chat'}
        self.value.update({k:[] for k in ('constraints','holds','decisions','do_not_replay','next_steps','known_gaps','changed_files','completed_actions','active_jobs','uncertain_outcomes')})
    def response(self,value=None):return {'id':'resp-synthetic','model':'mimo-test','status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(self.value if value is None else value)}]}]}
    def run_normalize(self,response=None):return normalize_response(response or self.response(),self.snapshot,evidence_dir=self.directory)
    def test_valid_response_is_unchanged_without_artifacts(self):
        response=self.response();self.assertIs(self.run_normalize(response),response);self.assertEqual(list(self.directory.iterdir()),[])
    def test_exact_json_fence_removed_with_original_provenance(self):
        response=self.response();part=response['output'][0]['content'][0]
        part['text']='```json\n'+part['text']+'\n```'
        result=self.run_normalize(response)
        self.assertEqual(json.loads(result['output'][0]['content'][0]['text']),self.value)
        self.assertEqual(json.loads((self.directory/'original-handoff-response.json').read_bytes()),response)
        self.assertTrue(result['handoff_normalization']['exact_json_fence_removed'])
        self.assertEqual(result['handoff_normalization']['changes'],[])
    def test_fences_do_not_relax_json_binding_or_allow_extra_prose(self):
        for template in ('Before\n```json\n{}\n```','```json\n{}\n```\nAfter','```\n{}\n```','```json\n{}\n```\n```json\n{{}}\n```'):
            response=self.response();response['output'][0]['content'][0]['text']=template.format(json.dumps(self.value))
            with self.assertRaises(HandoffRejected):self.run_normalize(response)
        response=self.response();self.value['binding']={}
        response['output'][0]['content'][0]['text']='```json\n'+json.dumps(self.value)+'\n```'
        with self.assertRaises(HandoffRejected):self.run_normalize(response)
    def test_malformed_claims_are_preserved_as_unverified_notes(self):
        for field in ('changed_files','completed_actions','active_jobs','uncertain_outcomes'):self.value[field]=['unverified '+field]
        original=self.response();result=self.run_normalize(original)
        handoff=validate_handoff(result['output'][0]['content'][0]['text'].encode(),self.snapshot)
        self.assertEqual(len(handoff['known_gaps']),4)
        for field in ('changed_files','completed_actions','active_jobs','uncertain_outcomes'):
            self.assertEqual(handoff[field],[]);self.assertTrue(any(json.dumps('unverified '+field) in text for text in handoff['known_gaps']))
        self.assertEqual(json.loads((self.directory/'original-handoff-response.json').read_bytes()),original)
        self.assertFalse(result['handoff_normalization']['bindings_modified']);self.assertFalse(result['handoff_normalization']['model_requeried'])
    def test_valid_evidence_record_stays_unchanged(self):
        good={'action':'Observed marker','evidence_ref':'source:known-command-receipt'}
        self.value['completed_actions']=[good,'other claim']
        result=self.run_normalize();value=json.loads(result['output'][0]['content'][0]['text'])
        self.assertEqual(value['completed_actions'],[good]);self.assertEqual(len(value['known_gaps']),1)
    def test_binding_core_unknown_fields_and_nonlist_containers_are_never_repaired(self):
        variants=[]
        for key,value in [('binding',{}),('objective',None),('extra','unknown'),('active_jobs','not-list'),('known_gaps',[42])]:
            v=copy.deepcopy(self.value);v[key]=value;v['completed_actions']=['claim'];variants.append(v)
        for v in variants:
            with self.assertRaises(HandoffRejected):self.run_normalize(self.response(v))
        self.assertEqual(list(self.directory.iterdir()),[])
    def test_no_truncation_or_list_limit_relaxation(self):
        self.value['completed_actions']=['x'*4000]
        with self.assertRaises(HandoffRejected):self.run_normalize()
        self.value['completed_actions']=['claim'];self.value['known_gaps']=['gap']*100
        with self.assertRaises(HandoffRejected):self.run_normalize()
    def test_duplicate_inner_json_and_tool_output_are_rejected(self):
        response=self.response();response['output'][0]['content'][0]['text']='{"binding":{},"binding":{}}'
        with self.assertRaises(HandoffRejected):self.run_normalize(response)
        response=self.response();response['output'].append({'type':'function_call','name':'anything'})
        with self.assertRaises(HandoffRejected):self.run_normalize(response)
    def test_wrong_model_or_incomplete_response_cannot_gain_completion(self):
        for key,val in [('model','other'),('status','incomplete'),('id','')]:
            response=self.response();response[key]=val
            with self.assertRaises(HandoffRejected):self.run_normalize(response)

if __name__=='__main__':unittest.main()
