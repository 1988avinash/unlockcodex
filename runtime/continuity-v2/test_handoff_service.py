import json
from pathlib import Path
import tempfile
import unittest

from handoff import make_snapshot
from handoff_service import OutgoingHandoffService, GenerationBlocked, bind_saved_checkpoint
from test_handoff import checkpoint, SOURCE


class ServiceTests(unittest.TestCase):
    def test_outgoing_model_writes_checkpoint_once_and_new_model_gets_bound_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot=make_snapshot(chat_id='chat-1',source_provider='openai',source_model='old-model',
                last_turn_id='turn-1',captured_at=1000,visible_source=SOURCE)
            requests=[]
            def generate(request):
                requests.append(request)
                return {'model':'old-model','status':'completed','output':[{'type':'message',
                    'role':'assistant','content':[{'type':'output_text','text':json.dumps(checkpoint(snapshot))}]}]}
            service=OutgoingHandoffService(Path(directory)/'service')
            saved=service.generate('logical-1',snapshot=snapshot,visible_source=SOURCE,executor=generate)
            duplicate=service.generate('logical-1',snapshot=snapshot,visible_source=SOURCE,executor=generate)
            self.assertEqual(saved['checkpoint_id'],duplicate['checkpoint_id'])
            self.assertEqual(len(requests),1)
            self.assertEqual(requests[0]['model'],'old-model')
            self.assertEqual(requests[0]['tools'],[])
            self.assertEqual(requests[0]['tool_choice'],'none')
            handoff=bind_saved_checkpoint(saved,request_sha256='a'*64,current_turn_id='turn-2',now=1005)
            self.assertTrue(handoff['stale'])
            self.assertEqual(handoff['checkpoint_turn_id'],'turn-1')
            self.assertEqual(handoff['checkpoint_source_sha256'],snapshot['source_sha256'])
            self.assertTrue(handoff['requires_reconciliation'])

    def test_uncertain_generation_survives_restart_and_blocks_id_evasion(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot=make_snapshot(chat_id='chat-1',source_provider='openai',source_model='old-model',
                last_turn_id='turn-1',captured_at=1000,visible_source=SOURCE)
            root=Path(directory)/'service'
            calls=[]
            def timeout(request):
                calls.append(request)
                raise TimeoutError('Synthetic unknown remote result')
            with self.assertRaises(TimeoutError):
                OutgoingHandoffService(root).generate('first',snapshot=snapshot,visible_source=SOURCE,executor=timeout)
            for ident in ('first','new-id'):
                with self.assertRaises(GenerationBlocked):
                    OutgoingHandoffService(root).generate(ident,snapshot=snapshot,visible_source=SOURCE,executor=timeout)
            self.assertEqual(len(calls),1)
            reopened=OutgoingHandoffService(root)
            reopened.reconcile('first',evidence_receipt='synthetic-native-status-checked')
            with self.assertRaises(GenerationBlocked):
                reopened.generate('first',snapshot=snapshot,visible_source=SOURCE,executor=timeout)
            def successor(request):
                return {'model':'old-model','status':'completed','output':[{'type':'message','role':'assistant',
                    'content':[{'type':'output_text','text':json.dumps(checkpoint(snapshot))}]}]}
            result=reopened.generate('reviewed-successor',snapshot=snapshot,visible_source=SOURCE,executor=successor)
            self.assertEqual(result['snapshot']['chat_id'],'chat-1')

    def test_wrong_model_or_tool_call_cannot_be_saved_as_handoff(self):
        for invalid in ({'model':'other-model','status':'completed','output':[]},
                        {'model':'old-model','status':'completed','output':[{'type':'function_call','name':'upload'}]}):
            with self.subTest(response=invalid), tempfile.TemporaryDirectory() as directory:
                snapshot=make_snapshot(chat_id='chat-1',source_provider='openai',source_model='old-model',
                    last_turn_id='turn-1',captured_at=1000,visible_source=SOURCE)
                service=OutgoingHandoffService(Path(directory)/'service')
                with self.assertRaises(GenerationBlocked):
                    service.generate('first',snapshot=snapshot,visible_source=SOURCE,executor=lambda request:invalid)
                self.assertIsNone(service.store.load_latest('chat-1',current_turn_id='turn-1',now=1001))


if __name__=='__main__':unittest.main()
