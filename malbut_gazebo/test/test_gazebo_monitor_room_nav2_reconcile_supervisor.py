"""Tests for the pure exact-UUID Nav2 reconciliation supervisor."""

import pytest

from malbut_gazebo.gazebo_monitor_room_nav2_adapter import Nav2CancelRequest
from malbut_gazebo.gazebo_monitor_room_nav2_reconcile_store import (
    GazeboMonitorRoomNav2ReconcileStore,
)
from malbut_gazebo.gazebo_monitor_room_nav2_reconcile_supervisor import (
    GazeboMonitorRoomNav2ReconcileSupervisor,
    Nav2ReconcileQuiescenceValidation,
    Nav2ReconcileTransport,
    TrustedNav2ReconcileQuiescenceValidator,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    CancelOperation,
    GazeboMonitorRoomStore,
    OrderedSemanticSample,
    PrepareOperation,
)


_DIGEST_A = 'a' * 64
_DIGEST_B = 'b' * 64
_DIGEST_C = 'c' * 64
_BOOT = '11111111-2222-3333-4444-555555555555'


def _boot():
    return _BOOT


class _Clock:
    def __init__(self, value=11.0):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


class _FakeTransport(Nav2ReconcileTransport):
    def __init__(self):
        self.status = 'unknown'
        self.result = 'unknown'
        self.cancel = 'unknown'
        self.status_calls = []
        self.result_calls = []
        self.cancel_calls = []
        self.status_hook = None
        self.result_hook = None
        self.cancel_hook = None
        self.status_error = None
        self.result_error = None
        self.cancel_error = None
        self.status_raw = None
        self.result_raw = None
        self.cancel_raw = None
        self.send_calls = 0

    @staticmethod
    def _goal_report(query, status):
        return {
            'operation_id': query.operation_id,
            'goal_uuid': query.goal_uuid,
            'binding_digest': query.binding_digest,
            'fence_epoch': query.fence_epoch,
            'status': status,
            'evidence_digest': _DIGEST_B,
        }

    def observe_status(self, query):
        self.status_calls.append(query)
        if self.status_hook is not None:
            self.status_hook(query)
        if self.status_error is not None:
            raise self.status_error
        if self.status_raw is not None:
            return self.status_raw
        return self._goal_report(query, self.status)

    def get_result(self, query):
        self.result_calls.append(query)
        if self.result_hook is not None:
            self.result_hook(query)
        if self.result_error is not None:
            raise self.result_error
        if self.result_raw is not None:
            return self.result_raw
        return self._goal_report(query, self.result)

    def cancel_goal(self, request):
        self.cancel_calls.append(request)
        if self.cancel_hook is not None:
            self.cancel_hook(request)
        if self.cancel_error is not None:
            raise self.cancel_error
        if self.cancel_raw is not None:
            return self.cancel_raw
        return self._goal_report(request, self.cancel)


class _QuiescenceValidator(TrustedNav2ReconcileQuiescenceValidator):
    def __init__(self, *, outcome='quiescent'):
        self.outcome = outcome
        self.calls = []
        self.hook = None
        self.error = None
        self.mutate_result = None

    def validate_quiescence(self, request, *, checked_at):
        self.calls.append((request, checked_at))
        if self.hook is not None:
            self.hook(request)
        if self.error is not None:
            raise self.error
        result = Nav2ReconcileQuiescenceValidation(
            operation_id=request.operation_id,
            goal_uuid=request.goal_uuid,
            binding_digest=request.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            checked_at=checked_at,
            outcome=self.outcome,
            evidence_digest=_DIGEST_C,
        )
        if self.mutate_result is not None:
            self.mutate_result(result)
        return result


def _request():
    return PrepareOperation(
        prepare_request_id='prepare-1',
        operation_id='operation-1',
        robot_id='robot-1',
        map_id='home-map',
        map_revision='map-revision-1',
        semantic_revision='semantic-revision-1',
        zones_digest=_DIGEST_A,
        target_binding_digest=_DIGEST_A,
        effects_digest=_DIGEST_A,
        profile_digest=_DIGEST_A,
        plan_digest=_DIGEST_A,
        ordered_semantic_samples=(
            OrderedSemanticSample(0, 0, 0, 1000, 2000),
        ),
        deadline=100.0,
    )


