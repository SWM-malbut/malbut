"""
Report the real robot's autonomous drives as the web map screen's drive mode (지도 탭 › 자율주행).

The system manager lists every running mission, wherever it was started (the map tab,
the developer screen, a voice request). Room patrol publishes its stage, how much of the
house it has seen and which rooms remain on ``/patrol/status`` and keeps its last result
there; person following publishes whether it sees the person and how far away on
``/tracking/person/status``. Together they give the simulator's drive mode fields: a
patrol or a person following in progress with its mission ID as the session, or idle
with the last patrol's result. The real robot cannot pause either, only stop it.
"""

import math
import re

PATROL = 'patrol'
FOLLOW = 'follow_person'
# The map tab follows the person in front of the 말벗 at this fixed distance.
FOLLOW_DISTANCE_M = 0.6
# Manager capability → the web's drive mode name.
_DRIVE_MODES = {PATROL: 'patrol', FOLLOW: 'person_following'}
# The web accepts session IDs of these characters only; a mission ID is a UUID string.
_SESSION = re.compile(r'[A-Za-z0-9_-]{8,128}')
THOROUGHNESS_LEVELS = (0, 1, 2)
# MissionStatus states (malbut_interfaces/msg/MissionStatus.msg).
_PENDING, _RUNNING, _CANCELING, _SUSPENDED = 0, 1, 2, 3
_MISSION_LISTS = ('active_foreground_missions', 'suspended_missions', 'pending_missions')
_PHASES = ('planning', 'navigating', 'observing', 'stopping')
# patrol_manager's result when every reachable viewpoint was tried before the target.
_PARTIAL = 'No usable untried viewpoint remains'
_WAITING = 'Waiting for a patrol goal'
MAX_ROOM_NAMES = 32


def drive_mission(system):
    """Return the patrol or person following the manager is running, suspending or queuing."""
    for key in _MISSION_LISTS:
        for mission in (system or {}).get(key) or []:
            if (isinstance(mission, dict) and mission.get('capability_id') in _DRIVE_MODES
                    and _SESSION.fullmatch(str(mission.get('mission_id')))):
                return mission
    return None


def _rooms(value):
    if not isinstance(value, list):
        return []
    return [str(name)[:40] for name in value[:MAX_ROOM_NAMES]]


def _progress(status):
    coverage = status.get('coverage_ratio')
    visited = status.get('viewpoints_visited')
    return {
        'coverage_ratio': round(float(coverage), 3) if type(coverage) in (int, float) else 0.0,
        'viewpoints_visited': int(visited) if type(visited) is int else 0,
    }


def last_patrol(status):
    """Name how the last patrol ended: done, partial, failed or stopped (none if none ran)."""
    state = status.get('state')
    detail = str(status.get('detail') or '')
    if state == 'completed':
        outcome = 'done'
    elif state == 'aborted':
        outcome = 'partial' if detail.startswith(_PARTIAL) else 'failed'
    elif state == 'idle' and detail and detail != _WAITING:
        outcome = 'stopped'  # Canceled; the status keeps how far it got.
    else:
        return None
    return {'outcome': outcome, **_progress(status),
            'inaccessible_rooms': _rooms(status.get('inaccessible_rooms'))}


def robot_drive_mode(system, patrol, tracking, *, ready, can_follow):
    """
    Return the drive mode for the state upload.

    ``patrol`` and ``tracking`` are the latest ``/patrol/status`` and
    ``/tracking/person/status`` (None when not received). ``ready`` says the robot
    drives on a saved map with a known position; ``can_follow`` that the person
    following server is up.
    """
    mission = drive_mission(system)
    patrol = patrol if isinstance(patrol, dict) else None
    tracking = tracking if isinstance(tracking, dict) else None
    available = []
    if ready and mission is None:
        available += ['patrol'] if patrol is not None else []
        available += ['person_following'] if can_follow else []
    detail = {
        'available_modes': available,
        'can_pause': False,
        'thoroughness_levels': list(THOROUGHNESS_LEVELS),
        'follow_distance_m': FOLLOW_DISTANCE_M,
    }
    if mission is None:
        result = last_patrol(patrol) if patrol else None
        if result is not None:
            detail['last_patrol'] = result
        return {'mode': 'idle', 'state': 'idle', 'sessionId': None, 'message': None,
                'detail': detail}
    mission_state = mission.get('state')
    capability = mission.get('capability_id')
    if capability == FOLLOW:
        tracking_state = str((tracking or {}).get('state') or '')
        if mission_state == _CANCELING:
            state = 'stopping'
        elif mission_state == _PENDING or tracking_state in ('', 'STOPPED', 'IDLE'):
            state = 'starting'
        else:
            state = 'active'
        distance = (tracking or {}).get('current_distance_m')
        detail.update({
            'tracking_state': tracking_state or 'IDLE',
            'target_visible': (tracking or {}).get('target_visible') is True,
            'current_distance_m': round(float(distance), 2)
            if type(distance) in (int, float) and math.isfinite(distance) else None,
        })
    else:
        phase = patrol.get('state') if patrol else None
        current = phase in _PHASES
        if mission_state == _CANCELING or phase == 'stopping':
            state = 'stopping'
        elif mission_state == _PENDING or not current:
            state = 'starting'  # The status still shows the previous patrol's end.
        else:
            state = 'active'
        detail.update({
            'patrol_phase': phase if current else 'planning',
            'suspended': mission_state == _SUSPENDED,
            **(_progress(patrol) if current else _progress({})),
            'unvisited_rooms': _rooms(patrol.get('unvisited_rooms')) if current else [],
        })
    return {'mode': _DRIVE_MODES[capability], 'state': state,
            'sessionId': str(mission.get('mission_id')), 'message': None, 'detail': detail}
