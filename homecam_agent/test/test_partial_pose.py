"""Partial observation requests must preserve uncertainty and source evidence."""
import copy
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_partial_pose import (  # noqa: E402
    PartialConfig, PartialPoseExperiment, descent_evidence, geometry, partial_upper,
)


def point(name, x, y, confidence=.9):
    return dict(name=name, x=x, y=y, confidence=confidence)


def row(frame, tid='one'):
    points = [point('left_shoulder', .65, .43), point('right_shoulder', .62, .38),
              point('left_elbow', .60, .47), point('right_elbow', .56, .43),
              point('left_wrist', .52, .46), point('right_wrist', .51, .47),
              point('left_ear', .59, .44)]
    features = dict(usable=False, near_floor=False, floor_height_m=None,
                    uncertainties=['insufficient_body_keypoints'])
    obs = dict(track_id=tid, observation_index=0, features=copy.deepcopy(features),
               pose=dict(boxConfidence=.8, keypoints=points,
                         box=dict(left=.48, top=.32, right=.73, bottom=.51)))
    return dict(case_id='C', timestamp_s=frame/10, frame_index=frame, observations=[obs],
                fall_analysis=dict(
                    status='ok', unassignedCount=0, robotMotion='unknown',
                    observationId=f'obs-{frame}', candidates=[], tracks=[dict(
                        targetTrackId=tid, trackingState='tracked', features=features)]))


def step(engine, frame, tid='one'):
    return engine.update(row(frame, tid), image_size=(640, 400))[0]


def test_partial_is_request_not_complete_pose_or_fall_claim():
    engine = PartialPoseExperiment()
    for f in (0, 2, 4):
        assert step(engine, f) == []
    original = row(6)
    before = copy.deepcopy(original)
    candidate = engine.update(original, image_size=(640, 400))[0][0]
    assert original == before
    assert candidate['reasons'] == ['partial_upper_body_needs_review']
    assert candidate['candidateKind'] == 'found_down'
    assert candidate['requiresVerification']
    assert candidate['evidence']['pose']['usable'] is False
    assert candidate['evidenceEndSec'] == candidate['emittedAtSec'] == .6
    assert [s['frame'] for s in candidate['evidence']['temporal']['observedSamples']] == [
        0, 2, 4, 6]
    assert 'camera_motion_uncompensated' in candidate['uncertainties']
    assert step(engine, 8) == []


@pytest.mark.parametrize('change', ['weak', 'no_head', 'no_arm', 'duplicate', 'nan', 'upright'])
def test_partial_rejects_box_or_confidence_alone(change):
    obs = row(0)['observations'][0]
    if change == 'weak':
        obs['pose']['boxConfidence'] = .2
    elif change == 'no_head':
        obs['pose']['keypoints'] = obs['pose']['keypoints'][:-1]
    elif change == 'no_arm':
        obs['pose']['keypoints'] = [p for p in obs['pose']['keypoints']
                                    if not p['name'].endswith('wrist')]
    elif change == 'duplicate':
        obs['pose']['keypoints'].append(obs['pose']['keypoints'][0])
    elif change == 'nan':
        obs['pose']['boxConfidence'] = float('nan')
    else:
        obs['pose']['box'].update(top=.01, bottom=.99)
    sample = geometry(obs, PartialConfig())
    assert sample is None or not partial_upper(sample, (640, 400))


@pytest.mark.parametrize('change', ['missing', 'ambiguous', 'failed', 'clock', 'gap', 'new_id'])
def test_interrupted_evidence_not_counted_as_observation(change):
    engine = PartialPoseExperiment()
    step(engine, 0)
    step(engine, 2)
    r = row(4)
    if change == 'missing':
        r['observations'] = []
        r['fall_analysis']['tracks'][0]['trackingState'] = 'missing'
    elif change == 'ambiguous':
        r['fall_analysis']['tracks'][0]['trackingState'] = 'ambiguous'
    elif change == 'failed':
        r['fall_analysis']['status'] = 'unavailable'
    elif change == 'clock':
        r = row(0)
    elif change == 'gap':
        r = row(20)
    else:
        r = row(4, 'two')
    assert engine.update(r, image_size=(640, 400))[0] == []
    assert step(engine, 6 if change != 'gap' else 22) == []


