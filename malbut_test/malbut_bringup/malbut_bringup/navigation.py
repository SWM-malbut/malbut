"""
Send the 말벗 to a point picked on the web map (SWM25-237).

The web map screen speaks the simulator's navigation contract. A preview checks the
point against the saved map's floor and no-entry Zones (a point just off the floor
moves to the nearest floor within 0.5 m) and asks the Nav2 planner for a path. Starting
the preview sends one navigate_to_pose mission, cancel stops only that mission, and the
robot state reports its progress in the web's navigation fields.
"""

import math
import secrets
import time

import yaml

from .zones import RESTRICTED_MARGIN_M


PREVIEW_TTL_S = 30
SNAP_RADIUS_M = 0.5
SNAP_STEP_M = 0.05
# The planner may stop a little short of the goal; farther means it cannot get there.
MAX_GOAL_GAP_M = 0.3
PATH_SPACING_M = 0.1
MAX_PATH_POINTS = 150
MAX_PREVIEWS = 16
# Manager request states (web_panel) as the web map screen names a destination drive.
_STATES = {
    'PENDING': 'driving', 'RUNNING': 'driving', 'UNCONFIRMED': 'driving',
    'CANCELING': 'canceling', 'SUCCEEDED': 'succeeded', 'CANCELED': 'canceled',
    'ABORTED': 'failed', 'REJECTED': 'failed', 'ERROR': 'failed',
}


# The padded footprint's half-diagonal: a centre this close to a no-entry Zone puts
# the body in it, and Nav2 then refuses every motion (2026-10-08).
FOOTPRINT_REACH_M = 0.19
ESCAPE_RADIUS_M = 1.0
# The bridge applies a keepout change within a second; the global costmap updates at 1 Hz.
ZONE_SETTLE_S = 2.5
# Below this the drive never really left its start (Nav2 gave up where it stood).
STUCK_AT_START_M = 0.15
# The reasons the web map names for a failed drive (목업 23번).
FAILURES = ('blocked_start', 'blocked_way', 'zone_stuck', 'manual_drive', 'fall_check',
            'fall_check_started', 'localization_lost', 'unknown')
_FALL_MISSIONS = ('fall_confirmation', 'fall_approach')
_MISSION_LISTS = ('active_foreground_missions', 'active_background_missions',
                  'pending_missions', 'suspended_missions')


class NavigationError(ValueError):
    """A destination cannot be previewed, started or canceled; the message is for people."""


def point_in_geometry(point, geometry):
    """Return whether a map point lies in a Polygon or MultiPolygon, outside its holes."""
    if not isinstance(geometry, dict):
        return False
    coordinates = geometry.get('coordinates')
    polygons = {'Polygon': [coordinates], 'MultiPolygon': coordinates}.get(geometry.get('type'))
    for polygon in polygons if isinstance(polygons, list) else []:
        if (isinstance(polygon, list) and polygon and _in_ring(point, polygon[0])
                and not any(_in_ring(point, hole) for hole in polygon[1:])):
            return True
    return False


