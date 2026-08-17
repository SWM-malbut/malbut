"""Tests for the authenticated Homecam semantic adapter."""

import copy
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

import malbut_agent_server.homecam_semantic as homecam_semantic
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import (
    build_monitor_room_target_resolver,
)
from malbut_agent_server.homecam_semantic import (
    AuthenticatedHomecamSemanticResolver,
    HomecamSemanticBindingError,
    HomecamSemanticConfig,
    HomecamSemanticUnavailableError,
    HTTPSHomecamSemanticTransport,
    MAX_HOME_CAM_RESPONSE_BYTES,
    VerifiedSemanticSnapshotEvidence,
    normalize_homecam_origin,
)
from malbut_agent_server.speech import MonitorRoomTargetRequest


_SERVICE_TOKEN = 's' * 64
_SIGNING_SECRET = 'k' * 64
_SUBJECT_DIGEST = hashlib.sha256(b'cognito-subject-1').hexdigest()


def _room_payload() -> dict:
    room = {
        'type': 'Feature',
        'id': 'room-living',
        'properties': {
            'role': 'room',
            'room_id': 'room-living',
            'name': '거실',
            'category': 'living_room',
            'area_m2': 16.0,
            'representative_point': [2.0, 2.0],
            'clearance_m': 2.0,
            'color': '#dce8ff',
            'generated': False,
        },
        'geometry': {
            'type': 'Polygon',
            'coordinates': [[
                [0.0, 0.0],
                [4.0, 0.0],
                [4.0, 4.0],
                [0.0, 4.0],
                [0.0, 0.0],
            ]],
        },
    }
    return {
        'revision': 'srv-7-0123456789abcdef',
        'mapId': 'map-home',
        'mapRevision': 'grid-revision-1',
        'userMap': {
            'type': 'FeatureCollection',
            'format': 'malbut-user-map-v1',
            'map_id': 'map-home',
            'map_revision': 'grid-revision-1',
            'legacy_map_ids': [],
            'frame_id': 'map',
            'generated_at': '2026-08-15T01:02:03+00:00',
            'source': {'resolution': 0.05},
            'room_segmentation': {
                'method': 'user_edited',
                'room_count': 1,
            },
            'features': [room],
        },
        'zones': None,
    }


def _canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _envelope(**updates) -> dict:
    semantics = _room_payload()
    semantics_json = _canonical(semantics).decode('utf-8')
    value = {
        'schemaVersion': 1,
        'issuer': 'malbut-homecam-web',
        'audience': 'malbut-agent-semantic-v1',
        'agentUserId': 'local-user',
        'principalSubjectDigest': _SUBJECT_DIGEST,
        'deviceId': 'malbut-sim-01',
        'deviceBindingRevision': hashlib.sha256(
            b'principal-device-membership-3'
        ).hexdigest(),
        'authorizationRevision': 'auth-3',
        'mapGeneration': '7',
        'sourceIsFinalized': True,
        'issuedAtMs': 1_000_000,
        'expiresAtMs': 1_005_000,
        'contentSha256': hashlib.sha256(
            semantics_json.encode('utf-8')
        ).hexdigest(),
        'semanticsJson': semantics_json,
    }
    value.update(updates)
    signed = dict(value)
    signed.pop('semanticsJson')
    value['signature'] = hmac.new(
        _SIGNING_SECRET.encode('ascii'),
        _canonical(signed),
        hashlib.sha256,
    ).hexdigest()
    return value


def _resign(envelope: dict) -> dict:
    """Return an envelope with a valid signature for its current fields."""
    value = copy.deepcopy(envelope)
    signed = dict(value)
    signed.pop('signature', None)
    signed.pop('semanticsJson')
    value['signature'] = hmac.new(
        _SIGNING_SECRET.encode('ascii'),
        _canonical(signed),
        hashlib.sha256,
    ).hexdigest()
    return value


def _envelope_for_semantics(semantics: dict, **updates) -> dict:
    """Return a signed envelope containing the supplied semantic JSON."""
    semantics_json = _canonical(semantics).decode('utf-8')
    return _envelope(
        semanticsJson=semantics_json,
        contentSha256=hashlib.sha256(
            semantics_json.encode('utf-8')
        ).hexdigest(),
        **updates,
    )


