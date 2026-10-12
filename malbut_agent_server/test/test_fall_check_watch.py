"""The voice move lock follows the fall check like the app's move buttons."""

import json

from malbut_agent_server.fall_check_watch import (
    FALL_CHECK_RESPONSE, FALL_CHECK_STATUS_TIMEOUT_S, FallCheckWatch,
)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def status(active, **extra):
    return json.dumps({'active': active, 'phase': 'approach' if active else None, **extra})


def test_fresh_active_status_locks_and_goes_stale_after_three_seconds():
    clock = Clock()
    watch = FallCheckWatch(clock)
    assert not watch.active() and watch.move_refusal() is None
    watch.observe_status(status(True))
    assert watch.active() and watch.move_refusal() == FALL_CHECK_RESPONSE
    clock.now += FALL_CHECK_STATUS_TIMEOUT_S + 0.1  # The fall rules stopped reporting.
    assert not watch.active()
    watch.observe_status(status(False))
    assert not watch.active()


def test_a_fall_mission_in_manager_locks_without_the_status():
    watch = FallCheckWatch(Clock())
    watch.observe_missions(['patrol'])
    assert not watch.active()
    watch.observe_missions(iter(['patrol', 'fall_approach']))
    assert watch.active()
    watch.observe_missions(['fall_confirmation'])
    assert watch.active()
    watch.observe_missions([])
    assert not watch.active()


def test_malformed_status_is_ignored():
    watch = FallCheckWatch(Clock())
    watch.observe_status(status(True))
    for bad in ('x', '[]', json.dumps({'active': 'yes'}), json.dumps({}), None,
                json.dumps({'active': False, 'pad': 'a' * 5000})):
        watch.observe_status(bad)
    assert watch.active()
