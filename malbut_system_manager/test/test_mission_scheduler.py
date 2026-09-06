"""Unit tests for the ROS-independent mission scheduling policy."""

import pytest

from malbut_system_manager.mission_scheduler import MissionScheduler
from malbut_system_manager.models import (
    CapabilityManifest,
    CommandKind,
    ControlMode,
    ExecutionMode,
    MissionPriority,
    MissionRecord,
    MissionState,
    SystemState,
    TerminalOutcome,
)
from malbut_system_manager.state_store import StateStore


def _mission(
    mission_id,
    *,
    mode=ExecutionMode.FOREGROUND,
    priority=MissionPriority.NORMAL,
):
    capability = CapabilityManifest(
        capability_id=f'capability_{mission_id}',
        title=mission_id,
        description=f'Test capability for {mission_id}',
        command_kind=CommandKind.ACTION,
        command_name=f'/test/{mission_id}',
        command_type='test_interfaces/action/Test',
        execution_mode=mode,
        priority=priority,
        input_fields={},
        interface_type=object,
        source_path=f'/test/{mission_id}.yaml',
    )
    return MissionRecord(
        mission_id=mission_id,
        capability=capability,
        arguments={},
    )


def _ready_scheduler(*, conflict_policy=None):
    state = StateStore()
    state.ready = True
    if conflict_policy is None:
        return state, MissionScheduler(state)
    return state, MissionScheduler(
        state,
        conflict_policy=conflict_policy,
    )


def _completion(effects, mission_id):
    return next(
        completion
        for completion in effects.complete
        if completion.mission_id == mission_id
    )


def test_background_missions_run_concurrently_without_leaving_idle():
    """Background-only work must remain concurrent and report IDLE."""
    state, scheduler = _ready_scheduler()
    first = _mission('background-1', mode=ExecutionMode.BACKGROUND)
    second = _mission('background-2', mode=ExecutionMode.BACKGROUND)

    first_effects = scheduler.submit(first)
    second_effects = scheduler.submit(second)

    assert first_effects.start == ['background-1']
    assert second_effects.start == ['background-2']
    assert list(state.active_background) == [
        'background-1',
        'background-2',
    ]
    assert state.system_state is SystemState.IDLE


def test_foreground_mission_sets_executing_system_state():
    """One active foreground mission must make the runtime executing."""
    state, scheduler = _ready_scheduler()

    effects = scheduler.submit(_mission('foreground'))

    assert effects.start == ['foreground']
    assert state.active_foreground['foreground'].state is MissionState.RUNNING
    assert state.system_state is SystemState.EXECUTING_MISSION


def test_lower_priority_conflict_is_rejected_without_side_effects():
    """A lower-priority request must not disturb its active blocker."""
    state, scheduler = _ready_scheduler()
    active = _mission('active', priority=MissionPriority.HIGH)
    lower = _mission('lower', priority=MissionPriority.NORMAL)
    scheduler.submit(active)

    effects = scheduler.submit(lower)

    assert effects.start == []
    assert effects.cancel == []
    assert _completion(effects, 'lower').outcome is TerminalOutcome.ABORTED
    assert 'higher priority HIGH' in _completion(
        effects,
        'lower',
    ).message
    assert state.active_foreground['active'].state is MissionState.RUNNING
    assert state.get('lower') is None


@pytest.mark.parametrize(
    'incoming_priority',
    [MissionPriority.NORMAL, MissionPriority.HIGH],
)
def test_equal_or_higher_priority_waits_for_conflict_to_be_canceled(
    incoming_priority,
):
    """A preemptor starts only after the downstream reports CANCELED."""
    state, scheduler = _ready_scheduler()
    active = _mission('active', priority=MissionPriority.NORMAL)
    incoming = _mission('incoming', priority=incoming_priority)
    scheduler.submit(active)

    submitted = scheduler.submit(incoming)

    assert submitted.start == []
    assert submitted.cancel == ['active']
    assert state.active_foreground['active'].state is MissionState.CANCELING
    assert state.pending['incoming'].waiting_for == {'active'}

    terminal = scheduler.handle_terminal(
        'active',
        TerminalOutcome.CANCELED,
    )

    assert terminal.start == ['incoming']
    assert state.active_foreground['incoming'].state is MissionState.RUNNING
    assert state.suspended['active'].state is MissionState.SUSPENDED


