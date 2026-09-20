"""Changes after a sent snapshot must survive a normal/failed first answer."""
import copy
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_fall_recheck import (  # noqa: E402
    DROP, LOST, DropConfig, HorizontalDrop, IncidentRechecks, RecheckConfig,
    horizontal_descent,
)


def sample(time=0, frame=0, drop=0, translation=0):
    points = dict(nose=(.3, .3+drop), left_eye=(.28, .29+drop),
                  right_eye=(.32, .29+drop), left_shoulder=(.4, .33),
                  right_shoulder=(.43, .36), left_hip=(.65, .36), right_hip=(.68, .37))
    return dict(time=time, frame=frame, horizontal=True, box=[.2, .2, .8, .6],
                points={n: (x, y+translation) for n, (x, y) in points.items()})


def row(time, drop=0):
    s = sample(time, round(time*10), drop)
    features = dict(horizontal=True)
    observation = dict(track_id='T', observation_index=0, features=features,
                       pose=dict(boxConfidence=.3,
                                 box=dict(zip(('left', 'top', 'right', 'bottom'), s['box'])),
                                 keypoints=[dict(name=n, x=x, y=y, confidence=.9)
                                            for n, (x, y) in s['points'].items()]))
    return dict(case_id='C', frame_index=s['frame'], timestamp_s=time,
                observations=[observation], fall_analysis=dict(
                    status='ok', robotMotion='unknown', observationId=f'obs-{time}',
                    tracks=[dict(targetTrackId='T', trackingState='tracked', features=features)]))


def event(time=0, kind='found_down', reasons=(), rid='R', dispatch='update'):
    c = dict(candidateId=f'c-{time}', targetTrackId='T', candidateKind=kind,
             reasons=list(reasons), evidenceEndSec=time, emittedAtSec=time)
    return dict(request_id=rid, dispatch_kind=dispatch, dispatch_time_s=time,
                origin=dict(candidate=c))


def test_drop_from_lying_without_upright_requirement():
    engine = HorizontalDrop()
    assert engine.observe(row(0), (640, 400)) == []
    assert engine.observe(row(.2), (640, 400)) == []
    original = row(.6, .14)
    before = copy.deepcopy(original)
    c, = engine.observe(original, (640, 400))
    assert original == before
    assert c['candidateKind'] == 'fall_suspected'
    assert c['reasons'] == [DROP]
    assert c['evidenceStartSec'] == 0 and c['evidenceEndSec'] == .6
    assert c['requiresVerification'] and 'not_confirmed_fall' in c['uncertainties']
    assert engine.observe(row(.8, .15), (640, 400)) == []


@pytest.mark.parametrize('change', [
    'static', 'translation', 'upright', 'no_hip', 'no_head',
    'backward_time', 'scale_jump', 'tiny_drop'])
def test_drop_rejects_non_evidence(change):
    a, b = sample(), sample(.6, 6, .14)
    if change == 'static':
        b = sample(.6, 6)
    elif change == 'translation':
        b = sample(.6, 6, translation=.14)
    elif change == 'upright':
        a['horizontal'] = False
    elif change == 'no_hip':
        del b['points']['left_hip']
    elif change == 'no_head':
        del b['points']['nose']
    elif change == 'backward_time':
        b['time'] = -1
    elif change == 'scale_jump':
        b['box'][2] = .3
    else:
        b = sample(.6, 6, .01)
    assert horizontal_descent(a, b, DropConfig()) is None


@pytest.mark.parametrize('change', [
    'gap', 'moving', 'failed', 'id', 'case', 'resolution', 'ambiguous', 'weak_joint'])
def test_no_cross_track_or_interruption_evidence(change):
    engine = HorizontalDrop()
    engine.observe(row(0), (640, 400))
    engine.observe(row(.2), (640, 400))
    r, size = row(.6, .14), (640, 400)
    if change == 'gap':
        r = row(2, .14)
    elif change == 'moving':
        r['fall_analysis']['robotMotion'] = 'moving'
    elif change == 'failed':
        r['fall_analysis']['status'] = 'unavailable'
    elif change == 'id':
        r['observations'][0]['track_id'] = 'NEW'
    elif change == 'case':
        r['case_id'] = 'NEW'
    elif change == 'resolution':
        size = (1280, 720)
    elif change == 'ambiguous':
        r['fall_analysis']['tracks'][0]['trackingState'] = 'ambiguous'
    else:
        r['observations'][0]['pose']['keypoints'][0]['confidence'] = .1
    assert engine.observe(r, size) == []


def started():
    s = IncidentRechecks()
    s.ingest(event(dispatch='request'))
    first, = s.tick(0, 0, 0)
    return s, first


@pytest.mark.parametrize('outcome', ['normal', 'needs_check', 'failed'])
def test_late_change_survives_inflight_response(outcome):
    s, first = started()
    s.ingest(event(.5, 'fall_suspected', [DROP]))
    s.ingest(event(.7, reasons=[LOST]))
    assert s.tick(2, 20, 2) == []
    s.complete(first['call_id'], 3, outcome)
    call, = s.tick(3, 30, 3)
    assert call['incident_id'] == 'R' and call['sequence'] == 2
    assert {p['reason'] for p in call['changes']} == {'new_fall_motion', DROP, LOST}
    assert call['available_through_frame'] == 30
    assert s.tick(3.5, 35, 3.5) == []


