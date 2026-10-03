"""Fall Cloud key sync with the web server. No redirects, proxies or logging of keys."""

import json
import urllib.error
import urllib.request

from malbut_agent_server.adapters.outbound.homecam_fall_events import (
    FallUploadError, HomecamFallEventClient,
)
from malbut_agent_server.adapters.outbound.ollama_cloud_fall import valid_cloud_key


class HomecamFallKeyClient(HomecamFallEventClient):
    """Same origin, token and transport rules as the fall event uploader."""

    def __init__(self, **options):
        super().__init__(**options)
        self._url = self._url.rsplit('/api/', 1)[0] + '/api/device/v1/fall-cloud-key'

    def sync(self, known_version, model):
        """Return (key_version, changed, api_key); api_key None with changed=True means deleted."""
        data = json.dumps(dict(knownVersion=known_version, model=model),
                          separators=(',', ':')).encode()
        request = urllib.request.Request(self._url, data=data, method='POST', headers={
            'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json',
            'X-Malbut-Device-Id': self._device_id})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body = response.read(8193)
                if response.status != 200 or len(body) > 8192:
                    raise FallUploadError('invalid_ack')
                result = json.loads(body)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise FallUploadError(f'http_{code}' if code in {400, 401, 403, 429, 503}
                                  else 'upload_failed') from None
        except FallUploadError:
            raise
        except Exception:
            raise FallUploadError() from None
        if (not isinstance(result, dict) or set(result) != {'keyVersion', 'changed', 'apiKey'}
                or type(result['keyVersion']) is not int or result['keyVersion'] < 0
                or type(result['changed']) is not bool
                or not (result['apiKey'] is None or valid_cloud_key(result['apiKey']))
                or (result['apiKey'] is not None and not result['changed'])):
            raise FallUploadError('invalid_ack')
        return result['keyVersion'], result['changed'], result['apiKey']
