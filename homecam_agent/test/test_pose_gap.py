"""Observed evidence and short-loss verification contracts, not medical accuracy."""
import copy
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from replay_leg_change import tracking_snapshot  # noqa: E402,F401
from experimental_leg_change import merge_requests  # noqa: E402
from experimental_pose_gap import PoseGapConfig, PoseGapExperiment  # noqa: E402
from homecam_detector.fall_candidate import FallCandidateDetector  # noqa: E402
from homecam_detector.pose import PersonPose, PoseKeypoint  # noqa: E402
from homecam_detector.pose_tracker import (  # noqa: E402
    PoseTrackingResult, TrackedPose, UnassignedPose,
)
from score_fall_baseline import events, case_score, separated_groups  # noqa: E402


def pose(low=True):
    points = ([(235, 282), (235, 315), (365, 282), (365, 315),
               (455, 284), (455, 317), (550, 286), (550, 320)] if low else
              [(260, 100), (300, 100), (264, 220), (296, 220),
               (265, 280), (295, 280), (266, 345), (294, 345)])
    box = (190, 250, 570, 350) if low else (220, 60, 340, 360)
    names = [f'{side}_{joint}' for joint in ('shoulder', 'hip', 'knee', 'ankle')
             for side in ('left', 'right')]
    return PersonPose(.8, (box[0]/640, box[1]/400, box[2]/640, box[3]/400),
                      tuple(PoseKeypoint(n, x/640, y/400, .9)
                            for n, (x, y) in zip(names, points)), 8)


class Replay:
    def __init__(self):
        self.base = FallCandidateDetector()
        self.exp = PoseGapExperiment()
        self.rows = []

    def step(self, frame, person=None, tid='a', state=None, unassigned=False,
             size=(640, 400), depth=None):
        track = TrackedPose(tid, state or ('tracked' if person else 'missing'),
                            person, 'strong', 3, 3, 0, ())
        unknown = (UnassignedPose(pose(), 'ambiguous'),) if unassigned else ()
        tracks = PoseTrackingResult((track,), unknown, ())
        base = self.base.update(tracks, capture_time=frame/12, image_size=size,
                                robot_motion='unknown', depth_by_track=depth)
        observations = ([dict(observation_index=0, pose=person.as_dict(), track_id=tid)]
                        if person else [])
        extra, details = self.exp.update(
            tracks, base, capture_time=frame/12, image_size=size,
            frame_index=frame, observations=observations)
        row = dict(case_id='X', frame_index=frame, timestamp_s=frame/12,
                   observations=observations, fall_analysis=dict(base, candidates=extra))
        self.rows.append(row)
        return extra, details, base


def three_low(replay):
    for frame in (0, 2, 5):
        assert not replay.step(frame, pose())[0]


def make_missing_request():
    r = Replay()
    three_low(r)
    assert not r.step(7)[0]
    result = r.step(10)[0]
    assert len(result) == 1
    return r, result[0]


def test_short_loss_is_last_seen_request_not_current_or_witnessed_fall():
    r, c = make_missing_request()
    assert c['candidateKind'] == 'found_down' and c['requiresVerification']
    assert c['reasons'] == ['low_pose_then_unobserved']
    assert c['observationAvailability'] == 'missing'
    assert c['emittedAtSec'] == 10/12
    assert c['evidenceEndSec'] == 5/12
    assert c['evidenceReference']['frameIndex'] == 5
    assert c['evidence']['temporal']['observedFrames'] == [0, 2, 5]
    assert c['evidence']['temporal']['observedSpanSec'] == pytest.approx(5/12)
    assert c['evidence']['temporal']['missingTimesSec'] == [7/12, 10/12]
    assert r.rows[-1]['observations'] == []
    e = events(r.rows)[0]
    assert e['evidence_frame_index'] == 5 and e['frame_index'] == 10
    assert e['association_mode'] == 'last_observed_not_current'
    assert not r.step(11)[0]  # no duplicate request for the same track


