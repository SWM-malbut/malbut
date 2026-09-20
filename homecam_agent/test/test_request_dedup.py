"""Request-only safety contracts; not real-world person identity validation."""
import copy
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from experimental_request_dedup import (  # noqa: E402
    RequestDedupConfig, RequestDedupExperiment, pair_evidence,
)
from replay_request_dedup import payloads  # noqa: E402


def observation(tid='a', partial=False, index=0, dx=0, dy=0):
    box = (.60, .23, .86, .58) if partial else (.1, .2, .9, .6)
    joints = {'nose': (.75, .3), 'left_eye': (.76, .29), 'right_eye': (.74, .29),
              'right_shoulder': (.67, .36), 'right_elbow': (.64, .42),
              'right_wrist': (.69, .45), 'left_shoulder': (.80, .37),
              'left_elbow': (.82, .43), 'left_wrist': (.79, .48)}
    return dict(track_id=tid, observation_index=index, pose=dict(
        boxConfidence=.11, visibleKeypoints=len(joints),
        box=dict(zip(('left', 'top', 'right', 'bottom'),
                     (box[0]+dx, box[1]+dy, box[2]+dx, box[3]+dy))),
        keypoints=[dict(name=n, x=x+dx, y=y+dy, confidence=.9)
                   for n, (x, y) in joints.items()]))


def candidate(tid='a', kind='found_down', name=None):
    return dict(candidateId=name or f'candidate-{tid}', targetTrackId=tid,
                candidateKind=kind, revision=1, requiresVerification=True)


def row(frame, observations=None, candidates=(), updates=(), **states):
    obs = [observation(), observation('b', True, 1)] if observations is None else observations
    states = {**{o['track_id']: 'tracked' for o in obs}, **states}
    return dict(frame_index=frame, timestamp_s=frame/10, observations=obs,
                verification_updates=list(updates), fall_analysis=dict(
                    status='ok', candidates=list(candidates), unassignedCount=0,
                    tracks=[dict(targetTrackId=tid, trackingState=s) for tid, s in states.items()]))


def step(exp, r, size=(640, 400)):
    before = copy.deepcopy(r)
    result = exp.update(r, image_size=size)
    assert r == before  # Never replace boxes, IDs, timestamps or original evidence.
    assert payloads(result[0], result[1]) == payloads(
        r['fall_analysis']['candidates'], r['verification_updates'])
    return result


def prepared(first='a'):
    exp = RequestDedupExperiment()
    step(exp, row(0))
    step(exp, row(2))
    out, updates, details = step(exp, row(4, candidates=[candidate(first)]))
    assert len(out) == 1 and not updates and details[0]['confirmed']
    return exp


@pytest.mark.parametrize('first,second', [('a', 'b'), ('b', 'a')])
def test_same_head_and_arm_links_requests_in_either_order_without_merging_ids(first, second):
    exp = prepared(first)
    out, updates, _ = step(exp, row(6, candidates=[candidate(second)]))
    assert not out and len(updates) == 1
    u = updates[0]
    assert u['requestCandidateId'] == f'candidate-{first}'
    assert u['evidence']['targetTrackId'] == second
    assert u['deduplication']['trackIds'] == ['a', 'b']
    assert [s['frameIndex'] for s in u['deduplication']['actualSamples']] == [0, 2, 4, 6]


def test_only_one_initial_request_on_same_frame_when_relation_already_confirmed():
    exp = RequestDedupExperiment()
    for f in [0, 2]:
        step(exp, row(f))
    out, updates, _ = step(exp, row(4, candidates=[candidate(), candidate('b')]))
    assert len(out) == len(updates) == 1


def test_unmoving_person_and_weak_detection_are_not_removed():
    exp = prepared()
    out, updates, _ = step(exp, row(6, candidates=[candidate('b')]))
    assert not out and updates[0]['evidence']['requiresVerification']
    # This only removes a repeated request, not the initial suspicious posture.
    assert len(exp.requests) == 1


