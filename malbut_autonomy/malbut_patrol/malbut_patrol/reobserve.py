"""Read sealed observations and choose bounded alternate views without calling an LLM."""

import hashlib
from itertools import permutations
import json
import math
from pathlib import Path
import re
import sqlite3

import numpy as np

from malbut_patrol.coverage import Viewpoint, _flood_distances


def read_results(directory, patrol_id, map_id, simulated):
    """Read only the current immutable run and owner; None means analysis is pending."""
    directory = Path(directory).expanduser()
    if not re.fullmatch('[a-f0-9]{32}', patrol_id):
        return []
    manifest = directory / 'runs' / (patrol_id + '.json')
    database = directory / 'observations.sqlite3'
    if not manifest.exists() or not database.exists():
        return None
    try:
        if manifest.is_symlink() or manifest.stat().st_size > 16384:
            return []
        sealed = json.loads(manifest.read_text())
        captures = sealed['captures']
        if sealed['patrol_id'] != patrol_id or not 1 <= len(captures) <= 24:
            return []
        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=.1)
        try:
            connection.row_factory = sqlite3.Row
            owner = connection.execute('SELECT user_id FROM owner').fetchone()[0]
            rows = []
            for capture in captures:
                identity = capture['observation_id']
                if not re.fullmatch('[a-f0-9]{32}', identity):
                    return []
                path = directory / 'captures' / (identity + '.json')
                if path.is_symlink() or path.stat().st_size > 16384:
                    return []
                contents = path.read_bytes()
                if hashlib.sha256(contents).hexdigest() != capture['manifest_sha256']:
                    return []
                metadata = json.loads(contents)
                if (metadata['patrol_id'] != patrol_id or metadata['map_id'] != map_id
                        or metadata['simulated'] is not simulated):
                    return []
                row = connection.execute('SELECT * FROM observations WHERE id=? AND user_id=?',
                                         (identity, owner)).fetchone()
                if row is None or row['status'] == 'pending':
                    return None
                if (row['status'] != 'ready' or metadata != json.loads(row['metadata'])
                        or len(row['analysis']) > 120000):
                    return []
                rows.append((metadata, json.loads(row['analysis'])))
            return rows
        finally:
            connection.close()
    except sqlite3.OperationalError:
        return None
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return []


def unresolved_targets(rows, maximum=2):
    """Request fresh evidence for visible objects; hints are never identity matches."""
    targets = []
    for metadata, analysis in sorted(rows, key=lambda r: r[0]['captured_at'], reverse=True):
        for index, item in enumerate(analysis['objects']):
            person = item.get('kind') == 'person'
            spatial = item.get('spatial', {})
            hint = spatial.get('reobserve_hint', {})
            xy = hint.get('xy')
            if (spatial.get('status') != 'unknown' or item.get('confidence') != 'high'
                    or hint.get('purpose') != 'aim_only_not_object_position'
                    or not isinstance(xy, list) or len(xy) != 2
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in xy)):
                continue
            camera = metadata['camera_pose']
            pose = [camera['x'], camera['y']]
            if not .15 <= math.dist(xy, pose) <= 5:
                continue
            kind = 'person' if person else item.get('kind', 'object')
            if any(t['kind'] == kind and t.get('name') == item['name']
                   and math.dist(xy, t['xy']) < .7 for t in targets):
                continue
            targets.append({'xy': xy, 'camera_xy': pose, 'kind': kind, 'name': item['name'],
                            'observation_id': metadata['observation_id'],
                            'object_index': index, 'reason': spatial.get('reason')})
    # Prefer people before static furniture; keep the newest evidence within
    # each priority and retain the configured bound on additional captures.
    targets.sort(key=lambda t: t['kind'] != 'person')
    return targets[:maximum]


