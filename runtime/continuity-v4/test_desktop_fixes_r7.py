"""Regression tests for r7 Codex Desktop 26.1103 fixes and expanded model catalog."""
from contextlib import closing
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

WORK = Path(__file__).resolve().parents[1]
for name in ('continuity-v4', 'continuity-v3', 'continuity-v2', 'desktop-route-v2/deps'):
    if str(WORK / name) not in sys.path:
        sys.path.insert(0, str(WORK / name))

import agy_bridge
import agy_executor
import desktop_configuration_v4 as desktop_cfg
import gateway_v4 as gw
from history_v4 import HistoryBlocked, translate_request
from production_adapters_v4 import AdapterBlocked, ProductionAdapters
from protocol_profiles_v4 import prepare_native
from route_policy import PlanPolicy
from route_registry import RouteRegistry, RouteSpec

CAP = 'r' * 48
ACCOUNT = 'acct-r7-test'
CHAT_1 = '42e01ee6-26db-7723-bbcf-885c40390aa8'
CHAT_2 = '0dac85db-91c2-7955-86d4-5fc2f3328648'
SAMPLE_CHILD = 'db06b1ac-6383-7071-90d3-d8bf419e7329'
TURN_1 = '551d016c-fc41-7315-9235-ca606d0617e8'
TURN_2 = 'ae2a48e2-84b3-7979-bfeb-8a68514ea5b0'


class MockUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        body = json.loads(raw)
        self.server.calls.append({'headers': dict(self.headers), 'body': body})
        for ti, tool in enumerate(body.get('tools') or []):
            desc = tool.get('description', '') if isinstance(tool, dict) else ''
            if isinstance(desc, str) and len(desc) > 1048576:
                err = json.dumps({'error': {'message': f"Invalid 'tools[{ti}].description': string too long.", 'code': 'string_above_max_length'}}).encode()
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
        for ii, item in enumerate(body.get('input') or [] if isinstance(body.get('input'), list) else []):
            if isinstance(item, dict) and item.get('type') == 'additional_tools':
                for ni, ns in enumerate(item.get('tools') or []):
                    for si, sub in enumerate((ns.get('tools') or []) if isinstance(ns, dict) else []):
                        sdesc = sub.get('description', '') if isinstance(sub, dict) else ''
                        if isinstance(sdesc, str) and len(sdesc) > 1048576:
                            err = json.dumps({'error': {'message': f"Invalid 'input[{ii}].tools[{ni}].tools[{si}].description': string too long.", 'code': 'string_above_max_length'}}).encode()
                            self.send_response(400)
                            self.send_header('Content-Type', 'application/json')
                            self.send_header('Content-Length', str(len(err)))
                            self.end_headers()
                            self.wfile.write(err)
                            return
        model = body['model']
        inputs = body.get('input', [])
        is_compact = isinstance(inputs, list) and bool(inputs) and inputs[-1].get('type') == 'compaction_trigger'
        if is_compact and self.server.compaction_v2_empty_completed:
            item = {'type': 'compaction', 'id': 'cmp_v2_1', 'encrypted_content': 'ENC_COMPACT_V2'}
            events = [
                ('response.created', {'type': 'response.created', 'response': {
                    'id': 'resp_cmp_1', 'model': model, 'status': 'in_progress', 'output': []}}),
                ('response.output_item.added', {'type': 'response.output_item.added', 'output_index': 0,
                    'item': {'type': 'compaction', 'id': 'cmp_v2_1', 'encrypted_content': ''}}),
                ('response.output_item.done', {'type': 'response.output_item.done', 'output_index': 0, 'item': item}),
                # Exact ResponsesCompactionV2 quirk: terminal response.completed has output=[]
                ('response.completed', {'type': 'response.completed', 'response': {
                    'id': 'resp_cmp_1', 'model': model, 'status': 'completed', 'output': []}}),
            ]
            payload = b''.join(
                f'event: {name}\ndata: {json.dumps(ev)}\n\n'.encode() for name, ev in events
            )
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if body.get('instructions', '').startswith('ECHO_LARGE_SSE:'):
            msg = {'type': 'message', 'id': f'msg_{len(self.server.calls)}', 'role': 'assistant',
                   'status': 'completed', 'content': [{'type': 'output_text', 'text': f'LARGE_SSE_OK:{model}'}]}
            echo = 'T' * (1500 * 1024)  # 1.5 MiB echoed instructions/tools inside response.created
            events = [
                ('response.created', {'type': 'response.created', 'response': {
                    'id': 'resp_large_1', 'model': model, 'status': 'in_progress',
                    'instructions': echo, 'output': []}}),
                ('response.output_item.done', {'type': 'response.output_item.done', 'output_index': 0, 'item': msg}),
                ('response.completed', {'type': 'response.completed', 'response': {
                    'id': 'resp_large_1', 'model': model, 'status': 'completed',
                    'instructions': echo, 'output': [msg]}}),
            ]
            payload = b''.join(
                f'event: {name}\ndata: {json.dumps(ev)}\n\n'.encode() for name, ev in events
            )
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        msg = {'type': 'message', 'id': f'msg_{len(self.server.calls)}', 'role': 'assistant',
               'status': 'completed', 'content': [{'type': 'output_text', 'text': f'OK:{model}'}]}
        resp = json.dumps({'id': f'resp_{len(self.server.calls)}', 'model': model,
                           'status': 'completed', 'output': [msg]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def start_mock_upstream(compaction_v2_empty_completed=False):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), MockUpstream)
    srv.calls = []
    srv.compaction_v2_empty_completed = compaction_v2_empty_completed
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv


def fake_quota_payload(used_percent=20):
    return {
        'accountId': ACCOUNT,
        'ordinaryUsageAllowed': used_percent < 100,
        'rateLimitsByLimitId': {
            'codex': {
                'limitId': 'codex',
                'spendControlReached': False,
                'rateLimitReachedType': None if used_percent < 100 else 'rate_limit_reached',
                'primary': {'usedPercent': used_percent, 'windowDurationMins': 10080},
                'credits': {'hasCredits': True, 'unlimited': False, 'approxLocalMessages': [10, 10]},
            }
        },
    }


class FakeQuotaReader:
    def __init__(self, used_percent=20):
        self.used_percent = used_percent

    def read(self, token, account):
        return fake_quota_payload(self.used_percent)


class FakeHandoffExecutor:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get('evidence_dir') is not None:
            Path(kwargs['evidence_dir']).mkdir(parents=True, exist_ok=False)

    def __call__(self, request):
        snapshot = json.loads(request['prompt'].split('HOST SNAPSHOT:\n', 1)[1].split('\nVISIBLE SOURCE', 1)[0])
        handoff = dict(
            binding={key: snapshot[key] for key in ('chat_id', 'last_turn_id', 'source_sha256')},
            objective='Continue the existing task',
            user_intent='Preserve the same chat',
            constraints=['Reconcile actual files'],
            holds=[],
            decisions=[],
            changed_files=[],
            completed_actions=[],
            active_jobs=[],
            uncertain_outcomes=[],
            do_not_replay=['Do not retry uncertain external requests'],
            next_steps=['Reconcile before continuing'],
            known_gaps=['Media was not inspected'],
        )
        return {
            'id': 'resp_handoff_1',
            'model': request['model'],
            'status': 'completed',
            'output': [{'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': json.dumps(handoff)}]}],
        }