def test_missing_candidate_uses_exact_last_coobserved_reference_without_new_pose():
    exp = prepared()
    step(exp, row(6, [observation()], b='missing'))
    c = candidate('b')
    c.update(observationAvailability='missing', evidenceReference=dict(
        frameIndex=4, timestampSec=.4, observationIndex=1))
    out, updates, _ = step(exp, row(8, [observation()], candidates=[c], b='missing'))
    assert not out and len(updates) == 1
    assert updates[0]['deduplication']['currentPoseAvailable'] == {'a': True, 'b': False}
    assert updates[0]['deduplication']['actualSamples'][-1]['frameIndex'] == 4
    assert updates[0]['evidence'] == c


@pytest.mark.parametrize('ref', [dict(frameIndex=2, timestampSec=.2, observationIndex=1),
                                 dict(frameIndex=4, timestampSec=.4, observationIndex=0),
                                 dict(frameIndex=4, timestampSec=.5, observationIndex=1)])
def test_wrong_last_seen_reference_cannot_merge(ref):
    exp = prepared()
    c = dict(candidate('b'), observationAvailability='missing', evidenceReference=ref)
    out, updates, _ = step(exp, row(6, [observation()], candidates=[c], b='missing'))
    assert out == [c] and not updates


@pytest.mark.parametrize('problem', ['different_head', 'different_arm', 'only_boxes',
                                     'low_confidence', 'equal_boxes', 'outside',
                                     'nan', 'duplicate_joint', 'box_only_overlap'])
def test_overlap_or_one_body_part_alone_never_identifies_same_person(problem):
    a, b = observation(), observation('b', True, 1)
    if problem == 'equal_boxes':
        b['pose']['box'] = copy.deepcopy(a['pose']['box'])
    elif problem == 'box_only_overlap':
        b['pose']['box']['right'] = 1.0
        b['pose']['box']['left'] = .8
    elif problem == 'only_boxes':
        b['pose']['keypoints'] = []
    elif problem == 'duplicate_joint':
        b['pose']['keypoints'].append(copy.deepcopy(b['pose']['keypoints'][0]))
    else:
        for p in b['pose']['keypoints']:
            head = p['name'] in {'nose', 'left_eye', 'right_eye'}
            if problem == 'different_head' and head:
                p['y'] += .12
            elif problem == 'different_arm' and not head:
                p['y'] += .10
            elif problem == 'low_confidence':
                p['confidence'] = .64
            elif problem == 'outside':
                p['x'] = 1.1
            elif problem == 'nan':
                p['y'] = float('nan')
    assert pair_evidence(a, b, (640, 400), RequestDedupConfig()) is None


def test_sitting_helper_and_lying_person_with_overlapping_boxes_keep_two_requests():
    exp = RequestDedupExperiment()
    a = observation()
    b = observation('b', True, 1, dy=.10)
    for f in [0, 2, 4]:
        out, updates, _ = step(exp, row(f, [a, b], candidates=(
            [candidate(), candidate('b')] if f == 4 else [])))
    assert len(out) == 2 and not updates


@pytest.mark.parametrize('problem', ['too_few', 'too_brief', 'long_gap', 'interrupted'])
def test_requires_consecutive_actual_temporal_evidence(problem):
    exp = RequestDedupExperiment()
    times = {'too_few': [0, 2], 'too_brief': [0, 1, 2],
             'long_gap': [0, 2, 8], 'interrupted': [0, 2, 4, 6]}[problem]
    for i, f in enumerate(times):
        obs = [observation()] if problem == 'interrupted' and f == 4 else None
        out, updates, _ = step(exp, row(
            f, obs, candidates=[candidate(), candidate('b')] if i == len(times)-1 else [],
            **({'b': 'missing'} if obs else {})))
    assert len(out) == 2 and not updates


@pytest.mark.parametrize('problem', ['unassigned', 'ambiguous', 'expired', 'new_id',
                                     'different_head', 'size', 'clock', 'failed'])
