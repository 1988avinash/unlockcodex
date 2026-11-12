"""Production callback composition; no activation, credential copies or retries.

The gateway owns admission and history provenance. This factory only binds its
admitted scope to quota reads, visible warnings and outgoing-model checkpoints.
"""
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from types import MappingProxyType

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT.parent/'continuity-v3') not in sys.path:
    sys.path.append(str(ROOT.parent/'continuity-v3'))
if str(ROOT.parent/'continuity-v2') not in sys.path:
    sys.path.append(str(ROOT.parent/'continuity-v2'))
from handoff import MAX_SOURCE, _no_links, make_snapshot, build_prompt
from handoff_service import OutgoingHandoffService, bind_saved_checkpoint, canonical
from history_v4 import bound_tool_descriptions
from quota_policy import Snapshot, parse_snapshot
from native_quota import NativeQuotaReader, windows_boot_epoch

MINIMAX_MODEL = 'MiniMax-M3.1-Flash'
MAX_NATIVE = 8*1024*1024


class AdapterBlocked(ValueError):
    pass


def strict_json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise AdapterBlocked('Duplicate JSON member')
            out[key] = value
        return out
    value = json.loads(raw, object_pairs_hook=pairs)
    canonical(value)
    return value


def _credential(provider):
    """The user's own key for this provider id, from the per-user DPAPI store."""
    from credentials import load, CredentialUnavailable
    try:return load(provider)
    except CredentialUnavailable as error:raise AdapterBlocked(str(error)) from None


def _executor(**kwargs):
    from handoff_executor_v4 import ResponsesHandoffExecutor
    kwargs.pop('model')  # The service supplies and validates exact response model.
    return ResponsesHandoffExecutor(**kwargs)


def _notifier(directory):
    from desktop_notifier import DesktopNotifier
    return DesktopNotifier(directory)


