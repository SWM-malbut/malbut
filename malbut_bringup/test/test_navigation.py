"""Destination sending from the web map on the real robot (SWM25-237), without ROS."""

import json
import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from malbut_bringup import cloud_sync
from malbut_bringup.cloud_sync import CloudSync, space_documents, state_payload
from malbut_bringup.navigation import (
    distance_to_geometry, escape_point, FAILURES, NavigationError, Navigator, path_summary,
    point_in_geometry, resolve_goal,
)
from malbut_bringup.zone_override import ZoneOverride
from malbut_bringup.web_panel import PanelData, validate_command
from malbut_bringup.web_runtime import SavedMapCatalog
from malbut_bringup.zones import write_zones, zone_feature

from test_space_files import _house


PREVIEW_ID = '7f4b5ec0-2a1b-4c3d-8e9f-0a1b2c3d4e5f'
START_ID = '8f4b5ec0-2a1b-4c3d-8e9f-0a1b2c3d4e5f'
CANCEL_ID = '9f4b5ec0-2a1b-4c3d-8e9f-0a1b2c3d4e5f'
REQUEST_ID = 'a' * 32
POSE = {'x': 0.0, 'y': 0.0, 'yaw': 0.0}


def _square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


FLOOR = {'type': 'Polygon', 'coordinates': [_square(0, 0, 4, 3), _square(1, 1, 1.4, 1.4)]}
BLOCKED = {'type': 'Polygon', 'coordinates': [_square(3, 0, 4, 1)]}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _straight(x, y, _yaw):
    return [(0.0, 0.0), (x / 2, y / 2), (x, y)]


def test_points_inside_floor_but_not_in_holes_or_other_polygons():
    assert point_in_geometry((0.5, 0.5), FLOOR)
    assert not point_in_geometry((1.2, 1.2), FLOOR), 'a hole is not floor'
    assert not point_in_geometry((5, 5), FLOOR)
    many = {'type': 'MultiPolygon', 'coordinates': [[_square(0, 0, 1, 1)], [_square(2, 2, 3, 3)]]}
    assert point_in_geometry((2.5, 2.5), many) and not point_in_geometry((1.5, 1.5), many)
    assert not point_in_geometry((0.5, 0.5), {'type': 'LineString', 'coordinates': []})


def test_a_point_just_off_the_floor_moves_onto_it_and_no_entry_is_refused():
    assert resolve_goal((2.0, 2.0), [FLOOR], [BLOCKED]) == ((2.0, 2.0), 0.0)
    goal, moved = resolve_goal((2.0, 3.2), [FLOOR], [BLOCKED])
    assert point_in_geometry(goal, FLOOR) and 0.2 <= moved <= 0.25
    with pytest.raises(NavigationError, match='바닥'):
        resolve_goal((2.0, 4.0), [FLOOR], [BLOCKED])
    with pytest.raises(NavigationError, match='진입 금지'):
        resolve_goal((3.5, 0.5), [FLOOR], [BLOCKED])
    # Off the floor beside a no-entry Zone: the nearest allowed floor, never the Zone.
    goal, _moved = resolve_goal((3.2, -0.2), [FLOOR], [BLOCKED])
    assert point_in_geometry(goal, FLOOR) and not point_in_geometry(goal, BLOCKED)


def test_a_goal_in_the_no_entry_margin_moves_out_so_the_parked_body_stays_clear():
    # BLOCKED spans x 3..4, y 0..1; 0.1 m left of it is in the 0.2 m margin.
    goal, moved = resolve_goal((2.9, 0.5), [FLOOR], [BLOCKED])
    assert distance_to_geometry(goal, BLOCKED) >= 0.2 - 1e-9
    assert 0.09 <= moved <= 0.2  # Snapping searches 5 cm rings.
    assert resolve_goal((2.7, 0.5), [FLOOR], [BLOCKED]) == ((2.7, 0.5), 0.0)
    assert resolve_goal((2.9, 0.5), [FLOOR], [BLOCKED], margin=0.0) == ((2.9, 0.5), 0.0)
    assert distance_to_geometry((2.0, 0.5), BLOCKED) == pytest.approx(1.0)
    assert distance_to_geometry((3.5, 0.5), BLOCKED) == pytest.approx(0.5)  # inside: to its edge


