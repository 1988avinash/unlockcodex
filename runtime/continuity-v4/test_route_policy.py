import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'continuity-v2'))
import tempfile,unittest
from quota_policy import Snapshot
from route_policy import PlanPolicy
from route_registry import RouteRegistry,RouteSpec


class Policies(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'policy.sqlite3'
        self.args=dict(account_id='a',thread_id='chat',clock_epoch='boot',routes={'minimax':'MiniMax Token Plan','mimo':'MiMo Token Plan','alibaba':'Alibaba Token Plan','gemini':'Gemini signed-in plan'},auto_fallback_id='minimax')
        self.policy=PlanPolicy(self.path,**self.args)
        self.ready={'openai',*self.args['routes']}
    def tearDown(self):self.tmp.cleanup()
    def snapshot(self,remaining=80,observed=100):return Snapshot('a',observed,'boot',True,True,remaining,'synthetic')
    def admit(self,ident='request',remaining=80,now=100,**kw):
        return self.policy.admit(ident,self.snapshot(remaining,now),now=now,ready_routes=self.ready,**kw)
    def acknowledge(self,decision):
        self.assertEqual(decision.route_id,'pause');self.assertIsNotNone(decision.warning)
        self.policy.acknowledge_warning(decision.warning['id'],'rendered-receipt')
    def switch(self,ident,route,now=100):
        self.acknowledge(self.admit(ident,requested_route_id=route,now=now))
        d=self.admit(ident,requested_route_id=route,now=now);self.assertEqual(d.route_id,route)
        self.policy.settle(ident,'completed');return d
    def test_warn_before_auto_switch_and_no_replay(self):
        self.acknowledge(self.admit(remaining=9));self.assertEqual(self.admit(remaining=9).route_id,'minimax')
        self.assertEqual(self.admit(remaining=9).reason,'request_already_recorded')
    def test_all_plans_explicit_selection_remain_sticky_after_restart(self):
        for n,route in enumerate(self.args['routes']):
            self.switch('switch'+str(n),route,100+n)
            self.policy=PlanPolicy(self.path,**self.args)
            d=self.admit('next'+str(n),now=105+n);self.assertEqual(d.route_id,route)
            self.policy.settle('next'+str(n),'completed')
    def test_no_automatic_plan_chain_after_uncertain(self):
        self.acknowledge(self.admit(remaining=0));self.admit(remaining=0);self.policy.settle('request','uncertain')
        self.assertEqual(self.admit('new',requested_route_id='alibaba').reason,'uncertain_request')
    def test_unsupported_profile_stops_before_warning_or_reservation(self):
        self.ready.remove('mimo');d=self.admit(requested_route_id='mimo')
        self.assertEqual(d.reason,'route_unavailable_or_incompatible');self.assertIsNone(d.warning)
        self.assertEqual(self.admit().route_id,'openai')
    def test_no_credit_recovery_without_fresh_headroom(self):
        self.switch('switch','gemini')
        self.assertEqual(self.admit('low',remaining=24,now=101,recover_openai=True).route_id,'gemini')
        self.policy.settle('low','completed')
        self.acknowledge(self.admit('recover',remaining=50,now=102,recover_openai=True))
        self.assertEqual(self.admit('recover',remaining=50,now=102,recover_openai=True).route_id,'openai')
    def test_other_account_snapshot_never_admits_openai(self):
        d=self.policy.admit('new',Snapshot('other',100,'boot',True,True,99,'bad'),now=100,ready_routes=self.ready)
        self.assertEqual(d.route_id,'pause');self.assertIn('account_mismatch',d.warning['text'])
    def test_unqualified_route_cannot_be_selected(self):
        self.assertEqual(self.admit(requested_route_id='payg').reason,'invalid_route_selection')
    def test_route_change_requires_explicit_migration(self):
        changed={**self.args,'routes':{**self.args['routes'],'other':'other'}}
        with self.assertRaisesRegex(ValueError,'migration'):PlanPolicy(self.path,**changed)
    def test_reconciliation_allows_only_distinct_future_work(self):
        self.admit();self.policy.settle('request','uncertain');self.policy.reconcile('request',evidence_receipt='400-rejection-proof')
        self.assertEqual(self.admit().reason,'request_already_recorded')
        self.assertEqual(self.admit('new').route_id,'openai')
    def test_native_internal_lane_shares_plan_attempt_latch(self):
        self.switch('switch','mimo')
        with self.policy._transaction() as db:db.execute('INSERT INTO attempts VALUES(?,?,?,?)',(self.policy.scope,'guardian','reserved','openai'))
        self.assertEqual(self.admit('normal').reason,'active_request')


class Registry(unittest.TestCase):
    def spec(self,**kwargs):
        return RouteSpec(**dict(dict(route_id='minimax',provider_id='minimax',actual_model='MiniMax-M3.1-Flash',
            native_alias='tp-minimax-m31',endpoint='https://api.minimax.io/v1/responses',protocol_profile='responses-full',
            supports_native_tools=True,auto_fallback_qualified=True,qualification_ref='reviewed-receipt',display_name='MiniMax'),**kwargs))
    def test_exact_route_lookup(self):
        r=RouteRegistry([self.spec()],auto_fallback_id='minimax');self.assertEqual(r.resolve_alias('tp-minimax-m31'),r.get('minimax'))
        self.assertIsNone(r.resolve_alias('model-from-client-metadata'))
    def test_endpoint_must_be_https_and_single_per_provider(self):
        # Any provider may bring its own HTTPS endpoint...
        RouteRegistry([self.spec(provider_id='my-provider',endpoint='https://llm.example.com/v1/responses')],auto_fallback_id='minimax')
        # ...but never plaintext, credentials-in-URL, a query string, or the reserved OpenAI id.
        for opts in ({'endpoint':'http://llm.example.com/v1/responses'},{'endpoint':'https://user:pw@llm.example.com/v1/responses'},
                {'endpoint':'https://llm.example.com/v1/responses?key=1'},{'endpoint':'llm.example.com/v1/responses'},
                {'provider_id':'openai'},{'provider_id':'Bad Id'}):
            with self.assertRaises(ValueError,msg=opts):RouteRegistry([self.spec(**opts)],auto_fallback_id='minimax')
        # One provider id cannot fan its key out to a second host.
        with self.assertRaises(ValueError):
            RouteRegistry([self.spec(),self.spec(route_id='another',native_alias='tp-another',endpoint='https://other.example/v1/responses')],auto_fallback_id='minimax')
    def test_untuned_provider_gets_generic_dialect(self):
        import json
        from protocol_profiles_v4 import prepare_native
        import handoff_executor_v4 as hexec
        def effort(provider,value):
            body={'model':'x','tools':[],'input':[],'reasoning':{'effort':value},'access_programs':{'cyber':['standard']}}
            out=json.loads(prepare_native(json.dumps(body).encode(),self.spec(provider_id=provider,actual_model='some-model')))
            self.assertNotIn('access_programs',out);self.assertEqual(out['model'],'some-model')
            return out['reasoning']['effort']
        # An unknown provider only ever sees low/medium/high; tuned ids keep their own mapping.
        self.assertEqual([effort('my-provider',v) for v in ('minimal','low','medium','high','xhigh','persistent')],['low','low','medium','high','high','high'])
        self.assertEqual(effort('minimax','xhigh'),'xhigh')
        # Its checkpoint request carries no native replay and none of the OpenAI-only text controls.
        self.addCleanup(hexec.ENDPOINTS.pop,'my-provider',None)
        hexec.ENDPOINTS['my-provider']='https://llm.example.com/v1/responses'
        sent=json.loads(hexec.prepare_body({'provider':'my-provider','model':'some-model','prompt':'p'*2000,'tools':[],'tool_choice':'none',
            'max_output_tokens':4096,'native_history':[{'type':'message','role':'user','content':[]}]}))
        self.assertEqual((len(sent['input']),sent['max_output_tokens'],'text' in sent),(1,4096,False))
        with self.assertRaises(hexec.ExecutorFailed):hexec.prepare_body({'provider':'unregistered','model':'m','prompt':'p'*2000,'tools':[],'tool_choice':'none'})
    def test_text_only_and_unqualified_auto_rejected(self):
        for opts in ({'supports_native_tools':False},{'auto_fallback_qualified':False}):
            with self.assertRaises(ValueError):RouteRegistry([self.spec(**opts)],auto_fallback_id='minimax')
    def test_alias_collision_rejected(self):
        with self.assertRaises(ValueError):RouteRegistry([self.spec(),self.spec(route_id='another')],auto_fallback_id='minimax')

if __name__=='__main__':unittest.main()
