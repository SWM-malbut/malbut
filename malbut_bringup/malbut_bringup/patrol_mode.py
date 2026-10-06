"""
Report room patrol as the web map screen's drive mode (지도 탭 › 자율주행).

The system manager lists every running mission, wherever it was started (the map tab,
the developer screen, a voice request). The patrol module publishes its stage, how much
of the house it has seen and which rooms remain on ``/patrol/status``, and keeps its last
result there. Together they give the simulator's drive mode fields: a patrol in progress
with its mission ID as the session, or idle with the last patrol's result. The real robot
cannot pause a patrol, only stop it.
"""

import re

PATROL = 'patrol'
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


def patrol_mission(system):
    """Return the patrol mission the manager is running, suspending or about to run."""
    for key in _MISSION_LISTS:
        for mission in (system or {}).get(key) or []:
            if (isinstance(mission, dict) and mission.get('capability_id') == PATROL
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


def patrol_drive_mode(system, status, ready):
    """
    Return the drive mode for the state upload.

    ``status`` is the latest ``/patrol/status`` (None when no patrol module runs) and
    ``ready`` says the robot drives on a saved map with a known position.
    """
    mission = patrol_mission(system)
    status = status if isinstance(status, dict) else None
    detail = {
        'available_modes': [PATROL] if ready and status is not None and mission is None else [],
        'can_pause': False,
        'thoroughness_levels': list(THOROUGHNESS_LEVELS),
    }
    if mission is None:
        result = last_patrol(status) if status else None
        if result is not None:
            detail['last_patrol'] = result
        return {'mode': 'idle', 'state': 'idle', 'sessionId': None, 'message': None,
                'detail': detail}
    mission_state = mission.get('state')
    phase = status.get('state') if status else None
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
        **(_progress(status) if current else _progress({})),
        'unvisited_rooms': _rooms(status.get('unvisited_rooms')) if current else [],
    })
    return {'mode': PATROL, 'state': state, 'sessionId': str(mission.get('mission_id')),
            'message': None, 'detail': detail}
