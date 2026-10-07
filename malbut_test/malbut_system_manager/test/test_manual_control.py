"""Unit tests for manual control activity without ROS communication."""

from types import SimpleNamespace

from malbut_interfaces.msg import SystemState
from malbut_system_manager.manual_control_node import ManualActivity, ManualControl


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


def test_stop_requires_both_held_sources_to_return_to_neutral():
    """Repeating old teleop or a held joystick cannot undo an explicit stop."""
    activity = ManualActivity(idle_timeout_s=5.0, joy_deadzone=0.1)
    activity.joy(0.0, [0.5, 0.0, 0.0])
    assert activity.teleop(0.0, _twist(x=0.1))
    activity.stop()
    assert not activity.teleop(1.0, _twist(x=0.1))
    assert not activity.teleop(2.0, _twist())
    assert not activity.teleop(3.0, _twist(x=0.1))
    activity.joy(4.0, [0.0, 0.0, 0.0])
    assert activity.teleop(5.0, _twist(x=0.1))


def test_manual_input_waits_for_manager_epoch_and_binds_the_goal():
    """Manual input cannot submit an unbound or pre-stop generation of work."""
    sent = []

    def send(goal):
        sent.append(goal)
        return SimpleNamespace(add_done_callback=lambda _: None)

    manual = SimpleNamespace(
        activity=ManualActivity(5.0, 0.1), manual=False, movement_runtime_id='',
        movement_epoch=0, request_active=False, last_request=float('-inf'),
        retry_delay_s=1.0, capability_id='manual_drive', _accepted=lambda _: None,
        client=SimpleNamespace(server_is_ready=lambda: True, send_goal_async=send))
    ManualControl._teleop(manual, _twist(x=0.1))
    assert not sent
    ManualControl._state(manual, SystemState(movement_runtime_id='manager', movement_epoch=4))
    ManualControl._teleop(manual, _twist(x=0.1))
    assert len(sent) == 1
    assert sent[0].require_movement_epoch
    assert (sent[0].movement_runtime_id, sent[0].movement_epoch) == ('manager', 4)
    ManualControl._state(manual, SystemState(movement_runtime_id='manager', movement_epoch=5))
    assert not manual.activity.teleop(100.0, _twist(x=0.1))
