"""Retention requests use real observations, not imputed posture or identity."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from experimental_pose_retention import PoseRetentionConfig, PoseRetentionExperiment  # noqa: E402


def features(kind='low'):
    out = dict(usable=True, body_points=8, box_confidence=.8, near_floor=False,
               floor_height_m=None, horizontal=True, compact_body=False,
               torso_angle_deg=70., box_aspect=1.5, body_spread_aspect=1.8,
               box_height=.4, uncertainties=['floor_distance_unavailable'],
               anchor_names=['left_hip', 'left_shoulder', 'right_hip', 'right_shoulder'])
    if kind == 'support':
        out.update(horizontal=False, torso_angle_deg=44., box_aspect=2.2,
                   body_spread_aspect=2., box_height=.3)
    elif kind == 'upright':
        out.update(horizontal=False, torso_angle_deg=5., box_aspect=.5,
                   body_spread_aspect=.5, box_height=.8)
    elif kind == 'poor':
        out.update(usable=False)
    return out


def row(frame, kind='low', tid='a', transition=0):
    f = features(kind) if kind is not None else None
    obs = [] if f is None else [dict(
        track_id=tid, observation_index=0, features=copy.deepcopy(f),
        pose=dict(boxConfidence=.8, keypoints=[],
                  box=dict(left=.1, top=.1, right=.5, bottom=.5)))]
    diag = dict(targetTrackId=tid, features=f, trackingState='tracked' if f else 'missing',
                transitionSamples=transition)
    return dict(case_id='X', timestamp_s=frame/10, frame_index=frame, observations=obs,
                fall_analysis=dict(status='ok', unassignedCount=0, tracks=[diag],
                                   observationId=f'obs-{frame}', robotMotion='unknown',
                                   candidates=[]))


def step(exp, frame, kind='low', **kwargs):
    return exp.update(row(frame, kind, **kwargs), image_size=(640, 400))


def gaps(exp):
    for frame, kind in [(0, 'low'), (2, None), (4, None), (6, 'low'), (8, None)]:
        assert step(exp, frame, kind)[0] == []
    return step(exp, 10)[0]


def test_reobserved_low_does_not_count_gap_as_observed_time():
    exp = PoseRetentionExperiment()
    result = gaps(exp)
    assert len(result) == 1
    c = result[0]
    assert c['candidateKind'] == 'found_down'
    assert c['observationAvailability'] == 'observed' and c['requiresVerification']
    assert c['observationId'] == 'obs-10'
    assert c['emittedAtSec'] == c['evidenceEndSec'] == 1.
    temporal = c['evidence']['temporal']
    assert [s['frame'] for s in temporal['observedSamples']] == [0, 6, 10]
    assert temporal['evidenceWindowSpanSec'] == 1.
    assert temporal['observedAdjacentSpanSec'] == 0.
    assert [s['frame'] for s in temporal['missingSamples']] == [2, 4, 8]
    assert 'continuous_low_posture_unproven' in c['uncertainties']
    assert 'camera_motion_uncompensated' in c['uncertainties']
    assert step(exp, 12)[0] == []


def test_seed_then_two_actual_supporting_poses_can_request_review():
    exp = PoseRetentionExperiment()
    assert step(exp, 0, transition=1)[0] == []
    assert step(exp, 2, 'support')[0] == []
    result = step(exp, 4, 'support')[0]
    assert len(result) == 1
    c = result[0]
    assert c['candidateKind'] == 'fall_suspected' and c['requiresVerification']
    assert c['reasons'] == ['rapid_change_then_observed_support']
    temporal = c['evidence']['temporal']
    assert [s['frame'] for s in temporal['observedSamples']] == [0, 2, 4]
    assert temporal['currentLowPostureConfirmed'] is False
    assert c['evidence']['pose']['horizontal'] is False
    assert temporal['observedAdjacentSpanSec'] == pytest.approx(.4)
    assert step(exp, 6, 'support')[0] == []


def test_seed_with_missing_frame_requires_two_real_followups():
    exp = PoseRetentionExperiment()
    step(exp, 0, transition=1)
    assert step(exp, 2, None)[0] == []
    assert step(exp, 4, 'support')[0] == []
    c = step(exp, 6, 'support')[0][0]
    assert c['evidence']['temporal']['observedAdjacentSpanSec'] == pytest.approx(.2)
    assert [s['frame'] for s in c['evidence']['temporal']['observedSamples']] == [0, 4, 6]


@pytest.mark.parametrize('kind', ['low', 'support', 'upright', 'poor', None])
def test_no_seed_and_no_reobserved_low_cannot_create_requests(kind):
    exp = PoseRetentionExperiment()
    for frame in range(0, 30, 2):
        assert step(exp, frame, kind)[0] == []


def test_missing_alone_does_not_finish_one_observation():
    exp = PoseRetentionExperiment()
    step(exp, 0, transition=1)
    for frame in range(2, 20, 2):
        assert step(exp, frame, None)[0] == []


@pytest.mark.parametrize('mode', ['gap', 'transition'])
@pytest.mark.parametrize('interruption', [
    'poor', 'upright', 'ambiguous', 'unassigned', 'anchors', 'new_id',
    'size', 'failed', 'clock', 'stream_gap', 'above_floor', 'case',
])
def test_interruption_cannot_borrow_old_evidence(mode, interruption):
    exp = PoseRetentionExperiment()
    step(exp, 0, transition=1 if mode == 'transition' else 0)
    if mode == 'gap':
        step(exp, 2, None)
        step(exp, 4)
        middle, last = 6, 8
    else:
        middle, last = 2, 4
    current = row(middle, 'support' if mode == 'transition' else 'low')
    size = (640, 400)
    if interruption in {'poor', 'upright'}:
        current = row(middle, interruption)
    elif interruption == 'ambiguous':
        current['fall_analysis']['tracks'][0]['trackingState'] = 'ambiguous'
    elif interruption == 'unassigned':
        current['fall_analysis']['unassignedCount'] = 1
    elif interruption == 'anchors':
        current['fall_analysis']['tracks'][0]['features']['anchor_names'].pop()
    elif interruption == 'new_id':
        current = row(middle, tid='b')
    elif interruption == 'size':
        size = (800, 600)
    elif interruption == 'failed':
        current['fall_analysis']['status'] = 'unavailable'
    elif interruption == 'clock':
        current = row(0)
    elif interruption == 'stream_gap':
        current = row(middle+10)
        last = middle+12
    elif interruption == 'above_floor':
        current['fall_analysis']['tracks'][0]['features']['floor_height_m'] = 1.
    elif interruption == 'case':
        current['case_id'] = 'Y'
    assert exp.update(current, image_size=size)[0] == []
    assert step(exp, last, 'support' if mode == 'transition' else 'low')[0] == []


def test_retained_low_expires_after_missing_timeout():
    exp = PoseRetentionExperiment()
    for frame, kind in [(0, 'low'), (2, None), (4, 'low'), (6, None),
                        (8, None), (10, None), (12, None), (14, 'low')]:
        assert step(exp, frame, kind)[0] == []


def test_unsupported_pose_cannot_complete_transition():
    for change in [dict(body_points=4), dict(torso_angle_deg=10.),
                   dict(box_aspect=.8), dict(body_spread_aspect=.8), dict(box_height=.8)]:
        exp = PoseRetentionExperiment()
        step(exp, 0, transition=1)
        current = row(2, 'support')
        current['fall_analysis']['tracks'][0]['features'].update(change)
        assert exp.update(current, image_size=(640, 400))[0] == []
        assert step(exp, 4, 'support')[0] == []


def test_expired_track_removes_history():
    exp = PoseRetentionExperiment()
    step(exp, 0, transition=1)
    empty = row(2, None)
    empty['fall_analysis']['tracks'] = []
    exp.update(empty, image_size=(640, 400))
    assert not exp.states
    for frame in (4, 6):
        assert step(exp, frame, 'support')[0] == []


def test_observations_from_different_tracks_do_not_combine():
    exp = PoseRetentionExperiment()
    step(exp, 0)
    step(exp, 2, None)
    step(exp, 4, tid='b')
    step(exp, 6, None, tid='b')
    assert step(exp, 8, tid='a')[0] == []


def test_no_mutation_or_gt_dependent_branch():
    exp = PoseRetentionExperiment()
    a, b = row(0, transition=1), row(0, transition=1)
    a['label'], a['boxes'] = 'fall', [[0, 0, 100, 100]]
    b['label'], b['boxes'] = 'normal', []
    before = copy.deepcopy(a)
    other = PoseRetentionExperiment()
    assert exp.update(a, image_size=(640, 400)) == other.update(b, image_size=(640, 400))
    assert a == before


def test_separate_modes_and_control():
    disabled = PoseRetentionConfig(reobserved_low=False, transition_followup=False)
    assert gaps(PoseRetentionExperiment(disabled)) == []
    assert gaps(PoseRetentionExperiment(replace(disabled, transition_followup=True))) == []
    assert len(gaps(PoseRetentionExperiment(replace(disabled, reobserved_low=True)))) == 1
    exp = PoseRetentionExperiment(replace(disabled, reobserved_low=True))
    step(exp, 0, transition=1)
    step(exp, 2, 'support')
    assert step(exp, 4, 'support')[0] == []


@pytest.mark.parametrize('kwargs', [
    dict(reobserved_low=1), dict(maximum_missing_sec=float('nan')),
    dict(minimum_low_observations=2), dict(minimum_followup_observations=True),
    dict(maximum_frame_gap_sec=1.), dict(minimum_evidence_window_sec=3.),
    dict(minimum_followup_span_sec=1.), dict(minimum_followup_torso_deg=90.),
])
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        PoseRetentionConfig(**kwargs)


def test_duplicate_track_observation_rejected():
    current = row(0)
    current['observations'].append(copy.deepcopy(current['observations'][0]))
    with pytest.raises(ValueError):
        PoseRetentionExperiment().update(current, image_size=(640, 400))


def replay_input():
    result = []
    for frame, kind in [(0, 'low'), (2, None), (4, None), (6, 'low'), (8, None),
                        (10, 'low')]:
        current = row(frame, kind)
        current['fall_analysis']['tracks'][0]['status'] = 'no_candidate'
        current.update(baseline_analysis=copy.deepcopy(current['fall_analysis']),
                       verification_updates=[], verification_routes={})
        result.append(current)
    return result


def test_replay_control_preserves_existing_semantics():
    from replay_pose_retention import MODES, replay_rows
    from replay_pose_stability import semantic
    rows = replay_input()
    before = copy.deepcopy(rows)
    result = list(replay_rows(rows, {'X': dict(width=640, height=400)}, MODES['control']))
    assert rows == before
    assert [semantic(r) for r in result] == [semantic(r) for r in rows]
    assert not any(r['retention_raw_candidates'] for r in result)


def test_existing_request_receives_new_evidence_without_duplicate_call():
    from replay_pose_retention import replay_rows
    from replay_request_dedup import payloads
    rows = replay_input()
    existing = dict(candidateId='existing', revision=1, targetTrackId='a',
                    candidateKind='found_down', evidence=dict(original='preserve this'))
    rows[0]['fall_analysis']['candidates'] = [existing]
    result = list(replay_rows(rows, {'X': dict(width=640, height=400)}, PoseRetentionConfig()))
    requests = [c for r in result for c in r['fall_analysis']['candidates']]
    assert requests == [existing]
    last = result[-1]
    assert len(last['verification_updates']) == 1
    assert last['verification_updates'][0]['requestCandidateId'] == 'existing'
    assert payloads(last['fall_analysis']['candidates'], last['verification_updates']) == payloads(
        last['retention_raw_candidates'], [])


@pytest.mark.parametrize('include_request_frame', [True, False])
def test_scoring_uses_all_reviewed_frames_without_interpolating(tmp_path, include_request_frame):
    from replay_pose_retention import additional_evidence, replay_rows
    rows = replay_input()
    meta = dict(case_id='X', width=640, height=400, fps=10)
    result = list(replay_rows(rows, {'X': meta}, PoseRetentionConfig()))
    frames = [0, 6, 10] if include_request_frame else [0, 6]
    person = dict(person_id='P01', role='target',
                  boxes=[[f, 64, 40, 320, 200] for f in frames])
    annotation = dict(case_id='X', target_person_id='P01', onset_frames=[0, 1], persons=[person])
    bundle = dict(
        classifications=dict(cases=[dict(case_id='X', label='observed_fall', review_case_id='X')]),
        annotations=dict(cases=[annotation]))
    (tmp_path/'evaluation_labels.json').write_text(json.dumps(bundle))
    (tmp_path/'media.json').write_text(json.dumps(dict(cases=[meta])))
    overlay = dict(match=dict(visible_coverage=.5, prediction_coverage=.25, margin=.1))
    evidence = additional_evidence(result, tmp_path, overlay)
    assert len(evidence) == 1 and evidence[0]['frame_index'] == 10
    assert evidence[0]['exact_frame_target_match'] is (True if include_request_frame else None)
    assert evidence[0]['timing']['delay_s'] == [.9, 1.]
