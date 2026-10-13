"""The voice move lock follows the fall check like the app's move buttons."""

import json

import pytest

from malbut_agent_server.fall_check_watch import (
    FALL_CHECK_RESPONSE, FALL_CHECK_STATUS_TIMEOUT_S, FallCheckWatch, asks_for_help,
    asks_to_stop,
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


@pytest.mark.parametrize('phase,inside', [
    ('approach', True), ('look', True), ('ask', True), ('wait', True),
    ('return', False), (None, False),
])
def test_the_window_runs_from_the_suspicion_to_the_end_of_the_question(phase, inside):
    """2026-10-13: the fall check comes first until its question ends, not on the way back."""
    clock = Clock()
    watch = FallCheckWatch(clock)
    watch.observe_status(json.dumps({'active': True, 'phase': phase}))
    assert watch.in_window() is inside
    assert watch.active()  # Moves stay locked for the whole check, return included.
    clock.now += FALL_CHECK_STATUS_TIMEOUT_S + 0.1
    assert not watch.in_window()
    watch.observe_status(json.dumps({'active': False, 'phase': None}))
    assert not watch.in_window()


@pytest.mark.parametrize('text', ['도와줘', '살려 주세요', '너무 아파요', '못 일어나겠어',
                                  '119 불러줘', '구급차 좀', '다쳤어'])
def test_spoken_help_requests_are_recognised(text):
    assert asks_for_help(text)


@pytest.mark.parametrize('text', ['괜찮아', '불 꺼줘', '아파트 이름이 뭐야', '오늘 날씨 어때', ''])
def test_ordinary_talk_is_not_a_help_request(text):
    assert not asks_for_help(text)


@pytest.mark.parametrize('text', ['그만', '멈춰', '그만해', '멈춰 줘', '취소해 줘', '정지', '서'])
def test_spoken_stops_still_cancel_during_the_check(text):
    assert asks_to_stop(text)


@pytest.mark.parametrize('text', ['괜찮아', '오늘 날씨 어때', '서울 날씨', '도와줘'])
def test_other_talk_is_not_a_stop(text):
    assert not asks_to_stop(text)
