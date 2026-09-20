"""Observed posture tolerance contracts. No model calls or medical-accuracy claims."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from experimental_pose_stability import PoseStabilityConfig, PoseStabilityExperiment
from replay_pose_stability import candidate_timing, replay_rows


def row(frame, kind='low', tid='a'):
    feature = dict(usable=kind != 'unusable', horizontal=kind == 'low', compact_body=False,
                   floor_height_m=None, near_floor=False, box_aspect=2.4,
                   body_spread_aspect=3., body_vertical_fraction=.5,
                   anchor_names=['left_shoulder', 'right_shoulder', 'left_hip', 'right_hip'],
                   uncertainties=[], torso_angle_deg=75 if kind == 'low' else 30)
    if kind == 'upright':
        feature.update(box_aspect=.5, body_spread_aspect=.4, body_vertical_fraction=.9)
    obs = dict(track_id=tid, observation_index=0, pose=dict(
        boxConfidence=.2, keypoints=[], box=dict(left=.1, top=.2, right=.8, bottom=.5)))
    return dict(case_id='X', frame_index=frame, timestamp_s=frame / 10,
                observations=[obs], verification_updates=[], pose_ms=1., pipeline_ms=2.,
                fall_analysis=dict(status='ok', observationId=f'frame-{frame}',
                                   robotMotion='unknown', unassignedCount=0, expiredTrackIds=[],
                                   candidates=[], tracks=[dict(targetTrackId=tid,
                                   trackingState='tracked', features=feature)]))


def step(exp, r, size=(640, 400)):
    before = copy.deepcopy(r)
    result = exp.update(r, image_size=size)
    assert r == before
    return result


def setup_history():
    exp = PoseStabilityExperiment()
    for f in (0, 2, 4):
        assert not step(exp, row(f))[0]
    return exp


def test_one_observed_disagreement_retains_history_but_not_positive_duration():
    exp = setup_history()
    assert not step(exp, row(6, 'disagreement'))[0]
    out, details = step(exp, row(8))
    assert not out
    assert details[0]['observed_low_span_sec'] == pytest.approx(.4)
    out, _ = step(exp, row(10))
    assert len(out) == 1
    c = out[0]
    assert c['candidateKind'] == 'found_down' and c['requiresVerification']
    assert c['observationAvailability'] == 'observed'
    assert c['observationId'] == 'frame-10'
    temporal = c['evidence']['temporal']
    assert temporal['lowFrames'] == [0, 2, 4, 8, 10]
    assert temporal['disagreementFrames'] == [6]
    assert temporal['observedLowSpanSec'] == pytest.approx(.6)
    assert temporal['lowFraction'] == pytest.approx(5 / 6)
    assert 'weak_pose_detection' in c['uncertainties']
    assert not step(exp, row(12))[0]


def test_no_synthetic_time_across_two_outlier_edges_even_after_long_wait():
    exp = setup_history()
    step(exp, row(8, 'disagreement'))
    out, details = step(exp, row(12))
    assert not out and details[0]['observed_low_span_sec'] == pytest.approx(.4)


def test_second_disagreement_resets_history():
    exp = setup_history()
    step(exp, row(6, 'disagreement'))
    step(exp, row(8, 'disagreement'))
    for f in (10, 12, 14, 16):
        assert not step(exp, row(f))[0]


@pytest.mark.parametrize('reason', ['missing', 'ambiguous', 'unusable', 'upright',
                                    'anchor_change', 'unassigned', 'measured_above_floor'])
def test_non_jitter_conditions_do_not_borrow_previous_low_evidence(reason):
    exp = setup_history()
    r = row(6, 'disagreement')
    d = r['fall_analysis']['tracks'][0]
    if reason == 'missing':
        r['observations'] = []
        d.update(trackingState='missing', features=None)
    elif reason == 'ambiguous':
        d['trackingState'] = 'ambiguous'
    elif reason == 'unusable':
        d['features']['usable'] = False
    elif reason == 'upright':
        r = row(6, 'upright')
    elif reason == 'anchor_change':
        d['features']['anchor_names'] = ['left_hip', 'left_shoulder']
    elif reason == 'unassigned':
        r['fall_analysis']['unassignedCount'] = 1
    else:
        d['features'].update(floor_height_m=1., near_floor=False)
    step(exp, r)
    for f in (8, 10, 12):
        assert not step(exp, row(f))[0]


@pytest.mark.parametrize('reason', ['clock', 'frame', 'gap', 'size', 'id'])
def test_discontinuous_stream_cannot_complete_previous_evidence(reason):
    exp = setup_history()
    r = row(6, 'disagreement')
    size = (640, 400)
    if reason == 'clock':
        r['timestamp_s'] = .4
    elif reason == 'frame':
        r['frame_index'] = 4
    elif reason == 'gap':
        r = row(10, 'disagreement')
    elif reason == 'size':
        size = (800, 600)
    else:
        r = row(6, 'disagreement', 'b')
    step(exp, r, size)
    for f in (12, 14, 16):
        assert not step(exp, row(f), size)[0]


def test_not_an_extra_trigger_for_continuously_low_or_upright_person():
    for kind in ['low', 'upright', 'disagreement']:
        exp = PoseStabilityExperiment()
        for f in range(0, 40, 2):
            assert not step(exp, row(f, kind))[0]


def test_window_expiration_drops_old_disagreement():
    exp = setup_history()
    step(exp, row(6, 'disagreement'))
    for f in range(8, 32, 2):
        r = row(f, 'disagreement' if f < 28 else 'low')
        assert not step(exp, r)[0]


@pytest.mark.parametrize('field', ['body_spread_aspect', 'body_vertical_fraction', 'box_aspect'])
@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), '2', True])
def test_bad_geometry_is_not_an_allowed_disagreement(field, value):
    exp = setup_history()
    r = row(6, 'disagreement')
    r['fall_analysis']['tracks'][0]['features'][field] = value
    step(exp, r)
    for f in (8, 10, 12):
        assert not step(exp, row(f))[0]


def test_duplicate_observation_fails_instead_of_selecting_rank_one():
    exp = setup_history()
    r = row(6)
    r['observations'].append(copy.deepcopy(r['observations'][0]))
    with pytest.raises(ValueError, match='duplicate'):
        step(exp, r)


def test_two_people_keep_independent_evidence():
    exp = setup_history()
    for f in (6, 8, 10):
        r = row(f, 'disagreement' if f == 6 else 'low')
        other = row(f, 'upright', 'b')
        r['observations'] += other['observations']
        r['fall_analysis']['tracks'] += other['fall_analysis']['tracks']
        out, _ = step(exp, r)
    assert len(out) == 1 and out[0]['targetTrackId'] == 'a'


@pytest.mark.parametrize('kw', [dict(window_sec=True), dict(window_sec=float('nan')),
    dict(minimum_low_samples=2), dict(maximum_disagreements=2),
    dict(minimum_low_fraction=.5), dict(minimum_low_fraction=1.1),
    dict(maximum_body_vertical_fraction=2), dict(minimum_low_span_sec=3),
    dict(max_sample_gap_sec=3), dict(minimum_body_aspect=1)])
def test_invalid_configuration_rejected(kw):
    with pytest.raises(ValueError):
        PoseStabilityConfig(**kw)


def test_existing_candidate_and_later_evidence_survive_without_duplicate_request():
    rows = [row(f, 'disagreement' if f == 6 else 'low') for f in range(0, 14, 2)]
    original = dict(candidateId='original', revision=1, candidateKind='fall_suspected',
                    targetTrackId='a', requiresVerification=True)
    rows[2]['fall_analysis']['candidates'] = [original]
    out = list(replay_rows(rows, {'X': dict(width=640, height=400)}, PoseStabilityConfig(), True))
    assert [c for r in out for c in r['fall_analysis']['candidates']] == [original]
    update, = [u for r in out for u in r['verification_updates']]
    assert update['requestCandidateId'] == 'original'
    assert update['evidence']['candidateKind'] == 'found_down'
    assert update['evidence']['evidenceEndSec'] == 1.


def test_control_has_no_new_candidate_and_input_is_unchanged():
    rows = [row(f, 'disagreement' if f == 6 else 'low') for f in range(0, 14, 2)]
    before = copy.deepcopy(rows)
    out = list(replay_rows(rows, {'X': dict(width=640, height=400)}, PoseStabilityConfig(), False))
    assert not any(r['fall_analysis']['candidates'] or r['stability_raw_candidates'] for r in out)
    assert rows == before


def test_new_approved_timing_without_entry_state_is_supported():
    out = candidate_timing(dict(onset_frames=[12, 18], first_down_frames=None), 2., 12)
    assert out == dict(basis='onset', delay_s=[.5, 1.], position='after')
    early = candidate_timing(dict(onset_frames=[12, 18]), .5, 12)
    assert early['position'] == 'early' and early['delay_s'] == [-1., -.5]


def test_missing_timing_is_unknown_not_zero_and_already_down_uses_discovery():
    assert candidate_timing({}, 2., 12)['delay_s'] is None
    out = candidate_timing(dict(entry_state='already_down', first_down_frames=[0, 0],
                                 onset_frames=[12, 24]), .2, 12)
    assert out == dict(basis='first_down', delay_s=[.2, .2], position='after')
