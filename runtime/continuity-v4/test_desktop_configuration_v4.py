import json,tempfile,unittest,hashlib
from pathlib import Path
from desktop_configuration_v4 import stage,apply,rollback


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.config=self.root/'config.toml';self.original=b'model = "gpt-test"\n# Existing hook\n[hooks]\npath = "keep-me"\n'
        self.config.write_bytes(self.original)
        self.cache=self.root/'models.json';self.cache.write_text(json.dumps({'models':[{'slug':'gpt-test','base_instructions':'Keep original instructions','tool_mode':'code_mode_only','node_repl_auto_review_required':True,'use_responses_lite':True}, {'slug':'codex-auto-review','use_responses_lite':True,'node_repl_auto_review_required':False}]}))
        self.service=self.root/'service.json';self.service.write_text(json.dumps({'host':'127.0.0.1','port':12345,'capability':'synthetic-capability','openai_models':['gpt-test'],'recovery_aliases':{'return-test':'gpt-test'}}))
        proof=self.root/'qualified.json';proof.write_text(json.dumps({'catalog_metadata':{'context_window':131072,'max_context_window':131072,'input_modalities':['text']}}))
        value=json.loads(self.service.read_text());value['route_specs']=[{'native_alias':'tp-mimo-flash','display_name':'MiMo Token Plan','protocol_profile':'responses-lite','qualification_ref':str(proof)}]
        value['qualification_files']={str(proof):hashlib.sha256(proof.read_bytes()).hexdigest()};self.service.write_text(json.dumps(value))
        self.directory=self.root/'activation'
        self.manifest=stage(config_path=self.config,model_cache_path=self.cache,service_config_path=self.service,directory=self.directory)
        self.health={'mode':'verified_multichat_continuity_service','config_sha256':self.manifest['service_config_sha256'],'pid':123}
    def test_stage_is_inert_apply_preserves_settings_and_rollback_exact_bytes(self):
        self.assertEqual(self.config.read_bytes(),self.original)
        catalog=json.loads((self.directory/'catalog.json').read_text(encoding='utf-8'))
        self.assertEqual(catalog['models'][0]['tool_mode'],'code_mode_only')
        self.assertEqual(catalog['models'][0]['base_instructions'],'Keep original instructions')
        self.assertFalse(catalog['models'][0]['use_responses_lite'])
        self.assertEqual(catalog['models'][1],{'slug':'codex-auto-review','use_responses_lite':True,'node_repl_auto_review_required':False})
        plan=next(m for m in catalog['models'] if m['slug']=='tp-mimo-flash')
        self.assertTrue(plan['use_responses_lite']);self.assertTrue(plan['node_repl_auto_review_required'])
        self.assertEqual(plan['context_window'],131072);self.assertEqual(plan['input_modalities'],['text'])
        apply(self.directory,self.health);self.assertIn(b'path = "keep-me"',self.config.read_bytes())
        rollback(self.directory);self.assertEqual(self.config.read_bytes(),self.original)
    def test_concurrent_settings_change_and_wrong_service_block_activation(self):
        with self.assertRaises(ValueError):apply(self.directory,dict(self.health,config_sha256='incorrect'))
        self.config.write_bytes(self.original+b'# concurrent edit\n')
        with self.assertRaises(ValueError):apply(self.directory,self.health)
    def test_normal_and_return_gpt_caps_protect_automatic_fallback_but_leave_reviewer_intact(self):
        catalog=json.loads((self.directory/'catalog.json').read_text(encoding='utf-8'))
        for slug in ('gpt-test','return-test'):
            item=next(m for m in catalog['models'] if m['slug']==slug)
            self.assertEqual((item['context_window'],item['max_context_window']),(131072,131072))
            self.assertTrue(item['node_repl_auto_review_required'])
            self.assertEqual(item['tool_mode'],'code_mode_only')
        reviewer=next(m for m in catalog['models'] if m['slug']=='codex-auto-review')
        self.assertEqual(reviewer,{'slug':'codex-auto-review','use_responses_lite':True,'node_repl_auto_review_required':False})
    def test_changed_catalog_or_post_activation_edit_cannot_be_overwritten(self):
        apply(self.directory,self.health);self.config.write_bytes(self.config.read_bytes()+b'# concurrent\n')
        with self.assertRaises(ValueError):rollback(self.directory)


if __name__=='__main__':unittest.main()
