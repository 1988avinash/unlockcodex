from contextlib import closing
import copy
import hashlib
import os
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import production_service_v4 as service


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.package=self.root/'package';self.package.mkdir()
        for name in service.REQUIRED:
            path=self.package/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic pinned code')
        self.external=self.root/'credential_adapter.py';self.external.write_text('# synthetic code')
        self.native=self.root/'native.exe';self.native.write_bytes(b'synthetic native executable')
        self.python=self.root/'python.exe';self.python.write_bytes(b'synthetic Python executable')
        self.patch=patch.object(service,'EXTERNAL_REQUIRED',{str(self.external)});self.patch.start();self.addCleanup(self.patch.stop)
        self.config={'version':4,'package_root':str(self.package),'state_dir':str(self.root/'state'),
            'host':'127.0.0.1','port':58723,'account_id':'synthetic-account','capability':'synthetic-capability-'+'a'*32,
            'openai_models':['gpt-test'],'recovery_aliases':{},'cold_routes':[],'registered_chats':[],
            'enroll_new_chats':False,'allow_incomplete_visible_rebase':False,'allow_paid_compaction':False,'allow_native_review':False,'allow_paid_lite_compaction':False,
            'native_runtime':service._pin(self.native),'python_runtime':service._pin(self.python),
            'files':{name:service.sha256(self.package/name) for name in service.package_files(self.package)},
            'external_files':{str(self.external):service.sha256(self.external)}}
        proof=self.root/'qualified-route.json'
        evidence={'qualified_for_native':True,'provider_id':'minimax','actual_model':'MiniMax-M3.1-Flash',
            'protocol_profile':'responses-full','auto_fallback_qualified':True}
        proof.write_text(json.dumps(evidence),encoding='utf-8')
        route=dict(evidence,route_id='minimax',native_alias='tp-minimax-m31',display_name='MiniMax Token Plan',
            endpoint='https://api.minimax.io/v1/responses',supports_native_tools=True,qualification_ref=str(proof))
        route.pop('qualified_for_native')
        self.config.update(route_specs=[route],auto_fallback_id='minimax',qualification_files={str(proof):service.sha256(proof)})
    def verify(self,config=None):return service.verify_config(config or self.config,test_artifacts=True)
    def test_plan_qualification_hash_model_and_endpoint_are_bound(self):
        for field,value in [('endpoint','http://other.example/responses'),('endpoint','https://user:pw@other.example/responses'),('provider_id','openai'),
                ('actual_model','unqualified-model'),('protocol_profile','responses-lite')]:
            candidate=copy.deepcopy(self.config);candidate['route_specs'][0][field]=value
            with self.assertRaises(service.ServiceBlocked):self.verify(candidate)
        proof=Path(self.config['route_specs'][0]['qualification_ref']);proof.write_text('{}')
        with self.assertRaisesRegex(service.ServiceBlocked,'evidence changed'):self.verify()
    def test_unreviewed_route_cannot_be_enabled_by_config_only(self):
        proof=Path(self.config['route_specs'][0]['qualification_ref']);data=json.loads(proof.read_text())
        data['qualified_for_native']=False;proof.write_text(json.dumps(data))
        self.config['qualification_files'][str(proof)]=service.sha256(proof)
        with self.assertRaisesRegex(service.ServiceBlocked,'does not cover'):self.verify()
    def test_auto_fallback_requires_explicit_route_qualification(self):
        candidate=copy.deepcopy(self.config);candidate['auto_fallback_id']='unknown'
        with self.assertRaisesRegex(service.ServiceBlocked,'automatic fallback'):self.verify(candidate)
    def test_valid_manifest_has_no_state_or_startup_side_effects(self):
        pins=self.verify()
        self.assertEqual(pins['native_runtime_sha256'],service.sha256(self.native))
        self.assertFalse((self.root/'state').exists())
    def test_modified_code_and_added_shadow_module_fail(self):
        path=self.package/'continuity-v3/native_quota.py'
        path.write_text('modified')
        with self.assertRaisesRegex(service.ServiceBlocked,'hash mismatch'):self.verify()
        path.write_bytes(b'synthetic pinned code')
        (self.package/'continuity-v3/sqlite3.py').write_text('unapproved shadow module')
        with self.assertRaisesRegex(service.ServiceBlocked,'membership'):self.verify()
    def test_shadow_package_directory_fails_even_without_manifest_entry(self):
        shadow=self.package/'continuity-v2/httpx';shadow.mkdir();(shadow/'__init__.py').write_text('unapproved')
        with self.assertRaisesRegex(service.ServiceBlocked,'flat'):self.verify()
    def test_sourceless_bytecode_cannot_shadow_pinned_dependency(self):
        (self.package/'continuity-v3/httpx.pyc').write_bytes(b'unapproved sourceless code')
        with self.assertRaisesRegex(service.ServiceBlocked,'sourceless'):self.verify()
    def test_existing_credential_import_root_cannot_shadow_library(self):
        workflow=self.root/'workflow';workflow.mkdir()
        libraries=self.root/'libraries';libraries.mkdir();(libraries/'httpx').mkdir()
        (workflow/'httpx.py').write_text('unapproved')
        with self.assertRaisesRegex(service.ServiceBlocked,'shadows'):
            service.reject_credential_shadows(workflow,libraries)
    def test_nonpackage_venv_directory_is_not_stdlib_shadow(self):
        workflow=self.root/'workflow';workflow.mkdir();(workflow/'venv').mkdir();(workflow/'__pycache__').mkdir()
        libraries=self.root/'libraries';libraries.mkdir();(libraries/'__pycache__').mkdir()
        service.reject_credential_shadows(workflow,libraries)
        (workflow/'venv/__init__.py').write_text('actual shadow package')
        with self.assertRaisesRegex(service.ServiceBlocked,'shadows'):service.reject_credential_shadows(workflow,libraries)
    def test_missing_dependency_external_change_and_native_change_fail(self):
        config=copy.deepcopy(self.config);config['files'].pop(next(iter(config['files'])))
        with self.assertRaises(service.ServiceBlocked):self.verify(config)
        self.external.write_text('modified credential code')
        with self.assertRaisesRegex(service.ServiceBlocked,'External dependency'):self.verify()
        self.external.write_text('# synthetic code')
        self.native.write_bytes(b'changed native')
        with self.assertRaisesRegex(service.ServiceBlocked,'executable hash'):self.verify()
    def test_public_port_and_credential_config_fields_are_rejected(self):
        for key,value in [('host','0.0.0.0'),('port',0),('access_token','synthetic-never-store'),('capability','short')]:
            config=copy.deepcopy(self.config);config[key]=value
            with self.assertRaises(service.ServiceBlocked):self.verify(config)
    def test_nonbundled_native_requires_test_opt_in(self):
        with patch.object(service,'dependency_files',return_value={str(self.external)}):
            with self.assertRaisesRegex(service.ServiceBlocked,'bundled native'):service.verify_config(self.config)
    def test_approved_config_hash_and_duplicate_keys(self):
        path=self.root/'config.json';raw=service.encoded(self.config);path.write_bytes(raw)
        self.assertEqual(service.read_config(path,hashlib.sha256(raw).hexdigest()),self.config)
        with self.assertRaises(service.ServiceBlocked):service.read_config(path,'0'*64)
        raw=b'{"version":1,"version":1}';path.write_bytes(raw)
        with self.assertRaisesRegex(service.ServiceBlocked,'Duplicate'):
            service.read_config(path,hashlib.sha256(raw).hexdigest())
    def test_cold_seed_cannot_enroll_another_account(self):
        config=copy.deepcopy(self.config)
        config['registered_chats']=['11111111-1111-4111-8111-111111111111']
        config['cold_routes']=[{'account_id':'other-account','thread_id':config['registered_chats'][0],'route':{}}]
        with self.assertRaisesRegex(service.ServiceBlocked,'Cold routes'):self.verify(config)
    def test_hidden_launch_is_isolated_and_does_not_claim_health(self):
        path=self.root/'config.json';path.write_bytes(service.encoded(self.config));digest=service.sha256(path)
        with patch.object(service,'verify_config',return_value={}), patch.object(service,'python_base_pin',return_value={}), \
                patch.object(service,'private_state',return_value=self.root), \
                patch.object(service,'verified_service_process',return_value={'wrapper':{'pid':123}}), \
                patch.object(service.subprocess,'Popen',return_value=SimpleNamespace(pid=123)) as popen:
            result=service.launch_hidden(path,digest)
        self.assertFalse(result['health_verified'])
        args,kwargs=popen.call_args
        self.assertEqual(args[0][1:4],['-I','-S','-B'])
        self.assertTrue(kwargs['creationflags'] & service.subprocess.CREATE_NO_WINDOW)
        self.assertTrue(kwargs['creationflags'] & service.subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertNotIn('OPENAI_API_KEY',kwargs['env'])
        self.assertNotIn('CODEX_HOME',kwargs['env'])
    def test_reused_state_repairs_every_owned_descendant(self):
        state=self.root/'private-state';who=SimpleNamespace(stdout='"synthetic-user","S-1-5-21-123"\n')
        with patch.object(service.subprocess,'run',return_value=who),patch.object(service,'set_private_acl') as apply:
            service.private_state(state,'account-a')
            nested=state/'nested';nested.mkdir();child=nested/'prior-explicit-grants.txt';child.write_text('synthetic')
            apply.reset_mock();service.private_state(state,'account-a')
            actual={call.args[0] for call in apply.call_args_list}
            self.assertEqual(actual,{state,nested,child,state/'continuity-state-owner.json'})
            apply.reset_mock()
            with self.assertRaisesRegex(service.ServiceBlocked,'not owned'):service.private_state(state,'other-account')
            apply.assert_not_called()
    def test_unowned_nonempty_state_is_not_modified(self):
        state=self.root/'unowned';state.mkdir();(state/'keep.txt').write_text('unchanged')
        who=SimpleNamespace(stdout='"synthetic-user","S-1-5-21-123"\n')
        with patch.object(service.subprocess,'run',return_value=who),patch.object(service,'set_private_acl') as apply:
            with self.assertRaisesRegex(service.ServiceBlocked,'not owned'):service.private_state(state,'account-a')
            apply.assert_not_called()
        self.assertEqual((state/'keep.txt').read_text(),'unchanged')
    def test_state_hardlinks_are_rejected_before_acl_changes(self):
        state=self.root/'hardlink-state';who=SimpleNamespace(stdout='"synthetic-user","S-1-5-21-123"\n')
        with patch.object(service.subprocess,'run',return_value=who),patch.object(service,'set_private_acl') as apply:
            service.private_state(state,'account-a')
            outside=self.root/'outside.txt';outside.write_text('unmodified')
            os.link(outside,state/'linked.txt');apply.reset_mock()
            with self.assertRaisesRegex(service.ServiceBlocked,'Hard-linked'):service.private_state(state,'account-a')
            apply.assert_not_called()
    @unittest.skipUnless(os.name=='nt','Windows ACL regression')
    def test_actual_windows_protected_child_acl_is_replaced(self):
        import ctypes
        from ctypes import wintypes
        state=self.root/'real-acl-test';service.private_state(state,'synthetic-acl-account')
        child=state/'child.txt';child.write_text('synthetic data only')
        # Deliberately give Everyone an explicit grant on this owned test file.
        sid='S-1-1-0';service.set_private_acl(child,sid)
        service.private_state(state,'synthetic-acl-account')
        advapi=ctypes.WinDLL('advapi32',use_last_error=True);kernel=ctypes.WinDLL('kernel32')
        get=advapi.GetFileSecurityW;get.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
        needed=wintypes.DWORD();get(str(child),4,None,0,ctypes.byref(needed))
        buffer=ctypes.create_string_buffer(needed.value)
        self.assertTrue(get(str(child),4,buffer,needed.value,ctypes.byref(needed)))
        output=ctypes.c_void_p();convert=advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes=[ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
        self.assertTrue(convert(buffer,1,4,ctypes.byref(output),None))
        try:
            sddl=ctypes.wstring_at(output)
            self.assertNotIn(';;;WD)',sddl)
            self.assertNotIn('S-1-1-0',sddl)
            self.assertEqual(sddl.count('(A;'),1)
            self.assertIn('D:P',sddl)
        finally:
            kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree(output)


class SyntheticUpstream(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(body)
        raw=json.dumps({'id':'synthetic-response','model':body['model'],'status':'completed','output':[
            {'id':'synthetic-message','type':'message','role':'assistant','content':[{'type':'output_text','text':'Accepted'}]}]}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)


class ColdAliasHTTPTests(unittest.TestCase):
    def test_cold_alias_rejects_before_reservation_then_openai_succeeds(self):
        from gateway_v4 import create_server
        from test_additional_handoffs import registry
        from production_adapters_v4 import ProductionAdapters
        from test_production_adapters_v4 import Reader
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);account='synthetic-account';chat='11111111-1111-4111-8111-111111111111'
            cap='synthetic-service-capability-'+'b'*32
            upstream=ThreadingHTTPServer(('127.0.0.1',0),SyntheticUpstream);upstream.calls=[]
            thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
            class HealthyReader(Reader):
                def read(self,token,account):
                    result=super().read(token,account)
                    result['rateLimitsByLimitId']['codex']['primary']['usedPercent']=20
                    return result
            reader=HealthyReader()
            factory=ProductionAdapters(state_root=root,openai_models={'gpt-test'},native_reader=reader,
                clock_epoch='synthetic-boot',clock=lambda:100.,notifier_factory=lambda path:lambda event:'synthetic-notice',
                executor_factory=lambda **kwargs:(_ for _ in ()).throw(AssertionError('No checkpoint expected')))
            gateway=create_server(route_registry=registry(),credential_reader=lambda spec:'synthetic-key',state_dir=root/'gateway',capability=cap,allowed_accounts={account},registered_chats={(account,chat)},
                clock_epoch='synthetic-boot',clock=lambda:100.,callback_factory=factory,openai_models={'gpt-test'},
                allow_test_upstreams=True,test_upstreams={'openai':f'http://127.0.0.1:{upstream.server_port}/responses'},timeout=5)
            service.install_health(gateway,{'mode':'synthetic_verified_service','manifest_sha256':'c'*64})
            worker=threading.Thread(target=gateway.serve_forever,daemon=True);worker.start()
            def send(model,turn):
                headers={'thread-id':chat,'session-id':chat,'ChatGPT-Account-ID':account,'Authorization':'Bearer synthetic-token',
                    'x-codex-turn-metadata':json.dumps({'thread_id':chat,'session_id':chat,'turn_id':turn})}
                body={'model':model,'input':[{'type':'message','role':'user','content':[{'type':'input_text','text':'Synthetic request'}]}]}
                connection=http.client.HTTPConnection(*gateway.server_address,timeout=5)
                try:
                    connection.request('POST','/'+cap+'/responses',json.dumps(body),headers)
                    response=connection.getresponse();return response.status,response.read()
                finally:connection.close()
            try:
                connection=http.client.HTTPConnection(*gateway.server_address,timeout=5)
                connection.request('GET','/'+cap+'/health');response=connection.getresponse()
                self.assertEqual(response.status,200)
                self.assertEqual(json.loads(response.read())['manifest_sha256'],'c'*64)
                connection.close()
                status,body=send('tp-minimax-m31','cold-turn')
                self.assertEqual(status,409)
                self.assertIn(b'Outgoing model is not verified',body)
                self.assertEqual(upstream.calls,[]);self.assertEqual(reader.calls,[])
                scope=gateway.registry.get(account,chat)
                for filename,table in [('quota.sqlite3','attempts'),('evidence.sqlite3','requests')]:
                    with closing(sqlite3.connect(scope.state_dir/filename)) as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
                self.assertEqual(send('gpt-test','valid-turn')[0],200)
                self.assertEqual(len(upstream.calls),1)
                with self.assertRaises(OSError):
                    create_server(route_registry=registry(),credential_reader=lambda spec:'synthetic-key',state_dir=root/'second-instance',capability=cap,allowed_accounts={account},registered_chats={(account,chat)},
                        clock_epoch='synthetic-boot',clock=lambda:100.,callback_factory=factory,openai_models={'gpt-test'},port=gateway.server_port)
            finally:
                gateway.shutdown();gateway.server_close();worker.join()
                upstream.shutdown();upstream.server_close();thread.join()


if __name__=='__main__':unittest.main()
