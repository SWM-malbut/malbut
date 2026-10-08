"""Room patrol and person following on the web map's 자율주행 card, without ROS."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_bringup.cloud_sync import CloudSync, panel_command, state_payload
from malbut_bringup.drive_mode import last_patrol, robot_drive_mode
from malbut_bringup.web_panel import PanelData, validate_command


MISSION_ID = '0f4b5ec0-2a1b-4c3d-8e9f-0a1b2c3d4e5f'
COMMAND_ID = '7f4b5ec0-2a1b-4c3d-8e9f-0a1b2c3d4e5f'


def _system(state=1, capability='patrol', where='active_foreground_missions'):
    return {where: [{'mission_id': MISSION_ID, 'capability_id': capability, 'mode': 0,
                     'priority': 0, 'state': state}]}


def _status(state='observing', detail='', **extra):
    return {'state': state, 'detail': detail, 'coverage_ratio': 0.45, 'viewpoints_visited': 3,
            'unvisited_rooms': ['침실', '주방'], 'inaccessible_rooms': [], **extra}


def test_a_running_patrol_is_the_session_whoever_started_it():
    mode = robot_drive_mode(_system(), _status(), None, ready=True, can_follow=False)
    assert (mode['mode'], mode['state'], mode['sessionId']) == ('patrol', 'active', MISSION_ID)
    detail = mode['detail']
    assert detail['patrol_phase'] == 'observing' and detail['coverage_ratio'] == 0.45
    assert detail['viewpoints_visited'] == 3 and detail['unvisited_rooms'] == ['침실', '주방']
    assert detail['can_pause'] is False and detail['thoroughness_levels'] == [0, 1, 2]
    assert detail['available_modes'] == [], 'no second patrol while one runs'


@pytest.mark.parametrize('mission_state, phase, expected', [
    (0, 'completed', 'starting'),   # Queued; the status still shows the last patrol.
    (1, 'completed', 'starting'),   # Accepted, before patrol publishes its first stage.
    (1, 'planning', 'active'),
    (2, 'navigating', 'stopping'),
    (1, 'stopping', 'stopping'),
    (3, 'navigating', 'active'),    # Suspended by the manager for another mission.
])
def test_patrol_states_follow_the_manager_and_the_patrol_stage(mission_state, phase, expected):
    mode = robot_drive_mode(_system(mission_state), _status(phase), None,
                            ready=True, can_follow=False)
    assert mode['state'] == expected
    if expected == 'starting':
        assert mode['detail']['coverage_ratio'] == 0.0 and mode['detail']['unvisited_rooms'] == []


@pytest.mark.parametrize('status, outcome', [
    (_status('completed', 'Requested coverage and reachable room visits completed'), 'done'),
    (_status('aborted', 'No usable untried viewpoint remains; partial coverage returned',
             inaccessible_rooms=['창고']), 'partial'),
    (_status('aborted', 'Saved map changed; start a new patrol on that map'), 'failed'),
    (_status('idle', 'Patrol canceled'), 'stopped'),
])
def test_the_last_result_stays_until_the_next_patrol(status, outcome):
    mode = robot_drive_mode({}, status, None, ready=True, can_follow=False)
    assert (mode['mode'], mode['state'], mode['sessionId']) == ('idle', 'idle', None)
    result = mode['detail']['last_patrol']
    assert result['outcome'] == outcome and result['coverage_ratio'] == 0.45
    assert result['viewpoints_visited'] == 3
    if outcome == 'partial':
        assert result['inaccessible_rooms'] == ['창고']
    assert mode['detail']['available_modes'] == ['patrol']


def test_the_rooms_a_patrol_used_reach_the_web_so_it_can_count_them():
    running = robot_drive_mode(_system(), _status(room_count=4), None,
                               ready=True, can_follow=False)
    assert running['detail']['room_count'] == 4
    done = robot_drive_mode({}, _status('completed', 'done', room_count=4, unvisited_rooms=[]),
                            None, ready=True, can_follow=False)['detail']['last_patrol']
    assert done['room_count'] == 4 and done['unvisited_rooms'] == []
    stopped = last_patrol(_status('idle', 'Patrol canceled', room_count=4))
    assert stopped['unvisited_rooms'] == ['침실', '주방']
    assert last_patrol(_status('completed', 'done', room_count=0))['room_count'] == 0
    # An older patrol node sends no count; a bad one is dropped, not guessed.
    assert 'room_count' not in last_patrol(_status('completed', 'done'))
    assert 'room_count' not in last_patrol(_status('completed', 'done', room_count='4'))


def test_patrol_is_offered_only_when_it_can_run():
    waiting = _status('idle', 'Waiting for a patrol goal')
    assert last_patrol(waiting) is None, 'a fresh start has no result'
    assert robot_drive_mode({}, waiting, None, ready=True, can_follow=False)['detail'][
        'available_modes'] == ['patrol']
    assert robot_drive_mode({}, waiting, None, ready=False, can_follow=False)['detail'][
        'available_modes'] == []
    assert robot_drive_mode({}, None, None, ready=True, can_follow=False)['detail'][
        'available_modes'] == [], (
        'no patrol module in this Bringup')
    other = robot_drive_mode(_system(capability='navigate_to_pose'), waiting, None,
                             ready=True, can_follow=False)
    assert other['mode'] == 'idle'
    bad = _system()
    bad['active_foreground_missions'][0]['mission_id'] = 'x'
    assert robot_drive_mode(bad, _status(), None, ready=True, can_follow=False)['mode'] == 'idle'


def test_the_web_starts_and_stops_patrol_through_the_existing_bridge():
    start = panel_command('drive_mode_start', {'mode': 'patrol', 'thoroughness': 2})
    assert start == {'command': 'start', 'capability': 'patrol', 'arguments': {'thoroughness': 2}}
    assert panel_command('drive_mode_start', {'mode': 'patrol'})['arguments'] == {
        'thoroughness': 1}
    stop = panel_command('drive_mode_stop', {'mode': 'patrol', 'sessionId': MISSION_ID})
    assert stop == {'command': 'cancel_mission', 'mission_id': MISSION_ID}
    # The manager's own mission ID: the goal UUID as 32 hex digits, no hyphens.
    manager_id = MISSION_ID.replace('-', '')
    assert panel_command('drive_mode_stop', {'mode': 'patrol', 'sessionId': manager_id}) == {
        'command': 'cancel_mission', 'mission_id': manager_id}
    for operation, payload in [
        ('drive_mode_start', {'mode': 'roaming'}),
        ('drive_mode_start', {'mode': 'patrol', 'thoroughness': 3}),
        ('drive_mode_stop', {'mode': 'patrol', 'sessionId': '../etc'}),
        ('drive_mode_pause', {'mode': 'patrol', 'sessionId': MISSION_ID}),
        ('drive_mode_resume', {'mode': 'patrol', 'sessionId': MISSION_ID}),
    ]:
        with pytest.raises(ValueError):
            panel_command(operation, payload)
    with pytest.raises(ValueError):
        validate_command({'command': 'cancel_mission', 'mission_id': MISSION_ID, 'x': 1})


def test_the_state_upload_carries_patrol_for_the_map_screen():
    snapshot = PanelData().snapshot()
    snapshot['runtime'].update(state='READY', mode='navigation', ready=True)
    snapshot['servers'] = {'manager': True, 'autoslam': False, 'follow_person': False}
    snapshot['system'] = _system()
    snapshot['patrol'] = _status()
    upload = state_payload(snapshot, {'active': True, 'pose': {'x': 0, 'y': 0, 'yaw': 0}}, [])
    assert upload['driveMode']['mode'] == 'patrol'
    assert upload['driveMode']['sessionId'] == MISSION_ID
    assert len(json.dumps(upload).encode()) < 64 * 1024
    snapshot['system'] = {}
    idle = state_payload(snapshot, {'active': False}, [])['driveMode']
    assert idle['mode'] == 'idle' and idle['detail']['available_modes'] == []


def test_dispatch_queues_patrol_like_other_missions():
    bridge = SimpleNamespace(data=PanelData(), catalog=Mock(), node=Mock(),
                             submit=Mock(return_value='request-id'))
    sync = CloudSync(bridge, Mock())
    sync.dispatch({'id': COMMAND_ID, 'operation': 'drive_mode_start',
                   'payload': {'mode': 'patrol', 'thoroughness': 0}})
    assert sync.pending[COMMAND_ID]['ok']
    bridge.submit.assert_called_once_with(
        {'command': 'start', 'capability': 'patrol', 'arguments': {'thoroughness': 0}})


def test_following_the_person_in_front_is_a_drive_mode_too():
    system = _system(capability='follow_person')
    tracking = {'state': 'TRACKING', 'target_visible': True, 'current_distance_m': 0.83}
    mode = robot_drive_mode(system, None, tracking, ready=True, can_follow=True)
    assert (mode['mode'], mode['state'], mode['sessionId']) == (
        'person_following', 'active', MISSION_ID)
    detail = mode['detail']
    assert detail['tracking_state'] == 'TRACKING' and detail['target_visible'] is True
    assert detail['current_distance_m'] == 0.83 and detail['follow_distance_m'] == 0.6
    assert detail['can_pause'] is False and detail['available_modes'] == []
    lost = robot_drive_mode(system, None, {'state': 'RECOVERING', 'target_visible': False,
                                           'current_distance_m': float('inf')},
                            ready=True, can_follow=True)
    assert lost['state'] == 'active' and lost['detail']['current_distance_m'] is None
    assert robot_drive_mode(system, None, {'state': 'IDLE'}, ready=True,
                            can_follow=True)['state'] == 'starting'
    assert robot_drive_mode(_system(2, 'follow_person'), None, tracking, ready=True,
                            can_follow=True)['state'] == 'stopping'


def test_following_is_offered_when_its_server_is_up_and_nothing_runs():
    idle = robot_drive_mode({}, _status('idle', 'Waiting for a patrol goal'), None,
                            ready=True, can_follow=True)
    assert idle['detail']['available_modes'] == ['patrol', 'person_following']
    assert robot_drive_mode({}, None, None, ready=True, can_follow=True)['detail'][
        'available_modes'] == ['person_following']
    assert robot_drive_mode({}, None, None, ready=True, can_follow=False)['detail'][
        'available_modes'] == []
    assert robot_drive_mode(_system(), _status(), None, ready=True, can_follow=True)['detail'][
        'available_modes'] == [], 'no second drive while a patrol runs'


def test_the_web_starts_and_stops_following_at_the_fixed_distance():
    assert panel_command('drive_mode_start', {'mode': 'person_following'}) == {
        'command': 'start', 'capability': 'follow_person',
        'arguments': {'target_mode': 0, 'target_person_id': '', 'desired_distance_m': 0.6}}
    assert panel_command('drive_mode_stop', {'mode': 'person_following',
                                             'sessionId': MISSION_ID}) == {
        'command': 'cancel_mission', 'mission_id': MISSION_ID}
    for payload in ({'mode': 'person_following', 'distance': 2.0},
                    {'mode': 'person_following', 'thoroughness': 1}):
        with pytest.raises(ValueError):
            panel_command('drive_mode_start', payload)
    validate_command(panel_command('drive_mode_start', {'mode': 'person_following'}))


def test_saved_maps_carry_when_they_were_written(tmp_path):
    map_file = tmp_path / 'home.yaml'
    map_file.write_text('image: home.pgm\n')
    snapshot = PanelData().snapshot()
    upload = state_payload(snapshot, {'active': False}, [
        {'id': 'home.yaml', 'name': 'home', 'path': str(map_file)},
        {'id': 'gone.yaml', 'name': 'gone', 'path': str(tmp_path / 'gone.yaml')}])
    home, gone = upload['target']['maps']
    assert home['id'] == 'home.yaml' and home['savedAt'].endswith('+00:00')
    assert gone['savedAt'] is None


def test_the_upload_offers_following_only_while_its_server_is_up():
    snapshot = PanelData().snapshot()
    snapshot['runtime'].update(state='READY', mode='navigation', ready=True)
    info = {'active': True, 'pose': {'x': 0, 'y': 0, 'yaw': 0}}
    for up, offered in ((True, ['person_following']), (False, [])):
        snapshot['servers'] = {'manager': True, 'autoslam': True, 'follow_person': up}
        assert state_payload(snapshot, info, [])['driveMode']['detail'][
            'available_modes'] == offered


# ---------------------------------------------------------------- fall check stops a patrol

from datetime import datetime, timezone  # noqa: E402

from malbut_bringup.drive_mode import PatrolFallStops  # noqa: E402

T0 = datetime(2026, 10, 8, 1, 0, 0, tzinfo=timezone.utc)


def _fall(capability='fall_approach', where='pending_missions'):
    return {where: [{'mission_id': COMMAND_ID, 'capability_id': capability, 'mode': 0,
                     'priority': 3, 'state': 0}]}


def _approach(phase, outcome, seconds=5):
    return {'request_id': 'q', 'phase': phase, 'outcome': outcome,
            'at': T0.replace(second=seconds).isoformat()}


CANCELED = dict(state='idle', detail='Patrol canceled')


def _stopped_by_fall(stops):
    assert stops.observe(_system(), _status(), None) is None
    return stops.observe(_fall(), _status(**CANCELED), None)


def test_a_patrol_stopped_by_a_fall_check_says_so_and_does_not_resume():
    stops = PatrolFallStops(clock=lambda: T0)
    stop = _stopped_by_fall(stops)
    assert stop == {'stopped_at': T0.isoformat(), 'fall_result': 'pending', 'returned': False}
    mode = robot_drive_mode(_fall(), _status(**CANCELED), None, ready=True, can_follow=False,
                            fall_stop=stop)
    result = mode['detail']['last_patrol']
    assert result['outcome'] == 'fall_check' and result['fall_result'] == 'pending'
    assert mode['detail']['fall_check_active'] is True
    stops.observe(_fall('fall_approach', 'active_foreground_missions'), _status(**CANCELED),
                  _approach('approach', 'arrived'))
    stop = stops.observe({}, _status(**CANCELED), _approach('return', 'returned', 40))
    assert stop['fall_result'] == 'not_a_person' and stop['returned'] is True
    mode = robot_drive_mode({}, _status(**CANCELED), None, ready=True, can_follow=False,
                            fall_stop=stop)
    assert mode['mode'] == 'idle' and mode['detail']['fall_check_active'] is False
    assert mode['detail']['available_modes'] == ['patrol']  # Restart is the user's choice.


@pytest.mark.parametrize('steps,expected', [
    ([(_fall('fall_confirmation', 'active_foreground_missions'), None)], 'pending'),
    ([({}, None)], 'asked'),
    ([(_fall('fall_approach', 'active_foreground_missions'), _approach('approach', 'no_path'))],
     'unreachable'),
    ([(_fall('fall_approach', 'active_foreground_missions'), _approach('approach', 'arrived')),
      (_fall('fall_confirmation', 'active_foreground_missions'),
       _approach('approach', 'arrived'))],
     'person'),
])
def test_the_check_result_follows_the_fall_missions(steps, expected):
    stops = PatrolFallStops(clock=lambda: T0)
    _stopped_by_fall(stops)
    for system, approach in steps:
        stop = stops.observe(system, _status(**CANCELED), approach)
    assert stop['fall_result'] == expected


def test_a_users_stop_or_an_old_stop_is_not_blamed_on_a_fall():
    stops = PatrolFallStops(clock=lambda: T0)
    assert stops.observe(_system(), _status(), None) is None
    assert stops.observe({}, _status(**CANCELED), None) is None  # The user pressed 중지.
    assert stops.observe(_fall(), _status(**CANCELED), None) is None  # Fall later, while idle.
    mode = robot_drive_mode(_fall(), _status(**CANCELED), None, ready=True, can_follow=False,
                            fall_stop=None)
    assert mode['detail']['last_patrol']['outcome'] == 'stopped'


def test_a_new_patrol_clears_the_fall_reason_and_old_results_are_ignored():
    stops = PatrolFallStops(clock=lambda: T0)
    _stopped_by_fall(stops)
    earlier = dict(_approach('return', 'returned'), at='2026-10-08T00:59:55+00:00')
    assert stops.observe({}, _status(**CANCELED), earlier)['fall_result'] == 'asked'
    assert stops.observe(_system(), _status(), None) is None
    assert stops.stop is None
