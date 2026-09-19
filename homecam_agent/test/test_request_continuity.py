"""Conservative routing proposal: retain evidence and never infer normality."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_request_continuity import ContinuityConfig, ContinuityProbe  # noqa: E402


def observation(tid='old', *, head_only=False, shift=0, horizontal=True):
    points = dict(nose=(.60, .40), left_eye=(.59, .38), right_eye=(.62, .38),
                  left_shoulder=(.55, .45), right_shoulder=(.55, .47),
                  left_hip=(.35, .45), right_hip=(.35, .47))
    if head_only:
        points = {n: p for n, p in points.items() if n in ('nose', 'left_eye', 'right_eye')}
    box = (.55, .35, .65, .43) if head_only else (.20, .30, .75, .60)
    shifted_box = dict(zip(('left', 'top', 'right', 'bottom'),
                           (box[0]+shift, box[1], box[2]+shift, box[3])))
    return dict(track_id=tid, features=dict(horizontal=horizontal, usable=not head_only),
                pose=dict(box=shifted_box,
                          keypoints=[dict(name=n, x=x+shift, y=y, confidence=.95)
                                     for n, (x, y) in points.items()]))


def row(frame, observations, *, time=None, status='ok'):
    return dict(frame_index=frame, timestamp_s=frame/10 if time is None else time,
                observations=observations, fall_analysis=dict(status=status, tracks=[
                    dict(targetTrackId=o['track_id'], trackingState='tracked')
                    for o in observations if o['track_id'] is not None]))


def request(frame, tid='old', *, kind='found_down', motion='unknown'):
    return dict(request_id='request-'+tid, dispatch_kind='request',
                decision_frame_index=frame, dispatch_time_s=frame/10,
                origin=dict(candidate=dict(
                    targetTrackId=tid, candidateKind=kind, evidence=dict(robotMotion=motion))))


def setup():
    e = ContinuityProbe()
    e.observe(row(0, [observation()]), (640, 400))
    assert e.route(request(0))['action'] == 'request'
    return e


def continuation(e):
    for f in (2, 4):
        e.observe(row(f, [observation('face', head_only=True)]), (640, 400))
    for f in (6, 8, 10):
        e.observe(row(f, [observation('new')]), (640, 400))
    return e.route(request(10, 'new'))


def test_id_change_with_continuous_head_and_repeated_body():
    result = continuation(setup())
    assert result['action'] == 'attach_evidence'
    assert result['parent_request_id'] == 'request-old'
    assert result['proof']['restored_body_frames'] == [6, 8, 10]
    assert not result['proof']['identity_verified']
    assert not result['proof']['normality_inferred']
    assert result['original'] == request(10, 'new')


@pytest.mark.parametrize('issue', [
    'missing', 'other_head', 'upright', 'ambiguous',
    'clock_backwards', 'long_gap', 'size', 'bad_status'])
def test_interruption_never_borrows_old_identity(issue):
    e = setup()
    bad = row(1, [observation()])
    size = (640, 400)
    if issue == 'missing':
        bad = row(1, [])
    elif issue == 'other_head':
        bad = row(1, [observation(), observation('other', shift=-.1)])
    elif issue == 'upright':
        bad = row(1, [observation(horizontal=False)])
    elif issue == 'ambiguous':
        bad['fall_analysis']['tracks'][0]['trackingState'] = 'ambiguous'
    elif issue == 'clock_backwards':
        bad['timestamp_s'] = -.1
    elif issue == 'long_gap':
        bad['timestamp_s'] = .5
    elif issue == 'size':
        size = (1280, 800)
    else:
        bad['fall_analysis']['status'] = 'error'
    e.observe(bad, size)
    assert continuation(e)['action'] == 'request'


def test_new_fall_always_gets_request():
    e = setup()
    continuation(e)
    assert e.route(request(10, 'new', kind='fall_suspected'))['action'] == 'request'


def test_known_camera_motion_always_gets_request():
    e = setup()
    continuation(e)
    assert e.route(request(10, 'new', motion='moving'))['action'] == 'request'


def test_only_one_body_observation_not_enough():
    e = setup()
    e.observe(row(2, [observation('new')]), (640, 400))
    assert e.route(request(2, 'new'))['action'] == 'request'


def test_two_possible_roots_not_arbitrarily_ranked():
    e = setup()
    e.roots['another'] = copy.deepcopy(e.roots['request-old'])
    assert continuation(e)['action'] == 'request'


def test_expired_incident_not_reused():
    e = setup()
    for f in range(2, 54, 2):
        e.observe(row(f, [observation('new')]), (640, 400))
    assert e.route(request(52, 'new'))['action'] == 'request'


def test_weak_fragment_alone_never_proves_continuity():
    e = setup()
    part = observation('face', head_only=True)
    part['pose']['keypoints'][0]['confidence'] = .1
    e.observe(row(1, [part]), (640, 400))
    assert not e.roots


def test_weak_fragment_does_not_conflict_with_independent_complete_head():
    e = setup()
    part = observation('face', head_only=True)
    part['pose']['keypoints'][0]['confidence'] = .1
    e.observe(row(1, [observation(), part]), (640, 400))
    assert len(e.roots) == 1
    assert continuation(e)['action'] == 'attach_evidence'


def test_original_evidence_and_ids_unchanged():
    e = setup()
    data = row(2, [observation('new')])
    q = request(2, 'new')
    before = copy.deepcopy((data, q))
    e.observe(data, (640, 400))
    e.route(q)
    assert (data, q) == before


def test_duplicate_track_assignment_rejected():
    e = setup()
    with pytest.raises(ValueError, match='duplicate observation'):
        e.observe(row(2, [observation(), observation()]), (640, 400))


def test_error_status_cannot_seed_new_incident():
    e = ContinuityProbe()
    e.observe(row(0, [observation()], status='error'), (640, 400))
    assert e.route(request(0))['action'] == 'request'
    assert not e.roots


@pytest.mark.parametrize('kwargs', [dict(max_age_sec=6), dict(min_body_samples=1),
                                    dict(min_box_iou=2), dict(max_body_gap_sec=-1)])
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        ContinuityConfig(**kwargs)