class DesktopFixesR7Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.openai_up = start_mock_upstream(compaction_v2_empty_completed=True)
        self.minimax_up = start_mock_upstream()
        self.alibaba_up = start_mock_upstream()
        self.mimo_up = start_mock_upstream()
        self.now = 100.0
        self.wall = 1794340000
        self.quota_reader = FakeQuotaReader(used_percent=20)
        self.agy_cli_models = []

        def fake_agy_runner(folder, live=False, timeout=90, prepared=None, scope=None):
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=False)
            packet = agy_bridge.strict_json(prepared)
            cli_model = agy_bridge.resolve_cli_model(packet['source'])
            self.agy_cli_models.append(cli_model)
            events = [
                {'event': 'init', 'conversation_id': 'conv-1',
                 'init': {'model': cli_model, 'permission_mode': 'strict'}},
                {'event': 'result', 'result': {
                    'conversation_id': 'conv-1', 'status': 'SUCCESS', 'num_turns': 1,
                    'structured_output': {'kind': 'message', 'text': f'AGY_OK:{cli_model}', **({'calls': []} if packet['tools'] else {})}, 'usage': {}}},
            ]
            decoded = agy_bridge.decode_result(events, cli_model, packet['tools'])
            if cli_model != packet['model']:
                decoded['adapter_evidence']['cli_selected_model'] = cli_model
                decoded['model'] = packet['model']
            (folder / 'translated-response.json').write_text(json.dumps(decoded), encoding='utf-8')
            return {'status': 'completed', 'native_model_request_log_count': 1}

        def agy_route_exec(route, prepared, scope_context):
            scope = {k: scope_context[k] for k in ('account_id', 'thread_id', 'request_id')}
            resp = agy_executor.AgyExecutor(
                model=route.actual_model, evidence_dir=scope_context['evidence_dir'],
                timeout=30, runner=fake_agy_runner, scope=scope
            ).execute_prepared(prepared)
            resp['adapter_evidence']['scope'] = scope
            return agy_bridge.canonical(resp)

        def agy_handoff_factory(*, provider, model=None, evidence_dir=None, account_id=None, thread_id=None, **kw):
            if evidence_dir is not None:
                Path(evidence_dir).mkdir(parents=True, exist_ok=False)
            def run_handoff(request):
                snapshot = json.loads(request['prompt'].split('HOST SNAPSHOT:\n', 1)[1].split('\nVISIBLE SOURCE', 1)[0])
                handoff = dict(
                    binding={key: snapshot[key] for key in ('chat_id', 'last_turn_id', 'source_sha256')},
                    objective='Continue after AGY',
                    user_intent='Switch back to GPT',
                    constraints=[],
                    holds=[],
                    decisions=[],
                    changed_files=[],
                    completed_actions=[],
                    active_jobs=[],
                    uncertain_outcomes=[],
                    do_not_replay=['Do not retry uncertain external requests'],
                    next_steps=['Continue'],
                    known_gaps=[],
                )
                return {
                    'id': 'resp_agy_h_1',
                    'model': request['model'],
                    'status': 'completed',
                    'output': [{'type': 'message', 'role': 'assistant',
                                'content': [{'type': 'output_text', 'text': json.dumps(handoff)}]}],
                    'adapter_evidence': {'adapter_generated': True, 'useG1Credits': False},
                }
            return run_handoff

        self.routes = RouteRegistry([
            RouteSpec('minimax', 'minimax', 'MiniMax-M3.1-Flash', 'tp-minimax-m31',
                      'https://api.minimax.io/v1/responses', 'responses-full',
                      True, True, 'qual/minimax.json', display_name='MiniMax M3.1 Flash'),
            RouteSpec('minimax-m3', 'minimax', 'MiniMax-M3', 'tp-minimax-m3',
                      'https://api.minimax.io/v1/responses', 'responses-full',
                      True, False, 'qual/minimax-m3.json', display_name='MiniMax M3'),
            RouteSpec('mimo-pro', 'mimo', 'mimo-v2.6-pro', 'tp-mimo-pro',
                      'https://token-plan-sgp.xiaomimimo.com/v1/responses', 'responses-lite',
                      True, False, 'qual/mimo-pro.json', display_name='MiMo V2.6 Pro'),
            RouteSpec('deepseek-v41-flash', 'alibaba', 'deepseek-v4.1-flash', 'tp-deepseek-v41-flash',
                      'https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/responses', 'responses-full',
                      True, False, 'qual/deepseek-v41-flash.json', display_name='DeepSeek V4.1 Flash'),
            RouteSpec('gemini-argon', 'antigravity', 'gemini-4-argon-low', 'tp-gemini-argon-agy',
                      'agy:session-only-structured', 'agy-structured',
                      True, False, 'qual/gemini-argon.json', display_name='Gemini 4 Argon'),
        ], auto_fallback_id='minimax')

        self.adapters = ProductionAdapters(
            state_root=self.root / 'state',
            route_registry=self.routes,
            openai_models={'gpt-test', 'gpt-6.1-terra'},
            default_cold_openai_model='gpt-test',
            recovery_aliases={'chatgpt-return-sol': 'gpt-test'},
            native_reader=self.quota_reader,
            notifier_factory=lambda d: (lambda notice: 'delivered-r7'),
            executor_factory=lambda **kw: FakeHandoffExecutor(**kw),
            provider_executor_factories={'antigravity': agy_handoff_factory},
            provider_preflight_encoders={'antigravity': agy_bridge.prepare_body},
            credential_reader=lambda p: f'KEY_{p}',
            clock=lambda: self.now,
            wall_clock=lambda: self.wall,
            clock_epoch='boot-r7',
            cache_seconds=0,
        )
        self.server = gw.create_server(
            state_dir=self.root / 'state/gateway',
            capability=CAP,
            allowed_accounts={ACCOUNT},
            registered_chats={(ACCOUNT, CHAT_1)},
            clock_epoch='boot-r7',
            clock=lambda: self.now,
            callback_factory=self.adapters,
            route_registry=self.routes,
            credential_reader=lambda spec: f'KEY_{spec.provider_id}',
            protocol_preparers={
                'responses-full': prepare_native,
                'responses-lite': prepare_native,
                'agy-structured': agy_executor.protocol_preparer,
            },
            route_executors={'agy-structured': agy_route_exec},
            openai_models={'gpt-test', 'gpt-6.1-terra'},
            recovery_aliases={'chatgpt-return-sol': 'gpt-test'},
            enroll_new_chats=True,
            allow_incomplete_visible_rebase=True,
            allow_paid_compaction=True,
            allow_paid_lite_compaction=True,
            test_upstreams={
                'openai': f'http://127.0.0.1:{self.openai_up.server_port}/v1/responses',
                'minimax': f'http://127.0.0.1:{self.minimax_up.server_port}/v1/responses',
                'minimax-m3': f'http://127.0.0.1:{self.minimax_up.server_port}/v1/responses',
                'mimo-pro': f'http://127.0.0.1:{self.mimo_up.server_port}/v1/responses',
                'deepseek-v41-flash': f'http://127.0.0.1:{self.alibaba_up.server_port}/v1/responses',
            },
            allow_test_upstreams=True,
        )
        self.srv_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.srv_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for up in (self.openai_up, self.minimax_up, self.alibaba_up, self.mimo_up):
            up.shutdown()
            up.server_close()
        self.tmp.cleanup()

    def post(self, body, *, chat=CHAT_1, session=None, turn=TURN_1, extra_headers=None):
        session = session or chat
        meta = {'thread_id': chat, 'session_id': session, 'turn_id': turn}
        headers = {
            'thread-id': chat,
            'session-id': session,
            'ChatGPT-Account-ID': ACCOUNT,
            'Authorization': 'Bearer OAUTH_R7',
            'Content-Type': 'application/json',
            'x-client-request-id': chat,
            'x-codex-turn-metadata': json.dumps(meta),
        }
        if extra_headers:
            headers.update(extra_headers)
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        conn.request('POST', f'/{CAP}/responses', json.dumps(body), headers)
        resp = conn.getresponse()
        data = resp.read()
        hdrs = dict(resp.getheaders())
        conn.close()
        return resp.status, data, hdrs

    def test_companion_page_context_and_compaction_v2_empty_completed_output(self):
        companion_open = {
            'type': 'function_call_output',
            'id': 'fco_open_page_1',
            'name': 'codex_apps_open_page',
            'namespace': 'agent',
            'output': '{"page_id":null}',
        }
        companion_sel = {
            'type': 'function_call_output',
            'id': 'fco_sel_page_1',
            'name': 'codex_apps_selected_page_part_1_of_1',
            'namespace': 'agent',
            'output': '{"selected":[]}',
        }
        user_msg = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Compact with companion context'}]}

        # 1. Arbitrary orphan tool output without companion signature MUST still fail closed!
        bad_orphan = {'type': 'function_call_output', 'id': 'fco_bad', 'name': 'shell_command', 'output': 'bad'}
        with self.assertRaises(HistoryBlocked):
            translate_request(
                json.dumps({'input': [bad_orphan, user_msg]}).encode(),
                chat_id=CHAT_1, source_provider='openai', target_provider='minimax', disclosure_acknowledged=True,
            )

        # 2. Remote compaction with Companion Page items + ResponsesCompactionV2 output=[] SSE succeeds!
        compact_body = {
            'model': 'gpt-test',
            'tools': [],
            'input': [companion_open, companion_sel, user_msg, {'type': 'compaction_trigger'}],
        }
        status, raw, hdrs = self.post(compact_body, turn=TURN_1)
        self.assertEqual(status, 200, raw)
        self.assertIn(b'event: response.completed', raw)

        # 3. Subsequent turn switching to DeepSeek V4.1 Flash replays bounded compaction summary AND projects companion items cleanly!
        self.now += 1
        self.wall += 1
        next_body = {
            'model': 'tp-deepseek-v41-flash',
            'tools': [],
            'reasoning': {'effort': 'high'},
            'input': [
                {'type': 'compaction', 'id': 'cmp_v2_1', 'encrypted_content': 'ENC_COMPACT_V2'},
                companion_open,
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Follow up after compact'}]},
            ],
        }
        status2, raw2, hdrs2 = self.post(next_body, turn=TURN_2)
        self.assertEqual(status2, 200, raw2)
        self.assertEqual(hdrs2['X-Continuity-Route'], 'deepseek-v41-flash')
        sent_items = self.alibaba_up.calls[-1]['body']['input']
        self.assertFalse(any(i.get('type') == 'function_call_output' and i.get('call_id') is None for i in sent_items))
        self.assertTrue(any('codex_apps_open_page' in json.dumps(i) for i in sent_items))
        self.assertTrue(any('Bounded outgoing-model task handoff for compacted context' in json.dumps(i) for i in sent_items))

    def test_cold_unseeded_tp_selection_and_downshift_compaction(self):
        # Default ProductionAdapters without default_cold_openai_model still blocks cold switches (existing contract).
        strict_adapters = ProductionAdapters(
            state_root=self.root / 'strict_state',
            openai_models={'gpt-test'},
            native_reader=self.quota_reader,
            clock_epoch='boot-r7',
        )
        with self.assertRaises(AdapterBlocked):
            strict_adapters.validate_initial_route(ACCOUNT, CHAT_2, {'model': 'tp-minimax-m3'}, lambda: None)

        # Opt-in default_cold_openai_model allows brand-new chat CHAT_2 to start directly on tp-minimax-m3!
        body = {
            'model': 'tp-minimax-m3',
            'tools': [],
            'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Start directly on M3'}]}],
        }
        status, raw, hdrs = self.post(body, chat=CHAT_2, turn=TURN_1)
        self.assertEqual(status, 200, raw)
        self.assertEqual(hdrs['X-Continuity-Route'], 'minimax-m3')
        self.assertEqual(json.loads(raw)['model'], 'MiniMax-M3')

        # ModelDownshift compaction on another unseeded chat when body['model'] is tp-minimax-m3 also succeeds!
        chat_3 = 'c4de2da4-f6e2-71b6-872a-ee84a51d3ba8'
        compact_body = {
            'model': 'tp-minimax-m3',
            'tools': [],
            'input': [
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Long prompt'}]},
                {'type': 'compaction_trigger'},
            ],
        }
        status_c, raw_c, _ = self.post(compact_body, chat=chat_3, turn=TURN_2)
        self.assertEqual(status_c, 200, raw_c)

    def test_explicit_dropdown_switching_agy_effort_and_returning_to_openai_slug(self):
        # 1. Start on Gemini 4 Argon with effort='high' -> dispatches gemini-4-argon-high!
        b1 = {
            'model': 'tp-gemini-argon-agy',
            'tools': [],
            'reasoning': {'effort': 'high'},
            'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Use Argon high'}]}],
        }
        s1, r1, h1 = self.post(b1, turn='turn-a1')
        self.assertEqual(s1, 200, r1)
        self.assertEqual(h1['X-Continuity-Route'], 'gemini-argon')
        self.assertEqual(self.agy_cli_models[-1], 'gemini-4-argon-high')
        self.assertIn('AGY_OK:gemini-4-argon-high', r1.decode())

        # 2. Switch dropdown directly back to 'gpt-test' (ordinary OpenAI slug, not recovery alias) with healthy quota!
        self.now += 2
        self.wall += 2
        b2 = {
            'model': 'gpt-test',
            'tools': [],
            'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Back to GPT'}]}],
        }
        s2, r2, h2 = self.post(b2, turn='turn-a2')
        self.assertEqual(s2, 200, r2)
        self.assertEqual(h2['X-Continuity-Route'], 'openai')
        self.assertEqual(self.openai_up.calls[-1]['body']['model'], 'gpt-test')

        # 3. Auto-fallback when quota exhausted stays sticky if user does NOT change dropdown model!
        self.now += 2
        self.wall += 2
        self.quota_reader.used_percent = 95
        b3 = {
            'model': 'gpt-test',
            'tools': [],
            'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Low quota auto fallback'}]}],
        }
        s3, r3, h3 = self.post(b3, turn='turn-a3')
        self.assertEqual(s3, 200, r3)
        self.assertEqual(h3['X-Continuity-Route'], 'minimax')

        # Quota recovers to 20% used, user keeps 'gpt-test' unchanged -> stays sticky on minimax!
        self.now += 2
        self.wall += 2
        self.quota_reader.used_percent = 20
        b4 = {
            'model': 'gpt-test',
            'tools': [],
            'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Same model stays sticky'}]}],
        }
        s4, r4, h4 = self.post(b4, turn='turn-a4')
        self.assertEqual(s4, 200, r4)
        self.assertEqual(h4['X-Continuity-Route'], 'minimax')

    def test_thread_sample_ephemeral_lineage_with_null_parent_turn_id(self):
        # Ephemeral sampling thread spawned from CHAT_1 with parent_turn_id=None and request UUID in x-client-request-id
        sample_meta = {
            'thread_id': SAMPLE_CHILD,
            'session_id': CHAT_1,
            'parent_thread_id': CHAT_1,
            'turn_id': TURN_2,
            'parent_turn_id': None,
            'root_turn_id': None,
        }
        body = {
            'model': 'chatgpt-return-sol',
            'tools': [],
            'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Sample candidate'}]}],
        }
        status, raw, _ = self.post(
            body,
            chat=SAMPLE_CHILD,
            session=CHAT_1,
            turn=TURN_2,
            extra_headers={
                'x-codex-parent-thread-id': CHAT_1,
                'x-client-request-id': '94a1cbce-330e-4da3-baf0-fffdd8c559b8',
                'x-codex-turn-metadata': json.dumps(sample_meta),
            },
        )
        self.assertEqual(status, 200, raw)

    def test_large_sse_created_frame_over_1mib_echoing_tools_succeeds(self):
        body = {
            'model': 'tp-minimax-m3',
            'instructions': 'ECHO_LARGE_SSE:simulate 1.5 MiB tool catalog echo',
            'tools': [],
            'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Hello large SSE'}]}],
        }
        status, raw, hdrs = self.post(body, chat=CHAT_2, turn='turn-large-1')
        self.assertEqual(status, 200, raw)
        self.assertEqual(hdrs['X-Continuity-Route'], 'minimax-m3')
        self.assertIn(b'LARGE_SSE_OK:MiniMax-M3', raw)

    def test_additive_route_migration_and_desktop_catalog_visibility(self):
        mig_dir = self.root / 'mig'
        mig_dir.mkdir()
        r_old = RouteRegistry([
            RouteSpec('minimax', 'minimax', 'MiniMax-M3.1-Flash', 'tp-minimax-m31',
                      'https://api.minimax.io/v1/responses', 'responses-full', True, True, 'qual/m.json'),
        ], auto_fallback_id='minimax')
        reg = gw.Registry(mig_dir, {ACCOUNT}, {(ACCOUNT, CHAT_1)}, True, 'boot-r7', r_old)
        scope = reg.get(ACCOUNT, CHAT_1)
        self.assertIsNotNone(scope)

        # Incompatible mutation of existing route must fail!
        r_bad = RouteRegistry([
            RouteSpec('minimax', 'minimax', 'MiniMax-CHANGED', 'tp-minimax-m31',
                      'https://api.minimax.io/v1/responses', 'responses-full', True, True, 'qual/m.json'),
        ], auto_fallback_id='minimax')
        with self.assertRaises(ValueError):
            gw.Registry.migrate_route_expansion(mig_dir / 'registry.sqlite3', r_old, r_bad)

        # Additive expansion succeeds and allows reopening Registry & PlanPolicy!
        r_new = RouteRegistry([
            RouteSpec('minimax', 'minimax', 'MiniMax-M3.1-Flash', 'tp-minimax-m31',
                      'https://api.minimax.io/v1/responses', 'responses-full', True, True, 'qual/m.json'),
            RouteSpec('minimax-m3', 'minimax', 'MiniMax-M3', 'tp-minimax-m3',
                      'https://api.minimax.io/v1/responses', 'responses-full', True, False, 'qual/m3.json'),
        ], auto_fallback_id='minimax')
        gw.Registry.migrate_route_expansion(mig_dir / 'registry.sqlite3', r_old, r_new)
        PlanPolicy.migrate_route_expansion(
            scope.state_dir / 'quota.sqlite3',
            old_routes={'minimax': 'minimax'},
            new_routes={'minimax': 'minimax', 'minimax-m3': 'minimax-m3'},
            auto_fallback_id='minimax',
        )
        reg2 = gw.Registry(mig_dir, {ACCOUNT}, {(ACCOUNT, CHAT_1)}, True, 'boot-r7', r_new)
        self.assertIsNotNone(reg2.get(ACCOUNT, CHAT_1))

        # Desktop catalog preserves 'hide' on hidden native OpenAI models and supports allow_replace=True
        cfg_file = self.root / 'config.toml'
        cfg_file.write_text('openai_base_url = "http://127.0.0.1:8788/old"\nmodel_catalog_json = "C:/old/catalog.json"\nmodel = "gpt-6.1-sol"\n', encoding='utf-8')
        mc_file = self.root / 'models_cache.json'
        mc_file.write_text(json.dumps({'models': [
            {'slug': 'gpt-6.1-sol', 'display_name': 'GPT-6.1 Sol', 'visibility': 'list', 'use_responses_lite': True},
            {'slug': 'gpt-reserve', 'display_name': 'GPT Reserve', 'visibility': 'hide', 'use_responses_lite': True},
        ]}), encoding='utf-8')
        qual_file = self.root / 'q.json'
        qual_raw = json.dumps({'catalog_metadata': {'context_window': 131072, 'max_context_window': 131072, 'input_modalities': ['text']}}).encode()
        qual_file.write_bytes(qual_raw)
        svc_file = self.root / 'svc.json'
        svc_file.write_text(json.dumps({
            'host': '127.0.0.1', 'port': 8788, 'capability': CAP,
            'openai_models': ['gpt-6.1-sol', 'gpt-reserve'],
            'route_specs': [{'native_alias': 'tp-minimax-m3', 'display_name': 'MiniMax M3', 'protocol_profile': 'responses-full', 'qualification_ref': str(qual_file)}],
            'qualification_files': {str(qual_file): hashlib.sha256(qual_raw).hexdigest()},
            'recovery_aliases': {},
        }), encoding='utf-8')
        with self.assertRaises(ValueError):
            desktop_cfg.stage(config_path=cfg_file, model_cache_path=mc_file, service_config_path=svc_file, directory=self.root / 'st1')
        manifest = desktop_cfg.stage(
            config_path=cfg_file, model_cache_path=mc_file, service_config_path=svc_file,
            directory=self.root / 'st2', allow_replace=True,
        )
        cat = json.loads((self.root / 'st2/catalog.json').read_text(encoding='utf-8'))['models']
        by_slug = {m['slug']: m for m in cat}
        self.assertEqual(by_slug['gpt-6.1-sol']['visibility'], 'list')
        self.assertEqual(by_slug['gpt-reserve']['visibility'], 'hide')
        self.assertEqual(by_slug['tp-minimax-m3']['visibility'], 'list')
        self.assertEqual(manifest['auth_modified'], False)

    def test_reconcile_and_archive_unblocks_identical_compaction_body_and_preflight_rollover(self):
        from handoff import build_prompt, make_snapshot
        import handoff_executor_v4 as hexec

        # 1. Exact binding reminder is placed in the final user directive for both HTTP and AGY handoffs!
        snap = make_snapshot(
            chat_id=CHAT_1, source_provider='minimax', source_model='MiniMax-M3.1-Flash',
            last_turn_id=TURN_1, captured_at=1794340000, visible_source=b'{"visible":"context"}',
        )
        prompt = build_prompt(snap, b'{"visible":"context"}')
        prepared_http = json.loads(hexec.prepare_body({
            'provider': 'minimax', 'model': 'MiniMax-M3.1-Flash', 'prompt': prompt,
            'tools': [], 'tool_choice': 'none', 'max_output_tokens': 4096,
        }))
        expected_binding = json.dumps({k: snap[k] for k in ('chat_id', 'last_turn_id', 'source_sha256')}, separators=(',', ':'))
        self.assertIn(expected_binding, prepared_http['input'][-1]['content'][0]['text'])

        snap_agy = dict(snap, source_provider='antigravity', source_model='gemini-4-argon-low')
        prompt_agy = build_prompt(snap_agy, b'{"visible":"context"}')
        prepared_agy = json.loads(agy_bridge.prepare_body({
            'provider': 'antigravity', 'model': 'gemini-4-argon-low', 'prompt': prompt_agy,
            'tools': [], 'tool_choice': 'none',
        }))
        self.assertIn(expected_binding, prepared_agy['source']['input'][0]['content'])

        # 2. AGY cached usage normalizes total_tokens accurately for Codex context window UI!
        agy_decoded = agy_bridge.decode_result([
            {'event': 'init', 'conversation_id': 'c-1', 'init': {'model': 'gemini-4-argon-low', 'permission_mode': 'strict'}},
            {'event': 'result', 'result': {
                'conversation_id': 'c-1', 'status': 'SUCCESS', 'num_turns': 1,
                'structured_output': {'kind': 'message', 'text': 'OK'},
                'usage': {'input_tokens': 2441, 'cache_read_tokens': 16310, 'output_tokens': 468, 'thinking_tokens': 438, 'total_tokens': 2909},
            }},
        ], 'gemini-4-argon-low', [])
        self.assertEqual(agy_decoded['usage']['input_tokens'], 18751)
        self.assertEqual(agy_decoded['usage']['total_tokens'], 19219)
        self.assertEqual(agy_decoded['usage']['input_tokens_details'], {'cached_tokens': 16310})

        # 3. Un-dispatched preflight draft on turn-p1 rolls over cleanly when retried on turn-p2!
        compact_body = {
            'model': 'gpt-test',
            'tools': [],
            'input': [
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Same compact payload'}]},
                {'type': 'compaction_trigger'},
            ],
        }
        orig_notifier = self.adapters.notifier_factory
        self.adapters.notifier_factory = lambda d: (lambda notice: '')
        s_pause, r_pause, _ = self.post(compact_body, turn='turn-p1')
        self.assertEqual(s_pause, 409, r_pause)
        self.adapters.notifier_factory = orig_notifier

        # 4. Simulate failed outgoing handoff on turn-p2 -> 502 uncertain -> retry blocked with 409 -> archive -> retry on turn-p3 returns 200!
        fail_once = {'boom': True}
        orig_exec_factory = self.adapters.executor_factory
        def flaky_exec_factory(**kw):
            inner = orig_exec_factory(**kw)
            def call(req):
                if fail_once['boom']:
                    raise ValueError('simulated pre-dispatch handoff binding failure')
                return inner(req)
            return call
        self.adapters.executor_factory = flaky_exec_factory
        s_fail, _, hdr_fail = self.post(compact_body, turn='turn-p2')
        self.assertEqual(s_fail, 502)
        req_id = hdr_fail['X-Continuity-Request-ID']

        fail_once['boom'] = False
        # Without reconciliation, uncertain outcome blocks retry (502); with old reconcile() alone, same turn hits 409 request_already_recorded!
        s_blocked, r_blocked, _ = self.post(compact_body, turn='turn-p3')
        self.assertEqual(s_blocked, 502, r_blocked)
        scope = self.server.registry.get(ACCOUNT, CHAT_1)
        scope.policy.reconcile(req_id, evidence_receipt='legacy-cutover-without-archive')
        s_dup, r_dup, _ = self.post(compact_body, turn='turn-p2')
        self.assertEqual(s_dup, 409, r_dup)
        self.assertIn(b'request_already_recorded', r_dup)

        # Reconcile and archive req_id -> preserves forensic rows under archived_id and unblocks turn-p4 with exact same body!
        receipt = gw.reconcile_and_archive_request(scope.state_dir, req_id, evidence_receipt='unit-test-r10-cutover')
        self.assertTrue(receipt['archived_id'].startswith('reconciled:'))

        self.now += 2
        self.wall += 2
        s_ok, r_ok, _ = self.post(compact_body, turn='turn-p4')
        self.assertEqual(s_ok, 200, r_ok)
        self.assertIn(b'event: response.completed', r_ok)
        self.adapters.executor_factory = orig_exec_factory

    def test_per_model_context_windows_preserved_across_openai_and_plans(self):
        cfg_file = self.root / 'cw_config.toml'
        cfg_file.write_text('model = "gpt-6.1-sol"\n', encoding='utf-8')
        mc_file = self.root / 'cw_models_cache.json'
        mc_file.write_text(json.dumps({'models': [
            {'slug': 'gpt-6.1-sol', 'display_name': 'GPT-6.1 Sol', 'visibility': 'list',
             'use_responses_lite': True, 'context_window': 500000, 'max_context_window': 872000,
             'effective_context_window_percent': 95},
            {'slug': 'gpt-6-astra', 'display_name': 'GPT-6 Astra', 'visibility': 'list',
             'use_responses_lite': True, 'context_window': 272000, 'max_context_window': 872000,
             'effective_context_window_percent': 95},
        ]}), encoding='utf-8')
        q_m3 = self.root / 'q_m3.json'
        q_m3.write_bytes(json.dumps({'catalog_metadata': {
            'context_window': 1048576, 'max_context_window': 1048576,
            'effective_context_window_percent': 95, 'input_modalities': ['text', 'image'],
        }}).encode())
        q_m27 = self.root / 'q_m27.json'
        q_m27.write_bytes(json.dumps({'catalog_metadata': {
            'context_window': 204800, 'max_context_window': 204800,
            'effective_context_window_percent': 95, 'input_modalities': ['text', 'image'],
        }}).encode())
        q_qwen = self.root / 'q_qwen.json'
        q_qwen.write_bytes(json.dumps({'catalog_metadata': {
            'context_window': 1000000, 'max_context_window': 1000000,
            'effective_context_window_percent': 95, 'input_modalities': ['text', 'image'],
        }}).encode())
        svc_file = self.root / 'cw_svc.json'
        svc_file.write_text(json.dumps({
            'host': '127.0.0.1', 'port': 8788, 'capability': CAP,
            'openai_models': ['gpt-6.1-sol', 'gpt-6-astra'],
            'route_specs': [
                {'native_alias': 'tp-minimax-m3', 'display_name': 'MiniMax M3', 'protocol_profile': 'responses-full', 'qualification_ref': str(q_m3)},
                {'native_alias': 'tp-minimax-m27', 'display_name': 'MiniMax M2.7', 'protocol_profile': 'responses-full', 'qualification_ref': str(q_m27)},
                {'native_alias': 'tp-alibaba-qwen', 'display_name': 'Qwen 3.8 Max', 'protocol_profile': 'responses-full', 'qualification_ref': str(q_qwen)},
            ],
            'qualification_files': {
                str(q): hashlib.sha256(q.read_bytes()).hexdigest() for q in (q_m3, q_m27, q_qwen)
            },
            'recovery_aliases': {'chatgpt-return-sol': 'gpt-6.1-sol'},
        }), encoding='utf-8')
        desktop_cfg.stage(config_path=cfg_file, model_cache_path=mc_file, service_config_path=svc_file, directory=self.root / 'cw_stage')
        models = {m['slug']: m for m in json.loads((self.root / 'cw_stage/catalog.json').read_text(encoding='utf-8'))['models']}
        self.assertEqual((models['gpt-6.1-sol']['context_window'], models['gpt-6.1-sol']['max_context_window']), (500000, 872000))
        self.assertEqual((models['chatgpt-return-sol']['context_window'], models['chatgpt-return-sol']['max_context_window']), (500000, 872000))
        self.assertEqual((models['gpt-6-astra']['context_window'], models['gpt-6-astra']['max_context_window']), (272000, 872000))
        self.assertEqual((models['tp-minimax-m3']['context_window'], models['tp-minimax-m3']['max_context_window']), (1048576, 1048576))
        self.assertEqual((models['tp-minimax-m27']['context_window'], models['tp-minimax-m27']['max_context_window']), (204800, 204800))
        self.assertEqual((models['tp-alibaba-qwen']['context_window'], models['tp-alibaba-qwen']['max_context_window']), (1000000, 1000000))

    def test_oversized_exec_tool_description_over_1mib_bounded_for_compaction_handoff_and_turns(self):
        from handoff_executor_v4 import prepare_body as handoff_prepare_body
        from history_v4 import MAX_TOOL_DESCRIPTION_UNITS, bound_tool_descriptions

        big_exec_desc = (
            'Run JavaScript code in a persistent Node-compatible execution context.\n'
            'Some deferred nested tools may be omitted from this description.\n'
            '### `builtin` — Built-in Tools\n' + ('B' * 12000) + '\n'
            '### `mcp__codex_apps__gmail` — Gmail Cloud Connector\n' + ('C' * 980000) + '\n'
            '### `mcp__workflow_providers` — Local Workflow Providers\n' + ('W' * 280000)
        )
        self.assertGreater(len(big_exec_desc), 1260000)
        exec_tool = {
            'type': 'function',
            'name': 'exec',
            'description': big_exec_desc,
            'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}}, 'required': ['code']},
        }

        # 1. Full-mode turn on MiniMax followed by remote /compact with 1.27M-char exec.description
        u1 = {'type': 'message', 'id': 'msg_u_big1', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Hello with 530 cloud tools'}]}
        s1, r1, _ = self.post({'model': 'tp-minimax-m31', 'instructions': 'sys', 'tools': [exec_tool], 'input': [u1]}, turn='turn-big-1')
        self.assertEqual(s1, 200, r1)
        mm_sent_desc = self.minimax_up.calls[-1]['body']['tools'][0]['description']
        self.assertLessEqual(len(mm_sent_desc), MAX_TOOL_DESCRIPTION_UNITS)
        self.assertIn('### `builtin`', mm_sent_desc)
        self.assertIn('### `mcp__workflow_providers`', mm_sent_desc)
        self.assertNotIn('mcp__codex_apps__gmail', mm_sent_desc)

        a1 = json.loads(r1)['output'][0]
        self.now += 2
        self.wall += 2
        compact_req = {
            'model': 'tp-minimax-m31',
            'instructions': 'sys',
            'tools': [exec_tool],
            'input': [u1, a1, {'type': 'compaction_trigger'}],
        }
        sc, rc, _ = self.post(compact_req, turn='turn-big-2')
        self.assertEqual(sc, 200, rc)
        self.assertIn(b'event: response.completed', rc)
        oa_compact_desc = self.openai_up.calls[-1]['body']['tools'][0]['description']
        self.assertLessEqual(len(oa_compact_desc), MAX_TOOL_DESCRIPTION_UNITS)
        self.assertIn('### `mcp__workflow_providers`', oa_compact_desc)
        self.assertNotIn('mcp__codex_apps__gmail', oa_compact_desc)

        # 2. Lite-mode switch from OpenAI (gpt-test) -> tp-mimo-pro with 1.27M-char additional_tools
        handoff_wires = []
        orig_exec_factory = self.adapters.executor_factory

        class CapturingHandoffExecutor(FakeHandoffExecutor):
            def __call__(self_exec, request):
                wire = handoff_prepare_body(request)
                handoff_wires.append(json.loads(wire))
                return super().__call__(request)

        self.adapters.executor_factory = lambda **kw: CapturingHandoffExecutor(**kw)
        try:
            u_lite = {'type': 'message', 'id': 'msg_u_lite1', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Start on OpenAI'}]}
            s_oa, r_oa, _ = self.post({'model': 'gpt-test', 'instructions': 'sys', 'tools': [exec_tool], 'input': [u_lite]}, chat=CHAT_2, turn='turn-lite-1')
            self.assertEqual(s_oa, 200, r_oa)
            a_oa = json.loads(r_oa)['output'][0]

            self.now += 2
            self.wall += 2
            lite_decl = {
                'type': 'additional_tools',
                'role': 'developer',
                'tools': [{'type': 'namespace', 'name': 'exec', 'description': 'Code mode', 'tools': [exec_tool]}],
            }
            u_lite2 = {'type': 'message', 'id': 'msg_u_lite2', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Switch to MiMo Pro lite'}]}
            s_mimo, r_mimo, _ = self.post(
                {'model': 'tp-mimo-pro', 'instructions': 'sys', 'input': [lite_decl, u_lite, a_oa, u_lite2]},
                chat=CHAT_2,
                turn='turn-lite-2',
                extra_headers={'x-openai-internal-codex-responses-lite': 'true'},
            )
            self.assertEqual(s_mimo, 200, r_mimo)
            self.assertTrue(handoff_wires)
            oa_handoff_body = handoff_wires[-1]
            oa_handoff_desc = oa_handoff_body['input'][0]['tools'][0]['tools'][0]['description']
            self.assertLessEqual(len(oa_handoff_desc), MAX_TOOL_DESCRIPTION_UNITS)
            self.assertIn('### `mcp__workflow_providers`', oa_handoff_desc)
            self.assertNotIn('mcp__codex_apps__gmail', oa_handoff_desc)
            self.assertNotIn('mcp__codex_apps__gmail', oa_handoff_body['instructions'])

            mimo_sent_desc = self.mimo_up.calls[-1]['body']['input'][0]['tools'][0]['tools'][0]['description']
            self.assertLessEqual(len(mimo_sent_desc), MAX_TOOL_DESCRIPTION_UNITS)
            self.assertIn('### `mcp__workflow_providers`', mimo_sent_desc)
        finally:
            self.adapters.executor_factory = orig_exec_factory

        # 3. Fallback deterministic bound when a non-codex_apps description still exceeds limit
        plain_huge = {'tools': [{'type': 'function', 'name': 'huge', 'description': 'X' * 1200000}], 'input': []}
        self.assertTrue(bound_tool_descriptions(plain_huge))
        self.assertLessEqual(len(plain_huge['tools'][0]['description']), MAX_TOOL_DESCRIPTION_UNITS)

    def test_r12_mimo_include_antigravity_web_search_media_amsg_handoff_and_qwen36_effort(self):
        from handoff_executor_v4 import prepare_body as handoff_prepare_body

        # 1. MiMo strips unsupported output.encrypted_content & access_programs; qwen3.6-flash caps effort to medium
        mimo_spec = RouteSpec('mimo-pro', 'mimo', 'mimo-v2.6-pro', 'tp-mimo-pro',
                              'https://token-plan-sgp.xiaomimimo.com/v1/responses', 'responses-lite', True, False, 'q.json')
        mimo_raw = json.dumps({
            'model': 'tp-mimo-pro',
            'include': ['reasoning.encrypted_content', 'output.encrypted_content'],
            'access_programs': {'pro': True},
            'reasoning': {'effort': 'xhigh'},
            'input': [{'type': 'additional_tools', 'role': 'developer', 'tools': []}],
        }).encode()
        mimo_prep = json.loads(prepare_native(mimo_raw, mimo_spec))
        self.assertEqual(mimo_prep['include'], ['reasoning.encrypted_content'])
        self.assertNotIn('access_programs', mimo_prep)
        self.assertEqual(mimo_prep['reasoning']['effort'], 'high')

        mimo_only_out = json.dumps({
            'model': 'tp-mimo-pro',
            'include': ['output.encrypted_content'],
            'input': [{'type': 'additional_tools', 'role': 'developer', 'tools': []}],
        }).encode()
        self.assertNotIn('include', json.loads(prepare_native(mimo_only_out, mimo_spec)))

        q36_spec = RouteSpec('qwen36-flash', 'alibaba', 'qwen3.6-flash', 'tp-qwen36-flash',
                             'https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/responses',
                             'responses-full', True, False, 'q.json')
        for eff, expected in (('high', 'medium'), ('xhigh', 'medium'), ('max', 'medium'), ('minimal', 'low'), ('low', 'low'), ('none', 'none')):
            q36_prep = json.loads(prepare_native(json.dumps({
                'model': 'tp-qwen36-flash',
                'access_programs': {'plus': True},
                'reasoning': {'effort': eff},
                'tools': [],
                'input': [{'type': 'message', 'role': 'user', 'content': 'hi'}],
            }).encode(), q36_spec))
            self.assertEqual(q36_prep['reasoning']['effort'], expected)
            self.assertNotIn('access_programs', q36_prep)

        # 2. Antigravity strips hosted web_search tool and projects historical media in messages & tool outputs
        agy_chat = 'e8b00ab2-15e6-73dd-8fce-9fda8fe0389f'
        agy_body = {
            'model': 'tp-gemini-argon-agy',
            'reasoning': {'effort': 'high'},
            'tools': [
                {'type': 'web_search', 'external_web_access': True},
                {'type': 'custom', 'name': 'exec', 'description': 'Run JS'},
            ],
            'input': [
                {'type': 'message', 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': 'Inspect screenshot'},
                    {'type': 'input_image', 'image_url': 'data:image/png;base64,AAAA'},
                ]},
                {'type': 'custom_tool_call', 'id': 'ct_1', 'call_id': 'call_1', 'name': 'exec', 'input': 'snap()'},
                {'type': 'custom_tool_call_output', 'call_id': 'call_1', 'output': [
                    {'type': 'input_text', 'text': 'Captured'},
                    {'type': 'input_image', 'image_url': 'data:image/png;base64,BBBB'},
                ]},
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Summarize UI'}]},
            ],
        }
        s_agy, r_agy, h_agy = self.post(agy_body, chat=agy_chat, turn='turn-agy-media-1')
        self.assertEqual(s_agy, 200, r_agy)
        self.assertEqual(h_agy['X-Continuity-Route'], 'gemini-argon')
        self.assertEqual(self.agy_cli_models[-1], 'gemini-4-argon-high')

        # 3. Cold switch on existing chat with amsg_* (agent_message + encrypted_content) sanitizes handoff native_history
        amsg_chat = '84461c7d-fc67-7731-8d72-45c3df525948'
        captured_handoff = []
        orig_exec_factory = self.adapters.executor_factory

        class InspectingHandoffExecutor(FakeHandoffExecutor):
            def __call__(self_exec, request):
                wire = handoff_prepare_body(request)
                captured_handoff.append(json.loads(wire))
                return super().__call__(request)

        self.adapters.executor_factory = lambda **kw: InspectingHandoffExecutor(**kw)
        try:
            amsg_body = {
                'model': 'tp-deepseek-v41-flash',
                'tools': [],
                'input': [
                    {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Coordinate subagents'}]},
                    {
                        'type': 'agent_message',
                        'id': 'amsg_peer_1',
                        'author': '/root/worker',
                        'recipient': '/root',
                        'content': [
                            {'type': 'input_text', 'text': 'Subagent completed task.'},
                            {'type': 'encrypted_content', 'encrypted_content': 'UNHYDRATABLE_AMSG_OPAQUE'},
                        ],
                        'internal_chat_message_metadata_passthrough': {'turn_id': 'old'},
                    },
                    {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Continue on DeepSeek'}]},
                ],
            }
            s_ds, r_ds, h_ds = self.post(amsg_body, chat=amsg_chat, turn='turn-amsg-1')
            self.assertEqual(s_ds, 200, r_ds)
            self.assertEqual(h_ds['X-Continuity-Route'], 'deepseek-v41-flash')
            self.assertTrue(captured_handoff)
            handoff_json_str = json.dumps(captured_handoff[-1])
            self.assertNotIn('UNHYDRATABLE_AMSG_OPAQUE', handoff_json_str)
            self.assertNotIn('"agent_message"', handoff_json_str)
            self.assertIn('Subagent completed task.', handoff_json_str)
        finally:
            self.adapters.executor_factory = orig_exec_factory

        # 4. Outgoing handoff with prompt > 900,000 chars moves prompt to input so instructions stays well under 1 MiB
        big_prompt = 'HOST SNAPSHOT:\n{"chat_id":"c","last_turn_id":"t","source_sha256":"' + ('a' * 64) + '"}\nVISIBLE SOURCE:\n' + ('V' * 1050000)
        large_handoff = json.loads(handoff_prepare_body({
            'provider': 'openai',
            'model': 'gpt-6.1-sol',
            'prompt': big_prompt,
            'max_prompt_bytes': 2 * 1024 * 1024,
            'max_request_bytes': 4 * 1024 * 1024,
            'tools': [],
            'tool_choice': 'none',
            'native_history': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hi'}]}],
        }))
        self.assertLess(len(large_handoff['instructions']), 1024)
        self.assertEqual(large_handoff['input'][-2]['content'][0]['text'], big_prompt)
        self.assertIn('Write the source-bound task handoff JSON now.', large_handoff['input'][-1]['content'][0]['text'])

    def test_r13_reconciled_sse_provenance_client_stream_abort_and_historical_tool_controls(self):
        from handoff_executor_v4 import prepare_body as handoff_prepare_body

        # 1. Local client SSE abort (e.g. subagent wakeup / user Stop) reconciles cleanly and attributes emitted encrypted reasoning!
        abort_ready = threading.Event()
        release_tail = threading.Event()

        class SlowStreamUpstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.server.calls.append(body)
                model = body['model']
                rs_item = {'type': 'reasoning', 'id': 'rs_partial_mm_1', 'summary': [{'type': 'summary_text', 'text': 'Partial plan'}], 'encrypted_content': 'ENC_PARTIAL_MM_1'}
                first_chunk = (
                    f'event: response.created\ndata: {json.dumps({"type": "response.created", "response": {"id": "resp_part_1", "model": model, "status": "in_progress", "output": []}})}\n\n'
                    f'event: response.output_item.done\ndata: {json.dumps({"type": "response.output_item.done", "output_index": 0, "item": rs_item})}\n\n'
                ).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(first_chunk)
                self.wfile.flush()
                abort_ready.set()
                release_tail.wait(timeout=5)
                import time as _time
                for idx in range(20):
                    try:
                        _time.sleep(0.02)
                        delta = f'event: response.output_text.delta\ndata: {json.dumps({"type": "response.output_text.delta", "output_index": 1, "delta": f"tok{idx}"})}\n\n'.encode()
                        self.wfile.write(delta)
                        self.wfile.flush()
                    except OSError:
                        return

        slow_srv = ThreadingHTTPServer(('127.0.0.1', 0), SlowStreamUpstream)
        slow_srv.calls = []
        threading.Thread(target=slow_srv.serve_forever, daemon=True).start()
        self.addCleanup(slow_srv.server_close)
        self.addCleanup(slow_srv.shutdown)
        orig_endpoints = self.server.endpoints
        self.server.endpoints = dict(orig_endpoints, **{'minimax-m3': f'http://127.0.0.1:{slow_srv.server_port}/v1/responses'})
        try:
            chat_abort = '0458f032-34d4-77f4-bb00-5ccbc9175a6f'
            u_init = {'type': 'message', 'id': 'msg_ab_1', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Coordinate subagents'}]}
            conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
            hdrs = {
                'thread-id': chat_abort, 'session-id': chat_abort, 'ChatGPT-Account-ID': ACCOUNT,
                'Authorization': 'Bearer OAUTH_R7', 'Content-Type': 'application/json',
                'x-client-request-id': chat_abort,
                'x-codex-turn-metadata': json.dumps({'thread_id': chat_abort, 'session_id': chat_abort, 'turn_id': 'turn-ab-1'}),
            }
            conn.request('POST', f'/{CAP}/responses', json.dumps({'model': 'tp-minimax-m3', 'tools': [], 'input': [u_init]}), hdrs)
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertTrue(abort_ready.wait(timeout=5))
            # Read until rs_partial_mm_1 arrives, then abort client socket just like Codex Desktop on amsg_* arrival!
            buf = b''
            while b'rs_partial_mm_1' not in buf:
                buf += resp.fp.readline()
            resp.close()
            conn.close()
            release_tail.set()
        finally:
            self.server.endpoints = orig_endpoints

        # Immediately send the continuation turn containing rs_partial_mm_1 + host-observed amsg_*!
        self.now += 2
        self.wall += 2
        rs_saved = {'type': 'reasoning', 'id': 'rs_partial_mm_1', 'summary': [{'type': 'summary_text', 'text': 'Partial plan'}], 'encrypted_content': 'ENC_PARTIAL_MM_1'}
        amsg_wake = {
            'type': 'agent_message', 'id': 'amsg_15994068-d6d1-780f-b812-5db51b4e6909',
            'author': '/root/worker_1', 'recipient': '/root',
            'content': [{'type': 'input_text', 'text': 'Chapter 4 done'}, {'type': 'encrypted_content', 'encrypted_content': 'ENC_AMSG_WAKE'}],
        }
        s_cont, r_cont, h_cont = self.post(
            {'model': 'tp-minimax-m3', 'tools': [], 'input': [u_init, rs_saved, amsg_wake]},
            chat=chat_abort, turn='turn-ab-2',
        )
        self.assertEqual(s_cont, 200, r_cont)
        self.assertEqual(h_cont['X-Continuity-Route'], 'minimax-m3')

        # Switching that same chat to tp-deepseek-v41-flash also translates rs_partial_mm_1 cleanly (not 502)!
        self.now += 2
        self.wall += 2
        s_sw, r_sw, h_sw = self.post(
            {'model': 'tp-deepseek-v41-flash', 'tools': [], 'input': [u_init, rs_saved, amsg_wake, {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Switch to DeepSeek'}]}]},
            chat=chat_abort, turn='turn-ab-3',
        )
        self.assertEqual(s_sw, 200, r_sw)
        self.assertEqual(h_sw['X-Continuity-Route'], 'deepseek-v41-flash')

        # 2. Multi-model additional_tools shapes: post-compaction baseline controls + lite <-> full switches
        chat_post_cmp = '6014dabd-28c2-78fc-8880-7de40a9212df'
        add_pre = {'type': 'additional_tools', 'id': 'at_caeb7704-bf71-7747-8793-2a344344cc37', 'role': 'developer',
                   'tools': [{'type': 'namespace', 'name': 'ns1', 'tools': [{'type': 'function', 'name': 'fn1', 'parameters': {'type': 'object'}}]}]}
        cmp_base = {'type': 'compaction', 'id': 'cmp_base_pcmp', 'encrypted_content': 'ENC_BASE_CMP_pcmp'}
        add_post = {'type': 'additional_tools', 'id': 'at_a9b482fd-ada9-746a-82fc-de5379462dee', 'role': 'developer',
                    'tools': [{'type': 'namespace', 'name': 'ns2', 'tools': [{'type': 'function', 'name': 'fn2', 'parameters': {'type': 'object'}}]}]}
        body_pcmp = {
            'model': 'gpt-6.1-terra',
            'tools': [{'type': 'function', 'name': 'exec', 'parameters': {'type': 'object'}}],
            'input': [
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Before compact'}]},
                add_pre, cmp_base,
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'After compact'}]},
                add_post,
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Continue on Terra'}]},
            ],
        }
        s_terra, r_terra, h_terra = self.post(body_pcmp, chat=chat_post_cmp, turn='turn-pcmp-1')
        self.assertEqual(s_terra, 200, r_terra)
        self.assertEqual(h_terra['X-Continuity-Route'], 'openai')

        # Now switch that chat to tp-deepseek-v41-flash AND tp-gemini-argon-agy (with parameters=None tool!)
        self.now += 2
        self.wall += 2
        body_pcmp_ds = dict(body_pcmp, model='tp-deepseek-v41-flash')
        s_pcmp_ds, r_pcmp_ds, _ = self.post(body_pcmp_ds, chat=chat_post_cmp, turn='turn-pcmp-2')
        self.assertEqual(s_pcmp_ds, 200, r_pcmp_ds)

        self.now += 2
        self.wall += 2
        body_pcmp_agy = dict(body_pcmp, model='tp-gemini-argon-agy', tools=[{'type': 'function', 'name': 'null_params_tool', 'parameters': None}])
        s_pcmp_agy, r_pcmp_agy, _ = self.post(body_pcmp_agy, chat=chat_post_cmp, turn='turn-pcmp-3')
        self.assertEqual(s_pcmp_agy, 200, r_pcmp_agy)

        # 3. Chat starting in responses-lite (oversized additional_tools at index 0 + post-baseline additional_tools) -> full-mode routes + OpenAI!
        chat_lite_sw = '806d1a8c-8d0f-79cc-8a5d-cdd2ade38688'
        huge_desc = 'Run JS.\n### `builtin` — Builtin\n' + ('B' * 5000) + '\n### `mcp__codex_apps__drive` — Drive\n' + ('D' * 900000)
        lite_0 = {
            'type': 'additional_tools', 'id': 'at_64694913-2176-70c1-87ae-9efda7b18657', 'role': 'developer',
            'tools': [{'type': 'namespace', 'name': 'exec', 'tools': [{'type': 'function', 'name': 'exec', 'description': huge_desc, 'parameters': {'type': 'object'}}]}],
        }
        u_mimo1 = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Start on MiMo Pro'}]}
        s_m1, r_m1, _ = self.post(
            {'model': 'tp-mimo-pro', 'input': [lite_0, u_mimo1]},
            chat=chat_lite_sw, turn='turn-lsw-1',
            extra_headers={'x-openai-internal-codex-responses-lite': 'true'},
        )
        self.assertEqual(s_m1, 200, r_m1)
        a_m1 = json.loads(r_m1)['output'][0]

        self.now += 2
        self.wall += 2
        lite_2 = {
            'type': 'additional_tools', 'id': 'at_ed01d23b-6ea0-7be1-8f8b-350ac11a19d5', 'role': 'developer',
            'tools': [{'type': 'namespace', 'name': 'exec2', 'tools': [{'type': 'function', 'name': 'exec2', 'description': huge_desc, 'parameters': {'type': 'object'}}]}],
        }
        u_mimo2 = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Second MiMo turn'}]}
        s_m2, r_m2, _ = self.post(
            {'model': 'tp-mimo-pro', 'input': [lite_0, u_mimo1, a_m1, lite_2, u_mimo2]},
            chat=chat_lite_sw, turn='turn-lsw-2',
            extra_headers={'x-openai-internal-codex-responses-lite': 'true'},
        )
        self.assertEqual(s_m2, 200, r_m2)
        a_m2 = json.loads(r_m2)['output'][0]

        # Switch from MiMo lite (with no unknown opaque items -> direct non-rebase path!) to DeepSeek full mode AND back to OpenAI full mode!
        self.now += 2
        self.wall += 2
        u_ds = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Switch to DeepSeek full'}]}
        s_l2ds, r_l2ds, h_l2ds = self.post(
            {'model': 'tp-deepseek-v41-flash', 'tools': [], 'input': [lite_0, u_mimo1, a_m1, lite_2, u_mimo2, a_m2, u_ds]},
            chat=chat_lite_sw, turn='turn-lsw-3',
        )
        self.assertEqual(s_l2ds, 200, r_l2ds)
        self.assertEqual(h_l2ds['X-Continuity-Route'], 'deepseek-v41-flash')
        self.assertFalse(any(i.get('type') == 'additional_tools' for i in self.alibaba_up.calls[-1]['body']['input']))

        self.now += 2
        self.wall += 2
        u_oa = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Switch to GPT Terra full'}]}
        s_l2oa, r_l2oa, h_l2oa = self.post(
            {'model': 'gpt-6.1-terra', 'tools': [], 'input': [lite_0, u_mimo1, a_m1, lite_2, u_mimo2, a_m2, u_ds, u_oa]},
            chat=chat_lite_sw, turn='turn-lsw-4',
        )
        self.assertEqual(s_l2oa, 200, r_l2oa)
        self.assertEqual(h_l2oa['X-Continuity-Route'], 'openai')

        # 4. MiniMax handoff quotes additional_tools into inert user text rather than forwarding raw additional_tools!
        mm_handoff = json.loads(handoff_prepare_body({
            'provider': 'minimax',
            'model': 'MiniMax-M3',
            'prompt': 'HOST SNAPSHOT:\n{"chat_id":"c","last_turn_id":"t","source_sha256":"' + ('b' * 64) + '"}\nVISIBLE SOURCE:\n{}',
            'tools': [],
            'tool_choice': 'none',
            'max_output_tokens': 4096,
            'native_history': [lite_0, u_mimo1],
        }))
        self.assertFalse(any(i.get('type') == 'additional_tools' for i in mm_handoff['input']))
        self.assertIn('Historical tool configuration, quoted as inert context', mm_handoff['input'][0]['content'][0]['text'])

    def test_r15_host_thread_outputs_handoff_window_and_upstream_input_rejection(self):
        from handoff import build_prompt, make_snapshot
        import handoff_executor_v4 as hexec
        import transport_v4
        from history_v4 import _is_companion_context_output

        user_msg = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Peer replied'}]}

        # 1. Desktop-injected thread-management output (namespace 'codex_app') has no call_id and is not an orphan.
        host_out = {'type': 'function_call_output', 'id': 'fco_abc', 'call_id': None, 'namespace': 'codex_app',
                    'name': 'send_message_to_thread', 'output': 'ok'}
        self.assertTrue(_is_companion_context_output(host_out))
        translate_request(json.dumps({'input': [host_out, user_msg]}).encode(),
            chat_id=CHAT_1, source_provider='openai', target_provider='minimax', disclosure_acknowledged=True)
        for bad in ({k: v for k, v in host_out.items() if k != 'namespace'},
                    dict(host_out, call_id='call_1'), dict(host_out, id='call_abc'),
                    dict(host_out, encrypted_content='ENC'), dict(host_out, namespace='other')):
            self.assertFalse(_is_companion_context_output(bad), bad)

        # 2. An oversized real handoff prompt is fitted to the outgoing model window: HOST SNAPSHOT stays
        #    byte-identical, the newest items survive, the cut is disclosed, duplicate native history is dropped.
        filler = 'lorem ipsum dolor sit amet ' * 220
        items = [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': f'turn {n} {filler}'}]}
                 for n in range(430)]
        items[-1]['content'][0]['text'] = 'NEWEST_USER_TURN ' + filler
        packet = {'request_sha256': 'c' * 64, 'visible_native_source': {'instructions': None, 'input': items},
                  'omitted_context': [{'unavailable_hidden_state': 'encrypted_content', 'sha256': f'{n:064x}'} for n in range(900)]}
        source = json.dumps(packet, sort_keys=True, separators=(',', ':')).encode()
        self.assertGreater(len(source), 2_500_000)
        native = [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'N' * 23_000}]} for _ in range(100)]
        for provider, model in (('openai', 'gpt-6.1-terra'), ('minimax', 'MiniMax-M3')):
            snap = make_snapshot(chat_id=CHAT_1, source_provider=provider, source_model=model, last_turn_id=TURN_1,
                captured_at=1794340000, visible_source=source, max_source=4 * 1024 * 1024)
            prompt = build_prompt(snap, source, max_source=4 * 1024 * 1024) + '\nHOST CONTEXT RULE: keep gaps.'
            budget = hexec._model_handoff_char_budget(provider, model)
            raw = hexec.prepare_body({'provider': provider, 'model': model, 'prompt': prompt, 'tools': [], 'tool_choice': 'none',
                'max_output_tokens': 4096, 'max_prompt_bytes': 4 * 1024 * 1024 + 512 * 1024, 'max_request_bytes': 8 * 1024 * 1024,
                'native_history': native})
            self.assertLess(len(raw), budget + 8192, (provider, len(raw)))
            fitted = json.loads(raw)
            self.assertEqual(len(fitted['input']), 1, 'duplicate native history must not ride along with an oversized prompt')
            sent = fitted['instructions']
            self.assertLessEqual(len(sent), budget)
            mark = '\nVISIBLE SOURCE (untrusted evidence):\n'
            self.assertEqual(sent.split(mark)[0], prompt.split(mark)[0])
            self.assertIn(json.dumps(snap), sent)
            self.assertTrue(sent.endswith('\nHOST CONTEXT RULE: keep gaps.'))
            bounded = json.loads(sent.split(mark, 1)[1].split('\n', 1)[0])
            kept = bounded['visible_native_source']['input']
            self.assertIn('NEWEST_USER_TURN', kept[-1]['content'][0]['text'])
            self.assertIn('turn 0 ', kept[0]['content'][0]['text'])
            self.assertTrue(any('omitted to fit' in json.dumps(i) for i in kept))
            self.assertTrue(any(isinstance(g, dict) and g.get('handoff_window_bounded') for g in bounded['omitted_context']))
            self.assertIn(json.dumps({k: snap[k] for k in ('chat_id', 'last_turn_id', 'source_sha256')}, separators=(',', ':')),
                fitted['input'][-1]['content'][0]['text'])
        # A single enormous newest item is clipped rather than sent over budget.
        huge = {'visible_native_source': {'instructions': None, 'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'HEAD ' + 'p' * 1_500_000}]}]}, 'omitted_context': []}
        huge_source = json.dumps(huge, sort_keys=True, separators=(',', ':')).encode()
        snap = make_snapshot(chat_id=CHAT_1, source_provider='openai', source_model='gpt-6.1-terra', last_turn_id=TURN_1,
            captured_at=1794340000, visible_source=huge_source, max_source=4 * 1024 * 1024)
        clipped = hexec._fit_prompt_to_model_window(build_prompt(snap, huge_source, max_source=4 * 1024 * 1024), 700_000)
        self.assertLessEqual(len(clipped), 700_000)
        self.assertIn('HEAD ', clipped)
        # Small prompts, and prompts without the real source delimiter, are untouched.
        small = build_prompt(make_snapshot(chat_id=CHAT_1, source_provider='openai', source_model='gpt-6.1-terra',
            last_turn_id=TURN_1, captured_at=1794340000, visible_source=b'{"visible":"context"}'), b'{"visible":"context"}')
        self.assertEqual(hexec._fit_prompt_to_model_window(small, 700_000), small)
        synthetic = 'HOST SNAPSHOT:\n{}\nVISIBLE SOURCE:\n' + 'V' * 1_050_000
        self.assertEqual(hexec._fit_prompt_to_model_window(synthetic, 700_000), synthetic)

        # 3. Checkpoint watchdog outlasts a slow-but-streaming provider and still fits the 300 s gateway deadline.
        executor = hexec.ResponsesHandoffExecutor(provider='minimax', token='synthetic-token', account_id=ACCOUNT, thread_id=CHAT_1)
        self.assertEqual(executor.timeout, 240)
        self.assertEqual(transport_v4.MAX_WIRE, 4 * 1024 * 1024)

        # 4. A zero-output upstream input rejection reaches Desktop verbatim and never latches the chat.
        upstream_error = {'message': 'Your input exceeds the context window of this model.', 'type': 'invalid_request_error',
                          'param': 'input', 'code': 'context_length_exceeded'}

        class RejectingUpstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.server.calls.append(body)
                if self.server.mode == 'http400':
                    payload = json.dumps({'error': upstream_error}).encode()
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                else:
                    created = {'type': 'response.created', 'sequence_number': 0, 'response': {
                        'id': 'resp_rej_1', 'model': body['model'], 'status': 'in_progress', 'output': []}}
                    failed = {'type': 'error', 'sequence_number': 1, 'error': upstream_error}
                    payload = (f'event: response.created\ndata: {json.dumps(created)}\n\n'
                               f'event: error\ndata: {json.dumps(failed)}\n\n').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        rejecting = ThreadingHTTPServer(('127.0.0.1', 0), RejectingUpstream)
        rejecting.calls = []
        threading.Thread(target=rejecting.serve_forever, daemon=True).start()
        self.addCleanup(rejecting.server_close)
        self.addCleanup(rejecting.shutdown)
        orig_endpoints = self.server.endpoints
        for mode, chat in (('http400', 'c1b3f68b-7052-7eb2-8f45-040cdf4293af'), ('sse', '4470db96-ce42-729b-891c-8896ca428dfa')):
            rejecting.mode = mode
            body = {'model': 'tp-minimax-m3', 'tools': [], 'input': [
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Oversized turn ' + mode}]}]}
            self.server.endpoints = dict(orig_endpoints, **{'minimax-m3': f'http://127.0.0.1:{rejecting.server_port}/v1/responses'})
            try:
                status, raw, _ = self.post(body, chat=chat, turn='turn-rej-1')
            finally:
                self.server.endpoints = orig_endpoints
            self.assertEqual(status, 400 if mode == 'http400' else 200, raw)
            self.assertIn(b'context_length_exceeded', raw)
            self.assertNotIn(b'local_continuity_pause', raw)
            if mode == 'http400':
                self.assertEqual(json.loads(raw), {'error': upstream_error})
            states = []
            for db_path in (self.root / 'state/gateway').glob('*/evidence.sqlite3'):
                with closing(sqlite3.connect(db_path)) as db:
                    states += [row[0] for row in db.execute('SELECT state FROM requests')]
            self.assertNotIn('uncertain', states)
            self.assertIn('reconciled:upstream_input_rejected', states)
            # The identical retry is admitted immediately: no 409 uncertain_request latch.
            self.now += 2
            self.wall += 2
            status, raw, hdrs = self.post(body, chat=chat, turn='turn-rej-2')
            self.assertEqual(status, 200, raw)
            self.assertEqual(hdrs['X-Continuity-Route'], 'minimax-m3')
        # Ordinary upstream failures (5xx, auth, rate limits) keep the fail-closed pause.
        class Failing(RejectingUpstream):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                payload = json.dumps({'error': {'type': 'server_error', 'message': 'boom'}}).encode()
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        failing = ThreadingHTTPServer(('127.0.0.1', 0), Failing)
        threading.Thread(target=failing.serve_forever, daemon=True).start()
        self.addCleanup(failing.server_close)
        self.addCleanup(failing.shutdown)
        self.server.endpoints = dict(orig_endpoints, **{'minimax-m3': f'http://127.0.0.1:{failing.server_port}/v1/responses'})
        try:
            status, raw, _ = self.post({'model': 'tp-minimax-m3', 'tools': [], 'input': [
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Server error turn'}]}]},
                chat='5368a513-c43c-7cab-8060-a8e545cf4681', turn='turn-500-1')
        finally:
            self.server.endpoints = orig_endpoints
        self.assertEqual(status, 502, raw)
        self.assertIn(b'local_continuity_pause', raw)

    def test_r16_failed_pre_dispatch_handoff_archives_itself(self):
        chat = '9d052767-0f03-7feb-843f-646ce26bb625'
        u1 = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Start on GPT'}]}
        status, raw, _ = self.post({'model': 'gpt-test', 'tools': [], 'input': [u1]}, chat=chat, turn='turn-h-1')
        self.assertEqual(status, 200, raw)
        a1 = json.loads(raw)['output'][0]
        switch = {'model': 'tp-minimax-m3', 'tools': [], 'input': [u1, a1,
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Switch to MiniMax'}]}]}

        # The outgoing-model checkpoint fails (timeout, provider error, overflow) before anything is dispatched.
        failing = {'on': True}
        orig_exec_factory = self.adapters.executor_factory
        def flaky_exec_factory(**kw):
            inner = orig_exec_factory(**kw)
            def call(req):
                if failing['on']:
                    raise ValueError('simulated outgoing checkpoint failure')
                return inner(req)
            return call
        self.adapters.executor_factory = flaky_exec_factory
        self.addCleanup(setattr, self.adapters, 'executor_factory', orig_exec_factory)
        self.now += 2
        self.wall += 2
        upstream_calls = len(self.minimax_up.calls)
        status, raw, _ = self.post(switch, chat=chat, turn='turn-h-2')
        # 409 (not 502) so the native client reports it instead of auto-retrying a paid checkpoint.
        self.assertEqual(status, 409, raw)
        self.assertIn(b'nothing was sent', raw)
        self.assertEqual(len(self.minimax_up.calls), upstream_calls)
        scope = self.server.registry.get(ACCOUNT, chat)
        for name, table in (('evidence.sqlite3', 'requests'), ('quota.sqlite3', 'attempts'), ('handoffs/generation.sqlite3', 'generations')):
            with closing(sqlite3.connect(scope.state_dir / name)) as db:
                states = [row[0] for row in db.execute(f'SELECT state FROM {table}')]
            self.assertNotIn('uncertain', states, table)
            self.assertNotIn('reserved', states, table)
            self.assertIn('reconciled:pre_dispatch_failed', states, table)

        # The identical retry runs a fresh checkpoint and completes: no uncertain_request latch.
        failing['on'] = False
        self.now += 2
        self.wall += 2
        status, raw, hdrs = self.post(switch, chat=chat, turn='turn-h-3')
        self.assertEqual(status, 200, raw)
        self.assertEqual(hdrs['X-Continuity-Route'], 'minimax-m3')
        self.assertTrue(any('Saved task handoff' in json.dumps(i) for i in self.minimax_up.calls[-1]['body']['input']))

    def test_r17_upstream_stream_died_before_output_archives_itself(self):
        # Live 2026-11-11: upstream (minimax, deepseek) sent only response.created
        # then the connection died. Zero output left the provider, so the attempt
        # must archive itself instead of latching the chat on uncertain_request.
        class Dying(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                created = {'type': 'response.created', 'sequence_number': 0, 'response': {
                    'id': f'resp_died_{self.server.mode}', 'model': body['model'], 'status': 'in_progress', 'output': []}}
                frames = [f'event: response.created\ndata: {json.dumps(created)}\n\n']
                if self.server.mode == 'partial':
                    # Output was already delivered, then the connection died: still fail closed.
                    frames.append('event: response.output_item.done\ndata: ' + json.dumps({
                        'type': 'response.output_item.done', 'sequence_number': 1, 'output_index': 0,
                        'item': {'type': 'message', 'id': 'msg_died', 'role': 'assistant', 'status': 'completed',
                                 'content': [{'type': 'output_text', 'text': 'partial text'}]}}) + '\n\n')
                payload = ''.join(frames).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')  # no Content-Length: connection close is the EOF
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()

        dying = ThreadingHTTPServer(('127.0.0.1', 0), Dying)
        dying.calls = []
        threading.Thread(target=dying.serve_forever, daemon=True).start()
        self.addCleanup(dying.server_close)
        self.addCleanup(dying.shutdown)
        orig_endpoints = self.server.endpoints
        body = {'model': 'tp-minimax-m3', 'tools': [], 'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Upstream dies mid-turn'}]}]}

        # 1. Death before any output: 200 with a terminal error frame, archived, no latch.
        chat = 'bde3b99e-b35f-7723-71ef-ab1735a7e61f'
        dying.mode = 'created_only'
        self.server.endpoints = dict(orig_endpoints, **{'minimax-m3': f'http://127.0.0.1:{dying.server_port}/v1/responses'})
        try:
            status, raw, _ = self.post(body, chat=chat, turn='turn-die-1')
        finally:
            self.server.endpoints = orig_endpoints
        self.assertEqual(status, 200, raw)
        self.assertIn(b'response.created', raw)
        self.assertIn(b'upstream_stream_died', raw)
        self.assertNotIn(b'local_continuity_pause', raw)
        scope = self.server.registry.get(ACCOUNT, chat)
        for name, table in (('evidence.sqlite3', 'requests'), ('quota.sqlite3', 'attempts')):
            with closing(sqlite3.connect(scope.state_dir / name)) as db:
                states = [row[0] for row in db.execute(f'SELECT state FROM {table}')]
            self.assertNotIn('uncertain', states, table)
            self.assertIn('reconciled:upstream_stream_died_before_output', states, table)
        # The identical retry is admitted immediately.
        self.now += 2
        self.wall += 2
        status, raw, hdrs = self.post(body, chat=chat, turn='turn-die-2')
        self.assertEqual(status, 200, raw)
        self.assertEqual(hdrs['X-Continuity-Route'], 'minimax-m3')

        # 2. Death after delivered output: still fail closed with the pause (evidence incomplete).
        chat2 = '4c372d35-9aa3-71e2-8767-26a962abd78a'
        dying.mode = 'partial'
        self.server.endpoints = dict(orig_endpoints, **{'minimax-m3': f'http://127.0.0.1:{dying.server_port}/v1/responses'})
        try:
            status, raw, _ = self.post(body, chat=chat2, turn='turn-die-3')
        finally:
            self.server.endpoints = orig_endpoints
        self.assertEqual(status, 200, raw)
        self.assertIn(b'response.output_item.done', raw)
        self.assertIn(b'local_continuity_pause', raw)
        scope2 = self.server.registry.get(ACCOUNT, chat2)
        with closing(sqlite3.connect(scope2.state_dir / 'evidence.sqlite3')) as db:
            states = [row[0] for row in db.execute('SELECT state FROM requests')]
        self.assertIn('uncertain', states)
        self.assertFalse(any(s.startswith('reconciled:upstream_stream_died') for s in states))


if __name__ == '__main__':
    unittest.main()
