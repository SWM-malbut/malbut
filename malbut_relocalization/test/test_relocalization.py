"""Check relocalization decisions without AMCL, Nav2 or a moving robot."""

from concurrent.futures import Future
import json
import math
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from geometry_msgs.msg import PoseWithCovarianceStamped, TransformStamped
from lifecycle_msgs.msg import State
from malbut_interfaces.action import Relocalize
from malbut_relocalization.pose_store import map_identity, valid_pose, write_pose
from malbut_relocalization.relocalization_node import (
    accepts, Canceled, pose_message, pose_record, Relocalization,
)
import pytest
from rclpy.time import Time
from rcl_interfaces.msg import ParameterType, ParameterValue
from std_msgs.msg import String
from malbut_relocalization import relocalization_node

TRUE_POSE = (0.8, 0.4, 0.3)
WRONG_POSE = (-0.9, -0.5, 2.0)


def test_shutdown_drains_workers_before_destroying_node(monkeypatch):
    events = []
    node, executor = Mock(), Mock()
    executor.spin.side_effect = KeyboardInterrupt
    executor.shutdown.side_effect = lambda: events.append('stop executor')
    executor._executor.shutdown.side_effect = lambda **_: events.append('join workers')
    node.save_latest.side_effect = lambda: events.append('save pose')
    node.destroy_node.side_effect = lambda: events.append('destroy node')
    monkeypatch.setattr(relocalization_node, 'Relocalization', lambda: node)
    monkeypatch.setattr(relocalization_node, 'MultiThreadedExecutor', lambda **_: executor)
    monkeypatch.setattr(relocalization_node.rclpy, 'init', Mock())
    monkeypatch.setattr(relocalization_node.rclpy, 'ok', lambda: False)
    relocalization_node.main()
    assert events == ['stop executor', 'join workers', 'save pose', 'destroy node']
    executor._executor.shutdown.assert_called_once_with(wait=True)


def _pose(x=1.0, y=2.0, yaw=0.3, **values):
    return {'frame_id': 'map', 'x': x, 'y': y, 'yaw': yaw,
            'covariance': [0.1 if i % 7 == 0 else 0.0 for i in range(36)], **values}


def _done(value):
    future = Future()
    future.set_result(value)
    return future


class _Amcl:
    """Stand-in AMCL: answers initial poses and converges during a search."""

    def __init__(self, node, found=TRUE_POSE):
        self.node, self.found = node, found
        self.calls = []

    def estimate(self, x, y, yaw):
        message = pose_message(_pose(x, y, yaw), Time(seconds=100).to_msg())
        self.node.estimate = (self.node.clock.now + 1e-3, pose_record(message), message)
        # A scan follows every estimate.
        self.node.scan = (self.node.clock.now + 2e-3, self.node.room.scan(*TRUE_POSE))

    def initial_pose(self, message):
        self.calls.append('initialpose')
        pose = pose_record(message)
        self.estimate(pose['x'], pose['y'], pose['yaw'])

    def client(self, name):
        def call_async(_request):
            self.calls.append(name)
            if name == 'nomotion' and self.found is not None:
                self.estimate(*self.found)
            return _done(SimpleNamespace())
        return SimpleNamespace(wait_for_service=lambda timeout_sec: True, call_async=call_async)


class _Spin:
    def __init__(self, ready=True, accepted=True):
        self.ready, self.accepted, self.goals = ready, accepted, []

    def wait_for_server(self, timeout_sec):
        return self.ready

    def send_goal_async(self, goal):
        self.goals.append(goal)
        return _done(SimpleNamespace(accepted=self.accepted, cancel_goal_async=Mock(),
                                     get_result_async=lambda: _done(SimpleNamespace())))


