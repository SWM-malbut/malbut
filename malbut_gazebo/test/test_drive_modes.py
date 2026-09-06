"""Tests for autonomous-mode transports and common web states."""

import pytest

from malbut_gazebo.drive_modes import (
    AUTONOMOUS_MODES,
    TRIGGER_DRIVE_MODES,
    common_mode_state,
)


def test_common_modes_separate_service_and_action_transports():
    assert AUTONOMOUS_MODES == {
        "patrol", "person_following", "roaming",
    }
    assert TRIGGER_DRIVE_MODES == {"roaming"}


@pytest.mark.parametrize(
    ("mode", "state", "expected"),
    [
        ("patrol", "planning", "active"),
        ("patrol", "navigating", "active"),
        ("patrol", "observing", "active"),
        ("patrol", "stopping", "stopping"),
        ("patrol", "aborted", "failed"),
        ("patrol", "completed", "idle"),
        ("roaming", "selecting", "active"),
        ("roaming", "paused", "paused"),
        ("roaming", "idle", "idle"),
    ],
)
def test_manager_states_have_one_web_contract(mode, state, expected):
    assert common_mode_state(mode, {"state": state}) == expected
