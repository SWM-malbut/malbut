"""Evaluation bookkeeping regressions, not proof of human detection accuracy."""
import copy
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from replay_fall_baseline import sample_frames, verify_freeze, write_json, sha  # noqa: E402
from score_fall_baseline import (  # noqa: E402
    anchors, case_score, match_boxes, overlap_score, timing, validate_audit, validate_rows,
)


CRITERIA = dict(visible_coverage=.5, prediction_coverage=.25, margin=.1)


def test_source_sampling_and_sparse_anchors():
    assert len(sample_frames(63, 12)) == 27
    assert len(sample_frames(62, 12)) == 26
    assert sum(len(sample_frames(n, 12)) for n in [63] * 40 + [62]) == 1106
    assert set([0, 12, 24, 36, 48, 60]) <= set(sample_frames(63, 12))
    assert sample_frames(63, 12)[-1] == 62


@pytest.mark.parametrize('count,fps', [(0, 12), (63, 0), (63, float('nan')), (63, 4)])
def test_bad_sampling_fails(count, fps):
    with pytest.raises(ValueError):
        sample_frames(count, fps)


def test_visible_extent_can_match_larger_whole_body_but_not_entire_frame():
    gt = [10, 10, 20, 20]
    assert overlap_score(gt, [10, 0, 20, 20], CRITERIA) is not None
    assert overlap_score(gt, [0, 0, 100, 100], CRITERIA) is None
    assert overlap_score(gt, [40, 40, 50, 50], CRITERIA) is None
    assert overlap_score(gt, [15.1, 10, 20, 20], CRITERIA) is None


def test_mutual_unique_matches_never_assign_same_box_twice():
    box = [10, 10, 20, 20]
    assert match_boxes([box, box], [box], CRITERIA) == [
        ('ambiguous', None), ('ambiguous', None)]
    assert match_boxes([box], [box, box], CRITERIA) == [('ambiguous', None)]


def test_414_helper_is_not_a_target_match():
    lying, helper = [220, 147, 438, 210], [199, 71, 342, 208]
    assert match_boxes([lying, helper], [helper], CRITERIA) == [
        ('ambiguous', None), ('matched', 0)]


def test_unknown_frames_not_interpolated_or_treated_as_absent():
    case = dict(case_id='X', persons=[dict(
        person_id='P01', role='target', boxes=[[0, 10, 10, 20, 20]],
        spatial_unknown_frames=[12],
    )])
    rows = [dict(frame_index=f, observations=[], fall_analysis=dict(tracks=[]))
            for f in (0, 2, 12)]
    result = anchors(case, rows, dict(width=100, height=100), CRITERIA)
    assert len(result) == 1
    assert result[0]['frame_index'] == 0
    assert result[0]['status'] == 'unlinked'


def test_missing_labelled_frame_is_replay_error_not_miss():
    case = dict(case_id='X', persons=[dict(boxes=[[12, 0, 0, 10, 10]])])
    with pytest.raises(ValueError, match='not sampled'):
        anchors(case, [], {}, CRITERIA)


def test_delay_uses_interval_and_preserves_negative_values():
    case = dict(entry_state='standing', onset_frames=[12, 24])
    assert timing(case, 3, 12)['delay_s'] == [1, 2]
    assert timing(case, .5, 12)['delay_s'] == [-1.5, -.5]
    assert timing(case, .5, 12)['position'] == 'early'
    assert timing(case, 1.5, 12)['position'] == 'boundary_uncertain'


def test_already_down_uses_discovery_not_invented_fall_onset():
    case = dict(entry_state='already_down', onset_frames=None, first_down_frames=[0, 0])
    result = timing(case, .75, 12)
    assert result['basis'] == 'first_down'
    assert result['delay_s'] == [.75, .75]
    assert timing(dict(entry_state='standing', onset_frames=None), 1, 12)['delay_s'] is None