def _core_unknown(tmp_path, *, source_state='delivery_unknown'):
    core = GazeboMonitorRoomStore(
        tmp_path / 'core.sqlite3', boot_id_reader=_boot
    )
    core.prepare(_request(), now=1.0)
    core.acquire_lease(
        'operation-1',
        worker_id='core-worker',
        expected_fence=0,
        lease_seconds=20.0,
        now=2.0,
    )
    core.begin_preflight(
        core.transition_token(
            'operation-1', worker_id='core-worker'
        ),
        now=3.0,
    )
    core.record_send_intent(
        core.transition_token(
            'operation-1', worker_id='core-worker'
        ),
        preflight_digest=_DIGEST_A,
        now=4.0,
    )
    core.record_navigating(
        core.transition_token(
            'operation-1', worker_id='core-worker'
        ),
        acceptance_digest=_DIGEST_A,
        now=5.0,
    )
    if source_state == 'cancel_unknown':
        core.request_cancel(
            CancelOperation(
                cancel_request_id='core-cancel-1',
                transition=core.transition_token(
                    'operation-1', worker_id='core-worker'
                ),
                reason_code='operator_requested',
            ),
            now=6.0,
        )
        core.record_cancel_unknown(
            core.transition_token(
                'operation-1', worker_id='core-worker'
            ),
            code='nav2_cancel_terminal_not_observable',
            evidence_digest=_DIGEST_A,
            now=7.0,
        )
    else:
        core.record_delivery_unknown(
            core.transition_token(
                'operation-1', worker_id='core-worker'
            ),
            code='nav2_goal_not_observable',
            evidence_digest=_DIGEST_A,
            now=6.0,
        )
    return core


def _world(
    tmp_path,
    *,
    source_state='delivery_unknown',
    dwell=2.0,
    validator=None,
):
    core = _core_unknown(tmp_path, source_state=source_state)
    sidecar = GazeboMonitorRoomNav2ReconcileStore(
        tmp_path / 'reconcile.sqlite3',
        core_store_namespace=core.store_namespace,
        quiescence_dwell_seconds=dwell,
        boot_id_reader=_boot,
    )
    sidecar.register_unknown(core, 'operation-1', now=10.0)
    transport = _FakeTransport()
    clock = _Clock()
    supervisor = GazeboMonitorRoomNav2ReconcileSupervisor(
        core,
        sidecar,
        transport,
        worker_id='reconcile-worker',
        lease_seconds=20.0,
        quiescence_validator=validator,
        clock=clock,
    )
    return core, sidecar, transport, clock, supervisor


def _terminal(world, *, status='succeeded', attempt='reconcile-1'):
    _core, _sidecar, transport, _clock, supervisor = world
    transport.status = status
    return supervisor.reconcile_once('operation-1', attempt_id=attempt)


def test_construction_has_no_transport_call_or_start_surface(tmp_path):
    """Construction and the supervisor API cannot start or resend a goal."""
    _core, _sidecar, transport, _clock, supervisor = _world(tmp_path)
    assert transport.status_calls == []
    assert transport.result_calls == []
    assert transport.cancel_calls == []
    assert transport.send_calls == 0
    assert not hasattr(supervisor, 'ensure_started')
    assert not hasattr(supervisor, 'send_goal')


def test_terminal_status_short_circuits_result_but_stays_blocked(tmp_path):
    """Exact terminal status is recorded without release or core rewrite."""
    world = _world(tmp_path)
    core, _sidecar, transport, _clock, _supervisor = world
    observed = _terminal(world, status='succeeded')
    assert len(transport.status_calls) == 1
    assert transport.result_calls == []
    assert observed.state == 'blocked_terminal_observed'
    assert observed.terminal_status == 'succeeded'
    assert observed.robot_blocked is True
    assert observed.operation_success is False
    assert core.observe('operation-1').state == 'delivery_unknown'
    assert transport.send_calls == 0