def test_paths_are_thinned_to_ten_centimetres_and_keep_both_ends():
    points = [(index / 100, 0.0) for index in range(301)]
    summary = path_summary(points)
    assert summary['length_m'] == 3.0
    assert summary['points'][0] == [0.0, 0.0] and summary['points'][-1] == [3.0, 0.0]
    assert 30 <= len(summary['points']) <= 32
    long = path_summary([(index / 100, 0.0) for index in range(5001)])
    assert len(long['points']) <= 152


def test_preview_plans_to_the_goal_and_faces_along_the_path():
    navigator = Navigator(Clock())
    plan = Mock(side_effect=_straight)
    preview = navigator.preview(2.0, 3.2, pose=POSE, map_key='home:1', floor=[FLOOR],
                                blocked=[BLOCKED], plan=plan, busy=False)
    assert preview['expires_in_s'] == 30 and len(preview['preview_token']) >= 32
    assert preview['snapped'] and preview['snap_distance_m'] > 0
    assert preview['requested'] == {'x': 2.0, 'y': 3.2}
    resolved = preview['resolved']
    assert plan.call_args.args[:2] == pytest.approx((resolved['x'], resolved['y']), abs=1e-4)
    assert resolved['yaw'] == pytest.approx(math.atan2(resolved['y'], resolved['x']), abs=1e-4)
    assert preview['path']['points'][-1] == pytest.approx([resolved['x'], resolved['y']])
    assert json.dumps(preview, ensure_ascii=False)


@pytest.mark.parametrize('pose, busy, plan, message', [
    (None, False, _straight, '위치'),
    (POSE, True, _straight, '이동 중'),
    (POSE, False, Mock(side_effect=ValueError('Nav2 planner found no path')), '길'),
    (POSE, False, Mock(side_effect=TimeoutError()), '길'),
    (POSE, False, lambda x, y, yaw: [(0.0, 0.0), (x - 1.0, y)], '길'),
    (POSE, False, lambda x, y, yaw: [], '길'),
])
def test_preview_refusals_are_for_people(pose, busy, plan, message):
    with pytest.raises(NavigationError, match=message):
        Navigator(Clock()).preview(2.0, 2.0, pose=pose, map_key='home:1', floor=[FLOOR],
                                   blocked=[], plan=plan, busy=busy)


def test_a_preview_starts_once_within_30_seconds_on_the_same_map():
    clock = Clock()
    navigator = Navigator(clock)

    def preview():
        return navigator.preview(2.0, 2.0, pose=POSE, map_key='home:1', floor=[FLOOR],
                                 blocked=[], plan=_straight, busy=False)['preview_token']

    submit = Mock(return_value=REQUEST_ID)
    token = preview()
    started = navigator.start(token, map_key='home:1', busy=False, submit=submit)
    assert started['session_id'] == REQUEST_ID and started['state'] == 'driving'
    assert submit.call_args.args[0] == {'x': 2.0, 'y': 2.0, 'yaw': pytest.approx(0.785, abs=1e-3)}
    with pytest.raises(NavigationError, match='시간'):
        navigator.start(token, map_key='home:1', busy=False, submit=submit)
    late = preview()
    clock.now += 31
    with pytest.raises(NavigationError, match='시간'):
        navigator.start(late, map_key='home:1', busy=False, submit=submit)
    with pytest.raises(NavigationError, match='지도'):
        navigator.start(preview(), map_key='home:2', busy=False, submit=submit)
    with pytest.raises(NavigationError, match='이동 중'):
        navigator.start(preview(), map_key='home:1', busy=True, submit=submit)
    assert submit.call_count == 1


