"""Independent final contract checks; isolated fixtures only."""
import copy
import json
from pathlib import Path
import unittest
import test_production_service_v4 as service_fixture
import test_desktop_configuration_v4 as desktop_fixture
from handoff_executor_v4 import prepare_body


class FinalIntegrationReview(unittest.TestCase):
    def test_lite_compaction_requires_paid_flag_and_exact_hashed_live_proof(self):
        fixture=service_fixture.ManifestTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        service=service_fixture.service;config=copy.deepcopy(fixture.config)
        spec=copy.deepcopy(config['route_specs'][0]);proof=fixture.root/'mimo-proof.json'
        spec.update(route_id='mimo',provider_id='mimo',actual_model='mimo-v2.6-flash',native_alias='tp-mimo-flash',
            endpoint='https://token-plan-sgp.xiaomimimo.com/v1/responses',protocol_profile='responses-lite',
            auto_fallback_qualified=False,qualification_ref=str(proof))
        value={k:spec[k] for k in ('provider_id','actual_model','protocol_profile','auto_fallback_qualified')}
        value['qualified_for_native']=True;proof.write_text(json.dumps(value))
        config['route_specs'].append(spec);config['qualification_files'][str(proof)]=service.sha256(proof)
        config['allow_paid_lite_compaction']=True
        with self.assertRaisesRegex(service.ServiceBlocked,'paid compaction gate'):fixture.verify(config)
        config['allow_paid_compaction']=True
        with self.assertRaisesRegex(service.ServiceBlocked,'Live lite compaction qualification'):fixture.verify(config)
        value['paid_lite_compaction_qualified']=True;proof.write_text(json.dumps(value))
        with self.assertRaisesRegex(service.ServiceBlocked,'evidence changed'):fixture.verify(config)
        config['qualification_files'][str(proof)]=service.sha256(proof)
        self.assertIn('config_sha256',fixture.verify(config))

    def test_new_plan_handoffs_force_low_effort_without_tools_or_opaque_replay(self):
        for provider in ('mimo','alibaba'):
            body=json.loads(prepare_body({'provider':provider,'model':'qualified-model','tools':[],
                'tool_choice':'none','prompt':'Source-bound handoff only','max_output_tokens':6000,
                'native_history':[{'type':'reasoning','encrypted_content':'untransferable'}]}))
            self.assertEqual(body['reasoning'],{'effort':'low'})
            self.assertEqual(body['tools'],[]);self.assertEqual(body['tool_choice'],'none')
            self.assertNotIn('untransferable',json.dumps(body));self.assertEqual(body['max_output_tokens'],6000)

    def test_catalog_native_capability_override_keeps_mandatory_review_and_original_config(self):
        fixture=desktop_fixture.ConfigurationTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        cache=json.loads(fixture.cache.read_text());cache['models'][0]['supports_reasoning_effort_updates']=True
        fixture.cache.write_text(json.dumps(cache))
        config=json.loads(fixture.service.read_text());proof=Path(config['route_specs'][0]['qualification_ref'])
        value=json.loads(proof.read_text());value['catalog_metadata']['supports_reasoning_effort_updates']=False
        proof.write_text(json.dumps(value));config['qualification_files'][str(proof)]=desktop_fixture.hashlib.sha256(proof.read_bytes()).hexdigest()
        fixture.service.write_text(json.dumps(config));directory=fixture.root/'successor-stage'
        desktop_fixture.stage(config_path=fixture.config,model_cache_path=fixture.cache,service_config_path=fixture.service,directory=directory)
        catalog=json.loads((directory/'catalog.json').read_text())
        plan=next(item for item in catalog['models'] if item['slug']=='tp-mimo-flash')
        self.assertFalse(plan['supports_reasoning_effort_updates']);self.assertTrue(plan['node_repl_auto_review_required'])
        self.assertEqual(fixture.config.read_bytes(),fixture.original)


if __name__=='__main__':unittest.main()
