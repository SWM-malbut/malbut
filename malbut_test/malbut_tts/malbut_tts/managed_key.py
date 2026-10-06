"""The OpenAI key the owner sets on the web, read on every utterance (SWM25-235).

The same rules as malbut_agent_server.service_keys (this package does not
depend on it):
- No version file: the web never managed the key. Use OPENAI_API_KEY from the
  environment (the team key), exactly as before.
- A version file: the web manages it. Use the key file; when the owner deleted
  the key there is no key at all, never the team key again.

The key is never logged; only how it did ("health") is shared, as a short code.
"""

import os
from pathlib import Path
import re
import threading

DEFAULT_KEY_DIR = '~/.config/malbut/keys'
HEALTH_STATES = ('ok', 'missing', 'invalid', 'quota')
_CODE = re.compile(r'[a-z0-9_]{1,64}')


def key_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get('MALBUT_KEY_DIR') or DEFAULT_KEY_DIR).expanduser()


def _valid_key(text):
    return 8 <= len(text) <= 1024 and all(33 <= ord(c) <= 126 for c in text)


class OpenAIKey:
    """The OpenAI key to use right now, and how it last did.

    Utterances are seconds apart, so the files are simply read each time.
    """

    service = 'openai'

    def __init__(self, *, environ=None, directory=None):
        self._environ = os.environ if environ is None else environ
        directory = key_dir(self._environ) if directory is None else Path(directory)
        self._key_file = directory / 'openai.key'
        self._version_file = directory / 'openai.key.version'
        self._lock = threading.Lock()
        self._health = None
        self._listeners = []

    def current(self):
        if not self._version_file.exists():
            # Not managed by the web: the team key, as before.
            return (self._environ.get('OPENAI_API_KEY') or '').strip()
        try:
            text = self._key_file.read_text(encoding='ascii').strip()
        except (OSError, UnicodeDecodeError):
            return ''
        return text if _valid_key(text) else ''

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

    def __repr__(self):
        return 'OpenAIKey(key=<redacted>)'
