"""OpenAI/KMA key sync with the web server. No redirects, proxies or logging of keys."""

import json
import re
import urllib.error
import urllib.request

from malbut_agent_server.adapters.outbound.homecam_fall_events import (
    FallUploadError, HomecamFallEventClient,
)
from malbut_agent_server.service_keys import HEALTH_STATES, valid_key

SERVICES = ('openai', 'kma')
_MODEL = re.compile(r'[A-Za-z0-9_.:-]{1,100}')
_CODE = re.compile(r'[a-z0-9_]{1,64}')


class HomecamServiceKeyClient(HomecamFallEventClient):
    """Same origin, token and transport rules as the fall event uploader."""

    def __init__(self, **options):
        super().__init__(**options)
        self._url = self._url.rsplit('/api/', 1)[0] + '/api/device/v1/service-keys'

    def sync(self, known, model, health):
        """known {openai: n, kma: n}, the OpenAI model, health {service: (state, code)}
        → {service: (keyVersion, changed, apiKey)}; apiKey None with changed=True means deleted."""
        body = {
            'known': {name: int(known.get(name, 0)) for name in SERVICES},
            'models': {'openai': model if isinstance(model, str) and _MODEL.fullmatch(model) else None},
            'health': {
                name: {'state': state, 'code': code if isinstance(code, str) and _CODE.fullmatch(code) else None}
                for name, (state, code) in health.items() if state in HEALTH_STATES
            },
        }
        request = urllib.request.Request(self._url, data=json.dumps(body, separators=(',', ':')).encode(),
                                         method='POST', headers={
                                             'Authorization': 'Bearer ' + self._token,
                                             'Content-Type': 'application/json',
                                             'X-Malbut-Device-Id': self._device_id})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(8193)
                if response.status != 200 or len(raw) > 8192:
                    raise FallUploadError('invalid_ack')
                result = json.loads(raw)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise FallUploadError(f'http_{code}' if code in {400, 401, 403, 404, 429, 503}
                                  else 'upload_failed') from None
        except FallUploadError:
            raise
        except Exception:
            raise FallUploadError() from None
        if not isinstance(result, dict) or set(result) != set(SERVICES):
            raise FallUploadError('invalid_ack')
        reply = {}
        for name in SERVICES:
            item = result[name]
            if (not isinstance(item, dict) or set(item) != {'keyVersion', 'changed', 'apiKey'}
                    or type(item['keyVersion']) is not int or item['keyVersion'] < 0
                    or type(item['changed']) is not bool
                    or not (item['apiKey'] is None or valid_key(item['apiKey']))
                    or (item['apiKey'] is not None and not item['changed'])):
                raise FallUploadError('invalid_ack')
            reply[name] = (item['keyVersion'], item['changed'], item['apiKey'])
        return reply
