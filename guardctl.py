"""guardctl - install and operate Codex Continuity Guard on Windows.

  install     Build a pinned release and deployment from providers.json, start the
              service outside any Job Object, point Codex Desktop at it and register
              it to start at logon. Run again after editing providers.json or updating.
  set-key P   Store (or replace) the API key for provider id P, DPAPI-encrypted.
  status      Show service health, routed models and any paused requests.
  start/stop  Start or stop the installed service.
  reconcile   Stop, archive every paused (uncertain) request, start again.
  uninstall   Stop the service, restore Codex Desktop's routing, remove the logon entry.

Normally run through install.ps1 / guard.cmd, which use the private virtualenv.
No API key, account id or capability token is ever printed.
"""
import argparse
from contextlib import closing
import getpass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import types
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'runtime'
FOLDERS = ('continuity-v2', 'continuity-v3', 'continuity-v4')
LIVE_STATES = ('reserved', 'dispatched', 'upstream_complete')
TUNED = ('minimax', 'mimo', 'alibaba')


def fail(message):
    raise SystemExit('\nERROR: ' + message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path, name):
    """Compile the exact release bytes; never import them (no bytecode lands in a release)."""
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(Path(path).read_bytes(), str(path), 'exec'), module.__dict__)
    return module


def powershell(script, timeout=60):
    exe = Path(os.environ.get('SystemRoot', r'C:\Windows')) / r'System32\WindowsPowerShell\v1.0\powershell.exe'
    done = subprocess.run([str(exe), '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True,
                          text=True, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


# ---------------------------------------------------------------- providers.json

def parse_providers(data):
    """Validate the user's providers.json and derive the service's route list."""
    if not isinstance(data, dict):
        fail('providers.json must be a JSON object.')
    port = data.get('port', 8788)
    if type(port) is not int or not 1024 <= port <= 65535:
        fail('"port" must be a number between 1024 and 65535.')
    providers, models = data.get('providers'), data.get('models')
    if not isinstance(providers, dict) or not providers or not isinstance(models, list) or not models:
        fail('providers.json needs at least one entry under "providers" and one under "models".')
    endpoints = {}
    for ident, entry in providers.items():
        endpoint = entry.get('endpoint') if isinstance(entry, dict) else None
        target = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if not re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', ident) or ident in ('openai', 'antigravity'):
            fail(f'Provider id "{ident}" must be lower-case letters, digits, "-" or "_" (and not "openai").')
        if (target is None or target.scheme != 'https' or not target.hostname or target.username or target.password
                or target.query or target.fragment):
            fail(f'Provider "{ident}": "endpoint" must be an https:// URL of an OpenAI Responses API, e.g. https://host/v1/responses')
        if target.hostname.endswith('example.com'):
            fail('providers.json still contains the example placeholders. Replace them with your own provider, model and endpoint.')
        endpoints[ident] = endpoint
    routes, fallback = [], None
    for entry in models:
        if not isinstance(entry, dict):
            fail('Each entry under "models" must be an object.')
        provider, model = entry.get('provider'), entry.get('model')
        if provider not in endpoints:
            fail(f'Model "{model}" names provider "{provider}", which is not listed under "providers".')
        if not isinstance(model, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', model):
            fail(f'Model id "{model}" is not a valid model identifier.')
        route_id = re.sub('[^a-z0-9]+', '-', f'{provider}-{model}'.lower()).strip('-')[:100]
        protocol = entry.get('protocol', 'responses-full')
        if protocol not in ('responses-full', 'responses-lite'):
            fail(f'Model "{model}": "protocol" must be "responses-full" (default) or "responses-lite".')
        window = entry.get('context_window')
        if type(window) is not int or not 8192 <= window <= 2 * 1024 * 1024:
            fail(f'Model "{model}": "context_window" (tokens) is required, between 8192 and 2097152.')
        name = entry.get('name') or model
        if not isinstance(name, str) or not 0 < len(name) <= 200:
            fail(f'Model "{model}": "name" must be a short display label.')
        if any(r['route_id'] == route_id for r in routes):
            fail(f'Model "{model}" is listed twice for provider "{provider}".')
        if entry.get('auto_fallback') is True:
            if fallback:
                fail('Only one model may set "auto_fallback": true.')
            fallback = route_id
        routes.append({'route_id': route_id, 'provider_id': provider, 'actual_model': model, 'native_alias': 'tp-' + route_id,
                       'endpoint': endpoints[provider], 'protocol_profile': protocol, 'display_name': name,
                       'context_window': window, 'image_input': entry.get('image_input') is True})
    fallback = fallback or routes[0]['route_id']
    unused = sorted(set(endpoints) - {r['provider_id'] for r in routes})
    if unused:
        fail('Providers without any model: ' + ', '.join(unused) + '. Add a model for each or remove them.')
    return {'port': port, 'routes': routes, 'auto_fallback_id': fallback, 'providers': endpoints}


def read_providers(root, interactive):
    path = root / 'providers.json'
    if not path.exists():
        shutil.copyfile(HERE / 'providers.example.json', path)
        if not interactive:
            fail(f'Fill in your providers and models in {path}, then run install again.')
        print('\n' + '=' * 72 + '\n  ADD YOUR OWN PROVIDERS AND MODELS\n' + '=' * 72)
        print(f'  Notepad is opening {path}\n  Replace the example provider, endpoint and model with your own,\n'
              '  save, and close Notepad to continue. API keys are NOT typed here;\n  you will be asked for each key next.\n')
        subprocess.run(['notepad.exe', str(path)])
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except ValueError as error:
        fail(f'{path} is not valid JSON: {error}')
    return parse_providers(data)


def ensure_keys(credentials, providers, interactive):
    for ident, endpoint in sorted(providers.items()):
        try:
            credentials.load(ident)
        except credentials.CredentialUnavailable:
            if not interactive:
                fail(f'No API key stored for provider "{ident}". Run: guard set-key {ident}')
            print('\n' + '-' * 72 + f'\n  API KEY NEEDED for provider "{ident}" ({urlsplit(endpoint).hostname})\n'
                  '  Paste your own key and press Enter. It is not shown, and is stored\n'
                  '  encrypted for your Windows account only (never in any file you share).\n' + '-' * 72)
            credentials.store(ident, getpass.getpass('  Key: '))
            print(f'  Stored key for "{ident}".')


# ---------------------------------------------------------------- build

def build_release(root):
    files = sorted(p for folder in FOLDERS for p in (SOURCE / folder).iterdir()
                   if p.is_file() and p.suffix in ('.py', '.ps1') and not p.name.startswith('test_'))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(SOURCE).as_posix().encode() + b'\0' + path.read_bytes())
    release = root / 'releases' / ((HERE / 'VERSION').read_text().strip() + '-' + digest.hexdigest()[:8])
    if not release.exists():  # immutable: identical source always maps to the same folder
        partial = release.with_name(release.name + '.partial')
        shutil.rmtree(partial, ignore_errors=True)
        for path in files:
            target = partial / path.relative_to(SOURCE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        partial.rename(release)
    return release


def codex_inputs(codex_home):
    """Read-only: the signed-in account id and the user's own model catalog cache."""
    try:
        account = json.loads((codex_home / 'auth.json').read_text(encoding='utf-8'))['tokens']['account_id']
        cache = json.loads((codex_home / 'models_cache.json').read_text(encoding='utf-8'))['models']
    except (OSError, ValueError, KeyError, TypeError):
        fail(f'Codex Desktop is not set up in {codex_home}. Install it from the Microsoft Store, sign in with ChatGPT, '
             'open it once, then run install again.')
    if not isinstance(account, str) or not account:
        fail('Codex Desktop is not signed in with a ChatGPT account. Sign in, then run install again.')
    listed = sorted((m for m in cache if isinstance(m.get('slug'), str) and m['slug'].startswith('gpt-')),
                    key=lambda m: m.get('priority', 999))
    if not listed:
        fail('No OpenAI gpt-* models found in Codex Desktop\'s model cache; open Codex Desktop once and retry.')
    config = codex_home / 'config.toml'
    if not config.exists():
        config.write_bytes(b'')
    current = tomllib.loads(config.read_text(encoding='utf-8-sig'))
    slugs = [m['slug'] for m in listed]
    visible = [m['slug'] for m in listed if m.get('visibility') == 'list'] or slugs
    return account, slugs, current.get('model') if current.get('model') in slugs else visible[0], current


def build_deployment(root, release, providers, codex_home, previous):
    service = load(release / 'continuity-v4' / 'production_service_v4.py', 'guard_service')
    sys.path[:0] = [str(release / folder) for folder in reversed(FOLDERS)]
    from native_quota import NativeQuotaError, qualified_executable
    python = Path(sys.executable)
    if python.parent.parent != root / 'venv' or python.name.lower() != 'python.exe':
        fail(f'Run this through install.ps1 or guard.cmd so it uses the private virtualenv in {root / "venv"}.')
    account, openai_models, cold_model, _ = codex_inputs(codex_home)
    try:
        native = qualified_executable()
    except NativeQuotaError:
        fail('The Microsoft Store build of Codex Desktop was not found (C:\\Program Files\\WindowsApps\\OpenAI.Codex_*).')
    deployment = root / 'deployments' / time.strftime('%Y%m%d-%H%M%S')
    deployment.mkdir(parents=True)
    (root / 'qualifications').mkdir(exist_ok=True)  # stable paths: an unchanged model keeps an identical route identity
    specs, proofs = [], []
    for route in providers['routes']:
        automatic = route['route_id'] == providers['auto_fallback_id']
        proof = root / 'qualifications' / (route['route_id'] + '.json')
        proof.write_text(json.dumps({
            'qualified_for_native': True, 'declared_by': 'installer from the user\'s providers.json',
            'provider_id': route['provider_id'], 'actual_model': route['actual_model'],
            'protocol_profile': route['protocol_profile'], 'auto_fallback_qualified': automatic,
            'paid_lite_compaction_qualified': route['protocol_profile'] == 'responses-lite',
            'catalog_metadata': {'context_window': route['context_window'], 'max_context_window': route['context_window'],
                                 'input_modalities': ['text', 'image'] if route['image_input'] else ['text']}}, indent=2),
            encoding='utf-8')
        proofs.append(proof)
        specs.append({**{k: route[k] for k in ('route_id', 'provider_id', 'actual_model', 'native_alias', 'endpoint',
                                                 'protocol_profile', 'display_name')},
                      'supports_native_tools': True, 'auto_fallback_qualified': automatic, 'qualification_ref': str(proof)})
    capability = previous['capability'] if previous else None  # keep Desktop's URL stable across upgrades
    result = service.prepare_config(
        deployment / 'service-config.json', package_root=release, state_dir=root / 'state', account_id=account,
        openai_models=openai_models, port=providers['port'], native_runtime=native, python_runtime=python,
        external_files=service.dependency_files(python), recovery_aliases={}, registered_chats=(), enroll_new_chats=True,
        allow_incomplete_visible_rebase=True, allow_paid_compaction=True, allow_native_review=True,
        allow_paid_lite_compaction=True, route_specs=specs, auto_fallback_id=providers['auto_fallback_id'],
        qualification_files=proofs, capability=capability, default_cold_model=cold_model)
    helper = load(release / 'continuity-v4' / 'service_startup.py', 'guard_startup')
    service_path, pythonw = release / 'continuity-v4' / 'production_service_v4.py', python.with_name('pythonw.exe')
    staged = helper.stage_manifest(deployment / 'launch-manifest.json', config_path=deployment / 'service-config.json',
        config_sha256=result['config_sha256'], service_path=service_path, service_sha256=sha(service_path),
        pythonw_path=pythonw, pythonw_sha256=sha(pythonw))
    return deployment, staged['manifest_sha256']


# ---------------------------------------------------------------- route changes vs. existing chat state

def bound_routes(root):
    """The model list the saved chat state was last bound to, or None before the first start."""
    record = root / 'state-routes.json'
    registry = root / 'state' / 'gateway' / 'registry.sqlite3'
    return json.loads(record.read_text(encoding='utf-8')) if record.is_file() and registry.is_file() else None


def check_additive(bound, providers):
    """Saved chats refer to models by route; refuse changes that would strand them, before anything is stopped."""
    removed = sorted({s['route_id'] for s in bound['route_specs']} - {r['route_id'] for r in providers['routes']})
    if removed:
        names = ', '.join(f'{s["provider_id"]}/{s["actual_model"]}' for s in bound['route_specs'] if s['route_id'] in removed)
        fail(f'providers.json no longer lists: {names}. Models can be added but not removed, because existing chats may be '
             'using them. Put them back, or start from scratch with uninstall.ps1 -Purge.')
    if bound['auto_fallback_id'] != providers['auto_fallback_id']:
        fail(f'The auto_fallback model cannot be changed after the first install (it is route "{bound["auto_fallback_id"]}"). '
             'Keep "auto_fallback": true on that model, or start from scratch with uninstall.ps1 -Purge.')


def bind_state_routes(root, release, config):
    """Explicit additive migration of the stopped service's state to the new model list."""
    new = {'route_specs': config['route_specs'], 'auto_fallback_id': config['auto_fallback_id']}
    bound = bound_routes(root)
    if bound and bound != new:
        sys.path[:0] = [str(release / folder) for folder in reversed(FOLDERS)]
        import gateway_v4
        from route_policy import PlanPolicy
        from route_registry import RouteRegistry, RouteSpec
        old_routes, new_routes = (RouteRegistry([RouteSpec(**s) for s in c['route_specs']], auto_fallback_id=c['auto_fallback_id'])
                                  for c in (bound, new))
        gateway = root / 'state' / 'gateway'
        gateway_v4.Registry.migrate_route_expansion(gateway / 'registry.sqlite3', old_routes, new_routes)
        labels = [{s.route_id: s.display_name or s.route_id for s in r.routes} for r in (old_routes, new_routes)]
        for ledger in gateway.glob('*/quota.sqlite3'):
            try:
                PlanPolicy.migrate_route_expansion(ledger, old_routes=labels[0], new_routes=labels[1],
                                                   auto_fallback_id=new['auto_fallback_id'])
            except (ValueError, sqlite3.Error) as error:  # one odd chat must not block the whole gateway
                print(f'  Note: chat state {ledger.parent.name[:8]} was not migrated ({error}).')
    (root / 'state-routes.json').write_text(json.dumps(new, indent=2), encoding='utf-8')


# ---------------------------------------------------------------- running service

class Installed:
    """The currently installed release/deployment, loaded from <root>/current.json."""
    def __init__(self, root):
        self.root = root
        record = root / 'current.json'
        self.record = json.loads(record.read_text(encoding='utf-8')) if record.is_file() else None
        if self.record:
            self.release, self.deployment = Path(self.record['release']), Path(self.record['deployment'])
            self.config = json.loads((self.deployment / 'service-config.json').read_bytes())
            self.service = load(self.release / 'continuity-v4' / 'production_service_v4.py', 'guard_service')

    def require(self):
        if not self.record:
            fail(f'Nothing is installed in {self.root}. Run install.ps1 first.')
        return self


def health(config):
    try:
        conn = http.client.HTTPConnection('127.0.0.1', config['port'], timeout=3)
        conn.request('GET', '/' + config['capability'] + '/health', headers={'Connection': 'close'})
        resp = conn.getresponse()
        raw = resp.read(65536)
        conn.close()
        return json.loads(raw) if resp.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def open_rows(root):
    """Every request/attempt row that is neither completed nor already archived."""
    found, gateway = [], root / 'state' / 'gateway'
    for scope in sorted(p for p in gateway.iterdir() if p.is_dir()) if gateway.is_dir() else []:
        for name, table in (('evidence.sqlite3', 'requests'), ('quota.sqlite3', 'attempts')):
            if (scope / name).is_file():
                with closing(sqlite3.connect(scope / name, timeout=5)) as db:
                    found += [(scope, table, ident, state) for ident, state in db.execute(
                        f"SELECT id,state FROM {table} WHERE state!='completed' AND state NOT LIKE 'reconciled:%'")]
    return found


def stop(installed, wait_idle=True):
    live = health(installed.config)
    if not live:
        return False
    deadline = time.time() + (120 if wait_idle else 0)  # let an in-flight turn finish when one is running
    while time.time() < deadline and any(state in LIVE_STATES for *_, state in open_rows(installed.root)):
        time.sleep(0.5)
    identity = installed.service.process_module(installed.config)
    child = identity.process_identity(live['pid'])
    wrapper = identity.process_identity(child['parent'])
    for proc in (child, wrapper):
        if 'production_service_v4.py' not in proc['command'] or str(installed.deployment) not in proc['command']:
            fail('The listener on this port is not this installation\'s service; nothing was stopped.')
    for proc in (child, wrapper):
        subprocess.run(['taskkill', '/PID', str(proc['pid']), '/F'], capture_output=True, timeout=30)
        time.sleep(0.7)
    deadline = time.time() + 30
    while time.time() < deadline and health(installed.config) is not None:
        time.sleep(0.5)
    if health(installed.config) is not None:
        fail('The service did not stop.')
    return True


def archive_open_requests(root, release, label):
    """Paused or interrupted requests are archived so no chat stays latched. Service must be stopped."""
    sys.path[:0] = [str(release / folder) for folder in reversed(FOLDERS)]
    import gateway_v4
    archived = 0
    for scope, request_id in sorted({(scope, ident) for scope, _, ident, _ in open_rows(root)}):
        for name, table in (('evidence.sqlite3', 'requests'), ('quota.sqlite3', 'attempts')):
            with closing(sqlite3.connect(scope / name, timeout=5)) as db, db:
                db.execute(f"UPDATE {table} SET state='uncertain' WHERE id=? AND state IN (?,?,?)", (request_id, *LIVE_STATES))
        gateway_v4.reconcile_and_archive_request(scope, request_id, evidence_receipt=label)
        archived += 1
    return archived


def start(root, release, deployment, manifest_sha, config):
    """WMI creates the helper with no ancestor Job Object, so this works from any agent or terminal shell."""
    journal = root / 'state-startup' / 'owned-startup.json'
    if journal.is_file() and json.loads(journal.read_bytes()).get('config_sha256') != sha(deployment / 'service-config.json'):
        history = journal.parent / 'history'  # a journal for an older configuration is retired, never reused
        history.mkdir(exist_ok=True)
        journal.replace(history / (sha(journal) + '.json'))
    helper = release / 'continuity-v4' / 'service_startup.py'
    manifest = deployment / 'launch-manifest.json'
    command = subprocess.list2cmdline([str(Path(sys.executable).with_name('pythonw.exe')), '-I', '-S', '-B', str(helper),
                                       '--manifest', str(manifest), '--manifest-sha256', manifest_sha])
    code, out, err = powershell(
        "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='" + command.replace("'", "''")
        + "';CurrentDirectory='" + str(helper.parent).replace("'", "''") + "'};\"$($r.ReturnValue) $($r.ProcessId)\"")
    if code or not out.startswith('0 '):
        fail('Windows refused to start the service (WMI): ' + (out or err))
    expected, deadline = sha(deployment / 'service-config.json'), time.time() + 150
    while time.time() < deadline:
        time.sleep(1.5)
        live = health(config)
        if live and live.get('config_sha256') == expected and journal.is_file() and json.loads(journal.read_bytes()).get('status') == 'healthy':
            return live
    fail(f'The service did not become healthy within 150 seconds. Logs, if any: {root / "state"}\\launch-*')


# ---------------------------------------------------------------- Codex Desktop + logon

def activate_desktop(root, release, deployment, codex_home, live):
    desktop = load(release / 'continuity-v4' / 'desktop_configuration_v4.py', 'guard_desktop')
    config = codex_home / 'config.toml'
    current = tomllib.loads(config.read_text(encoding='utf-8-sig'))
    routed = bool(desktop.KEYS & current.keys())
    if routed and not str(current.get('openai_base_url', 'http://127.0.0.1:')).startswith('http://127.0.0.1:'):
        fail(f'{config} already sets openai_base_url to another service. Remove that line first if you want this gateway instead.')
    backup = root / 'codex-config.before-install.toml'
    if not backup.exists():
        shutil.copyfile(config, backup)
    try:
        desktop.stage(config_path=config, model_cache_path=codex_home / 'models_cache.json',
                      service_config_path=deployment / 'service-config.json', directory=deployment / 'desktop-activation',
                      allow_replace=routed)
        desktop.apply(deployment / 'desktop-activation', live)
    except ValueError as error:
        fail(f'Codex Desktop\'s config.toml was not changed: {error}. If its default "model" is one you removed from '
             'providers.json, select a GPT model in Codex Desktop, then run install again.')


def deactivate_desktop(root, release, codex_home):
    desktop = load(release / 'continuity-v4' / 'desktop_configuration_v4.py', 'guard_desktop')
    config = codex_home / 'config.toml'
    raw = config.read_bytes() if config.is_file() else b''
    restored = desktop._strip_routing_header(raw)  # removes only our two keys; later edits by the user are kept
    if restored != raw:
        shutil.copyfile(config, root / 'codex-config.before-uninstall.toml')
        desktop.atomic_write(config, restored)
    return restored != raw


def register_startup(release, deployment, manifest_sha):
    helper = load(release / 'continuity-v4' / 'service_startup.py', 'guard_startup')
    registration = load(release / 'continuity-v4' / 'startup_registration.py', 'guard_registration')
    link = Path(os.environ['APPDATA']) / r'Microsoft\Windows\Start Menu\Programs\Startup' / registration.NAME
    backup = deployment / 'startup-shortcut.previous.lnk'
    if link.exists():
        shutil.copy2(link, backup)
        link.unlink()
    try:
        bundle = helper.load_bundle(deployment / 'launch-manifest.json', manifest_sha)
        plan = registration.stage_registration(bundle, deployment / 'startup-activation', helper=helper)
        registration.activate(plan['plan_path'], plan['plan_sha256'], action='install')
    except BaseException:
        if backup.exists() and not link.exists():
            shutil.copy2(backup, link)
        raise
    return {'plan_path': plan['plan_path'], 'plan_sha256': plan['plan_sha256']}


# ---------------------------------------------------------------- commands

def credentials_module(root, release):
    module = load(release / 'continuity-v4' / 'credentials.py', 'guard_credentials')
    module.DIRECTORY = root / 'secrets'
    return module


def cmd_install(args):
    root, codex_home, interactive = args.root, args.codex_home, not args.yes
    if not SOURCE.is_dir():
        fail('install must be run from the downloaded repository folder (it contains runtime\\).')
    root.mkdir(parents=True, exist_ok=True)
    codex_inputs(codex_home)  # fail early, before anything is written or stopped
    providers = read_providers(root, interactive)
    if bound_routes(root):
        check_additive(bound_routes(root), providers)
    release = build_release(root)
    ensure_keys(credentials_module(root, release), providers['providers'], interactive)
    old = Installed(root)
    print('\nBuilding and verifying the pinned configuration (hashing the virtualenv takes a moment)...')
    deployment, manifest_sha = build_deployment(root, release, providers, codex_home, old.config if old.record else None)
    config = json.loads((deployment / 'service-config.json').read_bytes())
    if old.record and stop(old):
        print('Stopped the previous service.')
    archived = archive_open_requests(root, release, 'installer_restart')
    if archived:
        print(f'Archived {archived} paused request(s).')
    bind_state_routes(root, release, config)
    print('Starting the service...')
    live = start(root, release, deployment, manifest_sha, config)
    # Record the running deployment first, so a failure below can simply be fixed by running install again.
    record = {'release': str(release), 'deployment': str(deployment), 'manifest_sha256': manifest_sha, 'startup': None}
    (root / 'current.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    activate_desktop(root, release, deployment, codex_home, live)
    if not args.no_startup:
        record['startup'] = register_startup(release, deployment, manifest_sha)
        (root / 'current.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    if HERE != root:
        shutil.copyfile(HERE / 'guardctl.py', root / 'guardctl.py')
    (root / 'guard.cmd').write_text('@"%~dp0venv\\Scripts\\python.exe" -B "%~dp0guardctl.py" %*\r\n', encoding='ascii')
    print('\n' + '=' * 72 + '\n  INSTALLED\n' + '=' * 72)
    print(f'  Service:   http://127.0.0.1:{config["port"]} (healthy, pid {live["pid"]})')
    print('  Models added to the Codex model picker:')
    for route in providers['routes']:
        print(f'    - {route["display_name"]}   [{route["provider_id"]} / {route["actual_model"]}]'
              + ('   <- automatic fallback' if route['route_id'] == providers['auto_fallback_id'] else ''))
    print('\n  NEXT: fully quit and reopen Codex Desktop so it loads the new model list.')
    print(f'  Manage it later with:  "{root / "guard.cmd"}" status | stop | start | set-key <provider> | uninstall')


def cmd_set_key(args):
    installed = Installed(args.root)
    if installed.record:
        release, known = installed.release, {s['provider_id'] for s in installed.config['route_specs']}
    elif SOURCE.is_dir() and (args.root / 'providers.json').is_file():  # before the first install
        release, known = build_release(args.root), set(read_providers(args.root, False)['providers'])
    else:
        fail(f'Nothing is installed in {args.root} and it has no providers.json yet. Run install.ps1 first.')
    if args.provider not in known:
        fail(f'"{args.provider}" is not a configured provider id. Configured: ' + ', '.join(sorted(known)))
    credentials_module(args.root, release).store(args.provider, getpass.getpass(f'API key for "{args.provider}": '))
    print('Stored (encrypted for your Windows account). A running service uses it on the next request.')


def cmd_status(args):
    installed = Installed(args.root).require()
    live = health(installed.config)
    print('Service:    ' + (f'healthy on 127.0.0.1:{installed.config["port"]} (pid {live["pid"]})' if live else 'NOT RUNNING  (run: guard start)'))
    print('Release:    ' + installed.release.name)
    credentials = credentials_module(args.root, installed.release)
    for spec in installed.config['route_specs']:
        try:
            credentials.load(spec['provider_id'])
            key = 'key stored'
        except credentials.CredentialUnavailable:
            key = 'NO KEY  (run: guard set-key ' + spec['provider_id'] + ')'
        print(f'Model:      {spec["display_name"]}  [{spec["provider_id"]} / {spec["actual_model"]}]  {key}')
    paused = sorted({(scope.name[:8], state) for scope, _, _, state in open_rows(args.root) if state not in LIVE_STATES})
    print('Paused:     ' + (f'{len(paused)} chat(s) have a paused request  (run: guard reconcile)' if paused else 'none'))
    toml = tomllib.loads((args.codex_home / 'config.toml').read_text(encoding='utf-8-sig')) if (args.codex_home / 'config.toml').is_file() else {}
    print('Codex:      ' + ('routed through this service' if str(toml.get('openai_base_url', '')).startswith(f'http://127.0.0.1:{installed.config["port"]}/') else 'NOT routed (run install again)'))


def cmd_start(args):
    installed = Installed(args.root).require()
    if health(installed.config):
        print('Already running.')
        return
    live = start(args.root, installed.release, installed.deployment, installed.record['manifest_sha256'], installed.config)
    print(f'Started (pid {live["pid"]}).')


def cmd_stop(args):
    print('Stopped.' if stop(Installed(args.root).require()) else 'It was not running.')


def cmd_reconcile(args):
    installed = Installed(args.root).require()
    stop(installed)
    print(f'Archived {archive_open_requests(args.root, installed.release, "operator_reconcile")} paused request(s).')
    live = start(args.root, installed.release, installed.deployment, installed.record['manifest_sha256'], installed.config)
    print(f'Running again (pid {live["pid"]}).')


def cmd_uninstall(args):
    installed = Installed(args.root).require()
    stop(installed, wait_idle=False)
    if deactivate_desktop(args.root, installed.release, args.codex_home):
        print('Codex Desktop routing removed from config.toml (a copy of the routed file was kept).')
    if installed.record.get('startup'):
        registration = load(installed.release / 'continuity-v4' / 'startup_registration.py', 'guard_registration')
        try:
            registration.activate(installed.record['startup']['plan_path'], installed.record['startup']['plan_sha256'], action='uninstall')
            print('Logon startup entry removed.')
        except registration.RegistrationBlocked as error:
            print(f'Logon startup entry left in place ({error}); delete "Codex Continuity Guard" from your Startup folder by hand.')
    (args.root / 'current.json').unlink()
    print('Uninstalled. Restart Codex Desktop. Your chat state and encrypted keys remain in\n  '
          + str(args.root) + '\n  (delete that folder, or run uninstall.ps1 -Purge, to remove them).')


def main():
    parser = argparse.ArgumentParser(prog='guard', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=(HERE if (HERE / 'venv').is_dir() else Path(os.environ.get('LOCALAPPDATA', '')) / 'CodexContinuityGuard'),
                        help='installation folder (default: %%LOCALAPPDATA%%\\CodexContinuityGuard)')
    parser.add_argument('--codex-home', type=Path, default=Path.home() / '.codex', help='Codex configuration folder')
    commands = parser.add_subparsers(dest='command', required=True)
    install = commands.add_parser('install')
    install.add_argument('--yes', action='store_true', help='never prompt; fail if providers.json or a key is missing')
    install.add_argument('--no-startup', action='store_true', help='do not register a start-at-logon entry')
    commands.add_parser('set-key').add_argument('provider')
    for name in ('status', 'start', 'stop', 'reconcile', 'uninstall'):
        commands.add_parser(name)
    args = parser.parse_args()
    if os.name != 'nt':
        fail('Codex Continuity Guard supports Windows only.')
    args.root, args.codex_home = args.root.absolute(), args.codex_home.absolute()
    {'install': cmd_install, 'set-key': cmd_set_key, 'status': cmd_status, 'start': cmd_start, 'stop': cmd_stop,
     'reconcile': cmd_reconcile, 'uninstall': cmd_uninstall}[args.command](args)


if __name__ == '__main__':
    main()
