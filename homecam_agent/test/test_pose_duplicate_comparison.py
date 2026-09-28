"""Synthetic duplicate/identity safeguards; not video accuracy measurements."""
import copy
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_pose_duplicates import (
    DuplicateConfig, pair_evidence, pose_from_dict, retained_observations, suppress_duplicates,
)
from homecam_detector.pose import PersonPose, PoseKeypoint
from replay_pose_duplicate_comparison import suppressed_person_score
from replay_pose_view_comparison import Pipeline


def pose(*, dx=0, score=.8, box=(.1, .1, .7, .9), confidence=.9):
    points = [('left_shoulder', .3, .3), ('right_shoulder', .4, .3),
              ('left_hip', .3, .55), ('right_hip', .4, .55),
              ('left_elbow', .2, .4), ('right_elbow', .5, .4)]
    return PersonPose(score, box, tuple(PoseKeypoint(n, x+dx, y, confidence) for n, x, y in points),
                      len(points) if confidence >= .5 else 0)


def observation(source, p=None):
    return dict(source=source, pose=p or pose())


def test_same_anatomy_across_views_keeps_actual_highest_score():
    a, b = pose(score=.3), pose(score=.8, box=(.15, .05, .65, .85))
    observations = [observation('full', a), observation('cw', b)]
    before = copy.deepcopy(observations)
    kept, detail = suppress_duplicates(observations)
    assert kept == (b,) and kept[0] is b
    assert detail['groups'][0]['suppressed'] == [0]
    assert observations == before


def test_full_view_wins_equal_score_without_merging_keypoints():
    a, b = pose(), pose(dx=.001)
    kept, detail = suppress_duplicates([observation('cw', a), observation('full', b)])
    assert kept == (b,) and detail['kept_indices'] == [1]


def test_same_view_not_deduplicated_even_when_boxes_and_points_equal():
    kept, detail = suppress_duplicates([observation('full'), observation('full')])
    assert len(kept) == 2 and detail['pair_evidence'][0]['reason'] == 'same_view'


def test_overlapping_people_with_different_named_points_are_preserved():
    kept, detail = suppress_duplicates([observation('full'), observation('cw', pose(dx=.15))])
    assert len(kept) == 2
    assert detail['pair_evidence'][0]['reason'] == 'keypoint_disagreement'


def test_one_conflicting_high_confidence_joint_blocks_suppression():
    p = pose()
    altered = replace(p, keypoints=(replace(p.keypoints[0], x=.7), *p.keypoints[1:]))
    kept, _ = suppress_duplicates([observation('full', p), observation('cw', altered)])
    assert len(kept) == 2


def test_named_left_right_points_not_swapped_to_force_agreement():
    p = pose()
    altered = replace(p, keypoints=tuple(replace(k, x=.7-k.x) for k in p.keypoints))
    kept, _ = suppress_duplicates([observation('full', p), observation('cw', altered)])
    assert len(kept) == 2


def test_keypoints_alone_do_not_merge_low_overlap_boxes():
    kept, detail = suppress_duplicates([observation('full'), observation('cw', pose(box=(.5,.1,.9,.9)))])
    assert len(kept) == 2 and detail['pair_evidence'][0]['reason'] == 'box_overlap'


def test_missing_pose_information_keeps_both_boxes():
    p = replace(pose(), keypoints=(), visible_keypoints=0)
    kept, detail = suppress_duplicates([observation('full', p), observation('cw', p)])
    assert len(kept) == 2 and detail['pair_evidence'][0]['reason'] == 'insufficient_shared_anatomy'


def test_low_confidence_joints_do_not_count_as_agreement():
    kept, _ = suppress_duplicates([observation('full'), observation('cw', pose(confidence=.49))])
    assert len(kept) == 2


def test_conflicting_low_confidence_joint_not_used_as_anatomy():
    p = pose()
    extra = replace(p, keypoints=(*p.keypoints, PoseKeypoint('nose', .9, .9, .1)))
    kept, _ = suppress_duplicates([observation('full', p), observation('cw', extra)])
    assert len(kept) == 1


def test_shoulder_agreement_without_hips_is_not_sufficient():
    p = pose()
    p = replace(p, keypoints=tuple(k for k in p.keypoints if 'hip' not in k.name), visible_keypoints=4)
    kept, _ = suppress_duplicates([observation('full', p), observation('cw', p)])
    assert len(kept) == 2


def test_three_matching_torso_points_without_four_body_points_not_sufficient():
    p = replace(pose(), keypoints=pose().keypoints[:3], visible_keypoints=3)
    kept, _ = suppress_duplicates([observation('full', p), observation('cw', p)])
    assert len(kept) == 2


def test_one_to_many_across_same_view_keeps_entire_component():
    kept, detail = suppress_duplicates([observation('full'), observation('cw'), observation('cw')])
    assert len(kept) == 3 and detail['groups'][0]['reason'] == 'ambiguous_component'


def test_transitive_similarity_does_not_merge_two_different_endpoints():
    kept, detail = suppress_duplicates([observation('full'), observation('cw', pose(dx=.03)),
                                       observation('ccw', pose(dx=.06))])
    assert len(kept) == 3 and detail['groups'][0]['reason'] == 'ambiguous_component'
    assert sum(p['compatible'] for p in detail['pair_evidence']) == 2


