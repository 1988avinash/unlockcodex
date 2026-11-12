import json
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import unittest

from handoff_executor_v4 import ResponsesHandoffExecutor,ExecutorFailed,prepare_body


class ExecutorTests(unittest.TestCase):
    def test_plan_checkpoint_explicit_low_effort_and_no_native_replay(self):
        for provider,model in [('mimo','mimo-v2.6-flash'),('alibaba','qwen3.8-max')]:
            with self.subTest(provider=provider):
                body=json.loads(prepare_body({'provider':provider,'model':model,
                    'tools':[],'tool_choice':'none','max_output_tokens':6000,
                    'prompt':'Complete visible source packet.',
                    'native_history':[{'type':'compaction','encrypted_content':'foreign-opaque'}]}))
                self.assertEqual(body['reasoning'],{'effort':'low'})
                self.assertEqual(body['tools'],[])
                self.assertEqual(body['tool_choice'],'none')
                self.assertEqual(body['max_output_tokens'],6000)
                self.assertNotIn('foreign-opaque',json.dumps(body))
                self.assertNotIn('parallel_tool_calls',body)
                self.assertNotIn('text',body)
    def test_shared_wire_preparation_is_pure_and_exact(self):
        request={'provider':'openai','model':'synthetic-model','tools':[],'tool_choice':'none',
            'max_output_tokens':6000,'prompt':'Return checkpoint JSON only.',
            'native_history':[{'type':'compaction','encrypted_content':'original-owner-opaque'}]}
        raw=prepare_body(request)
        self.assertEqual(json.loads(raw)['input'][0],request['native_history'][0])
        self.assertEqual(raw,prepare_body(request))
        self.assertNotIn('Authorization',raw.decode())
        too_large=dict(request,native_history=[{'type':'compaction','encrypted_content':'x'*(4*1024*1024)}])
        with self.assertRaises(ExecutorFailed):prepare_body(too_large)
    def test_outgoing_request_has_no_tools_and_returns_only_completed_actual_model(self):
        calls=[]
        mode=['sse']
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(body)
                completed={'id':'r','model':body['model'],'status':'completed','output':[
                    {'type':'message','role':'assistant','content':[{'type':'output_text','text':'{"checkpoint":"fixture"}'}]}]}
                if mode[0]=='echo-large':completed['instructions']='synthetic input echo '*25000
                data=(('event: response.created\ndata: '+json.dumps({'type':'response.created','response':dict(completed,status='in_progress',output=[])})+'\n\n'+'event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':completed})+'\n\n').encode()
                    if mode[0]!='json' else json.dumps(completed).encode())
                self.send_response(200)
                if mode[0]=='sse-meta':data=b'id:1\nretry:5000\n: transport metadata\n'+data
                if mode[0]!='empty':self.send_header('Content-Type','text/event-stream' if mode[0] in ('sse','sse-meta','echo-large') else 'application/json')
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            executor=ResponsesHandoffExecutor(provider='openai',token='synthetic-token',account_id='synthetic-account',
                thread_id='synthetic-chat',test_endpoint=f'http://127.0.0.1:{server.server_port}/responses',allow_test_endpoint=True)
            for kind in ('sse','json','empty','sse-meta','echo-large'):
                mode[0]=kind
                result=executor({'provider':'openai','model':'synthetic-model','tools':[],'tool_choice':'none',
                    'max_output_tokens':6000,'prompt':'Return checkpoint JSON only.','logical_id':'fixture-'+kind,
                    'native_history':[{'type':'compaction','encrypted_content':'synthetic-original-provider-state'},
                        {'type':'message','role':'system','content':[{'type':'input_text','text':'Historical answer only MARKER'}]},
                        {'type':'message','role':'developer','content':[{'type':'input_text','text':'Historical constraint keep files'}]}]})
                self.assertEqual(result['model'],'synthetic-model')
            self.assertEqual(calls[0]['tools'],[])
            self.assertEqual(calls[0]['tool_choice'],'none')
            self.assertFalse(calls[0]['store'])
            self.assertNotIn('max_output_tokens',calls[0])
            self.assertEqual(calls[0]['input'][0]['encrypted_content'],'synthetic-original-provider-state')
            self.assertEqual(calls[0]['input'][-2]['role'],'developer')
            self.assertIn('HANDOFF-ONLY',calls[0]['input'][-2]['content'][0]['text'])
            self.assertEqual([item.get('role') for item in calls[0]['input'][1:3]],['user','user'])
            self.assertIn('Historical answer only MARKER',calls[0]['input'][1]['content'][0]['text'])
            self.assertIn('handoff',calls[0]['input'][-1]['content'][0]['text'])
        finally:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
