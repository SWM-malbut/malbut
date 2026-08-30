"""Contracts for the bounded concurrent-approval resolver gate."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import time

import pytest

from malbut_agent_server.named_target import BoundNamedTarget
from malbut_scenarios.concurrent_approval_resolver import (
    CONCURRENT_APPROVAL_OBSERVATION_FILENAME,
    ConcurrentApprovalGateError,
    ConcurrentApprovalGateObservation,
    ConcurrentApprovalGateSnapshot,
    ConcurrentApprovalResolverGate,
    concurrent_approval_observation_path,
    read_concurrent_approval_observation,
)


class _RecordingResolver:
    """Record test-only locations while returning one valid binding."""

    def __init__(self) -> None:
        self.calls = []
        self.target = BoundNamedTarget(
            room_name='private-room-name',
            room_category='private-category',
            binding_digest=hashlib.sha256(b'private-binding').hexdigest(),
        )

    def resolve(self, location: str) -> BoundNamedTarget:
        """Return the same object so delegation identity is observable."""
        self.calls.append(location)
        return self.target


def _wait_for_contenders(
    gate: ConcurrentApprovalResolverGate,
    expected: int,
    *,
    timeout_seconds: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if gate.snapshot().contender_count == expected:
            return
        time.sleep(0.001)
    raise AssertionError('bounded contender count was not reached')


def test_first_resolve_passes_then_exactly_two_resolves_rendezvous(
    tmp_path,
) -> None:
    """Hold the approval pair without delaying proposal or later calls."""
    delegate = _RecordingResolver()
    observation_path = tmp_path / (
        CONCURRENT_APPROVAL_OBSERVATION_FILENAME
    )
    gate = ConcurrentApprovalResolverGate(
        delegate,
        timeout_seconds=1.0,
        observation_path=observation_path,
    )

    proposal = gate.resolve('private-proposal-location')
    assert proposal is delegate.target
    assert delegate.calls == ['private-proposal-location']

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(gate.resolve, 'private-approval-location-a')
        _wait_for_contenders(gate, 1)
        assert not first.done()
        assert delegate.calls == ['private-proposal-location']

        second = executor.submit(
            gate.resolve,
            'private-approval-location-b',
        )
        assert first.result(timeout=1.0) is delegate.target
        assert second.result(timeout=1.0) is delegate.target

    assert gate.snapshot() == ConcurrentApprovalGateSnapshot(
        contender_count=2,
        release_count=2,
    )
    assert set(delegate.calls[1:]) == {
        'private-approval-location-a',
        'private-approval-location-b',
    }

    later = gate.resolve('private-worker-location')
    assert later is delegate.target
    assert delegate.calls[-1] == 'private-worker-location'
    assert gate.snapshot() == ConcurrentApprovalGateSnapshot(2, 2)
    assert read_concurrent_approval_observation(
        observation_path
    ) == ConcurrentApprovalGateObservation()


def test_observation_is_strict_private_and_cannot_be_reused(
    tmp_path,
) -> None:
    """Reject missing, extended, non-canonical, and stale proof files."""
    database = tmp_path / 'agent.sqlite3'
    path = concurrent_approval_observation_path(str(database))
    path.write_text(
        '{"contender_count":2,"fault_profile":"concurrent_approval",'
        '"release_count":2,"private_id":"secret"}',
        encoding='utf-8',
    )
    path.chmod(0o600)

    with pytest.raises(ConcurrentApprovalGateError):
        read_concurrent_approval_observation(path)
    path.write_text(
        '{"contender_count":2,"fault_profile":"concurrent_approval",'
        '"release_count":2,"release_count":2}',
        encoding='utf-8',
    )
    path.chmod(0o600)
    with pytest.raises(ConcurrentApprovalGateError):
        read_concurrent_approval_observation(path)
    with pytest.raises(ConcurrentApprovalGateError):
        ConcurrentApprovalResolverGate(
            _RecordingResolver(),
            observation_path=path,
        )


def test_broken_rendezvous_fails_closed_without_late_delegation() -> None:
    """Never resolve the timed-out contender or any later target."""
    delegate = _RecordingResolver()
    gate = ConcurrentApprovalResolverGate(
        delegate,
        timeout_seconds=0.02,
    )
    assert gate.resolve('private-proposal-location') is delegate.target

    with pytest.raises(ConcurrentApprovalGateError):
        gate.resolve('private-unpaired-approval')

    assert gate.snapshot() == ConcurrentApprovalGateSnapshot(1, 0)
    assert delegate.calls == ['private-proposal-location']
    with pytest.raises(ConcurrentApprovalGateError):
        gate.resolve('private-late-approval')
    assert delegate.calls == ['private-proposal-location']


def test_snapshot_and_repr_expose_counts_only() -> None:
    """Keep locations, target details, and delegate state out of output."""
    delegate = _RecordingResolver()
    gate = ConcurrentApprovalResolverGate(delegate)

    assert gate.snapshot() == ConcurrentApprovalGateSnapshot(0, 0)
    rendered = repr(gate)
    assert rendered == (
        'ConcurrentApprovalResolverGate('
        'contender_count=0, release_count=0)'
    )
    for secret in (
        'private-room-name',
        'private-category',
        'private-binding',
        'private-proposal-location',
    ):
        assert secret not in rendered


@pytest.mark.parametrize(
    'timeout_seconds',
    [True, 0, -1, float('nan'), float('inf'), 30.1, '1'],
)
def test_rejects_invalid_timeout(timeout_seconds) -> None:
    """Accept only a finite, bounded numeric wait."""
    with pytest.raises(ValueError):
        ConcurrentApprovalResolverGate(
            _RecordingResolver(),
            timeout_seconds=timeout_seconds,
        )


def test_rejects_delegate_without_resolve() -> None:
    """Fail construction before a malformed adapter reaches a request."""
    with pytest.raises(TypeError):
        ConcurrentApprovalResolverGate(object())
