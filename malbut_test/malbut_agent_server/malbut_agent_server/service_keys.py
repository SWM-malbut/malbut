"""The OpenAI (대화·목소리) and KMA (날씨) keys the owner sets on the web.

Rules (agreed 2026-10-06, the same as the fall Cloud key):
- No version file: the web never managed this key. Use the key from the
  environment (the team key exported before bringup), exactly as before.
- A version file: the web manages it. Use the key file; when the owner deleted
  the key there is no key at all, never the team key again.

Keys are read on use and cached by file change time, so a key that `key_sync`
writes is used from the next request without restarting anything. The key is
never logged; only how it did ("health") is shared, as a short code.
"""

import json
import os
import re
import threading
from functools import lru_cache
from pathlib import Path

DEFAULT_KEY_DIR = '~/.config/malbut/keys'
ENV_NAMES = {'openai': 'OPENAI_API_KEY', 'kma': 'KMA_SERVICE_KEY'}
HEALTH_STATES = ('ok', 'missing', 'invalid', 'quota')
_CODE = re.compile(r'[a-z0-9_]{1,64}')


def key_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get('MALBUT_KEY_DIR') or DEFAULT_KEY_DIR).expanduser()


def key_paths(service, directory):
    if service not in ENV_NAMES:
        raise ValueError('unknown key service')
    directory = Path(directory)
    return directory / f'{service}.key', directory / f'{service}.key.version'


def read_version(version_file):
    """The web version the files hold, and whether the owner deleted the key."""
    try:
        value = json.loads(Path(version_file).read_text())
    except (OSError, ValueError):
        return 0, False
    version = value.get('keyVersion') if isinstance(value, dict) else None
    if type(version) is not int or version < 0:
        return 0, False
    return version, value.get('deleted') is True


def valid_key(text):
    return isinstance(text, str) and 8 <= len(text) <= 1024 and all(33 <= ord(c) <= 126 for c in text)


def key_value(source):
    """A plain string, or the current value of a ManagedKey."""
    return source if isinstance(source, str) else source.current()


class ManagedKey:
    """The key one service uses right now, and how it last did."""

    def __init__(self, service, *, environ=None, directory=None):
        self.service = service
        self._env_name = ENV_NAMES[service]
        self._environ = os.environ if environ is None else environ
        self._key_file, self._version_file = key_paths(
            service, key_dir(self._environ) if directory is None else directory)
        self._lock = threading.Lock()
        self._stamp = object()
        self._value = ''
        self._generation = 0
        self._health = None
        self._listeners = []

    @staticmethod
    def _stat(path):
        try:
            info = path.stat()
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size, info.st_ino

    def _resolve(self, stamp):
        if stamp[0] is None:
            # Not managed by the web: the team key, as before.
            return (self._environ.get(self._env_name) or '').strip()
        if stamp[1] is None:
            return ''
        try:
            text = self._key_file.read_text(encoding='ascii').strip()
        except (OSError, UnicodeDecodeError):
            return ''
        return text if valid_key(text) else ''

    def current(self):
        stamp = (self._stat(self._version_file), self._stat(self._key_file),
                 self._environ.get(self._env_name))
        with self._lock:
            if stamp != self._stamp:
                value = self._resolve(stamp)
                if value != self._value:
                    self._generation += 1
                self._value, self._stamp = value, stamp
            return self._value

    @property
    def generation(self):
        """Changes whenever the key in use changes (to reset a failure circuit)."""
        self.current()
        return self._generation

    @property
    def managed(self):
        return self._version_file.exists()

    def report(self, state, code=None):
        """How the key just did. Listeners hear only changes."""
        if state not in HEALTH_STATES:
            raise ValueError('unknown key health state')
        code = code if isinstance(code, str) and _CODE.fullmatch(code) else None
        with self._lock:
            if self._health == (state, code):
                return
            self._health = (state, code)
            listeners = list(self._listeners)
        for listener in listeners:
            listener(self.service, state, code)

    @property
    def health(self):
        return self._health

    def add_listener(self, listener):
        with self._lock:
            self._listeners.append(listener)
            health = self._health
        if health is not None:
            listener(self.service, *health)

    def remove_listener(self, listener):
        """Release an observer when its owning runtime stops."""
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def __repr__(self):
        return f'ManagedKey(service={self.service!r}, key=<redacted>)'


@lru_cache(maxsize=None)
def _shared(service, directory):
    return ManagedKey(service, directory=directory)


def shared_key(service, environ=None):
    """One ManagedKey per service and key folder in this process, so every
    client shares the same key, health and change counter."""
    return _shared(service, str(key_dir(environ)))
