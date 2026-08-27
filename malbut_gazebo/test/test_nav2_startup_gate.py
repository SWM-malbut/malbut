"""Unit contracts for deterministic Nav2 lifecycle startup."""

import ast
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from malbut_gazebo.nav2_startup_gate import (
    LIFECYCLE_MANAGERS,
    LifecycleManagerTarget,
    RclpyLifecycleStartupPort,
    StartupGateError,
    _parse_arguments,
    _require_isolated_ros_context,
    start_nav2_managers,
)


MODULE_FILE = (
    Path(__file__).resolve().parents[1]
    / 'malbut_gazebo'
    / 'nav2_startup_gate.py'
)
STARTUP_ARGUMENTS = {
    'service_timeout_seconds': 12.0,
    'discovery_stability_seconds': 1.5,
    'quiet_period_seconds': 0.25,
    'response_timeout_seconds': 4.0,
}


class RecordingPort:
    """Controllable fake for the lifecycle-only outbound boundary."""

    def __init__(self):
        self.calls = []
        self.wait_error = None
        self.startup_results = {}
        self.startup_errors = {}
        self.active_errors = {}

    def await_stable_services(
        self,
        *,
        timeout_seconds,
        stability_seconds,
        quiet_period_seconds,
    ):
        self.calls.append((
            'await_stable_services',
            timeout_seconds,
            stability_seconds,
            quiet_period_seconds,
        ))
        if self.wait_error is not None:
            raise self.wait_error

    def startup(self, target, *, response_timeout_seconds):
        self.calls.append((
            'startup',
            target.label,
            response_timeout_seconds,
        ))
        error = self.startup_errors.get(target.label)
        if error is not None:
            raise error
        return self.startup_results.get(target.label, True)

    def confirm_active(self, target, *, response_timeout_seconds):
        self.calls.append((
            'confirm_active',
            target.label,
            response_timeout_seconds,
        ))
        error = self.active_errors.get(target.label)
        if error is not None:
            raise error


class FakeFuture:
    """Minimal rclpy future controlled entirely by a unit test."""

    def __init__(self, *, done=True, response=None):
        self._done = done
        self._response = response
        self.cancelled = False

    def done(self):
        return self._done

    def exception(self):
        return None

    def result(self):
        return self._response

    def cancel(self):
        self.cancelled = True


class FakeClient:
    """Record a single service boundary without creating DDS resources."""

    def __init__(self, future=None):
        self.future = future or FakeFuture()
        self.requests = []
        self.removed = []

    def service_is_ready(self):
        return True

    def call_async(self, request):
        self.requests.append(request)
        return self.future

    def remove_pending_request(self, future):
        self.removed.append(future)


class FakeNode:
    """Capture client construction without initializing rclpy."""

    def __init__(self):
        self.created = []

    def create_client(self, service_type, service_name):
        client = FakeClient()
        self.created.append((service_type, service_name, client))
        return client


def _run(port):
    return start_nav2_managers(port, **STARTUP_ARGUMENTS)


def test_lifecycle_targets_pin_scope_and_safe_startup_order():
    assert LIFECYCLE_MANAGERS == (
        LifecycleManagerTarget(
            label='localization',
            service_name='/lifecycle_manager_localization/manage_nodes',
            managed_nodes=('map_server', 'amcl'),
        ),
        LifecycleManagerTarget(
            label='collision',
            service_name='/collision_lifecycle_manager/manage_nodes',
            managed_nodes=('collision_monitor',),
        ),
        LifecycleManagerTarget(
            label='navigation',
            service_name='/lifecycle_manager_navigation/manage_nodes',
            managed_nodes=(
                'controller_server',
                'smoother_server',
                'planner_server',
                'behavior_server',
                'bt_navigator',
                'waypoint_follower',
                'velocity_smoother',
            ),
        ),
    )


def test_startup_waits_once_then_starts_and_confirms_each_target_once():
    port = RecordingPort()

    _run(port)

    assert port.calls == [
        ('await_stable_services', 12.0, 1.5, 0.25),
        ('startup', 'localization', 4.0),
        ('confirm_active', 'localization', 4.0),
        ('startup', 'collision', 4.0),
        ('confirm_active', 'collision', 4.0),
        ('startup', 'navigation', 4.0),
        ('confirm_active', 'navigation', 4.0),
    ]


