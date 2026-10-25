"""Check alternate viewpoints, sealed feedback, quotas, and evidence provenance."""

import hashlib
import json
import math
import sqlite3

import numpy as np

from malbut_patrol.coverage import CoverageGrid, CoveragePlanner, CoverageProfile
from malbut_patrol.photo_recorder import PhotoRecorder
from malbut_patrol.reobserve import (
    plan_reobservations, read_results, select_view, unresolved_targets,
)
from test_photo_recorder import camera_image


def planner(cells=None):
    return CoveragePlanner(CoverageGrid(np.zeros((60, 60)) if cells is None else cells,
                                        .1, -3, -3),
                           CoverageProfile(3, .9, .4), (-2., 0.), .26)


def target():
    return {'xy': [0., 0.], 'camera_xy': [-2., 0.], 'kind': 'person'}


def test_alternate_view_changes_position_angle_and_respects_travel():
    p, t = planner(), target()
    first = select_view(p, t, (-2., 0.))
    assert first is not None
    assert math.dist((first.x, first.y), t['camera_xy']) >= .6
    assert abs(math.atan2(math.sin(first.yaw), math.cos(first.yaw))) >= math.radians(20)
    assert math.isclose(first.yaw, math.atan2(-first.y, -first.x))
    second = select_view(p, t, (first.x, first.y), [(first.x, first.y)])
    assert second is not None
    assert math.dist((first.x, first.y), (second.x, second.y)) >= .6
    assert select_view(p, t, (-2., 0.), allowed=lambda x, y: False) is None
    assert select_view(p, t, (-2., 0.), max_travel=.01) is None


def test_wall_occlusion_and_unreachable_component_are_excluded():
    cells = np.zeros((60, 60))
    cells[:, 27:29] = 100
    p = planner(cells)
    assert select_view(p, target(), (-2., 0.)) is None


def test_retry_plan_is_rebuilt_from_actual_pose_and_keeps_a_finite_return_leg():
    p = planner()
    targets = [target(), dict(target(), xy=[0., 1.3], kind='box')]
    home = (-2., -1.5)
    steps, distance = plan_reobservations(p, targets, (-2., 0.), home)
    assert len(steps) == 2 and math.isfinite(distance)
    done, view = steps[0]
    # Simulate arriving off the requested goal, then plan the remaining target.
    actual = (view.x+.05, view.y+.03)
    remaining = [t for t in targets if t is not done]
    revised, length = plan_reobservations(
        p, remaining, actual, home, [(view.x, view.y)])
    assert len(revised) == 1 and revised[0][0] is remaining[0]
    assert length > 0
    assert (revised[0][1].x, revised[0][1].y) != (view.x, view.y)
    assert not p._observed.any(), 'Planning alone is not new evidence'


def test_retry_plan_does_not_dispatch_blocked_views():
    steps, distance = plan_reobservations(
        planner(), [target()], (-2., 0.), (-2., -1.), allowed=lambda x, y: False)
    assert steps == [] and math.isfinite(distance)


def row():
    metadata = {'version': 2, 'observation_id': 'b'*32, 'patrol_id': 'a'*32,
                'map_id': 'map', 'simulated': True, 'captured_at': '2026-10-23T00:00:00Z',
                'camera_pose': {'x': -2., 'y': 0., 'yaw': 0}}
    analysis = {'objects': [{'name': '사람', 'kind': 'person', 'confidence': 'high',
                            'spatial': {'status': 'unknown', 'region': None,
                                        'reason': 'region_boundary', 'reobserve_hint': {
                                            'xy': [0., 0.],
                                            'purpose': 'aim_only_not_object_position'}}}]}
    return metadata, analysis


