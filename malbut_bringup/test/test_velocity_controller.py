"""Exercise command expiration without ROS or a physical motor controller."""

import importlib.util
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


class Twist:
    """Provide the velocity fields consumed by the vendor controller."""

    def __init__(self):
        self.linear = SimpleNamespace(x=0.0, y=0.0, z=0.0)
        self.angular = SimpleNamespace(x=0.0, y=0.0, z=0.0)


@pytest.fixture
def rig(monkeypatch):
    state = SimpleNamespace(now=10.0, timeout=0.5, commands=[], warnings=[],
                            preprocessing=[], active=False, events=[], fail=False)

    def init():
        assert not state.active, 'vendor owns rclpy.init'
        state.active = True
        state.events.append('init')

    def shutdown():
        state.events.append('shutdown')
        state.active = False

    ros = SimpleNamespace(init=init, shutdown=shutdown, ok=lambda: state.active, spin=Mock())

    class Controller:
        def __init__(self, name):
            ros.init()
            state.node = self
            self.name = name
            self.linear_x = self.linear_y = self.angular_z = 0.0
            self.inputs = {
                '/controller/cmd_vel': self.cmd_vel_callback,
                '/cmd_vel': self.app_cmd_vel_callback,
                '/app/cmd_vel': self.acker_cmd_vel_callback,
            }

        def declare_parameter(self, name, default, descriptor):
            state.parameter = (name, default)
            state.parameter_descriptor = descriptor
            return SimpleNamespace(value=state.timeout)

        def create_timer(self, period, callback, *, clock):
            state.timer = SimpleNamespace(period=period, callback=callback, clock=clock)

        def get_logger(self):
            return SimpleNamespace(warning=state.warnings.append)

        def cmd_vel_callback(self, message):
            if state.fail:
                raise RuntimeError('motor publication failed')
            self.linear_x = message.linear.x
            self.linear_y = message.linear.y
            self.angular_z = message.angular.z
            state.commands.append((self.linear_x, self.linear_y, self.angular_z))
            state.events.append('motor')

        def app_cmd_vel_callback(self, message):
            state.preprocessing.append('app')
            message.linear.x = max(-0.2, min(0.2, message.linear.x))
            self.cmd_vel_callback(message)

        def acker_cmd_vel_callback(self, message):
            state.preprocessing.append('acker')
            if message.linear.x and message.angular.z:
                message.angular.z = message.linear.x * math.tan(message.angular.z) / 0.17706
            self.cmd_vel_callback(message)

        def shutdown(self, signum, frame):
            ros.shutdown()

        def destroy_node(self):
            state.events.append('destroy')

    class ExternalShutdownException(Exception):
        pass

    modules = {
        'controller': SimpleNamespace(),
        'controller.odom_publisher_node': SimpleNamespace(Controller=Controller),
        'geometry_msgs': SimpleNamespace(),
        'geometry_msgs.msg': SimpleNamespace(Twist=Twist),
        'rcl_interfaces': SimpleNamespace(),
        'rcl_interfaces.msg': SimpleNamespace(ParameterDescriptor=SimpleNamespace),
        'rclpy': ros,
        'rclpy.clock': SimpleNamespace(
            Clock=lambda **kwargs: SimpleNamespace(**kwargs),
            ClockType=SimpleNamespace(STEADY_TIME='steady')),
        'rclpy.executors': SimpleNamespace(ExternalShutdownException=ExternalShutdownException),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    source = Path(__file__).parents[1] / 'malbut_bringup' / 'velocity_controller.py'
    spec = importlib.util.spec_from_file_location('velocity_controller_under_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: state.now))
    state.module = module
    state.ros = ros
    return state


def motion(x=0.03, y=0.0, angular=0.0):
    """Create one independent velocity input."""
    message = Twist()
    message.linear.x = x
    message.linear.y = y
    message.angular.z = angular
    return message


def test_fresh_command_expires_at_boundary_and_resets_odom_once(rig):
    node = rig.module.VelocityController()
    node.inputs['/controller/cmd_vel'](motion(y=0.02, angular=0.1))
    rig.now = 10.499
    rig.timer.callback()
    assert rig.commands == [(0.03, 0.02, 0.1)]
    rig.now = 10.5
    rig.timer.callback()
    assert rig.commands[-1] == (0.0, 0.0, 0.0)
    assert (node.linear_x, node.linear_y, node.angular_z) == (0.0, 0.0, 0.0)
    rig.now = 20.0
    rig.timer.callback()
    assert len(rig.commands) == 2
    assert len(rig.warnings) == 1


def test_refresh_extends_deadline_and_new_input_recovers_after_timeout(rig):
    node = rig.module.VelocityController()
    node.cmd_vel_callback(motion())
    rig.now = 10.4
    node.cmd_vel_callback(motion(x=-0.03))
    rig.now = 10.6
    rig.timer.callback()
    assert rig.commands[-1] == (-0.03, 0.0, 0.0)
    rig.now = 11.0
    rig.timer.callback()
    assert rig.commands[-1] == (0.0, 0.0, 0.0)
    node.cmd_vel_callback(motion())
    assert rig.commands[-1] == (0.03, 0.0, 0.0)
    rig.now = 11.5
    rig.timer.callback()
    assert rig.commands[-1] == (0.0, 0.0, 0.0)
    assert len(rig.warnings) == 2


def test_idle_and_explicit_zero_do_not_generate_timeout_stops(rig):
    node = rig.module.VelocityController()
    rig.timer.callback()
    assert not rig.commands
    node.cmd_vel_callback(motion())
    node.cmd_vel_callback(Twist())
    rig.now = 20.0
    rig.timer.callback()
    assert len(rig.commands) == 2
    assert not rig.warnings


@pytest.mark.parametrize('topic,preprocessor', [('/cmd_vel', 'app'), ('/app/cmd_vel', 'acker')])
def test_app_inputs_preserve_preprocessing_and_share_the_watchdog(rig, topic, preprocessor):
    node = rig.module.VelocityController()
    node.inputs[topic](motion(x=0.3, angular=0.1))
    assert rig.preprocessing == [preprocessor]
    if preprocessor == 'app':
        assert rig.commands[-1] == (0.2, 0.0, 0.1)
    else:
        assert rig.commands[-1] == pytest.approx((0.3, 0.0, 0.3 * math.tan(0.1) / 0.17706))
    rig.now = 10.5
    rig.timer.callback()
    assert rig.commands[-1] == (0.0, 0.0, 0.0)


@pytest.mark.parametrize('topic', ['/controller/cmd_vel', '/cmd_vel', '/app/cmd_vel'])
@pytest.mark.parametrize('axis', ['x', 'y', 'angular'])
@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf])
def test_nonfinite_input_stops_before_vendor_preprocessing(rig, topic, axis, value):
    node = rig.module.VelocityController()
    node.cmd_vel_callback(motion())
    node.inputs[topic](motion(**{axis: value}))
    assert rig.commands[-1] == (0.0, 0.0, 0.0)
    assert not rig.preprocessing
    assert node.last_cmd_vel is None