def _node(monkeypatch, room, found=TRUE_POSE, spin=None):
    node = object.__new__(Relocalization)
    node._context = None
    node.settings = {'base_frame': 'base_footprint', 'match_distance_m': 0.10,
                     'match_ratio_min': 0.95, 'max_scan_range_m': 8.0,
                     'min_particles': 2000, 'max_particles': 8000}
    node.path = room.map_file.parent / 'last_pose.yaml'
    node.period, node.timeout = 5.0, 10.0
    node.lock = threading.Lock()
    node.map_file = node.identity = node.expected_map = None
    node.saving = False
    node.map_message = node.field = node.scan = None
    node.latest = None
    node.received = node.saved_received = 0.0
    node.initial_stamp = 0
    node.estimate = None
    node.busy = False
    node._start_observing = Mock()
    node._stop_observing = Mock()
    node.amcl_active = True
    node.state_future = None
    node.state_requested = 100.0
    node.get_logger = Mock()
    node.get_clock = lambda: SimpleNamespace(now=lambda: Time(seconds=100))
    node.get_subscriptions_info_by_topic = lambda _: [SimpleNamespace(node_name='amcl')]
    node.room = room
    # Waiting advances a fake clock, so timeouts end without real sleeping.
    node.clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr('malbut_relocalization.relocalization_node.time.monotonic',
                        lambda: node.clock.now)
    monkeypatch.setattr('malbut_relocalization.relocalization_node.time.sleep',
                        lambda seconds: setattr(node.clock, 'now', node.clock.now + seconds))
    monkeypatch.setattr('malbut_relocalization.relocalization_node.rclpy.ok',
                        lambda context=None: True)
    node.amcl = _Amcl(node, found)
    node.particle_limits, node.parameter_calls = (500, 3000), []

    def read_parameters(_request):
        return _done(SimpleNamespace(values=[ParameterValue(
            type=ParameterType.PARAMETER_INTEGER, integer_value=value)
            for value in node.particle_limits]))

    def set_parameters(request):
        node.particle_limits = tuple(item.value.integer_value for item in request.parameters)
        node.parameter_calls.append(node.particle_limits)
        # Humble reinitializes the filter at its startup pose on a limit change.
        node.amcl.estimate(0.0, 0.0, 0.0)
        return _done(SimpleNamespace(result=SimpleNamespace(successful=True, reason='')))

    node._particle_reader = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True, call_async=read_parameters)
    node._particle_writer = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True, call_async=set_parameters)
    node.publisher = Mock()
    node.publisher.publish.side_effect = node.amcl.initial_pose
    node.client = Mock()
    node.client.service_is_ready.return_value = True
    node.global_localization = node.amcl.client('global')
    node.nomotion_update = node.amcl.client('nomotion')
    node.spin = spin or _Spin()
    transform = TransformStamped()
    transform.transform.rotation.w = 1.0
    node.tf = SimpleNamespace(lookup_transform=lambda *_: transform)
    node.scan = (node.clock.now, room.scan(*TRUE_POSE))
    return node


def _select(node, map_file, mode='LOCALIZATION'):
    node._localization(String(data=json.dumps({'mode': mode, 'map': str(map_file)})))


def _ready(node, room):
    _select(node, room.map_file)
    node._map(room.grid())


def _handle(method=Relocalize.Goal.AUTO, initial_pose=None, cancel=False):
    goal = Relocalize.Goal()
    goal.method = method
    if initial_pose is not None:
        goal.initial_pose = initial_pose
    return SimpleNamespace(request=goal, is_cancel_requested=cancel,
                           publish_feedback=Mock(), succeed=Mock(), abort=Mock(),
                           canceled=Mock())


def _save(node, room, x, y, yaw):
    write_pose(node.path, map_identity(room.map_file), _pose(x, y, yaw))


def test_matching_saved_pose_is_kept_without_searching(monkeypatch, room):
    """Robot not moved while off: the saved pose fits the scan and is kept."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    _save(node, room, *TRUE_POSE)
    handle = _handle()
    result = node._execute(handle)
    handle.succeed.assert_called_once()
    assert result.success and 'saved pose confirmed' in result.message
    assert result.match_ratio > 0.9
    assert node.amcl.calls == ['initialpose', 'initialpose'] and not node.spin.goals
    assert node.parameter_calls == [(2000, 8000), (500, 3000)]
    assert not node.busy


def test_moved_robot_is_found_by_global_search(monkeypatch, room):
    """Robot moved while off: the saved pose misses and the map is searched."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    _save(node, room, *WRONG_POSE)
    result = node._execute(_handle())
    assert result.success and 'found by global search' in result.message
    assert 'saved pose matched only' in result.message
    calls = node.amcl.calls
    assert calls[0] == 'initialpose' and calls.index('global') < calls.index('nomotion')
    assert [goal.target_yaw for goal in node.spin.goals] == [pytest.approx(2 * math.pi)]
    assert result.pose.pose.pose.position.x == pytest.approx(TRUE_POSE[0])