def test_unknown_furniture_people_and_boxes_generate_bounded_requests():
    m, a = row()
    assert len(unresolved_targets([(m, a)]*10)) == 1
    assert unresolved_targets([(m, a)])[0]['observation_id'] == m['observation_id']
    a['objects'][0]['spatial']['status'] = 'located'
    assert not unresolved_targets([(m, a)])
    a['objects'][0]['spatial']['status'] = 'unknown'
    a['objects'][0].update(kind='object', name='의자')
    assert len(unresolved_targets([(m, a)])) == 1
    a['objects'][0]['name'] = '빨간 상자'
    assert len(unresolved_targets([(m, a)])) == 1
    a['objects'][0].update(kind='box', name='붉은 직육면체')
    assert len(unresolved_targets([(m, a)])) == 1
    a['objects'][0]['spatial']['reobserve_hint']['xy'][0] = float('nan')
    assert not unresolved_targets([(m, a)])


def test_people_take_priority_without_exceeding_retry_budget():
    import copy
    m, a = row()
    chair = copy.deepcopy(a['objects'][0])
    chair.update(name='의자', kind='object')
    shelf = copy.deepcopy(chair)
    shelf['name'] = '선반'
    a['objects'] = [chair, shelf, a['objects'][0]]
    targets = unresolved_targets([(m, a)], maximum=2)
    assert [t['name'] for t in targets] == ['사람', '의자']


def test_feedback_requires_ready_owner_map_environment_and_sealed_metadata(tmp_path):
    m, a = row()
    for name in ('runs', 'captures'):
        (tmp_path / name).mkdir()
    contents = json.dumps(m).encode()
    capture = tmp_path / 'captures' / (m['observation_id']+'.json')
    capture.write_bytes(contents)
    (tmp_path / 'runs' / (m['patrol_id']+'.json')).write_text(json.dumps({
        'patrol_id': m['patrol_id'], 'captures': [{'observation_id': m['observation_id'],
                                                  'manifest_sha256': hashlib.sha256(
                                                     contents).hexdigest()}]}))
    db = sqlite3.connect(tmp_path / 'observations.sqlite3')
    db.executescript('CREATE TABLE owner(user_id TEXT); INSERT INTO owner VALUES ("owner");'
                     'CREATE TABLE observations(id TEXT,user_id TEXT,metadata TEXT,'
                     'analysis TEXT,status TEXT);')
    db.execute('INSERT INTO observations VALUES (?,?,?,?,?)',
               (m['observation_id'], 'owner', contents.decode(), json.dumps(a), 'pending'))
    db.commit()
    args = (tmp_path, m['patrol_id'], 'map', True)
    assert read_results(*args) is None
    db.execute('UPDATE observations SET status="ready"')
    db.commit()
    assert read_results(*args) == [(m, a)]
    assert read_results(tmp_path, m['patrol_id'], 'another-map', True) == []
    assert read_results(tmp_path, m['patrol_id'], 'map', False) == []
    capture.write_text('{}')
    assert read_results(*args) == []
    db.close()


def test_reobservation_captures_only_at_arrival_and_preserves_parent(tmp_path):
    recorder = PhotoRecorder(tmp_path)
    grid = planner().grid
    recorder.begin(grid, 'map', '', reobserve_of='a'*32, reobserve_round=1, capture_limit=1)
    kwargs = dict(x=-1., y=1., yaw=-.5, room_names=['촬영방'], simulated=True,
                  reobserve_target={'observation_id': 'b'*32, 'object_index': 0})
    assert not recorder.offer(camera_image(), phase='REPOSITIONING', **kwargs)
    assert recorder.offer(camera_image(), phase='REOBSERVING', **kwargs)
    recorder._last_capture = 0
    kwargs['x'] = -2.
    assert not recorder.offer(camera_image(), phase='REOBSERVING', **kwargs)
    recorder.close()
    path = next((tmp_path / 'captures').glob('[a-f0-9]'*32+'.json'))
    saved = json.loads(path.read_text())
    assert saved['reobserve_of'] == 'a'*32 and saved['reobserve_round'] == 1
    assert saved['reobserve_target']['observation_id'] == 'b'*32
    assert saved['capture_rooms'] == ['촬영방']