def test_active_status_then_exact_get_result_records_terminal(tmp_path):
    """A nonterminal status falls through to exact retained result lookup."""
    world = _world(tmp_path)
    _core, _sidecar, transport, _clock, supervisor = world
    transport.status = 'active'
    transport.result = 'aborted'
    observed = supervisor.reconcile_once(
        'operation-1', attempt_id='reconcile-1'
    )
    assert len(transport.status_calls) == 1
    assert len(transport.result_calls) == 1
    assert transport.status_calls[0].goal_uuid == (
        transport.result_calls[0].goal_uuid
    )
    assert observed.terminal_status == 'aborted'
    assert observed.state == 'blocked_terminal_observed'


@pytest.mark.parametrize(
    ('status', 'result'),
    [
        ('unknown', 'unknown'),
        ('accepted', 'unknown'),
        ('active', 'rejected'),
    ],
)
def test_absence_and_nonterminal_reports_never_prove_not_sent(
    tmp_path, status, result
):
    """Standard Nav2 absence remains an unresolved, blocking ambiguity."""
    world = _world(tmp_path)
    _core, _sidecar, transport, _clock, supervisor = world
    transport.status = status
    transport.result = result
    observed = supervisor.reconcile_once(
        'operation-1', attempt_id='reconcile-1'
    )
    assert observed.state == 'blocked_unresolved'
    assert observed.robot_blocked is True
    assert observed.full_drop_certificate_digest is None
    assert transport.send_calls == 0


def test_status_and_result_requests_bind_exact_source_goal(tmp_path):
    """Both read paths use the immutable source UUID, binding, and fence."""
    world = _world(tmp_path)
    _core, sidecar, transport, _clock, supervisor = world
    supervisor.reconcile_once('operation-1', attempt_id='reconcile-1')
    anchor = sidecar.source_anchor('operation-1')
    for query in transport.status_calls + transport.result_calls:
        assert query.operation_id == anchor.operation_id
        assert query.goal_uuid == anchor.goal_uuid
        assert query.binding_digest == anchor.binding_digest
        assert query.worker_id == 'reconcile-worker'
        assert query.fence_epoch == 1


def test_same_reconcile_identity_never_reissues_reads(tmp_path):
    """Restart-shaped replay of both claimed reads makes no new call."""
    world = _world(tmp_path)
    _core, sidecar, transport, _clock, supervisor = world
    first = supervisor.reconcile_once(
        'operation-1', attempt_id='reconcile-1'
    )
    assert first.state == 'blocked_unresolved'
    assert (len(transport.status_calls), len(transport.result_calls)) == (1, 1)
    second = supervisor.reconcile_once(
        'operation-1', attempt_id='reconcile-1'
    )
    assert second.state == 'blocked_unresolved'
    assert (len(transport.status_calls), len(transport.result_calls)) == (1, 1)
    assert len(sidecar.attempts('operation-1')) == 2


@pytest.mark.parametrize('which', ['status', 'result'])
def test_read_exception_is_unknown_and_same_claim_is_not_retried(
    tmp_path, which
):
    """A read crash stays unresolved and does not replay its call identity."""
    world = _world(tmp_path)
    _core, _sidecar, transport, _clock, supervisor = world
    if which == 'status':
        transport.status_error = RuntimeError('private status marker')
    else:
        transport.result_error = RuntimeError('private result marker')
    first = supervisor.reconcile_once(
        'operation-1', attempt_id='reconcile-1'
    )
    counts = (len(transport.status_calls), len(transport.result_calls))
    assert first.state == 'blocked_unresolved'
    supervisor.reconcile_once('operation-1', attempt_id='reconcile-1')
    assert (len(transport.status_calls), len(transport.result_calls)) == counts


def test_malformed_or_mismatched_goal_report_stays_unknown(tmp_path):
    """Weak report shapes cannot promote terminal evidence."""
    world = _world(tmp_path)
    _core, _sidecar, transport, _clock, supervisor = world
    transport.status_raw = {'status': 'succeeded'}
    transport.result_raw = {
        'operation_id': 'operation-1',
        'goal_uuid': 'f' * 32,
        'binding_digest': _DIGEST_A,
        'fence_epoch': 1,
        'status': 'succeeded',
        'evidence_digest': _DIGEST_B,
    }
    observed = supervisor.reconcile_once(
        'operation-1', attempt_id='reconcile-1'
    )
    assert observed.state == 'blocked_unresolved'
    assert observed.terminal_status is None


