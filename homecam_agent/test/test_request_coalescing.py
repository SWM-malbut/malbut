"""Request scheduling/identity safeguards; no claims of real-world accuracy."""
import copy
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_request_coalescing import (  # noqa: E402
    CoalescingConfig, RequestCoalescer, tolerant_pair,
)
from experimental_request_dedup import RequestDedupConfig, pair_evidence  # noqa: E402
from test_request_dedup import candidate, observation, row  # noqa: E402


def step(exp, frame, *, obs=None, candidates=(), updates=(), now=None, **states):
    r = row(frame, obs, candidates, updates, **states)
    before = copy.deepcopy(r)
    out = exp.update(r, image_size=(640, 400), now_s=frame/10 if now is None else now)
    assert r == before
    return out


def queued(config=None):
    exp = RequestCoalescer(config)
    first = step(exp, 0, candidates=[candidate()])
    assert first[0]['dispatch_kind'] == 'request' and first[0]['delay_sec'] == 0
    assert not step(exp, 2, candidates=[candidate('b')])
    assert len(exp.pending) == 1
    return exp


def test_small_bounded_box_error_allowed_without_moving_any_box_or_joint():
    a, b = observation(), observation('b', True, 1)
    a['pose']['box']['right'] = .831
    before = copy.deepcopy((a, b))
    assert pair_evidence(a, b, (640, 400), RequestDedupConfig()) is None
    p = tolerant_pair(a, b, (640, 400), CoalescingConfig())
    assert .88 < p['raw_containment'] < .9
    assert p['padded_containment'] >= .9 and p['margin_px'][0] > 0
    assert (a, b) == before


@pytest.mark.parametrize('issue', ['too_far', 'head', 'arm', 'head_only', 'weak', 'equal'])
def test_padding_never_replaces_head_and_arm_or_area_constraints(issue):
    a, b = observation(), observation('b', True, 1)
    a['pose']['box']['right'] = .831
    if issue == 'too_far':
        a['pose']['box']['right'] = .82
    elif issue == 'equal':
        b['pose']['box'] = copy.deepcopy(a['pose']['box'])
    else:
        for p in b['pose']['keypoints']:
            head = p['name'] in {'nose', 'left_eye', 'right_eye'}
            if issue == 'head' and head or issue == 'arm' and not head:
                p['y'] += .1
            if issue == 'weak' or issue == 'head_only' and not head:
                p['confidence'] = .1
    assert tolerant_pair(a, b, (640, 400), CoalescingConfig()) is None


def test_resolution_scaled_geometry_is_equivalent():
    a, b, cfg = observation(), observation('b', True, 1), CoalescingConfig()
    a['pose']['box']['right'] = .831
    p, q = (tolerant_pair(a, b, size, cfg) for size in ((640, 400), (1280, 800)))
    assert p['raw_containment'] == q['raw_containment']
    assert p['padded_containment'] == q['padded_containment']
    assert q['margin_px'] == [2*v for v in p['margin_px']]


def test_first_request_immediate_then_three_real_samples_merge_pending():
    exp = queued()
    out = step(exp, 4)
    assert len(out) == 1 and not exp.pending
    event = out[0]
    assert event['dispatch_kind'] == 'update' and event['request_id'] == 'candidate-a'
    assert event['reason'] == 'confirmed_after_wait'
    assert event['origin']['frame_index'] == 2 and event['decision_frame_index'] == 4
    assert event['delay_sec'] == pytest.approx(.2)
    assert event['origin']['candidate'] == candidate('b')
    assert event['update']['evidence'] == candidate('b')


def test_short_missing_frame_preserves_actual_samples_but_does_not_invent_agreement():
    exp = queued()
    assert not step(exp, 4, obs=[observation()], b='missing')
    assert exp.details[0]['sampleFrames'] == [0, 2]
    out = step(exp, 6)
    assert out[0]['dispatch_kind'] == 'update'
    proof = out[0]['update']['deduplication']['actualSamples']
    assert [p['frameIndex'] for p in proof] == [0, 2, 6]
    assert proof[-1]['observedAdjacentSec'] == 0
    assert exp.details[0]['observedAdjacentSec'] == pytest.approx(.2)
    assert exp.details[0]['evidenceWindowSec'] == pytest.approx(.6)