def test_a_nearly_right_saved_pose_is_not_fitted_by_custom_code(monkeypatch, room):
    """Only AMCL may improve an imprecise starting pose."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    _save(node, room, TRUE_POSE[0] + 0.12, TRUE_POSE[1] - 0.1, TRUE_POSE[2] + 0.07)
    result = node._execute(_handle())
    assert result.success and 'found by global search' in result.message
    assert result.match_ratio > 0.95
    assert node.amcl.calls[0] == 'initialpose' and node.spin.goals
    position = result.pose.pose.pose.position
    assert math.hypot(position.x - TRUE_POSE[0], position.y - TRUE_POSE[1]) < 0.02
    covariance = node.publisher.publish.call_args.args[0].pose.covariance
    assert list(covariance) == _pose()['covariance']


def test_an_explicit_search_does_not_fit_or_rewind_to_the_previous_pose(monkeypatch, room):
    """Remove SWM25-239's custom fit and previous-pose score comparison."""
    node = _node(monkeypatch, room, found=WRONG_POSE)
    _ready(node, room)
    node.amcl.estimate(*TRUE_POSE)
    result = node._execute(_handle(Relocalize.Goal.GLOBAL_SEARCH))
    assert not result.success and result.match_ratio < 0.95
    assert 'global search matched only' in result.message
    assert 'returned to the previous pose' not in result.message
    assert node.amcl.calls.count('global') == 1
    assert result.pose.pose.pose.position.x == pytest.approx(WRONG_POSE[0])
    assert node.estimate[1]['x'] == pytest.approx(WRONG_POSE[0])


def test_a_pose_on_the_previous_map_is_not_returned_to(monkeypatch, room):
    node = _node(monkeypatch, room)
    node.amcl.estimate(*TRUE_POSE)
    _ready(node, room)
    assert node.estimate is None


def test_map_without_saved_pose_is_searched(monkeypatch, room):
    """A map AMCL never used and AutoSLAM did not save a pose for is searched."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    result = node._execute(_handle())
    assert result.success and result.message.startswith('no saved pose for this map')
    assert node.amcl.calls[0] == 'global' and node.amcl.calls[-1] == 'initialpose'


def test_global_search_skips_the_saved_pose(monkeypatch, room):
    """An explicit search ignores even a matching saved pose."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    _save(node, room, *TRUE_POSE)
    result = node._execute(_handle(Relocalize.Goal.GLOBAL_SEARCH))
    assert result.success and 'global' in node.amcl.calls


