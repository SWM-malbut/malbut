"""Observe Action acceptance and cancellation independently of workflow sends."""

from concurrent.futures import Future
import sys
import json
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from malbut_agent_server.robot_device_client import RobotDeviceClient


class Action:
    def __init__(self, node, action_type, name):
        assert name == '/malbut/mission/execute'
        self.sent = []
        self.ready = True

    def server_is_ready(self):
        return self.ready

    def send_goal_async(self, goal):
        future = Future()
        self.sent.append((goal, future))
        return future

    def destroy(self):
        pass


class Handle:
    accepted = True

    def __init__(self):
        self.canceled = 0
        self.result = Future()

    def cancel_goal_async(self):
        self.canceled += 1
        return Future()

    def get_result_async(self):
        return self.result


class Node:
    def create_client(self, service, name):
        assert name == '/malbut/mission/stop_movement'
        self.request = None
        self.response = Future()
        return SimpleNamespace(service_is_ready=lambda: True, call_async=self.call)

    def call(self, request):
        self.request = request
        return self.response

    def create_timer(self, seconds, callback):
        self.tick = callback
        return callback

    def destroy_timer(self, timer):
        pass

    def destroy_client(self, client):
        pass


@pytest.fixture
def transport(monkeypatch):
    modules = {name: ModuleType(name) for name in (
        'malbut_interfaces', 'malbut_interfaces.action', 'malbut_interfaces.srv',
        'rclpy', 'rclpy.action')}
    modules['malbut_interfaces.action'].ExecuteMission = SimpleNamespace(Goal=SimpleNamespace)
    modules['malbut_interfaces.srv'].StopMovement = SimpleNamespace(Request=SimpleNamespace)
    modules['rclpy.action'].ActionClient = Action
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    node, now = Node(), [100.0]
    client = RobotDeviceClient(node, clock=lambda: now[0])
    yield SimpleNamespace(client=client, node=node, now=now)
    client.close()


def manager_result(*, status=4, success=True, code='completed', payload='{}'):
    return SimpleNamespace(status=status, result=SimpleNamespace(result_yaml=yaml.safe_dump({
        'success': success, 'code': code, 'result_json': payload, 'message': 'observed',
    }), message='observed'))


@pytest.mark.parametrize('operation,arguments', [
    ('status', {}), ('runtime_stop', {}), ('map_list', {}),
    ('homecam_settings', {'cameraEnabled': False}), ('result_publish', {'request_id': 'voice'}),
])
def test_device_operation_only_uses_manager_goal(transport, operation, arguments):
    outcomes = []
    client = transport.client
    client.send('voice', operation, arguments, outcomes.append)
    goal, pending = client.action.sent[0]
    assert goal.capability_id == 'device_operation'
    envelope = yaml.safe_load(goal.arguments_yaml)
    assert envelope == {'request_id': 'voice', 'operation': operation,
                        'arguments_json': json.dumps(arguments, ensure_ascii=False)}
    handle = Handle()
    pending.set_result(handle)
    handle.result.set_result(manager_result(payload='{"runtime":{"state":"STOPPED"}}'))
    assert outcomes == [{'success': True, 'code': 'completed',
                         'result': {'runtime': {'state': 'STOPPED'}}, 'message': 'observed'}]


@pytest.mark.parametrize('operation', ['runtime_start', 'map_select'])
def test_preparation_keeps_original_movement_epoch_when_acceptance_is_late(transport, operation):
    outcomes = []
    arguments = {'map': 'home.yaml', 'movement_runtime_id': 'resident', 'movement_epoch': 8}
    transport.client.send('prepare', operation, arguments, outcomes.append)
    goal, pending = transport.client.action.sent[0]
    arguments['movement_epoch'] = 9
    transport.client.cancel('prepare')
    handle = Handle()
    pending.set_result(handle)
    assert handle.canceled == 1
    assert goal.require_movement_epoch is True
    assert goal.movement_runtime_id == 'resident' and goal.movement_epoch == 8
    assert json.loads(yaml.safe_load(goal.arguments_yaml)['arguments_json'])['movement_epoch'] == 8
    assert len(transport.client.action.sent) == 1


