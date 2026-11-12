import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
import sqlite3

from production_adapters_v4 import AdapterBlocked, ProductionAdapters, canonical, MINIMAX_MODEL
from handoff_service import GenerationBlocked


class Reader:
    def __init__(self):
        self.calls = []
        self.fail = False
    def read(self, token, account):
        self.calls.append((token, account))
        if self.fail:
            raise RuntimeError('synthetic credential error must not escape')
        return {'accountId': account, 'ordinaryUsageAllowed': True,
            'rateLimitsByLimitId': {'codex': {'limitId': 'codex', 'spendControlReached': False,
                'primary': {'usedPercent': 91, 'windowDurationMins': 10080}}}}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = 100.
        self.wall = 200
        self.reader = Reader()
        self.calls, self.history_calls, self.credentials = [], [], []
        self.route = None
        self.fail_execution = False
        self.kwargs = dict(state_root=self.root, openai_models={'gpt-test'}, recovery_aliases={'back-openai': 'gpt-test'},
            native_reader=self.reader, clock=lambda:self.clock, wall_clock=lambda:self.wall, clock_epoch='synthetic-boot',
            notifier_factory=lambda path:lambda event:'synthetic-rendered', executor_factory=self.executor,
            credential_reader=self.credential)
        self.factory = ProductionAdapters(**self.kwargs)
    def credential(self, provider):
        self.credentials.append(provider)
        return 'synthetic-minimax'
    def executor(self, **kwargs):
        kwargs['evidence_dir'].mkdir(parents=True, exist_ok=False)
        def execute(request):
            self.calls.append((kwargs, request))
            if self.fail_execution:
                raise TimeoutError('synthetic uncertain remote outcome')
            snapshot = json.loads(request['prompt'].split('HOST SNAPSHOT:\n',1)[1].split('\nVISIBLE SOURCE',1)[0])
            handoff = dict(binding={key:snapshot[key] for key in ('chat_id','last_turn_id','source_sha256')},
                objective='Continue the existing task', user_intent='Preserve the same chat',
                constraints=['Reconcile actual files'], holds=[], decisions=[], changed_files=[], completed_actions=[],
                active_jobs=[], uncertain_outcomes=[], do_not_replay=['Do not retry uncertain external requests'],
                next_steps=['Reconcile before continuing'], known_gaps=['Media was not inspected'])
            return {'id':'synthetic-completed-response','model':request['model'], 'status':'completed', 'output':[{'type':'message','role':'assistant',
                'content':[{'type':'output_text','text':json.dumps(handoff)}]}]}
        return execute
    def callbacks(self, *, account='account-a', thread='chat-a', token='synthetic-native', turn='turn-1', changes=None, factory=None):
        headers = {'authorization':'Bearer '+token, 'chatgpt-account-id':account,
            'thread-id':thread, 'session-id':thread, 'x-codex-turn-metadata':json.dumps({
                'thread_id':thread,'session_id':thread,'turn_id':turn})}
        headers.update(changes or {})
        def history(raw, provider):
            self.history_calls.append(provider)
            body = json.loads(raw)
            if provider == 'minimax':
                for item in body['input']:
                    item.pop('encrypted_content', None)
            return canonical(body)
        return (factory or self.factory)(account, thread, headers, {
            'state_dir':self.root/(account+'-'+thread), 'last_completed_route':lambda:self.route,
            'outgoing_history':history})
    def request(self, text='Task input', model='gpt-test', extra=None):
        body = {'model':model, 'instructions':'Respect current holds',
            'input':[{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}]}
        body['input'].extend(extra or [])
        raw = canonical(body)
        return hashlib.sha256(raw).hexdigest(), raw
    def test_shared_cache_expiry_and_credential_isolation(self):
        a = self.callbacks()['snapshot_reader']
        b = self.callbacks(thread='chat-b')['snapshot_reader']
        self.assertTrue(a().valid)
        self.assertEqual(b().remaining_percent, 9)
        self.assertEqual(len(self.reader.calls), 1)
        self.clock += 15
        b()
        self.assertEqual(len(self.reader.calls), 2)
        self.callbacks(token='synthetic-new')['snapshot_reader']()
        self.callbacks(account='account-b')['snapshot_reader']()
        self.assertEqual(len(self.reader.calls), 4)
    def test_reader_failure_is_invalid_without_secret_error(self):
        self.reader.fail = True
        snapshot = self.callbacks()['snapshot_reader']()
        self.assertFalse(snapshot.valid)
        self.assertEqual(snapshot.reason, 'native_quota_read_failed')
    def test_compaction_preserves_full_cached_spend_control_evidence(self):
        original=self.reader.read
        def read(token,account):
            value=original(token,account)
            value['rateLimitsByLimitId']['codex']['spendControlReached']=True
            value['rateLimitsByLimitId']['codex']['credits']={'hasCredits':True,'unlimited':False}
            return value
        self.reader.read=read
        ordinary=self.callbacks()['snapshot_reader']()
        self.assertFalse(ordinary.ordinary_allowed)
        evidence=self.factory.compaction_snapshot('account-a',{'chatgpt-account-id':'account-a','authorization':'Bearer synthetic-native'})
        self.assertTrue(evidence.spend_control_reached)
        self.assertTrue(evidence.credits_available)
        self.assertEqual(evidence.quota,ordinary)
        self.assertEqual(len(self.reader.calls),1)
    def test_compaction_reader_cannot_cross_account_and_refreshes_expired_cache(self):
        headers={'chatgpt-account-id':'account-a','authorization':'Bearer synthetic-native'}
        self.factory.compaction_snapshot('account-a',headers)
        with self.assertRaises(AdapterBlocked):self.factory.compaction_snapshot('account-b',headers)
        self.clock+=15
        self.factory.compaction_snapshot('account-a',headers)
        self.assertEqual(len(self.reader.calls),2)
    def test_no_eager_history_and_no_automatic_recovery(self):
        callbacks = self.callbacks()
        self.assertEqual(self.history_calls, [])
        self.assertFalse(callbacks['recovery_reader']())
    def test_actual_outgoing_provider_and_prior_checkpoint_survive_ui_model(self):
        first = self.callbacks()['checkpoint_callback'](*self.request())
        self.assertEqual(first['source_provider'], 'openai')
        self.route = {'provider':'minimax', 'model':MINIMAX_MODEL}
        self.wall += 1
        second = self.callbacks(turn='turn-2')['checkpoint_callback'](*self.request('Next native request'))
        self.assertEqual(second['source_provider'], 'minimax')
        self.assertEqual([entry[0]['provider'] for entry in self.calls], ['openai','minimax'])
        self.assertEqual(self.calls[1][0]['token'], 'synthetic-minimax')
        self.assertEqual(self.credentials, ['minimax'])
        self.assertIn(first['checkpoint_id'], self.calls[1][1]['prompt'])
        self.assertEqual(self.history_calls, ['openai','minimax'])
    def test_native_opaque_preserved_audit_media_uninspected(self):
        ident, raw = self.request(extra=[{'type':'compaction','encrypted_content':'synthetic-opaque-only',
            'summary':[{'type':'summary_text','text':'Readable context'}]},
            {'type':'message','role':'user','content':[{'type':'input_image','image_url':'data:image/png;base64,synthetic-media'}]}])
        before = bytes(raw)
        result = self.callbacks()['checkpoint_callback'](ident, raw)
        request = self.calls[0][1]
        self.assertEqual(request['native_history'][1]['encrypted_content'], 'synthetic-opaque-only')
        self.assertNotIn('synthetic-opaque-only', request['prompt'])
        self.assertNotIn('synthetic-media', request['prompt'])
        self.assertIn('Readable context', request['prompt'])
        self.assertIn('uninspected_media', request['prompt'])
        self.assertIn('unavailable_hidden_state', request['prompt'])
        self.assertEqual(raw, before)
        self.assertFalse(result['stale'])
    def test_completed_request_reused_after_factory_restart(self):
        request = self.request()
        initial = self.callbacks()['checkpoint_callback'](*request)
        self.wall += 30
        self.route = {'provider':'minimax', 'model':MINIMAX_MODEL}
        factory = ProductionAdapters(**self.kwargs)
        reused = self.callbacks(factory=factory)['checkpoint_callback'](*request)
        self.assertEqual(initial, reused)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.history_calls, ['openai'])
    def test_uncertain_outcome_blocks_same_and_new_logical_requests(self):
        self.fail_execution = True
        callback = self.callbacks()['checkpoint_callback']
        with self.assertRaises(TimeoutError):
            callback(*self.request())
        self.fail_execution = False
        factory = ProductionAdapters(**self.kwargs)
        callback = self.callbacks(factory=factory)['checkpoint_callback']
        with self.assertRaises(GenerationBlocked):
            callback(*self.request())
        with self.assertRaises(GenerationBlocked):
            callback(*self.request('New request cannot bypass uncertainty'))
        self.assertEqual(len(self.calls), 1)
    def test_scope_and_body_binding_rejected(self):
        with self.assertRaises(AdapterBlocked):
            self.callbacks(changes={'thread-id':'different-chat'})
        with self.assertRaises(AdapterBlocked):
            self.callbacks(changes={'x-codex-turn-metadata':'{"thread_id":"elsewhere"}'})
        with self.assertRaises(AdapterBlocked):
            self.callbacks()['checkpoint_callback']('0'*64, self.request()[1])
        self.assertEqual(self.calls, [])
    def test_large_visible_source_is_not_truncated(self):
        with self.assertRaises(AdapterBlocked):
            self.callbacks()['checkpoint_callback'](*self.request('x'*(1024*1024)))
        self.assertEqual(self.calls, [])
    def test_preflight_freezes_exact_wire_without_generation_or_credentials(self):
        from handoff_executor_v4 import prepare_body
        callback=self.callbacks()['checkpoint_callback'];ident,raw=self.request()
        proof=callback.preflight(ident,raw)
        self.assertEqual(self.calls,[]);self.assertEqual(self.credentials,[]);self.assertEqual(self.reader.calls,[])
        with closing(sqlite3.connect(next(self.root.rglob('generation.sqlite3')))) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM generations').fetchone()[0],0)
        self.wall+=10
        callback(ident,raw)
        actual=prepare_body(self.calls[0][1])
        self.assertEqual(proof['outgoing_request_sha256'],hashlib.sha256(actual).hexdigest())
        self.assertEqual(proof['outgoing_request_bytes'],len(actual))
        self.assertEqual(self.history_calls,['openai'])
    def test_exact_opaque_wire_bound_rejects_before_paid_generation(self):
        from handoff_executor_v4 import ExecutorFailed
        callback=self.callbacks()['checkpoint_callback']
        request=self.request(extra=[{'type':'compaction','encrypted_content':'x'*(4*1024*1024)}])
        with self.assertRaisesRegex(ExecutorFailed,'input exceeds'):
            callback.preflight(*request)
        self.assertEqual(self.calls,[]);self.assertEqual(self.credentials,[])
        with closing(sqlite3.connect(next(self.root.rglob('generation.sqlite3')))) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM generations').fetchone()[0],0)
    def test_preflight_checks_translated_bytes_not_original_opaque_size(self):
        self.route={'provider':'minimax','model':MINIMAX_MODEL}
        callback=self.callbacks()['checkpoint_callback']
        proof=callback.preflight(*self.request(extra=[{'type':'reasoning','encrypted_content':'x'*(2*1024*1024),
            'summary':[{'type':'summary_text','text':'Retained visible summary'}]}]))
        self.assertLess(proof['outgoing_request_bytes'],20000)
        self.assertEqual(proof['provider'],'minimax')
        self.assertEqual(self.calls,[]);self.assertEqual(self.credentials,[])
    def test_qualified_recovery_alias_and_unqualified_outgoing_route(self):
        result = self.callbacks()['checkpoint_callback'](*self.request(model='back-openai'))
        self.assertEqual(result['source_model'], 'gpt-test')
        self.route = {'provider':'minimax','model':'unqualified'}
        with self.assertRaises(AdapterBlocked):
            self.callbacks(turn='turn-2')['checkpoint_callback'](*self.request('Next'))
        self.assertEqual(len(self.calls), 1)
    def test_changed_native_turn_cannot_relabel_saved_context(self):
        request = self.request()
        self.callbacks()['checkpoint_callback'](*request)
        with self.assertRaises(AdapterBlocked):
            self.callbacks(turn='new-turn')['checkpoint_callback'](*request)
        self.assertEqual(len(self.calls), 1)
    def test_no_credentials_in_persistent_context(self):
        self.callbacks()['checkpoint_callback'](*self.request())
        for file in self.root.rglob('*'):
            if file.is_file():
                self.assertNotIn(b'synthetic-native', file.read_bytes())
                self.assertNotIn(b'synthetic-minimax', file.read_bytes())

    def cold_seed(self):
        return {'provider':'openai', 'model':'gpt-test', 'last_completed_turn_id':'old-completed-turn',
            'snapshot_sha256':'a'*64, 'evidence_ref':'trusted-host-snapshot.json'}

    def test_cold_alias_preflight_has_no_generation_or_quota_side_effects(self):
        with self.assertRaisesRegex(AdapterBlocked, 'Start with a qualified OpenAI model once'):
            self.factory.validate_initial_route('account-a', 'chat-a', {'model':'tp-minimax-m31'}, lambda:None)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.credentials, [])
        self.assertEqual(list(self.root.rglob('*.sqlite3')), [])

    def test_explicit_cold_seed_qualifies_old_model_and_is_bound_in_prompt(self):
        seed = self.cold_seed()
        factory = ProductionAdapters(**self.kwargs, cold_routes={('account-a','chat-a'):seed})
        result = factory.validate_initial_route('account-a','chat-a',{'model':'tp-minimax-m31'},lambda:None)
        self.assertEqual(result['origin']['kind'], 'trusted_cold_snapshot')
        self.assertEqual(result['model'], 'gpt-test')
        saved = self.callbacks(factory=factory)['checkpoint_callback'](*self.request(model='tp-minimax-m31'))
        self.assertEqual(saved['source_provider'], 'openai')
        self.assertIn(seed['snapshot_sha256'], self.calls[0][1]['prompt'])
        self.assertIn(seed['last_completed_turn_id'], self.calls[0][1]['prompt'])
        self.assertEqual(self.calls[0][0]['token'], 'synthetic-native')

    def test_cold_seed_cannot_cross_account_or_chat(self):
        factory = ProductionAdapters(**self.kwargs, cold_routes={('account-a','chat-a'):self.cold_seed()})
        for account,chat in [('account-b','chat-a'),('account-a','chat-b')]:
            with self.assertRaises(AdapterBlocked):
                factory.validate_initial_route(account,chat,{'model':'tp-minimax-m31'},lambda:None)

    def test_completed_provider_evidence_takes_precedence_over_cold_seed(self):
        factory = ProductionAdapters(**self.kwargs, cold_routes={('account-a','chat-a'):self.cold_seed()})
        route = factory.validate_initial_route('account-a','chat-a',{'model':'gpt-test'},
            lambda:{'provider':'minimax','model':MINIMAX_MODEL,'request_sha256':'b'*64})
        self.assertEqual(route['provider'], 'minimax')
        self.assertEqual(route['origin']['kind'], 'completed_gateway_receipt')

    def test_cold_seed_copied_and_invalid_config_rejected(self):
        seed = self.cold_seed()
        factory = ProductionAdapters(**self.kwargs, cold_routes={('account-a','chat-a'):seed})
        seed['model'] = 'not-qualified'
        route = factory.validate_initial_route('account-a','chat-a',{'model':'tp-minimax-m31'},lambda:None)
        self.assertEqual(route['model'], 'gpt-test')
        for field,value in [('provider','minimax'),('snapshot_sha256','unknown'),('last_completed_turn_id',''),('model','unknown')]:
            invalid = self.cold_seed();invalid[field] = value
            with self.assertRaises(AdapterBlocked):
                ProductionAdapters(**self.kwargs,cold_routes={('account-a','chat-a'):invalid})

    def test_metadata_cannot_act_as_cold_origin_evidence(self):
        body = {'model':'tp-minimax-m31','client_metadata':{'model':'gpt-test','previous_model':'gpt-test'}}
        with self.assertRaises(AdapterBlocked):
            self.factory.validate_initial_route('account-a','chat-a',body,lambda:None)


if __name__ == '__main__':
    unittest.main()
