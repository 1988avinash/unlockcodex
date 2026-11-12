import copy,json,unittest
from types import SimpleNamespace
from unittest.mock import patch
import process_identity as p
from process_identity import validate_pair,validate_self_job,IdentityBlocked

class ProcessIdentity(unittest.TestCase):
    def setUp(self):
        self.wp={'path':'C:/qualified/venv/python.exe','sha256':'a'*64};self.bp={'path':'C:/qualified/base/python.exe','sha256':'b'*64}
        self.args=['-I','-S','-B','C:/owned/helper.py','--manifest','C:/owned/manifest.json'];self.sid='S-1-5-21-1001'
        self.wrapper={'pid':10,'parent':5,'executable':self.wp['path'],'argv':[self.wp['path']]+self.args,'created':'2026-11-10T01:00:00.0000000Z','user_sid':self.sid,'session_id':1}
        self.child={'pid':11,'parent':10,'executable':self.bp['path'],'argv':[self.bp['path']]+self.args,'created':'2026-11-10T01:00:00.1000000Z','user_sid':self.sid,'session_id':1}
        self.job={'pid':11,'parent_pid':10,'parent_in_any_job':False,'in_any_job':True,'immediate_job_limit_flags':0x3000,'immediate_job_assigned_count':1,'immediate_job_pids':[11]}
    def proof(self):return validate_pair(self.wrapper,self.child,self.wp,self.bp,self.args,self.sid,1)
    def test_exact_pair_and_known_child_job(self):self.assertEqual(validate_self_job(self.proof(),self.job)['child']['pid'],11)
    def test_nested_wrapper_job_rejected_even_with_correct_immediate_child_job(self):
        self.job['parent_in_any_job']=True
        with self.assertRaises(IdentityBlocked):validate_self_job(self.proof(),self.job)
    def test_wrong_pid_reused_birth_arguments_identity_or_session_rejected(self):
        for key,value in [('parent',9),('executable','C:/other/python.exe'),('argv',[self.bp['path']]+self.args+['--other']),('created','2026-11-09T00:00:00Z'),('user_sid','S-1-5-21-OTHER'),('session_id',2)]:
            with self.subTest(key=key):
                original=self.child;self.child=copy.deepcopy(original);self.child[key]=value
                with self.assertRaises(IdentityBlocked):self.proof()
                self.child=original
    def test_unknown_job_limits_membership_or_unbound_parent_rejected(self):
        for key,value in [('immediate_job_limit_flags',0x2000),('immediate_job_pids',[11,12]),('immediate_job_assigned_count',2),('parent_pid',99),('in_any_job',False)]:
            with self.subTest(key=key):
                job=copy.deepcopy(self.job);job[key]=value
                with self.assertRaises(IdentityBlocked):validate_self_job(self.proof(),job)
    def test_handle_birth_rejects_same_pid_reuse_before_job_query(self):
        closed=[]
        fake=SimpleNamespace(OpenProcess=lambda *a:1,CloseHandle=lambda h:closed.append(h),IsProcessInJob=lambda *a:self.fail('Wrong-birth handle must never be queried'))
        with patch.object(p,'kernel',return_value=fake),patch.object(p,'creation_filetime',return_value=222):
            with self.assertRaises(IdentityBlocked):p.process_in_any_job(10,expected_creation_filetime=111)
        self.assertEqual(closed,[1])
    def test_cim_snapshot_rejects_native_handle_birth_mismatch(self):
        value=dict(self.child,command='qualified command')
        fake=SimpleNamespace(OpenProcess=lambda *a:1,CloseHandle=lambda h:True)
        with patch.object(p.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps(value))),patch.object(p,'argv',return_value=self.child['argv']),patch.object(p,'kernel',return_value=fake),patch.object(p,'creation_filetime',return_value=0):
            with self.assertRaises(IdentityBlocked):p.process_identity(11)
    def test_missing_native_birth_or_job_query_fails_closed(self):
        with self.assertRaises(IdentityBlocked):p.creation_filetime(SimpleNamespace(GetProcessTimes=lambda *a:False),1)
        fake=SimpleNamespace(QueryInformationJobObject=lambda *a:False)
        with patch.object(p,'kernel',return_value=fake),patch.object(p,'process_in_any_job',return_value=True):
            with self.assertRaises(IdentityBlocked):p.current_job_snapshot(expected_self_creation=1,expected_parent_creation=2)

if __name__=='__main__':unittest.main()