def _in_ring(point, ring):
    if not isinstance(ring, list) or len(ring) < 3:
        return False
    x, y = point
    inside = False
    previous = ring[-1]
    for current in ring:
        (x0, y0), (x1, y1) = previous[:2], current[:2]
        if (y1 > y) != (y0 > y) and x < (x0 - x1) * (y - y1) / (y0 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def distance_to_geometry(point, geometry):
    """Return the distance from a point to the nearest edge of a Polygon or MultiPolygon."""
    coordinates = geometry.get('coordinates') if isinstance(geometry, dict) else None
    polygons = {'Polygon': [coordinates], 'MultiPolygon': coordinates}.get(
        geometry.get('type') if isinstance(geometry, dict) else None)
    best = math.inf
    for polygon in polygons if isinstance(polygons, list) else []:
        for ring in polygon if isinstance(polygon, list) else []:
            if isinstance(ring, list) and len(ring) >= 2:
                for start, end in zip(ring, ring[1:] + ring[:1]):
                    best = min(best, _segment_distance(point, start[:2], end[:2]))
    return best


def _segment_distance(point, start, end):
    (px, py), (ax, ay), (bx, by) = point, start, end
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def resolve_goal(point, floor, blocked, margin=RESTRICTED_MARGIN_M):
    """
    Return where to drive for a picked point and how far it moved.

    A point in a no-entry Zone is refused. A point just off the floor, or so close to a
    no-entry Zone that the parked body would touch it (the robot's margin), moves to
    the nearest floor point within 0.5 m that keeps the margin.
    """
    if any(point_in_geometry(point, zone) for zone in blocked):
        raise NavigationError('진입 금지 구역이에요. 다른 곳을 골라 주세요.')

    def usable(candidate):
        return (any(point_in_geometry(candidate, area) for area in floor)
                and not any(point_in_geometry(candidate, zone)
                            or distance_to_geometry(candidate, zone) < margin
                            for zone in blocked))

    if usable(point):
        return point, 0.0
    for ring in range(1, int(round(SNAP_RADIUS_M / SNAP_STEP_M)) + 1):
        radius = ring * SNAP_STEP_M
        count = max(8, math.ceil(2 * math.pi * radius / SNAP_STEP_M))
        for index in range(count):
            angle = 2 * math.pi * index / count
            candidate = (point[0] + radius * math.cos(angle), point[1] + radius * math.sin(angle))
            if usable(candidate):
                return candidate, radius
    raise NavigationError('말벗이 다닐 수 있는 바닥을 골라 주세요.')


def escape_point(point, floor, blocked, margin=RESTRICTED_MARGIN_M):
    """
    Return where to drive first so the body clears every no-entry Zone.

    None: the body is already clear. False: no floor point within 1 m keeps the
    margin, so the robot cannot get out by itself.
    """
    if not any(point_in_geometry(point, zone) or distance_to_geometry(point, zone)
               < FOOTPRINT_REACH_M for zone in blocked):
        return None

    def usable(candidate):
        return (any(point_in_geometry(candidate, area) for area in floor)
                and not any(point_in_geometry(candidate, zone)
                            or distance_to_geometry(candidate, zone) < margin
                            for zone in blocked))

    for ring in range(1, int(round(ESCAPE_RADIUS_M / SNAP_STEP_M)) + 1):
        radius = ring * SNAP_STEP_M
        count = max(8, math.ceil(2 * math.pi * radius / SNAP_STEP_M))
        for index in range(count):
            angle = 2 * math.pi * index / count
            candidate = (point[0] + radius * math.cos(angle), point[1] + radius * math.sin(angle))
            if usable(candidate):
                return candidate
    return False


def failure_reason(request, start, system):
    """Name why a destination drive ended without arriving, for the web map (목업 23번)."""
    result = request.get('result') if isinstance(request.get('result'), dict) else {}
    message = ' '.join(str(value) for value in (result.get('message'), request.get('message'))
                       if value)
    if 'priority HIGH' in message:
        return 'manual_drive'  # Manual driving is the only HIGH mission.
    if 'priority URGENT' in message:
        return 'fall_check'
    if 'preempted' in message:
        running = {mission.get('capability_id') for key in _MISSION_LISTS
                   for mission in (system or {}).get(key) or [] if isinstance(mission, dict)}
        return 'fall_check_started' if running & set(_FALL_MISSIONS) else 'unknown'
    if request.get('state') in ('ERROR', 'REJECTED'):
        lowered = message.lower()
        return 'localization_lost' if 'pose' in lowered or 'locali' in lowered else 'unknown'
    if request.get('state') == 'ABORTED':
        pose = _feedback_pose(request)
        if pose is None or start is None or math.dist(pose, start) < STUCK_AT_START_M:
            return 'blocked_start'
        return 'blocked_way'
    return 'unknown'


def path_summary(points):
    """Return a planned path's length and its points every 10 cm (at most 150)."""
    length = sum(math.dist(start, end) for start, end in zip(points, points[1:]))
    spacing = max(PATH_SPACING_M, length / MAX_PATH_POINTS)
    kept = [points[0]]
    for point in points[1:-1]:
        if math.dist(point, kept[-1]) >= spacing:
            kept.append(point)
    if len(points) > 1:
        kept.append(points[-1])
    return {'length_m': round(length, 3),
            'points': [[round(x, 4), round(y, 4)] for x, y in kept]}


def _feedback_values(request):
    nested = (request.get('feedback') or {}).get('feedback_yaml')
    try:
        values = yaml.safe_load(nested) if isinstance(nested, str) else None
    except yaml.YAMLError:
        values = None
    return values if isinstance(values, dict) else None


def _feedback_pose(request):
    """Return where Nav2 last reported the robot during this drive, or None."""
    try:
        position = _feedback_values(request)['current_pose']['pose']['position']
        x, y = float(position['x']), float(position['y'])
    except (TypeError, KeyError, ValueError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def _feedback(request):
    """Read Nav2's remaining distance and time from the mission's forwarded feedback."""
    values = _feedback_values(request)
    if values is None:
        return None, None
    distance = values.get('distance_remaining')
    eta = values.get('estimated_time_remaining')
    distance = float(distance) if type(distance) in (int, float) else None
    if isinstance(eta, dict) and all(type(eta.get(key)) is int for key in ('sec', 'nanosec')):
        eta = eta['sec'] + eta['nanosec'] / 1e9
    else:
        eta = None
    return distance, eta


class Navigator:
    """Keep previews for 30 s and the one destination drive this bridge started."""

    def __init__(self, clock=time.monotonic):
        """Start with no preview and no drive."""
        self.clock = clock
        self.previews = {}
        self.session = None

    def busy(self, requests):
        """Return whether a destination drive (this one or another) is still going."""
        if self.session is not None and self.session['state'] == 'escaping':
            return True
        return any(item.get('capability') == 'navigate_to_pose'
                   and _STATES.get(item.get('state')) in ('driving', 'canceling')
                   for item in requests)

    def preview(self, x, y, *, pose, map_key, floor, blocked, plan, busy):
        """Check one picked point, plan to it and keep the result for 30 s."""
        if pose is None:
            raise NavigationError('말벗이 아직 지도에서 자기 위치를 찾고 있어요.')
        if busy:
            raise NavigationError('이동 중에는 새 목적지를 고를 수 없어요.')
        goal, moved = resolve_goal((x, y), floor, blocked)
        try:
            points = [(float(px), float(py)) for px, py in plan(goal[0], goal[1], pose['yaw'])]
        except (ValueError, RuntimeError, TimeoutError) as error:
            raise NavigationError('그곳까지 가는 길을 찾지 못했어요.') from error
        if not points or math.dist(points[-1], goal) > MAX_GOAL_GAP_M:
            raise NavigationError('그곳까지 가는 길을 찾지 못했어요.')
        yaw = pose['yaw']
        if len(points) > 1 and math.dist(points[-2], points[-1]) > 1e-6:
            yaw = math.atan2(points[-1][1] - points[-2][1], points[-1][0] - points[-2][0])
        resolved = {'x': round(goal[0], 4), 'y': round(goal[1], 4), 'yaw': round(yaw, 5)}
        path = path_summary(points)
        now = self.clock()
        self.previews = {token: record for token, record in self.previews.items()
                         if record['expires'] > now}
        while len(self.previews) >= MAX_PREVIEWS:
            del self.previews[next(iter(self.previews))]
        token = secrets.token_urlsafe(24)
        self.previews[token] = {'expires': now + PREVIEW_TTL_S, 'map_key': map_key,
                                'goal': resolved, 'path': path}
        return {
            'preview_token': token, 'expires_in_s': PREVIEW_TTL_S,
            'requested': {'x': x, 'y': y}, 'resolved': resolved,
            'snapped': moved > 0, 'snap_distance_m': round(moved, 3), 'path': path,
        }

    def start(self, token, *, map_key, busy, submit, escape=None, zones=None):
        """
        Send a previewed destination once; ``submit`` returns the mission's request ID.

        ``escape`` is where to drive first when the body touches a no-entry Zone
        (``escape_point``); ``zones(False)`` lifts the Zones for that short drive and
        ``zones(True)`` restores them before the destination itself is sent.
        """
        record = self.previews.pop(token, None)
        if record is None or record['expires'] <= self.clock():
            raise NavigationError('고른 목적지의 확인 시간이 지났어요. 다시 골라 주세요.')
        if record['map_key'] != map_key:
            raise NavigationError('그사이 지도가 바뀌었어요. 다시 골라 주세요.')
        if busy:
            raise NavigationError('이동 중에는 새 목적지를 고를 수 없어요.')
        session = {'goal': record['goal'], 'path': record['path'], 'submit': submit,
                   'zones': zones, 'reason': None}
        if escape is False:
            session.update(session_id=secrets.token_hex(16), request_id=None,
                           state='failed', phase='done', reason='zone_stuck')
        elif escape is not None and zones is not None:
            zones(False)
            session.update(session_id=secrets.token_hex(16), request_id=None, state='escaping',
                           phase='escape_wait', escape={'x': round(escape[0], 4),
                                                        'y': round(escape[1], 4),
                                                        'yaw': record['goal']['yaw']},
                           at=self.clock() + ZONE_SETTLE_S)
        else:
            request_id = submit(record['goal'])
            session.update(session_id=request_id, request_id=request_id, state='driving',
                           phase='drive')
        self.session = session
        return {key: session[key] for key in ('session_id', 'state', 'goal', 'path')}

    def escape_then(self, escape, *, submit, zones, then):
        """Drive out of a no-entry Zone first, then call ``then`` (a patrol start)."""
        if self.busy([]):
            raise NavigationError('말벗이 금지 구역에서 빠져나오는 중이에요.')
        point = escape if escape else (0.0, 0.0)
        goal = {'x': round(point[0], 4), 'y': round(point[1], 4), 'yaw': 0.0}
        session = {'session_id': secrets.token_hex(16), 'request_id': None, 'goal': goal,
                   'path': {'length_m': 0.0, 'points': []}, 'submit': submit,
                   'zones': zones, 'then': then, 'reason': None}
        if escape is False:
            session.update(state='failed', phase='done', reason='zone_stuck')
        else:
            zones(False)
            session.update(state='escaping', phase='escape_wait', escape=goal,
                           at=self.clock() + ZONE_SETTLE_S)
        self.session = session
        return {'session_id': session['session_id'], 'state': session['state']}

    def advance(self, requests):
        """Move an escape on: wait for the Zones, drive out, restore them, then drive."""
        session = self.session
        if session is None or session['phase'] not in ('escape_wait', 'escape', 'drive_wait'):
            return
        now = self.clock()
        try:
            if session['phase'] == 'escape_wait' and now >= session['at']:
                session['request_id'] = session['submit'](session['escape'])
                session['phase'] = 'escape'
            elif session['phase'] == 'escape':
                request = self._request(requests)
                state = request.get('state') if request else None
                if state in ('SUCCEEDED', 'CANCELED', 'ABORTED', 'REJECTED', 'ERROR'):
                    session['zones'](True)
                    if state == 'SUCCEEDED':
                        session.update(phase='drive_wait', at=now + ZONE_SETTLE_S)
                    else:
                        session.update(phase='done', state='failed', reason='zone_stuck')
            elif session['phase'] == 'drive_wait' and now >= session['at']:
                if session.get('then') is not None:
                    # Out of the Zone: the patrol takes over; the map card shows it.
                    session['then']()
                    session.update(phase='done', state='handed_over')
                else:
                    session['request_id'] = session['submit'](session['goal'])
                    session.update(phase='drive', state='driving')
        except (ValueError, RuntimeError) as error:
            if session.get('zones') is not None:
                session['zones'](True)
            session.update(phase='done', state='failed', reason='unknown',
                           message=str(error)[:256])

    def _request(self, requests):
        request_id = self.session.get('request_id') if self.session else None
        return next((item for item in requests if item.get('id') == request_id), None)

    def cancel(self, session_id, cancel):
        """Stop the drive this bridge started, and nothing else."""
        session = self.session
        if session is None or session['session_id'] != session_id:
            raise NavigationError('이미 끝났거나 바뀐 이동이에요.')
        if session['phase'] in ('escape_wait', 'drive_wait'):
            # Nothing runs in the manager between steps: restore the Zones and stop here.
            if session.get('zones') is not None:
                session['zones'](True)
            session.update(phase='done', state='canceled')
            return {'session_id': session_id, 'state': 'canceled'}
        if session.get('request_id'):
            cancel(session['request_id'])
        return {'session_id': session_id, 'state': 'canceling'}

    def target(self, requests, system=None):
        """Report the drive in the web map screen's navigation fields (empty before one)."""
        if self.session is None:
            return {}
        session = self.session
        request = self._request(requests) if session['phase'] == 'drive' else None
        if request is not None:
            session['state'] = _STATES.get(request.get('state'), 'driving')
            session['message'] = str(request.get('message') or '')[:256]
            if session['state'] == 'failed' and session.get('reason') is None:
                points = session['path'].get('points') or []
                start = tuple(points[0]) if points else None
                session['reason'] = failure_reason(request, start, system)
            distance, eta = _feedback(request)
            if distance is not None:
                session['distance_remaining_m'] = round(distance, 3)
            if eta is not None:
                session['estimated_time_remaining_s'] = round(eta, 1)
        length = session['path']['length_m']
        remaining = session.get('distance_remaining_m', length)
        progress = 1.0 if session['state'] == 'succeeded' else (
            max(0.0, min(1.0, 1 - remaining / length)) if length > 0 else 0.0)
        return {
            'session_id': session['session_id'], 'state': session['state'],
            'goal': session['goal'], 'path': session['path'],
            'path_length_m': length, 'initial_path_length_m': length,
            'distance_remaining_m': remaining,
            'estimated_time_remaining_s': session.get('estimated_time_remaining_s'),
            'progress_ratio': round(progress, 3), 'message': session.get('message', ''),
            'reason': session.get('reason') if session['state'] == 'failed' else None,
        }