@pytest.mark.parametrize('issue', [
    'different_person', 'third_person', 'unassigned', 'ambiguous',
    'failed', 'source_clock', 'expired', 'resolution'])
def test_contradiction_releases_pending_as_separate_request(issue):
    exp = queued()
    r = row(4)
    size = (640, 400)
    if issue == 'different_person':
        r['observations'][1] = observation('b', True, 1, dy=.1)
    elif issue == 'third_person':
        r = row(4, [observation(), observation('b', True, 1), observation('c', True, 2)])
    elif issue == 'unassigned':
        r['fall_analysis']['unassignedCount'] = 1
    elif issue == 'ambiguous':
        r['fall_analysis']['tracks'][1]['trackingState'] = 'ambiguous'
    elif issue == 'failed':
        r['fall_analysis']['status'] = 'error'
    elif issue == 'source_clock':
        r['timestamp_s'] = .1
    elif issue == 'expired':
        r = row(4, [observation()])
    else:
        size = (1280, 800)
    out = exp.update(r, image_size=size, now_s=.4)
    assert len(out) == 1 and not exp.pending
    assert out[0]['dispatch_kind'] == 'request'
    assert out[0]['origin']['candidate'] == candidate('b')


def test_no_frames_still_flushes_at_deadline_and_never_discards_evidence():
    exp = queued()
    assert exp.poll(.69) == []
    out = exp.poll(.7)
    assert len(out) == 1 and out[0]['dispatch_kind'] == 'request'
    assert out[0]['delay_sec'] == pytest.approx(.5)
    assert out[0]['reason'] == 'deadline'
    assert out[0]['decision_frame_index'] is None
    assert out[0]['last_observation_frame_index'] == 2
    assert exp.poll(.8) == []


def test_third_sample_at_deadline_cannot_retroactively_cancel_the_request():
    exp = queued()
    out = step(exp, 7)
    assert out[0]['reason'] == 'deadline' and out[0]['dispatch_kind'] == 'request'


def test_stop_flushes_pending_before_deadline():
    exp = queued()
    out = exp.flush(.3)
    assert len(out) == 1 and out[0]['reason'] == 'end_of_stream'
    assert out[0]['dispatch_kind'] == 'request'
    assert not exp.pending and exp.flush(.3) == []


def test_unrelated_person_and_new_fall_motion_are_never_delayed():
    exp = RequestCoalescer()
    step(exp, 0, candidates=[candidate()])
    out = step(exp, 2, obs=[observation(), observation('b', True, 1, dy=.1)],
               candidates=[candidate('b')])
    assert out[0]['dispatch_kind'] == 'request' and out[0]['delay_sec'] == 0
    exp = RequestCoalescer()
    step(exp, 0, candidates=[candidate()])
    out = step(exp, 2, candidates=[candidate('b', 'fall_suspected')])
    assert out[0]['dispatch_kind'] == 'request' and out[0]['delay_sec'] == 0


def test_new_evidence_on_pending_track_releases_old_before_processing_new():
    exp = queued()
    out = step(exp, 3, obs=[observation('b', True, 1)], a='missing',
               candidates=[candidate('b', 'fall_suspected', 'fall-b')])
    assert len(out) == 2 and not exp.pending
    assert out[0]['origin']['candidate'] == candidate('b')
    assert out[1]['origin']['candidate']['candidateKind'] == 'fall_suspected'
    assert out[1]['delay_sec'] == 0
    assert exp.tracker.requests['candidate-b']['kind'] == 'fall_suspected'


def test_geometry_only_does_not_wait_or_use_future_evidence():
    exp = RequestCoalescer(replace(CoalescingConfig(), pending_enabled=False))
    step(exp, 0, candidates=[candidate()])
    out = step(exp, 2, candidates=[candidate('b')])
    assert out[0]['dispatch_kind'] == 'request' and out[0]['delay_sec'] == 0
    assert not exp.pending