@pytest.mark.parametrize('operation', ['runtime_start', 'map_select'])
@pytest.mark.parametrize('binding', [{}, {'movement_runtime_id': 'resident', 'movement_epoch': True}])
def test_preparation_without_observed_epoch_is_not_sent(transport, operation, binding):
    outcomes = []
    transport.client.send('prepare', operation, binding, outcomes.append)
    assert transport.client.action.sent == []
    assert outcomes[0]['code'] == 'preparation_not_ready'


@pytest.mark.parametrize('status,message', [(5, 'canceled before admission'), (6, 'movement_stopped')])
def test_manager_confirmed_stop_without_backend_result_completes_observation(transport, status, message):
    outcomes = []
    transport.client.send('status', 'status', {}, outcomes.append)
    handle = Handle()
    transport.client.action.sent[0][1].set_result(handle)
    handle.result.set_result(SimpleNamespace(status=status, result=SimpleNamespace(
        result_yaml='', message=message)))
    assert transport.client.snapshot('status') == {'done': True, 'code': 'canceled'}
    assert outcomes[0]['success'] is False


@pytest.mark.parametrize('status,message', [(5, 'canceled'), (6, 'movement_stopped')])
def test_localization_stop_unconfirmed_is_not_hidden_by_public_stop_reason(transport, status, message):
    outcomes = []
    transport.client.send('status', 'status', {}, outcomes.append)
    handle = Handle()
    transport.client.action.sent[0][1].set_result(handle)
    result = manager_result(status=status, success=False, code='stop_unconfirmed')
    result.result.message = message
    handle.result.set_result(result)
    assert transport.client.snapshot('status') == {'done': True, 'code': 'stop_unconfirmed'}
    assert outcomes[0]['success'] is False
    assert outcomes[0]['code'] == 'stop_unconfirmed'


def test_manager_epoch_rejection_proves_preparation_was_not_dispatched(transport):
    outcomes = []
    transport.client.send('prepare', 'runtime_start', {
        'mode': 'mapping', 'movement_runtime_id': 'resident', 'movement_epoch': 8,
    }, outcomes.append)
    handle = Handle()
    transport.client.action.sent[0][1].set_result(handle)
    handle.result.set_result(SimpleNamespace(status=6, result=SimpleNamespace(
        result_yaml=yaml.safe_dump({'code': 'movement_epoch_changed',
                                   'movement_runtime_id': 'resident', 'movement_epoch': 9}),
        message='Movement was stopped after this request was prepared')))
    assert transport.client.snapshot('prepare') == {
        'done': True, 'code': 'movement_epoch_changed', 'not_dispatched': True,
    }
    assert outcomes[0]['success'] is False
    assert '실행하지 않았어요' in outcomes[0]['message']


@pytest.mark.parametrize('payload', [
    {'code': 'movement_epoch_changed', 'movement_runtime_id': 'resident', 'movement_epoch': True},
    {'code': 'movement_epoch_changed', 'movement_runtime_id': '', 'movement_epoch': 9},
    {'code': 'movement_epoch_changed', 'movement_runtime_id': 'resident', 'movement_epoch': -1},
])
def test_invalid_admission_rejection_remains_unknown(transport, payload):
    outcomes = []
    transport.client.send('prepare', 'status', {}, outcomes.append)
    handle = Handle()
    transport.client.action.sent[0][1].set_result(handle)
    handle.result.set_result(SimpleNamespace(status=6, result=SimpleNamespace(
        result_yaml=yaml.safe_dump(payload), message='rejected')))
    assert transport.client.snapshot('prepare') == {'done': True, 'code': 'unknown'}


