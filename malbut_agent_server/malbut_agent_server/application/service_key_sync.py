"""Keep the robot's OpenAI (대화·목소리) and KMA (날씨) key files in step with the web.

Rules (agreed 2026-10-06, as for the fall Cloud key):
- The server never had a key (version 0): leave the files alone; clients keep using
  the team key from the environment.
- A newer server version replaces the key file; a server delete removes the key and
  records the deletion, so clients have no key (not the team key) afterwards.
- A failed request is not a delete: keep using the last key.

Clients read the files on use (service_keys.ManagedKey), so nothing is restarted.
"""

import json
import logging
from pathlib import Path

from malbut_agent_server.application.fall_key_sync import write_private_file
from malbut_agent_server.service_keys import key_paths, read_version

LOG = logging.getLogger(__name__)
SERVICES = ('openai', 'kma')


class ServiceKeySync:
    def __init__(self, *, client, directory, model=None):
        self._client, self._dir, self._model = client, Path(directory), model
        self.failures = 0
        self.last_error = None

    def known(self):
        """Versions held; a version whose key file went missing is asked for again."""
        held = {}
        for service in SERVICES:
            key_file, version_file = key_paths(service, self._dir)
            version, deleted = read_version(version_file)
            held[service] = version if deleted or key_file.exists() else 0
        return held

    def fetch(self, health):
        """Blocking HTTP; run off the event loop. Returns the server reply or None."""
        try:
            reply = self._client.sync(self.known(), self._model, health)
        except Exception as error:  # noqa: BLE001 - any failure keeps the last keys
            self.failures += 1
            code = getattr(error, 'code', 'upload_failed')
            if code != self.last_error:
                # Codes only; never a key, token or response body.
                LOG.warning('service key sync failed: %s', code)
            self.last_error = code
            return None
        if self.last_error is not None:
            LOG.info('service key sync recovered')
        self.last_error = None
        return reply

    def apply(self, reply):
        """Write what changed; returns {service: kept|unchanged|replaced|deleted|write_failed}."""
        result = {}
        if reply is None:
            return {service: 'kept' for service in SERVICES}
        held = self.known()
        for service in SERVICES:
            version, changed, api_key = reply[service]
            if version == 0 or not changed or version == held[service]:
                result[service] = 'unchanged'
                continue
            key_file, version_file = key_paths(service, self._dir)
            try:
                self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                if api_key is None:
                    try:
                        key_file.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    write_private_file(key_file, (api_key + '\n').encode('ascii'))
                # The version is written last: after a crash the next sync fetches again.
                write_private_file(version_file, json.dumps(
                    {'keyVersion': version, 'deleted': api_key is None}).encode())
            except OSError:
                LOG.warning('service key write failed: %s', service)
                result[service] = 'write_failed'
                continue
            result[service] = 'deleted' if api_key is None else 'replaced'
        return result