def test_missing_time_does_not_add_observed_duration_when_pose_returns():
    r = Replay()
    three_low(r)
    r.step(7)
    assert not r.step(10, pose())[0]
    assert not r.step(12, pose())[0]
    out, _, _ = r.step(14, pose())
    assert len(out) == 1
    assert out[0]['reasons'] == ['low_pose_reobserved_after_gap']
    assert out[0]['observationAvailability'] == 'observed'
    assert out[0]['evidence']['temporal']['observedSpanSec'] == pytest.approx(9/12)
    assert out[0]['evidenceEndSec'] == 14/12


def test_loss_only_variant_does_not_join_reobserved_postures():
    r = Replay()
    r.exp = PoseGapExperiment(PoseGapConfig(resume_after_missing=False))
    three_low(r)
    r.step(7)
    for frame in (10, 12, 14):
        assert not r.step(frame, pose())[0]
    assert r.exp.states['a'].observed_span() == pytest.approx(4/12)


def test_loss_only_variant_retains_the_explicit_last_seen_request():
    r = Replay()
    r.exp = PoseGapExperiment(PoseGapConfig(resume_after_missing=False))
    three_low(r)
    r.step(7)
    assert r.step(10)[0][0]['reasons'] == ['low_pose_then_unobserved']


@pytest.mark.parametrize('frames', [(0, 2), (0, 1, 2)])
def test_few_or_too_brief_actual_observations_cannot_be_completed_with_missing(frames):
    r = Replay()
    for f in frames:
        r.step(f, pose())
    for f in (4, 6):
        assert not r.step(f)[0]


@pytest.mark.parametrize('interrupt', [
    'ambiguous', 'unassigned', 'poor_pose', 'upright', 'new_id', 'large_gap', 'image_size',
])
def test_uncertain_association_or_other_posture_breaks_evidence(interrupt):
    r = Replay()
    three_low(r)
    if interrupt == 'ambiguous':
        r.step(7, state='ambiguous')
    elif interrupt == 'unassigned':
        r.step(7, unassigned=True)
    elif interrupt == 'poor_pose':
        r.step(7, replace(pose(), keypoints=(), visible_keypoints=0))
    elif interrupt == 'upright':
        r.step(7, pose(False))
    elif interrupt == 'new_id':
        r.step(7, pose(), tid='b')
    elif interrupt == 'image_size':
        r.step(7, size=(800, 600))
    else:
        r.step(12)
    assert not r.step(14)[0]
    assert not r.step(16)[0]


def test_normal_upright_or_measured_above_floor_is_not_low_pose():
    for person, depth in ((pose(False), None), (pose(), {'a': dict(
            usable=True, alignedToRgb=True, stale=False, validTorsoRatio=1.,
            sampledTorsoPoints=4, torsoFloorDistanceM=1.)})):
        r = Replay()
        for frame in (0, 2, 5):
            r.step(frame, person, depth=depth)
        assert not r.step(7)[0]
        assert not r.step(10)[0]


def test_failed_frame_or_clock_does_not_finish_pending_evidence():
    r = Replay()
    three_low(r)
    assert not r.step(5)[0]  # duplicate source stamp: unavailable
    assert not r.step(7)[0]
    assert not r.step(10)[0]


def test_existing_baseline_request_is_not_called_again_after_disappearance():
    r, requested, outputs = Replay(), {}, []
    for f in (0, 2, 5, 7, 10, 12, 14):
        extra, _, base = r.step(f, pose() if f <= 10 else None)
        calls, updates = merge_requests(base['candidates'], extra, requested)
        outputs.extend(calls)
    assert len(outputs) == 1
    assert updates[0]['requestCandidateId'] == outputs[0]['candidateId']


@pytest.mark.parametrize('kw', [
    dict(minimum_low_samples=1), dict(minimum_missing_samples=True),
    dict(max_missing_sec=3), dict(window_sec=float('nan')), dict(minimum_observed_span_sec=.8),
])
def test_invalid_settings_rejected(kw):
    with pytest.raises(ValueError):
        PoseGapConfig(**kw)