def test_ordinary_updates_and_repeated_changes_do_not_flood():
    s, first = started()
    s.complete(first['call_id'], .1, 'normal')
    for t in (.2, .4, .6):
        s.ingest(event(t, reasons=['low_pose_reobserved_with_retained_evidence']))
        assert s.tick(t, round(t*10), t) == []
    s.ingest(event(1, reasons=[LOST]))
    assert s.tick(1.7, 17, 1.7) == []
    call, = s.tick(1.8, 18, 1.8)
    s.complete(call['call_id'], 2, 'normal')
    s.ingest(event(3, reasons=[LOST]))
    assert s.tick(4, 40, 4) == []


def test_continuous_stable_head_blocks_only_pose_loss_not_new_drop():
    s, first = started()
    s.complete(first['call_id'], .1, 'normal')
    s.ingest(event(.5, reasons=[LOST]), stable_head=True)
    assert s.tick(1.5, 15, 1.5) == []
    s.ingest(event(2, 'fall_suspected', [DROP]), stable_head=True)
    assert len(s.tick(3, 30, 3)) == 1


def test_limits_leave_unresolved_and_do_not_reclassify_normal():
    s = IncidentRechecks(RecheckConfig(maximum_rechecks=1))
    s.ingest(event(dispatch='request'))
    c, = s.tick(0, 0, 0)
    s.complete(c['call_id'], .1, 'normal')
    s.ingest(event(.5, reasons=[LOST]))
    c, = s.tick(2, 20, 2)
    s.complete(c['call_id'], 3, 'normal')
    s.ingest(event(4, 'fall_suspected', [DROP]))
    assert s.tick(5, 50, 5) == []
    assert s.incidents['R']['review_required']
    assert s.incidents['R']['pending']


def test_no_new_rgb_does_not_reuse_same_snapshot():
    s, first = started()
    s.ingest(event(.2, reasons=[LOST]))
    s.complete(first['call_id'], 2, 'normal')
    assert s.tick(3, 0, 0) == []
    assert s.incidents['R']['pending']


def test_eos_can_use_last_arrived_frame_after_slow_api():
    s, first = started()
    s.ingest(event(1, reasons=[LOST]))
    s.complete(first['call_id'], 6, 'normal')
    c, = s.tick(6, 50, 5)
    assert c['frame_time_s'] == 5 and c['dispatch_time_s'] == 6


def test_separate_incidents_and_invalid_completion():
    s, first = started()
    s.ingest(event(.1, rid='OTHER', dispatch='request'))
    other, = s.tick(.1, 1, .1)
    assert other['incident_id'] != first['incident_id']
    with pytest.raises(ValueError):
        s.complete('unknown', .2, 'normal')
    with pytest.raises(ValueError):
        s.tick(.1, 10, 1)


@pytest.mark.parametrize('cfg,kwargs', [
    (DropConfig(), {'minimum_references': 1}),
    (DropConfig(), {'window_s': float('nan')}),
    (RecheckConfig(), {'maximum_rechecks': True}),
    (RecheckConfig(), {'post_change_s': -1}),
])
def test_invalid_config(cfg, kwargs):
    with pytest.raises(ValueError):
        replace(cfg, **kwargs)


def test_fresh_later_drop_is_not_same_warning():
    s, first = started()
    s.complete(first['call_id'], .1, 'normal')
    s.ingest(event(.5, 'fall_suspected', [DROP]))
    c, = s.tick(2, 20, 2)
    s.complete(c['call_id'], 3, 'normal')
    s.ingest(event(4, 'fall_suspected', [DROP]))
    c, = s.tick(5, 50, 5)
    assert c['sequence'] == 3


def test_replay_waits_for_completion_and_keeps_final_rgb():
    from replay_fall_rechecks import replay_case
    r = row(0)
    first = event(dispatch='request')
    first.update(decision_frame_index=0)
    later = event(.5, 'fall_suspected', [DROP])
    result = replay_case([r], [first, later], dict(
        frames=11, fps=10, width=640, height=400), lambda c: (3.0, 'normal'))
    assert len(result['calls']) == 2
    assert result['calls'][1]['dispatch_time_s'] == 3
    assert result['calls'][1]['available_through_frame'] == 10
    assert result['calls'][1]['completed_at_s'] == 6


def test_terminal_scoring_is_not_best_of_or_no_call_normal():
    from replay_fall_rechecks import terminal_predictions
    labels = {'C': {}, 'D': {}}

    def record(label):
        return dict(case_id='C', incident_id='R', cleaned=dict(valid=True, prediction=dict(
            label=label, outcome='classified', explanation_ko='test')))

    records = [record('observed_fall'), record('normal_activity')]
    c, d = terminal_predictions(records, labels, True)
    assert c['prediction']['label'] == 'normal_activity'
    assert d['status'] == 'not_triggered' and not d['valid']
    records[-1]['cleaned'] = dict(valid=False, prediction=None)
    assert not terminal_predictions(records, labels, True)[0]['valid']