def test_backend_epoch_error_does_not_prove_preparation_was_not_dispatched(transport):
    outcomes = []
    transport.client.send('prepare', 'status', {}, outcomes.append)
    handle = Handle()
    transport.client.action.sent[0][1].set_result(handle)
    handle.result.set_result(manager_result(status=6, success=False, code='movement_epoch_changed'))
    assert transport.client.snapshot('prepare') == {'done': True, 'code': 'movement_epoch_changed'}
    assert 'not_dispatched' not in outcomes[0]


def test_rejected_goal_proves_no_dispatch_even_when_acceptance_is_late(transport):
    outcomes = []
    transport.client.send('prepare', 'status', {}, outcomes.append)
    transport.client.cancel('prepare')
    handle = Handle()
    handle.accepted = False
    transport.client.action.sent[0][1].set_result(handle)
    assert transport.client.snapshot('prepare') == {
        'done': True, 'code': 'rejected', 'not_dispatched': True,
    }


def test_missing_manager_does_not_fall_back_to_device_action(transport):
    outcomes = []
    transport.client.action.ready = False
    transport.client.send('voice', 'runtime_start', {}, outcomes.append)
    assert transport.client.action.sent == []
    assert outcomes[0]['code'] == 'unavailable'


@pytest.mark.parametrize('status', [5, 6])
def test_manager_terminal_status_cannot_be_overridden_by_device_success(transport, status):
    outcomes = []
    transport.client.send('voice', 'status', {}, outcomes.append)
    handle = Handle()
    transport.client.action.sent[0][1].set_result(handle)
    handle.result.set_result(manager_result(status=status))
    assert outcomes[0]['success'] is False


def test_stop_before_runtime_start_acceptance_cancels_late_handle_once(transport):
    outcomes = []
    client = transport.client
    client.send('wake', 'runtime_start', {'mode': 'mapping',
                'movement_runtime_id': 'resident', 'movement_epoch': 8}, outcomes.append)
    client.cancel('wake')
    handle = Handle()
    client.action.sent[0][1].set_result(handle)
    assert handle.canceled == 1
    assert len(client.action.sent) == 1
    handle.result.set_result(manager_result(status=5, success=False, code='canceled'))
    assert outcomes[0]['success'] is False


def test_timeout_late_acceptance_cancels_without_second_result_or_send(transport):
    outcomes = []
    client = transport.client
    client.send('wake', 'runtime_start', {'mode': 'mapping',
                'movement_runtime_id': 'resident', 'movement_epoch': 8}, outcomes.append)
    transport.now[0] += 241
    transport.node.tick()
    handle = Handle()
    client.action.sent[0][1].set_result(handle)
    handle.result.set_result(manager_result())
    assert handle.canceled == 1
    assert len(client.action.sent) == len(outcomes) == 1
    assert outcomes[0]['code'] == 'timeout'


@pytest.mark.parametrize('payload', ['{"value":NaN}', 'x' * (256 * 1024 + 1)])
def test_unverifiable_result_remains_unknown(transport, payload):
    outcomes = []
    client = transport.client
    client.send('status', 'status', {}, outcomes.append)
    handle = Handle()
    client.action.sent[0][1].set_result(handle)
    handle.result.set_result(manager_result(payload=payload))
    assert outcomes[0]['code'] == 'unknown'


def test_conditional_stop_sends_bound_ids_and_preserves_unresolved(transport):
    outcomes = []
    transport.client.stop('stop', outcomes.append, confirmed_ids=['observed'])
    assert transport.node.request.require_preemption_confirmation is True
    assert transport.node.request.confirmed_preemption_mission_ids == ['observed']
    transport.node.response.set_result(SimpleNamespace(
        stopped=False, code='preemption_confirmation_required', message='new work',
        affected_mission_ids=[], unresolved_mission_ids=['new']))
    assert outcomes[0]['result']['conflicting_mission_ids'] == ['new']