def test_transport_cannot_mutate_query_and_echo_new_authority(tmp_path):
    """Frozen-object bypass on a query is caught after collaborator return."""
    world = _world(tmp_path)
    _core, _sidecar, transport, _clock, supervisor = world

    def mutate(query):
        object.__setattr__(query, 'goal_uuid', 'f' * 32)

    transport.status_hook = mutate
    transport.status = 'succeeded'
    observed = supervisor.reconcile_once(
        'operation-1', attempt_id='reconcile-1'
    )
    assert observed.state == 'blocked_unresolved'
    assert observed.terminal_status is None


def test_post_call_lease_expiry_discards_terminal_report(tmp_path):
    """A report crossing the claimed lease edge cannot advance state."""
    world = _world(tmp_path)
    _core, sidecar, transport, clock, supervisor = world
    supervisor._lease_seconds = 2.0
    transport.status = 'succeeded'
    transport.status_hook = lambda _query: setattr(clock, 'value', 13.0)
    observed = supervisor.reconcile_once(
        'operation-1', attempt_id='reconcile-1'
    )
    assert observed.state == 'blocked_unresolved'
    assert len(sidecar.attempts('operation-1')) == 1


def test_explicit_cancel_is_exact_one_shot_and_terminal_stays_blocked(tmp_path):
    """One explicit cancel uses exact UUID and canceled still needs quiet."""
    world = _world(tmp_path, source_state='cancel_unknown')
    core, sidecar, transport, _clock, supervisor = world
    transport.cancel = 'canceled'
    observed = supervisor.cancel_once(
        'operation-1', cancel_request_id='cancel-reconcile-1'
    )
    assert len(transport.cancel_calls) == 1
    request = transport.cancel_calls[0]
    anchor = sidecar.source_anchor('operation-1')
    assert request.goal_uuid == anchor.goal_uuid
    assert request.binding_digest == anchor.binding_digest
    assert request.wire_payload_digest == sidecar.attempts(
        'operation-1'
    )[0].wire_payload_digest
    assert observed.state == 'blocked_terminal_observed'
    assert observed.terminal_status == 'canceled'
    assert observed.robot_blocked is True
    assert core.observe('operation-1').state == 'cancel_unknown'
    assert transport.send_calls == 0


@pytest.mark.parametrize('status', ['active', 'rejected', 'unknown'])
def test_nonterminal_cancel_report_remains_unresolved(tmp_path, status):
    """Cancel ACK, rejection, or absence is not terminal evidence."""
    world = _world(tmp_path)
    _core, _sidecar, transport, _clock, supervisor = world
    transport.cancel = status
    observed = supervisor.cancel_once(
        'operation-1', cancel_request_id='cancel-reconcile-1'
    )
    assert observed.state == 'blocked_unresolved'
    assert observed.terminal_status is None


def test_cancel_crash_is_persisted_and_same_id_never_reissued(tmp_path):
    """Crash after claim stays unknown and requires a new explicit identity."""
    world = _world(tmp_path)
    _core, sidecar, transport, _clock, supervisor = world
    transport.cancel_error = RuntimeError('private cancel marker')
    first = supervisor.cancel_once(
        'operation-1', cancel_request_id='cancel-reconcile-1'
    )
    assert first.state == 'blocked_unresolved'
    assert len(transport.cancel_calls) == 1
    assert len(sidecar.attempts('operation-1')) == 1
    second = supervisor.cancel_once(
        'operation-1', cancel_request_id='cancel-reconcile-1'
    )
    assert second.state == 'blocked_unresolved'
    assert len(transport.cancel_calls) == 1


