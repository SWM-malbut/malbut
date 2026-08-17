"""
Authenticated Homecam semantic adapter for ``monitor_room``.

The adapter deliberately accepts a server-configured user and device only.
It does not reuse browser cookies, follow redirects, honor proxy variables,
or let an LLM choose an origin, principal, or device.
"""

import copy
import http.client
import hashlib
import hmac
import ipaddress
import json
import math
import re
import ssl
import time
from dataclasses import InitVar, dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol
from urllib.parse import urlsplit

from malbut_agent_server.monitor_room_target import (
    MAX_SNAPSHOT_BYTES,
    Effects,
    TargetBinding,
    TrustedSemanticSnapshot,
    parse_trusted_semantic_snapshot,
    resolve_monitor_room_target,
)
from malbut_agent_server.schemas import (
    ValidationError,
    validate_user_id,
)


HOME_CAM_SEMANTIC_SCHEMA_VERSION = 1
HOME_CAM_SEMANTIC_PATH = '/api/internal/agent/semantic'
DEFAULT_HOME_CAM_TIMEOUT_SECONDS = 3
MAX_HOME_CAM_TIMEOUT_SECONDS = 10
# ``semanticsJson`` is embedded as a JSON string, so escaping can expand an
# otherwise valid semantic snapshot.  Keep the transport bounded while
# allowing the parser's full input budget in the worst JSON-escape case.
MAX_HOME_CAM_RESPONSE_BYTES = (MAX_SNAPSHOT_BYTES * 6) + (64 * 1024)
_SAFE_DEVICE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_SAFE_DNS_NAME = re.compile(
    r'^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}'
    r'[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}'
    r'[A-Za-z0-9])?$'
)
_LOWER_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_POSITIVE_DECIMAL = re.compile(r'^[1-9][0-9]{0,18}$')
_SERVER_REVISION = re.compile(r'^srv-([1-9][0-9]{0,18})-[0-9a-f]{16}$')
_AUTHORIZATION_REVISION = re.compile(r'^auth-([1-9][0-9]{0,18})$')
_RESPONSE_FIELD_NAMES = (
    'schemaVersion',
    'issuer',
    'audience',
    'agentUserId',
    'principalSubjectDigest',
    'deviceId',
    'deviceBindingRevision',
    'authorizationRevision',
    'mapGeneration',
    'sourceIsFinalized',
    'issuedAtMs',
    'expiresAtMs',
    'contentSha256',
    'semanticsJson',
    'signature',
)
_RESPONSE_FIELDS = frozenset(_RESPONSE_FIELD_NAMES)
_RESPONSE_STRING_FIELDS = (
    'issuer',
    'audience',
    'agentUserId',
    'principalSubjectDigest',
    'deviceId',
    'deviceBindingRevision',
    'authorizationRevision',
    'mapGeneration',
    'contentSha256',
    'semanticsJson',
    'signature',
)
HOME_CAM_SEMANTIC_ISSUER = 'malbut-homecam-web'
HOME_CAM_SEMANTIC_AUDIENCE = 'malbut-agent-semantic-v1'
MAX_ENVELOPE_LIFETIME_MS = 10_000
MAX_ENVELOPE_FUTURE_SKEW_MS = 2_000
_EVIDENCE_CONSTRUCTION_TOKEN = object()


class HomecamSemanticError(ValidationError):
    """Base fail-closed Homecam semantic adapter error."""

    def __init__(self, code: str, message: str) -> None:
        """Create one stable error without including credentials or URLs."""
        super().__init__(message)
        self.code = code


class HomecamSemanticUnavailableError(HomecamSemanticError):
    """Raised when the authenticated Homecam repository is unavailable."""


class HomecamSemanticBindingError(HomecamSemanticError):
    """Raised when a response is not bound to the configured actor/device."""


def _device_id(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_DEVICE_ID.fullmatch(value):
        raise ValueError('MALBUT_HOMECAM_DEVICE_ID is invalid')
    return value


def _agent_user_id(value: Any) -> str:
    normalized = validate_user_id(value)
    if not _SAFE_DEVICE_ID.fullmatch(normalized):
        raise ValueError('MALBUT_AGENT_USER_ID is invalid for Homecam')
    return normalized


def _service_token(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) < 43
        or len(value) > 512
        or not value.isascii()
        or any(
            ord(character) <= 32 or ord(character) >= 127
            for character in value
        )
    ):
        raise ValueError('MALBUT_HOMECAM_AGENT_TOKEN is invalid')
    return value