def test_service_discovery_timeout_fails_without_start_or_retry():
    port = RecordingPort()
    port.wait_error = TimeoutError('services did not stabilize')

    with pytest.raises(StartupGateError):
        _run(port)

    assert port.calls == [
        ('await_stable_services', 12.0, 1.5, 0.25),
    ]


def test_rejected_startup_stops_at_exact_target_without_retry():
    port = RecordingPort()
    port.startup_results['collision'] = False

    with pytest.raises(StartupGateError) as caught:
        _run(port)

    assert 'collision' in str(caught.value)
    assert port.calls == [
        ('await_stable_services', 12.0, 1.5, 0.25),
        ('startup', 'localization', 4.0),
        ('confirm_active', 'localization', 4.0),
        ('startup', 'collision', 4.0),
    ]


@pytest.mark.parametrize('error', [
    TimeoutError('active response timed out'),
    RuntimeError('manager response failed'),
])
def test_active_confirmation_error_stops_without_retry(error):
    port = RecordingPort()
    port.active_errors['collision'] = error

    with pytest.raises(StartupGateError) as caught:
        _run(port)

    assert 'collision' in str(caught.value)
    assert port.calls == [
        ('await_stable_services', 12.0, 1.5, 0.25),
        ('startup', 'localization', 4.0),
        ('confirm_active', 'localization', 4.0),
        ('startup', 'collision', 4.0),
        ('confirm_active', 'collision', 4.0),
    ]


def test_startup_exception_is_wrapped_and_never_retried():
    port = RecordingPort()
    port.startup_errors['localization'] = TimeoutError('response timed out')

    with pytest.raises(StartupGateError) as caught:
        _run(port)

    assert 'localization' in str(caught.value)
    assert port.calls == [
        ('await_stable_services', 12.0, 1.5, 0.25),
        ('startup', 'localization', 4.0),
    ]


def test_ros_port_creates_only_fixed_manager_and_state_clients():
    from lifecycle_msgs.srv import GetState
    from nav2_msgs.srv import ManageLifecycleNodes

    node = FakeNode()

    RclpyLifecycleStartupPort(node)

    expected = [
        (ManageLifecycleNodes, target.service_name)
        for target in LIFECYCLE_MANAGERS
    ]
    expected.extend(
        (GetState, f'/{node_name}/get_state')
        for target in LIFECYCLE_MANAGERS
        for node_name in target.managed_nodes
    )
    assert [
        (service_type, service_name)
        for service_type, service_name, _client in node.created
    ] == expected
    assert len(node.created) == 13


def test_unknown_response_is_removed_canceled_and_never_retried(
    monkeypatch,
):
    import rclpy

    monkeypatch.setattr(
        rclpy,
        'spin_until_future_complete',
        lambda *_args, **_kwargs: None,
    )
    future = FakeFuture(done=False)
    client = FakeClient(future)
    port = object.__new__(RclpyLifecycleStartupPort)
    port._node = object()

    with pytest.raises(StartupGateError) as caught:
        port._call_once(
            client,
            object(),
            response_timeout_seconds=0.5,
            error_prefix='localization_startup',
        )

    assert caught.value.code == (
        'localization_startup_response_unknown'
    )
    assert len(client.requests) == 1
    assert client.removed == [future]
    assert future.cancelled is True


def test_non_active_managed_node_fails_closed_at_exact_node(monkeypatch):
    import rclpy
    from lifecycle_msgs.msg import State
    from lifecycle_msgs.srv import GetState

    monkeypatch.setattr(
        rclpy,
        'spin_until_future_complete',
        lambda *_args, **_kwargs: None,
    )
    active = SimpleNamespace(
        current_state=SimpleNamespace(
            id=State.PRIMARY_STATE_ACTIVE,
        )
    )
    inactive = SimpleNamespace(
        current_state=SimpleNamespace(
            id=State.PRIMARY_STATE_INACTIVE,
        )
    )
    clients = {
        'map_server': FakeClient(FakeFuture(response=active)),
        'amcl': FakeClient(FakeFuture(response=inactive)),
    }
    port = object.__new__(RclpyLifecycleStartupPort)
    port._node = object()
    port._state_type = GetState
    port._state_clients = clients

    with pytest.raises(StartupGateError) as caught:
        port.confirm_active(
            LIFECYCLE_MANAGERS[0],
            response_timeout_seconds=0.5,
        )

    assert caught.value.code == 'amcl_state_not_active'
    assert len(clients['map_server'].requests) == 1
    assert len(clients['amcl'].requests) == 1


