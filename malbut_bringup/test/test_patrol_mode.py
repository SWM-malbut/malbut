"""Room patrol on the web map's 자율주행 card for the real robot, without ROS."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_bringup.cloud_sync import CloudSync, panel_command, state_payload
from malbut_bringup.patrol_mode import last_patrol, patrol_drive_mode
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
    mode = patrol_drive_mode(_system(), _status(), ready=True)
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
    mode = patrol_drive_mode(_system(mission_state), _status(phase), ready=True)
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
    mode = patrol_drive_mode({}, status, ready=True)
    assert (mode['mode'], mode['state'], mode['sessionId']) == ('idle', 'idle', None)
    result = mode['detail']['last_patrol']
    assert result['outcome'] == outcome and result['coverage_ratio'] == 0.45
    assert result['viewpoints_visited'] == 3
    if outcome == 'partial':
        assert result['inaccessible_rooms'] == ['창고']
    assert mode['detail']['available_modes'] == ['patrol']


def test_patrol_is_offered_only_when_it_can_run():
    waiting = _status('idle', 'Waiting for a patrol goal')
    assert last_patrol(waiting) is None, 'a fresh start has no result'
    assert patrol_drive_mode({}, waiting, ready=True)['detail']['available_modes'] == ['patrol']
    assert patrol_drive_mode({}, waiting, ready=False)['detail']['available_modes'] == []
    assert patrol_drive_mode({}, None, ready=True)['detail']['available_modes'] == [], (
        'no patrol module in this Bringup')
    other = patrol_drive_mode(_system(capability='follow_person'), waiting, ready=True)
    assert other['mode'] == 'idle'
    bad = _system()
    bad['active_foreground_missions'][0]['mission_id'] = 'x'
    assert patrol_drive_mode(bad, _status(), ready=True)['mode'] == 'idle'


def test_the_web_starts_and_stops_patrol_through_the_existing_bridge():
    start = panel_command('drive_mode_start', {'mode': 'patrol', 'thoroughness': 2})
    assert start == {'command': 'start', 'capability': 'patrol', 'arguments': {'thoroughness': 2}}
    assert panel_command('drive_mode_start', {'mode': 'patrol'})['arguments'] == {
        'thoroughness': 1}
    stop = panel_command('drive_mode_stop', {'mode': 'patrol', 'sessionId': MISSION_ID})
    assert stop == {'command': 'cancel_mission', 'mission_id': MISSION_ID}
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
    snapshot['servers'] = {'manager': True, 'autoslam': False}
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