def _visible(value, gaps):
    """Retain readable content while naming every omitted opaque/media object."""
    if isinstance(value, list):
        return [_visible(item, gaps) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get('type', '')
    if isinstance(kind, str) and any(word in kind.lower() for word in ('image', 'audio', 'video', 'file')):
        marker = {'uninspected_media': kind, 'sha256': hashlib.sha256(canonical(value)).hexdigest()}
        gaps.append(marker)
        return marker
    out = {}
    for key, item in value.items():
        if key in ('encrypted_content', 'internal_chat_message_metadata_passthrough'):
            marker = {'unavailable_hidden_state': key, 'sha256': hashlib.sha256(canonical(item)).hexdigest()}
            gaps.append(marker)
            out[key+'_reference'] = marker
        else:
            out[key] = _visible(item, gaps)
    return out


class ProductionAdapters:
    def qualified_plan(self,provider,model):
        if self.route_registry is None:return provider=='minimax' and model==MINIMAX_MODEL
        return any(r.provider_id==provider and r.actual_model==model for r in self.route_registry.routes)

    def __init__(self, *, state_root, openai_models, recovery_aliases=None,
                 native_reader=None, notifier_factory=None, executor_factory=None,
                 credential_reader=None, clock=time.monotonic, wall_clock=time.time,
                 clock_epoch=None, cache_seconds=15, cold_routes=None, route_registry=None,
                 provider_executor_factories=None, provider_preflight_encoders=None,
                 default_cold_openai_model=None, max_visible_source_bytes=MAX_SOURCE):
        if type(max_visible_source_bytes) is not int or not 1024<=max_visible_source_bytes<=4*1024*1024:
            raise AdapterBlocked('Bounded visible handoff source limit required')
        self.max_visible_source_bytes=max_visible_source_bytes
        self.route_registry=route_registry
        if route_registry is not None:
            # Plan providers are user configuration: a checkpoint goes to the route's own endpoint.
            from handoff_executor_v4 import ENDPOINTS
            ENDPOINTS.update({r.provider_id:r.endpoint for r in route_registry.routes if r.endpoint.startswith('https://')})
        self.provider_executor_factories=dict(provider_executor_factories or {})
        self.provider_preflight_encoders=dict(provider_preflight_encoders or {})
        self.root = Path(state_root).absolute()
        _no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.models = frozenset(openai_models)
        if not self.models or not all(isinstance(m, str) and m.startswith('gpt-') for m in self.models):
            raise AdapterBlocked('Explicit qualified OpenAI model set required')
        if default_cold_openai_model is not None and default_cold_openai_model not in self.models:
            raise AdapterBlocked('Default cold OpenAI model must be qualified')
        self.default_cold_openai_model = default_cold_openai_model
        self.aliases = dict(recovery_aliases or {})
        if any(model not in self.models for model in self.aliases.values()):
            raise AdapterBlocked('Recovery aliases must resolve to qualified models')
        seeds = {}
        for scope, seed in dict(cold_routes or {}).items():
            if (not isinstance(scope, tuple) or len(scope) != 2
                    or any(not isinstance(part, str) or not part for part in scope)
                    or not isinstance(seed, dict) or set(seed) != {
                        'provider', 'model', 'last_completed_turn_id', 'snapshot_sha256', 'evidence_ref'}):
                raise AdapterBlocked('Cold route requires exact trusted account/chat snapshot evidence')
            if (seed['provider'] != 'openai' or seed['model'] not in self.models
                    or not isinstance(seed['last_completed_turn_id'], str)
                    or not re.fullmatch('[A-Za-z0-9_-]{1,128}', seed['last_completed_turn_id'])
                    or not isinstance(seed['snapshot_sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', seed['snapshot_sha256'])
                    or not isinstance(seed['evidence_ref'], str) or not 0 < len(seed['evidence_ref']) <= 1000):
                raise AdapterBlocked('Cold route must identify a qualified old OpenAI model and immutable host snapshot')
            seeds[scope] = MappingProxyType(dict(seed))
        self.cold_routes = MappingProxyType(seeds)
        if not 0 <= cache_seconds <= 15:
            raise AdapterBlocked('Quota cache must be at most fifteen seconds')
        self.reader = native_reader or NativeQuotaReader(state_root=self.root/'quota-isolation')
        self.notifier_factory = notifier_factory or _notifier
        self.executor_factory = executor_factory or _executor
        self.credential_reader = credential_reader or _credential
        self.clock, self.wall_clock = clock, wall_clock
        self.clock_epoch = clock_epoch or windows_boot_epoch()
        self.cache_seconds = cache_seconds
        self.cache, self.cache_lock = {}, threading.Lock()
        self.full_quota_cache = {}

    def _account_snapshot(self,account,token):
        key = (account, hashlib.sha256(token.encode()).hexdigest())
        with self.cache_lock:
            now = self.clock()
            previous = self.cache.get(key)
            if previous and 0 <= now-previous.observed_monotonic < self.cache_seconds:
                return previous, self.full_quota_cache.get(key)
            payload = None
            try:
                payload = strict_json(canonical(self.reader.read(token, account)))
                value = parse_snapshot(payload, account_id=account, required_limits={'codex': {10080}},
                    observed_monotonic=self.clock(), clock_epoch=self.clock_epoch, full_read=True)
            except Exception:
                value = Snapshot(account, self.clock(), self.clock_epoch, False, False, None, 'native_quota_read_failed')
            self.cache[key] = value
            self.full_quota_cache[key] = payload
            for old in list(self.cache):
                if old[0] == account and old != key:
                    del self.cache[old]
                    self.full_quota_cache.pop(old,None)
            return value, payload

    def compaction_snapshot(self,account,headers):
        """Trusted full-read evidence for the separately authorized credit exception."""
        from paid_compaction_policy_v4 import parse_compaction_snapshot
        if headers.get('chatgpt-account-id')!=account:
            raise AdapterBlocked('Compaction quota account mismatch')
        auth=headers.get('authorization','')
        if not isinstance(auth,str) or not auth.startswith('Bearer ') or len(auth)<=7 or '\r' in auth or '\n' in auth:
            raise AdapterBlocked('Inbound native bearer required')
        snapshot,payload=self._account_snapshot(account,auth[7:])
        return parse_compaction_snapshot(payload,account_id=account,observed_monotonic=snapshot.observed_monotonic,
            clock_epoch=snapshot.clock_epoch,required_limits={'codex':{10080}})

    def validate_initial_route(self, account, thread, body, last_completed_route):
        """Call under the scope lock BEFORE quota admission or reservation.

        Cold seeds are trusted host configuration captured before exposing aliases,
        never request metadata or model-authored evidence. No history is read here.
        Unknown cold aliases are a correctable local 409, not an uncertain POST.
        """
        if not isinstance(body, dict) or not callable(last_completed_route):
            raise AdapterBlocked('Trusted scoped route preflight required')
        route = last_completed_route()
        if route is not None:
            if not isinstance(route, dict):
                raise AdapterBlocked('Completed outgoing route evidence is invalid')
            provider, model = route.get('provider'), route.get('model')
            origin = {'kind': 'completed_gateway_receipt', 'request_sha256': route.get('request_sha256')}
        elif (account, thread) in self.cold_routes:
            seed = dict(self.cold_routes[(account, thread)])
            provider, model = seed['provider'], seed['model']
            origin = {'kind': 'trusted_cold_snapshot', **seed}
        else:
            provider, model = 'openai', self.aliases.get(body.get('model'), body.get('model'))
            origin = {'kind': 'native_selected_openai_model'}
            if model not in self.models:
                if self.default_cold_openai_model is not None:
                    model = self.default_cold_openai_model
                    origin = {'kind': 'configured_default_cold_openai_model', 'model': model}
                else:
                    raise AdapterBlocked('Cold switch has no verified outgoing model. Start with a qualified OpenAI model once, '
                        'or enroll the existing chat with a trusted previous-model snapshot before selecting the Token Plan alias.')
        if not (provider == 'openai' and model in self.models or self.qualified_plan(provider,model)):
            raise AdapterBlocked('Actual outgoing model has not been qualified')
        return {'provider': provider, 'model': model, 'origin': origin}

    def __call__(self, account, thread, headers, scope_context):
        headers = dict(headers)
        root_session=scope_context.get('root_session_id',thread)
        parent=scope_context.get('parent_thread_id')
        if any(key != key.lower() for key in headers):
            raise AdapterBlocked('Canonical lowercase host headers required')
        for key, expected in (('chatgpt-account-id', account), ('thread-id', thread), ('session-id', root_session)):
            if headers.get(key) != expected:
                raise AdapterBlocked('Host account/chat scope mismatch')
        auth = headers.get('authorization', '')
        if not auth.startswith('Bearer ') or len(auth) <= 7 or '\r' in auth or '\n' in auth:
            raise AdapterBlocked('Inbound native bearer required')
        token = auth[7:]
        metadata = strict_json(headers.get('x-codex-turn-metadata', '{}'))
        if not isinstance(metadata, dict) or metadata.get('thread_id') != thread or metadata.get('session_id') != root_session:
            raise AdapterBlocked('Native turn metadata scope mismatch')
        if parent is not None and (headers.get('x-codex-parent-thread-id')!=parent or metadata.get('parent_thread_id')!=parent):
            raise AdapterBlocked('Native child parent does not match verified ancestry')
        if parent is None and (headers.get('x-codex-parent-thread-id') is not None or metadata.get('parent_thread_id') is not None):
            raise AdapterBlocked('Native parent ancestry has not been verified')
        turn = metadata.get('turn_id')
        if not isinstance(turn, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', turn):
            raise AdapterBlocked('Fresh native turn identity required')
        directory = Path(scope_context['state_dir']).absolute()
        _no_links(directory)
        if not directory.is_relative_to(self.root) or directory == self.root:
            raise AdapterBlocked('Scope state must remain inside private state root')
        directory.mkdir(parents=True, exist_ok=True)
        latest_route = scope_context['last_completed_route']
        outgoing_history = scope_context['outgoing_history']
        if not callable(latest_route) or not callable(outgoing_history):
            raise AdapterBlocked('Trusted scope history callbacks required')
        service = OutgoingHandoffService(directory/'handoffs')
        contexts = directory/'handoff-contexts.sqlite3'
        for suffix in ('', '-journal', '-wal', '-shm'):
            _no_links(Path(str(contexts)+suffix))
        with closing(sqlite3.connect(contexts)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS contexts(id TEXT PRIMARY KEY, data BLOB NOT NULL, sha256 TEXT NOT NULL)')

        def snapshot_reader():
            return self._account_snapshot(account,token)[0]

        def prepare_context(ident, raw):
            if not isinstance(raw, bytes) or len(raw) > MAX_NATIVE:
                raise AdapterBlocked('Native request source exceeds bound')
            body = strict_json(raw)
            if not isinstance(body, dict) or not re.fullmatch('[0-9a-f]{64}', ident) or hashlib.sha256(canonical(body)).hexdigest() != ident:
                raise AdapterBlocked('Exact admitted native request binding required')
            # Immutable context makes retries/restarts reuse the original time,
            # source and provider; the service never retries uncertain results.
            for suffix in ('', '-journal', '-wal', '-shm'):
                _no_links(Path(str(contexts)+suffix))
            with closing(sqlite3.connect(contexts, timeout=2)) as db, db:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT data,sha256 FROM contexts WHERE id=?', (ident,)).fetchone()
                if row:
                    if hashlib.sha256(row[0]).hexdigest() != row[1]:
                        raise AdapterBlocked('Saved checkpoint context integrity failed')
                    context = strict_json(row[0])
                    if context['account'] != account or context['snapshot']['chat_id'] != thread or context['request_sha256'] != ident:
                        raise AdapterBlocked('Saved checkpoint context scope mismatch')
                    if context['snapshot']['last_turn_id'] != turn:
                        with service.transaction() as gen_db:
                            has_gen = gen_db.execute('SELECT 1 FROM generations WHERE id=?', (ident,)).fetchone() is not None
                        if has_gen:
                            raise AdapterBlocked('Saved checkpoint context scope mismatch')
                        archived_ctx_id = 'preflight:' + hashlib.sha256((context['snapshot']['last_turn_id']+':'+ident).encode()).hexdigest()[:16] + ':' + ident[:32]
                        db.execute('UPDATE contexts SET id=? WHERE id=?', (archived_ctx_id, ident))
                        row = None
                if not row:
                    route = self.validate_initial_route(account, thread, body, latest_route)
                    provider, model = route['provider'], route['model']
                    prepared = outgoing_history(raw, provider)
                    if not isinstance(prepared, bytes) or len(prepared) > MAX_NATIVE:
                        raise AdapterBlocked('Outgoing history exceeds bound')
                    native = strict_json(prepared).get('input')
                    if not isinstance(native, list) or any(not isinstance(item, dict) for item in native):
                        raise AdapterBlocked('Materialized provider-qualified outgoing history required')
                    now = int(self.wall_clock())
                    prior = service.store.load_latest(thread, current_turn_id=turn, now=now)
                    gaps = []
                    visible_body = {'instructions': body.get('instructions'), 'input': body.get('input')}
                    if bound_tool_descriptions(visible_body):
                        gaps.append({'bounded_tool_declarations': 'additional_tools'})
                    if len(canonical(visible_body)) > 900000 and bound_tool_descriptions(visible_body, limit=4096):
                        gaps.append({'summarized_tool_declarations': 'additional_tools'})
                    visible = _visible(visible_body, gaps)
                    packet = {'request_sha256': ident, 'original_decoded_sha256': hashlib.sha256(raw).hexdigest(),
                        'visible_native_source': visible, 'omitted_context': gaps,
                        'outgoing_model_origin': route['origin'],
                        'hidden_state_is_not_decrypted': True, 'media_inspection_claimed': False,
                        'prior_checkpoint': prior, 'model_claims_require_reconciliation': True}
                    source = canonical(packet)
                    if len(source) > self.max_visible_source_bytes:
                        raise AdapterBlocked('Visible handoff source exceeds admitted bound; no truncation')
                    snapshot = make_snapshot(chat_id=thread, source_provider=provider, source_model=model,
                        last_turn_id=turn, captured_at=now, visible_source=source, max_source=self.max_visible_source_bytes)
                    context = {'account': account, 'request_sha256': ident, 'snapshot': snapshot,
                        'visible_source': source.decode(), 'native_history': native}
                    encoded = canonical(context)
                    db.execute('INSERT INTO contexts VALUES(?,?,?)', (ident, encoded, hashlib.sha256(encoded).hexdigest()))
                    # First-use preflight and later dispatch must see the same
                    # persisted JSON ordering, including build_prompt(snapshot).
                    context = strict_json(encoded)
            return context

        def outgoing_request(request,context):
            request = dict(request, native_history=context['native_history'])
            if self.max_visible_source_bytes > MAX_SOURCE and request.get('provider') != 'antigravity':
                request['max_prompt_bytes'] = self.max_visible_source_bytes + 512*1024
                request['max_request_bytes'] = 8*1024*1024
            request['prompt'] += ('\nHOST CONTEXT RULE: In addition to the visible audit packet, use the supplied '
                'provider-qualified native history for its factual task context. Do not expose private chain-of-thought '
                'or decrypt encrypted content. Report only supported task facts; preserve all known gaps and uncertainty.')
            return request

        def preflight(ident,raw):
            from handoff_executor_v4 import prepare_body,request_byte_limit
            context=prepare_context(ident,raw)
            snapshot=context['snapshot']
            request={'provider':snapshot['source_provider'],'model':snapshot['source_model'],
                'tools':[],'tool_choice':'none','max_output_tokens':16384,
                'prompt':build_prompt(snapshot,context['visible_source'].encode(),max_source=self.max_visible_source_bytes),
                'purpose':'task_handoff','logical_id':ident}
            encoder=self.provider_preflight_encoders.get(snapshot['source_provider'],prepare_body)
            exact=encoder(outgoing_request(request,context))
            bound_limit=9*1024*1024 if (self.max_visible_source_bytes>MAX_SOURCE and snapshot['source_provider']=='antigravity') else (
                8*1024*1024 if self.max_visible_source_bytes>MAX_SOURCE else request_byte_limit(snapshot['source_provider']))
            if not isinstance(exact,bytes) or not 0<len(exact)<=bound_limit:raise AdapterBlocked('Bounded exact checkpoint encoding required')
            return {'request_sha256':ident,'checkpoint_source_sha256':snapshot['source_sha256'],
                'outgoing_request_sha256':hashlib.sha256(exact).hexdigest(),'outgoing_request_bytes':len(exact),
                'provider':snapshot['source_provider'],'model':snapshot['source_model']}

        def checkpoint_callback(ident, raw):
            # Same frozen context, prompt decoration and exact wire encoder as
            # pre-admission validation. No duplicate approximate size formula.
            preflight(ident,raw)
            context=prepare_context(ident,raw)
            snapshot = context['snapshot']
            def execute(request):
                provider = snapshot['source_provider']
                selected_token = token if provider == 'openai' else (None if provider=='antigravity' else self.credential_reader(provider))
                executor_factory=self.provider_executor_factories.get(provider,self.executor_factory)
                executor = executor_factory(provider=provider, model=snapshot['source_model'], token=selected_token,
                    account_id=account, thread_id=thread, evidence_dir=directory/'handoff-transports'/ident)
                result=executor(outgoing_request(request,context))
                from handoff_normalization import normalize_response
                return normalize_response(result,snapshot,evidence_dir=directory/'handoff-transports'/ident)
            saved = service.generate(ident, snapshot=snapshot, visible_source=context['visible_source'].encode(), executor=execute, max_source=self.max_visible_source_bytes)
            return bind_saved_checkpoint(saved, request_sha256=ident, current_turn_id=turn, now=int(self.wall_clock()))

        checkpoint_callback.preflight=preflight
        current_decoded = scope_context.get('current_decoded_model')
        last_decoded_fn = scope_context.get('last_decoded_model')

        def recovery_reader():
            if not callable(last_decoded_fn) or current_decoded not in self.models:
                return False
            prior_decoded = last_decoded_fn()
            return isinstance(prior_decoded, str) and bool(prior_decoded) and prior_decoded != current_decoded

        return {'snapshot_reader': snapshot_reader, 'notifier': self.notifier_factory(directory/'notices'),
            'checkpoint_callback': checkpoint_callback, 'recovery_reader': recovery_reader}