def test_suspended_mission_resumes_after_preemptor_finishes():
    """A preempted public goal must restart after its preemptor ends."""
    state, scheduler = _ready_scheduler()
    original = _mission('original', priority=MissionPriority.LOW)
    preemptor = _mission('preemptor', priority=MissionPriority.HIGH)
    scheduler.submit(original)
    scheduler.submit(preemptor)
    scheduler.handle_terminal('original', TerminalOutcome.CANCELED)

    effects = scheduler.handle_terminal(
        'preemptor',
        TerminalOutcome.SUCCEEDED,
        result_yaml='success: true',
    )

    assert _completion(
        effects,
        'preemptor',
    ).outcome is TerminalOutcome.SUCCEEDED
    assert effects.start == ['original']
    assert state.active_foreground['original'].state is MissionState.RUNNING
    assert 'original' not in state.suspended


def test_explicit_cancel_completes_pending_goal_and_restores_old_goal():
    """Canceling a pending preemptor must resolve its upstream goal."""
    state, scheduler = _ready_scheduler()
    original = _mission('original', priority=MissionPriority.LOW)
    pending = _mission('pending', priority=MissionPriority.HIGH)
    scheduler.submit(original)
    scheduler.submit(pending)

    accepted, effects = scheduler.request_cancel('pending')

    assert accepted
    assert _completion(effects, 'pending').outcome is TerminalOutcome.CANCELED
    assert state.get('pending') is None
    assert state.active_foreground['original'].preempted_by is None

    terminal = scheduler.handle_terminal(
        'original',
        TerminalOutcome.CANCELED,
    )
    assert terminal.start == ['original']
    assert state.active_foreground['original'].state is MissionState.RUNNING


def test_explicit_cancel_completes_suspended_goal_without_resuming_it():
    """Canceling a suspended goal must remove its saved re-execution."""
    state, scheduler = _ready_scheduler()
    original = _mission('original', priority=MissionPriority.LOW)
    preemptor = _mission('preemptor', priority=MissionPriority.HIGH)
    scheduler.submit(original)
    scheduler.submit(preemptor)
    scheduler.handle_terminal('original', TerminalOutcome.CANCELED)

    accepted, effects = scheduler.request_cancel('original')

    assert accepted
    assert _completion(effects, 'original').outcome is TerminalOutcome.CANCELED
    assert state.get('original') is None

    finished = scheduler.handle_terminal(
        'preemptor',
        TerminalOutcome.SUCCEEDED,
    )
    assert finished.start == []
    assert state.get('original') is None


def test_rejected_preemption_cancel_aborts_preemptor_and_keeps_active():
    """A refused preemption must atomically restore the current mission."""
    state, scheduler = _ready_scheduler()
    active = _mission('active', priority=MissionPriority.LOW)
    preemptor = _mission('preemptor', priority=MissionPriority.HIGH)
    scheduler.submit(active)
    scheduler.submit(preemptor)

    effects = scheduler.handle_cancel_rejected(
        'active',
        'downstream refused cancellation',
    )

    assert _completion(
        effects,
        'preemptor',
    ).outcome is TerminalOutcome.ABORTED
    assert state.active_foreground['active'].state is MissionState.RUNNING
    assert state.active_foreground['active'].preempted_by is None
    assert state.get('preemptor') is None


def test_rejected_user_cancel_aborts_public_goal_but_tracks_downstream():
    """Resolve the caller while retaining authority over running work."""
    state, scheduler = _ready_scheduler()
    active = _mission('active')
    scheduler.submit(active)
    accepted, requested = scheduler.request_cancel('active')

    effects = scheduler.handle_cancel_rejected(
        'active',
        'downstream refused cancellation',
    )

    assert accepted
    assert requested.cancel == ['active']
    completion = _completion(effects, 'active')
    assert completion.outcome is TerminalOutcome.ABORTED
    assert 'still running' in completion.message
    assert state.active_foreground['active'].state is MissionState.RUNNING
    assert not state.active_foreground['active'].user_cancel_requested


def test_user_cancel_during_preemption_cannot_leave_pending_deadlock():
    """Abort the preemptor if the shared downstream cancel is rejected."""
    state, scheduler = _ready_scheduler()
    active = _mission('active', priority=MissionPriority.LOW)
    preemptor = _mission('preemptor', priority=MissionPriority.HIGH)
    scheduler.submit(active)
    scheduler.submit(preemptor)
    scheduler.request_cancel('active')

    effects = scheduler.handle_cancel_rejected(
        'active',
        'downstream refused cancellation',
    )

    assert _completion(
        effects,
        'preemptor',
    ).outcome is TerminalOutcome.ABORTED
    assert _completion(
        effects,
        'active',
    ).outcome is TerminalOutcome.ABORTED
    assert state.get('preemptor') is None
    assert state.active_foreground['active'].state is MissionState.RUNNING
    assert state.active_foreground['active'].preempted_by is None


