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
from std_msgs.msg import String

TRUE_POSE = (0.8, 0.4, 0.3)
WRONG_POSE = (-0.9, -0.5, 2.0)


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
    node.settings = {'base_frame': 'base_footprint', 'match_distance_m': 0.15,
                     'match_ratio_min': 0.5, 'max_scan_range_m': 8.0,
                     'search_attempts': 2}
    node.path = room.map_file.parent / 'last_pose.yaml'
    node.period, node.timeout = 5.0, 3.0
    node.lock = threading.Lock()
    node.map_file = node.identity = node.expected_map = None
    node.saving = False
    node.map_message = node.field = node.scan = None
    node.latest = None
    node.received = node.saved_received = 0.0
    node.initial_stamp = 0
    node.estimate = None
    node.busy = False
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
    assert node.amcl.calls == ['initialpose'] and not node.spin.goals
    assert not node.busy


def test_moved_robot_is_found_by_global_search(monkeypatch, room):
    """Robot moved while off: the saved pose misses and the map is searched."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    _save(node, room, *WRONG_POSE)
    result = node._execute(_handle())
    assert result.success and 'found by global search' in result.message
    assert 'saved pose matched only' in result.message
    assert node.amcl.calls[:2] == ['initialpose', 'global']
    assert [goal.target_yaw for goal in node.spin.goals] == [pytest.approx(2 * math.pi)]
    assert result.pose.pose.pose.position.x == pytest.approx(TRUE_POSE[0])


def test_map_without_saved_pose_is_searched(monkeypatch, room):
    """A map AMCL never used and AutoSLAM did not save a pose for is searched."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    result = node._execute(_handle())
    assert result.success and result.message.startswith('no saved pose for this map')
    assert 'initialpose' not in node.amcl.calls and node.amcl.calls[0] == 'global'


def test_global_search_skips_the_saved_pose(monkeypatch, room):
    """An explicit search ignores even a matching saved pose."""
    node = _node(monkeypatch, room)
    _ready(node, room)
    _save(node, room, *TRUE_POSE)
    result = node._execute(_handle(Relocalize.Goal.GLOBAL_SEARCH))
    assert result.success and node.amcl.calls[0] == 'global'


def test_search_that_never_matches_fails_after_the_attempts(monkeypatch, room):
    """An ambiguous or unmapped place is reported, not accepted."""
    node = _node(monkeypatch, room, found=WRONG_POSE)
    _ready(node, room)
    handle = _handle()
    result = node._execute(handle)
    handle.abort.assert_called_once()
    assert not result.success and 'global search matched only' in result.message
    assert node.amcl.calls.count('global') == 2 and len(node.spin.goals) == 2
    assert result.match_ratio < 0.5


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
    assert node.amcl.calls == ['initialpose'] and not node.spin.goals


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
