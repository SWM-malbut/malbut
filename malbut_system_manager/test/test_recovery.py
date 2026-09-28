"""Exercise manual recovery ownership without launching robot hardware."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from launch import LaunchContext
from launch_ros.actions import Node
from rclpy.action import GoalResponse

from malbut_system_manager.recovery import ProcessRecord, RecoveryOwner


def _owner():
    owner = object.__new__(RecoveryOwner)
    owner.condition = threading.Condition(threading.RLock())
    owner.records, owner.by_action = [], {}
    owner.launch_context = LaunchContext()
    owner.launch_context.extend_globals({'malbut_startup_complete': True})
    owner.launch_context.launch_configurations['malbut_startup_stage'] = json.dumps([0, 'sensors'])
    owner.service = Mock()
    owner.busy = owner.stopping = False
    owner.localization, owner.pose = {}, None
    owner.current_probe = None
    return owner


def _event(action):
    return SimpleNamespace(action=action, cmd=['/bin/true'], cwd=None, env={},
                           process_name='example-1', returncode=1)


def test_only_owned_exits_are_eligible_and_live_processes_are_not_relaunched():
    """A graph lookup never causes a duplicate process."""
    owner = _owner()
    action = Node(package='example', executable='example')
    event = _event(action)
    owner.started(event, owner.launch_context)
    record = owner.records[0]
    goal = SimpleNamespace(is_cancel_requested=False)
    owner._launch(record, goal)
    owner.service.include_launch_description.assert_not_called()
    owner.exited(event, owner.launch_context)
    owner._launch(record, goal)
    assert record.started is False
    description = owner.service.include_launch_description.call_args.args[0]
    actions = description.entities[0].execute(owner.launch_context)
    assert len(actions) == 1
    assert record.returncode is None
    owner._launch(record, goal)
    assert owner.service.include_launch_description.call_count == 1


def test_completed_initializers_are_not_recovery_targets():
    """An intentional pre-readiness exit is not a stopped runtime node."""
    owner = _owner()
    owner.launch_context.extend_globals({'malbut_startup_complete': False})
    event = _event(Node(package='example', executable='one_shot'))
    event.returncode = 0
    owner.started(event, owner.launch_context)
    owner.exited(event, owner.launch_context)
    assert not owner.records[0].required


def test_recovery_rejects_before_startup_duplicate_and_arbitrary_requests():
    """Only an explicit singleton recovery is allowed after successful startup."""
    owner = _owner()
    request = SimpleNamespace(capability_id='recovery', arguments_yaml='{}')
    owner.launch_context.extend_globals({'malbut_startup_complete': False})
    assert owner._goal(request) == GoalResponse.REJECT
    owner.launch_context.extend_globals({'malbut_startup_complete': True})
    request.arguments_yaml = '{command: arbitrary}'
    assert owner._goal(request) == GoalResponse.REJECT
    request.arguments_yaml = '{}'
    assert owner._goal(request) == GoalResponse.ACCEPT
    assert owner._goal(request) == GoalResponse.REJECT


def test_probe_uses_original_timeout_and_does_not_reuse_old_exit(tmp_path):
    """Each pass must wait for the fresh probe, never the cached startup result."""
    params = tmp_path / 'probe.yaml'
    params.write_text('/**:\n  ros__parameters:\n    startup_timeout_s: 37.0\n')
    owner = _owner()
    record = ProcessRecord(object(), (0, 'sensors'),
                           dict(cmd=['probe', '--params-file', str(params)],
                                cwd=None, env={}, name='probe'), probe=True, returncode=0)
    assert owner._probe_timeout(record) == 37.0
    owner._launch(record, SimpleNamespace(is_cancel_requested=False))
    assert not record.started
    assert record.returncode == 0  # Not yet launched; must NOT count as a new success.


def test_cancel_before_dispatch_does_not_start_a_process():
    """Cancellation does not kill a live application or create another one."""
    owner = _owner()
    record = ProcessRecord(object(), (0, 'sensors'),
                           dict(cmd=['/bin/true'], cwd=None, env={}, name='node'), returncode=1)
    goal = SimpleNamespace(is_cancel_requested=False)
    owner._launch(record, goal)
    goal.is_cancel_requested = True
    description = owner.service.include_launch_description.call_args.args[0]
    assert description.entities[0].execute(owner.launch_context) == []


def test_restarting_container_reloads_components_once():
    """Restarting an empty container without its components is not recovery."""
    owner = _owner()
    followup = Mock(return_value=[])
    record = ProcessRecord(object(), (2, 'navigation'),
                           dict(cmd=['/bin/true'], cwd=None, env={}, name='container'),
                           returncode=-11, followup=followup)
    owner._launch(record, SimpleNamespace(is_cancel_requested=False))
    description = owner.service.include_launch_description.call_args.args[0]
    description.entities[0].execute(owner.launch_context)
    followup.assert_called_once_with({}, None)
