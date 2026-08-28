"""Contracts for bounded named-navigation result polling."""

from types import SimpleNamespace

import pytest

from malbut_gazebo.named_navigation_cli import wait_for_terminal


class StatusFacade:
    """Yield controlled statuses without exposing a connector."""

    def __init__(self, states):
        """Store a finite state stream and call count."""
        self.states = iter(states)
        self.calls = 0

    def status(self, _execution):
        """Return the next typed-status-shaped value."""
        self.calls += 1
        state = next(self.states)
        return SimpleNamespace(state=state)


def test_wait_returns_first_known_terminal_without_extra_poll():
    """Stop immediately at one known terminal observation."""
    facade = StatusFacade(["driving", "succeeded", "failed"])
    clock = iter((0.0, 0.1))

    status = wait_for_terminal(
        facade,
        object(),
        timeout_s=10.0,
        poll_interval_s=0.05,
        monotonic=lambda: next(clock),
        sleep=lambda _seconds: None,
    )

    assert status.state == "succeeded"
    assert facade.calls == 2


def test_wait_timeout_observes_only_and_never_sends_or_cancels():
    """Let the caller own the one-shot timeout cancellation decision."""
    facade = StatusFacade(["driving", "driving"])
    clock = iter((0.0, 0.1, 1.1))

    with pytest.raises(TimeoutError):
        wait_for_terminal(
            facade,
            object(),
            timeout_s=1.0,
            poll_interval_s=0.05,
            monotonic=lambda: next(clock),
            sleep=lambda _seconds: None,
        )

    assert facade.calls == 2


@pytest.mark.parametrize(
    ("timeout", "interval"),
    ((0.0, 0.25), (601.0, 0.25), (10.0, 0.0), (10.0, 6.0)),
)
def test_wait_rejects_unbounded_poll_configuration(timeout, interval):
    """Reject invalid deadlines before any status I/O."""
    facade = StatusFacade(["succeeded"])

    with pytest.raises(ValueError):
        wait_for_terminal(
            facade,
            object(),
            timeout_s=timeout,
            poll_interval_s=interval,
        )

    assert facade.calls == 0