def _sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _LOWER_SHA256.fullmatch(value):
        raise ValueError(f'{field_name} is invalid')
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _json_digest(value: Any) -> str:
    """Return the canonical JSON digest for already validated JSON."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ):
        raise ValueError('semantic evidence JSON is invalid') from None
    return hashlib.sha256(encoded).hexdigest()


def _freeze_json(value: Any, depth: int = 0) -> Any:
    """Detach and recursively freeze one validated JSON value."""
    if depth > 32:
        raise ValueError('semantic evidence JSON is invalid')
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('semantic evidence JSON is invalid')
        return value
    if type(value) is list:
        return tuple(_freeze_json(item, depth + 1) for item in value)
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError('semantic evidence JSON is invalid')
        return MappingProxyType({
            key: _freeze_json(item, depth + 1)
            for key, item in value.items()
        })
    raise ValueError('semantic evidence JSON is invalid')


def _mutable_json_copy(value: Any, depth: int = 0) -> Any:
    """Return a plain JSON copy from supported mutable or frozen values."""
    if depth > 32:
        raise ValueError('semantic evidence JSON is invalid')
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('semantic evidence JSON is invalid')
        return value
    if type(value) in {list, tuple}:
        return [
            _mutable_json_copy(item, depth + 1)
            for item in value
        ]
    if type(value) in {dict, MappingProxyType}:
        if any(type(key) is not str for key in value):
            raise ValueError('semantic evidence JSON is invalid')
        return {
            key: _mutable_json_copy(item, depth + 1)
            for key, item in value.items()
        }
    raise ValueError('semantic evidence JSON is invalid')


def normalize_homecam_origin(value: Any) -> str:
    """Return one HTTPS origin with no path, credentials, or IP literal."""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError('MALBUT_HOMECAM_ORIGIN is invalid')
    if not value.isascii() or value != value.strip():
        raise ValueError('MALBUT_HOMECAM_ORIGIN is invalid')
    parsed = urlsplit(value)
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {'', '/'}
    ):
        raise ValueError('MALBUT_HOMECAM_ORIGIN must be an HTTPS origin')
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('MALBUT_HOMECAM_ORIGIN port is invalid') from None
    if port not in {None, 443}:
        raise ValueError('MALBUT_HOMECAM_ORIGIN must use port 443')
    hostname = parsed.hostname.rstrip('.').lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError('MALBUT_HOMECAM_ORIGIN cannot use an IP literal')
    if (
        hostname == 'localhost'
        or hostname.endswith('.localhost')
        or not _SAFE_DNS_NAME.fullmatch(hostname)
    ):
        raise ValueError('MALBUT_HOMECAM_ORIGIN hostname is invalid')
    return f'https://{hostname}'


@dataclass(frozen=True)
class HomecamSemanticConfig:
    """Server-owned binding for one Agent user and one Homecam device."""

    origin: str
    service_token: str = field(repr=False)
    envelope_signing_secret: str = field(repr=False)
    agent_user_id: str
    principal_subject_digest: str = field(repr=False)
    device_id: str
    timeout_seconds: int = DEFAULT_HOME_CAM_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Normalize the fixed origin, credential, actor, and device."""
        object.__setattr__(
            self,
            'origin',
            normalize_homecam_origin(self.origin),
        )
        object.__setattr__(
            self,
            'service_token',
            _service_token(self.service_token),
        )
        object.__setattr__(
            self,
            'envelope_signing_secret',
            _service_token(self.envelope_signing_secret),
        )
        if hmac.compare_digest(
            self.service_token,
            self.envelope_signing_secret,
        ):
            raise ValueError(
                'Homecam service and signing credentials must differ'
            )
        object.__setattr__(
            self,
            'agent_user_id',
            _agent_user_id(self.agent_user_id),
        )
        object.__setattr__(
            self,
            'principal_subject_digest',
            _sha256(
                self.principal_subject_digest,
                'MALBUT_HOMECAM_PRINCIPAL_SUBJECT_DIGEST',
            ),
        )
        object.__setattr__(self, 'device_id', _device_id(self.device_id))
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
            or self.timeout_seconds > MAX_HOME_CAM_TIMEOUT_SECONDS
        ):
            raise ValueError(
                'MALBUT_HOMECAM_TIMEOUT_SECONDS must be from 1 to 10'
            )


