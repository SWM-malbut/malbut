"""Clip-level independent paths: labels never enter routing, no provider calls."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from replay_parallel_pose_cloud import merge_clip_signals


def answer(label, status='responded', valid=True):
    return dict(status=status, valid=valid, prediction=dict(
        outcome='classified', label=label, explanation_ko='관찰 결과'))


def request(index):
    return dict(request_id=f'request-{index}', track_id=f'track-{index}', dispatch_s=1.0)


@pytest.mark.parametrize('label', ['observed_fall', 'suspected_fall'])
def test_cloud_can_trigger_with_no_yolo(label):
    result = merge_clip_signals([], answer(label))
    assert result['needs_check'] is True and result['sources'] == ['cloud_vlm']


def test_yolo_not_cancelled_by_cloud_normal():
    result = merge_clip_signals([request(1)], answer('normal_activity'))
    assert result['needs_check'] is True and result['sources'] == ['yolo_pose']
    assert result['yolo_cloud_disagree'] is True


def test_both_count_one_clip_but_not_one_person():
    result = merge_clip_signals([request(1), request(2)], answer('observed_fall'))
    assert result['needs_check'] is True
    assert result['sources'] == ['yolo_pose', 'cloud_vlm']
    assert result['source_track_ids'] == ['track-1', 'track-2']
    assert result['person_merge_verified'] is False
    assert result['final_fall_assessment'] is None


def test_negative_is_no_flag_not_a_new_normal_prediction():
    result = merge_clip_signals([], answer('normal_activity'))
    assert result['needs_check'] is False and result['final_fall_assessment'] is None


@pytest.mark.parametrize('failure', ['timeout', 'request_failed', 'bad_json', 'unobservable'])
@pytest.mark.parametrize('yolo', [False, True])
def test_failure_not_normal_and_does_not_clear_yolo(failure, yolo):
    response = answer('normal_activity')
    if failure == 'unobservable':
        response['prediction'].update(outcome='unobservable', label=None)
    elif failure == 'bad_json':
        response.update(valid=False, prediction=None)
    else:
        response.update(status=failure, valid=False, prediction=None)
    result = merge_clip_signals([request(1)] if yolo else [], response)
    assert result['cloud_positive'] is None
    assert result['needs_check'] is (True if yolo else None)


def test_duplicate_source_ids_rejected():
    with pytest.raises(ValueError, match='duplicate source request'):
        merge_clip_signals([request(1), request(1)], answer('observed_fall'))


def test_label_side_data_cannot_change_merging():
    response = answer('normal_activity')
    result = merge_clip_signals([request(1)], response)
    response.update(label='observed_fall', target_coverage=True)
    assert merge_clip_signals([request(1)], response) == result
