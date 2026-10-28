"""
Exercise real patrol methods in both source layouts without a ROS executor.

ROS and sensor boundaries are stubbed. Geometry uses the actual CoveragePlanner,
and the execution/cleanup callback bodies are compiled unchanged from each copy.
The real Action/executor boundary is covered separately by test_patrol_action.
"""

import ast
import importlib.util
import json
import math
from pathlib import Path
import sys
from threading import Event, RLock
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest


REPOSITORY = Path(__file__).resolve().parents[3]
LAYOUTS = ('malbut_autonomy/malbut_patrol', 'malbut_test/malbut_patrol')


@pytest.fixture(params=LAYOUTS)
def patrol(request, monkeypatch):
    package = REPOSITORY / request.param / 'malbut_patrol'
    spec = importlib.util.spec_from_file_location('cleanup_coverage', package / 'coverage.py')
    coverage = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, coverage)
    spec.loader.exec_module(coverage)
    source = package / 'patrol_manager.py'
    tree = ast.parse(source.read_text())
    selected = [node for node in tree.body if isinstance(node, ast.ClassDef)
                and node.name in ('PatrolInterrupted', 'PatrolManager')]
    selected[-1].bases = []  # No Node initialization or ROS transport in this test.
    namespace = {
        'json': json, 'math': math, 'time': time,
        'String': SimpleNamespace, 'Patrol': SimpleNamespace(
            Result=SimpleNamespace, Feedback=SimpleNamespace),
        'TransformException': type('TransformException', (Exception,), {}),
        'rclpy': SimpleNamespace(ok=lambda: True),
        'NavigateToPose': SimpleNamespace(Goal=SimpleNamespace),
        'PoseStamped': lambda: SimpleNamespace(
            header=SimpleNamespace(), pose=SimpleNamespace(
                position=SimpleNamespace(), orientation=SimpleNamespace())),
        '_grid': lambda _: coverage.CoverageGrid(np.zeros((30, 30)), 0.1, 0, 0),
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    manager = object.__new__(namespace['PatrolManager'])
    manager.lock = RLock()
    manager.busy = True
    manager.child = None
    manager.stop_event = Event()
    manager.map_changed = False
    manager.last_pump = 0.0
    manager.map_message = SimpleNamespace(header=SimpleNamespace(frame_id='map'))
    manager.camera_info = SimpleNamespace(width=640, k=[320.0])
    manager.settings = {
        'room_map_file': '', 'robot_clearance_m': 0.0,
        'observation_hz': 5.0, 'sensor_timeout_s': 10.0,
        'navigation_timeout_s': 10.0,
        'cancel_completion_timeout_s': 1.0,
        'inspection_calibration_file': '', 'coverage_source': 'frames',
        'require_room_entry': False,
    }
    manager.profiles = {1: coverage.CoverageProfile(3.0, 0.9, 0.8)}
    manager._ready = Mock(return_value='')
    manager._robot_xy = Mock(return_value=(0.55, 0.55))
    manager._observe = Mock()
    manager._allowed = Mock(return_value=True)
    manager._run_child = Mock()
    manager.navigation = Mock()
    manager.get_clock = Mock(return_value=SimpleNamespace(
        now=lambda: SimpleNamespace(to_msg=lambda: SimpleNamespace())))
    manager._change_processing = Mock()
    manager.get_logger = Mock()
    manager.status = Mock()

    def make_planner(*args, **kwargs):
        planner = coverage.CoveragePlanner(*args, **kwargs)
        # A legitimate partial patrol; no Nav2 goal is needed to exercise
        # feedback, room metadata, terminal result and processing cleanup.
        planner.select = Mock(return_value=None)
        return planner

    namespace['CoveragePlanner'] = make_planner
    handle = Mock(request=SimpleNamespace(thoroughness=1), is_cancel_requested=False)
    return manager, handle


@pytest.mark.parametrize('publisher_fails', [False, True])
def test_partial_patrol_releases_inputs_even_if_status_publication_fails(patrol, publisher_fails):
    manager, handle = patrol
    if publisher_fails:
        manager.status.publish.side_effect = RuntimeError('diagnostic publisher unavailable')
    result = manager._execute(handle)
    assert not result.success
    assert result.message.startswith('No usable untried viewpoint')
    handle.abort.assert_called_once()
    handle.publish_feedback.assert_called_once()
    manager._run_child.assert_not_called()
    assert manager.planner.room_names == ()
    assert not manager.busy
    response = manager._set_processing(SimpleNamespace(data=False), SimpleNamespace())
    assert response.success
    manager._change_processing.assert_called_once_with(False)
    if not publisher_fails:
        status = json.loads(manager.status.publish.call_args.args[0].data)
        assert status['state'] == 'aborted' and status['room_count'] == 0


def test_status_serialization_failure_cannot_prevent_child_cancellation(patrol):
    manager, _ = patrol
    manager.planner = SimpleNamespace(coverage_ratio=0.0)  # Broken diagnostic metadata.
    manager.visited = 0
    child = SimpleNamespace(settled=False, wake=Mock())
    child.cancel = Mock(side_effect=lambda: setattr(child, 'settled', True))
    manager.child = child
    assert manager._settle_child()
    child.cancel.assert_called_once()
    manager.get_logger().warning.assert_called_once()


def test_deployed_patrol_reaches_navigation_instead_of_failing_on_room_metadata(patrol):
    manager, handle = patrol
    namespace = manager._execute.__func__.__globals__
    original = namespace['CoveragePlanner']

    def make_one_viewpoint(*args, **kwargs):
        planner = original(*args, **kwargs)
        viewpoint = type(planner).select(planner, manager._robot_xy(), manager._allowed)
        assert viewpoint is not None
        planner.select.side_effect = [viewpoint, None]
        return planner

    namespace['CoveragePlanner'] = make_one_viewpoint
    manager._run_child.return_value = False  # A confirmed failed Nav2 result.
    result = manager._execute(handle)
    assert not result.message.startswith('Patrol failed:'), result.message
    manager._run_child.assert_called_once()
    client, request, owner, _ = manager._run_child.call_args.args
    assert client is manager.navigation and owner is handle
    assert request.pose.header.frame_id == 'map'
    assert not result.success and not manager.busy


def test_terminal_action_error_cannot_strand_settled_patrol(patrol):
    manager, handle = patrol
    handle.abort.side_effect = RuntimeError('Action transport closed')
    with pytest.raises(RuntimeError, match='Action transport closed'):
        manager._execute(handle)
    assert not manager.busy


def test_execution_error_is_aborted_and_releases_confirmed_ownership(patrol):
    manager, handle = patrol
    manager._observe.side_effect = RuntimeError('camera processing failed')
    result = manager._execute(handle)
    assert not result.success and 'camera processing failed' in result.message
    handle.abort.assert_called_once()
    assert not manager.busy


def test_unconfirmed_child_stop_keeps_patrol_inputs_owned(patrol):
    manager, handle = patrol
    manager._settle_child = Mock(return_value=False)
    result = manager._execute(handle)
    assert not result.success and 'Nav2 stop unconfirmed' in result.message
    assert manager.busy
    response = manager._set_processing(SimpleNamespace(data=False), SimpleNamespace())
    assert not response.success and response.message == 'patrol is still running'
    manager._change_processing.assert_not_called()


def test_processing_disable_check_and_change_share_goal_acceptance_lock(patrol):
    manager, _ = patrol
    manager.busy = False
    manager._change_processing.side_effect = lambda _: (
        manager.lock._is_owned() or pytest.fail('processing changed without ownership lock'))
    assert manager._set_processing(SimpleNamespace(data=False), SimpleNamespace()).success