def test_progress_comes_from_nav2_feedback_and_cancel_stops_only_this_drive():
    navigator = Navigator(Clock())
    assert navigator.target([]) == {}
    token = navigator.preview(3.0, 0.5, pose=POSE, map_key='home:1', floor=[FLOOR],
                              blocked=[], plan=lambda x, y, yaw: [(0.0, 0.5), (x, y)],
                              busy=False)['preview_token']
    navigator.start(token, map_key='home:1', busy=False, submit=lambda goal: REQUEST_ID)
    feedback = yaml.safe_dump({'distance_remaining': 1.0,
                               'estimated_time_remaining': {'sec': 4, 'nanosec': 500000000}})
    request = {'id': REQUEST_ID, 'capability': 'navigate_to_pose', 'state': 'RUNNING',
               'feedback': {'state': 'RUNNING', 'feedback_yaml': feedback}, 'message': ''}
    target = navigator.target([request])
    assert target['state'] == 'driving' and target['session_id'] == REQUEST_ID
    assert target['distance_remaining_m'] == 1.0 and target['estimated_time_remaining_s'] == 4.5
    assert target['initial_path_length_m'] == 3.0
    assert target['progress_ratio'] == pytest.approx(0.667, abs=1e-3)
    assert navigator.busy([request])

    with pytest.raises(NavigationError):
        navigator.cancel('b' * 32, Mock())
    cancel = Mock()
    assert navigator.cancel(REQUEST_ID, cancel) == {'session_id': REQUEST_ID, 'state': 'canceling'}
    cancel.assert_called_once_with(REQUEST_ID)

    for state, shown in [('CANCELING', 'canceling'), ('CANCELED', 'canceled'),
                         ('ABORTED', 'failed'), ('SUCCEEDED', 'succeeded')]:
        assert navigator.target([{**request, 'state': state}])['state'] == shown
    assert navigator.target([{**request, 'state': 'SUCCEEDED'}])['progress_ratio'] == 1.0
    # Older requests leave the bridge's history; the last state stays.
    assert navigator.target([])['state'] == 'succeeded'
    assert not navigator.busy([{**request, 'state': 'SUCCEEDED'}])


def test_only_one_named_goal_can_be_canceled_through_the_bridge():
    assert validate_command({'command': 'cancel', 'request_id': REQUEST_ID})
    for payload in ({'command': 'cancel', 'request_id': '../x'},
                    {'command': 'cancel', 'request_id': REQUEST_ID, 'extra': 1},
                    {'command': 'cancel', 'request_id': 7}):
        with pytest.raises(ValueError):
            validate_command(payload)


@pytest.fixture
def robot(tmp_path):
    cloud_sync._SPACE_CACHE.clear()
    path = _house(tmp_path)
    runtime = {'mode': 'navigation', 'map': str(path),
               'localization': {'mode': 'LOCALIZATION', 'map': str(path)}}
    data = PanelData()
    data.runtime = {**data.runtime, **runtime}
    data.map_snapshot = lambda: {'available': True, 'active': True, 'pose': dict(POSE)}
    bridge = SimpleNamespace(data=data, catalog=SavedMapCatalog(tmp_path), node=Mock(),
                             submit=Mock(return_value=REQUEST_ID),
                             plan_path=Mock(side_effect=_straight))
    return CloudSync(bridge, Mock()), bridge, path


def test_the_web_map_sends_the_real_robot_through_preview_start_and_cancel(robot):
    sync, bridge, path = robot
    write_zones(path, [zone_feature('restricted', [[1.5, 0.5], [2.0, 0.5], [2.0, 1.0]])])
    sync.dispatch({'id': PREVIEW_ID, 'operation': 'navigation_preview',
                   'payload': {'x': 1.0, 'y': 1.0}})
    receipt = sync.pending[PREVIEW_ID]
    assert receipt['ok'], receipt
    preview = receipt['result']
    assert preview['resolved']['x'] == 1.0 and preview['path']['length_m'] > 1.4

    sync.dispatch({'id': START_ID, 'operation': 'navigation_start',
                   'payload': {'previewToken': preview['preview_token']}})
    assert sync.pending[START_ID] == {'ok': True, 'result': {
        'session_id': REQUEST_ID, 'state': 'driving', 'goal': preview['resolved'],
        'path': preview['path']}}
    bridge.submit.assert_called_once_with({
        'command': 'start', 'capability': 'navigate_to_pose', 'arguments': preview['resolved']})
    validate_command(bridge.submit.call_args.args[0])

    # The state upload carries the drive in the map screen's fields beside the developer ones.
    snapshot = bridge.data.snapshot()
    snapshot['requests'] = [{'id': REQUEST_ID, 'capability': 'navigate_to_pose',
                             'state': 'RUNNING', 'route': 'manager', 'feedback': {},
                             'message': ''}]
    upload = state_payload(snapshot, bridge.data.map_snapshot(), [],
                           navigation=sync.navigator.target(snapshot['requests']))
    assert upload['target']['state'] == 'driving'
    assert upload['target']['session_id'] == REQUEST_ID
    assert upload['target']['requests'][0]['id'] == REQUEST_ID
    assert len(json.dumps(upload).encode()) < 64 * 1024

    sync.dispatch({'id': CANCEL_ID, 'operation': 'navigation_cancel',
                   'payload': {'sessionId': REQUEST_ID}})
    assert sync.pending[CANCEL_ID]['ok']
    assert bridge.submit.call_args.args[0] == {'command': 'cancel', 'request_id': REQUEST_ID}


