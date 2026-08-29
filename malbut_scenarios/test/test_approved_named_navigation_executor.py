"""Focused contracts for the approved named-navigation adapters."""

import hashlib
import json

import pytest

from malbut_agent_server.ports.approved_navigation_executor import (
    ApprovedNavigationOutcomeUnknown,
    ApprovedNavigationRejected,
)
from malbut_gazebo.named_navigation import parse_named_navigation_catalog
from malbut_gazebo.named_navigation_facade import (
    NamedNavigationFacade,
    SimulationNavigationAuthority,
)
from malbut_gazebo.robot_web_navigation_client import (
    EditorConfig,
    NavigationPreview,
    NavigationSession,
    NavigationStatus,
    RobotWebHTTPError,
    RobotWebNavigationClient,
    RobotWebOutcomeUnknown,
    RobotWebProtocolError,
    RobotWebReadiness,
)
from malbut_scenarios.approved_named_navigation_executor import (
    ApprovedNamedNavigationExecutor,
    RobotWebSimulationStateSource,
    SimulationStateSourceError,
)


DEVICE_ID = 'malbut-sim-01'
MAP_ID = 'map-small-house'
MAP_REVISION = 'revision-1'


def _catalog():
    value = {
        'type': 'FeatureCollection',
        'format': 'malbut-user-map-v1',
        'map_id': MAP_ID,
        'map_revision': MAP_REVISION,
        'frame_id': 'map',
        'room_segmentation': {'room_count': 1},
        'features': [{
            'type': 'Feature',
            'id': 'private-room-id',
            'properties': {
                'role': 'room',
                'room_id': 'private-room-id',
                'name': '거실',
                'category': 'living_room',
                'area_m2': 9.0,
                'representative_point': [1.25, -0.5],
                'clearance_m': 1.0,
            },
            'geometry': {
                'type': 'Polygon',
                'coordinates': [[
                    [0.0, -1.0], [3.0, -1.0], [3.0, 2.0],
                    [0.0, 2.0], [0.0, -1.0],
                ]],
            },
        }],
    }
    return parse_named_navigation_catalog(
        value,
        device_id=DEVICE_ID,
        expected_map_id=MAP_ID,
        expected_map_revision=MAP_REVISION,
        source_digest=hashlib.sha256(
            json.dumps(value, sort_keys=True).encode('utf-8')
        ).hexdigest(),
    )


class _FacadeClient:
    """Small SWM25-130 connector fake retaining opaque capabilities."""

    def __init__(self) -> None:
        self.calls = []
        self.owner = object()
        self.start_error = None
        self.status_error = None
        self.session = None
        self.config = EditorConfig(
            MAP_ID,
            MAP_REVISION,
            True,
            'a' * 64,
            DEVICE_ID,
            True,
        )

    def bootstrap(self):
        self.calls.append(('bootstrap',))
        return self.config

    def preview(
        self,
        *,
        map_id,
        map_revision,
        x,
        y,
        user_map_digest,
        target_binding_digest,
    ):
        self.calls.append((
            'preview',
            map_id,
            map_revision,
            x,
            y,
            user_map_digest,
        ))
        return NavigationPreview(
            'private-preview-token',
            self.owner,
            30.0,
            target_binding_digest,
        )

    def start(self, preview):
        self.calls.append(('start', preview))
        if self.start_error is not None:
            raise self.start_error
        preview._consumed = True
        self.session = NavigationSession(
            'private-navigation-session',
            self.owner,
            'driving',
            preview._target_binding_digest,
        )
        return self.session

    def status_for(self, session):
        self.calls.append(('status_for', session))
        if self.status_error is not None:
            raise self.status_error
        return NavigationStatus(
            'succeeded',
            session,
            1.0,
            None,
            'arrived at 1.25,-0.5',
        )


def _executor(client=None, **kwargs):
    actual_client = client or _FacadeClient()
    facade = NamedNavigationFacade(
        _catalog,
        actual_client,
        authority=SimulationNavigationAuthority.explicit_test_authority(),
    )
    return ApprovedNamedNavigationExecutor(facade, **kwargs), actual_client


def _target_digest():
    return _catalog().resolve('거실').binding_digest


def test_prepare_has_no_start_and_binds_the_expected_target():
    """Keep preview side-effect free and its private values redacted."""
    executor, client = _executor(id_factory=lambda: 'p' * 24)

    prepared = executor.prepare('거실', _target_digest())

    assert [call[0] for call in client.calls] == ['bootstrap', 'preview']
    assert 'start' not in repr(prepared)
    rendered = repr(prepared) + repr(executor)
    for private in (
        'private-preview-token',
        DEVICE_ID,
        MAP_ID,
        MAP_REVISION,
        '1.25',
        '-0.5',
        _target_digest(),
        'p' * 24,
    ):
        assert private not in rendered