def test_isolated_ros_context_accepts_local_non_default_domain():
    assert _require_isolated_ros_context({
        'ROS_DOMAIN_ID': '29',
        'ROS_LOCALHOST_ONLY': '1',
    }) == 29


@pytest.mark.parametrize(
    'domain_id',
    ['', '0', '101', '233', 'not-an-integer'],
)
def test_isolated_ros_context_rejects_shared_or_invalid_domain(domain_id):
    with pytest.raises(StartupGateError) as caught:
        _require_isolated_ros_context({
            'ROS_DOMAIN_ID': domain_id,
            'ROS_LOCALHOST_ONLY': '1',
        })

    assert caught.value.code == 'isolated_ros_domain_required'


@pytest.mark.parametrize('localhost_only', ['', '0', 'true'])
def test_isolated_ros_context_requires_localhost_only(localhost_only):
    with pytest.raises(StartupGateError) as caught:
        _require_isolated_ros_context({
            'ROS_DOMAIN_ID': '29',
            'ROS_LOCALHOST_ONLY': localhost_only,
        })

    assert caught.value.code == 'localhost_only_required'


def test_cli_accepts_only_finite_bounded_timing_values():
    parsed = _parse_arguments([
        '--service-timeout-seconds', '300',
        '--discovery-stability-seconds', '0.01',
        '--quiet-period-seconds', '0',
        '--response-timeout-seconds', '300',
    ])
    assert parsed.service_timeout_seconds == 300.0
    assert parsed.discovery_stability_seconds == 0.01
    assert parsed.quiet_period_seconds == 0.0
    assert parsed.response_timeout_seconds == 300.0

    defaults = _parse_arguments([])
    assert 0.0 < defaults.service_timeout_seconds <= 300.0
    assert 0.0 < defaults.discovery_stability_seconds <= 300.0
    assert 0.0 <= defaults.quiet_period_seconds <= 300.0
    assert 0.0 < defaults.response_timeout_seconds <= 300.0
    for value in vars(defaults).values():
        assert math.isfinite(value)


@pytest.mark.parametrize(
    ('flag', 'value'),
    [
        ('--service-timeout-seconds', '0'),
        ('--service-timeout-seconds', '-1'),
        ('--service-timeout-seconds', '301'),
        ('--discovery-stability-seconds', '0'),
        ('--discovery-stability-seconds', '301'),
        ('--quiet-period-seconds', '-0.01'),
        ('--quiet-period-seconds', '301'),
        ('--response-timeout-seconds', '0'),
        ('--response-timeout-seconds', '301'),
        ('--service-timeout-seconds', 'nan'),
        ('--discovery-stability-seconds', 'inf'),
        ('--quiet-period-seconds', '-inf'),
        ('--response-timeout-seconds', 'nan'),
    ],
)
def test_cli_rejects_out_of_range_or_non_finite_values(flag, value):
    with pytest.raises(SystemExit):
        _parse_arguments([flag, value])


def test_main_always_destroys_the_node_and_shuts_down_rclpy():
    tree = ast.parse(MODULE_FILE.read_text(encoding='utf-8'))
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'main'
    )
    finalizers = [
        node.finalbody
        for node in ast.walk(main)
        if isinstance(node, ast.Try) and node.finalbody
    ]

    assert finalizers
    assert any(
        {'destroy_node', 'shutdown'} <= {
            call.func.attr
            for statement in finalizer
            for call in ast.walk(statement)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
        }
        for finalizer in finalizers
    )


def test_gate_source_cannot_create_actions_or_depend_on_roaming():
    source = MODULE_FILE.read_text(encoding='utf-8')
    for forbidden in (
        'ActionClient',
        'NavigateToPose',
        'send_goal_async',
        'roaming',
        'start_roaming',
    ):
        assert forbidden not in source
