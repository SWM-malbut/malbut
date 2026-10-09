"""Read-only Homecam tools in the Agent process, independent of ROS Manager."""

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from malbut_agent_server.gateway import CapabilityRegistry, PROPOSAL_ONLY, ToolCapability
from malbut_agent_server.tools import HOMECAM_QUERY_TOOLS, TOOL_SPECS, validate_tool_arguments


_OPERATIONS = {name: name.removeprefix('get_') for name in HOMECAM_QUERY_TOOLS}
_FIELDS = {
    'get_homecam_status': {
        'cameraEnabled', 'microphoneEnabled', 'monitoringEnabled', 'fallEnabled',
        'cloudConsent', 'settingsRevision', 'mediaSettingsRevision', 'lastSeenAt',
        'p2pHealthy', 'storageHealthy', 'detectorHealthy', 'runtimeVerified',
        'mediaApplyReceipt', 'fallApplyReceipt', 'href',
    },
    'get_homecam_events': {'events', 'href'},
    'get_homecam_recordings': {'recordings', 'href'},
    'get_homecam_falls': {'incidents', 'href'},
}
_ITEM_FIELDS = {
    'events': {'id', 'eventType', 'confidence', 'occurredAt', 'recordingId', 'href'},
    'recordings': {'id', 'startedAt', 'endedAt', 'href'},
    'incidents': {'id', 'state', 'assessment', 'answer', 'fallSeen',
                  'occurredAt', 'updatedAt', 'href'},
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HomecamQueryClient:
    """Fixed queries only; owner delegation is checked by the existing API."""

    def __init__(self, base_url, token_file, *, opener=None):
        origin = urllib.parse.urlsplit(base_url)
        if (origin.scheme != 'https' or not origin.hostname
                or origin.username or origin.password or origin.port not in {None, 443}
                or origin.path not in {'', '/'} or origin.query or origin.fragment):
            raise ValueError('Homecam requires an HTTPS origin')
        self._url = base_url.rstrip('/') + '/api/device/v1/agent/operate'
        self._token_file = Path(token_file).expanduser()
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
        )

    @classmethod
    def from_env(cls, environ=None):
        environ = os.environ if environ is None else environ
        origin = (environ.get('HOMECAM_BACKEND_URL') or '').strip()
        token_file = (environ.get('HOMECAM_DEVICE_TOKEN_FILE') or '').strip()
        if not origin or not token_file:
            return None
        try:
            return cls(origin, token_file)
        except ValueError:
            return None

    def __call__(self, tool_name, arguments):
        from malbut_agent_server.fall_upload_worker import _read_token

        if tool_name not in _OPERATIONS:
            raise ValueError('unsupported Homecam query')
        validate_tool_arguments(tool_name, arguments)
        args = dict(arguments)
        if 'event_type' in args:
            kind = args.pop('event_type')
            if kind is not None:
                args['eventType'] = kind
        token = _read_token(self._token_file)
        if not token or any(char.isspace() for char in token):
            raise ValueError('invalid device token')
        # A new user query always gets fresh data, not the API's old receipt.
        payload = json.dumps({
            'requestId': uuid4().hex, 'operation': _OPERATIONS[tool_name], 'arguments': args,
        }).encode()
        request = urllib.request.Request(self._url, data=payload, method='POST', headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        })
        try:
            response = self._opener.open(request, timeout=8.0)
        except urllib.error.HTTPError as error:
            if error.code not in {401, 403}:
                error.close()
                raise RuntimeError('Homecam query unavailable') from None
            response = error
        with response as opened:
            body = opened.read(65537)
        if len(body) > 65536:
            raise ValueError('Homecam response too large')
        value = json.loads(body)
        if (not isinstance(value, dict) or type(value.get('success')) is not bool
                or not isinstance(value.get('result'), dict)):
            raise ValueError('invalid Homecam response')
        if not value['success']:
            code = value.get('code')
            return {'success': False, 'code': code if code in {
                'VOICE_DELEGATION_REQUIRED', 'UNAUTHORIZED', 'VOICE_CREDENTIAL_REVOKED',
            } else 'UNAVAILABLE', 'result': {}}
        result = {key: item for key, item in value['result'].items()
                  if key in _FIELDS[tool_name]}
        for key, fields in _ITEM_FIELDS.items():
            if key in result:
                if not isinstance(result[key], list):
                    raise ValueError('invalid Homecam list')
                result[key] = [{k: v for k, v in item.items() if k in fields}
                               for item in result[key][:arguments['limit']]
                               if isinstance(item, dict)]
        for key in ('mediaApplyReceipt', 'fallApplyReceipt'):
            if key in result and isinstance(result[key], dict):
                result[key] = {k: v for k, v in result[key].items()
                               if k in {'state', 'reason', 'runtimeVerified', 'reportAgeS',
                                        'fresh', 'applied', 'receivedAt'}}
        # Bound current-turn context; never pass raw HTTP errors or credentials.
        if len(json.dumps(result, ensure_ascii=False)) > 8192:
            raise ValueError('Homecam result too large for dialogue')
        return {'success': True, 'code': 'OK', 'result': result}


def configure_homecam_queries(runtime, client):
    """Enable API tools only when a trusted client is configured."""
    runtime.homecam_executor = client
    runtime.homecam_query_tools = HOMECAM_QUERY_TOOLS if client is not None else ()
    registry = runtime.capability_registry
    entries = []
    for name in TOOL_SPECS:
        entry = registry.get(name)
        if name in HOMECAM_QUERY_TOOLS:
            entry = ToolCapability(name, mode=PROPOSAL_ONLY, available=client is not None,
                                   timeout_seconds=10.0)
        if entry is not None:
            entries.append(entry)
    runtime.capability_registry = CapabilityRegistry(
        entries, runtime_mode=registry.runtime_mode, revision=registry.revision,
    )
    return runtime
