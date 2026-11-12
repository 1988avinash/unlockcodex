from contextlib import nullcontext
import hashlib,json
from pathlib import Path
import tempfile
import unittest
from startup_registration import install,uninstall,RegistrationBlocked,delete_exact_file
import os

class FakeStartup:
    def __init__(self):self.value=None;self.writes=0
    def read(self,path):return self.value
    def create_exclusive(self,path,data):
        if self.value is not None:raise FileExistsError(path)
        self.value=data;self.writes+=1
    def remove_exact(self,path,expected):
        if self.value!=expected:raise RegistrationBlocked('Changed entry')
        self.value=None;self.writes+=1
    def lock(self):return nullcontext()

class RegistrationBoundary(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Win32 file ownership test')
    def test_actual_windows_compare_delete_preserves_changed_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'owned-synthetic.lnk';path.write_bytes(b'owned')
            with self.assertRaisesRegex(RegistrationBlocked,'changed'):delete_exact_file(path,b'old')
            self.assertEqual(path.read_bytes(),b'owned')
            delete_exact_file(path,b'owned');self.assertFalse(path.exists())

    def test_install_is_idempotent_and_uninstall_refuses_changed_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);link=root/'staged.lnk';link.write_bytes(b'synthetic-owned-shortcut')
            plan={'purpose':'codex_continuity_startup','version':1,'target':str(root/'startup.lnk'),'staged_link':str(link),
                  'staged_sha256':hashlib.sha256(link.read_bytes()).hexdigest(),'ownership_receipt':str(root/'ownership.json')}
            native=FakeStartup();verify=lambda p:None
            self.assertEqual(install(plan,backend=native,verify=verify)['status'],'installed')
            self.assertEqual(install(plan,backend=native,verify=verify)['status'],'already_installed');self.assertEqual(native.writes,1)
            native.value=b'unrelated replacement'
            with self.assertRaises(RegistrationBlocked):uninstall(plan,backend=native,verify=verify)
            self.assertEqual(native.value,b'unrelated replacement')
            native.value=link.read_bytes();self.assertEqual(uninstall(plan,backend=native,verify=verify)['status'],'removed')

if __name__=='__main__':unittest.main()