def test_no_entry_zones_and_other_modes_refuse_before_planning(robot):
    sync, bridge, path = robot
    write_zones(path, [zone_feature('restricted', [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5],
                                                   [0.5, 1.5]])])
    sync.dispatch({'id': PREVIEW_ID, 'operation': 'navigation_preview',
                   'payload': {'x': 1.0, 'y': 1.0}})
    assert not sync.pending[PREVIEW_ID]['ok']
    assert '진입 금지' in sync.pending[PREVIEW_ID]['result']['error']
    bridge.plan_path.assert_not_called()

    bridge.data.runtime = {**bridge.data.runtime, 'mode': 'mapping',
                           'localization': {'mode': 'MAPPING'}}
    sync.dispatch({'id': START_ID, 'operation': 'navigation_preview',
                   'payload': {'x': 2.0, 'y': 1.0}})
    assert '저장된 지도로 주행 중' in sync.pending[START_ID]['result']['error']
    sync.dispatch({'id': CANCEL_ID, 'operation': 'navigation_preview',
                   'payload': {'x': 2.0, 'y': True}})
    assert not sync.pending[CANCEL_ID]['ok']
    bridge.submit.assert_not_called()


def test_space_documents_floor_is_what_the_preview_checks(robot):
    """The User Map built from the saved map is the floor the preview uses."""
    sync, bridge, _path = robot
    user_map, _zones, _revision = space_documents(bridge.data.runtime, bridge.catalog)
    floor = [feature['geometry'] for feature in user_map['features']
             if feature['properties']['role'] == 'walkable_area']
    assert floor and point_in_geometry((1.0, 1.0), floor[0])
    assert not point_in_geometry((5.0, 5.0), floor[0])
    sync.dispatch({'id': PREVIEW_ID, 'operation': 'navigation_preview',
                   'payload': {'x': 5.0, 'y': 5.0}})
    assert '바닥' in sync.pending[PREVIEW_ID]['result']['error']


def test_a_failed_plan_keeps_the_planners_reason_in_the_robot_log(robot):
    """The owner reads a plain reason; the robot log says why Nav2 refused."""
    sync, bridge, _path = robot
    bridge.plan_path.side_effect = ValueError('Nav2 planner found no path')
    sync.dispatch({'id': PREVIEW_ID, 'operation': 'navigation_preview',
                   'payload': {'x': 1.0, 'y': 1.0}})
    assert sync.pending[PREVIEW_ID]['result']['error'] == '그곳까지 가는 길을 찾지 못했어요.'
    bridge.node.get_logger().warning.assert_called_with(
        'Destination preview: Nav2 planner found no path')


# ---------------------------------------------------------------- 금지 구역에서 빠져나오기 · 실패 이유

ZONE = {'type': 'Polygon', 'coordinates': [_square(1.8, 1.0, 2.2, 1.4)]}


def test_an_escape_is_needed_only_when_the_body_touches_a_no_entry_zone():
    assert escape_point((1.0, 1.2), [FLOOR], [ZONE]) is None, 'clear of the Zone'
    for inside_or_touching in ((2.0, 1.2), (1.7, 1.2)):  # Inside; 0.1 m from its edge.
        exit_point = escape_point(inside_or_touching, [FLOOR], [ZONE])
        assert exit_point and not point_in_geometry(exit_point, ZONE)
        assert distance_to_geometry(exit_point, ZONE) >= 0.2 - 1e-9
        assert math.dist(exit_point, inside_or_touching) <= 1.0
    # Floor all around is inside the Zone's margin: it cannot get out by itself.
    boxed = {'type': 'Polygon', 'coordinates': [_square(0, 0, 4, 3)]}
    assert escape_point((2.0, 1.2), [FLOOR], [boxed]) is False


