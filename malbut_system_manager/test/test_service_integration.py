"""Real ROS tests for mixed Action and Service missions on one scheduler."""

from threading import Event, Thread
import time

from action_msgs.msg import GoalStatus
import pytest
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_srvs.srv import SetBool, Trigger
import yaml

from malbut_interfaces.action import ExecuteMission, FollowPerson
from malbut_system_manager.system_manager_node import SystemManagerNode


TIMEOUT_S = 8.0


def _wait(predicate):
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('ROS condition did not complete')


def _result(future):
    _wait(future.done)
    return future.result()


class _Endpoints(Node):
    """Temporary standard Services and an Action with observable completion."""

    def __init__(self):
        super().__init__('test_mixed_mission_endpoints')
        self.requests = []
        self.service_entered = Event()
        self.release_service = Event()
        self.service_finished = Event()
        self.action_entered = Event()
        self.action_finished = Event()
        group = ReentrantCallbackGroup()
        self.create_service(
            SetBool, '/test_mixed/set_bool', self._set_bool, callback_group=group,
        )
        self.create_service(
            Trigger, '/test_mixed/trigger', self._trigger, callback_group=group,
        )
        self.action = ActionServer(
            self,
            FollowPerson,
            '/test_mixed/action',
            execute_callback=self._action,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=group,
        )

    def _set_bool(self, request, response):
        self.requests.append(request.data)
        self.service_entered.set()
        response.success = self.release_service.wait(TIMEOUT_S)
        response.message = 'applied' if response.success else 'test timeout'
        self.service_finished.set()
        return response

    def _trigger(self, _request, response):
        # A generic manager returns the exact response, including false flags.
        response.success = False
        response.message = 'application declined the operation'
        return response

    def _action(self, goal_handle):
        self.action_entered.set()
        deadline = time.monotonic() + TIMEOUT_S
        while not goal_handle.is_cancel_requested:
            if time.monotonic() >= deadline:
                goal_handle.abort()
                return FollowPerson.Result()
            time.sleep(0.01)
        self.action_finished.set()
        goal_handle.canceled()
        return FollowPerson.Result()

    def destroy_node(self):
        """Release the Action graph entities before the owning node."""
        self.action.destroy()
        return super().destroy_node()


def _manifest(capability_id, kind, endpoint, interface, fields, resources):
    return {
        'schema_version': 1,
        'capability': {
            'id': capability_id, 'title': capability_id, 'description': 'test only',
        },
        'command': {'kind': kind, 'name': endpoint, 'type': interface},
        'input': {'fields': fields},
        'execution': {
            'mode': 'FOREGROUND', 'priority': 'NORMAL', 'resources': resources,
        },
    }


class _Harness:
    """Expose public mission requests without reaching into manager internals."""

    def __init__(self, client, endpoints):
        self.client = client
        self.endpoints = endpoints

    def send(self, capability, arguments='{}', feedback=None):
        """Send the same ExecuteMission contract for either transport kind."""
        goal = ExecuteMission.Goal(capability_id=capability, arguments_yaml=arguments)
        handle = _result(self.client.send_goal_async(
            goal,
            feedback_callback=(
                lambda msg: feedback.append(msg.feedback.state)
            ) if feedback is not None else None,
        ))
        assert handle.accepted
        return handle