def test_orphaned_downstream_is_never_resumed_after_later_preemption():
    """A mission whose public goal ended must not start a new generation."""
    state, scheduler = _ready_scheduler()
    active = _mission('active', priority=MissionPriority.LOW)
    preemptor = _mission('preemptor', priority=MissionPriority.HIGH)
    scheduler.submit(active)
    scheduler.request_cancel('active')
    scheduler.handle_cancel_rejected(
        'active',
        'downstream refused cancellation',
    )

    scheduler.submit(preemptor)
    canceled = scheduler.handle_terminal(
        'active',
        TerminalOutcome.CANCELED,
    )
    finished = scheduler.handle_terminal(
        'preemptor',
        TerminalOutcome.SUCCEEDED,
    )

    assert canceled.start == ['preemptor']
    assert state.get('active') is None
    assert 'active' not in finished.start


def test_higher_priority_request_replaces_a_pending_preemptor():
    """Keep the highest-priority request while downstream cancellation waits."""
    state, scheduler = _ready_scheduler()
    active = _mission('active', priority=MissionPriority.LOW)
    normal = _mission('normal', priority=MissionPriority.NORMAL)
    urgent = _mission('urgent', priority=MissionPriority.URGENT)
    scheduler.submit(active)
    scheduler.submit(normal)

    effects = scheduler.submit(urgent)

    assert _completion(effects, 'normal').outcome is TerminalOutcome.ABORTED
    assert state.get('normal') is None
    assert state.pending['urgent'].waiting_for == {'active'}
    assert state.active_foreground['active'].preempted_by == 'urgent'

    terminal = scheduler.handle_terminal(
        'active',
        TerminalOutcome.CANCELED,
    )
    assert terminal.start == ['urgent']


def test_replaced_pending_recomputes_custom_foreground_conflicts():
    """A replacement must cancel its own blockers and release old ones."""
    conflict_pairs = {
        frozenset(('first', 'old')),
        frozenset(('second', 'new')),
    }

    def conflict_policy(active, incoming):
        return frozenset((active.mission_id, incoming.mission_id)) in (
            conflict_pairs
        )

    state, scheduler = _ready_scheduler(conflict_policy=conflict_policy)
    first = _mission('first', priority=MissionPriority.LOW)
    second = _mission('second', priority=MissionPriority.LOW)
    old = _mission('old', priority=MissionPriority.NORMAL)
    new = _mission('new', priority=MissionPriority.HIGH)
    scheduler.submit(first)
    scheduler.submit(second)
    scheduler.submit(old)

    effects = scheduler.submit(new)

    assert _completion(effects, 'old').outcome is TerminalOutcome.ABORTED
    assert effects.cancel == ['second']
    assert state.active_foreground['first'].preempted_by is None
    assert state.active_foreground['second'].preempted_by == 'new'
    assert state.pending['new'].waiting_for == {'second'}


def test_pending_replacement_cannot_preempt_higher_new_conflict():
    """Recheck all blockers before replacing an accepted pending mission."""
    conflict_pairs = {
        frozenset(('low', 'old')),
        frozenset(('high', 'new')),
    }

    def conflict_policy(active, incoming):
        return frozenset((active.mission_id, incoming.mission_id)) in (
            conflict_pairs
        )

    state, scheduler = _ready_scheduler(conflict_policy=conflict_policy)
    high = _mission('high', priority=MissionPriority.HIGH)
    low = _mission('low', priority=MissionPriority.LOW)
    old = _mission('old', priority=MissionPriority.NORMAL)
    new = _mission('new', priority=MissionPriority.NORMAL)
    scheduler.submit(high)
    scheduler.submit(low)
    scheduler.submit(old)

    effects = scheduler.submit(new)

    assert _completion(effects, 'new').outcome is TerminalOutcome.ABORTED
    assert 'higher priority HIGH' in _completion(effects, 'new').message
    assert state.pending['old'].waiting_for == {'low'}
    assert state.active_foreground['low'].preempted_by == 'old'


def test_dispatch_timeout_aborts_public_goal_but_retains_authority():
    """An unresolved dispatch must remain tracked after caller failure."""
    state, scheduler = _ready_scheduler()
    active = _mission('active')
    scheduler.submit(active)

    effects = scheduler.handle_dispatch_timeout(
        'active',
        'goal response timed out',
    )

    assert _completion(effects, 'active').outcome is TerminalOutcome.ABORTED
    assert state.active_foreground['active'].state is MissionState.RUNNING
    assert not state.active_foreground['active'].resumable


