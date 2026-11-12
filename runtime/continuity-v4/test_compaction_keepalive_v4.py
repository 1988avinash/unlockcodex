"""Scoped compaction heartbeat shape, immutable wire, and no false success."""
import json,time,unittest
from http.server import BaseHTTPRequestHandler
from gateway_v4 import digest
from test_remote_compaction_v4 import RemoteCompactionTests,CHAT_A,USER,TRIGGER,COMPACT

class HeartbeatUpstream(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append({'body':body,'headers':dict(self.headers)})
        response={'id':'heartbeat-bound-id','model':body['model'],'status':'completed','output':[COMPACT]}
        created={'type':'response.created','response':dict(response,status='in_progress',output=[])}
        heartbeat=self.server.heartbeat
        events=([heartbeat,created] if self.server.early else [created,{'type':'response.compaction.compacting'},heartbeat])
        if self.server.terminal:events.append({'type':'response.completed','response':response})
        raw=b''.join(b'event: '+event['type'].encode()+b'\ndata: '+json.dumps(event).encode()+b'\n\n' for event in events)
        self.server.raw_wire=raw
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)

class CompactionKeepaliveTests(unittest.TestCase):
    def fixture(self,heartbeat=None,early=False,terminal=True):
        fixture=RemoteCompactionTests();fixture.setUp();self.addCleanup(fixture.tearDown);fixture.enable()
        upstream=fixture.upstreams['openai'];upstream.RequestHandlerClass=HeartbeatUpstream
        upstream.heartbeat=heartbeat if heartbeat is not None else {'type':'keepalive','sequence_number':4}
        upstream.early=early;upstream.terminal=terminal
        return fixture
    def send(self,fixture):
        return fixture.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
    def settled(self,fixture):
        scope=fixture.server.registry.get('account-a',CHAT_A)
        ident=digest({'model':'gpt-test','input':[USER,TRIGGER]});deadline=time.monotonic()+2
        while True:
            receipt=scope.store.receipt(ident)
            if receipt and receipt['state'] in ('completed','uncertain'):return scope,ident,receipt
            if time.monotonic()>deadline:self.fail('Request did not settle within bound')
            time.sleep(.01)
    def test_exact_observed_heartbeat_continues_and_retains_original_wire(self):
        fixture=self.fixture();status,raw,_=self.send(fixture)
        self.assertEqual(status,200);self.assertIn(b'"type": "keepalive", "sequence_number": 4',raw)
        self.assertIn(b'response.completed',raw)
        scope,ident,receipt=self.settled(fixture);self.assertEqual(receipt['state'],'completed')
        with scope.store.transaction() as db:wire=db.execute('SELECT response_wire FROM requests WHERE id=?',(ident,)).fetchone()[0]
        self.assertEqual(wire,fixture.upstreams['openai'].raw_wire)
    def test_malformed_heartbeat_never_delivers_payload_or_terminal(self):
        cases=[{'type':'keepalive','sequence_number':True},{'type':'keepalive','sequence_number':-1},
            {'type':'keepalive','sequence_number':4.0},{'type':'keepalive'},
            {'type':'keepalive','sequence_number':4,'item':{'type':'function_call','name':'FORBIDDEN_ACTION'}},
            {'type':'keepalive','sequence_number':4,'response':{'status':'completed'}}]
        for value in cases:
            with self.subTest(value=value):
                fixture=self.fixture(value);_,raw,_=self.send(fixture)
                self.assertNotIn(b'event: keepalive',raw);self.assertNotIn(b'FORBIDDEN_ACTION',raw)
                self.assertNotIn(b'response.completed',raw)
                self.assertEqual(self.settled(fixture)[2]['state'],'uncertain')
                count=len(fixture.upstreams['openai'].calls)
                self.assertEqual(self.send(fixture)[0],409);self.assertEqual(len(fixture.upstreams['openai'].calls),count)
    def test_keepalive_requires_previously_bound_actual_response_identity(self):
        fixture=self.fixture(early=True);_,raw,_=self.send(fixture)
        self.assertNotIn(b'event: keepalive',raw);self.assertNotIn(b'response.completed',raw)
        self.assertEqual(self.settled(fixture)[2]['state'],'uncertain')
    def test_keepalive_without_terminal_is_uncertain_and_never_replayed(self):
        fixture=self.fixture(terminal=False);_,raw,_=self.send(fixture)
        self.assertIn(b'event: keepalive',raw);self.assertNotIn(b'response.completed',raw)
        self.assertEqual(self.settled(fixture)[2]['state'],'uncertain')
        self.assertEqual(self.send(fixture)[0],409);self.assertEqual(len(fixture.upstreams['openai'].calls),1)

if __name__=='__main__':unittest.main()
