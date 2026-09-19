"""Annotation bookkeeping tests; these do not validate visual ground truth."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'review_fall_annotations', ROOT / 'scripts/review_fall_annotations.py')
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


@pytest.fixture
def inputs():
    draft = json.loads((ROOT / 'evaluations/synthetic_fall_v1/'
                       'spatial_temporal_draft.json').read_text())
    # Stub provenance for structural unit tests only. The export command verifies
    # the real, pinned source-label bytes and all 41 video hashes separately.
    labels = {'cases': []}
    media = {'source_labels_sha256': draft['source_labels_sha256'], 'cases': []}
    for case in draft['cases']:
        cid = case['case_id']
        labels['cases'].append(dict(case_id=cid, source_sha256='a'*64,
                                    source_path=cid+'.mp4',
                                    expected_candidate_detection=int(cid[3:]) <= 21))
        media['cases'].append(dict(case_id=cid, sha256='a'*64, source_path=cid+'.mp4',
                                   frames=62 if cid == 'SYN006' else 63,
                                   fps=12.0, width=640, height=400))
    return draft, labels, media


def test_complete_draft_is_valid_but_not_approved(inputs):
    before = copy.deepcopy(inputs)
    summary = review.validate(*inputs)
    assert inputs == before
    assert summary['cases'] == 41
    assert summary['target_cases'] == 21
    assert summary['person_tracks'] == 42
    assert summary['approved_new_spatial_temporal_labels'] == 0
    assert summary['formal_metrics_ready'] is False


@pytest.mark.parametrize('key,value', [
    ('new_labels_user_approved', True),
    ('annotation_status', 'approved'),
    ('dataset_usage', 'held_out_test'),
    ('box_interpolation', 'linear'),
    ('time_format', 'seconds'),
    ('source_labels_sha256', 'bad'),
])
def test_invalid_provenance_and_policies_rejected(inputs, key, value):
    inputs[0][key] = value
    with pytest.raises(ValueError):
        review.validate(*inputs)


@pytest.mark.parametrize('interval', [[-1, 2], [5, 4], [0, 63], [True, 2], [0.0, 2], [0]])
def test_invalid_temporal_intervals_rejected(inputs, interval):
    inputs[0]['cases'][0]['onset_frames'] = interval
    with pytest.raises(ValueError):
        review.validate(*inputs)


def test_414_target_is_not_the_helper(inputs):
    case = inputs[0]['cases'][11]
    assert case['case_id'] == 'SYN012'
    assert case['target_person_id'] == 'P01'
    assert [p['role'] for p in case['persons']] == ['target', 'other']
    assert case['onset_frames'] is None
    assert case['first_down_frames'] == [0, 0]
    case['target_person_id'] = 'P02'
    with pytest.raises(ValueError, match='target role'):
        review.validate(*inputs)


def test_431_encounter_precedes_later_motion(inputs):
    case = inputs[0]['cases'][15]
    assert case['case_id'] == 'SYN016'
    assert case['entry_state'] == 'already_down'
    assert case['onset_frames'] is None
    assert case['first_down_frames'] == [0, 0]
    assert case['additional_motion']['onset_frames'][0] > 0
    case['onset_frames'] = [0, 0]
    case['onset_status'] = 'visible_interval'
    with pytest.raises(ValueError, match='initial fall time'):
        review.validate(*inputs)


def test_unknown_time_is_not_zero():
    assert review.seconds(None, 12) is None
    assert review.seconds([0, 0], 12) == [0.0, 0.0]
    assert review.seconds([12, 24], 12) == [1.0, 2.0]
    assert review.time_label(None, 'occluded') == '가려져 확인 불가'
    assert review.time_label([0, 0]) == '0초'


def test_unannotated_frames_are_not_interpolated(inputs):
    person = inputs[0]['cases'][0]['persons'][0]
    assert review.box_at(person, 0) == person['boxes'][0][1:]
    assert review.box_at(person, 1) is None
    assert review.box_at(person, 62) is None


def test_covered_person_retains_identity_without_invented_box(inputs):
    person = inputs[0]['cases'][10]['persons'][0]
    assert person['person_id'] == 'P01'
    assert person['spatial_unknown_frames'] == [36, 48, 60]
    assert review.box_at(person, 36) is None
    person['boxes'].append([36, 10, 10, 20, 20])
    with pytest.raises(ValueError, match='also marked unknown'):
        review.validate(*inputs)


@pytest.mark.parametrize('box', [
    [0, 0, 0, 641, 20], [0, 5, 5, 4, 20], [63, 0, 0, 20, 20],
    [0, False, 0, 20, 20], [0, 0, 0, 20],
])
def test_invalid_boxes_rejected(inputs, box):
    inputs[0]['cases'][0]['persons'][0]['boxes'][0] = box
    with pytest.raises(ValueError):
        review.validate(*inputs)


def test_duplicate_id_rejected(inputs):
    case = inputs[0]['cases'][11]
    case['persons'][1]['person_id'] = 'P01'
    with pytest.raises(ValueError, match='duplicate person IDs'):
        review.validate(*inputs)


def test_unreviewed_box_rejected(inputs):
    inputs[0]['cases'][2]['persons'][0]['boxes'].insert(1, [1, 0, 0, 20, 20])
    with pytest.raises(ValueError, match='unreviewed box'):
        review.validate(*inputs)


def test_media_hash_mismatch_rejected(inputs):
    inputs[2]['cases'][0]['sha256'] = 'b'*64
    with pytest.raises(ValueError, match='video hash'):
        review.validate(*inputs)


def test_duplicate_case_rejected(inputs):
    inputs[0]['cases'].append(copy.deepcopy(inputs[0]['cases'][0]))
    with pytest.raises(ValueError, match='duplicate cases'):
        review.validate(*inputs)


def test_normal_rest_is_not_a_fall_event(inputs):
    case = inputs[0]['cases'][21]
    case['first_down_frames'] = [0, 0]
    with pytest.raises(ValueError, match='normal activity'):
        review.validate(*inputs)


def test_occluded_landing_cannot_have_fabricated_time(inputs):
    inputs[0]['cases'][6]['landing_frames'] = [40, 40]
    with pytest.raises(ValueError, match='status/time mismatch'):
        review.validate(*inputs)