def select_view(planner, target, robot_xy, previous=(), allowed=None, max_travel=3.,
                return_xy=None, checkpoint=None):
    """Score safe new viewpoints by parallax, depth range and travel; not calibrated entropy."""
    start = planner._nearest_cell(planner.reachable, robot_xy, planner._clearance_m)
    if start is None:
        return None
    travel = _flood_distances(planner.reachable, start, checkpoint)
    home = (planner._nearest_cell(planner.reachable, return_xy, planner._clearance_m)
            if return_xy is not None else None)
    if return_xy is not None and home is None:
        return None
    return_travel = (_flood_distances(planner.reachable, home, checkpoint)
                     if home is not None else None)
    tx, ty = target['xy']
    avoided = [target['camera_xy'], *previous]
    best = None
    best_score = -math.inf
    for index, (row, col) in enumerate(planner._candidates):
        if travel[row, col] < 0:
            continue
        cost = float(travel[row, col])*planner.grid.resolution
        x, y = map(float, planner.grid.cell_to_world(row, col))
        distance = math.hypot(x-tx, y-ty)
        if (not .8 <= distance <= 2.5 or cost > max_travel
                or any(math.hypot(x-p[0], y-p[1]) < .6 for p in avoided)
                or (allowed is not None and not allowed(x, y))):
            continue
        angle = math.atan2(y-ty, x-tx)
        parallax = min(abs(math.atan2(math.sin(angle-math.atan2(p[1]-ty, p[0]-tx)),
                                      math.cos(angle-math.atan2(p[1]-ty, p[0]-tx))))
                       for p in avoided)
        if parallax < math.radians(20):
            continue
        # Stop before the estimated surface; a target itself may occupy a map cell.
        fractions = np.linspace(0, max(0., 1-.25/distance),
                                max(2, math.ceil(distance/planner.grid.resolution*2)))
        cells = [planner.grid.world_to_cell(x+(tx-x)*t, y+(ty-y)*t) for t in fractions]
        if any(not planner.grid.contains(r, c) or not planner.grid.free[r, c]
               for r, c in cells):
            continue
        detour = cost
        if return_travel is not None:
            if return_travel[row, col] < 0 or return_travel[start] < 0:
                continue
            detour += float(return_travel[row, col]-return_travel[start])*planner.grid.resolution
        score = min(parallax, math.pi/2) / ((1+max(0., detour))*(1+abs(distance-1.5)))
        if score > best_score:
            best_score = score
            best = Viewpoint(x, y, math.atan2(ty-y, tx-x), index)
    return best


def plan_reobservations(planner, targets, robot_xy, return_xy, previous=(),
                        allowed=None, max_travel=3., checkpoint=None):
    """
    Try both orders of at most two targets, including the final return leg.

    View selection is heuristic; permutation search is exact only over those
    generated alternatives. Re-run after each movement using its measured pose.
    """
    if len(targets) > 2:
        raise ValueError('At most two reobservation targets per round')
    home = planner._nearest_cell(planner.reachable, return_xy, planner._clearance_m)
    if home is None:
        return [], math.inf
    fields = {}

    def distance(a, b):
        start = planner._nearest_cell(planner.reachable, a, planner._clearance_m)
        end = planner._nearest_cell(planner.reachable, b, planner._clearance_m)
        if start is None or end is None:
            return math.inf
        if start not in fields:
            fields[start] = _flood_distances(planner.reachable, start, checkpoint)
        cells = fields[start][end]
        return float(cells)*planner.grid.resolution if cells >= 0 else math.inf

    best, length = [], distance(robot_xy, return_xy)
    for order in permutations(targets):
        xy, avoided, steps, cost = robot_xy, list(previous), [], 0.
        for target in order:
            if checkpoint:
                checkpoint()
            view = select_view(planner, target, xy, avoided, allowed, max_travel,
                               return_xy, checkpoint)
            if view is None:
                continue
            destination = (view.x, view.y)
            cost += distance(xy, destination)
            steps.append((target, view))
            avoided.append(destination)
            xy = destination
        cost += distance(xy, return_xy)
        if (len(steps), -cost) > (len(best), -length):
            best, length = steps, cost
    return best, length
