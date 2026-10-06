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


def resolve_goal(point, floor, blocked):
    """
    Return where to drive for a picked point and how far it moved.

    A point in a no-entry Zone is refused. A point just off the floor moves to the
    nearest floor point within 0.5 m that no Zone forbids.
    """
    if any(point_in_geometry(point, zone) for zone in blocked):
        raise NavigationError('진입 금지 구역이에요. 다른 곳을 골라 주세요.')

    def usable(candidate):
        return (any(point_in_geometry(candidate, area) for area in floor)
                and not any(point_in_geometry(candidate, zone) for zone in blocked))

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


def _feedback(request):
    """Read Nav2's remaining distance and time from the mission's forwarded feedback."""
    nested = (request.get('feedback') or {}).get('feedback_yaml')
    try:
        values = yaml.safe_load(nested) if isinstance(nested, str) else None
    except yaml.YAMLError:
        values = None
    if not isinstance(values, dict):
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

    def start(self, token, *, map_key, busy, submit):
        """Send a previewed destination once; ``submit`` returns the mission's request ID."""
        record = self.previews.pop(token, None)
        if record is None or record['expires'] <= self.clock():
            raise NavigationError('고른 목적지의 확인 시간이 지났어요. 다시 골라 주세요.')
        if record['map_key'] != map_key:
            raise NavigationError('그사이 지도가 바뀌었어요. 다시 골라 주세요.')
        if busy:
            raise NavigationError('이동 중에는 새 목적지를 고를 수 없어요.')
        session_id = submit(record['goal'])
        self.session = {'session_id': session_id, 'goal': record['goal'],
                        'path': record['path'], 'state': 'driving'}
        return {'session_id': session_id, 'state': 'driving',
                'goal': record['goal'], 'path': record['path']}

    def cancel(self, session_id, cancel):
        """Stop the drive this bridge started, and nothing else."""
        if self.session is None or self.session['session_id'] != session_id:
            raise NavigationError('이미 끝났거나 바뀐 이동이에요.')
        cancel(session_id)
        return {'session_id': session_id, 'state': 'canceling'}

    def target(self, requests):
        """Report the drive in the web map screen's navigation fields (empty before one)."""
        if self.session is None:
            return {}
        session = self.session
        request = next((item for item in requests if item.get('id') == session['session_id']),
                       None)
        if request is not None:
            session['state'] = _STATES.get(request.get('state'), 'driving')
            session['message'] = str(request.get('message') or '')[:256]
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
        }
