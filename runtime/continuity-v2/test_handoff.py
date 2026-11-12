import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from handoff import CheckpointStore, HandoffRejected, make_snapshot, build_prompt


SOURCE = b'User: finish the synthetic test. Assistant: file prepared; upload outcome unknown.'


def checkpoint(snapshot):
    return {
        'binding': {key: snapshot[key] for key in ('chat_id', 'last_turn_id', 'source_sha256')},
        'objective': 'Finish the synthetic test', 'user_intent': 'Continue in the same chat',
        'constraints': ['No production writes'], 'holds': ['Publishing is paused'],
        'decisions': ['Keep the original history'],
        'changed_files': [{'path': 'A:/synthetic/output.txt', 'sha256': 'a'*64,
                           'change': 'Prepared candidate', 'evidence_ref': 'receipt-1'}],
        'completed_actions': [{'action': 'Prepared candidate', 'evidence_ref': 'receipt-1'}],
        'active_jobs': [{'id': 'job-1', 'owner': 'synthetic-worker', 'state': 'uncertain',
                         'evidence_ref': 'receipt-2'}],
        'uncertain_outcomes': [{'action': 'Upload', 'evidence_ref': 'receipt-2',
                                'reconcile_before_retry': True}],
        'do_not_replay': ['Upload job-1 until native remote status is checked'],
        'next_steps': ['Check job-1 status, then continue unresolved work'],
        'known_gaps': ['Upload result is not confirmed']}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)/'store'
        self.snapshot = make_snapshot(chat_id='chat-1', source_provider='openai',
            source_model='gpt-test', last_turn_id='turn-1', captured_at=1000,
            visible_source=SOURCE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_model_checkpoint_survives_restart_and_preserves_uncertainty(self):
        prompt = build_prompt(self.snapshot, SOURCE)
        self.assertIn('Do not provide private chain-of-thought', prompt)
        self.assertIn(self.snapshot['source_sha256'], prompt)
        store = CheckpointStore(self.root)
        first = store.save(json.dumps(checkpoint(self.snapshot)).encode(),
            snapshot=self.snapshot, expected_latest=None)
        result = CheckpointStore(self.root).load_latest('chat-1', current_turn_id='turn-1',
                                                       now=1010, max_age_seconds=60)
        self.assertEqual(result['checkpoint_id'], first)
        self.assertFalse(result['stale'])
        self.assertTrue(result['requires_reconciliation'])
        self.assertEqual(result['handoff']['uncertain_outcomes'][0]['reconcile_before_retry'], True)
        self.assertEqual(result['snapshot']['source_provider'], 'openai')

    def test_parent_traversal_and_model_reference_traversal_are_denied(self):
        with self.assertRaises(HandoffRejected):
            CheckpointStore(self.root/'..'/'elsewhere')
        value=checkpoint(self.snapshot)
        value['changed_files'][0]['path']='../../outside.txt'
        with self.assertRaises(HandoffRejected):
            CheckpointStore(self.root).save(json.dumps(value).encode(), snapshot=self.snapshot, expected_latest=None)

    def test_older_source_boundary_cannot_replace_newer_checkpoint(self):
        store=CheckpointStore(self.root)
        first=store.save(json.dumps(checkpoint(self.snapshot)).encode(),snapshot=self.snapshot,expected_latest=None)
        older=dict(self.snapshot,captured_at=999,last_turn_id='turn-old')
        with self.assertRaises(HandoffRejected):
            store.save(json.dumps(checkpoint(older)).encode(),snapshot=older,expected_latest=first)
        self.assertEqual(store.load_latest('chat-1',current_turn_id='turn-1',now=1010)['checkpoint_id'],first)

    def test_stale_checkpoint_is_available_but_never_claims_recovered_history(self):
        store=CheckpointStore(self.root)
        self.assertIsNone(store.load_latest('chat-1',current_turn_id='turn-1',now=1000))
        store.save(json.dumps(checkpoint(self.snapshot)).encode(),snapshot=self.snapshot,expected_latest=None)
        value=store.load_latest('chat-1',current_turn_id='turn-2',now=1400,max_age_seconds=60)
        self.assertTrue(value['stale'])
        self.assertTrue(value['requires_reconciliation'])
        self.assertFalse(value['unknown_encrypted_compaction_recovered'])
        self.assertFalse(value['model_claims_verified'])

    def test_malformed_unbound_and_unsafe_claims_leave_latest_intact(self):
        store=CheckpointStore(self.root)
        valid=checkpoint(self.snapshot)
        first=store.save(json.dumps(valid).encode(),snapshot=self.snapshot,expected_latest=None)
        variants=[]
        bad=copy.deepcopy(valid); bad['binding']['chat_id']='another-chat';variants.append(json.dumps(bad).encode())
        bad=copy.deepcopy(valid); bad['uncertain_outcomes'][0]['reconcile_before_retry']=False;variants.append(json.dumps(bad).encode())
        bad=copy.deepcopy(valid); del bad['known_gaps'];variants.append(json.dumps(bad).encode())
        variants.extend([b'{"binding":1,"binding":2}',b'[]',b'NaN',b' '*131073])
        for raw in variants:
            with self.subTest(raw=raw[:40]), self.assertRaises(HandoffRejected):
                store.save(raw,snapshot=self.snapshot,expected_latest=first)
        self.assertEqual(store.load_latest('chat-1',current_turn_id='turn-1',now=1010)['checkpoint_id'],first)

    def test_concurrent_successors_cannot_overwrite_each_other(self):
        store=CheckpointStore(self.root)
        first=store.save(json.dumps(checkpoint(self.snapshot)).encode(),snapshot=self.snapshot,expected_latest=None)
        def submit(turn):
            snapshot=dict(self.snapshot,last_turn_id=turn,captured_at=1010)
            try:
                return CheckpointStore(self.root).save(json.dumps(checkpoint(snapshot)).encode(),
                    snapshot=snapshot,expected_latest=first)
            except HandoffRejected:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(submit,['turn-2','turn-3']))
        self.assertEqual(sum(value is not None for value in results),1)
        current=store.load_latest('chat-1',current_turn_id='turn-3',now=1020)
        self.assertIn(current['checkpoint_id'],results)

    def test_prompt_rejects_a_different_source_than_the_snapshot(self):
        with self.assertRaises(HandoffRejected):
            build_prompt(self.snapshot,SOURCE+b'changed')

    def test_saved_checkpoint_is_explicitly_stale_after_later_turn_or_time(self):
        store=CheckpointStore(self.root)
        store.save(json.dumps(checkpoint(self.snapshot)).encode(),snapshot=self.snapshot,expected_latest=None)
        for turn, now in [('turn-2',1010),('turn-1',1401),('turn-1',999)]:
            with self.subTest(turn=turn,now=now):
                result=store.load_latest('chat-1',current_turn_id=turn,now=now)
                self.assertTrue(result['stale'])
                self.assertTrue(result['requires_reconciliation'])
                self.assertFalse(result['unknown_encrypted_compaction_recovered'])

    def test_concurrent_writer_cannot_overwrite_the_latest_pointer(self):
        first=CheckpointStore(self.root)
        second=CheckpointStore(self.root)
        first_id=first.save(json.dumps(checkpoint(self.snapshot)).encode(),snapshot=self.snapshot,expected_latest=None)
        replacement=checkpoint(self.snapshot)
        replacement['objective']='A different checkpoint from the other writer'
        with self.assertRaises(HandoffRejected):
            second.save(json.dumps(replacement).encode(),snapshot=self.snapshot,expected_latest=None)
        self.assertEqual(second.load_latest('chat-1',current_turn_id='turn-1',now=1010)['checkpoint_id'],first_id)

    def test_wrong_chat_binding_and_replay_permission_are_rejected(self):
        for mutation in ('chat','replay','hash'):
            value=checkpoint(self.snapshot)
            if mutation=='chat': value['binding']['chat_id']='other-chat'
            if mutation=='hash': value['binding']['source_sha256']='b'*64
            if mutation=='replay': value['uncertain_outcomes'][0]['reconcile_before_retry']=False
            with self.subTest(mutation=mutation), self.assertRaises(HandoffRejected):
                CheckpointStore(self.root).save(json.dumps(value).encode(),snapshot=self.snapshot,expected_latest=None)


if __name__ == '__main__':
    unittest.main()
