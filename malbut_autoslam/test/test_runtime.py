"""Validate startup ownership without launching robot or simulator processes."""

from dataclasses import replace
import signal
from unittest.mock import Mock

import pytest

from malbut_autoslam.runtime import OwnedRuntime, RuntimeGraph, missing_components


EMPTY = RuntimeGraph((), (), (), (), (), False)
READY = RuntimeGraph(
    ('/slam_toolbox', '/controller_server', '/planner_server', '/bt_navigator'),
    ('/slam_toolbox',), ('/lidar',), ('/ekf',), ('/scan_normalizer',), True)


def test_empty_graph_requests_only_predefined_mapping_components():
    """An empty real runtime needs sensors, mapper, Nav2 and scan adaptation."""
    assert all(missing_components(EMPTY).values())


def test_existing_mapping_components_are_never_restarted():
    """An externally prepared complete graph produces no process launches."""
    assert not any(missing_components(READY).values())


def test_existing_sensors_are_reused_when_mapping_is_missing():
    """Do not launch a second factory robot composition over running sensors."""
    graph = replace(EMPTY, scan_publishers=('/lidar',), odom_publishers=('/ekf',))
    assert missing_components(graph) == {
        'start_hardware': False, 'start_slam': True,
        'start_navigation': True, 'start_scan_adapter': True,
    }


def test_existing_raw_scan_mapper_does_not_need_our_adapter():
    """Reuse a complete external pipeline that never used normalized scans."""
    assert not any(missing_components(
        replace(READY, normalized_scan_publishers=())).values())


def test_existing_nav2_nodes_await_readiness_without_duplicate_startup():
    """Discovery of an inactive component is enough to prohibit a second launch."""
    graph = replace(READY, navigation_present=False, map_publishers=())
    assert not missing_components(graph)['start_navigation']
    assert not missing_components(graph)['start_slam']


@pytest.mark.parametrize('graph, message', [
    (replace(READY, nodes=READY.nodes + ('/amcl',)), 'Saved-map'),
    (replace(READY, nodes=READY.nodes + ('/map_server',)), 'Saved-map'),
    (replace(READY, nodes=READY.nodes + ('/slam_toolbox',)), 'Duplicate'),
    (replace(READY, map_publishers=('/slam_toolbox', '/other')), 'Multiple map'),
    (replace(READY, map_publishers=('/custom_mapper',)), 'Unknown map'),
    (replace(READY, scan_publishers=('/lidar', '/lidar2')), 'Multiple scan'),
    (replace(READY, odom_publishers=()), 'Only part'),
    (replace(EMPTY, nodes=('/robot_state_publisher',)), 'Hardware nodes exist'),
])
def test_ambiguous_or_incompatible_runtime_is_not_modified(graph, message):
    """Conflicts must be resolved explicitly instead of killing external owners."""
    with pytest.raises(RuntimeError, match=message):
        missing_components(graph)


def test_local_mapping_lock_prevents_two_servers_racing(tmp_path):
    """Hold one advisory lock for the whole Goal, including reused components."""
    first, second = OwnedRuntime(tmp_path), OwnedRuntime(tmp_path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match='Another AutoSLAM'):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()


def test_launch_uses_argument_list_and_new_owned_session(tmp_path, monkeypatch):
    """Only the predefined helper receives separate launch argument tokens."""
    process = Mock(pid=1234)
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr('malbut_autoslam.runtime.subprocess.Popen', popen)
    runtime = OwnedRuntime(tmp_path)
    runtime.acquire()
    components = missing_components(EMPTY)
    runtime.start(components, '/scan_raw', '/odom', '/scan_normalized')
    command = popen.call_args.args[0]
    assert command[:4] == ['ros2', 'launch', 'malbut_bringup', 'mapping_backend.launch.py']
    assert 'start_hardware:=true' in command
    assert 'normalized_scan_topic:=/scan_normalized' in command
    assert popen.call_args.kwargs['start_new_session'] is True
    assert 'shell' not in popen.call_args.kwargs
    runtime.check()
    process.poll.return_value = 2
    with pytest.raises(RuntimeError, match='Mapping launch exited'):
        runtime.check()
    monkeypatch.setattr(runtime, '_group_alive', lambda: False)
    runtime.close()


def test_reused_runtime_does_not_spawn_or_signal_any_process(tmp_path, monkeypatch):
    """Even successful completion never stops externally supplied ROS nodes."""
    popen, killpg = Mock(), Mock()
    monkeypatch.setattr('malbut_autoslam.runtime.subprocess.Popen', popen)
    monkeypatch.setattr('malbut_autoslam.runtime.os.killpg', killpg)
    runtime = OwnedRuntime(tmp_path)
    runtime.acquire()
    runtime.start(missing_components(READY), '/scan_raw', '/odom', '/scan_normalized')
    runtime.close()
    popen.assert_not_called()
    killpg.assert_not_called()


def test_shutdown_escalates_only_within_owned_group(tmp_path, monkeypatch):
    """Hung launch cleanup is bounded and never uses global process name matching."""
    runtime = OwnedRuntime(tmp_path)
    runtime.acquire()
    runtime.process = Mock(pid=1234)
    alive = True
    signals = []

    def send(pid, sig):
        nonlocal alive
        signals.append((pid, sig))
        if sig == signal.SIGKILL:
            alive = False

    ticks = iter(range(0, 1000, 5))
    monkeypatch.setattr('malbut_autoslam.runtime.time.monotonic', lambda: next(ticks))
    monkeypatch.setattr('malbut_autoslam.runtime.os.killpg', send)
    monkeypatch.setattr(runtime, '_group_alive', lambda: alive)
    runtime.close()
    assert signals == [(1234, signal.SIGINT), (1234, signal.SIGTERM), (1234, signal.SIGKILL)]
    assert runtime.lock_file is None


def test_unconfirmed_shutdown_keeps_startup_lock(tmp_path, monkeypatch):
    """Do not allow a replacement runtime when owned processes cannot be removed."""
    runtime = OwnedRuntime(tmp_path)
    runtime.acquire()
    runtime.process = Mock(pid=1234)
    ticks = iter(range(0, 1000, 5))
    monkeypatch.setattr('malbut_autoslam.runtime.time.monotonic', lambda: next(ticks))
    monkeypatch.setattr('malbut_autoslam.runtime.os.killpg', Mock())
    monkeypatch.setattr(runtime, '_group_alive', lambda: True)
    try:
        with pytest.raises(RuntimeError, match='ownership retained'):
            runtime.close()
        assert runtime.lock_file is not None
    finally:
        monkeypatch.setattr(runtime, '_group_alive', lambda: False)
        runtime.close()