def test_restart_after_cancel_claim_never_issues_claimed_call(tmp_path):
    """A process crash after durable claim cannot replay cancel on restart."""
    world = _world(tmp_path)
    core, sidecar, _transport, clock, _supervisor = world
    lease = sidecar.acquire_lease(
        'operation-1',
        worker_id='reconcile-worker',
        expected_fence=0,
        lease_seconds=20.0,
        now=clock.value,
    )
    anchor = sidecar.source_anchor('operation-1')
    request = Nav2CancelRequest(
        operation_id='operation-1',
        worker_id='reconcile-worker',
        fence_epoch=lease.fence_epoch,
        cancel_request_id='cancel-reconcile-1',
        goal_uuid=anchor.goal_uuid,
        binding_digest=anchor.binding_digest,
    )
    assert sidecar.claim_attempt(
        'operation-1',
        attempt_id=request.cancel_request_id,
        kind='cancel',
        worker_id=request.worker_id,
        fence_epoch=request.fence_epoch,
        request_fingerprint=request.request_fingerprint,
        wire_payload_digest=request.wire_payload_digest,
        now=clock.value,
    ).claimed is True
    path = tmp_path / 'reconcile.sqlite3'
    sidecar.close()
    reopened = GazeboMonitorRoomNav2ReconcileStore(
        path,
        core_store_namespace=core.store_namespace,
        quiescence_dwell_seconds=2.0,
        boot_id_reader=_boot,
    )
    fresh_transport = _FakeTransport()
    restarted = GazeboMonitorRoomNav2ReconcileSupervisor(
        core,
        reopened,
        fresh_transport,
        worker_id='reconcile-worker',
        lease_seconds=20.0,
        clock=clock,
    )
    observed = restarted.cancel_once(
        'operation-1', cancel_request_id='cancel-reconcile-1'
    )
    assert observed.state == 'blocked_unresolved'
    assert fresh_transport.cancel_calls == []
    assert fresh_transport.send_calls == 0


def test_cancel_report_after_lease_expiry_is_discarded(tmp_path):
    """Cancel terminality crossing its claim expiry cannot advance state."""
    world = _world(tmp_path)
    _core, sidecar, transport, clock, supervisor = world
    supervisor._lease_seconds = 2.0
    transport.cancel = 'canceled'
    transport.cancel_hook = lambda _request: setattr(clock, 'value', 13.0)
    observed = supervisor.cancel_once(
        'operation-1', cancel_request_id='cancel-reconcile-1'
    )
    assert observed.state == 'blocked_unresolved'
    assert observed.terminal_status is None
    assert len(sidecar.attempts('operation-1')) == 1


def test_known_terminal_goal_is_never_canceled(tmp_path):
    """A succeeded, aborted, or canceled goal gets no later cancel call."""
    world = _world(tmp_path)
    _core, _sidecar, transport, _clock, supervisor = world
    _terminal(world, status='aborted')
    observed = supervisor.cancel_once(
        'operation-1', cancel_request_id='cancel-reconcile-1'
    )
    assert observed.state == 'blocked_terminal_observed'
    assert transport.cancel_calls == []


def test_cancel_mutation_or_mismatch_cannot_promote_canceled(tmp_path):
    """A transport cannot mutate exact cancel authority and echo success."""
    world = _world(tmp_path)
    _core, _sidecar, transport, _clock, supervisor = world

    def mutate(request):
        object.__setattr__(request, 'goal_uuid', 'f' * 32)

    transport.cancel_hook = mutate
    transport.cancel = 'canceled'
    observed = supervisor.cancel_once(
        'operation-1', cancel_request_id='cancel-reconcile-1'
    )
    assert observed.state == 'blocked_unresolved'
    assert observed.terminal_status is None


def test_terminal_before_dwell_does_not_call_quiescence_validator(tmp_path):
    """The trusted quiet seam is not called before the durable dwell edge."""
    validator = _QuiescenceValidator()
    world = _world(tmp_path, validator=validator)
    _core, sidecar, _transport, clock, supervisor = world
    _terminal(world)
    clock.value = 12.9
    observed = supervisor.establish_quiescence_once(
        'operation-1', attempt_id='quiet-1'
    )
    assert observed.state == 'blocked_terminal_observed'
    assert validator.calls == []
    assert all(
        attempt.kind != 'quiescence'
        for attempt in sidecar.attempts('operation-1')
    )


def test_default_quiescence_authority_fails_closed(tmp_path):
    """No trusted validator means terminal evidence remains blocked."""
    world = _world(tmp_path)
    _core, _sidecar, _transport, clock, supervisor = world
    _terminal(world)
    clock.value = 13.0
    observed = supervisor.establish_quiescence_once(
        'operation-1', attempt_id='quiet-1'
    )
    assert observed.state == 'blocked_terminal_observed'
    assert observed.full_drop_certificate_digest is None


