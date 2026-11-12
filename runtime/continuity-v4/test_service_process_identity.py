import copy,hashlib,tempfile,unittest
import os,subprocess,sys
from pathlib import Path
from unittest.mock import patch,MagicMock
import production_service_v4 as service

class RuntimePins(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Native Windows terminated process handle')
    def test_exited_process_with_held_original_handle_is_proven_dead(self):
        import process_identity
        child=subprocess.Popen([sys.executable,'-I','-S','-B','-c','pass'],creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            birth=process_identity.creation_filetime(process_identity.kernel(),int(child._handle))
            child.wait(timeout=10)
            state=service.original_process_state({'pid':child.pid,'creation_filetime':birth},process_identity)
            self.assertEqual(state['state'],'same_birth_exited')
        finally:
            if child.poll() is None:child.terminate();child.wait(timeout=10)
    def test_missing_child_with_live_wrapper_is_not_clean_shutdown(self):
        class Missing(ValueError):pass
        identity=MagicMock();identity.IdentityBlocked=Missing
        identity.process_identity.return_value={'user_sid':'S-1-test','session_id':1}
        with patch.object(service,'process_module',return_value=identity),patch.object(service,'python_base_pin',return_value={}),\
             patch.object(service,'original_process_state',side_effect=[{'state':'same_birth_alive'},{'state':'absent'}]):
            with self.assertRaisesRegex(service.ServiceBlocked,'partially alive'):
                service.verified_service_process(11,{'python_runtime':{}},'config','a'*64,
                    expected={'wrapper':{'pid':10,'user_sid':'S-1-test'},'child':{'pid':11,'user_sid':'S-1-test'}})

    def test_native_birth_proves_absent_reused_or_live_without_adopting_pid(self):
        record={'pid':123,'creation_filetime':100};identity=MagicMock();kernel=identity.kernel.return_value
        kernel.OpenProcess.return_value=0
        with patch.object(service.ctypes,'get_last_error',return_value=87):self.assertEqual(service.original_process_state(record,identity)['state'],'absent')
        with patch.object(service.ctypes,'get_last_error',return_value=5):
            with self.assertRaisesRegex(service.ServiceBlocked,'cannot be established'):service.original_process_state(record,identity)
        kernel.OpenProcess.return_value=456
        kernel.WaitForSingleObject.return_value=258
        for birth,state in [(100,'same_birth_alive'),(200,'different_birth')]:
            identity.creation_filetime.return_value=birth
            self.assertEqual(service.original_process_state(record,identity)['state'],state)
        identity.process_identity.assert_not_called()

    def test_dead_healthy_pair_can_retire_after_new_interactive_logon(self):
        expected={'wrapper':{'pid':10,'user_sid':'S-1-test','session_id':1},'child':{'pid':11,'user_sid':'S-1-test','session_id':1}}
        identity=MagicMock();identity.process_identity.return_value={'user_sid':'S-1-test','session_id':2}
        with patch.object(service,'process_module',return_value=identity),patch.object(service,'python_base_pin',return_value={}),\
             patch.object(service,'original_process_state',side_effect=[{'state':'absent'},{'state':'different_birth'}]):
            result=service.verified_service_process(11,{'python_runtime':{}},'config','a'*64,expected=expected)
        self.assertTrue(result['original_pair_gone']);self.assertEqual(result['recorded_identity'],expected)
        identity.process_identity.assert_called_once() # only current caller, no adoption of reused PID

    def test_selector_and_base_are_pinned_before_runtime_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);venv=root/'venv';scripts=venv/'Scripts';scripts.mkdir(parents=True)
            home=root/'base';home.mkdir()
            for name in ('python.exe','pythonw.exe','python311.dll','python3.dll','vcruntime140.dll','vcruntime140_1.dll'):(home/name).write_bytes(name.encode())
            wrapper=scripts/'python.exe';wrapper.write_bytes(b'wrapper')
            selector=venv/'pyvenv.cfg';selector.write_text(f'home = {home}\nexecutable = {home / "python.exe"}\nversion = 3.11.9\n')
            layout=service.python_runtime_layout(wrapper)
            config={'python_runtime':service._pin(wrapper),'external_files':{str(p):service.sha256(p) for p in layout['files']}}
            self.assertEqual(service.python_base_pin(config)['path'],str(home/'python.exe'))
            for target in (selector,home/'python.exe',home/'pythonw.exe',home/'python311.dll'):
                original=target.read_bytes();target.write_bytes(original+b' changed')
                with self.assertRaises((service.ServiceBlocked,KeyError)):service.python_base_pin(config)
                target.write_bytes(original)

    def test_changed_runtime_blocks_before_any_launch(self):
        with patch.object(service,'read_config',return_value={}),patch.object(service,'verify_config'),\
             patch.object(service,'python_base_pin',side_effect=service.ServiceBlocked('changed selector')),\
             patch.object(service.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(service.ServiceBlocked,'changed selector'):service.launch_hidden('unused','a'*64)
            launch.assert_not_called()

if __name__=='__main__':unittest.main()
