"""Offline fixed-view geometry/score tests; no model or provider calls."""
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_pose_views import fixed_tiles, merge_views, project_tile, restore_rotation
from homecam_detector.pose import PersonPose, PoseKeypoint
from replay_pose_view_comparison import Pipeline, detection_score


def pose(box=(.1,.2,.4,.8),score=.8):
    return PersonPose(score,box,(PoseKeypoint('left_wrist',.15,.25,.7),
                                PoseKeypoint('right_wrist',.3,.6,0)),1)


def test_clockwise_inverse_rect_and_anatomical_names():
    p=restore_rotation(pose(),'cw')
    assert p.box==pytest.approx((.2,.6,.8,.9))
    assert (p.keypoints[0].x,p.keypoints[0].y)==pytest.approx((.25,.85))
    assert p.keypoints[0].name=='left_wrist'
    assert p.keypoints[1].name=='right_wrist' and p.keypoints[1].confidence==0
    assert p.box_confidence==.8 and p.visible_keypoints==1


def test_counterclockwise_inverse_rect():
    p=restore_rotation(pose(),'ccw')
    assert p.box==pytest.approx((.2,.1,.8,.4))
    assert (p.keypoints[0].x,p.keypoints[0].y)==pytest.approx((.75,.15))


@pytest.mark.parametrize('box',[(0,0,1,1),(.1,.2,.4,.8),(.5,0,1,.2)])
def test_inverse_transforms_round_trip(box):
    original=pose(box)
    restored=restore_rotation(restore_rotation(original,'cw'),'ccw')
    assert restored.box==pytest.approx(original.box)
    for a,b in zip(restored.keypoints,original.keypoints):
        assert (a.x,a.y)==pytest.approx((b.x,b.y))
        assert a.name==b.name and a.confidence==b.confidence


@pytest.mark.parametrize('box',[(-.1,0,.5,.5),(0,0,0,1),(0,0,1,1.1),(0,float('nan'),1,1)])
def test_invalid_rotation_box_rejected(box):
    with pytest.raises(ValueError):restore_rotation(pose(box),'cw')


def test_unknown_rotation_rejected():
    with pytest.raises(ValueError):restore_rotation(pose(),'upside_down')


def test_fixed_tiles_cover_canvas_and_do_not_take_person_inputs():
    tiles=fixed_tiles((640,400))
    assert tiles==((0,0,400,400),(240,0,640,400))
    assert tiles[0][2]>tiles[1][0]
    with pytest.raises(ValueError):fixed_tiles((1280,720))


def test_crop_coordinates_return_to_the_unmodified_rgb_canvas():
    p,reason=project_tile(pose(),(240,0,640,400),(640,400))
    assert reason is None
    assert p.box==pytest.approx((280/640,.2,400/640,.8))
    assert (p.keypoints[0].x,p.keypoints[0].y)==pytest.approx((300/640,.25))


@pytest.mark.parametrize('box,rect',[
    ((.2,.1,1,.8),(0,0,400,400)),((0,.1,.5,.8),(240,0,640,400))])
def test_internal_crop_border_does_not_create_a_partial_person(box,rect):
    p,reason=project_tile(pose(box),rect,(640,400))
    assert p is None and reason=='internal_crop_edge'


def test_original_image_border_is_not_an_internal_crop_border():
    p,reason=project_tile(pose((0,0,.5,1)),(0,0,400,400),(640,400))
    assert p is not None and reason is None


def test_stronger_observed_duplicate_wins_without_averaging_or_fake_confidence():
    full=pose(score=.2); rotated=pose(score=.8)
    merged,raw=merge_views([dict(source='full',pose=full),dict(source='cw',pose=rotated)])
    assert merged==(rotated,) and merged[0].box_confidence==.8
    assert len(raw)==2 and raw[1]['duplicate_of']==0


def test_full_view_wins_score_tie_and_keeps_its_actual_keypoints():
    full=pose(); rotated=replace(full,keypoints=())
    merged,raw=merge_views([dict(source='cw',pose=rotated),dict(source='full',pose=full)])
    assert merged==(full,) and raw[0]['source']=='full'


def test_distinct_and_partial_overlaps_remain_competitors():
    a=pose((.1,.1,.4,.8));b=pose((.2,.1,.5,.8));c=pose((.7,.1,.9,.8))
    merged,raw=merge_views([dict(source='full',pose=a),dict(source='cw',pose=b),dict(source='ccw',pose=c)])
    assert len(merged)==3 and all(r['duplicate_of'] is None for r in raw)


def test_fusion_order_does_not_generate_a_track_or_fall_label():
    merged,raw=merge_views([dict(source='cw',pose=pose())])
    assert set(raw[0])=={'raw_index','source','pose','duplicate_of','fused_index'}
    assert all('track_id' not in r and 'assessment' not in r for r in raw)


def test_inference_pipeline_does_not_turn_a_box_without_anatomy_into_fall():
    pipeline=Pipeline()
    observations=[]
    for i in range(4):
        row=pipeline.step([pose()],i,100+i*.2,b'\xff\xd8test\xff\xd9',True,.01,[])
        observations.append(row)
        assert row['candidate_payload']['candidates']==[]
        assert row['candidate_payload']['tracks'][0]['associationUsable'] is False
    assert len({r['tracks'][0]['track_id'] for r in observations})==1


def test_person_score_requires_exact_approved_frame_and_separates_strong():
    rows=[dict(source_frame=f,poses=[pose((.1,.1,.5,.8),.2).as_dict()]) for f in (0,1)]
    annotation=dict(persons=[dict(person_id='target',role='target',boxes=[[0,64,40,320,320]])])
    result=detection_score(rows,annotation,dict(width=640,height=400),
                           dict(visible_coverage=.5,prediction_coverage=.25,margin=.1))
    assert len(result)==1 and result[0]['source_frame']==0
    assert result[0]['all_status']=='matched' and result[0]['strong_status']=='unlinked'


def test_no_gt_is_not_a_success_or_automatic_background_verdict():
    result=detection_score([dict(source_frame=1,poses=[pose().as_dict()])],dict(persons=[]),
                           dict(width=640,height=400),dict(visible_coverage=.5,prediction_coverage=.25,margin=.1))
    assert result==[]
