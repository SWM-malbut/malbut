"""Observe Action acceptance and cancellation independently of workflow sends."""

from concurrent.futures import Future
import sys
from types import ModuleType, SimpleNamespace

import pytest

from malbut_agent_server.robot_device_client import RobotDeviceClient


class Action:
    def __init__(self, node, action_type, name):
        assert name == '/malbut/device/operate'
        self.sent = []

    def server_is_ready(self):
        return True

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
    modules['malbut_interfaces.action'].DeviceOperation = SimpleNamespace(Goal=SimpleNamespace)
    modules['malbut_interfaces.srv'].StopMovement = SimpleNamespace(Request=SimpleNamespace)
    modules['rclpy.action'].ActionClient = Action
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    node, now = Node(), [100.0]
    client = RobotDeviceClient(node, clock=lambda: now[0])
    yield SimpleNamespace(client=client, node=node, now=now)
    client.close()


def test_stop_before_runtime_start_acceptance_cancels_late_handle_once(transport):
    outcomes = []
    client = transport.client
    client.send('wake', 'runtime_start', {'mode': 'mapping'}, outcomes.append)
    client.cancel('wake')
    handle = Handle()
    client.action.sent[0][1].set_result(handle)
    assert handle.canceled == 1
    assert len(client.action.sent) == 1
    handle.result.set_result(SimpleNamespace(status=5, result=SimpleNamespace(
        success=False, code='canceled', result_json='{}', message='canceled')))
    assert outcomes[0]['success'] is False


def test_timeout_late_acceptance_cancels_without_second_result_or_send(transport):
    outcomes = []
    client = transport.client
    client.send('wake', 'runtime_start', {'mode': 'mapping'}, outcomes.append)
    transport.now[0] += 241
    transport.node.tick()
    handle = Handle()
    client.action.sent[0][1].set_result(handle)
    handle.result.set_result(SimpleNamespace(status=4, result=SimpleNamespace(
        success=True, code='completed', result_json='{}', message='late')))
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
    handle.result.set_result(SimpleNamespace(status=4, result=SimpleNamespace(
        success=True, code='completed', result_json=payload, message='untrusted')))
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
