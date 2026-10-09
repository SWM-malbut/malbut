"""Unit tests for manual control activity without ROS communication."""

from types import SimpleNamespace

from malbut_system_manager.manual_control_node import ManualActivity


def _twist(x=0.0, y=0.0, z=0.0):
    return SimpleNamespace(linear=SimpleNamespace(x=x, y=y), angular=SimpleNamespace(z=z))


def test_released_input_ends_after_the_quiet_period():
    """Zero commands are not operation; the session ends five seconds later."""
    activity = ManualActivity(idle_timeout_s=5.0, joy_deadzone=0.1)
    assert activity.teleop(10.0, _twist(x=0.15))
    assert not activity.teleop(11.0, _twist())
    assert not activity.idle(14.9)
    assert activity.idle(15.0)


def test_held_stick_keeps_the_session_without_new_commands():
    """The vendor node publishes only on change; board Joy shows a held stick."""
    activity = ManualActivity(idle_timeout_s=5.0, joy_deadzone=0.1)
    activity.teleop(0.0, _twist(y=0.15))
    for now in (1.0, 4.0, 8.0):
        activity.joy(now, [0.6, 0.0, 0.0, 0.0])
    assert not activity.idle(12.9)
    activity.joy(13.0, [0.05, 0.0, 0.0, 0.9])
    assert activity.idle(13.0)


def test_new_session_gets_the_full_quiet_period():
    """A session started by the web or CLI is not ended immediately."""
    activity = ManualActivity(idle_timeout_s=5.0, joy_deadzone=0.1)
    activity.started(100.0)
    assert not activity.idle(104.0)
    assert activity.idle(105.0)
