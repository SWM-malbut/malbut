"""Cover lifecycle repair decisions without hardware or navigation goals."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import ChangeState, GetState
from nav2_msgs.srv import ManageLifecycleNodes
import pytest
from rcl_interfaces.srv import SetParametersAtomically
from rclpy.task import Future
from std_srvs.srv import Trigger

from malbut_system_manager.lifecycle_recovery import LifecycleRecovery, LifecycleUnavailable


def _fixture(values, active=False):
    repair = LifecycleRecovery(Mock())
    states, commands = dict(values), []
    manager_active = active

    def call(kind, name, request, *args, **kwargs):
        nonlocal manager_active
        node = name.split('/')[1]
        if kind is GetState:
            state = states[node]
            return SimpleNamespace(current_state=State(id=state, label={
                1: 'unconfigured', 2: 'inactive', 3: 'active'}[state]))
        if kind is Trigger:
            return SimpleNamespace(success=manager_active)
        commands.append((kind, name, request))
        if kind is SetParametersAtomically:
            return SimpleNamespace(result=SimpleNamespace(successful=True))
        if kind is ChangeState:
            assert states[node] == State.PRIMARY_STATE_UNCONFIGURED
            states[node] = State.PRIMARY_STATE_INACTIVE
        else:
            assert kind is ManageLifecycleNodes
            expected = (State.PRIMARY_STATE_UNCONFIGURED
                        if request.command == request.STARTUP else State.PRIMARY_STATE_INACTIVE)
            assert all(state == expected for state in states.values())
            states.update({key: State.PRIMARY_STATE_ACTIVE for key in states})
            manager_active = True
        return SimpleNamespace(success=True)

    repair._call = Mock(side_effect=call)
    spec = dict(manager='manager', nodes=tuple(states), parameters={
        'map_server': {'yaml_filename': '/maps/home.yaml'}})
    reports = []
    return repair, spec, reports, commands


@pytest.mark.parametrize('state,command', [(1, 'STARTUP'), (2, 'RESUME')])
def test_responsive_inactive_group_uses_official_manager(state, command):
    """Paused maps are preserved; cleaned-up maps get their path restored."""
    repair, spec, reports, commands = _fixture({'map_server': state, 'amcl': state})
    repair.recover(spec, time.monotonic() + 1, lambda: False, reports.append)
    assert reports[-1]['state'] == 'ACTIVE'
    assert commands[-1][0] is ManageLifecycleNodes
    assert commands[-1][2].command == getattr(ManageLifecycleNodes.Request, command)
    parameter_writes = [item for item in commands if item[0] is SetParametersAtomically]
    assert len(parameter_writes) == (1 if state == 1 else 0)


def test_partial_configuration_is_completed_before_manager_resume():
    """Humble STARTUP cannot configure an already inactive server."""
    repair, spec, reports, commands = _fixture({'map_server': 2, 'amcl': 1})
    repair.recover(spec, time.monotonic() + 1, lambda: False, reports.append)
    assert [item[0] for item in commands] == [ChangeState, ManageLifecycleNodes]
    assert commands[0][1] == '/amcl/change_state'
    assert commands[1][2].command == ManageLifecycleNodes.Request.RESUME


def test_healthy_group_is_read_only():
    """No reset, parameter write, transition or restart for an active group."""
    repair, spec, reports, commands = _fixture({'map_server': 3, 'amcl': 3}, active=True)
    repair.recover(spec, time.monotonic() + 1, lambda: False, reports.append)
    assert commands == []
    assert reports[-1]['state'] == 'PASS'


def test_mixed_active_group_is_not_blindly_reset():
    """Do not race an unfinished activation or disrupt a healthy server."""
    repair, spec, reports, commands = _fixture({'map_server': 3, 'amcl': 2})
    with pytest.raises(RuntimeError, match='mixed/transitioning'):
        repair.recover(spec, time.monotonic() + 1, lambda: False, reports.append)
    assert commands == []
    assert reports[-1]['before'] == {'map_server': 'active', 'amcl': 'inactive'}
    assert reports[-1]['state'] == 'FAILED'


def test_failed_manager_command_cannot_report_success():
    """Only fresh ACTIVE responses make a recovery successful."""
    repair, spec, reports, _ = _fixture({'amcl': 2})
    original = repair._call.side_effect

    def call(kind, *args, **kwargs):
        if kind is ManageLifecycleNodes:
            return SimpleNamespace(success=False)
        return original(kind, *args, **kwargs)

    repair._call.side_effect = call
    with pytest.raises(RuntimeError, match='RESUME failed'):
        repair.recover(spec, time.monotonic() + 1, lambda: False, reports.append)
    assert reports[-1]['state'] == 'FAILED'


def test_command_timeout_retains_pending_request_but_read_timeout_does_not():
    """A timed-out write may still execute; a discarded read has no side effects."""
    node = Mock()
    client = node.create_client.return_value
    client.service_is_ready.return_value = True
    repair = LifecycleRecovery(node)
    future = Future()
    client.call_async.return_value = future
    # Cancel just after dispatch, without any wall-clock waiting.
    with pytest.raises(RuntimeError, match='canceled'):
        repair._call(ManageLifecycleNodes, '/manager/manage_nodes',
                     ManageLifecycleNodes.Request(), time.monotonic() + 1,
                     lambda: client.call_async.called, command=True)
    assert repair.busy and not future.cancelled()
    future.set_result(ManageLifecycleNodes.Response(success=True))
    assert not repair.busy
    client.call_async.reset_mock()
    future = Future()
    client.call_async.return_value = future
    with pytest.raises(RuntimeError, match='canceled'):
        repair._call(GetState, '/amcl/get_state', GetState.Request(),
                     time.monotonic() + 1, lambda: client.call_async.called)
    client.remove_pending_request.assert_called_once_with(future)
    assert future.cancelled()


@pytest.mark.parametrize('reason', ['service_not_discovered', 'response_timeout'])
def test_unresponsive_is_distinct_from_inactive_and_never_activates(reason):
    """A live process with lost endpoints is reported, not restarted or activated."""
    repair, spec, reports, commands = _fixture({'map_server': 2})
    repair._call.side_effect = LifecycleUnavailable('/map_server/get_state', reason)
    with pytest.raises(LifecycleUnavailable, match=reason):
        repair.recover(spec, time.monotonic() + 1, lambda: False, reports.append)
    assert commands == []
    assert reports[-1]['state'] == 'UNRESPONSIVE'


def test_one_lost_read_response_is_retried_before_diagnosing_a_hang(monkeypatch):
    """A new read succeeds after response loss; no process restart is needed."""
    node = Mock()
    client = node.create_client.return_value
    client.service_is_ready.return_value = True
    first, second = Future(), Future()
    second.set_result(GetState.Response(current_state=State(id=3, label='active')))
    client.call_async.side_effect = [first, second]
    clock = [0.0]
    monkeypatch.setattr('malbut_system_manager.lifecycle_recovery.time.monotonic',
                        lambda: clock[0])
    monkeypatch.setattr('malbut_system_manager.lifecycle_recovery.time.sleep',
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    repair = LifecycleRecovery(node)
    repair.response_timeout_s = 0.1
    result = repair._call(GetState, '/server/get_state', GetState.Request(), 1, lambda: False)
    assert result.current_state.id == 3
    assert client.call_async.call_count == 2
    assert first.cancelled()
    assert not repair.busy