@pytest.mark.parametrize('cell', [-1, 100])
def test_global_search_rejects_a_map_without_known_free_cells(monkeypatch, room, cell):
    """Never enter AMCL's uniform sampler with an empty free-space vector."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    grid = room.grid()
    grid.data = [cell] * len(grid.data)
    node._map(grid)
    handle = _handle(Relocalize.Goal.GLOBAL_SEARCH)
    result = node._execute(handle)
    assert not result.success and 'no known free cells' in result.message
    handle.abort.assert_called_once()
    assert 'global' not in node.amcl.calls and not node.spin.goals


def test_given_pose_remains_available_on_the_unknown_default_map(monkeypatch, room):
    node = _node(monkeypatch, room)
    _ready(node, room)
    grid = room.grid()
    grid.data = [-1] * len(grid.data)
    node._map(grid)
    given = pose_message(_pose(*TRUE_POSE), Time(seconds=100).to_msg())
    result = node._execute(_handle(Relocalize.Goal.GIVEN_POSE, given))
    assert result.success and result.match_ratio == 0.
    assert node.amcl.calls == ['initialpose', 'initialpose'] and not node.spin.goals


def test_search_that_never_matches_stops_at_ten_seconds(monkeypatch, room):
    """An ambiguous or unmapped place is reported, not accepted."""
    node = _node(monkeypatch, room, found=WRONG_POSE)
    _ready(node, room)
    handle = _handle()
    result = node._execute(handle)
    handle.abort.assert_called_once()
    assert not result.success and 'global search matched only' in result.message
    assert node.amcl.calls.count('global') == 1 and len(node.spin.goals) == 1
    assert result.match_ratio < 0.5
    assert 10.0 <= node.clock.now - 100.0 < 10.1
    assert node.particle_limits == (500, 3000)


def test_search_continues_without_spin_when_it_is_unavailable(monkeypatch, room):
    """No behavior server yet: AMCL still updates in place."""
    node = _node(monkeypatch, room, spin=_Spin(ready=False))
    _ready(node, room)
    assert node._execute(_handle()).success
    node.get_logger().warning.assert_any_call(
        'Spin is unavailable; searching without rotating')


def test_given_pose_is_applied_and_its_match_is_reported(monkeypatch, room):
    """An operator pose uses the same map-frame contract as RViz."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    wrong = pose_message(_pose(*TRUE_POSE), Time(seconds=100).to_msg())
    wrong.header.frame_id = 'odom'
    result = node._execute(_handle(Relocalize.Goal.GIVEN_POSE, wrong))
    assert not result.success and 'map frame' in result.message
    given = pose_message(_pose(*TRUE_POSE), Time(seconds=100).to_msg())
    result = node._execute(_handle(Relocalize.Goal.GIVEN_POSE, given))
    assert result.success and result.match_ratio > 0.9
    assert node.amcl.calls == ['initialpose', 'initialpose'] and not node.spin.goals


@pytest.mark.parametrize('prepare, reason', [
    (lambda node, room: None, 'no saved map is selected'),
    (lambda node, room: (_ready(node, room), setattr(node, 'amcl_active', False)),
     'AMCL is not active'),
    (lambda node, room: _select(node, room.map_file),
     'the selected map was not received'),
    (lambda node, room: (_ready(node, room), setattr(node, 'scan', None)),
     'no recent LiDAR scan'),
])
def test_missing_inputs_fail_without_publishing(monkeypatch, room, prepare, reason):
    """Without a selected map, AMCL, its map or a scan, nothing is sent."""
    node = _node(monkeypatch, room)
    prepare(node, room)
    handle = _handle()
    result = node._execute(handle)
    handle.abort.assert_called_once()
    assert not result.success and reason in result.message
    assert node.amcl.calls == []


def test_only_the_selected_saved_map_is_matched(monkeypatch, room):
    """Never judge a pose against the SLAM map or the previous saved map."""
    node = _node(monkeypatch, room)
    _select(node, room.map_file)
    other = room.grid()
    other.info.width = 60
    node._map(other)
    assert node._selected_field() is None
    node._map(room.grid())
    assert node._selected_field() is not None
    _select(node, room.map_file, mode='MAPPING')
    assert node.map_file is None and node._selected_field() is None


def test_switching_selects_the_map_but_only_localization_saves(monkeypatch, room):
    """The manager finds the pose while switching; poses are saved afterwards."""
    node = _node(monkeypatch, room)
    _select(node, room.map_file, mode='SWITCHING')
    assert node.identity == map_identity(room.map_file) and not node.saving
    _select(node, room.map_file)
    assert node.saving