def test_terminal_plus_dwell_plus_trusted_quiet_mints_full_drop(tmp_path):
    """All three safety conditions are required for sidecar release."""
    validator = _QuiescenceValidator()
    world = _world(tmp_path, validator=validator)
    core, _sidecar, transport, clock, supervisor = world
    terminal = _terminal(world, status='succeeded')
    assert terminal.robot_blocked is True
    clock.value = 13.0
    released = supervisor.establish_quiescence_once(
        'operation-1', attempt_id='quiet-1'
    )
    assert len(validator.calls) == 1
    assert released.state == 'released_quiescent'
    assert released.safe_block_released is True
    assert released.full_drop_certificate_digest is not None
    assert released.operation_success is False
    assert released.core_admission_released is False
    assert core.observe('operation-1').robot_blocked is True
    assert transport.send_calls == 0


@pytest.mark.parametrize('failure', ['not_quiet', 'exception', 'mutation'])
def test_untrusted_or_malformed_quiescence_never_releases(
    tmp_path, failure
):
    """Negative, exceptional, or mutated quiet evidence fails closed."""
    validator = _QuiescenceValidator()
    if failure == 'not_quiet':
        validator.outcome = 'not_quiescent'
    elif failure == 'exception':
        validator.error = RuntimeError('private quiet marker')
    else:
        validator.hook = lambda request: object.__setattr__(
            request, 'goal_uuid', 'f' * 32
        )
    world = _world(tmp_path, validator=validator)
    _core, _sidecar, _transport, clock, supervisor = world
    _terminal(world)
    clock.value = 13.0
    observed = supervisor.establish_quiescence_once(
        'operation-1', attempt_id='quiet-1'
    )
    assert observed.state == 'blocked_terminal_observed'
    assert observed.full_drop_certificate_digest is None


def test_quiescence_result_frozen_bypass_fails_closed(tmp_path):
    """A mutated validator result cannot mint a full-drop certificate."""
    validator = _QuiescenceValidator()
    validator.mutate_result = lambda result: object.__setattr__(
        result, 'checked_at', 99.0
    )
    world = _world(tmp_path, validator=validator)
    _core, _sidecar, _transport, clock, supervisor = world
    _terminal(world)
    clock.value = 13.0
    observed = supervisor.establish_quiescence_once(
        'operation-1', attempt_id='quiet-1'
    )
    assert observed.state == 'blocked_terminal_observed'


def test_quiescence_crossing_lease_expiry_cannot_release(tmp_path):
    """A quiet proof outside its persisted lease edge is discarded."""
    validator = _QuiescenceValidator()
    world = _world(tmp_path, dwell=0.0, validator=validator)
    _core, _sidecar, _transport, clock, supervisor = world
    _terminal(world)
    supervisor._lease_seconds = 2.0
    validator.hook = lambda _request: setattr(clock, 'value', 13.0)
    clock.value = 11.0
    observed = supervisor.establish_quiescence_once(
        'operation-1', attempt_id='quiet-1'
    )
    assert observed.state == 'blocked_terminal_observed'
    assert observed.full_drop_certificate_digest is None


def test_restart_with_new_attempt_observes_but_never_resends_goal(tmp_path):
    """Process restart can keep observing the stable UUID without a start API."""
    world = _world(tmp_path)
    core, sidecar, transport, clock, supervisor = world
    supervisor.reconcile_once('operation-1', attempt_id='reconcile-1')
    path = tmp_path / 'reconcile.sqlite3'
    sidecar.close()
    reopened = GazeboMonitorRoomNav2ReconcileStore(
        path,
        core_store_namespace=core.store_namespace,
        quiescence_dwell_seconds=2.0,
        boot_id_reader=_boot,
    )
    new_transport = _FakeTransport()
    new_transport.result = 'canceled'
    clock.value = 32.0
    restarted = GazeboMonitorRoomNav2ReconcileSupervisor(
        core,
        reopened,
        new_transport,
        worker_id='replacement-worker',
        lease_seconds=20.0,
        clock=clock,
    )
    observed = restarted.reconcile_once(
        'operation-1', attempt_id='reconcile-2'
    )
    assert observed.terminal_status == 'canceled'
    assert len(new_transport.status_calls) == 1
    assert len(new_transport.result_calls) == 1
    assert new_transport.send_calls == 0