@pytest.fixture
def mixed_manager(tmp_path):
    """Run a manager against temporary manifests and real ROS graph entities."""
    manifests = [
        _manifest(
            'set_bool', 'SERVICE', '/test_mixed/set_bool', 'std_srvs/srv/SetBool',
            {'data': {'type': 'bool', 'description': 'value', 'default': True}},
            ['BASE'],
        ),
        _manifest(
            'trigger', 'SERVICE', '/test_mixed/trigger', 'std_srvs/srv/Trigger',
            {}, [],
        ),
        _manifest(
            'action', 'ACTION', '/test_mixed/action',
            'malbut_interfaces/action/FollowPerson',
            {
                'target_mode': {'type': 'uint8', 'description': 'mode', 'default': 0},
                'target_person_id': {'type': 'string', 'description': 'id', 'default': ''},
                'desired_distance_m': {
                    'type': 'float32', 'description': 'distance', 'default': 1.0,
                },
            },
            ['BASE'],
        ),
    ]
    for manifest in manifests:
        path = tmp_path / (manifest['capability']['id'] + '.yaml')
        path.write_text(yaml.safe_dump(manifest), encoding='utf-8')
    rclpy.init()
    endpoints = _Endpoints()
    manager = SystemManagerNode(manifest_directory=str(tmp_path))
    requester = Node('test_mixed_mission_requester')
    nodes = (endpoints, manager, requester)
    executor = MultiThreadedExecutor(num_threads=6)
    for node in nodes:
        executor.add_node(node)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    client = ActionClient(requester, ExecuteMission, '/malbut/mission/execute')
    try:
        assert client.wait_for_server(timeout_sec=TIMEOUT_S)
        # Establish graph discovery before dispatch; no execution retry is used.
        probes = [
            requester.create_client(SetBool, '/test_mixed/set_bool'),
            requester.create_client(Trigger, '/test_mixed/trigger'),
        ]
        for probe in probes:
            assert probe.wait_for_service(timeout_sec=TIMEOUT_S)
            requester.destroy_client(probe)
        yield _Harness(client, endpoints)
    finally:
        endpoints.release_service.set()
        manager.begin_shutdown()
        try:
            _wait(lambda: manager.downstream_execution_count == 0)
        except AssertionError:
            manager.force_shutdown()
        client.destroy()
        executor.shutdown(timeout_sec=TIMEOUT_S)
        thread.join(timeout=TIMEOUT_S)
        for node in reversed(nodes):
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_service_request_response_and_empty_request(mixed_manager):
    """Request fields reach a standard server and response YAML is preserved."""
    test = mixed_manager
    test.endpoints.release_service.set()
    handle = test.send('set_bool', '{data: false}')
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_SUCCEEDED
    assert test.endpoints.requests == [False]
    assert yaml.safe_load(result.result.result_yaml) == {
        'success': True, 'message': 'applied',
    }
    result = _result(test.send('trigger').get_result_async())
    assert result.status == GoalStatus.STATUS_SUCCEEDED
    assert yaml.safe_load(result.result.result_yaml)['success'] is False


def test_action_preempting_service_waits_for_response(mixed_manager):
    """Equal-priority replacement cannot overlap an outstanding Service."""
    test = mixed_manager
    feedback = []
    service = test.send('set_bool', feedback=feedback)
    assert test.endpoints.service_entered.wait(TIMEOUT_S)
    replacement = test.send('action')
    _wait(lambda: 'CANCELING' in feedback)
    assert not test.endpoints.action_entered.is_set()
    assert not service.get_result_async().done()
    # A resource-free request remains executable during this transition.
    result = _result(test.send('trigger').get_result_async())
    assert result.status == GoalStatus.STATUS_SUCCEEDED
    assert not test.endpoints.action_entered.is_set()
    test.endpoints.release_service.set()
    assert test.endpoints.action_entered.wait(TIMEOUT_S)
    assert test.endpoints.service_finished.is_set()
    result = _result(service.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert 'preempted' in result.result.message
    assert len(test.endpoints.requests) == 1
    assert _result(replacement.cancel_goal_async()).goals_canceling
    assert _result(replacement.get_result_async()).status == GoalStatus.STATUS_CANCELED


def test_service_preempting_action_waits_for_action_cancellation(mixed_manager):
    """Service admission follows the existing Action resource preemption rule."""
    test = mixed_manager
    action = test.send('action')
    assert test.endpoints.action_entered.wait(TIMEOUT_S)
    test.endpoints.release_service.set()
    service = test.send('set_bool')
    assert test.endpoints.service_entered.wait(TIMEOUT_S)
    assert test.endpoints.action_finished.is_set()
    assert _result(action.get_result_async()).status == GoalStatus.STATUS_ABORTED
    assert _result(service.get_result_async()).status == GoalStatus.STATUS_SUCCEEDED


def test_client_cancel_does_not_abandon_service(mixed_manager):
    """Upper cancellation finishes only after a real response confirms completion."""
    test = mixed_manager
    service = test.send('set_bool')
    assert test.endpoints.service_entered.wait(TIMEOUT_S)
    assert _result(service.cancel_goal_async()).goals_canceling
    result_future = service.get_result_async()
    assert not result_future.done()
    test.endpoints.release_service.set()
    result = _result(result_future)
    assert result.status == GoalStatus.STATUS_CANCELED
    assert 'side effects are not undone' in result.result.message
    assert yaml.safe_load(result.result.result_yaml)['success'] is True
    assert len(test.endpoints.requests) == 1
