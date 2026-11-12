"""Compaction admission uses the exact existing detached-provider byte limits."""
import unittest
from test_remote_compaction_v4 import RemoteCompactionTests,CHAT_A,USER,TRIGGER

class RemoteHandoffBound(unittest.TestCase):
    def fixture(self,provider,size):
        f=RemoteCompactionTests();f.setUp();self.addCleanup(f.tearDown);f.enable()
        if provider=='minimax':
            self.assertEqual(f.send(CHAT_A,'account-a','initial')[0],200)
            f.remaining[CHAT_A]=9
            self.assertEqual(f.send(CHAT_A,'account-a','switch')[0],200)
        f.stop();original=f.kwargs['callback_factory']
        def factory(*args):
            callbacks=original(*args);preflight=callbacks['checkpoint_callback'].preflight
            def checked(ident,raw):
                value=preflight(ident,raw);value['outgoing_request_bytes']=size;value['provider']=provider
                return value
            callbacks['checkpoint_callback'].preflight=checked;return callbacks
        factory.compaction_snapshot=original.compaction_snapshot
        f.kwargs['callback_factory']=factory;f.start();f.events.clear();f.upstreams['openai'].calls.clear()
        return f
    def test_native31mib_preflight_can_reach_authorized_compaction(self):
        for provider in ('openai','minimax'):
            with self.subTest(provider=provider):
                f=self.fixture(provider,3_110_428)
                status,raw,_=f.send(CHAT_A,'account-a','compact',[USER,TRIGGER])
                self.assertEqual(status,200,raw);self.assertEqual(len(f.upstreams['openai'].calls),1)
                self.assertEqual([e[1] for e in f.events],['notice','checkpoint'])
    def test_native_over4mib_stops_before_notice_or_generation(self):
        for provider in ('openai','minimax'):
            with self.subTest(provider=provider):
                f=self.fixture(provider,4*1024*1024+1)
                self.assertEqual(f.send(CHAT_A,'account-a','compact',[USER,TRIGGER])[0],502)
                self.assertEqual(f.events,[]);self.assertEqual(f.upstreams['openai'].calls,[])
    def test_other_provider_over2mib_remains_blocked(self):
        for provider in ('mimo','alibaba','antigravity'):
            with self.subTest(provider=provider):
                f=self.fixture(provider,2*1024*1024+1)
                self.assertEqual(f.send(CHAT_A,'account-a','compact',[USER,TRIGGER])[0],502)
                self.assertEqual(f.events,[]);self.assertEqual(f.upstreams['openai'].calls,[])

if __name__=='__main__':unittest.main()