def fixture_case():
    case = dict(case_id='X', entry_state='standing', onset_frames=[12, 24])
    label = dict(source_path='X.mp4', label='suspected_fall', expected_candidate_detection=True)
    event = dict(key='event1', frame_index=36, timestamp_s=3,
                 candidate=dict(candidateKind='found_down'))
    return case, label, event


@pytest.mark.parametrize('association,outcome', [
    ('target', 'target_candidate'), ('other_person', 'wrong_target_only'),
    ('background', 'wrong_target_only'), ('unknown', 'unresolved_association_or_time'),
])
def test_event_must_refer_to_target(association, outcome):
    case, label, event = fixture_case()
    audit = {'event1': dict(association=association, note='manual RGB check')}
    assert case_score(case, label, [event], audit, 12)['outcome'] == outcome


def test_no_output_and_unknown_are_different():
    case, label, event = fixture_case()
    assert case_score(case, label, [], {}, 12)['outcome'] == 'no_output'
    assert case_score(case, label, [event], {}, 12)['outcome'].startswith('unresolved')


def test_candidate_before_incident_is_not_a_hit_and_revisions_are_one_case():
    case, label, event = fixture_case()
    audit = {'event1': dict(association='target', note='manual')}
    early = dict(event, timestamp_s=.5, frame_index=6)
    assert case_score(case, label, [early], audit, 12)['outcome'] == 'early_only'
    scored = case_score(case, label, [event, event], audit, 12)
    assert scored['outcome'] == 'target_candidate'
    assert scored['output_count'] == 2


def test_negative_output_is_not_guardian_false_alarm_metric():
    case, label, event = fixture_case()
    label.update(label='normal_activity', expected_candidate_detection=False)
    assert case_score(case, label, [event], {}, 12)['outcome'] == 'unnecessary_candidate'
    assert case_score(case, label, [], {}, 12)['outcome'] == 'no_candidate_on_negative'
    with pytest.raises(ValueError):
        case_score(case, label, [event], {'event1': dict(association='target', note='bad')}, 12)


def test_audit_cannot_be_applied_to_another_run_or_omit_outputs():
    audit = dict(frames_sha256='abc', method='manual_RGB_output_association_not_blind',
                 events=[dict(key='a', association='target', note='RGB')])
    assert validate_audit(audit, [dict(key='a')], 'abc')['a']['association'] == 'target'
    with pytest.raises(ValueError):
        validate_audit(audit, [dict(key='a')], 'other')
    with pytest.raises(ValueError):
        validate_audit(audit, [dict(key='a'), dict(key='b')], 'abc')
    audit['events'].append(copy.deepcopy(audit['events'][0]))
    with pytest.raises(ValueError):
        validate_audit(audit, [dict(key='a')], 'abc')


def test_scoring_rejects_partial_duplicate_or_failed_replays():
    media = dict(cases=[dict(case_id='X', frames=2, fps=5)])
    run = dict(sample_fps=5, fall_config_sha256='config')
    rows = [dict(case_id='X', frame_index=i, timestamp_s=i/5, pose_ms=1, pipeline_ms=2,
                 fall_analysis=dict(status='ok', configSha256='config')) for i in range(2)]
    validate_rows(rows, media, run)
    for bad in (rows[:1], rows + [rows[0]], rows[::-1]):
        with pytest.raises(ValueError):
            validate_rows(bad, media, run)
    rows[0]['fall_analysis']['status'] = 'invalid_capture_time'
    with pytest.raises(ValueError):
        validate_rows(rows, media, run)


def test_frozen_input_change_rejected(tmp_path):
    write_json(tmp_path / 'media.json', dict(cases=[]))
    write_json(tmp_path / 'freeze.json', dict(files={'media.json': sha(tmp_path / 'media.json')}))
    verify_freeze(tmp_path)
    (tmp_path / 'media.json').write_text('{}')
    with pytest.raises(ValueError):
        verify_freeze(tmp_path)


def test_no_overwrite_of_previous_json_results(tmp_path):
    write_json(tmp_path / 'test.json', {})
    with pytest.raises(FileExistsError):
        write_json(tmp_path / 'test.json', {})
