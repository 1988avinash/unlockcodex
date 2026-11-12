from contextlib import nullcontext
import unittest
import hashlib,json,tempfile
from pathlib import Path
import service_startup
import subprocess,sys
from unittest.mock import patch,MagicMock
from service_startup import ensure_running, StartupBlocked,load_bundle,read_health

class StartupBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(service_startup.os.name=='nt','Native Windows token and file locks')
    def test_registration_lock_works_without_service_bundle_or_optional_identity_env(self):
        from startup_registration import WindowsStartupBackend
        original=service_startup.current_user_sid()
        with patch.dict(service_startup.os.environ,{'USERDOMAIN':'misleading','USERNAME':'not-the-user'}):
            self.assertEqual(service_startup.current_user_sid(),original)
            with WindowsStartupBackend(service_startup).lock():pass
        with patch.dict(service_startup.os.environ,{},clear=True):self.assertEqual(service_startup.current_user_sid(),original)

    @unittest.skipUnless(service_startup.os.name=='nt','Native cross-process byte-range lock')
    def test_shared_journal_file_excludes_another_process_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'startup.lock'
            script="import sys;sys.path.insert(0,sys.argv[1]);from service_startup import journal_lock,StartupBlocked\ntry:\n with journal_lock(sys.argv[2],timeout=.2): print('acquired')\nexcept StartupBlocked: print('blocked')"
            command=[sys.executable,'-I','-S','-B','-c',script,str(Path(service_startup.__file__).parent),str(path)]
            with service_startup.journal_lock(path):
                result=subprocess.run(command,capture_output=True,text=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.stdout.strip(),'blocked')
            result=subprocess.run(command,capture_output=True,text=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.stdout.strip(),'acquired')

    def test_public_canary_health_gate_accepts_only_bound_pair(self):
        service=MagicMock();bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64},'service':service}
        pair={'wrapper':{'pid':10},'child':{'pid':11}}
        health={'mode':'verified_multichat_continuity_service','architecture_version':4,'pid':11,'port':12345,**bundle['pins'],'process_identity':dict(pair,job={'qualified':True})}
        self.assertEqual(service_startup.validate_health_process(health,bundle,pair),pair)
        for invalid in (True,{},dict(pair,child={'pid':12}),{'original_pair_gone':True}):
            with self.assertRaises(StartupBlocked):service_startup.validate_health_process(health,bundle,invalid)

    def test_mutex_name_uses_verified_sid_not_optional_environment(self):
        sid='S-1-5-21-123';names=[]
        for env in ({},{'USERNAME':'user','USERDOMAIN':'PC'},{'USERNAME':'spoof','USERDOMAIN':'elsewhere'}):
            with patch.dict(service_startup.os.environ,env,clear=True):names.append(service_startup.mutex_name(sid,12345))
        self.assertEqual(len(set(names)),1)
        self.assertNotEqual(names[0],service_startup.mutex_name('S-1-5-21-456',12345))
        self.assertNotEqual(names[0],service_startup.mutex_name(sid,12346))

    def test_verified_dead_pair_allows_new_launch_preserving_prior_journal(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        prior={'pid':11,'status':'healthy','config_sha256':'a'*64,'process_identity':{'wrapper':{'pid':10,'creation_filetime':100},'child':{'pid':11,'creation_filetime':101}}}
        retired={'original_pair_gone':True,'recorded_identity':prior['process_identity'],'exit_evidence':{'wrapper':'absent','child':'different_birth'}}
        health={'mode':'verified_multichat_continuity_service','architecture_version':4,'pid':21,'port':12345,**bundle['pins']}
        replies=iter([None,health]);journal=[]
        result=ensure_running(bundle,health_reader=lambda c:next(replies),process_owner=lambda pid,b:retired if pid==11 else True,
            launch=lambda b:{'pid':21},mutex=lambda b:nullcontext(),read_pending=lambda:prior,write_pending=journal.append)
        self.assertEqual(result['pid'],21);self.assertEqual(journal[0]['previous_journal'],prior)
        self.assertEqual(journal[0]['previous_exit_evidence'],retired)

    def test_missing_pending_wrapper_cannot_restart_without_both_original_births(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        prior={'pid':10,'status':'pending','config_sha256':'a'*64,'process_identity':{'wrapper':{'pid':10,'creation_filetime':100}}}
        with self.assertRaisesRegex(StartupBlocked,'fully reconciled'):
            ensure_running(bundle,health_reader=lambda c:None,process_owner=lambda p,b:None,launch=lambda b:self.fail('duplicate'),
                mutex=lambda b:nullcontext(),read_pending=lambda:prior)

    def test_health_self_report_must_match_independent_process_pair(self):
        service=MagicMock();bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64},'service':service}
        pair={'wrapper':{'pid':10,'creation_filetime':123},'child':{'pid':11,'creation_filetime':124}}
        health={'mode':'verified_multichat_continuity_service','architecture_version':4,'pid':11,'port':12345,**bundle['pins'],
            'process_identity':{'wrapper':pair['wrapper'],'child':{'pid':11,'creation_filetime':999},'job':{}}}
        with self.assertRaisesRegex(StartupBlocked,'independently observed'):
            ensure_running(bundle,health_reader=lambda c:health,process_owner=lambda p,b:pair,
                launch=lambda b:self.fail('duplicate'),mutex=lambda b:nullcontext())
        service.process_module.assert_not_called()
    def test_corrupted_pending_pid_cannot_be_replaced_by_valid_health(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        pair={'wrapper':{'pid':10,'creation_filetime':123},'child':{'pid':11,'creation_filetime':124}}
        health={'mode':'verified_multichat_continuity_service','architecture_version':4,'pid':11,'port':12345,**bundle['pins']}
        with self.assertRaisesRegex(StartupBlocked,'Journal PID'):
            ensure_running(bundle,health_reader=lambda c:health,process_owner=lambda p,b:pair,
                launch=lambda b:self.fail('duplicate'),mutex=lambda b:nullcontext(),
                read_pending=lambda:{'pid':999,'status':'pending','config_sha256':'a'*64,'process_identity':pair})
    def test_windows_delayed_refusal_uses_connect_bound_but_never_retries_timeout(self):
        connection=MagicMock();connection.connect.side_effect=ConnectionRefusedError()
        with patch.object(service_startup.http.client,'HTTPConnection',return_value=connection) as factory:
            self.assertIsNone(read_health({'host':'127.0.0.1','port':12345,'capability':'synthetic'}))
            self.assertEqual(factory.call_args.kwargs['timeout'],5)
            connection.connect.assert_called_once();connection.request.assert_not_called()
        connection=MagicMock();connection.connect.side_effect=TimeoutError()
        with patch.object(service_startup.http.client,'HTTPConnection',return_value=connection):
            with self.assertRaisesRegex(StartupBlocked,'unknown listener'):read_health({'host':'127.0.0.1','port':12345,'capability':'synthetic'})
            connection.connect.assert_called_once()
    def test_redirector_wrapper_binds_distinct_health_child_and_reuses_pair(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        wrapper={'pid':43,'creation_filetime':123};child={'pid':44,'parent':43,'creation_filetime':124}
        pair={'wrapper':wrapper,'child':child};journal=[];launches=[]
        health={'mode':'verified_multichat_continuity_service','architecture_version':4,'pid':44,'port':12345,**bundle['pins']}
        def owner(pid,b):
            expected=b.get('expected_process_identity')
            self.assertEqual(expected['wrapper'],wrapper)
            return pair if pid==44 else {'wrapper':wrapper}
        replies=iter([None,health])
        result=ensure_running(bundle,health_reader=lambda c:next(replies),process_owner=owner,
            launch=lambda b:launches.append(1) or {'pid':43,'process_identity':{'wrapper':wrapper}},
            mutex=lambda b:nullcontext(),write_pending=journal.append)
        self.assertEqual(result['pid'],44);self.assertEqual(journal[-1]['process_identity'],pair)
        result=ensure_running(bundle,health_reader=lambda c:health,process_owner=owner,
            launch=lambda b:self.fail('duplicate'),mutex=lambda b:nullcontext(),read_pending=lambda:journal[-1])
        self.assertEqual(result['status'],'already_running');self.assertEqual(launches,[1])

    def test_other_wrapper_or_reused_birth_cannot_claim_pending_health(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        wrapper={'pid':43,'creation_filetime':123}
        health={'mode':'verified_multichat_continuity_service','architecture_version':4,'pid':44,'port':12345,**bundle['pins']}
        for changed in ({'pid':45,'creation_filetime':123},{'pid':43,'creation_filetime':999}):
            replies=iter([None,health])
            with self.assertRaises(StartupBlocked):
                ensure_running(bundle,health_reader=lambda c:next(replies),process_owner=lambda p,b:{'wrapper':changed,'child':{'pid':44}},
                    launch=lambda b:{'pid':43,'process_identity':{'wrapper':wrapper}},mutex=lambda b:nullcontext())

    def test_matching_live_instance_is_reused_without_launch(self):
        bundle={'config':{'host':'127.0.0.1','port':12345,'capability':'synthetic'},'pins':{'config_sha256':'a'*64,'manifest_sha256':'b'*64,'native_runtime_sha256':'c'*64}}
        health={'mode':'verified_multichat_continuity_service','architecture_version':4,'pid':42,'port':12345,**bundle['pins']}
        calls=[]
        result=ensure_running(bundle,health_reader=lambda config:health,process_owner=lambda pid,b:pid==42,
            launch=lambda b:calls.append(b),mutex=lambda b:nullcontext())
        self.assertEqual(result['status'],'already_running');self.assertEqual(result['pid'],42);self.assertEqual(calls,[])

    def test_absent_service_launches_once_then_requires_matching_pid_health(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        health={'mode':'verified_multichat_continuity_service','architecture_version':4,'pid':43,'port':12345,**bundle['pins']}
        replies=iter([None,None,health]);calls=[];journal=[]
        result=ensure_running(bundle,health_reader=lambda config:next(replies),process_owner=lambda pid,b:True,
            launch=lambda b:calls.append(b) or {'pid':43},mutex=lambda b:nullcontext(),sleeper=lambda _:None,
            read_pending=lambda:None,write_pending=journal.append)
        self.assertEqual(result['status'],'started');self.assertEqual(len(calls),1);self.assertEqual(journal[0]['status'],'launching');self.assertEqual(journal[1]['pid'],43)

    def test_timeout_and_second_start_do_not_duplicate_pending_process(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        journal=[];calls=[]
        for _ in range(2):
            times=iter([0,0,2])
            with self.assertRaisesRegex(StartupBlocked,'uncertain'):
                ensure_running(bundle,health_reader=lambda c:None,process_owner=lambda pid,b:True,
                    launch=lambda b:calls.append(1) or {'pid':43},mutex=lambda b:nullcontext(),
                    clock=lambda:next(times),sleeper=lambda _:None,timeout=1,
                    read_pending=lambda:journal[-1] if journal else None,write_pending=journal.append)
        self.assertEqual(calls,[1]);self.assertEqual(journal[-1]['pid'],43)

    def test_crash_between_launch_and_pid_record_is_not_replayed(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        with self.assertRaisesRegex(StartupBlocked,'Unreconciled'):
            ensure_running(bundle,health_reader=lambda c:None,process_owner=lambda pid,b:True,
                launch=lambda b:self.fail('must not launch'),mutex=lambda b:nullcontext(),
                read_pending=lambda:{'pid':None,'status':'launching','config_sha256':'a'*64})

    def test_pid_journal_failure_retains_reservation_and_blocks_second_launch(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}};journal=[];launches=[]
        def write_pending(value):
            if value['pid'] is not None:raise OSError('synthetic journal failure')
            journal.append(value)
        with self.assertRaises(OSError):
            ensure_running(bundle,health_reader=lambda c:None,process_owner=lambda pid,b:True,
                launch=lambda b:launches.append(1) or {'pid':43},mutex=lambda b:nullcontext(),write_pending=write_pending)
        with self.assertRaisesRegex(StartupBlocked,'Unreconciled'):
            ensure_running(bundle,health_reader=lambda c:None,process_owner=lambda pid,b:True,
                launch=lambda b:launches.append(2),mutex=lambda b:nullcontext(),read_pending=lambda:journal[-1])
        self.assertEqual(launches,[1]);self.assertIsNone(journal[-1]['pid'])

    def test_failed_reservation_prevents_process_creation(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        def fail(value):raise OSError('synthetic durable storage failure')
        with self.assertRaises(OSError):
            ensure_running(bundle,health_reader=lambda c:None,process_owner=lambda pid,b:True,
                launch=lambda b:self.fail('must not launch'),mutex=lambda b:nullcontext(),write_pending=fail)

    def test_foreign_healthy_service_is_not_replaced(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        health={'mode':'verified_multichat_continuity_service','architecture_version':4,'pid':43,'port':12345,'config_sha256':'b'*64}
        with self.assertRaisesRegex(StartupBlocked,'differs'):
            ensure_running(bundle,health_reader=lambda c:health,process_owner=lambda pid,b:True,
                launch=lambda b:self.fail('must not launch'),mutex=lambda b:nullcontext())

    def test_health_redirect_is_rejected_without_following(self):
        from http.server import BaseHTTPRequestHandler,HTTPServer
        import threading
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path);self.send_response(302);self.send_header('Location','http://example.invalid/');self.end_headers()
            def log_message(self,*a):pass
        server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.handle_request);thread.start()
        try:
            with self.assertRaisesRegex(StartupBlocked,'redirects'):
                read_health({'host':'127.0.0.1','port':server.server_port,'capability':'synthetic'})
        finally:thread.join(2);server.server_close()
        self.assertEqual(seen,['/synthetic/health'])

    def test_pending_owned_process_is_waited_for_without_duplicate_or_retry(self):
        bundle={'config':{'port':12345},'pins':{'config_sha256':'a'*64}}
        health={'mode':'verified_multichat_continuity_service','architecture_version':4,'pid':43,'port':12345,**bundle['pins']}
        replies=iter([None,health]);calls=[]
        result=ensure_running(bundle,health_reader=lambda config:next(replies),process_owner=lambda pid,b:True,
            launch=lambda b:calls.append(b),mutex=lambda b:nullcontext(),sleeper=lambda _:None,
            read_pending=lambda:{'pid':43,'config_sha256':'a'*64},write_pending=lambda x:None)
        self.assertEqual(result['status'],'recovered_pending_start');self.assertEqual(calls,[])

    def test_modified_service_is_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);script=root/'evil.py';marker=root/'executed';script.write_text('raise AssertionError("must not import")')
            config=root/'config.json';config.write_text('{}');pythonw=root/'pythonw.exe';pythonw.write_bytes(b'synthetic')
            pin=lambda p:{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            manifest={'version':1,'helper':pin(Path(service_startup.__file__).resolve()),'service':pin(script),'config':pin(config),'pythonw':pin(pythonw)}
            manifest['service']['sha256']='0'*64;p=root/'launch.json';raw=json.dumps(manifest).encode();p.write_bytes(raw)
            with self.assertRaisesRegex(StartupBlocked,'hash'):load_bundle(p,hashlib.sha256(raw).hexdigest())
            self.assertFalse(marker.exists())

if __name__=='__main__':unittest.main()
