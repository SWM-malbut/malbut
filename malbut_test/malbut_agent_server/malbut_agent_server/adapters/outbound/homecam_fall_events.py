"""Metadata upload worker. Run outside the ROS/Cloud inference event loop.

The web service acknowledges durable storage, not push delivery. No redirects,
proxy inheritance, automatic fresh IDs or Cloud-model calls are allowed here.
"""

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from malbut_agent_server.domain.fall_monitoring import identifier, positive


class FallUploadError(RuntimeError):
    def __init__(self, code='upload_failed', *, blocked=False):
        super().__init__(code)
        self.code, self.blocked = code, blocked


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HomecamFallEventClient:
    def __init__(self, *, base_url, device_id, device_token, allowed_hosts, timeout_s=10):
        identifier(device_id)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', device_id):
            raise ValueError('invalid device identifier')
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme != 'https' or parsed.hostname not in allowed_hosts
                or parsed.username or parsed.password or parsed.port not in {None, 443}
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
            raise ValueError('approved HTTPS origin required')
        if (not isinstance(device_token, str) or not device_token
                or any(c.isspace() for c in device_token)):
            raise ValueError('invalid device token')
        positive(timeout_s, 'timeout_s')
        self._url = base_url.rstrip('/') + '/api/device/v1/fall-events'
        self._token, self._timeout = device_token, timeout_s
        self._device_id = device_id
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def store(self, payload):
        data = payload.encode('utf-8')
        if len(data) > 8192:
            raise FallUploadError('http_413', blocked=True)
        event_id = json.loads(payload)['eventId']
        request = urllib.request.Request(self._url, data=data, method='POST', headers={
            'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json',
            'X-Malbut-Device-Id': self._device_id})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body = response.read(8193)
                if response.status not in {200, 201} or len(body) > 8192:
                    raise FallUploadError('invalid_ack')
                result = json.loads(body)
                if (not isinstance(result, dict) or result.get('stored') is not True
                        or result.get('eventId') != event_id):
                    raise FallUploadError('invalid_ack')
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise FallUploadError(
                f'http_{code}' if code in {400, 401, 403, 409, 413, 429, 503} else 'upload_failed',
                blocked=code in {400, 401, 403, 409, 413}) from None
        except FallUploadError:
            raise
        except Exception:
            raise FallUploadError() from None


class FallEventUploader:
    def __init__(self, journal, client):
        self._journal, self._client = journal, client
        self._lock = threading.Lock()

    def run_once(self):
        if not self._lock.acquire(blocking=False):
            return False
        try:
            pending = self._journal.pending()
            if pending is None:
                return False
            try:
                self._client.store(pending['payload'])
            except FallUploadError as error:
                self._journal.failed(pending['event_id'], code=error.code, blocked=error.blocked)
            except Exception:
                self._journal.failed(pending['event_id'], code='upload_failed')
            else:
                self._journal.acknowledge(pending['event_id'])
            return True
        finally:
            self._lock.release()
