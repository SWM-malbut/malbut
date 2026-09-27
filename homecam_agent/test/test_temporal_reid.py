"""Synthetic temporal contracts, not real-world ReID accuracy evidence."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_temporal_reid import TemporalReIDConfig, TemporalReIDTracker
from homecam_detector.pose import PersonPose, PoseKeypoint
from homecam_detector.pose_tracker import PersonPoseTracker
from replay_pose_view_comparison import Pipeline
from replay_temporal_reid import replay_poses, score_temporal


def pose(box=(.1,.1,.3,.5),score=.9):
    return PersonPose(score,box,(),0)


def observed(result):
    return [t for t in result.tracks if t.pose is not None]


def constant(p,now):
    return [1.,0.]


@pytest.mark.parametrize('key', ['minimum_cosine','appearance_weight'])
@pytest.mark.parametrize('value', [True,0,-1,float('inf'),float('nan'),3.])
def test_invalid_experiment_config(key,value):
    with pytest.raises(ValueError): TemporalReIDConfig(**{key:value})


def test_invalid_arm_does_not_silently_ignore_reid():
    with pytest.raises(ValueError): TemporalReIDTracker()
    with pytest.raises(ValueError): TemporalReIDTracker(constant,use_reid='yes')


def test_instrumented_baseline_exactly_matches_original_tracker():
    actual=PersonPoseTracker(); trial=TemporalReIDTracker(use_reid=False)
    trial._prefix=actual._prefix
    for t,poses in [(0,[pose()]),(.2,[pose(score=.2)]),(.4,[]),(.6,[pose()]),
                    (2,[pose(),pose((.6,.1,.8,.5))]),(2.2,[pose((.2,.1,.4,.5))])]:
        assert actual.update(poses,t)==trial.update(poses,t)


def test_same_person_keeps_id_with_actual_past_reference():
    tracker=TemporalReIDTracker(constant)
    first=observed(tracker.update([pose()],0))[0]
    current=observed(tracker.update([pose((.11,.1,.31,.5))],.2))[0]
    assert first.track_id==current.track_id and len(current.history)==2
    d=tracker.frame_diagnostic['accepted'][0]
    assert d['previous_at']==0 and d['observed_at']==.2 and d['cosine']==1
    assert d['used_reid']


def test_appearance_change_at_same_position_cannot_borrow_old_history():
    tracker=TemporalReIDTracker(lambda p,t:[1.,0.] if t==0 else [0.,1.])
    first=observed(tracker.update([pose()],0))[0]
    second=tracker.update([pose()],.2)
    current=observed(second)[0]
    assert current.track_id!=first.track_id and current.observation_count==1
    assert next(t for t in second.tracks if t.track_id==first.track_id).pose is None
    assert tracker.frame_diagnostic['pair_costs'][0]['reason']=='appearance_gate'
    assert tracker.frame_diagnostic['accepted']==[]


def test_identical_clothes_cannot_override_geometric_gate():
    tracker=TemporalReIDTracker(constant)
    first=observed(tracker.update([pose()],0))[0]
    current=observed(tracker.update([pose((.75,.6,.95,.95))],.2))[0]
    assert current.track_id!=first.track_id
    assert tracker.frame_diagnostic['pair_costs'][0]['reason']=='geometry_gate'


def test_distinct_appearance_disambiguates_close_candidates_one_to_one():
    left,right=(.1,.1,.3,.5),(.3,.1,.5,.5)
    next_left,next_right=(.2,.1,.4,.5),(.22,.1,.42,.5)
    def feature(p,now):
        a=left if now==0 else next_left
        return [1.,0.] if p.box==a else [0.,1.]
    base=PersonPoseTracker(); trial=TemporalReIDTracker(feature)
    initial=[pose(left),pose(right)]
    previous={t.pose.box:t.track_id for t in observed(trial.update(initial,0))}
    base.update(initial,0)
    current=[pose(next_left),pose(next_right)]
    assert len(observed(base.update(current,.2)))<2
    after=observed(trial.update(current,.2))
    assert len(after)==2 and len({t.track_id for t in after})==2
    assert {t.pose.box:t.track_id for t in after}=={next_left:previous[left],next_right:previous[right]}


def test_appearance_tie_keeps_ambiguous_observation_unassigned_and_unlearned():
    tracker=TemporalReIDTracker(constant)
    tracker.update([pose(),pose((.3,.1,.5,.5))],0)
    before=copy.deepcopy(tracker._references)
    result=tracker.update([pose((.2,.1,.4,.5))],.2)
    assert len(result.unassigned)==1 and all(t.pose is None for t in result.tracks)
    assert tracker.frame_diagnostic['accepted']==[]
    assert set(before)==set(tracker._references)
    assert all(r['observed_at']==0 for r in tracker._references.values())


def test_missing_competitor_feature_reverts_entire_frame_not_one_cost():
    left,right=pose(),pose((.3,.1,.5,.5))
    def feature(p,now):
        return None if now==0 and p.box==right.box else [1.,0.]
    trial=TemporalReIDTracker(feature); base=PersonPoseTracker(); trial._prefix=base._prefix
    assert trial.update([left,right],0)==base.update([left,right],0)
    merged=pose((.2,.1,.4,.5))
    assert trial.update([merged],.2)==base.update([merged],.2)
    assert trial.frame_diagnostic['fallback_reason']=='missing_competing_feature'
    assert all(d['reason']=='baseline_geometry' for d in trial.frame_diagnostic['pair_costs'])


def test_weak_repeat_is_not_upgraded_by_appearance():
    tracker=TemporalReIDTracker(constant)
    for t in (0,.2,.4):
        current=observed(tracker.update([pose(score=.2)],t))[0]
    assert current.state=='tracked' and current.confidence_level=='weak'
    assert current.pose.box_confidence==.2


def test_gap_keeps_old_reference_without_fabricating_current_observation():
    tracker=TemporalReIDTracker(constant)
    first=observed(tracker.update([pose()],0))[0]
    missing=tracker.update([],.2).tracks[0]
    assert missing.pose is None and missing.state=='missing' and missing.observation_count==1
    assert next(iter(tracker._references.values()))['observed_at']==0
    current=observed(tracker.update([pose()],.4))[0]
    assert current.track_id==first.track_id and current.consecutive_observations==1
    assert [h.observed_at for h in current.history]==[0,.4]
    assert tracker.frame_diagnostic['accepted'][0]['gap_s']==.4


def test_expiry_and_reset_do_not_restore_old_ids_even_when_appearance_matches():
    tracker=TemporalReIDTracker(constant)
    first=observed(tracker.update([pose()],0))[0].track_id
    second=tracker.update([pose()],1.01)
    assert second.expired_track_ids==(first,)
    second_id=observed(second)[0].track_id
    assert first!=second_id and len(tracker._references)==1
    tracker.reset()
    assert tracker._references=={} and tracker._current=={}
    third=observed(tracker.update([pose()],0))[0].track_id
    assert third not in (first,second_id)


def test_gallery_does_not_grow_unbounded_or_learn_overflow():
    tracker=TemporalReIDTracker(constant,max_people=1)
    for i in range(40):
        result=tracker.update([pose(),pose((.7,.1,.9,.5),score=.8)],i*.2)
        assert len(tracker._references)==1 and len(result.unassigned)==1
    assert len(result.tracks[0].history)==30


def test_mutated_provider_vector_does_not_rewrite_reference():
    feature=np.array([1.,0.])
    tracker=TemporalReIDTracker(lambda p,t:feature)
    first=observed(tracker.update([pose()],0))[0].track_id
    feature[:]=[0.,1.]
    assert observed(tracker.update([pose()],.2))[0].track_id!=first


@pytest.mark.parametrize('bad', [[0.,0.],[float('nan'),0.],[[1,0]],[]])
def test_invalid_embedding_does_not_partially_advance_state(bad):
    tracker=TemporalReIDTracker(constant)
    first=observed(tracker.update([pose()],0))[0]
    tracker.feature_provider=lambda p,t:bad
    with pytest.raises(ValueError): tracker.update([pose()],.2)
    tracker.feature_provider=constant
    second=observed(tracker.update([pose()],.2))[0]
    assert second.track_id==first.track_id and second.observation_count==2


def test_invalid_time_or_pose_does_not_consult_encoder_or_modify_reference():
    tracker=TemporalReIDTracker(constant)
    tracker.update([pose()],0)
    def forbidden(*args): raise AssertionError('validation must run first')
    tracker.feature_provider=forbidden
    with pytest.raises(ValueError): tracker.update([pose()],0)
    bad=PersonPose(.9,pose().box,(PoseKeypoint('nose',float('nan'),.2,.9),),1)
    with pytest.raises(ValueError): tracker.update([bad],.2)
    assert next(iter(tracker._references.values()))['observed_at']==0


def frozen_rows():
    pipeline=Pipeline()
    originals={0:b'jpeg0',1:b'jpeg1',2:b'jpeg2'}
    rows=[pipeline.step((pose(),),i,100+i*.2,originals[i],True,0,[]) for i in originals]
    features=SimpleNamespace(record=dict(evidence=dict(frame_indices=[0,1,2])),originals=originals)
    return rows,features


@pytest.mark.parametrize('serialized',[False,True])
def test_baseline_full_candidate_and_tracking_fields_reproduce(serialized):
    rows,features=frozen_rows()
    if serialized: rows=json.loads(json.dumps(rows))
    result=replay_poses(rows,features,'baseline')
    for old,new in zip(rows,result):
        assert all(json.loads(json.dumps(old[k]))==json.loads(json.dumps(new[k])) for k in old)


def test_temporal_audit_does_not_interpolate_missing_identity_frames():
    rows,features=frozen_rows()
    result=replay_poses(rows,features,'baseline')
    annotation=dict(case_id='synthetic',persons=[dict(person_id='one',role='target',
        boxes=[[0,64,40,192,200],[2,64,40,192,200]])])
    score=score_temporal(result,annotation,dict(width=640,height=400),
                         dict(visible_coverage=.5,prediction_coverage=.25,margin=.1))
    assert score['edge_counts']=={'unverified':2}
    assert [r['source_frame'] for r in score['anchors']]==[0,2]
    assert score['matched_anchor_id_changes']==0


def test_temporal_audit_detects_same_track_borrowing_different_person():
    rows,features=frozen_rows()
    result=replay_poses(rows,features,'baseline')
    annotation=dict(case_id='synthetic',persons=[
        dict(person_id='one',role='target',boxes=[[0,64,40,192,200]]),
        dict(person_id='two',role='other',boxes=[[1,64,40,192,200],[2,64,40,192,200]])])
    score=score_temporal(result,annotation,dict(width=640,height=400),
                         dict(visible_coverage=.5,prediction_coverage=.25,margin=.1))
    assert score['edge_counts']=={'different_gt_people':1,'same_gt_person':1}
    assert len(score['mixed_identity_tracks'])==1
    assert score['mixed_identity_tracks'][0]['person_ids']==['one','two']
