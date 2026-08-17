"""Focused tests for the trusted live Gazebo Nav2 validator seam."""

import copy
from dataclasses import FrozenInstanceError, dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path

import numpy as np
import pytest

from malbut_agent_server.homecam_semantic import (
    AuthenticatedHomecamSemanticResolver,
    HomecamSemanticConfig,
    VerifiedSemanticSnapshotEvidence,
)
from malbut_gazebo.gazebo_monitor_room_active_map import (
    ActiveMapEvidenceResolver,
    ActiveMapResolverConfig,
    ActiveMapStaticNavigationProjection,
)
from malbut_gazebo.gazebo_monitor_room_live_validator import (
    GazeboMonitorRoomLiveEvidence,
    GazeboMonitorRoomLiveEvidenceUnavailableError,
    GazeboMonitorRoomLiveValidator,
    GazeboMonitorRoomLiveValidatorError,
    TrustedGazeboMonitorRoomLiveEvidenceSource,
)
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    Nav2CancelRequest,
    Nav2PreflightRequest,
    Nav2StartRequest,
)
from malbut_gazebo.gazebo_monitor_room_navigation_safety import (
    MapCostGrid,
    PathPoint,
    SamplePath,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    CancelOperation,
    GazeboMonitorRoomStore,
    OrderedSemanticSample,
    PrepareOperation,
)
from malbut_gazebo.map_lifecycle import MapGrid, persist_map_revision


_DIGEST = 'a' * 64
_RESULT_DIGEST = 'b' * 64
_SERVICE_TOKEN = 's' * 64
_SIGNING_SECRET = 'k' * 64
_SUBJECT_DIGEST = hashlib.sha256(b'local-subject').hexdigest()