def test_ids_do_not_share_history():
    engine = PartialPoseExperiment()
    for f in (0, 2, 4, 6, 8):
        assert step(engine, f, 'a' if f % 4 == 0 else 'b') == []


def test_unassigned_other_person_does_not_erase_unique_assigned_track():
    engine = PartialPoseExperiment()
    for f in (0, 2, 4, 6):
        r = row(f)
        r['observations'].append(dict(row(f, None)['observations'][0], observation_index=1))
        r['observations'][-1]['pose']['box'] = dict(left=.01, top=.01, right=.2, bottom=.2)
        r['fall_analysis']['unassignedCount'] = 1
        result = engine.update(r, image_size=(640, 400))[0]
    assert len(result) == 1 and result[0]['targetTrackId'] == 'one'


def test_descent_requires_shared_joints_real_drop_and_time_order():
    names = ['left_shoulder', 'right_shoulder', 'left_hip', 'right_hip', 'left_wrist']
    a = dict(box=[.3, .1, .5, .9], time=0, frame=0,
             points={n: (.4, .2+i*.08) for i, n in enumerate(names)})
    b = dict(box=[.25, .55, .75, .8], time=.6, frame=6,
             points={n: (.4, .6+i*.03) for i, n in enumerate(names)})
    proof = descent_evidence(a, b, (640, 400))
    assert proof and proof['referenceFrame'] == 0
    assert descent_evidence(a, dict(b, time=0), (640, 400)) is None
    assert descent_evidence(a, dict(b, points={'left_hip': (.4, .6)}), (640, 400)) is None
    assert descent_evidence(a, dict(b, points=a['points']), (640, 400)) is None


@pytest.mark.parametrize('kwargs', [
    dict(partial_samples=2), dict(descent_samples=True),
    dict(window_sec=float('nan')), dict(minimum_detection=2), dict(partial_upper=1)])
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        replace(PartialConfig(), **kwargs)


def test_disabled_does_not_request():
    engine = PartialPoseExperiment(PartialConfig(partial_upper=False, observed_descent=False))
    for frame in range(0, 20, 2):
        assert step(engine, frame) == []


@pytest.mark.parametrize('change', ['overlap_unassigned', 'above_floor', 'new_case', 'size'])
def test_uncertain_identity_or_contradictory_depth_resets_evidence(change):
    engine = PartialPoseExperiment()
    step(engine, 0)
    step(engine, 2)
    r, size = row(4), (640, 400)
    if change == 'overlap_unassigned':
        r['observations'].append(dict(row(4, None)['observations'][0], observation_index=1))
    elif change == 'above_floor':
        r['fall_analysis']['tracks'][0]['features']['floor_height_m'] = .9
    elif change == 'new_case':
        r['case_id'] = 'OTHER'
    else:
        size = (1280, 720)
    assert engine.update(r, image_size=size)[0] == []
    assert step(engine, 6) == []


@pytest.mark.parametrize('count,span,end', [(3, .3, 8), (2, .15, 6)])
def test_actual_descent_sequence_requests_without_fabricating_final_pose(count, span, end):
    engine = PartialPoseExperiment(PartialConfig(partial_upper=False,
                                                 descent_samples=count, descent_span_sec=span))
    names = ['left_shoulder', 'right_shoulder', 'left_hip', 'right_hip', 'left_wrist']
    results = []
    for frame in range(0, end+1, 2):
        r = row(frame)
        pose = r['observations'][0]['pose']
        if frame < 4:
            pose['box'] = dict(left=.3, top=.1, right=.5, bottom=.9)
            pose['keypoints'] = [point(n, .4, .2+i*.08) for i, n in enumerate(names)]
        else:
            pose['box'] = dict(left=.25, top=.55, right=.75, bottom=.8)
            pose['keypoints'] = [point(n, .4, .6+i*.03) for i, n in enumerate(names)]
        results.extend(engine.update(r, image_size=(640, 400))[0])
    assert len(results) == 1
    candidate = results[0]
    assert candidate['candidateKind'] == 'fall_suspected'
    assert candidate['emittedAtSec'] == end/10
    assert candidate['evidence']['pose']['usable'] is False
    assert [s['frame'] for s in candidate['evidence']['temporal']['observedSamples']] == list(
        range(4, end+1, 2))
