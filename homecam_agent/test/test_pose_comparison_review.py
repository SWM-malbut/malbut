"""A raw request is not a hit; apply identical rules to all comparison stages."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from score_pose_comparison_review import (  # noqa: E402
    group_metrics, score_request, summarize_case, time_position, validate_decisions,
)


def result(*, association='target', scene='down', label='observed_fall',
           evidence_frame=30, dispatch=3., onset=(10, 20)):
    return score_request(
        dict(label=label, actual_evidence_frame=evidence_frame),
        dict(dispatch_time_s=dispatch),
        dict(association=association, scene=scene, note='explicit RGB review'),
        dict(onset_frames=onset), 10)


@pytest.mark.parametrize('association', ['other_person', 'non_person', 'unresolved'])
def test_wrong_or_unknown_target_never_success(association):
    value = result(association=association)
    assert not value['target_coverage'] and not value['time_validated']


@pytest.mark.parametrize('scene', ['normal', 'non_person', 'unresolved', 'pre_event'])
def test_actual_person_with_wrong_scene_is_not_success(scene):
    assert not result(scene=scene)['target_coverage']


def test_historical_pre_onset_evidence_not_rescued_by_later_dispatch():
    value = result(evidence_frame=5, dispatch=3.)
    assert value['evidence_timing']['position'] == 'early'
    assert value['dispatch_timing']['position'] == 'after'
    assert not value['target_coverage']


def test_before_onset_dispatch_not_success():
    assert not result(dispatch=.5)['time_validated']


def test_boundary_is_explicit_and_retains_interval():
    value = result(evidence_frame=15, dispatch=1.5)
    assert value['time_validated']
    assert value['evidence_timing']['position'] == 'boundary'
    assert value['dispatch_timing']['delay_interval_s'] == [-.5, .5]


def test_after_onset_true_positive():
    assert result()['time_validated']


def test_missing_time_is_not_invented_or_claimed_validated():
    value = result(label='suspected_fall', onset=None)
    assert value['target_coverage']
    assert not value['time_validated']
    assert value['evidence_timing']['delay_interval_s'] is None


def test_found_down_zero_frame_is_valid_time_reference():
    assert time_position(dict(first_down_frames=[0, 0]), .8, 10)['position'] == 'after'


def test_risk_posture_is_not_confirmed_fall_success():
    assert not result(scene='risk_posture')['target_coverage']
    assert result(scene='risk_posture', label='suspected_fall')['target_coverage']


def test_duplicate_requests_count_once_per_video():
    case = summarize_case('C', 'observed_fall', [result(), result()], {})
    group = group_metrics([case], 'observed_fall')
    assert group['total'] == group['requested'] == group['target_coverage'] == 1
    assert group['requests'] == 2


def test_later_good_request_can_rescue_earlier_wrong_request():
    case = summarize_case('C', 'observed_fall', [result(association='non_person'), result()], {})
    assert case['target_coverage'] and case['time_validated']


def test_unresolved_remains_in_denominator():
    cases = [summarize_case('A', 'observed_fall', [result()], {}),
             summarize_case('B', 'observed_fall', [result(association='unresolved')], {}),
             summarize_case('C', 'observed_fall', [], {})]
    group = group_metrics(cases, 'observed_fall')
    assert group['total'] == 3 and group['target_coverage'] == 1
    assert group['outcomes']['unresolved_evidence'] == 1


def test_pet_request_on_normal_video_is_not_removed():
    value = result(label='normal_activity', association='non_person', scene='non_person')
    case = summarize_case('C', 'normal_activity', [value], {})
    assert case['outcome'] == 'unnecessary_request'
    assert group_metrics([case], 'normal_activity')['requested'] == 1


@pytest.mark.parametrize('invalid', ['missing', 'duplicate', 'pending', 'blank', 'normal_target'])
def test_incomplete_reviews_cannot_be_scored(invalid):
    events = [dict(audit_id='U001', label='observed_fall')]
    review = dict(columns=['audit_id', 'association', 'scene', 'note'],
                  entries=[['U001', 'target', 'down', 'reviewed']])
    if invalid == 'missing':
        review['entries'] = []
    elif invalid == 'duplicate':
        review['entries'] *= 2
    elif invalid == 'pending':
        review['entries'][0][1] = 'pending'
    elif invalid == 'blank':
        review['entries'][0][3] = ' '
    else:
        events[0]['label'] = 'normal_activity'
    with pytest.raises(ValueError):
        validate_decisions(events, review)


def test_valid_review_preserved_not_mutated():
    events = [dict(audit_id='U001', label='observed_fall')]
    review = dict(columns=['audit_id', 'association', 'scene', 'note'],
                  entries=[['U001', 'target', 'down', 'reviewed']])
    before = copy.deepcopy(review)
    assert validate_decisions(events, review)['U001']['association'] == 'target'
    assert review == before