def test_dispatch_timeout_during_preemption_aborts_waiting_request():
    """A preemptor must not start while its blocker dispatch is unresolved."""
    state, scheduler = _ready_scheduler()
    active = _mission('active', priority=MissionPriority.LOW)
    preemptor = _mission('preemptor', priority=MissionPriority.HIGH)
    scheduler.submit(active)
    scheduler.submit(preemptor)

    effects = scheduler.handle_dispatch_timeout(
        'active',
        'goal response timed out',
    )

    assert _completion(effects, 'active').outcome is TerminalOutcome.ABORTED
    assert (
        _completion(effects, 'preemptor').outcome
        is TerminalOutcome.ABORTED
    )
    assert state.get('preemptor') is None
    assert state.active_foreground['active'].preempted_by is None


def test_dispatch_timeout_resumes_compatible_suspended_mission():
    """Aborting a multi-blocker preemptor must release safe old work."""
    conflicts = {
        frozenset(('first', 'new')),
        frozenset(('second', 'new')),
    }

    def conflict_policy(active, incoming):
        return frozenset((active.mission_id, incoming.mission_id)) in conflicts

    state, scheduler = _ready_scheduler(conflict_policy=conflict_policy)
    first = _mission('first', priority=MissionPriority.LOW)
    second = _mission('second', priority=MissionPriority.LOW)
    new = _mission('new', priority=MissionPriority.HIGH)
    scheduler.submit(first)
    scheduler.submit(second)
    scheduler.submit(new)
    scheduler.handle_terminal('second', TerminalOutcome.CANCELED)

    effects = scheduler.handle_dispatch_timeout(
        'first',
        'goal response timed out',
    )

    assert effects.start == ['second']
    assert state.active_foreground['second'].state is MissionState.RUNNING
    assert state.get('new') is None


def test_preemption_after_dispatch_timeout_can_abort_waiting_request():
    """A later request must be released if the unknown blocker cannot stop."""
    state, scheduler = _ready_scheduler()
    active = _mission('active', priority=MissionPriority.LOW)
    preemptor = _mission('preemptor', priority=MissionPriority.HIGH)
    scheduler.submit(active)
    scheduler.handle_dispatch_timeout(
        'active',
        'goal response timed out',
    )
    submitted = scheduler.submit(preemptor)

    effects = scheduler.handle_cancel_rejected(
        'active',
        'cancel resolution timed out',
    )

    assert submitted.cancel == ['active']
    assert _completion(
        effects,
        'preemptor',
    ).outcome is TerminalOutcome.ABORTED
    assert state.get('preemptor') is None
    assert state.active_foreground['active'].state is MissionState.RUNNING


def test_injected_compatibility_policy_allows_multiple_foregrounds():
    """The scheduler must admit foreground pairs approved by policy."""
    state, scheduler = _ready_scheduler(
        conflict_policy=lambda _active, _incoming: False,
    )

    first = scheduler.submit(_mission('first'))
    second = scheduler.submit(_mission('second'))

    assert first.start == ['first']
    assert second.start == ['second']
    assert list(state.active_foreground) == ['first', 'second']
    assert state.system_state is SystemState.EXECUTING_MISSION


def test_system_state_uses_documented_precedence():
    """Aggregate state must use emergency-to-idle precedence exactly."""
    state = StateStore()
    foreground = _mission('foreground')
    state.activate(foreground)
    state.recharging = True

    assert state.system_state is SystemState.BOOTING

    state.emergency = True
    assert state.system_state is SystemState.EMERGENCY

    state.ready = True
    assert state.system_state is SystemState.EMERGENCY

    state.emergency = False
    assert state.system_state is SystemState.RECHARGING

    state.recharging = False
    assert state.system_state is SystemState.EXECUTING_MISSION

    state.remove('foreground')
    assert state.system_state is SystemState.IDLE

    state.control_mode = ControlMode.MANUAL
    assert state.system_state is SystemState.IDLE


def test_shutdown_aborts_waiting_work_and_cancels_active_work():
    """Shutdown must not start or resume another mission."""
    state, scheduler = _ready_scheduler()
    active = _mission('active', priority=MissionPriority.LOW)
    pending = _mission('pending', priority=MissionPriority.HIGH)
    scheduler.submit(active)
    scheduler.submit(pending)

    effects = scheduler.request_shutdown()

    assert effects.start == []
    assert effects.cancel == ['active']
    assert _completion(effects, 'pending').outcome is TerminalOutcome.ABORTED
    assert state.get('pending') is None
    assert state.active_foreground['active'].state is MissionState.CANCELING

    finished = scheduler.handle_terminal(
        'active',
        TerminalOutcome.CANCELED,
    )
    assert _completion(finished, 'active').outcome is TerminalOutcome.ABORTED
    assert finished.start == []
    assert state.system_state is SystemState.IDLE