def test_release_prevents_preview_cache_exhaustion_without_robot_effect():
    """Allow sequential blocked preflights without retaining capabilities."""
    executor, client = _executor(id_factory=lambda: 'p' * 24)

    for _index in range(300):
        prepared = executor.prepare('거실', _target_digest())
        executor.release(prepared)

    assert [call[0] for call in client.calls].count('preview') == 300
    assert [call[0] for call in client.calls].count('start') == 0


def test_released_started_reference_cannot_start_or_read_status_again():
    """Releasing local caches must never enable a second external start."""
    identifiers = iter(('p' * 24, 'h' * 24))
    executor, client = _executor(id_factory=lambda: next(identifiers))
    prepared = executor.prepare('거실', _target_digest())
    handle = executor.start(
        prepared,
        committed_intent_id='dispatch-intent-1',
    )

    executor.release(prepared)

    with pytest.raises(ApprovedNavigationRejected):
        executor.start(
            prepared,
            committed_intent_id='dispatch-intent-1',
        )
    with pytest.raises(ApprovedNavigationRejected):
        executor.status(handle)
    assert [call[0] for call in client.calls].count('start') == 1


def test_prepare_rejects_wrong_binding_without_start():
    """Do not accept a preview for a changed confirmation target."""
    executor, client = _executor(id_factory=lambda: 'p' * 24)

    with pytest.raises(ApprovedNavigationRejected) as caught:
        executor.prepare('거실', 'f' * 64)

    assert caught.value.code == 'target_binding_mismatch'
    assert caught.value.outcome_known is True
    assert [call[0] for call in client.calls].count('start') == 0


def test_same_committed_intent_starts_once_and_returns_cached_handle():
    """Make retries within one process idempotent without another goal."""
    identifiers = iter(('p' * 24, 'h' * 24))
    executor, client = _executor(id_factory=lambda: next(identifiers))
    prepared = executor.prepare('거실', _target_digest())

    first = executor.start(
        prepared,
        committed_intent_id='dispatch-intent-1',
    )
    second = executor.start(
        prepared,
        committed_intent_id='dispatch-intent-1',
    )

    assert first is second
    assert [call[0] for call in client.calls].count('start') == 1
    assert 'h' * 24 not in repr(first)
    with pytest.raises(ApprovedNavigationRejected) as mismatch:
        executor.start(
            prepared,
            committed_intent_id='dispatch-intent-2',
        )
    assert mismatch.value.code == 'prepared_intent_mismatch'
    assert [call[0] for call in client.calls].count('start') == 1


def test_unknown_start_is_cached_and_never_resent():
    """Preserve ambiguity after a response loss instead of retrying."""
    client = _FacadeClient()
    client.start_error = RobotWebOutcomeUnknown(
        'start',
        cause_code='TRANSPORT_ERROR',
    )
    executor, _ = _executor(client, id_factory=lambda: 'p' * 24)
    prepared = executor.prepare('거실', _target_digest())

    for _attempt in range(2):
        with pytest.raises(ApprovedNavigationOutcomeUnknown) as caught:
            executor.start(
                prepared,
                committed_intent_id='dispatch-intent-1',
            )
        assert caught.value.operation == 'start'
        assert caught.value.cause_code == 'TRANSPORT_ERROR'
        assert caught.value.outcome_known is False

    assert [call[0] for call in client.calls].count('start') == 1


def test_definite_start_rejection_is_cached_without_retry():
    """Retain an explicit server rejection for the committed intent."""
    client = _FacadeClient()
    client.start_error = RobotWebHTTPError(409, 'NAVIGATION_REJECTED')
    executor, _ = _executor(client, id_factory=lambda: 'p' * 24)
    prepared = executor.prepare('거실', _target_digest())

    for _attempt in range(2):
        with pytest.raises(ApprovedNavigationRejected) as caught:
            executor.start(
                prepared,
                committed_intent_id='dispatch-intent-1',
            )
        assert caught.value.code == 'NAVIGATION_REJECTED'

    assert [call[0] for call in client.calls].count('start') == 1