@pytest.mark.parametrize('corruption', [
    'wrong_time', 'future_frame', 'not_latest', 'current_pose', 'wrong_index', 'stale',
])
def test_historical_evidence_cannot_be_invented_or_carried_forward(corruption):
    r, _ = make_missing_request()
    rows = copy.deepcopy(r.rows)
    c = rows[-1]['fall_analysis']['candidates'][0]
    if corruption == 'wrong_time':
        c['evidenceReference']['timestampSec'] += .1
    elif corruption == 'future_frame':
        c['evidenceReference']['frameIndex'] = 10
    elif corruption == 'not_latest':
        c['evidenceReference']['frameIndex'] = 2
    elif corruption == 'current_pose':
        rows[-1]['observations'] = copy.deepcopy(rows[2]['observations'])
    elif corruption == 'wrong_index':
        c['evidenceReference']['observationIndex'] = 99
    else:
        rows[-1]['timestamp_s'] = 2
    with pytest.raises(ValueError):
        events(rows)


def test_request_after_onset_does_not_make_pre_onset_evidence_a_hit():
    r, _ = make_missing_request()
    e = events(r.rows)[0]
    case = dict(case_id='X', entry_state='standing', onset_frames=[6, 7])
    label = dict(label='observed_fall', source_path='X.mp4', expected_candidate_detection=True)
    audit = {e['key']: dict(association='target', note='RGB review')}
    scored = case_score(case, label, [e], audit, 12)
    assert scored['outcome'] == 'early_only'
    assert scored['events'][0]['position'] == 'after'
    assert scored['events'][0]['evidence_position'] == 'early'


def classified(label, cid, hit=False):
    outcome = 'target_candidate' if hit else 'no_output'
    first = dict(association_mode='current_observation') if hit else None
    return dict(case_id=cid, classification=label, output_count=int(hit), outcome=outcome,
                first_target_candidate=first)


def test_441_remains_ambiguous_not_a_fall_miss_or_normal_success():
    cases = [classified('observed_fall', 'fall', True), classified('observed_fall', 'miss'),
             classified('found_down', 'discovery', True),
             classified('suspected_fall', '441'), classified('suspected_fall', '207', True),
             classified('normal_activity', 'normal'), classified('normal_activity', 'false', True),
             classified('unobservable', 'hidden')]
    groups = separated_groups(cases)
    assert groups['observed_fall']['total'] == 2
    assert groups['observed_fall']['missed_cases'] == ['miss']
    assert groups['observed_fall']['candidate_coverage_bounds'] == [.5, .5]
    assert groups['found_down']['target_candidate_cases'] == ['discovery']
    assert groups['normal_activity']['unnecessary_candidate_rate'] == .5
    for label in ('suspected_fall', 'unobservable'):
        assert groups[label]['evaluation_role'] == 'descriptive_only'
        assert 'missed_cases' not in groups[label]
        assert 'candidate_coverage_bounds' not in groups[label]
    assert groups['suspected_fall']['cases_without_output'] == ['441']


def test_no_group_is_silently_dropped_and_zero_denominator_is_unknown():
    groups = separated_groups([])
    assert len(groups) == 5
    assert groups['observed_fall']['candidate_coverage_bounds'] == [None, None]
    assert groups['normal_activity']['unnecessary_candidate_rate'] is None
    with pytest.raises(ValueError):
        separated_groups([classified('invented', 'X')])


def test_same_number_of_negative_clips_does_not_hide_extra_requests():
    one = classified('normal_activity', '108', True)
    two = dict(one, output_count=2)
    before = separated_groups([one])['normal_activity']
    after = separated_groups([two])['normal_activity']
    assert before['unnecessary_candidate_rate'] == after['unnecessary_candidate_rate'] == 1
    assert before['output_count'] == 1 and after['output_count'] == 2
    assert after['multiple_output_cases'] == ['108']