def _canonical(value) -> bytes:
    """Encode one deterministic JSON value for test signatures."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _map_grid(cells=None) -> MapGrid:
    """Return a small axis-aligned map with ample central clearance."""
    values = (
        np.zeros((40, 40), dtype=np.int16)
        if cells is None
        else np.asarray(cells, dtype=np.int16).copy()
    )
    values.setflags(write=False)
    return MapGrid(40, 40, 0.1, -2.0, -2.0, 0.0, values)


def _persist_map(tmp_path: Path, cells=None):
    """Persist and protect one real active-map fixture."""
    map_store = tmp_path / 'protected-map-store'
    map_store.mkdir(mode=0o700)
    manifest = persist_map_revision(_map_grid(cells), map_store)
    _protect_map_revision(map_store, manifest)
    resolver = ActiveMapEvidenceResolver(
        ActiveMapResolverConfig(str(map_store))
    )
    projection = resolver.resolve_static_navigation_projection()
    return map_store, manifest, resolver, projection


def _protect_map_revision(map_store: Path, manifest: dict) -> None:
    """Apply the source permissions required by the trusted resolver."""
    os.chmod(map_store, 0o700)
    os.chmod(map_store / 'versions', 0o700)
    os.chmod(map_store / 'versions' / manifest['revision'], 0o700)
    for field_name in ('map_yaml', 'map_image', 'user_map'):
        os.chmod(map_store / manifest[field_name], 0o600)
    os.chmod(map_store / 'active.json', 0o600)


def _zone_feature(behavior: str, *, closed: bool = True) -> dict:
    """Return a zone whose boundary cuts the default planner path."""
    ring = [
        [-0.1, -0.2],
        [0.1, -0.2],
        [0.1, 0.2],
        [-0.1, 0.2],
        [-0.1, -0.2],
    ]
    if not closed:
        ring.pop()
    return {
        'type': 'Feature',
        'id': f'zone-{behavior}',
        'properties': {
            'role': 'semantic_zone',
            'behavior': behavior,
        },
        'geometry': {'type': 'Polygon', 'coordinates': [ring]},
    }


def _zones(manifest: dict, *features: dict) -> dict:
    """Build the complete semantic-zone projection used by the validator."""
    return {
        'type': 'FeatureCollection',
        'format': 'malbut-semantic-zones-v1',
        'map_id': manifest['map_id'],
        'map_revision': manifest['map_revision'],
        'frame_id': 'map',
        'features': list(features),
    }


def _semantic_payload(
    manifest: dict,
    zones,
    *,
    source_revision='srv-7-0123456789abcdef',
) -> dict:
    """Return one finalized Homecam payload bound to the active map."""
    room = {
        'type': 'Feature',
        'id': 'room-living',
        'properties': {
            'role': 'room',
            'room_id': 'room-living',
            'name': 'Living room',
            'category': 'living_room',
            'area_m2': 9.0,
            'representative_point': [0.0, 0.0],
            'clearance_m': 1.0,
        },
        'geometry': {
            'type': 'Polygon',
            'coordinates': [[
                [-1.5, -1.5],
                [1.5, -1.5],
                [1.5, 1.5],
                [-1.5, 1.5],
                [-1.5, -1.5],
            ]],
        },
    }
    return {
        'revision': source_revision,
        'mapId': manifest['map_id'],
        'mapRevision': manifest['map_revision'],
        'userMap': {
            'type': 'FeatureCollection',
            'format': 'malbut-user-map-v1',
            'map_id': manifest['map_id'],
            'map_revision': manifest['map_revision'],
            'frame_id': 'map',
            'features': [room],
        },
        'zones': zones,
    }


def _signed_envelope(semantics: dict) -> dict:
    """Sign one semantic snapshot through the production verifier."""
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
    signed = dict(value)
    signed.pop('semanticsJson')
    value['signature'] = hmac.new(
        _SIGNING_SECRET.encode('ascii'),
        _canonical(signed),
        hashlib.sha256,
    ).hexdigest()
    return value


class _EnvelopeTransport:
    """Return one detached signed semantic envelope."""

    def __init__(self, envelope) -> None:
        """Store the fixture envelope without performing I/O."""
        self.envelope = envelope

    def fetch(self, **_values):
        """Return a detached response mapping."""
        return copy.deepcopy(self.envelope)


def _semantic_evidence(
    manifest: dict,
    zones=None,
    *,
    source_revision='srv-7-0123456789abcdef',
) -> VerifiedSemanticSnapshotEvidence:
    """Produce nominal evidence through the authenticated resolver."""
    semantics = _semantic_payload(
        manifest,
        zones,
        source_revision=source_revision,
    )
    config = HomecamSemanticConfig(
        origin='https://homecam.example.test',
        service_token=_SERVICE_TOKEN,
        envelope_signing_secret=_SIGNING_SECRET,
        agent_user_id='local-user',
        principal_subject_digest=_SUBJECT_DIGEST,
        device_id='malbut-sim-01',
        timeout_seconds=3,
    )
    return AuthenticatedHomecamSemanticResolver(
        config,
        transport=_EnvelopeTransport(_signed_envelope(semantics)),
        clock=lambda: 1002.0,
    ).fetch_snapshot_evidence()


class _SemanticSource:
    """Return a controlled sequence of already verified snapshots."""

    def __init__(self, *evidences) -> None:
        """Record the immutable evidence sequence."""
        self.evidences = evidences
        self.calls = 0

    def fetch_snapshot_evidence(self):
        """Return the next snapshot, repeating the final one."""
        index = min(self.calls, len(self.evidences) - 1)
        self.calls += 1
        return self.evidences[index]


class _Clock:
    """Expose a manually advanced authority clock."""

    def __init__(self, value: float) -> None:
        """Set the initial time."""
        self.value = value

    def __call__(self):
        """Return the current exact float."""
        return self.value


class _LiveSource(TrustedGazeboMonitorRoomLiveEvidenceSource):
    """Build exact live evidence over a real active-map projection."""

    def __init__(
        self,
        projection=None,
        *,
        start=(-0.5, 0.0),
        unavailable=False,
    ) -> None:
        """Configure a free live costmap and direct planner path."""
        self.projection = projection
        self.start = start
        self.unavailable = unavailable
        self.calls = []
        self.last_evidence = None

    def capture(
        self,
        request,
        *,
        checked_at,
        active_map_evidence_digest,
        semantic_content_digest,
    ):
        """Return one current, coordinate-bound live snapshot."""
        self.calls.append(request.request_fingerprint)
        if self.unavailable:
            raise GazeboMonitorRoomLiveEvidenceUnavailableError(
                '/private/live/source/unavailable'
            )
        active_map = self.projection.active_map_evidence
        start = PathPoint(float(self.start[0]), float(self.start[1]))
        target = PathPoint(request.x_m, request.y_m)
        evidence = GazeboMonitorRoomLiveEvidence(
            request_fingerprint=request.request_fingerprint,
            operation_id=request.operation_id,
            goal_uuid=request.goal_uuid,
            active_map_evidence_digest=active_map_evidence_digest,
            semantic_content_digest=semantic_content_digest,
            captured_at=checked_at,
            valid_until=checked_at + 1.0,
            lifecycle_ready=True,
            tf_ready=True,
            planner_succeeded=True,
            lifecycle_evidence_digest='b' * 64,
            transform_evidence_digest='c' * 64,
            compute_path_evidence_digest='d' * 64,
            start_point=start,
            target_point=target,
            costmap=MapCostGrid(
                'map',
                active_map.width,
                active_map.height,
                active_map.resolution,
                active_map.origin_x,
                active_map.origin_y,
                active_map.origin_yaw,
                [0] * (active_map.width * active_map.height),
            ),
            path=SamplePath('map', [start, target]),
        )
        self.last_evidence = evidence
        return evidence


def _preflight_request(store: GazeboMonitorRoomStore):
    """Rebuild the adapter request only from private durable evidence."""
    binding = store.private_operation_binding('operation-1')
    sample = store.private_current_sample('operation-1')
    return Nav2PreflightRequest(
        operation_id=binding.operation_id,
        robot_id=binding.robot_id,
        map_id=binding.map_id,
        map_revision=binding.map_revision,
        semantic_revision=binding.semantic_revision,
        zones_digest=binding.zones_digest,
        target_binding_digest=binding.target_binding_digest,
        effects_digest=binding.effects_digest,
        profile_digest=binding.profile_digest,
        plan_digest=binding.plan_digest,
        sample_count=binding.sample_count,
        sample_index=sample.index,
        polygon_ordinal=sample.polygon_ordinal,
        row_ordinal=sample.row_ordinal,
        goal_uuid=sample.goal_uuid,
        binding_digest=binding.binding_digest,
        x_m=sample.x_m,
        y_m=sample.y_m,
        frame_id=sample.frame_id,
    )


@dataclass
class _Scenario:
    """Collect real collaborators for one validator test."""

    map_store: Path
    manifest: dict
    resolver: ActiveMapEvidenceResolver
    projection: ActiveMapStaticNavigationProjection
    semantic: VerifiedSemanticSnapshotEvidence
    semantic_source: _SemanticSource
    live_source: _LiveSource
    store: GazeboMonitorRoomStore
    clock: _Clock
    validator: GazeboMonitorRoomLiveValidator
    preflight: Nav2PreflightRequest


def _scenario(tmp_path: Path, *, zones=None, cells=None) -> _Scenario:
    """Build a real map, semantic snapshot, store, and preflight state."""
    map_store, manifest, resolver, projection = _persist_map(
        tmp_path, cells
    )
    zone_value = zones(manifest) if callable(zones) else zones
    semantic = _semantic_evidence(manifest, zone_value)
    snapshot = semantic.snapshot
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(
        PrepareOperation(
            prepare_request_id='prepare-1',
            operation_id='operation-1',
            robot_id='robot-1',
            map_id=snapshot.map_id,
            map_revision=snapshot.map_revision,
            semantic_revision=snapshot.semantic_revision,
            zones_digest=snapshot.zones_digest,
            target_binding_digest=_DIGEST,
            effects_digest=_DIGEST,
            profile_digest=_DIGEST,
            plan_digest=_DIGEST,
            ordered_semantic_samples=(
                OrderedSemanticSample(0, 0, 0, 500, 0),
                OrderedSemanticSample(1, 0, 1, 500, 500),
            ),
            deadline=100.0,
        ),
        now=1.0,
    )
    store.acquire_lease(
        'operation-1',
        worker_id='worker-1',
        expected_fence=0,
        lease_seconds=80.0,
        now=2.0,
    )
    store.begin_preflight(
        store.transition_token('operation-1', worker_id='worker-1'),
        now=3.0,
    )
    semantic_source = _SemanticSource(semantic)
    live_source = _LiveSource(projection)
    clock = _Clock(4.0)
    validator = GazeboMonitorRoomLiveValidator(
        store,
        semantic_source,
        resolver,
        live_source,
        clock=clock,
    )
    return _Scenario(
        map_store=map_store,
        manifest=manifest,
        resolver=resolver,
        projection=projection,
        semantic=semantic,
        semantic_source=semantic_source,
        live_source=live_source,
        store=store,
        clock=clock,
        validator=validator,
        preflight=_preflight_request(store),
    )


def test_happy_preflight_and_start_authority_use_real_dispatch_claim(
    tmp_path,
) -> None:
    """Ready proof plus a durable exact claim authorizes one start."""
    scenario = _scenario(tmp_path)

    validation = scenario.validator.validate_preflight(
        scenario.preflight,
        checked_at=4.0,
    )

    assert validation.outcome == 'ready'
    assert validation.code == 'live_preflight_ready'
    assert validation.request_fingerprint == (
        scenario.preflight.request_fingerprint
    )
    assert len(validation.live_binding_digest) == 64
    assert len(validation.path_evidence_digest) == 64

    scenario.store.record_send_intent(
        scenario.store.transition_token(
            'operation-1', worker_id='worker-1'
        ),
        preflight_digest=validation.path_evidence_digest,
        now=4.5,
    )
    observation = scenario.store.observe('operation-1')
    start = Nav2StartRequest(
        preflight=scenario.preflight,
        worker_id='worker-1',
        fence_epoch=observation.fence_epoch,
        lease_expires_at=observation.lease_expires_at,
        deadline=observation.deadline,
        preflight_digest=validation.path_evidence_digest,
    )
    transition = scenario.store.transition_token(
        'operation-1', worker_id='worker-1'
    )
    assert scenario.store.claim_start_dispatch(
        transition,
        start_fingerprint=start.request_fingerprint,
        binding_digest=start.preflight.binding_digest,
        preflight_digest=start.preflight_digest,
        wire_payload_digest=start.wire_payload_digest,
        now=5.0,
    ) is True
    scenario.clock.value = 6.0

    authorization = scenario.validator.authorize_start(
        start,
        checked_at=6.0,
    )

    assert authorization.operation_id == 'operation-1'
    assert authorization.goal_uuid == scenario.preflight.goal_uuid
    assert authorization.request_fingerprint == start.request_fingerprint
    assert authorization.wire_payload_digest == start.wire_payload_digest
    assert authorization.checked_at == 6.0
    assert len(authorization.authority_evidence_digest) == 64
    assert scenario.store.claim_start_dispatch(
        transition,
        start_fingerprint=start.request_fingerprint,
        binding_digest=start.preflight.binding_digest,
        preflight_digest=start.preflight_digest,
        wire_payload_digest=start.wire_payload_digest,
        now=7.0,
    ) is False


def test_cancel_authority_uses_real_claim_at_nonzero_sample_index(
    tmp_path,
) -> None:
    """Cancellation binds the observed second sample instead of index zero."""
    scenario = _scenario(tmp_path)
    store = scenario.store
    store.record_send_intent(
        store.transition_token('operation-1', worker_id='worker-1'),
        preflight_digest=_DIGEST,
        now=4.0,
    )
    store.record_navigating(
        store.transition_token('operation-1', worker_id='worker-1'),
        acceptance_digest=_DIGEST,
        now=5.0,
    )
    advanced = store.record_sample_succeeded(
        store.transition_token('operation-1', worker_id='worker-1'),
        result_evidence_digest=_RESULT_DIGEST,
        now=6.0,
    )
    assert advanced.current_sample_index == 1
    store.record_send_intent(
        store.transition_token('operation-1', worker_id='worker-1'),
        preflight_digest=_DIGEST,
        now=7.0,
    )
    store.record_navigating(
        store.transition_token('operation-1', worker_id='worker-1'),
        acceptance_digest=_DIGEST,
        now=8.0,
    )
    store.request_cancel(
        CancelOperation(
            cancel_request_id='cancel-1',
            transition=store.transition_token(
                'operation-1', worker_id='worker-1'
            ),
        ),
        now=9.0,
    )
    pending = store.observe('operation-1')
    binding = store.private_operation_binding('operation-1')
    cancel = Nav2CancelRequest(
        operation_id='operation-1',
        worker_id='worker-1',
        fence_epoch=pending.fence_epoch,
        cancel_request_id='cancel-1',
        goal_uuid=pending.current_goal_uuid,
        binding_digest=binding.binding_digest,
    )
    transition = store.transition_token(
        'operation-1', worker_id='worker-1'
    )
    assert transition.sample_index == 1
    assert store.claim_cancel_dispatch(
        transition,
        cancel_request_id=cancel.cancel_request_id,
        request_fingerprint=cancel.request_fingerprint,
        binding_digest=cancel.binding_digest,
        wire_payload_digest=cancel.wire_payload_digest,
        now=10.0,
    ) is True
    scenario.clock.value = 11.0

    authorization = scenario.validator.authorize_cancel(
        cancel,
        checked_at=11.0,
    )

    assert authorization.operation_id == 'operation-1'
    assert authorization.cancel_request_id == 'cancel-1'
    assert authorization.goal_uuid == pending.current_goal_uuid
    assert authorization.request_fingerprint == cancel.request_fingerprint
    assert authorization.wire_payload_digest == cancel.wire_payload_digest
    assert authorization.checked_at == 11.0
    assert len(authorization.authority_evidence_digest) == 64
    assert scenario.semantic_source.calls == 0
    assert scenario.live_source.calls == []


def test_live_evidence_is_frozen_redacted_and_non_authoritative(
    tmp_path,
) -> None:
    """The live DTO cannot claim effects or hide nested mutation."""
    scenario = _scenario(tmp_path)
    evidence = scenario.live_source.capture(
        scenario.preflight,
        checked_at=4.0,
        active_map_evidence_digest=(
            scenario.projection.active_map_evidence.evidence_digest
        ),
        semantic_content_digest=scenario.semantic.content_sha256,
    )

    assert repr(evidence) == 'GazeboMonitorRoomLiveEvidence(<redacted>)'
    assert evidence.runtime_mode == 'gazebo'
    assert evidence.simulation is True
    for name in (
        'physical_authorized',
        'physical_effects',
        'viewer_live',
        'camera_coverage_validated',
        'coverage_achieved',
    ):
        assert getattr(evidence, name) is False
    with pytest.raises(FrozenInstanceError):
        evidence.operation_id = 'changed'

    costs = object.__getattribute__(evidence.costmap, '_costs')
    object.__setattr__(evidence.costmap, '_costs', (254,) + costs[1:])
    with pytest.raises(ValueError) as raised:
        evidence.canonical_copy()
    assert '/private' not in str(raised.value)
    assert raised.value.__cause__ is None


def test_unavailable_live_source_is_retryable_and_content_free(
    tmp_path,
) -> None:
    """Temporary capture loss produces only the fixed retryable result."""
    scenario = _scenario(tmp_path)
    scenario.live_source.unavailable = True

    validation = scenario.validator.validate_preflight(
        scenario.preflight,
        checked_at=4.0,
    )

    assert validation.outcome == 'retryable'
    assert validation.code == 'live_evidence_unavailable'
    assert validation.request_fingerprint == (
        scenario.preflight.request_fingerprint
    )
    assert '/private' not in repr(validation)
    assert scenario.semantic_source.calls == 1
    assert len(scenario.live_source.calls) == 1


def test_same_snapshot_static_projection_rejects_free_live_costmap(
    tmp_path,
) -> None:
    """A forged-clear live grid cannot erase the saved-map obstacle."""
    cells = np.zeros((40, 40), dtype=np.int16)
    cells[20, 20] = 100
    scenario = _scenario(tmp_path, cells=cells)

    validation = scenario.validator.validate_preflight(
        scenario.preflight,
        checked_at=4.0,
    )

    assert validation.outcome == 'rejected'
    assert validation.code == 'live_evidence_rejected'
    live_costs = object.__getattribute__(
        scenario.live_source.last_evidence.costmap,
        '_costs',
    )
    assert set(live_costs) == {0}
    assert scenario.semantic_source.calls == 1


def test_second_semantic_fetch_content_drift_rejects_preflight(
    tmp_path,
) -> None:
    """A second signed payload with equal revisions cannot replace proof."""
    scenario = _scenario(tmp_path)
    changed = _semantic_evidence(
        scenario.manifest,
        source_revision='srv-7-fedcba9876543210',
    )
    assert changed.snapshot.semantic_revision == (
        scenario.semantic.snapshot.semantic_revision
    )
    assert changed.content_sha256 != scenario.semantic.content_sha256
    scenario.semantic_source.evidences = (scenario.semantic, changed)
    scenario.semantic_source.calls = 0

    validation = scenario.validator.validate_preflight(
        scenario.preflight,
        checked_at=4.0,
    )

    assert validation.outcome == 'rejected'
    assert validation.code == 'live_evidence_rejected'
    assert scenario.semantic_source.calls == 2
    assert len(scenario.live_source.calls) == 1


def test_second_active_map_projection_drift_rejects_preflight(
    tmp_path,
) -> None:
    """A later valid active snapshot cannot replace the captured bundle."""
    scenario = _scenario(tmp_path)
    second_manifest = persist_map_revision(
        _map_grid(), scenario.map_store
    )
    _protect_map_revision(scenario.map_store, second_manifest)
    changed = (
        scenario.resolver.resolve_static_navigation_projection()
    )
    first_map = scenario.projection.active_map_evidence
    second_map = changed.active_map_evidence
    assert second_map.map_id == first_map.map_id
    assert second_map.map_revision == first_map.map_revision
    assert second_map.evidence_digest != first_map.evidence_digest
    projections = iter((scenario.projection, changed))
    calls = []

    def resolve_sequence():
        calls.append('projection')
        return next(projections)

    scenario.validator._active_map_resolve = resolve_sequence

    validation = scenario.validator.validate_preflight(
        scenario.preflight,
        checked_at=4.0,
    )

    assert validation.outcome == 'rejected'
    assert validation.code == 'live_evidence_rejected'
    assert calls == ['projection', 'projection']
    assert scenario.semantic_source.calls == 2
    assert len(scenario.live_source.calls) == 1


@pytest.mark.parametrize(
    ('behavior', 'expected_outcome'),
    (('allow', 'ready'), ('restricted', 'rejected')),
)
def test_zone_boundary_crossing_respects_allow_and_restricted_behavior(
    tmp_path,
    behavior,
    expected_outcome,
) -> None:
    """Allow geometry is validated, while restricted boundaries block."""
    scenario = _scenario(
        tmp_path,
        zones=lambda manifest: _zones(
            manifest, _zone_feature(behavior)
        ),
    )

    validation = scenario.validator.validate_preflight(
        scenario.preflight,
        checked_at=4.0,
    )

    assert validation.outcome == expected_outcome
    if behavior == 'allow':
        assert scenario.semantic_source.calls == 2
    else:
        assert scenario.semantic_source.calls == 1


def test_malformed_allow_zone_is_validated_and_rejected(tmp_path) -> None:
    """Non-restricted behavior does not bypass strict GeoJSON validation."""
    scenario = _scenario(
        tmp_path,
        zones=lambda manifest: _zones(
            manifest, _zone_feature('allow', closed=False)
        ),
    )

    validation = scenario.validator.validate_preflight(
        scenario.preflight,
        checked_at=4.0,
    )

    assert validation.outcome == 'rejected'
    assert validation.code == 'live_evidence_rejected'
    assert scenario.live_source.calls == []


def test_constructor_performs_no_fetch_projection_or_live_capture(
    tmp_path,
    monkeypatch,
) -> None:
    """Construction binds callables without filesystem or source reads."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    resolver = ActiveMapEvidenceResolver(
        ActiveMapResolverConfig(str(tmp_path / 'missing-map-store'))
    )
    semantic = _SemanticSource()
    live = _LiveSource()
    calls = []

    def resolve(_self):
        calls.append('projection')
        raise AssertionError('must not resolve during construction')

    monkeypatch.setattr(
        ActiveMapEvidenceResolver,
        'resolve_static_navigation_projection',
        resolve,
    )

    validator = GazeboMonitorRoomLiveValidator(
        store,
        semantic,
        resolver,
        live,
        clock=lambda: 1.0,
    )

    assert type(validator) is GazeboMonitorRoomLiveValidator
    assert semantic.calls == 0
    assert live.calls == []
    assert calls == []


def test_constructor_error_is_content_free_and_chain_free(tmp_path) -> None:
    """Collaborator exception text never crosses the validator boundary."""
    class ExplodingSemanticSource:
        """Raise while the validator inspects the semantic callable."""

        @property
        def fetch_snapshot_evidence(self):
            """Expose a deliberately hostile accessor."""
            raise RuntimeError('/private/semantic/credential')

    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    resolver = ActiveMapEvidenceResolver(
        ActiveMapResolverConfig(str(tmp_path / 'missing-map-store'))
    )

    with pytest.raises(GazeboMonitorRoomLiveValidatorError) as raised:
        GazeboMonitorRoomLiveValidator(
            store,
            ExplodingSemanticSource(),
            resolver,
            _LiveSource(),
        )

    error = raised.value
    assert error.code == 'live_validator_invalid_configuration'
    assert str(error) == 'live_validator_invalid_configuration'
    assert '/private' not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__traceback__ is None