def test_relation_cannot_survive_uncertain_association_or_new_geometry(problem):
    exp = prepared()
    r = row(6, candidates=[candidate('b')])
    size = (640, 400)
    if problem == 'unassigned':
        r['fall_analysis']['unassignedCount'] = 1
    elif problem == 'ambiguous':
        r['fall_analysis']['tracks'][1]['trackingState'] = 'ambiguous'
    elif problem == 'expired':
        r['fall_analysis']['tracks'].pop()
        r['observations'].pop()
    elif problem == 'new_id':
        r = row(6, [observation(), observation('c', True, 1)], candidates=[candidate('c')])
    elif problem == 'different_head':
        r['observations'][1]['pose']['keypoints'][0]['y'] += .15
    elif problem == 'size':
        size = (1280, 800)
    elif problem == 'clock':
        r['timestamp_s'] = .4
    elif problem == 'failed':
        r['fall_analysis']['status'] = 'error'
    out, updates, _ = step(exp, r, size)
    assert len(out) == 1 and not updates


def test_stale_request_cannot_consume_a_new_incident():
    exp = prepared()
    for f in range(6, 28, 2):
        out, updates, _ = step(exp, row(f, candidates=[candidate('b')] if f == 26 else []))
    assert len(out) == 1 and not updates


def test_last_seen_match_expires_without_filling_missing_time():
    exp = prepared()
    for f in [6, 8, 10]:
        c = dict(candidate('b'), observationAvailability='missing', evidenceReference=dict(
            frameIndex=4, timestampSec=.4, observationIndex=1))
        out, updates, _ = step(exp, row(
            f, [observation()], b='missing', candidates=[c] if f == 10 else []))
    assert len(out) == 1 and not updates


def test_current_requester_cannot_borrow_a_missing_peers_old_box():
    exp = prepared()
    out, updates, _ = step(exp, row(
        6, [observation('b', True, 1)], a='missing', candidates=[candidate('b')]))
    assert len(out) == 1 and not updates


def test_third_matching_pose_breaks_relation_without_transitive_union():
    exp = prepared()
    obs = [observation(), observation('b', True, 1), observation('c', True, 2)]
    out, updates, details = step(exp, row(6, obs, candidates=[candidate('b'), candidate('c')]))
    assert len(out) == 2 and not updates and not details


def test_new_fall_motion_is_never_hidden_by_an_earlier_found_down_request():
    exp = prepared()
    out, updates, _ = step(exp, row(6, candidates=[candidate('b', 'fall_suspected')]))
    assert len(out) == 1 and not updates


def test_prior_fall_request_cannot_consume_a_different_tracks_found_down():
    exp = RequestDedupExperiment()
    for f in [0, 2, 4]:
        step(exp, row(f, candidates=[candidate(kind='fall_suspected')] if f == 4 else []))
    out, updates, _ = step(exp, row(6, candidates=[candidate('b')]))
    assert len(out) == 1 and not updates


def test_later_update_splits_back_out_when_the_relation_is_no_longer_valid():
    exp = prepared()
    step(exp, row(6, candidates=[candidate('b')]))
    b = observation('b', True, 1, dy=.10)
    u = dict(requestCandidateId='candidate-b', evidence=candidate('b', name='new-b'))
    out, updates, _ = step(exp, row(8, [observation(), b], updates=[u]))
    assert out == [u['evidence']] and not updates
    assert exp.routes == {'a': 'candidate-a', 'b': 'new-b'}


def test_update_retains_all_original_payload_when_relation_still_valid():
    exp = prepared()
    step(exp, row(6, candidates=[candidate('b')]))
    u = dict(requestCandidateId='candidate-b', evidence=candidate('b', name='new-b'),
             custom='original_metadata')
    out, updates, _ = step(exp, row(8, updates=[u]))
    assert not out and updates[0]['requestCandidateId'] == 'candidate-a'
    assert updates[0]['evidence'] == u['evidence'] and updates[0]['custom'] == u['custom']


@pytest.mark.parametrize('kwargs', [
    dict(minimum_samples=True), dict(minimum_samples=1), dict(maximum_area_ratio=1.1),
    dict(minimum_span_sec=0), dict(maximum_head_distance=float('nan')),
    dict(maximum_sample_gap_sec=1),
])
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        RequestDedupConfig(**kwargs)


def test_scale_change_does_not_change_same_frame_correspondence():
    a, b, cfg = observation(), observation('b', True, 1), RequestDedupConfig()
    assert pair_evidence(a, b, (640, 400), cfg) == pair_evidence(a, b, (1280, 800), cfg)
    assert replace(cfg, maximum_area_ratio=.1).maximum_area_ratio == .1