def test_failed_vendor_command_does_not_refresh_the_deadline(rig):
    node = rig.module.VelocityController()
    node.cmd_vel_callback(motion())
    rig.now = 10.4
    rig.fail = True
    with pytest.raises(RuntimeError, match='motor publication failed'):
        node.cmd_vel_callback(motion())
    assert node.last_cmd_vel == 10.0
    rig.fail = False
    rig.now = 10.5
    rig.timer.callback()
    assert rig.commands[-1] == (0.0, 0.0, 0.0)


@pytest.mark.parametrize('timeout', [0.0, -0.5, math.nan, math.inf, -math.inf])
def test_timeout_must_be_positive_and_finite(rig, timeout):
    rig.timeout = timeout
    with pytest.raises(ValueError, match='positive and finite'):
        rig.module.VelocityController()


def test_timer_uses_steady_clock_and_configured_timeout(rig):
    rig.timeout = 0.25
    node = rig.module.VelocityController()
    assert rig.parameter == ('cmd_vel_timeout', 0.5)
    assert rig.parameter_descriptor.read_only
    assert rig.timer.clock.clock_type == 'steady'
    assert rig.timer.period == 0.02
    node.cmd_vel_callback(motion())
    rig.now = 10.25
    rig.timer.callback()
    assert rig.commands[-1] == (0.0, 0.0, 0.0)


def test_shutdown_sends_zero_before_closing_ros(rig):
    node = rig.module.VelocityController()
    node.cmd_vel_callback(motion())
    node.shutdown(2, None)
    assert rig.events[-2:] == ['motor', 'shutdown']
    assert rig.commands[-1] == (0.0, 0.0, 0.0)


def test_main_initializes_once_and_stops_before_cleanup(rig):
    rig.module.main()
    rig.ros.spin.assert_called_once_with(rig.node)
    assert rig.events == ['init', 'motor', 'destroy', 'shutdown']


def test_failed_shutdown_zero_still_releases_ros(rig):
    rig.fail = True
    with pytest.raises(RuntimeError, match='motor publication failed'):
        rig.module.main()
    assert rig.events == ['init', 'destroy', 'shutdown']