@dataclass(frozen=True)
class _SemanticEvidenceCanonicalState:
    """Private detached baseline for current-value revalidation."""

    snapshot: TrustedSemanticSnapshot = field(repr=False)
    content_sha256: str
    map_generation: int
    authorization_generation: int
    expires_at_ms: int
    zones_json: bytes = field(repr=False)


@dataclass(frozen=True)
class VerifiedSemanticSnapshotEvidence:
    """Immutable projection of one currently verified signed snapshot."""

    snapshot: TrustedSemanticSnapshot
    content_sha256: str
    map_generation: int
    authorization_generation: int
    expires_at_ms: int
    _zones: Any = field(repr=False, compare=False, hash=False)
    _construction_token: InitVar[object] = None
    _canonical_state: _SemanticEvidenceCanonicalState = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self, _construction_token: object) -> None:
        """Validate the projection and reject public construction."""
        if _construction_token is not _EVIDENCE_CONSTRUCTION_TOKEN:
            raise TypeError(
                'semantic snapshot evidence must come from its resolver'
            )
        if type(self.snapshot) is not TrustedSemanticSnapshot:
            raise TypeError('snapshot must be TrustedSemanticSnapshot')
        if type(self.content_sha256) is not str or not (
            _LOWER_SHA256.fullmatch(self.content_sha256)
        ):
            raise ValueError('content_sha256 is invalid')
        for name in ('map_generation', 'authorization_generation'):
            value = getattr(self, name)
            if (
                type(value) is not int
                or not _POSITIVE_DECIMAL.fullmatch(str(value))
            ):
                raise ValueError(f'{name} is invalid')
        if type(self.expires_at_ms) is not int or self.expires_at_ms < 1:
            raise ValueError('expires_at_ms is invalid')
        source_revision = self.snapshot.source_revision
        source_match = (
            _SERVER_REVISION.fullmatch(source_revision)
            if type(source_revision) is str
            else None
        )
        if (
            source_match is None
            or source_match.group(1) != str(self.map_generation)
        ):
            raise ValueError('snapshot map generation is invalid')
        zones = _mutable_json_copy(self._zones)
        if zones is not None and type(zones) is not dict:
            raise ValueError('snapshot zones are invalid')
        if _json_digest(zones) != self.snapshot.zones_digest:
            raise ValueError('snapshot zones digest is invalid')
        zones_json = _canonical_json(zones)
        object.__setattr__(self, '_zones', _freeze_json(zones))
        object.__setattr__(
            self,
            '_canonical_state',
            _SemanticEvidenceCanonicalState(
                snapshot=copy.deepcopy(self.snapshot),
                content_sha256=self.content_sha256,
                map_generation=self.map_generation,
                authorization_generation=self.authorization_generation,
                expires_at_ms=self.expires_at_ms,
                zones_json=zones_json,
            ),
        )

    def canonical_copy(self) -> 'VerifiedSemanticSnapshotEvidence':
        """Revalidate current values and return a detached canonical copy."""
        invalid = False
        matches = False
        canonical_snapshot: Any = None
        canonical_zones: Any = None
        state: Any = None
        try:
            state = self._canonical_state
            current_zones = _mutable_json_copy(self._zones)
            matches = (
                type(state) is _SemanticEvidenceCanonicalState
                and type(self.snapshot) is TrustedSemanticSnapshot
                and type(self.content_sha256) is str
                and type(self.map_generation) is int
                and type(self.authorization_generation) is int
                and type(self.expires_at_ms) is int
                and self.snapshot == state.snapshot
                and self.content_sha256 == state.content_sha256
                and self.map_generation == state.map_generation
                and self.authorization_generation
                == state.authorization_generation
                and self.expires_at_ms == state.expires_at_ms
                and _canonical_json(current_zones) == state.zones_json
            )
            if matches:
                canonical_snapshot = copy.deepcopy(state.snapshot)
                canonical_zones = json.loads(
                    state.zones_json.decode('utf-8')
                )
        except (
            AttributeError,
            OverflowError,
            RecursionError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ):
            invalid = True
        if invalid or not matches:
            raise ValueError('semantic snapshot evidence is invalid')
        try:
            return VerifiedSemanticSnapshotEvidence(
                snapshot=canonical_snapshot,
                content_sha256=state.content_sha256,
                map_generation=state.map_generation,
                authorization_generation=state.authorization_generation,
                expires_at_ms=state.expires_at_ms,
                _zones=canonical_zones,
                _construction_token=_EVIDENCE_CONSTRUCTION_TOKEN,
            )
        except (
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            raise ValueError(
                'semantic snapshot evidence is invalid'
            ) from None

    @property
    def zones(self) -> Optional[Mapping[str, Any]]:
        """Return the complete, detached, recursively frozen zones value."""
        return self._zones

    @property
    def expires_at_wall(self) -> float:
        """Return the signed expiry as Unix wall-clock seconds."""
        return self.expires_at_ms / 1000.0


@dataclass(frozen=True)
class _VerifiedSemanticEnvelope:
    """Private result of authenticating one Homecam response envelope."""

    semantics: Mapping[str, Any] = field(repr=False)
    device_binding_revision: str
    content_sha256: str
    map_generation: int
    authorization_generation: int
    expires_at_ms: int


class HomecamSemanticTransport(Protocol):
    """Fetch one service-authenticated semantic envelope."""

    def fetch(
        self,
        *,
        agent_user_id: str,
        principal_subject_digest: str,
        device_id: str,
    ) -> Mapping[str, Any]:
        """Return an untrusted JSON envelope from Homecam."""


class HTTPSHomecamSemanticTransport:
    """Direct HTTPS transport that never follows redirects or uses proxies."""

    def __init__(self, config: HomecamSemanticConfig) -> None:
        """Keep one immutable configuration without opening a connection."""
        if not isinstance(config, HomecamSemanticConfig):
            raise TypeError('config must be HomecamSemanticConfig')
        self._config = config

    def fetch(
        self,
        *,
        agent_user_id: str,
        principal_subject_digest: str,
        device_id: str,
    ) -> Mapping[str, Any]:
        """POST a bounded request to the fixed internal semantic path."""
        if agent_user_id != self._config.agent_user_id:
            raise HomecamSemanticBindingError(
                'homecam_principal_mismatch',
                'Homecam semantic principal is not available',
            )
        if device_id != self._config.device_id:
            raise HomecamSemanticBindingError(
                'homecam_device_mismatch',
                'Homecam semantic device is not available',
            )
        if (
            principal_subject_digest
            != self._config.principal_subject_digest
        ):
            raise HomecamSemanticBindingError(
                'homecam_principal_mismatch',
                'Homecam semantic principal is not available',
            )
        body = json.dumps(
            {
                'schemaVersion': HOME_CAM_SEMANTIC_SCHEMA_VERSION,
                'agentUserId': agent_user_id,
                'principalSubjectDigest': principal_subject_digest,
                'deviceId': device_id,
            },
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        hostname = urlsplit(self._config.origin).hostname
        if hostname is None:
            raise RuntimeError('validated Homecam origin has no hostname')
        connection = None
        failed = False
        response_body = b''
        response_status = 0
        content_type = ''
        content_encoding = ''
        response_oversized = False
        try:
            connection = http.client.HTTPSConnection(
                hostname,
                port=443,
                timeout=self._config.timeout_seconds,
                context=ssl.create_default_context(),
            )
            connection.request(
                'POST',
                HOME_CAM_SEMANTIC_PATH,
                body=body,
                headers={
                    'Accept': 'application/json',
                    'Accept-Encoding': 'identity',
                    'Authorization': (
                        f'Bearer {self._config.service_token}'
                    ),
                    'Connection': 'close',
                    'Content-Length': str(len(body)),
                    'Content-Type': 'application/json',
                },
            )
            response = connection.getresponse()
            response_status = response.status
            content_type = response.getheader('Content-Type', '')
            content_encoding = response.getheader('Content-Encoding', '')
            length_header = response.getheader('Content-Length')
            if length_header is not None:
                try:
                    declared_length = int(length_header)
                except ValueError:
                    declared_length = MAX_HOME_CAM_RESPONSE_BYTES + 1
                if (
                    declared_length < 0
                    or declared_length > MAX_HOME_CAM_RESPONSE_BYTES
                ):
                    response_oversized = True
                else:
                    response_body = response.read(
                        MAX_HOME_CAM_RESPONSE_BYTES + 1
                    )
            else:
                response_body = response.read(
                    MAX_HOME_CAM_RESPONSE_BYTES + 1
                )
        except (OSError, http.client.HTTPException, ssl.SSLError):
            failed = True
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    failed = True
        if failed:
            raise HomecamSemanticUnavailableError(
                'homecam_semantic_unavailable',
                'Homecam semantic source is unavailable',
            )
        if (
            response_status != 200
            or content_type.split(';', 1)[0].strip().lower()
            != 'application/json'
            or content_encoding.strip().lower() not in {'', 'identity'}
            or response_oversized
            or len(response_body) > MAX_HOME_CAM_RESPONSE_BYTES
        ):
            raise HomecamSemanticUnavailableError(
                'homecam_semantic_unavailable',
                'Homecam semantic source is unavailable',
            )
        invalid_json = False
        payload: Any = None
        try:
            payload = json.loads(response_body.decode('utf-8'))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
        ):
            invalid_json = True
        if invalid_json or not isinstance(payload, dict):
            raise HomecamSemanticUnavailableError(
                'homecam_semantic_unavailable',
                'Homecam semantic source is unavailable',
            )
        return payload


def monitor_room_live_effects() -> Effects:
    """Return the one explicit effects profile supported by this slice."""
    return Effects(
        physical_navigation=True,
        camera_capture=True,
        external_video_stream=True,
        video_recording=False,
        audio_capture=False,
        max_duration_seconds=300,
        coverage_mode='whole_room',
        viewer_scope='requesting_user',
        talkback_allowed=False,
    )


class AuthenticatedHomecamSemanticResolver:
    """Resolve a trusted Homecam envelope into one immutable room target."""

    def __init__(
        self,
        config: HomecamSemanticConfig,
        transport: Optional[HomecamSemanticTransport] = None,
        effects: Optional[Effects] = None,
        clock=time.time,
    ) -> None:
        """Build a resolver with a fixed actor, device, and effects policy."""
        if not isinstance(config, HomecamSemanticConfig):
            raise TypeError('config must be HomecamSemanticConfig')
        chosen_transport = transport
        if chosen_transport is None:
            chosen_transport = HTTPSHomecamSemanticTransport(config)
        if not callable(getattr(chosen_transport, 'fetch', None)):
            raise TypeError('transport must provide fetch()')
        chosen_effects = effects or monitor_room_live_effects()
        if not isinstance(chosen_effects, Effects):
            raise TypeError('effects must be Effects')
        self._config = config
        self._transport = chosen_transport
        self._effects = chosen_effects
        if not callable(clock):
            raise TypeError('clock must be callable')
        self._clock = clock

    def _require_request_current(self, request: Any) -> None:
        """Reject stale proposal context before and after remote I/O."""
        issued_at = getattr(request, 'issued_at', None)
        expires_at = getattr(request, 'expires_at', None)
        invalid_time = False
        now = 0.0
        issued = 0.0
        expires = 0.0
        try:
            clock_value = self._clock()
            if (
                isinstance(clock_value, bool)
                or isinstance(issued_at, bool)
                or not isinstance(issued_at, (int, float))
                or isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
            ):
                invalid_time = True
            else:
                now = float(clock_value)
                issued = float(issued_at)
                expires = float(expires_at)
        except (OverflowError, TypeError, ValueError):
            invalid_time = True
        if (
            invalid_time
            or not math.isfinite(now)
            or not math.isfinite(issued)
            or not math.isfinite(expires)
            or issued < 0
            or expires <= issued
            or now < issued
            or now >= expires
        ):
            raise HomecamSemanticBindingError(
                'homecam_request_expired',
                'Homecam semantic request is not current',
            )

    def _snapshot_response_envelope(self, value: Any) -> Mapping[str, Any]:
        """Read every untrusted response field once into a plain dictionary."""
        invalid = False
        envelope = {}
        try:
            if not isinstance(value, Mapping):
                invalid = True
            else:
                keys = tuple(value)
                if (
                    len(keys) != len(_RESPONSE_FIELD_NAMES)
                    or any(type(key) is not str for key in keys)
                    or set(keys) != _RESPONSE_FIELDS
                ):
                    invalid = True
                else:
                    envelope = {
                        name: value[name]
                        for name in _RESPONSE_FIELD_NAMES
                    }
        except Exception:
            invalid = True
        if invalid:
            raise HomecamSemanticBindingError(
                'homecam_response_invalid',
                'Homecam semantic response is invalid',
            )
        return envelope

    def _verify_envelope(
        self,
        envelope: Mapping[str, Any],
        *,
        device_binding_revision: str,
    ) -> _VerifiedSemanticEnvelope:
        """Verify the signed audience, actor, content, and short TTL."""
        if set(envelope) != _RESPONSE_FIELDS:
            raise HomecamSemanticBindingError(
                'homecam_response_invalid',
                'Homecam semantic response is invalid',
            )
        signature = envelope.get('signature')
        unsigned = dict(envelope)
        unsigned.pop('signature', None)
        unsigned.pop('semanticsJson', None)
        signature_body_invalid = False
        expected_signature = ''
        try:
            expected_signature = hmac.new(
                self._config.envelope_signing_secret.encode('ascii'),
                _canonical_json(unsigned),
                hashlib.sha256,
            ).hexdigest()
        except (
            OverflowError,
            RecursionError,
            TypeError,
            UnicodeEncodeError,
            ValueError,
        ):
            signature_body_invalid = True
        if (
            signature_body_invalid
            or type(signature) is not str
            or not _LOWER_SHA256.fullmatch(signature)
            or not hmac.compare_digest(signature, expected_signature)
        ):
            raise HomecamSemanticBindingError(
                'homecam_response_invalid',
                'Homecam semantic response is invalid',
            )
        semantics_json = envelope.get('semanticsJson')
        content_digest = envelope.get('contentSha256')
        semantics_bytes = b''
        invalid_semantics_encoding = False
        try:
            if type(semantics_json) is not str:
                invalid_semantics_encoding = True
            else:
                semantics_bytes = semantics_json.encode('utf-8')
        except UnicodeEncodeError:
            invalid_semantics_encoding = True
        if (
            invalid_semantics_encoding
            or len(semantics_bytes) > MAX_SNAPSHOT_BYTES
            or type(content_digest) is not str
            or not _LOWER_SHA256.fullmatch(content_digest)
            or not hmac.compare_digest(
                content_digest,
                hashlib.sha256(semantics_bytes).hexdigest(),
            )
        ):
            raise HomecamSemanticBindingError(
                'homecam_response_invalid',
                'Homecam semantic response is invalid',
            )
        invalid_semantics = False
        semantics: Any = None
        try:
            semantics = json.loads(semantics_json)
        except (json.JSONDecodeError, RecursionError):
            invalid_semantics = True
        if invalid_semantics or not isinstance(semantics, dict):
            raise HomecamSemanticBindingError(
                'homecam_response_invalid',
                'Homecam semantic response is invalid',
            )
        issued_at = envelope.get('issuedAtMs')
        expires_at = envelope.get('expiresAtMs')
        invalid_clock = False
        now_ms = 0.0
        try:
            clock_value = self._clock()
            if isinstance(clock_value, bool):
                invalid_clock = True
            else:
                now_ms = float(clock_value) * 1000.0
        except Exception:
            invalid_clock = True
        if (
            type(issued_at) is not int
            or type(expires_at) is not int
            or invalid_clock
            or not math.isfinite(now_ms)
            or issued_at < 0
            or expires_at <= issued_at
            or expires_at - issued_at > MAX_ENVELOPE_LIFETIME_MS
            or issued_at > now_ms + MAX_ENVELOPE_FUTURE_SKEW_MS
            or now_ms >= expires_at
        ):
            raise HomecamSemanticBindingError(
                'homecam_response_expired',
                'Homecam semantic response is not current',
            )
        map_generation = envelope.get('mapGeneration')
        authorization_revision = envelope.get('authorizationRevision')
        revision = semantics.get('revision')
        source_match = (
            _SERVER_REVISION.fullmatch(revision)
            if type(revision) is str
            else None
        )
        authorization_match = (
            _AUTHORIZATION_REVISION.fullmatch(authorization_revision)
            if type(authorization_revision) is str
            else None
        )
        if (
            type(map_generation) is not str
            or not _POSITIVE_DECIMAL.fullmatch(map_generation)
            or authorization_match is None
            or source_match is None
            or source_match.group(1) != map_generation
        ):
            raise HomecamSemanticBindingError(
                'homecam_response_invalid',
                'Homecam semantic response is invalid',
            )
        return _VerifiedSemanticEnvelope(
            semantics=semantics,
            device_binding_revision=device_binding_revision,
            content_sha256=content_digest,
            map_generation=int(map_generation),
            authorization_generation=int(authorization_match.group(1)),
            expires_at_ms=expires_at,
        )

    def _fetch_verified_envelope(self) -> _VerifiedSemanticEnvelope:
        """Fetch and authenticate one response for the fixed principal."""
        response = self._transport.fetch(
            agent_user_id=self._config.agent_user_id,
            principal_subject_digest=(
                self._config.principal_subject_digest
            ),
            device_id=self._config.device_id,
        )
        envelope = self._snapshot_response_envelope(response)
        binding_revision = envelope.get('deviceBindingRevision')
        if (
            type(envelope.get('schemaVersion')) is not int
            or envelope.get('schemaVersion')
            != HOME_CAM_SEMANTIC_SCHEMA_VERSION
            or any(
                type(envelope.get(name)) is not str
                for name in _RESPONSE_STRING_FIELDS
            )
            or envelope.get('issuer') != HOME_CAM_SEMANTIC_ISSUER
            or envelope.get('audience') != HOME_CAM_SEMANTIC_AUDIENCE
            or envelope.get('agentUserId')
            != self._config.agent_user_id
            or envelope.get('principalSubjectDigest')
            != self._config.principal_subject_digest
            or envelope.get('deviceId') != self._config.device_id
            or envelope.get('sourceIsFinalized') is not True
            or not _LOWER_SHA256.fullmatch(binding_revision)
        ):
            raise HomecamSemanticBindingError(
                'homecam_response_invalid',
                'Homecam semantic response is invalid',
            )
        return self._verify_envelope(
            envelope,
            device_binding_revision=binding_revision,
        )

    def _snapshot_evidence(
        self,
        verified: _VerifiedSemanticEnvelope,
    ) -> VerifiedSemanticSnapshotEvidence:
        """Parse and project authenticated semantics without raw envelope."""
        snapshot = parse_trusted_semantic_snapshot(
            verified.semantics,
            device_id=self._config.device_id,
            device_binding_revision=verified.device_binding_revision,
            source_is_finalized=True,
        )
        try:
            return VerifiedSemanticSnapshotEvidence(
                snapshot=snapshot,
                content_sha256=verified.content_sha256,
                map_generation=verified.map_generation,
                authorization_generation=(
                    verified.authorization_generation
                ),
                expires_at_ms=verified.expires_at_ms,
                _zones=verified.semantics.get('zones'),
                _construction_token=_EVIDENCE_CONSTRUCTION_TOKEN,
            )
        except (
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            raise HomecamSemanticBindingError(
                'homecam_response_invalid',
                'Homecam semantic response is invalid',
            ) from None

    def fetch_snapshot_evidence(self) -> VerifiedSemanticSnapshotEvidence:
        """Return one immutable projection of a current signed snapshot."""
        return self._snapshot_evidence(self._fetch_verified_envelope())

    def resolve(self, request: Any) -> TargetBinding:
        """Fetch and parse the configured user's finalized map snapshot."""
        request_user_id = getattr(request, 'user_id', None)
        location = getattr(request, 'location', None)
        if request_user_id != self._config.agent_user_id:
            raise HomecamSemanticBindingError(
                'homecam_principal_mismatch',
                'Homecam semantic principal is not available',
            )
        if not isinstance(location, str):
            raise HomecamSemanticBindingError(
                'homecam_request_invalid',
                'Homecam semantic request is invalid',
            )
        self._require_request_current(request)
        verified = self._fetch_verified_envelope()
        self._require_request_current(request)
        evidence = self._snapshot_evidence(verified)
        return resolve_monitor_room_target(
            evidence.snapshot,
            location,
            self._effects,
        )