class Zones:
    def __init__(self):
        self.calls = []

    def __call__(self, on):
        self.calls.append(on)


def _escape_session(clock, escape=(1.5, 1.2)):
    navigator = Navigator(clock)
    token = navigator.preview(3.0, 0.5, pose=POSE, map_key='home:1', floor=[FLOOR],
                              blocked=[], plan=lambda x, y, yaw: [(0.0, 0.5), (x, y)],
                              busy=False)['preview_token']
    submit, zones = Mock(side_effect=['e' * 32, REQUEST_ID]), Zones()
    started = navigator.start(token, map_key='home:1', busy=False, submit=submit,
                              escape=escape, zones=zones)
    return navigator, submit, zones, started


def _request(request_id, state):
    return {'id': request_id, 'capability': 'navigate_to_pose', 'state': state,
            'feedback': {}, 'message': ''}


def test_the_robot_drives_out_of_the_zone_first_then_to_the_destination():
    clock = Clock()
    navigator, submit, zones, started = _escape_session(clock)
    session = started['session_id']
    assert started['state'] == 'escaping' and zones.calls == [False]
    assert navigator.busy([]), 'no second destination while it escapes'
    navigator.advance([])
    submit.assert_not_called()  # Waits for the costmaps to drop the Zones.
    clock.now += 2.5
    navigator.advance([])
    assert submit.call_args.args[0] == {'x': 1.5, 'y': 1.2, 'yaw': started['goal']['yaw']}
    assert navigator.target([_request('e' * 32, 'RUNNING')])['state'] == 'escaping'
    navigator.advance([_request('e' * 32, 'SUCCEEDED')])
    assert zones.calls == [False, True] and submit.call_count == 1
    clock.now += 2.5
    navigator.advance([])
    assert submit.call_args.args[0] == started['goal']
    target = navigator.target([_request(REQUEST_ID, 'RUNNING')])
    assert target['state'] == 'driving' and target['session_id'] == session
    cancel = Mock()
    assert navigator.cancel(session, cancel)['state'] == 'canceling'
    cancel.assert_called_once_with(REQUEST_ID)


def test_an_escape_that_cannot_finish_restores_the_zones_and_names_the_reason():
    clock = Clock()
    navigator, submit, zones, _started = _escape_session(clock)
    clock.now += 2.5
    navigator.advance([])
    navigator.advance([_request('e' * 32, 'ABORTED')])
    target = navigator.target([])
    assert (target['state'], target['reason']) == ('failed', 'zone_stuck')
    assert zones.calls == [False, True] and submit.call_count == 1
    # No floor to drive out to: it fails at once, without moving.
    navigator, submit, zones, started = _escape_session(Clock(), escape=False)
    assert (started['state'], navigator.target([])['reason']) == ('failed', 'zone_stuck')
    submit.assert_not_called()
    assert zones.calls == []


def test_canceling_between_steps_restores_the_zones_without_a_manager_request():
    navigator, submit, zones, started = _escape_session(Clock())
    assert navigator.cancel(started['session_id'], Mock()) == {
        'session_id': started['session_id'], 'state': 'canceled'}
    assert zones.calls == [False, True]
    navigator.advance([])
    submit.assert_not_called()
    assert navigator.target([])['state'] == 'canceled'


def _feedback_at(x, y):
    return {'state': 'RUNNING', 'feedback_yaml': yaml.safe_dump(
        {'current_pose': {'pose': {'position': {'x': x, 'y': y, 'z': 0.0}}},
         'distance_remaining': 1.2})}


