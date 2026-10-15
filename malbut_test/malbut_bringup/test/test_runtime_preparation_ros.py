"""
ROS callback regression tests, without Gazebo, sensors or robot motion.

The backend is a deterministic lifecycle/processing service fixture. These
tests exercise the real RuntimeControl services, callback groups and executor.
"""

from functools import partial
from threading import Event, Thread
import time
from types import SimpleNamespace

from malbut_interfaces.msg import MissionStatus, SystemState
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from nav2_msgs.srv import ManageLifecycleNodes
import pytest
import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_srvs.srv import SetBool, Trigger

from malbut_bringup.runtime_control import GROUPS, PROCESSING_GROUPS, RuntimeControl
from malbut_bringup.nav2_stack import MOTION_NODES, PLANNING_NODES


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'ROS preparation did not settle'
        time.sleep(0.01)


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv('ROS_DOMAIN_ID', '228')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    rclpy.init()
    control = RuntimeControl()
    # Only map/sensor initialization is bypassed; preparation is unmodified.
    control._initialized = control._map_initialized = True
    control._wait_map_tf = lambda: None
    control._sync()
    backend = Node('runtime_backend_fixture')
    commands = []
    processing = []
    active = {group: False for group in GROUPS}
    delay = SimpleNamespace(release=None, entered=Event())

    def manage(group, request, response):
        commands.append((group, request.command))
        active[group] = request.command == ManageLifecycleNodes.Request.RESUME
        if delay.release is not None:
            delay.entered.set()
            assert delay.release.wait(5), 'test did not release the lifecycle reply'
        response.success = True
        return response

    def process(service, request, response):
        processing.append((service, request.data))
        response.success = True
        return response

    def manager_state(group, request, response):
        response.success = active[group]
        return response

    def node_state(group, request, response):
        response.current_state.id = (State.PRIMARY_STATE_ACTIVE if active[group]
                                     else State.PRIMARY_STATE_INACTIVE)
        return response

    for group, service in GROUPS.items():
        backend.create_service(ManageLifecycleNodes, service, partial(manage, group))
        backend.create_service(Trigger, service.replace('/manage_nodes', '/is_active'),
                               partial(manager_state, group))
        for name in (MOTION_NODES if group == 'motion' else PLANNING_NODES):
            backend.create_service(GetState, '/' + name + '/get_state',
                                   partial(node_state, group))
    for services in PROCESSING_GROUPS.values():
        for service in services:
            backend.create_service(SetBool, service, partial(process, service))
    operator = Node('runtime_preparation_client')
    publisher = operator.create_publisher(
        SystemState, '/malbut/state',
        QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    clients = {cap: operator.create_client(Trigger, '/malbut/runtime/prepare/' + cap)
               for cap in ('follow_person', 'manual_drive')}
    executor = MultiThreadedExecutor(num_threads=2)
    for node in (control, backend, operator):
        executor.add_node(node)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        _wait(lambda: all(client.service_is_ready() for client in clients.values()))
        yield SimpleNamespace(control=control, publisher=publisher, clients=clients,
                              commands=commands, processing=processing, active=active,
                              delay=delay)
    finally:
        if delay.release is not None:
            delay.release.set()
        control._closing = True
        control._resolve_preparations()
        _wait(lambda: not control._preparations)
        assert executor.shutdown(timeout_sec=5)
        thread.join(5)
        executor._executor.shutdown(wait=True)
        assert not thread.is_alive()
        for node in (control, backend, operator):
            node.destroy_node()
        rclpy.shutdown()


def _mission(capability, identifier):
    return MissionStatus(mission_id=identifier, capability_id=capability,
                         state=MissionStatus.RUNNING)


def test_more_canceled_preparations_than_workers_do_not_block_a_new_request(runtime):
    stale = []
    for _ in range(12):
        stale.append(runtime.clients['follow_person'].call_async(Trigger.Request()))
        # Admit each RPC before the next, without overflowing DDS KEEP_LAST.
        _wait(lambda: len(runtime.control._preparations) == len(stale))
        # This is how MissionExecutor stops waiting locally. The server's
        # Trigger callback remains pending because ROS services have no cancel.
        runtime.clients['follow_person'].remove_pending_request(stale[-1])
        stale[-1].cancel()
    runtime.publisher.publish(SystemState(active_foreground_missions=[
        _mission('manual_drive', 'new-manual')]))
    current = runtime.clients['manual_drive'].call_async(Trigger.Request())
    _wait(current.done)
    assert current.result().success
    assert len(runtime.control._preparations) == len(stale)
    assert all(future.cancelled() for future in stale)
    assert runtime.commands == [('motion', ManageLifecycleNodes.Request.RESUME)]
    # Closing must also resolve the RPCs whose client already canceled.
    runtime.control._closing = True
    runtime.control._resolve_preparations()
    _wait(lambda: not runtime.control._preparations)


def test_background_owner_keeps_planning_after_foreground_tracking_finishes(runtime):
    runtime.publisher.publish(SystemState(
        active_foreground_missions=[_mission('follow_person', 'follow')],
        active_background_missions=[_mission('compute_path_to_pose', 'path-a'),
                                    _mission('compute_path_to_pose', 'path-b')]))
    prepared = runtime.clients['follow_person'].call_async(Trigger.Request())
    _wait(prepared.done)
    assert prepared.result().success
    assert runtime.control._active == {'motion': True, 'planning': True}
    assert all(runtime.control._processing_active[service]
               for service in PROCESSING_GROUPS['person_perception'])
    assert not runtime.control._processing_active['/patrol_manager/set_processing']
    runtime.publisher.publish(SystemState(active_background_missions=[
        _mission('compute_path_to_pose', 'path-a')]))
    _wait(lambda: not any(runtime.control._processing_active.values()))
    _wait(lambda: runtime.control._active == {'motion': False, 'planning': True})
    assert runtime.control._synced_demand == (frozenset({'compute_path_to_pose'}), False)
    assert ('planning', ManageLifecycleNodes.Request.PAUSE) not in runtime.commands
    runtime.publisher.publish(SystemState())
    _wait(lambda: runtime.control._synced_demand == (frozenset(), False))
    assert runtime.control._active == {'motion': False, 'planning': False}
    assert runtime.commands.count(('planning', ManageLifecycleNodes.Request.PAUSE)) == 1


def test_new_request_detects_a_native_pause_not_recorded_in_the_local_cache(runtime):
    runtime.publisher.publish(SystemState(active_foreground_missions=[
        _mission('manual_drive', 'manual')]))
    _wait(lambda: runtime.control._capabilities == {'manual_drive'})
    prepared = runtime.clients['manual_drive'].call_async(Trigger.Request())
    _wait(prepared.done)
    assert prepared.result().success
    # Model native lifecycle/bond handling outside RuntimeControl. The mission
    # demand and local active cache have not changed.
    runtime.active['motion'] = False
    assert runtime.control._active['motion']
    prepared = runtime.clients['manual_drive'].call_async(Trigger.Request())
    _wait(prepared.done)
    assert prepared.result().success
    assert runtime.commands.count(('motion', ManageLifecycleNodes.Request.RESUME)) == 2


def test_lifecycle_reply_after_timeout_is_consumed_without_duplicate_activation(runtime):
    runtime.control.timeout = 0.1
    runtime.delay.release = Event()
    runtime.publisher.publish(SystemState(active_foreground_missions=[
        _mission('manual_drive', 'slow-manual')]))
    assert runtime.delay.entered.wait(5)
    _wait(lambda: runtime.control._uncertain)
    # A timeout does not prove that the already-sent RESUME failed remotely.
    assert runtime.commands == [('motion', ManageLifecycleNodes.Request.RESUME)]
    runtime.delay.release.set()
    _wait(lambda: not runtime.control._uncertain and runtime.control._active['motion'])
    prepared = runtime.clients['manual_drive'].call_async(Trigger.Request())
    _wait(prepared.done)
    assert prepared.result().success
    assert runtime.commands == [('motion', ManageLifecycleNodes.Request.RESUME)]
