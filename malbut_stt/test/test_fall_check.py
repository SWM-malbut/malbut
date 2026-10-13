"""The guardian's talk waits while a fall check holds the speaker (2026-10-13)."""

import json

import pytest

from malbut_stt.fall_check import STATUS_TIMEOUT_S, FallCheckWindow


class Clock:
    def __init__(self):
        self.now = 50.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize('phase,held', [
    ('approach', True), ('look', True), ('ask', True), ('wait', True),
    ('return', False), (None, False),
])
def test_the_suspicion_until_the_question_ends_holds_the_speaker(phase, held):
    clock = Clock()
    window = FallCheckWindow(clock)
    assert not window.active()
    window.observe(json.dumps({'active': True, 'phase': phase}))
    assert window.active() is held
    clock.now += STATUS_TIMEOUT_S + 0.1  # The fall rules stopped reporting.
    assert not window.active()


def test_an_ended_check_or_a_malformed_status_does_not_hold_it():
    window = FallCheckWindow(Clock())
    window.observe(json.dumps({'active': True, 'phase': 'approach'}))
    window.observe(json.dumps({'active': False, 'phase': None}))
    assert not window.active()
    for bad in ('not json', '[]', json.dumps({'active': 'yes'}), 'x' * 5000, None):
        window.observe(bad)
    assert not window.active()
