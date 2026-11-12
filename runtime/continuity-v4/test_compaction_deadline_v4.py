"""Deterministic owned-socket deadlines; no waits or provider requests."""
import io,json,threading,unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch
import transport_v4 as transport

RAW=b'{"model":"gpt-test","input":[{"type":"compaction_trigger"}]}'

class CompactionDeadline(unittest.TestCase):
    def exercise(self,elapsed,*,qualified=True,wrong_model=False,binding=None):
        clock=SimpleNamespace(now=100.);timers=[];calls=[];states=[];completed=[]
        class Timer:
            def __init__(self,seconds,callback):self.seconds=seconds;self.callback=callback;self.deadline=clock.now+seconds;self.cancelled=False;timers.append(self)
            def start(self):pass
            def cancel(self):self.cancelled=True
        class Socket:
            closed=False
            def shutdown(self,*args):self.closed=True
            def close(self):self.closed=True
        sock=Socket()
        response={'id':'bound-response','model':'gpt-test','status':'completed','output':[]}
        events=[{'type':'response.created','response':dict(response,status='in_progress')},
                {'type':'response.completed','response':dict(response,model='wrong' if wrong_model else 'gpt-test')}]
        wire=b''.join(('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n').encode() for e in events)
        class Response:
            status=200
            def __init__(self):self.data=io.BytesIO(wire);self.closed=False
            def getheader(self,key,default=None):return 'text/event-stream' if key=='Content-Type' else default
            def readline(self,limit):
                if self.data.tell()>0:clock.now=100.+elapsed
                if timers and clock.now>=timers[-1].deadline:timers[-1].callback()
                if sock.closed:raise OSError('Owned socket deadline')
                return self.data.readline(limit)
            def close(self):self.closed=True
        upstream=Response()
        class Connection:
            def __init__(self,*args,**kwargs):self.sock=sock;self.timeout=kwargs['timeout'];calls.append(self)
            def connect(self):pass
            def request(self,*args):self.requested=args
            def getresponse(self):return upstream
            def close(self):sock.close()
        class Store:
            def state(self,*args):states.append(args)
            def completed_upstream(self,*args):completed.append(args)
        class Handler(transport.Handler):
            def send_response(self,*args):self.response_started=True
            def send_header(self,*args):pass
            def end_headers(self):pass
        handler=object.__new__(Handler);handler.headers=Message();handler.request_id='synthetic-request'
        handler.response_started=False;handler.wfile=io.BytesIO()
        handler.server=SimpleNamespace(timeout_seconds=120,endpoints={'openai':'http://127.0.0.1/responses'},store=Store(),
            finishing=threading.Event(),thread_id='fcba0057-466a-73b9-b5c4-a46244e34911')
        if qualified:handler.remote_compaction_forwarded=RAW if binding is None else binding
        error=None
        with patch.object(transport.http.client,'HTTPConnection',Connection),patch.object(transport.threading,'Timer',Timer),patch.object(transport.time,'monotonic',lambda:clock.now):
            try:handler._forward('openai','gpt-test',RAW)
            except Exception as exc:error=exc
        self.assertTrue(upstream.closed);self.assertTrue(sock.closed);self.assertTrue(timers[0].cancelled)
        self.assertEqual(handler.server.timeout_seconds,120)
        return error,timers[0].seconds,calls,states,completed,handler.wfile.getvalue()
    def test_qualified_compaction_terminal_after180seconds_within300bound(self):
        error,seconds,calls,states,completed,wire=self.exercise(180)
        self.assertIsNone(error);self.assertEqual(seconds,300);self.assertEqual(calls[0].timeout,300)
        self.assertEqual(len(completed),1);self.assertIn(b'response.completed',wire)
    def test_ordinary_or_unbound_trigger_keeps120seconds(self):
        for options in ({'qualified':False},{'binding':b'different accepted body'}):
            with self.subTest(options=options):
                error,seconds,calls,states,completed,wire=self.exercise(180,**options)
                self.assertIsInstance(error,OSError);self.assertEqual(seconds,120)
                self.assertEqual(calls[0].timeout,120);self.assertEqual(len(calls),1);self.assertFalse(completed)
                self.assertEqual(states[-1][1],'uncertain');self.assertNotIn(b'response.completed',wire)
    def test_compaction_deadline_still_bounded_and_uncertain(self):
        error,seconds,calls,states,completed,wire=self.exercise(301)
        self.assertIsInstance(error,OSError);self.assertEqual(seconds,300)
        self.assertEqual(len(calls),1);self.assertFalse(completed);self.assertEqual(states[-1][1],'uncertain')
        self.assertNotIn(b'response.completed',wire)
    def test_extension_never_bypasses_actual_model_validation(self):
        error,seconds,calls,states,completed,wire=self.exercise(180,wrong_model=True)
        self.assertIsInstance(error,ValueError);self.assertFalse(completed);self.assertEqual(states[-1][1],'uncertain')
        self.assertNotIn(b'response.completed',wire)

if __name__=='__main__':unittest.main()