def test_unknown_method_and_cancel_do_not_publish(monkeypatch, room):
    """Invalid or canceled requests never touch AMCL."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    assert 'unknown relocalization method' in node._execute(_handle(method=7)).message
    handle = _handle(cancel=True)
    node._execute(handle)
    handle.canceled.assert_called_once()
    assert node.amcl.calls == []
    with pytest.raises(Canceled):
        node._check(_handle(cancel=True))


def test_acceptance_follows_the_requested_spread():
    """AMCL may correct within the requested uncertainty, with a minimum margin."""
    requested = _pose(covariance=[0.25 if i in (0, 7) else 0.0 for i in range(36)])
    assert accepts(_pose(x=2.4), requested)
    assert not accepts(_pose(x=2.6), requested)
    assert accepts(_pose(yaw=0.75), _pose())
    assert not accepts(_pose(yaw=1.3), _pose())


def test_busy_server_rejects_a_second_goal(monkeypatch, room):
    """One AMCL correction runs at a time."""
    node = _node(monkeypatch, room)
    assert node._goal(None).name == 'ACCEPT'
    assert node._goal(None).name == 'REJECT'


def test_only_fresh_amcl_estimates_after_corrections_are_saved(monkeypatch, room):
    """Avoid writing stale, inactive, pre-correction or non-map estimates."""
    node = _node(monkeypatch, room)
    _select(node, room.map_file)
    msg = pose_message(_pose(), Time(seconds=10).to_msg())
    node._receive(msg)
    assert node.latest is None
    msg.header.stamp = Time(seconds=100).to_msg()
    node.amcl_active = False
    node._receive(msg)
    assert node.latest is not None
    node.save_latest()
    assert not Path(node.path).exists()
    node.amcl_active = True
    node._receive(msg)
    assert valid_pose(node.latest)
    node.save_latest()
    assert Path(node.path).is_file()
    before = Path(node.path).stat().st_mtime_ns
    node.save_latest()
    assert Path(node.path).stat().st_mtime_ns == before
    node._initialpose(pose_message(_pose(), Time(seconds=100).to_msg()))
    assert node.latest is None
    msg.header.stamp = Time(seconds=99).to_msg()
    node._receive(msg)
    assert node.latest is None


def test_lost_state_reply_is_retried_and_absent_amcl_is_inactive(monkeypatch, room):
    """A lost GetState reply or a restarted AMCL cannot leave a stale ACTIVE state."""
    node = _node(monkeypatch, room)
    lost = Future()
    node.state_future = lost
    node._poll_amcl()
    node.client.call_async.assert_not_called()
    node.state_requested -= node.period
    replacement = Future()
    node.client.call_async.return_value = replacement
    node._poll_amcl()
    node.client.remove_pending_request.assert_called_once_with(lost)
    assert lost.cancelled() and not node.amcl_active
    replacement.set_result(SimpleNamespace(current_state=State(id=2)))
    node._poll_amcl()
    assert not node.amcl_active
    node.state_future = _done(SimpleNamespace(current_state=State(id=3)))
    node._poll_amcl()
    assert node.amcl_active
    node.client.service_is_ready.return_value = False
    node._poll_amcl()
    assert not node.amcl_active and node.state_future is None


def test_estimate_message_round_trip():
    """Stored poses and /initialpose messages keep position, heading and spread."""
    message = pose_message(_pose(), Time(seconds=1).to_msg())
    assert isinstance(message, PoseWithCovarianceStamped)
    assert pose_record(message)['yaw'] == pytest.approx(0.3)


class _LateSpin(_Spin):
    def __init__(self, ready_after):
        super().__init__()
        self.ready_after, self.waits = ready_after, 0

    def wait_for_server(self, timeout_sec):
        self.waits += 1
        return self.waits > self.ready_after


def test_search_waits_for_a_spin_server_that_comes_up_late(monkeypatch, room):
    """2026-10-09: right after a start the search gave up on Spin and did not turn."""
    node = _node(monkeypatch, room, spin=_LateSpin(ready_after=20))
    node.timeout = 60.0
    _ready(node, room)
    assert node._execute(_handle(Relocalize.Goal.GLOBAL_SEARCH)).success
    assert node.spin.waits == 21 and len(node.spin.goals) == 1
    node.get_logger().warning.assert_not_called()


def test_restore_uses_original_limits_and_amcls_actual_covariance(monkeypatch, room):
    node = _node(monkeypatch, room)
    node.particle_limits = (750, 3500)
    _ready(node, room)
    saved = _pose(*TRUE_POSE)
    saved['covariance'][0], saved['covariance'][7], saved['covariance'][35] = 0.07, 0.12, 0.04
    write_pose(node.path, map_identity(room.map_file), saved)
    original = node.amcl.initial_pose

    def keep_uncertainty(message):
        original(message)
        node.estimate[1]['covariance'] = list(message.pose.covariance)

    node.publisher.publish.side_effect = keep_uncertainty
    assert node._execute(_handle()).success
    assert node.parameter_calls == [(2000, 8000), (750, 3500)]
    assert node.particle_limits == (750, 3500)
    assert list(node.publisher.publish.call_args.args[0].pose.covariance) == saved['covariance']
    assert node.estimate[1]['x'] == pytest.approx(TRUE_POSE[0])


def test_parameter_failure_does_not_leave_boosted_limits(monkeypatch, room):
    node = _node(monkeypatch, room)
    _ready(node, room)
    _save(node, room, *TRUE_POSE)
    normal = node._particle_writer.call_async

    def reject_boost(request):
        if request.parameters[0].value.integer_value == 2000:
            return _done(SimpleNamespace(result=SimpleNamespace(successful=False, reason='no')))
        return normal(request)

    node._particle_writer.call_async = reject_boost
    result = node._execute(_handle())
    assert not result.success and 'rejected particle limits' in result.message
    assert node.particle_limits == (500, 3000)
    assert node.estimate[1]['x'] == pytest.approx(TRUE_POSE[0])
    assert not node.spin.goals


@pytest.mark.parametrize('cancel', [False, True])
def test_live_spin_stops_on_threshold_or_cancellation_before_parameter_restore(
        monkeypatch, room, cancel):
    node = _node(monkeypatch, room)
    _ready(node, room)
    handle = _handle(Relocalize.Goal.GLOBAL_SEARCH)
    result_future = Future()
    child = SimpleNamespace(accepted=True, get_result_async=lambda: result_future)
    events = []

    def stop():
        events.append('spin stopped')
        result_future.set_result(SimpleNamespace())
        return _done(SimpleNamespace())

    child.cancel_goal_async = stop
    node.spin.send_goal_async = lambda goal: _done(child)
    original_sleep = relocalization_node.time.sleep

    def advance(seconds):
        original_sleep(seconds)
        if not result_future.done():
            if cancel:
                handle.is_cancel_requested = True
            else:
                node.amcl.estimate(*TRUE_POSE)

    monkeypatch.setattr(relocalization_node.time, 'sleep', advance)
    original_set = node._particle_writer.call_async

    def set_limits(request):
        if request.parameters[0].value.integer_value == 500:
            events.append('limits restored')
            assert result_future.done()
        return original_set(request)

    node._particle_writer.call_async = set_limits
    result = node._execute(handle)
    assert events == ['spin stopped', 'limits restored']
    assert node.particle_limits == (500, 3000)
    assert node.clock.now - 100.0 < 1.0
    if cancel:
        handle.canceled.assert_called_once()
    else:
        assert result.success and result.match_ratio >= 0.95


def test_missing_parameter_response_is_bounded(monkeypatch, room):
    node = _node(monkeypatch, room)
    _ready(node, room)
    node._particle_reader.call_async = lambda _: Future()
    result = node._execute(_handle())
    assert not result.success and 'read particle limits' in result.message
    assert not node.parameter_calls and not node.spin.goals
    assert 10 <= node.clock.now - 100 < 10.1


def test_late_spin_acceptance_is_canceled_and_settled_before_restore(monkeypatch, room):
    node = _node(monkeypatch, room)
    _ready(node, room)
    handle = _handle(Relocalize.Goal.GLOBAL_SEARCH)
    accepted, stopped = Future(), Future()
    child = SimpleNamespace(accepted=True, get_result_async=lambda: stopped)
    events = []

    def stop():
        events.append('spin stopped')
        stopped.set_result(SimpleNamespace())
        return _done(SimpleNamespace())

    child.cancel_goal_async = stop
    node.spin.send_goal_async = lambda _: accepted
    normal_sleep = relocalization_node.time.sleep

    def advance(seconds):
        normal_sleep(seconds)
        handle.is_cancel_requested = True
        if node.clock.now >= 100.15 and not accepted.done():
            accepted.set_result(child)

    monkeypatch.setattr(relocalization_node.time, 'sleep', advance)
    normal_set = node._particle_writer.call_async

    def set_limits(request):
        if request.parameters[0].value.integer_value == 500:
            assert stopped.done()
            events.append('limits restored')
        return normal_set(request)

    node._particle_writer.call_async = set_limits
    node._execute(handle)
    handle.canceled.assert_called_once()
    assert events == ['spin stopped', 'limits restored']
    assert node.particle_limits == (500, 3000)