def test_complete_unique_view_component_keeps_one_measured_pose():
    kept, detail = suppress_duplicates([observation('full'), observation('cw', pose(dx=.005)),
                                       observation('ccw', pose(dx=.01))])
    assert len(kept) == 1 and len(detail['groups'][0]['suppressed']) == 2


def test_independent_people_groups_do_not_share_representative():
    a = pose(box=(.1,.1,.5,.9))
    b = pose(dx=.3, box=(.5,.1,.9,.9))
    kept, detail = suppress_duplicates([observation('full', a), observation('cw', a),
                                       observation('full', b), observation('cw', b)])
    assert kept == (a, b) and len(detail['groups']) == 2


def test_distances_use_actual_non_square_canvas_pixels():
    result = pair_evidence(observation('full'), observation('cw', pose(dx=.01)), DuplicateConfig())
    expected = 6.4/((.6*640)**2+(.8*400)**2)**.5
    assert result['mean_distance'] == pytest.approx(expected)


@pytest.mark.parametrize('field,value', [('minimum_iou',0),('minimum_iou',1.1),
    ('minimum_body_points',3),('minimum_body_points',4.5),('minimum_torso_points',2),
    ('keypoint_confidence',False),('maximum_distance',float('nan')),('maximum_mean_distance',.2)])
def test_invalid_configs_rejected(field, value):
    with pytest.raises(ValueError):
        DuplicateConfig(**{field:value})


@pytest.mark.parametrize('change', ['box', 'score', 'name', 'duplicate', 'coordinate', 'visible'])
def test_malformed_pose_is_not_silently_fixed(change):
    value = pose().as_dict()
    if change == 'box': value['box']['right'] = -.1
    elif change == 'score': value['boxConfidence'] = float('inf')
    elif change == 'name': value['keypoints'][0]['name'] = 'invented_joint'
    elif change == 'duplicate': value['keypoints'][0]['name'] = value['keypoints'][1]['name']
    elif change == 'coordinate': value['keypoints'][0]['x'] = 1.1
    else: value['visibleKeypoints'] = True
    with pytest.raises(ValueError):
        pose_from_dict(value)


def test_roundtrip_preserves_frozen_pose():
    p = pose()
    assert pose_from_dict(p.as_dict()) == p


def test_view_provenance_cannot_be_changed_or_missing():
    p = pose().as_dict()
    row = dict(poses=[p], view_predictions=[dict(fused_index=0, source='full', pose=copy.deepcopy(p))])
    assert retained_observations(row, 'tiles')[0]['pose'] == pose()
    changed = copy.deepcopy(row); changed['view_predictions'][0]['pose']['boxConfidence'] = .4
    with pytest.raises(ValueError): retained_observations(changed, 'tiles')
    changed = copy.deepcopy(row); changed['view_predictions'] = []
    with pytest.raises(ValueError): retained_observations(changed, 'tiles')


def test_duplicate_suppression_does_not_invent_high_confidence_or_normal_clearance():
    observations = [observation('full', pose(score=.2)), observation('cw', pose(score=.3))]
    pipeline = Pipeline()
    for i in range(4):
        kept, _ = suppress_duplicates(observations)
        row = pipeline.step(kept, i, 100+i*.25, b'test', False, .01, [])
        assert row['poses'][0]['boxConfidence'] == .3
        assert all(not t['associationUsable'] for t in row['candidate_payload']['tracks'])
        assert all(t['subjectCheck']['state'] == 'unknown' for t in row['candidate_payload']['tracks'])


def test_posthoc_pair_audit_exposes_different_people_and_unknown_frames():
    a, b = pose(box=(.1,.1,.3,.9)), pose(box=(.5,.1,.7,.9))
    group = dict(indices=[0,1], kept=0, suppressed=[1])
    rows = [dict(source_frame=f, duplicate_evidence=dict(groups=[group])) for f in (0,1)]
    original = [dict(source_frame=f, poses=[a.as_dict(),b.as_dict()]) for f in (0,1)]
    annotation = dict(persons=[dict(person_id='P1', role='target', boxes=[[0,64,40,192,360]]),
                              dict(person_id='P2', role='other', boxes=[[0,320,40,448,360]])])
    result = suppressed_person_score(rows, original, annotation, dict(width=640,height=400),
                                    dict(visible_coverage=.5,prediction_coverage=.25,margin=.1))
    assert [r['status'] for r in result] == ['different_people','unverified']


def test_posthoc_pair_audit_counts_same_person_only_with_exact_gt_for_both():
    p = pose().as_dict()
    rows = [dict(source_frame=0, duplicate_evidence=dict(groups=[dict(indices=[0,1],kept=0,suppressed=[1])]))]
    original = [dict(source_frame=0,poses=[p,p])]
    annotation = dict(persons=[dict(person_id='P1',role='target',boxes=[[0,64,40,448,360]])])
    result = suppressed_person_score(rows, original, annotation, dict(width=640,height=400),
                                    dict(visible_coverage=.5,prediction_coverage=.25,margin=.1))
    assert result[0]['status'] == 'same_person_on_exact_gt'
