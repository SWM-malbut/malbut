"""Keep the fall Cloud key in step with the owner's key on the web server.

Rules (agreed 2026-10-03):
- The server never had a key (version 0): keep the robot's own key file.
- A newer server version replaces the key file; a server delete removes it.
- A failed request is not a delete: keep using the last key.

The key is written atomically (0600, same directory) and handed to the
provider without a restart. The key itself is never logged.
"""

import json
import logging
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)


def write_private_file(path: Path, data: bytes):
    """Atomic 0600 write next to the target; the rename is durable before returning."""
    # Unique 0600 temp name: a file left by a crash never blocks the next write.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)  # make the rename durable before the version file
    finally:
        os.close(directory)


SYNCED_KEY_NAME = 'fall-cloud.key'


def key_sync_plan(settings, environ, *, directory_ok):
    """How this robot gets the owner's fall Cloud key (2026-10-14).

    An explicit ``key_sync`` in the config wins, with its key file as before.
    Otherwise the robot's web settings (HOMECAM_BACKEND_URL and
    HOMECAM_DEVICE_TOKEN_FILE, which the uploader already uses) turn sync on,
    so a robot set up from git needs no extra config. The synced key goes
    where only this account writes: the configured key file's directory if it
    qualifies, else next to the journal (a 0700 directory of this account).
    Until the owner registers a key the configured key file is used; once the
    web manages it (a version file exists), only the synced key is.

    Returns (sync settings or None, file the sync writes, file read at start).
    """
    from malbut_agent_server.fall_runtime import FallKeySyncSettings

    if settings.key_sync is not None:
        return settings.key_sync, settings.cloud_key_file, settings.cloud_key_file
    backend = environ.get('HOMECAM_BACKEND_URL', '').strip()
    token = environ.get('HOMECAM_DEVICE_TOKEN_FILE', '').strip()
    host = urlsplit(backend).hostname if backend else None
    if not host or not token:
        return None, settings.cloud_key_file, settings.cloud_key_file
    try:
        sync = FallKeySyncSettings.parse(
            {'base_url': backend, 'allow_hosts': [host], 'token_file': token})
    except ValueError:
        return None, settings.cloud_key_file, settings.cloud_key_file
    synced = (settings.cloud_key_file if directory_ok(settings.cloud_key_file)
              else settings.journal_path.parent / SYNCED_KEY_NAME)
    managed = synced.with_name(synced.name + '.version').exists()
    return sync, synced, synced if managed else settings.cloud_key_file


class FallCloudKeySync:
    def __init__(self, *, client, key_file: Path, model: str, apply_key):
        self._client, self._key_file, self._model = client, Path(key_file), model
        self._apply = apply_key
        self._version_file = self._key_file.with_name(self._key_file.name + '.version')
        # A version without its key file (removed by hand, failed restore) would
        # never be resent: ask the server again from scratch.
        self.key_version = self._read_version() if self._key_file.exists() else 0
        self.failures = 0
        self.last_error = None

    def _read_version(self):
        try:
            value = json.loads(self._version_file.read_text())
            version = value.get('keyVersion') if isinstance(value, dict) else None
            return version if type(version) is int and version >= 0 else 0
        except (OSError, ValueError):
            return 0

    def _write(self, path: Path, data: bytes):
        write_private_file(path, data)

    def _record_error(self, code):
        self.failures += 1
        if code != self.last_error:
            # Codes only; never the key, token or response body.
            LOG.warning('fall cloud key sync failed: %s', code)
        self.last_error = code

    def fetch(self):
        """Blocking HTTP; run off the event loop. Returns the server reply or None on failure."""
        try:
            reply = self._client.sync(self.key_version, self._model)
        except Exception as error:  # noqa: BLE001 - any failure keeps the last key
            self._record_error(getattr(error, 'code', 'upload_failed'))
            return None
        if self.last_error is not None:
            LOG.info('fall cloud key sync recovered')
        self.last_error = None
        return reply

    def apply(self, reply):
        """Apply a fetched reply on the caller's thread; returns what changed."""
        if reply is None:
            return 'kept'
        version, changed, api_key = reply
        if version == 0 or not changed or version == self.key_version:
            return 'unchanged'
        try:
            if api_key is None:
                try:
                    self._key_file.unlink()
                except FileNotFoundError:
                    pass
                result = 'deleted'
            else:
                self._write(self._key_file, (api_key + '\n').encode('ascii'))
                result = 'replaced'
            # The version is written last: after a crash the next sync fetches again.
            self._write(self._version_file, json.dumps({'keyVersion': version}).encode())
        except OSError:
            # Keep running with the current key; the next sync retries.
            self._record_error('key_write_failed')
            return 'write_failed'
        self.key_version = version
        self._apply(api_key)
        return result
