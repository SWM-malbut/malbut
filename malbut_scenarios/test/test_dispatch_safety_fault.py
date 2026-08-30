"""Contract tests for scenario-only dispatch Safety fault primitives."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from malbut_agent_server.domain.robot_action import (
    ActionBinding,
    ActionState,
    RobotAction,
)
from malbut_agent_server.ports.action_repository import ActionClaim
from malbut_agent_server.robot_state_source import RobotStateEvidence
from malbut_agent_server.schemas import RobotState
from malbut_scenarios.dispatch_safety_fault import (
    ClaimArmedActionRepository,
    DispatchMapRevisionGate,
    DispatchSafetyFaultCoordinator,
    DispatchSafetyFaultError,
    DispatchSafetyFaultObservation,
    DispatchSafetyRobotStateSource,
    dispatch_safety_observation_path,
    read_dispatch_safety_observation,
)
from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboSafetyProfile,
    safety_contract,
)


def _arguments_digest(arguments: dict) -> str:
    payload = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _action() -> RobotAction:
    arguments = {'location': '거실'}
    return RobotAction(
        action_id='action-1',
        operation_id='operation-1',
        binding=ActionBinding(
            confirmation_request_id='confirmation-1',
            proposal_fingerprint='a' * 64,
            arguments_digest=_arguments_digest(arguments),
            target_binding_digest='b' * 64,
            user_id='user-1',
            conversation_id='conversation-1',
            session_instance_id='session-1',
            generation=1,
            conversation_revision=2,
            decision_id='decision-1',
            tool_name='navigate',
            arguments=arguments,
            target_room_name='거실',
            target_room_category='living_room',
            confirmation_state_evidence_id='confirmation-state-1',
            confirmation_state_observed_at=99.0,
            confirmation_safety_policy_revision='malbut-safety-v1',
        ),
        state=ActionState.PENDING_PREFLIGHT,
        revision=1,
        created_at=100.0,
        updated_at=100.0,
        dispatch_expires_at=200.0,
    )


def _claim(*, updated_at: float = 100.0) -> ActionClaim:
    return ActionClaim(
        action=_action().transition(
            ActionState.CLAIMED,
            updated_at=updated_at,
        ),
        worker_id='worker-1',
        claim_token='private-claim-token',
        fence=1,
        lease_expires_at=200.0,
    )


def _evidence(
    evidence_id: str,
    observed_at: float,
    *,
    emergency_stop: bool = False,
) -> RobotStateEvidence:
    return RobotStateEvidence(
        state=RobotState(
            battery_percent=83.0,
            navigation_available=True,
            localization_ok=True,
            emergency_stop=emergency_stop,
            camera_available=False,
            privacy_mode=True,
            docked=False,
            forbidden_zones=('현관',),
        ),
        observed_at=observed_at,
        evidence_id=evidence_id,
        trusted=True,
    )


class _SequenceSource:
    def __init__(self, *values: object) -> None:
        self.values = iter(values)
        self.calls = 0

    def read(self):
        self.calls += 1
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class _Repository:
    def __init__(self, claims: list[ActionClaim | None]) -> None:
        self.claims = list(claims)
        self.calls: list[tuple] = []

    def get(self, action_id):
        self.calls.append(('get', action_id))
        return None

    def find_by_confirmation(self, confirmation_request_id):
        self.calls.append(('find', confirmation_request_id))
        return None

    def claim_next(self, worker_id, *, now, lease_for):
        self.calls.append(('claim', worker_id, now, lease_for))
        return self.claims.pop(0) if self.claims else None

    def record_dispatch_intent(self, claim, authorization, *, now):
        self.calls.append(('intent', claim, authorization, now))
        return 'intent-result'

    def block(self, claim, *, result_code, now):
        self.calls.append(('block', claim, result_code, now))
        return 'block-result'

    def mark_started(self, intent, *, now):
        self.calls.append(('started', intent, now))
        return 'started-result'

    def finish(self, intent, state, *, result_code, now):
        self.calls.append(('finish', intent, state, result_code, now))
        return 'finish-result'

    def recover_uncertain_after_restart(self, *, now):
        self.calls.append(('recover', now))
        return 7


def _coordinator(
    tmp_path: Path,
    profile: TextGazeboSafetyProfile,
    **kwargs,
) -> DispatchSafetyFaultCoordinator:
    return DispatchSafetyFaultCoordinator(
        tmp_path / 'agent.sqlite3',
        profile,
        **kwargs,
    )


def _arm_after_one_real_read(
    source: DispatchSafetyRobotStateSource,
    coordinator: DispatchSafetyFaultCoordinator,
) -> RobotStateEvidence:
    first = source.read()
    coordinator.arm_after_claim(_claim())
    return first


@pytest.mark.parametrize(
    'profile',
    tuple(TextGazeboSafetyProfile),
)
def test_every_profile_publishes_only_its_exact_contract(
    tmp_path: Path,
    profile: TextGazeboSafetyProfile,
) -> None:
    """Bind every allowlisted profile to exact counts and one private file."""
    clock = _Clock(100.6)
    gate = DispatchMapRevisionGate(
        lambda: 'initial-map',
        lambda: 'changed-map',
    )
    coordinator = _coordinator(
        tmp_path,
        profile,
        map_switch_callback=(
            gate.switch
            if profile is TextGazeboSafetyProfile.MAP_REVISION_CHANGED
            else None
        ),
        clock=clock,
        sleeper=clock.sleep,
    )
    before = _evidence('proposal-state', 99.5)
    after = _evidence('dispatch-state-private', 100.5)
    delegate = _SequenceSource(before, after)
    source = DispatchSafetyRobotStateSource(delegate, coordinator)

    assert _arm_after_one_real_read(source, coordinator) is before
    result = source.read()

    contract = safety_contract(profile)
    observation = read_dispatch_safety_observation(
        coordinator.observation_path
    )
    assert observation == DispatchSafetyFaultObservation(
        safety_profile=profile,
        result_code=contract.result_code,
        claim_arm_count=1,
        preclaim_read_count=1,
        postclaim_read_count=1,
        fault_application_count=contract.fault_application_count,
        map_switch_count=contract.map_switch_count,
    )
    assert coordinator.completed_observation() == observation
    assert delegate.calls == 2
    assert stat_mode(coordinator.observation_path) == 0o600
    assert 'dispatch-state-private' not in (
        coordinator.observation_path.read_text(encoding='ascii')
    )

    if profile is TextGazeboSafetyProfile.EMERGENCY_STOP:
        assert type(result.state) is RobotState
        assert result is not after
        assert result.state.emergency_stop is True
        assert result.state.battery_percent == after.state.battery_percent
        assert result.state.navigation_available is True
        assert result.state.localization_ok is True
        assert result.state.privacy_mode is True
        assert result.state.forbidden_zones == ('현관',)
        assert result.observed_at == after.observed_at
        assert result.trusted is after.trusted
        assert result.evidence_id != after.evidence_id
    else:
        assert result is after

    if profile is TextGazeboSafetyProfile.STALE_STATE:
        assert after.observed_at >= _claim().action.created_at
        assert clock.value - after.observed_at > 2.0
        assert clock.sleeps == [pytest.approx(2.0)]
    else:
        assert clock.sleeps == []

    if profile is TextGazeboSafetyProfile.MAP_REVISION_CHANGED:
        assert gate.switch_count == 1
        assert gate.load() == 'changed-map'
    else:
        assert gate.switch_count == 0
        assert gate.load() == 'initial-map'


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777


def test_repository_arms_only_after_real_nonempty_claim(
    tmp_path: Path,
) -> None:
    """A polling miss cannot arm the state fault before durable work exists."""
    coordinator = _coordinator(
        tmp_path,
        TextGazeboSafetyProfile.EMERGENCY_STOP,
    )
    source = DispatchSafetyRobotStateSource(
        _SequenceSource(
            _evidence('proposal-state', 99.5),
            _evidence('dispatch-state', 100.5),
        ),
        coordinator,
    )
    source.read()
    claim = _claim()
    delegate = _Repository([None, claim, None])
    repository = ClaimArmedActionRepository(delegate, coordinator)

    assert repository.claim_next(
        'worker-1', now=99.0, lease_for=10.0
    ) is None
    assert repository.claim_next(
        'worker-1', now=100.0, lease_for=10.0
    ) is claim
    assert source.read().state.emergency_stop is True
    assert repository.claim_next(
        'worker-1', now=101.0, lease_for=10.0
    ) is None
    assert repository.recover_uncertain_after_restart(now=102.0) == 7
    assert repository.get('action') is None
    assert repository.find_by_confirmation('confirmation') is None
    assert repr(repository) == 'ClaimArmedActionRepository(configured=True)'
    assert [item[0] for item in delegate.calls] == [
        'claim', 'claim', 'claim', 'recover', 'get', 'find',
    ]


def test_state_source_reads_delegate_before_duplicate_sequence_fails(
    tmp_path: Path,
) -> None:
    """Even an invalid third read consumes the real boundary first."""
    coordinator = _coordinator(
        tmp_path,
        TextGazeboSafetyProfile.EMERGENCY_STOP,
    )
    delegate = _SequenceSource(
        _evidence('proposal', 99.5),
        _evidence('dispatch', 100.5),
        _evidence('unexpected-third-real-read', 101.0),
    )
    source = DispatchSafetyRobotStateSource(delegate, coordinator)
    source.read()
    coordinator.arm_after_claim(_claim())
    source.read()

    with pytest.raises(DispatchSafetyFaultError) as caught:
        source.read()

    assert caught.value.code == 'dispatch_safety_fault_sequence_invalid'
    assert delegate.calls == 3


def test_stale_fault_rejects_a_sample_that_predates_approval(
    tmp_path: Path,
) -> None:
    """Never relabel pre-approval evidence as the intended stale sample."""
    clock = _Clock(100.5)
    coordinator = _coordinator(
        tmp_path,
        TextGazeboSafetyProfile.STALE_STATE,
        clock=clock,
        sleeper=clock.sleep,
    )
    source = DispatchSafetyRobotStateSource(
        _SequenceSource(
            _evidence('proposal', 99.5),
            _evidence('predates-action', 99.9),
        ),
        coordinator,
    )
    source.read()
    coordinator.arm_after_claim(_claim())

    with pytest.raises(DispatchSafetyFaultError) as caught:
        source.read()

    assert caught.value.code == 'dispatch_safety_fault_stale_window_invalid'
    assert not coordinator.observation_path.exists()


@pytest.mark.parametrize(
    ('now', 'observed_at'),
    (
        (102.6, 100.5),
        (100.5, 100.6),
    ),
    ids=('already-stale', 'future-dated'),
)
def test_stale_fault_requires_an_originally_fresh_nonfuture_sample(
    tmp_path: Path,
    now: float,
    observed_at: float,
) -> None:
    """Reject invalid source freshness before sleeping or publishing proof."""
    clock = _Clock(now)
    coordinator = _coordinator(
        tmp_path,
        TextGazeboSafetyProfile.STALE_STATE,
        clock=clock,
        sleeper=clock.sleep,
    )
    source = DispatchSafetyRobotStateSource(
        _SequenceSource(
            _evidence('proposal', 99.5),
            _evidence('invalid-dispatch-freshness', observed_at),
        ),
        coordinator,
    )
    source.read()
    coordinator.arm_after_claim(_claim())

    with pytest.raises(DispatchSafetyFaultError) as caught:
        source.read()

    assert caught.value.code == 'dispatch_safety_fault_stale_window_invalid'
    assert clock.sleeps == []
    assert not coordinator.observation_path.exists()


def test_map_callback_failure_is_typed_and_publishes_no_proof(
    tmp_path: Path,
) -> None:
    """A failed authoritative switch cannot masquerade as injected evidence."""
    def broken_switch() -> None:
        raise RuntimeError('private callback failure')

    coordinator = _coordinator(
        tmp_path,
        TextGazeboSafetyProfile.MAP_REVISION_CHANGED,
        map_switch_callback=broken_switch,
    )
    source = DispatchSafetyRobotStateSource(
        _SequenceSource(
            _evidence('proposal', 99.5),
            _evidence('dispatch', 100.5),
        ),
        coordinator,
    )
    source.read()
    coordinator.arm_after_claim(_claim())

    with pytest.raises(DispatchSafetyFaultError) as caught:
        source.read()

    assert caught.value.code == 'dispatch_safety_fault_map_switch_failed'
    assert 'private callback failure' not in str(caught.value)
    assert not coordinator.observation_path.exists()


def test_malformed_real_source_is_rejected_before_fault_application(
    tmp_path: Path,
) -> None:
    """The scenario wrapper cannot turn a malformed object into evidence."""
    coordinator = _coordinator(
        tmp_path,
        TextGazeboSafetyProfile.EMERGENCY_STOP,
    )
    delegate = _SequenceSource(object())
    source = DispatchSafetyRobotStateSource(delegate, coordinator)

    with pytest.raises(DispatchSafetyFaultError) as caught:
        source.read()

    assert caught.value.code == 'dispatch_safety_fault_state_invalid'
    assert delegate.calls == 1


@pytest.mark.parametrize(
    'mutate',
    ('mode', 'extra', 'duplicate', 'noncanonical', 'symlink'),
)
def test_observation_reader_rejects_noncanonical_or_unsafe_files(
    tmp_path: Path,
    mutate: str,
) -> None:
    """Reject mode, shape, duplicate-key, byte, and link substitution."""
    profile = TextGazeboSafetyProfile.EMERGENCY_STOP
    coordinator = _coordinator(tmp_path, profile)
    source = DispatchSafetyRobotStateSource(
        _SequenceSource(
            _evidence('proposal', 99.5),
            _evidence('dispatch', 100.5),
        ),
        coordinator,
    )
    source.read()
    coordinator.arm_after_claim(_claim())
    source.read()
    original = coordinator.observation_path
    payload = original.read_bytes()
    candidate = tmp_path / f'{mutate}.json'

    if mutate == 'mode':
        candidate.write_bytes(payload)
        candidate.chmod(0o644)
    elif mutate == 'extra':
        value = json.loads(payload)
        value['extra'] = 1
        candidate.write_text(
            json.dumps(value, sort_keys=True, separators=(',', ':')),
            encoding='ascii',
        )
        candidate.chmod(0o600)
    elif mutate == 'duplicate':
        candidate.write_bytes(
            payload[:-1] + b',"claim_arm_count":1}'
        )
        candidate.chmod(0o600)
    elif mutate == 'noncanonical':
        candidate.write_bytes(payload + b'\n')
        candidate.chmod(0o600)
    else:
        candidate.symlink_to(original)

    selected = candidate.resolve() if mutate != 'symlink' else candidate
    with pytest.raises(DispatchSafetyFaultError) as caught:
        read_dispatch_safety_observation(selected)

    assert caught.value.code == 'dispatch_safety_fault_observation_invalid'


def test_existing_observation_name_is_never_overwritten(
    tmp_path: Path,
) -> None:
    """Reject a pre-existing regular file or symlink at construction time."""
    database = tmp_path / 'agent.sqlite3'
    path = dispatch_safety_observation_path(database)
    path.write_text('existing-private-value', encoding='utf-8')
    before = path.read_bytes()

    with pytest.raises(DispatchSafetyFaultError) as caught:
        DispatchSafetyFaultCoordinator(
            database,
            TextGazeboSafetyProfile.EMERGENCY_STOP,
        )

    assert caught.value.code == 'dispatch_safety_fault_observation_invalid'
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    'database_path',
    (
        ':memory:',
        Path(':memory:'),
        '',
        '   ',
        Path(''),
    ),
)
def test_coordinator_rejects_non_durable_database_paths(
    database_path: str | Path,
) -> None:
    """Path wrappers and empty values cannot bypass durable DB binding."""
    with pytest.raises(ValueError):
        DispatchSafetyFaultCoordinator(
            database_path,
            TextGazeboSafetyProfile.EMERGENCY_STOP,
        )


def test_profile_callback_and_observation_arguments_are_strict(
    tmp_path: Path,
) -> None:
    """Caller data cannot create an unallowlisted profile or output path."""
    database = tmp_path / 'agent.sqlite3'
    with pytest.raises(ValueError):
        DispatchSafetyFaultCoordinator(database, 'arbitrary_fault')
    with pytest.raises(TypeError):
        DispatchSafetyFaultCoordinator(
            database,
            TextGazeboSafetyProfile.MAP_REVISION_CHANGED,
        )
    with pytest.raises(ValueError):
        DispatchSafetyFaultCoordinator(
            database,
            TextGazeboSafetyProfile.EMERGENCY_STOP,
            map_switch_callback=lambda: None,
        )
    with pytest.raises(DispatchSafetyFaultError):
        DispatchSafetyFaultCoordinator(
            database,
            TextGazeboSafetyProfile.EMERGENCY_STOP,
            observation_path=tmp_path / 'caller-selected.json',
        )


def test_closing_coordinator_fails_after_the_real_read(
    tmp_path: Path,
) -> None:
    """Owner shutdown prevents injection without skipping the real source."""
    coordinator = _coordinator(
        tmp_path,
        TextGazeboSafetyProfile.EMERGENCY_STOP,
    )
    delegate = _SequenceSource(_evidence('still-read-first', 99.5))
    source = DispatchSafetyRobotStateSource(delegate, coordinator)
    coordinator.close()

    with pytest.raises(DispatchSafetyFaultError) as caught:
        source.read()

    assert caught.value.code == 'dispatch_safety_fault_closed'
    assert delegate.calls == 1