def _zones_payload() -> dict:
    """Return one nested full-zone projection for immutability tests."""
    return {
        'type': 'FeatureCollection',
        'format': 'malbut-semantic-zones-v1',
        'map_id': 'map-home',
        'map_revision': 'grid-revision-1',
        'features': [{
            'type': 'Feature',
            'id': 'zone-private-1',
            'properties': {
                'role': 'privacy_zone',
                'rules': ['no_camera', 'no_navigation'],
                'policy': {'enabled': True},
            },
            'geometry': {
                'type': 'Polygon',
                'coordinates': [[
                    [1.0, 1.0],
                    [1.5, 1.0],
                    [1.5, 1.5],
                    [1.0, 1.5],
                    [1.0, 1.0],
                ]],
            },
        }],
    }


class _Transport:
    def __init__(self, envelope=None):
        self.envelope = _envelope() if envelope is None else envelope
        self.calls = []

    def fetch(self, **values):
        self.calls.append(values)
        return copy.deepcopy(self.envelope)


class _DirectTransport:
    """Return a custom response object without copying away its behavior."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def fetch(self, **values):
        self.calls.append(values)
        return self.response


class _ChangingResponseMapping(Mapping):
    """Return valid response values once and changed values thereafter."""

    def __init__(self, response):
        self._response = response
        self.reads = {}

    def __getitem__(self, key):
        count = self.reads.get(key, 0) + 1
        self.reads[key] = count
        if count == 1:
            return self._response[key]
        if key == 'deviceId':
            return 'changed-device'
        return None

    def __iter__(self):
        return iter(self._response)

    def __len__(self):
        return len(self._response)


class _BuiltinLookingString(str):
    """Harmless non-builtin string used to assert exact wire types."""


class _BuiltinLookingInteger(int):
    """Harmless non-builtin integer used to assert exact wire types."""


class _DivergentEncodingString(str):
    """A string whose overridden encoding differs from its JSON value."""

    def __new__(cls, value, encoded):
        instance = super().__new__(cls, value)
        instance._encoded = encoded
        return instance

    def encode(self, _encoding='utf-8', _errors='strict'):
        return self._encoded


def _config() -> HomecamSemanticConfig:
    return HomecamSemanticConfig(
        origin='https://homecam.example.test',
        service_token=_SERVICE_TOKEN,
        envelope_signing_secret=_SIGNING_SECRET,
        agent_user_id='local-user',
        principal_subject_digest=_SUBJECT_DIGEST,
        device_id='malbut-sim-01',
        timeout_seconds=3,
    )


def _request(user_id='local-user', location='거실'):
    return MonitorRoomTargetRequest(
        user_id=user_id,
        speech_session_id='speech-1',
        source_utterance_id='utterance-1',
        conversation_id='conversation-1',
        conversation_session_instance_id='instance-1',
        conversation_generation=1,
        conversation_revision=1,
        conversation_ordinal=1,
        agent_request_id='request-1',
        turn_id='turn-1',
        decision_id='decision-1',
        location=location,
        issued_at=1000.0,
        expires_at=1030.0,
    )


def test_signed_envelope_binds_actor_device_generation_and_effects() -> None:
    """A valid short-lived envelope creates one immutable room target."""
    transport = _Transport()
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=transport,
        clock=lambda: 1002.0,
    )

    target = resolver.resolve(_request())

    assert target.device_id == 'malbut-sim-01'
    assert target.source_revision == 'srv-7-0123456789abcdef'
    assert target.room_id == 'room-living'
    assert target.room_name == '거실'
    assert target.effects.physical_navigation is True
    assert target.effects.external_video_stream is True
    assert target.effects.video_recording is False
    assert target.effects.audio_capture is False
    assert transport.calls == [{
        'agent_user_id': 'local-user',
        'principal_subject_digest': _SUBJECT_DIGEST,
        'device_id': 'malbut-sim-01',
    }]


def test_fetch_snapshot_evidence_projects_only_verified_metadata() -> None:
    """The seam exposes typed signed metadata without the raw envelope."""
    envelope = _envelope()
    transport = _Transport(envelope)
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=transport,
        clock=lambda: 1002.0,
    )

    evidence = resolver.fetch_snapshot_evidence()

    assert type(evidence) is VerifiedSemanticSnapshotEvidence
    assert evidence.snapshot.device_id == 'malbut-sim-01'
    assert evidence.snapshot.source_revision == (
        'srv-7-0123456789abcdef'
    )
    assert evidence.content_sha256 == envelope['contentSha256']
    assert type(evidence.map_generation) is int
    assert evidence.map_generation == 7
    assert type(evidence.authorization_generation) is int
    assert evidence.authorization_generation == 3
    assert type(evidence.expires_at_ms) is int
    assert evidence.expires_at_ms == 1_005_000
    assert evidence.expires_at_wall == 1005.0
    assert evidence.zones is None
    assert hash(evidence) == hash(evidence)
    assert len(transport.calls) == 1

    representation = repr(evidence)
    assert 'semanticsJson' not in representation
    assert '거실' not in representation
    assert _SERVICE_TOKEN not in representation
    assert _SIGNING_SECRET not in representation
    with pytest.raises(FrozenInstanceError):
        evidence.map_generation = 8


def test_snapshot_evidence_constructor_is_resolver_private() -> None:
    """Callers cannot mint nominal verified evidence from public values."""
    resolved = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(),
        clock=lambda: 1002.0,
    ).fetch_snapshot_evidence()

    with pytest.raises(TypeError):
        VerifiedSemanticSnapshotEvidence(
            snapshot=resolved.snapshot,
            content_sha256=resolved.content_sha256,
            map_generation=resolved.map_generation,
            authorization_generation=resolved.authorization_generation,
            expires_at_ms=resolved.expires_at_ms,
            _zones=None,
        )


def test_snapshot_evidence_canonical_copy_revalidates_current_values() \
        -> None:
    """A consumer gets a newly detached and recursively frozen copy."""
    semantics = _room_payload()
    semantics['zones'] = _zones_payload()
    evidence = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(_envelope_for_semantics(semantics)),
        clock=lambda: 1002.0,
    ).fetch_snapshot_evidence()

    object.__setattr__(evidence, '_zones', _zones_payload())
    canonical = evidence.canonical_copy()

    assert canonical == evidence
    assert canonical is not evidence
    assert canonical.snapshot is not evidence.snapshot
    assert isinstance(canonical.zones, MappingProxyType)
    assert canonical.zones is not evidence.zones
    assert isinstance(canonical.zones['features'], tuple)


@pytest.mark.parametrize(
    'mutation',
    (
        lambda value: object.__setattr__(
            value,
            'content_sha256',
            '0' * 64,
        ),
        lambda value: object.__setattr__(value, 'map_generation', 8),
        lambda value: object.__setattr__(value, 'expires_at_ms', 1_006_000),
        lambda value: object.__setattr__(
            value.snapshot,
            'map_id',
            'changed-map',
        ),
        lambda value: object.__setattr__(
            value,
            '_zones',
            {'private': 'changed-zone'},
        ),
    ),
    ids=('digest', 'generation', 'expiry', 'snapshot', 'zones'),
)
def test_snapshot_evidence_canonical_copy_rejects_current_mutation(
    mutation,
) -> None:
    """Frozen-field bypasses cannot silently enter a canonical copy."""
    evidence = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(),
        clock=lambda: 1002.0,
    ).fetch_snapshot_evidence()
    mutation(evidence)

    with pytest.raises(ValueError) as error:
        evidence.canonical_copy()

    assert str(error.value) == 'semantic snapshot evidence is invalid'
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_snapshot_evidence_zones_are_complete_detached_and_frozen() -> None:
    """Every signed zone survives projection without a mutable alias."""
    zones = _zones_payload()
    semantics = _room_payload()
    semantics['zones'] = zones
    envelope = _envelope_for_semantics(semantics)
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(envelope),
        clock=lambda: 1002.0,
    )

    evidence = resolver.fetch_snapshot_evidence()
    projected = evidence.zones

    assert isinstance(projected, MappingProxyType)
    assert projected['map_id'] == 'map-home'
    assert isinstance(projected['features'], tuple)
    feature = projected['features'][0]
    assert isinstance(feature, MappingProxyType)
    assert feature['id'] == 'zone-private-1'
    assert feature['properties']['rules'] == (
        'no_camera',
        'no_navigation',
    )
    assert feature['properties']['policy']['enabled'] is True
    assert evidence.snapshot.zones_digest == hashlib.sha256(
        _canonical(zones)
    ).hexdigest()

    zones['features'][0]['id'] = 'mutated-after-fetch'
    assert feature['id'] == 'zone-private-1'
    with pytest.raises(TypeError):
        projected['map_id'] = 'mutated-map'
    with pytest.raises(TypeError):
        feature['properties']['policy']['enabled'] = False
    assert 'zone-private-1' not in repr(evidence)


def test_snapshot_evidence_accepts_current_exact_replay_without_high_water() \
        -> None:
    """The current seam deliberately has no persistent replay high-water."""
    transport = _Transport()
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=transport,
        clock=lambda: 1002.0,
    )

    first = resolver.fetch_snapshot_evidence()
    replay = resolver.fetch_snapshot_evidence()

    assert replay == first
    assert len(transport.calls) == 2


def test_snapshot_evidence_rejects_replay_at_signed_expiry() -> None:
    """An otherwise exact replay fails at the exclusive signed deadline."""
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(),
        clock=lambda: 1005.0,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        resolver.fetch_snapshot_evidence()

    assert error.value.code == 'homecam_response_expired'


def test_snapshot_evidence_rejects_content_mutation_without_leakage() -> None:
    """Post-signature semantic mutation is rejected without echoing it."""
    private_marker = 'private-response-zone-marker'
    envelope = _envelope()
    semantics = json.loads(envelope['semanticsJson'])
    semantics['zones'] = {'untrusted': private_marker}
    envelope['semanticsJson'] = _canonical(semantics).decode('utf-8')
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(envelope),
        clock=lambda: 1002.0,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        resolver.fetch_snapshot_evidence()

    assert error.value.code == 'homecam_response_invalid'
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert private_marker not in str(error.value)
    assert _SIGNING_SECRET not in str(error.value)


def test_response_mapping_values_are_snapshotted_exactly_once() -> None:
    """A changing Mapping cannot split validation across different values."""
    response = _envelope()
    changing = _ChangingResponseMapping(response)
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_DirectTransport(changing),
        clock=lambda: 1002.0,
    )

    evidence = resolver.fetch_snapshot_evidence()

    assert evidence.snapshot.device_id == 'malbut-sim-01'
    assert set(changing.reads) == set(response)
    assert set(changing.reads.values()) == {1}
    assert changing['deviceId'] == 'changed-device'
    assert evidence.snapshot.device_id == 'malbut-sim-01'


def test_semantics_string_with_divergent_encoding_is_rejected() -> None:
    """The content digest and JSON parser cannot observe different text."""
    envelope = _envelope()
    committed_json = envelope['semanticsJson']
    alternate = _room_payload()
    alternate['userMap']['features'][0]['properties']['name'] = '다른 방'
    alternate_json = _canonical(alternate).decode('utf-8')
    assert hashlib.sha256(alternate_json.encode('utf-8')).hexdigest() != (
        envelope['contentSha256']
    )
    envelope['semanticsJson'] = _DivergentEncodingString(
        alternate_json,
        committed_json.encode('utf-8'),
    )
    assert hashlib.sha256(envelope['semanticsJson'].encode()).hexdigest() == (
        envelope['contentSha256']
    )
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_DirectTransport(envelope),
        clock=lambda: 1002.0,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        resolver.fetch_snapshot_evidence()

    assert error.value.code == 'homecam_response_invalid'
    assert '다른 방' not in str(error.value)


@pytest.mark.parametrize(
    'field_name',
    (
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
    ),
)
def test_snapshot_evidence_requires_exact_builtin_envelope_strings(
    field_name,
) -> None:
    """String subclasses are never retained as authenticated wire values."""
    envelope = _envelope()
    envelope[field_name] = _BuiltinLookingString(envelope[field_name])
    if field_name != 'signature':
        envelope = _resign(envelope)
    assert type(envelope[field_name]) is _BuiltinLookingString
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_DirectTransport(envelope),
        clock=lambda: 1002.0,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        resolver.fetch_snapshot_evidence()

    assert error.value.code == 'homecam_response_invalid'


@pytest.mark.parametrize(
    'envelope',
    (
        _resign(_envelope(deviceId='other-device')),
        _envelope_for_semantics({
            **_room_payload(),
            'revision': 'srv-8-0123456789abcdef',
        }),
    ),
    ids=('device', 'source-generation'),
)
def test_snapshot_evidence_rejects_signed_device_or_source_rebinding(
    envelope,
) -> None:
    """A valid signature cannot rebind the configured device or source."""
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(envelope),
        clock=lambda: 1002.0,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        resolver.fetch_snapshot_evidence()

    assert error.value.code == 'homecam_response_invalid'


@pytest.mark.parametrize(
    'updates, expected_code',
    (
        ({'schemaVersion': True}, 'homecam_response_invalid'),
        ({'mapGeneration': 7}, 'homecam_response_invalid'),
        ({'authorizationRevision': 3}, 'homecam_response_invalid'),
        ({'sourceIsFinalized': 1}, 'homecam_response_invalid'),
        (
            {'issuedAtMs': _BuiltinLookingInteger(1_000_000)},
            'homecam_response_expired',
        ),
        ({'expiresAtMs': 1_005_000.0}, 'homecam_response_expired'),
    ),
    ids=(
        'schema-type',
        'map-generation-type',
        'authorization-generation-type',
        'finalized-type',
        'issued-type',
        'expiry-type',
    ),
)
def test_snapshot_evidence_preserves_strict_signed_field_types(
    updates,
    expected_code,
) -> None:
    """JSON lookalikes never become trusted generation or expiry values."""
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(_envelope(**updates)),
        clock=lambda: 1002.0,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        resolver.fetch_snapshot_evidence()

    assert error.value.code == expected_code


@pytest.mark.parametrize(
    'mutation',
    (
        lambda value: value.update(agentUserId='other-user'),
        lambda value: value.update(deviceId='other-device'),
        lambda value: value.update(principalSubjectDigest='0' * 64),
        lambda value: value.update(sourceIsFinalized=False),
        lambda value: value.update(audience='other-audience'),
        lambda value: value.update(mapGeneration='8'),
        lambda value: value.update(signature='0' * 64),
        lambda value: value.update(expiresAtMs=1_002_000),
    ),
)
def test_envelope_mutation_or_expiry_fails_closed(mutation) -> None:
    """Every actor, source, signature, and TTL mutation is rejected."""
    envelope = _envelope()
    mutation(envelope)
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(envelope),
        clock=lambda: 1002.0,
    )

    with pytest.raises(HomecamSemanticBindingError):
        resolver.resolve(_request())


@pytest.mark.parametrize(
    'mutation, expected_code',
    (
        (
            lambda value: value.update(audience='other-audience'),
            'homecam_response_invalid',
        ),
        (
            lambda value: value.update(sourceIsFinalized=False),
            'homecam_response_invalid',
        ),
        (
            lambda value: value.update(mapGeneration='8'),
            'homecam_response_invalid',
        ),
        (
            lambda value: value.update(expiresAtMs=1_002_000),
            'homecam_response_expired',
        ),
        (
            lambda value: value.update(
                issuedAtMs=1_004_001,
                expiresAtMs=1_005_000,
            ),
            'homecam_response_expired',
        ),
        (
            lambda value: value.update(expiresAtMs=1_020_000),
            'homecam_response_expired',
        ),
    ),
)
def test_validly_resigned_invalid_envelopes_fail_the_semantic_rule(
    mutation,
    expected_code,
) -> None:
    """A valid HMAC must not bypass audience, source, generation, or TTL."""
    envelope = _envelope()
    mutation(envelope)
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(_resign(envelope)),
        clock=lambda: 1002.0,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        resolver.resolve(_request())

    assert error.value.code == expected_code


def test_request_deadline_is_checked_before_and_after_transport() -> None:
    """Remote I/O cannot begin late or commit after the Agent proposal TTL."""
    before = _Transport()
    expired = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=before,
        clock=lambda: 1030.0,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        expired.resolve(_request())

    assert error.value.code == 'homecam_request_expired'
    assert before.calls == []

    samples = iter((1002.0, 1002.0, 1030.0))
    after = _Transport()
    crosses_deadline = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=after,
        clock=lambda: next(samples),
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        crosses_deadline.resolve(_request())

    assert error.value.code == 'homecam_request_expired'
    assert len(after.calls) == 1


@pytest.mark.parametrize(
    'issued_at, expires_at',
    (
        (10**10000, 10**10000 + 1),
        (True, 1030.0),
        (1000.0, float('inf')),
    ),
    ids=('overflow', 'boolean', 'non-finite'),
)
def test_request_time_normalization_is_typed_and_chain_free(
    issued_at,
    expires_at,
) -> None:
    """Malformed or overflowing proposal clocks never escape raw errors."""
    request = _request()
    object.__setattr__(request, 'issued_at', issued_at)
    object.__setattr__(request, 'expires_at', expires_at)
    transport = _Transport()
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=transport,
        clock=lambda: 1002.0,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        resolver.resolve(request)

    assert error.value.code == 'homecam_request_expired'
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert transport.calls == []


def test_envelope_clock_failure_is_typed_and_chain_free() -> None:
    """An injected clock failure cannot expose its raw exception context."""
    calls = iter((1002.0, RuntimeError('/private/clock/source')))

    def clock():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(),
        clock=clock,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        resolver.resolve(_request())

    assert error.value.code == 'homecam_response_expired'
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_wrong_agent_user_is_rejected_before_transport() -> None:
    """A client-shaped user ID cannot select the configured Homecam user."""
    transport = _Transport()
    resolver = AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=transport,
        clock=lambda: 1002.0,
    )

    with pytest.raises(HomecamSemanticBindingError) as error:
        resolver.resolve(_request(user_id='other-user'))

    assert error.value.code == 'homecam_principal_mismatch'
    assert transport.calls == []


@pytest.mark.parametrize(
    'origin',
    (
        'http://homecam.example.test',
        'https://127.0.0.1',
        'https://localhost',
        'https://homecam.example.test/path',
        'https://user@homecam.example.test',
        'https://homecam.example.test:8443',
    ),
)
def test_homecam_origin_rejects_redirect_and_ssrf_shapes(origin) -> None:
    """Only one certificate-verified production HTTPS origin is accepted."""
    with pytest.raises(ValueError):
        normalize_homecam_origin(origin)


def test_settings_and_factory_are_all_or_nothing_and_redact_secrets() -> None:
    """Partial production bindings fail startup and representations redact."""
    config_representation = repr(_config())
    assert _SERVICE_TOKEN not in config_representation
    assert _SIGNING_SECRET not in config_representation
    assert _SUBJECT_DIGEST not in config_representation

    partial = Settings(homecam_origin='https://homecam.example.test')
    with pytest.raises(ValueError):
        partial.validate_for_server()

    settings = Settings(
        user_id='local-user',
        homecam_origin='https://homecam.example.test',
        homecam_agent_token=_SERVICE_TOKEN,
        homecam_signing_secret=_SIGNING_SECRET,
        homecam_principal_subject_digest=_SUBJECT_DIGEST,
        homecam_device_id='malbut-sim-01',
    )
    settings.validate_for_server()
    representation = repr(settings)
    assert _SERVICE_TOKEN not in representation
    assert _SIGNING_SECRET not in representation
    assert _SUBJECT_DIGEST not in representation

    transport = _Transport()
    resolver = build_monitor_room_target_resolver(
        settings,
        transport=transport,
    )
    assert isinstance(resolver, AuthenticatedHomecamSemanticResolver)
    assert transport.calls == []


def test_service_and_envelope_signing_credentials_are_separate() -> None:
    """One leaked bearer credential must not also sign semantic evidence."""
    with pytest.raises(ValueError):
        HomecamSemanticConfig(
            origin='https://homecam.example.test',
            service_token=_SERVICE_TOKEN,
            envelope_signing_secret=_SERVICE_TOKEN,
            agent_user_id='local-user',
            principal_subject_digest=_SUBJECT_DIGEST,
            device_id='malbut-sim-01',
        )


class _HTTPResponse:
    """Small fake for the direct HTTPS transport boundary."""

    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = 'application/json; charset=utf-8',
        content_encoding: str = 'identity',
        content_length: str | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._headers = {
            'Content-Type': content_type,
            'Content-Encoding': content_encoding,
        }
        if content_length is not None:
            self._headers['Content-Length'] = content_length
        self.read_limits = []

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, limit):
        self.read_limits.append(limit)
        return self._body[:limit]


class _HTTPSConnection:
    """Capture one outbound call without network access."""

    def __init__(self, response, *, request_error=None, close_error=None):
        self.response = response
        self.request_error = request_error
        self.close_error = close_error
        self.request_call = None
        self.closed = False

    def request(self, method, path, *, body, headers):
        self.request_call = (method, path, body, headers)
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _patch_https_connection(monkeypatch, connection):
    constructor_calls = []

    def build(hostname, *, port, timeout, context):
        constructor_calls.append((hostname, port, timeout, context))
        return connection

    monkeypatch.setattr(
        homecam_semantic.http.client,
        'HTTPSConnection',
        build,
    )
    return constructor_calls


def test_direct_https_transport_uses_one_fixed_bounded_request(
    monkeypatch,
) -> None:
    """The production transport sends no caller-selected URL or identity."""
    body = json.dumps(_envelope()).encode('utf-8')
    response = _HTTPResponse(body, content_length=str(len(body)))
    connection = _HTTPSConnection(response)
    constructor_calls = _patch_https_connection(monkeypatch, connection)
    transport = HTTPSHomecamSemanticTransport(_config())

    result = transport.fetch(
        agent_user_id='local-user',
        principal_subject_digest=_SUBJECT_DIGEST,
        device_id='malbut-sim-01',
    )

    assert result == _envelope()
    assert len(constructor_calls) == 1
    hostname, port, timeout, context = constructor_calls[0]
    assert hostname == 'homecam.example.test'
    assert port == 443
    assert timeout == 3
    assert context.verify_mode != 0
    method, path, request_body, headers = connection.request_call
    assert method == 'POST'
    assert path == '/api/internal/agent/semantic'
    assert json.loads(request_body) == {
        'agentUserId': 'local-user',
        'deviceId': 'malbut-sim-01',
        'principalSubjectDigest': _SUBJECT_DIGEST,
        'schemaVersion': 1,
    }
    assert headers['Authorization'] == f'Bearer {_SERVICE_TOKEN}'
    assert headers['Accept-Encoding'] == 'identity'
    assert headers['Connection'] == 'close'
    assert response.read_limits == [MAX_HOME_CAM_RESPONSE_BYTES + 1]
    assert connection.closed is True


@pytest.mark.parametrize(
    'response',
    (
        _HTTPResponse(b'{}', status=302),
        _HTTPResponse(b'{}', content_type='text/html'),
        _HTTPResponse(b'{}', content_encoding='gzip'),
        _HTTPResponse(
            b'',
            content_length=str(MAX_HOME_CAM_RESPONSE_BYTES + 1),
        ),
        _HTTPResponse(b'', content_length='not-a-number'),
        _HTTPResponse(b'\xff'),
        _HTTPResponse(b'not-json'),
        _HTTPResponse(b'[]'),
        _HTTPResponse((b'[' * 2_000) + b'0' + (b']' * 2_000)),
    ),
)
def test_direct_https_transport_rejects_untrusted_response_shapes(
    monkeypatch,
    response,
) -> None:
    """Redirects, encodings, oversize bodies, and invalid JSON fail closed."""
    connection = _HTTPSConnection(response)
    _patch_https_connection(monkeypatch, connection)
    transport = HTTPSHomecamSemanticTransport(_config())

    with pytest.raises(HomecamSemanticUnavailableError) as error:
        transport.fetch(
            agent_user_id='local-user',
            principal_subject_digest=_SUBJECT_DIGEST,
            device_id='malbut-sim-01',
        )

    assert error.value.code == 'homecam_semantic_unavailable'
    assert connection.closed is True


def test_direct_https_transport_sanitizes_network_and_close_failures(
    monkeypatch,
) -> None:
    """Transport exceptions never retain a URL, credential, or raw context."""
    secret_error = OSError(
        f'https://private.example/{_SERVICE_TOKEN}/operator-path'
    )
    connection = _HTTPSConnection(
        _HTTPResponse(b'{}'),
        request_error=secret_error,
        close_error=OSError('private close path'),
    )
    _patch_https_connection(monkeypatch, connection)
    transport = HTTPSHomecamSemanticTransport(_config())

    with pytest.raises(HomecamSemanticUnavailableError) as error:
        transport.fetch(
            agent_user_id='local-user',
            principal_subject_digest=_SUBJECT_DIGEST,
            device_id='malbut-sim-01',
        )

    assert error.value.code == 'homecam_semantic_unavailable'
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert _SERVICE_TOKEN not in str(error.value)


def test_direct_https_transport_sanitizes_connection_setup_failure(
    monkeypatch,
) -> None:
    """TLS and connection setup failures also use the stable public error."""
    private_detail = f'/private/ca/{_SERVICE_TOKEN}.pem'

    def fail_connection(*_args, **_kwargs):
        raise OSError(private_detail)

    monkeypatch.setattr(
        homecam_semantic.http.client,
        'HTTPSConnection',
        fail_connection,
    )
    transport = HTTPSHomecamSemanticTransport(_config())

    with pytest.raises(HomecamSemanticUnavailableError) as error:
        transport.fetch(
            agent_user_id='local-user',
            principal_subject_digest=_SUBJECT_DIGEST,
            device_id='malbut-sim-01',
        )

    assert error.value.code == 'homecam_semantic_unavailable'
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert private_detail not in str(error.value)