def test_missing_candidate_not_queued_and_wrong_reference_not_merged():
    exp = RequestCoalescer()
    step(exp, 0, candidates=[candidate()])
    c = dict(candidate('b'), observationAvailability='missing', evidenceReference=dict(
        frameIndex=0, timestampSec=0, observationIndex=1))
    out = step(exp, 2, obs=[observation()], candidates=[c], b='missing')
    assert out[0]['dispatch_kind'] == 'request' and not exp.pending


def test_original_update_metadata_is_preserved_in_envelope_and_routed_update():
    exp = queued()
    step(exp, 4)
    u = dict(requestCandidateId='candidate-b', evidence=candidate('b', name='b-next'),
             audit_custom={'untouched': True})
    out = step(exp, 6, updates=[u])
    assert out[0]['origin']['original_update'] == u
    assert out[0]['update']['audit_custom'] == u['audit_custom']
    assert out[0]['update']['evidence'] == u['evidence']


@pytest.mark.parametrize('kwargs', [
    dict(box_margin_fraction=.2), dict(box_margin_fraction=True),
    dict(minimum_raw_containment=.2), dict(evidence_window_sec=.1),
    dict(pending_enabled=1), dict(pending_minimum_samples=True), dict(pending_minimum_samples=1),
    dict(pending_minimum_span_sec=.4), dict(maximum_pending_sec=.6),
    dict(maximum_pending_items=0), dict(maximum_pending_items=True),
    dict(maximum_pending_sec=float('nan'))])
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        CoalescingConfig(**kwargs)


def test_dispatch_clock_regression_rejected_without_discarding_pending():
    exp = queued()
    with pytest.raises(ValueError):
        exp.poll(.1)
    assert len(exp.pending) == 1
    assert exp.poll(.7)[0]['dispatch_kind'] == 'request'


def test_old_request_can_keep_receiving_evidence_only_during_continuous_direct_match():
    exp = queued()
    step(exp, 4)
    for frame in range(6, 28, 2):
        out = step(exp, frame, candidates=[candidate('b', name='b-later')] if frame == 26 else [])
    assert out[0]['dispatch_kind'] == 'update' and out[0]['request_id'] == 'candidate-a'
    proof = out[0]['update']['deduplication']
    assert proof['requestAgePolicy'] == 'continued_fresh_direct_correspondence'
    assert proof['originalRequestTimestampSec'] == 0
    assert exp.tracker.requests['candidate-a']['timestamp_s'] == 0


def test_missing_evidence_update_keeps_proven_route_without_borrowing_new_pose():
    exp = queued()
    step(exp, 4)
    for frame in range(6, 28, 2):
        step(exp, frame)
    step(exp, 28, obs=[observation()], b='missing')
    c = dict(candidate('b', name='b-missing'), observationAvailability='missing',
             evidenceReference=dict(frameIndex=26, timestampSec=2.6, observationIndex=1))
    out = step(exp, 30, obs=[observation()], candidates=[c], b='missing')
    assert out[0]['dispatch_kind'] == 'update'
    assert out[0]['origin']['candidate'] == c
    assert out[0]['update']['deduplication']['currentPoseAvailable']['b'] is False


def test_old_route_cannot_reappear_after_contradiction_even_with_new_matching_samples():
    exp = queued()
    step(exp, 4)
    step(exp, 6, obs=[observation(), observation('b', True, 1, dy=.1)])
    assert not exp.tracker.confirmed_routes
    for frame in range(8, 28, 2):
        out = step(exp, frame, candidates=[candidate('b', name='b-new')] if frame == 26 else [])
    assert out[0]['dispatch_kind'] == 'request' and out[0]['request_id'] == 'b-new'


def test_new_fall_on_a_proven_partial_track_splits_out_immediately():
    exp = queued()
    step(exp, 4)
    out = step(exp, 6, candidates=[candidate('b', 'fall_suspected', 'b-fall')])
    assert out[0]['dispatch_kind'] == 'request' and out[0]['delay_sec'] == 0
    assert 'b' not in exp.tracker.confirmed_routes
