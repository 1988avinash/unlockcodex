"""Provider API keys, encrypted for the current Windows user with DPAPI.

Keys never appear in configuration, logs or the repository. Each provider id
has one file, <install root>/secrets/<provider>.key; only the Windows account
that stored it can decrypt it. The service sets DIRECTORY once at startup.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re

DIRECTORY = Path(os.environ.get('LOCALAPPDATA', '.')) / 'CodexContinuityGuard' / 'secrets'


class CredentialUnavailable(ValueError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_char))]


def _dpapi(function, raw, provider):
    crypt, kernel = ctypes.WinDLL('crypt32', use_last_error=True), ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    entropy = ('codex-continuity-guard:' + provider).encode()  # binds a file to its provider id
    source, salt, result = (_Blob(len(b), ctypes.cast(ctypes.create_string_buffer(b, len(b)), ctypes.POINTER(ctypes.c_char)))
                            for b in (raw, entropy, b''))
    call = getattr(crypt, function)
    call.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                     wintypes.DWORD, ctypes.POINTER(_Blob)]
    call.restype = wintypes.BOOL
    if not call(ctypes.byref(source), None, ctypes.byref(salt), None, None, 1, ctypes.byref(result)):  # 1 = no UI
        raise CredentialUnavailable('Windows could not protect/unprotect the key for provider ' + provider)
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def _path(provider):
    if not isinstance(provider, str) or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', provider):
        raise CredentialUnavailable('Invalid provider id')
    return Path(DIRECTORY) / (provider + '.key')


def store(provider, key):
    if not isinstance(key, str) or not key.strip() or '\r' in key or '\n' in key:
        raise CredentialUnavailable('A single-line API key is required')
    path = _path(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_dpapi('CryptProtectData', key.strip().encode(), provider))


def load(provider):
    path = _path(provider)
    if not path.is_file():
        raise CredentialUnavailable(f'No API key stored for provider "{provider}"; run: guardctl set-key {provider}')
    return _dpapi('CryptUnprotectData', path.read_bytes(), provider).decode()


def demo():
    import tempfile
    global DIRECTORY
    saved = DIRECTORY
    with tempfile.TemporaryDirectory() as folder:
        DIRECTORY = Path(folder)
        try:
            store('example', ' sk-test-123 ')
            assert load('example') == 'sk-test-123'
            assert b'sk-test-123' not in _path('example').read_bytes()
            (Path(folder) / 'other.key').write_bytes(_path('example').read_bytes())
            try:
                load('other')
                raise AssertionError('a key file must not decrypt under another provider id')
            except CredentialUnavailable:
                pass
            try:
                load('missing')
                raise AssertionError('missing key must fail')
            except CredentialUnavailable:
                pass
        finally:
            DIRECTORY = saved
    print('credentials self-check ok')


if __name__ == '__main__':
    demo()