def test_status_is_typed_terminal_and_redacts_raw_message():
    """Return only bounded result evidence for the opaque exact session."""
    identifiers = iter(('p' * 24, 'h' * 24))
    executor, client = _executor(id_factory=lambda: next(identifiers))
    prepared = executor.prepare('거실', _target_digest())
    handle = executor.start(
        prepared,
        committed_intent_id='dispatch-intent-1',
    )

    status = executor.status(handle)

    assert status.state == 'succeeded'
    assert status.terminal is True
    assert status.result_code == 'NAVIGATION_SUCCEEDED'
    assert status.progress_ratio == 1.0
    assert status.simulation is True
    assert status.physical_authorized is False
    assert [call[0] for call in client.calls].count('start') == 1
    rendered = repr(status) + repr(handle)
    assert 'private-navigation-session' not in rendered
    assert '1.25' not in rendered
    assert '-0.5' not in rendered


def test_status_transport_failure_is_outcome_unknown():
    """Never invent a terminal result when observation is unavailable."""
    identifiers = iter(('p' * 24, 'h' * 24))
    client = _FacadeClient()
    executor, _ = _executor(
        client,
        id_factory=lambda: next(identifiers),
    )
    prepared = executor.prepare('거실', _target_digest())
    handle = executor.start(
        prepared,
        committed_intent_id='dispatch-intent-1',
    )
    client.status_error = RobotWebOutcomeUnknown(
        'status',
        cause_code='TRANSPORT_ERROR',
    )

    with pytest.raises(ApprovedNavigationOutcomeUnknown) as caught:
        executor.status(handle)

    assert caught.value.operation == 'status'
    assert caught.value.cause_code == 'TRANSPORT_ERROR'


def _readiness(
    *,
    simulation=True,
    device_id=DEVICE_ID,
    sequence=1,
    source_age_seconds=0.0,
):
    material = f'{simulation}:{device_id}:{sequence}'.encode('utf-8')
    return RobotWebReadiness(
        device_id=device_id,
        map_id=MAP_ID,
        map_revision=MAP_REVISION,
        simulation=simulation,
        navigation_enabled=True,
        nav2_all_active=True,
        localization_ok=True,
        pose_available=True,
        snapshot_sequence=sequence,
        _source_age_seconds=source_age_seconds,
        _content_fingerprint=hashlib.sha256(material).hexdigest(),
    )


class _ReadinessClient(RobotWebNavigationClient):
    def __init__(self, values):
        self.values = iter(values)

    def readiness(self):
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value


def _state_source(client, *, clock=lambda: 100.0):
    return RobotWebSimulationStateSource(
        client,
        expected_device_id=DEVICE_ID,
        expected_map_id=MAP_ID,
        expected_map_revision=MAP_REVISION,
        assumed_battery_percent=83.0,
        clock=clock,
    )


def test_simulation_state_is_fresh_unique_and_uses_explicit_battery():
    """Hash every sample and never pretend the assumed battery was sensed."""
    source = _state_source(
        _ReadinessClient((_readiness(), _readiness())),
    )

    first = source.read()
    second = source.read()

    assert first.evidence_id != second.evidence_id
    assert first.observed_at == second.observed_at == 100.0
    assert first.trusted is True
    assert first.state.battery_percent == 83.0
    assert first.state.navigation_available is True
    assert first.state.localization_ok is True
    assert first.state.emergency_stop is False
    assert first.state.privacy_mode is True
    assert 'explicit_simulation_assumption' in repr(source)
    for private in (DEVICE_ID, MAP_ID, MAP_REVISION):
        assert private not in repr(source)


def test_simulation_state_preserves_upstream_age_and_request_transit():
    """Do not reset a nearly stale Robot Web sample to receipt time."""
    clock_values = iter((100.0, 100.2))
    source = _state_source(
        _ReadinessClient((_readiness(source_age_seconds=1.9),)),
        clock=lambda: next(clock_values),
    )

    evidence = source.read()

    assert evidence.observed_at == pytest.approx(98.1)
    assert 100.2 - evidence.observed_at > 2.0


@pytest.mark.parametrize(
    ('value', 'expected_code'),
    [
        (_readiness(simulation=False), 'simulation_runtime_required'),
        (
            _readiness(device_id='another-device'),
            'runtime_binding_mismatch',
        ),
        ({'simulation': True}, 'malformed_robot_web_readiness'),
        (
            RobotWebProtocolError('INVALID_NAV2_STATUS'),
            'robot_web_readiness_unavailable',
        ),
    ],
)
def test_simulation_state_source_fails_closed(value, expected_code):
    """Reject physical, mismatched, or malformed Robot Web snapshots."""
    source = _state_source(_ReadinessClient((value,)))

    with pytest.raises(SimulationStateSourceError) as caught:
        source.read()

    assert caught.value.code == expected_code