@pytest.mark.parametrize('request_fields, system, reason', [
    ({'state': 'ABORTED', 'feedback': _feedback_at(0.05, 0.5)}, None, 'blocked_start'),
    ({'state': 'ABORTED', 'feedback': _feedback_at(1.5, 0.5)}, None, 'blocked_way'),
    ({'state': 'ABORTED', 'feedback': {}}, None, 'blocked_start'),
    ({'state': 'ABORTED', 'result': {
        'message': 'conflicting mission abc has higher priority HIGH'}}, None, 'manual_drive'),
    ({'state': 'ABORTED', 'result': {
        'message': 'conflicting mission abc has higher priority URGENT'}}, None, 'fall_check'),
    ({'state': 'ABORTED', 'result': {'message': 'mission preempted by a replacement request'}},
     {'active_foreground_missions': [{'capability_id': 'fall_confirmation'}]},
     'fall_check_started'),
    ({'state': 'ABORTED', 'result': {'message': 'mission preempted by a replacement request'}},
     {'active_foreground_missions': [{'capability_id': 'follow_person'}]}, 'unknown'),
    ({'state': 'ERROR', 'message': 'Navigation requires a live map and current robot pose'},
     None, 'localization_lost'),
])
def test_a_failed_drive_names_why_for_the_web_map(request_fields, system, reason):
    navigator = Navigator(Clock())
    token = navigator.preview(3.0, 0.5, pose=POSE, map_key='home:1', floor=[FLOOR],
                              blocked=[], plan=lambda x, y, yaw: [(0.0, 0.5), (x, y)],
                              busy=False)['preview_token']
    navigator.start(token, map_key='home:1', busy=False, submit=lambda goal: REQUEST_ID)
    request = {'id': REQUEST_ID, 'capability': 'navigate_to_pose', 'message': '',
               **request_fields}
    target = navigator.target([request], system)
    assert (target['state'], target['reason']) == ('failed', reason)
    assert reason in FAILURES
    # The reason stays with the drive after its request leaves the bridge's history.
    assert navigator.target([])['reason'] == reason


def test_a_robot_standing_in_a_zone_escapes_before_the_drive_it_was_sent(robot):
    sync, bridge, path = robot
    write_zones(path, [zone_feature('restricted', [[0.8, 0.8], [1.2, 0.8], [1.2, 1.2],
                                                   [0.8, 1.2]])])
    bridge.data.map_snapshot = lambda: {'available': True, 'active': True,
                                        'pose': {'x': 1.0, 'y': 1.0, 'yaw': 0.0}}
    bridge.zone_override = ZoneOverride()
    sync.dispatch({'id': PREVIEW_ID, 'operation': 'navigation_preview',
                   'payload': {'x': 2.0, 'y': 1.0}})
    token = sync.pending[PREVIEW_ID]['result']['preview_token']
    sync.dispatch({'id': START_ID, 'operation': 'navigation_start',
                   'payload': {'previewToken': token}})
    assert sync.pending[START_ID]['result']['state'] == 'escaping'
    assert bridge.zone_override.escaping()
    bridge.submit.assert_not_called()


def test_a_patrol_sent_while_standing_in_a_zone_starts_after_the_escape(robot):
    sync, bridge, path = robot
    write_zones(path, [zone_feature('restricted', [[0.8, 0.8], [1.2, 0.8], [1.2, 1.2],
                                                   [0.8, 1.2]])])
    bridge.data.map_snapshot = lambda: {'available': True, 'active': True,
                                        'pose': {'x': 1.0, 'y': 1.0, 'yaw': 0.0}}
    bridge.zone_override = ZoneOverride()
    clock = Clock()
    sync.navigator.clock = clock
    sync.dispatch({'id': START_ID, 'operation': 'drive_mode_start',
                   'payload': {'mode': 'patrol', 'thoroughness': 1}})
    assert sync.pending[START_ID]['result']['status'] == 'escaping'
    bridge.submit.assert_not_called()
    clock.now += 2.5
    sync.navigator.advance([])
    assert bridge.submit.call_args.args[0]['capability'] == 'navigate_to_pose'
    sync.navigator.advance([_request(REQUEST_ID, 'SUCCEEDED')])
    assert not bridge.zone_override.escaping()
    clock.now += 2.5
    sync.navigator.advance([])
    assert bridge.submit.call_args.args[0] == {
        'command': 'start', 'capability': 'patrol', 'arguments': {'thoroughness': 1}}
    assert sync.navigator.target([])['state'] == 'handed_over'
    # Clear of every Zone: the patrol starts at once, as before.
    bridge.data.map_snapshot = lambda: {'available': True, 'active': True,
                                        'pose': {'x': 2.0, 'y': 1.0, 'yaw': 0.0}}
    sync.dispatch({'id': CANCEL_ID, 'operation': 'drive_mode_start',
                   'payload': {'mode': 'patrol'}})
    assert sync.pending[CANCEL_ID]['result']['status'] == 'queued'
