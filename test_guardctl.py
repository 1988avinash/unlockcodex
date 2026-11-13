"""Checks for the installer's own logic: providers.json validation and release assembly.

Run:  python -B -m unittest test_guardctl      (any Python 3.11; no network, no installation touched)
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import guardctl

GOOD = {
    'port': 8788,
    'providers': {'my-llm': {'endpoint': 'https://llm.example.net/v1/responses'},
                  'minimax': {'endpoint': 'https://api.minimax.io/v1/responses'}},
    'models': [
        {'provider': 'my-llm', 'model': 'Big-Model.v2', 'name': 'Big Model', 'context_window': 200000, 'image_input': True},
        {'provider': 'minimax', 'model': 'mm-1', 'context_window': 128000, 'auto_fallback': True, 'protocol': 'responses-lite'},
    ],
}


class Providers(unittest.TestCase):
    def test_valid_file_becomes_routes(self):
        parsed = guardctl.parse_providers(copy.deepcopy(GOOD))
        first, second = parsed['routes']
        self.assertEqual((first['route_id'], first['native_alias'], first['display_name'], first['protocol_profile'], first['image_input']),
                         ('my-llm-big-model-v2', 'tp-my-llm-big-model-v2', 'Big Model', 'responses-full', True))
        self.assertEqual((second['display_name'], second['protocol_profile']), ('mm-1', 'responses-lite'))
        self.assertEqual(parsed['auto_fallback_id'], 'minimax-mm-1')
        self.assertEqual(parsed['providers']['my-llm'], 'https://llm.example.net/v1/responses')
        # No model marked: the first one becomes the automatic fallback.
        unmarked = copy.deepcopy(GOOD)
        del unmarked['models'][1]['auto_fallback']
        self.assertEqual(guardctl.parse_providers(unmarked)['auto_fallback_id'], 'my-llm-big-model-v2')

    def test_mistakes_are_rejected_with_a_message(self):
        def broken(change):
            data = copy.deepcopy(GOOD)
            change(data)
            with self.assertRaises(SystemExit) as caught:
                guardctl.parse_providers(data)
            self.assertIn('ERROR', str(caught.exception))
        broken(lambda d: d['providers']['my-llm'].update(endpoint='http://llm.example.net/v1/responses'))    # not https
        broken(lambda d: d['providers']['my-llm'].update(endpoint='https://k:v@llm.example.net/v1/responses'))  # key in URL
        broken(lambda d: d['providers'].update({'OpenAI': {'endpoint': 'https://x.net/v1/responses'}}))       # bad id
        broken(lambda d: d['providers'].update({'openai': {'endpoint': 'https://x.net/v1/responses'}}))       # reserved id
        broken(lambda d: d['providers'].update({'unused': {'endpoint': 'https://x.net/v1/responses'}}))       # no model
        broken(lambda d: d['models'][0].update(provider='missing'))
        broken(lambda d: d['models'][0].pop('context_window'))
        broken(lambda d: d['models'][0].update(protocol='chat-completions'))
        broken(lambda d: d['models'][0].update(auto_fallback=True))                                            # two fallbacks
        broken(lambda d: d['models'].append(dict(d['models'][0])))                                             # duplicate
        broken(lambda d: d.update(port=80))
        broken(lambda d: d.update(models=[]))

    def test_shipped_example_is_valid_json_but_refused_until_edited(self):
        example = json.loads((guardctl.HERE / 'providers.example.json').read_text(encoding='utf-8'))
        with self.assertRaises(SystemExit) as caught:
            guardctl.parse_providers(example)
        self.assertIn('placeholders', str(caught.exception))


class RouteChanges(unittest.TestCase):
    def test_models_may_be_added_but_not_removed_or_refallbacked(self):
        parsed = guardctl.parse_providers(copy.deepcopy(GOOD))
        bound = {'route_specs': [{'route_id': r['route_id'], 'provider_id': r['provider_id'], 'actual_model': r['actual_model']}
                                 for r in parsed['routes']], 'auto_fallback_id': parsed['auto_fallback_id']}
        guardctl.check_additive(bound, parsed)                               # unchanged
        more = copy.deepcopy(GOOD)
        more['models'].append({'provider': 'my-llm', 'model': 'extra', 'context_window': 64000})
        guardctl.check_additive(bound, guardctl.parse_providers(more))       # added
        fewer = copy.deepcopy(GOOD)
        del fewer['models'][0]
        del fewer['providers']['my-llm']
        with self.assertRaises(SystemExit) as caught:
            guardctl.check_additive(bound, guardctl.parse_providers(fewer))  # removed
        self.assertIn('my-llm/Big-Model.v2', str(caught.exception))
        moved = copy.deepcopy(GOOD)
        moved['models'][0]['auto_fallback'], moved['models'][1]['auto_fallback'] = True, False
        with self.assertRaises(SystemExit):
            guardctl.check_additive(bound, guardctl.parse_providers(moved))  # different automatic fallback


class Repair(unittest.TestCase):
    def test_only_the_codex_runtime_is_repinned_automatically(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            files = {name: folder / name for name in ('codex.exe', 'dep.py', 'python.exe', 'code.py')}
            for name, path in files.items():
                path.write_bytes(name.encode())
            config = {'native_runtime': {'path': str(files['codex.exe']), 'sha256': guardctl.sha(files['codex.exe'])},
                      'external_files': {str(files['dep.py']): guardctl.sha(files['dep.py'])},
                      'python_runtime': {'path': str(files['python.exe']), 'sha256': guardctl.sha(files['python.exe'])},
                      'package_root': str(folder), 'files': {'code.py': guardctl.sha(files['code.py'])}}
            self.assertEqual(guardctl.changed_pins(config), (False, []))
            files['codex.exe'].write_bytes(b'updated by the Store')                      # a Codex Desktop update
            self.assertEqual(guardctl.changed_pins(config), (True, []))
            files['codex.exe'].unlink()                                                 # the old package folder is gone
            self.assertEqual(guardctl.changed_pins(config), (True, []))
            files['codex.exe'].write_bytes(b'codex.exe')
            for name in ('dep.py', 'python.exe', 'code.py'):                            # anything else must not be auto-trusted
                files[name].write_bytes(b'tampered')
                native, others = guardctl.changed_pins(config)
                self.assertEqual((native, len(others)), (False, 1), name)
                files[name].write_bytes(name.encode())


class Release(unittest.TestCase):
    def test_release_is_runtime_only_and_stable(self):
        with tempfile.TemporaryDirectory() as folder:
            first = guardctl.build_release(Path(folder))
            names = sorted(p.relative_to(first).as_posix() for p in first.rglob('*') if p.is_file())
            self.assertIn('continuity-v4/production_service_v4.py', names)
            self.assertIn('continuity-v4/credentials.py', names)
            self.assertFalse([n for n in names if '/test_' in n or n.endswith('.pyc')])
            self.assertEqual(guardctl.build_release(Path(folder)), first)  # same source, same immutable folder


if __name__ == '__main__':
    unittest.main()
