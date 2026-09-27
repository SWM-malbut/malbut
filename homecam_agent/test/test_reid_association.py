"""Synthetic contract/safety tests, not measured model accuracy."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_reid_association import (
    ARMS, AssociationExperiment, ReIDConfig, cosine, offline_associator,
)
from replay_reid_association import FrozenFeatures, deduplicated_diagnostics
from replay_reviewed_pose_cloud import replay_scene
from test_reviewed_pose_cloud_replay import fixture

BOX = (.1, .3, .7, .8)


def pose(key='target', token='continuous', box=BOX, usable=True):
    return key, token, SimpleNamespace(box=box, association_usable=usable)


def finding(*regions):
    return SimpleNamespace(regions=tuple(SimpleNamespace(frame_index=i, box=box) for i, box in regions))


def constant(index, box):
    return [1., 0.]


@pytest.mark.parametrize('field', ['minimum_iou','iou_margin','minimum_cosine','cosine_margin'])
@pytest.mark.parametrize('value', [0, -1, 1.01, True, float('nan'), float('inf')])
def test_invalid_trial_settings(field, value):
    with pytest.raises(ValueError):
        ReIDConfig(**{field:value})


@pytest.mark.parametrize('a,b', [(None,[1,0]), ([0,0],[1,0]), ([float('nan')],[1]),
    ([float('inf')],[1]), ([],[]), ([1],[1,0]), ([[1,0]],[[1,0]])])
def test_invalid_features_do_not_become_high_scores(a,b):
    assert cosine(a,b) is None


def test_cosine_normalizes_and_is_not_identity_probability():
    assert cosine([2,0],[10,0]) == 1.
    assert cosine([1,0],[0,2]) == 0.
    assert cosine([1,0],[-1,0]) == -1.


@pytest.mark.parametrize('arm', ARMS)
def test_same_supported_person_on_every_frame(arm):
    experiment=AssociationExperiment(arm,constant)
    result=experiment(finding((0,BOX),(1,BOX)), [[pose()],[pose()]])
    assert (result.reason,result.subject_key,result.token)==('matched','target','continuous')


def test_relaxed_iou_control_separates_geometry_change_from_reid():
    narrow=(.1,.3,.3,.8)  # IoU 1/3, no magic identity information in this test.
    snapshot=[[pose(box=narrow)],[pose(box=narrow)]]
    query=finding((0,BOX),(1,BOX))
    assert AssociationExperiment('baseline',constant)(query,snapshot).reason=='no_matching_track'
    assert AssociationExperiment('wider_iou',constant)(query,snapshot).reason=='matched'
    assert AssociationExperiment('wider_iou_reid',constant)(query,snapshot).reason=='matched'


def test_same_appearance_cannot_override_no_geometric_candidate():
    snapshot=[[pose(box=(.8,.1,.95,.9))]]*2
    assert AssociationExperiment('wider_iou_reid',constant)(
        finding((0,BOX),(1,BOX)),snapshot).reason=='no_matching_track'


@pytest.mark.parametrize('token,usable', [(None,False),(None,True),('x',False)])
def test_appearance_cannot_create_observation_permission_or_token(token,usable):
    assert AssociationExperiment('wider_iou_reid',constant)(finding((0,BOX),(1,BOX)),
        [[pose(token=token,usable=usable)]]*2).reason=='track_unusable'


@pytest.mark.parametrize('second', [pose(token='new'), pose(key='helper')])
def test_appearance_does_not_stitch_different_people_or_track_segments(second):
    assert AssociationExperiment('wider_iou_reid',constant)(
        finding((0,BOX),(1,BOX)),[[pose()],[second]]).reason=='track_changed'


def test_weak_competitor_is_not_removed_to_select_strong_helper():
    snapshot=[[pose(),pose(key='weak',token=None,usable=False)]]*2
    result=AssociationExperiment('wider_iou_reid',constant)(finding((0,BOX),(1,BOX)),snapshot)
    assert result.reason=='ambiguous_appearance' and result.subject_key is None


def test_reid_can_choose_unique_candidate_but_not_skip_low_similarity():
    helper=(.11,.3,.71,.8)
    snapshot=[[pose(),pose(key='helper',box=helper)]]*2
    def appearance(index,box):
        return [1.,0.] if tuple(box)==BOX else [0.,1.]
    query=finding((0,BOX),(1,BOX))
    assert AssociationExperiment('wider_iou',appearance)(query,snapshot).reason=='ambiguous_tracks'
    assert AssociationExperiment('wider_iou_reid',appearance)(query,snapshot).subject_key=='target'
    assert AssociationExperiment('wider_iou_reid',appearance)(
        query,[[pose(box=helper)]]*2).reason=='appearance_mismatch'


def test_missing_competitor_feature_is_unknown_not_negative_evidence():
    helper=(.11,.3,.71,.8)
    def appearance(index,box):
        return [1.,0.] if tuple(box)==BOX else None
    snapshot=[[pose(),pose(key='helper',box=helper)]]*2
    assert AssociationExperiment('wider_iou_reid',appearance)(
        finding((0,BOX),(1,BOX)),snapshot).reason=='appearance_unavailable'


@pytest.mark.parametrize('regions,reason', [
    ([(0,BOX)],'insufficient_locations'), ([(0,BOX),(2,BOX)],'invalid_sample_index'),
    ([(0,BOX),(-1,BOX)],'invalid_sample_index'), ([(0,BOX),(True,BOX)],'invalid_sample_index'),
    ([(0,BOX),(1,(-.1,.3,.7,.8))],'invalid_box')])
def test_no_subset_selection_or_invalid_index(regions,reason):
    assert AssociationExperiment('wider_iou_reid',constant)(
        finding(*regions),[[pose()],[pose()]]).reason==reason


def test_all_region_failures_remain_in_diagnostics():
    experiment=AssociationExperiment('wider_iou_reid',constant)
    query=finding((0,BOX),(1,BOX))
    snapshot=[[pose(token=None,usable=False)],[]]
    assert experiment(query,snapshot).reason=='track_unusable'
    assert [r['reason'] for r in experiment.records[0]['regions']]==['track_unusable','no_matching_track']
    experiment(query,snapshot)
    assert len(deduplicated_diagnostics(experiment))==1


def test_injection_is_scoped_and_restored_even_on_failure():
    import replay_reviewed_pose_cloud as replay
    import malbut_agent_server.application.cloud_fall_monitor as monitor
    original_replay, original_monitor = replay.associate_finding, monitor.associate_finding
    trial=AssociationExperiment('wider_iou_reid',constant)
    with pytest.raises(RuntimeError):
        with offline_associator(trial):
            assert replay.associate_finding is monitor.associate_finding is trial
            raise RuntimeError('synthetic')
    assert replay.associate_finding is original_replay
    assert monitor.associate_finding is original_monitor


@pytest.mark.parametrize('arm', ARMS)
def test_actual_incident_merge_repeat_budget_and_db_survive(arm,tmp_path):
    rows,originals,record,result,config,_=fixture()
    untouched=copy.deepcopy((rows,record,result))
    with offline_associator(AssociationExperiment(arm,constant)):
        replay=replay_scene(rows,originals,record,result,config,tmp_path/'trial.sqlite')
    assert replay['before'][0]['incident_id']==replay['after'][0]['incident_id']
    assert replay['after'][0]['sources']==('yolo_pose','cloud_crosscheck')
    assert replay['after'][0]['rechecks']==0
    assert replay['repeat_incidents_unchanged'] and replay['journal_reopen_verified']
    assert (rows,record,result)==untouched


@pytest.mark.parametrize('change,reason', [('weak','track_unusable'),('lost','track_changed')])
def test_real_monitor_keeps_unsafe_discovery_unidentified(tmp_path,change,reason):
    rows,originals,record,result,config,_=fixture()
    if change=='weak': rows[0]['candidate_payload']['tracks'][0]['associationUsable']=False
    else: rows[1]['candidate_payload']['tracks']=[]
    with offline_associator(AssociationExperiment('wider_iou_reid',constant)):
        replay=replay_scene(rows,originals,record,result,config,tmp_path/'trial.sqlite')
    d=next(e['discovery'] for e in replay['events'] if e['discovery'])
    assert d['reason']==reason and d['subject_key'] is None
    assert d['association_status']=='unidentified'
    assert replay['before']==[i for i in replay['after'] if i['subject_key'] is not None]
    assert sum(i['subject_key'] is None for i in replay['after'])==1


@pytest.mark.parametrize('change', ['normal','invalid'])
def test_reid_does_not_rescue_invalid_reply_or_close_scene_normal(tmp_path,change):
    rows,originals,record,result,config,_=fixture()
    if change=='invalid': result.update(outcome='invalid_response',normalized_response=None)
    else: result['normalized_response'].update(assessment='normal_activity',findings=[])
    def forbidden(*args):
        raise AssertionError('no finding means no embedding')
    with offline_associator(AssociationExperiment('wider_iou_reid',forbidden)):
        replay=replay_scene(rows,originals,record,result,config,tmp_path/'trial.sqlite')
    assert replay['before']==replay['after'] and not replay['findings']


def image_features():
    import cv2
    import hashlib
    ok, data=cv2.imencode('.jpg', np.zeros((400,640,3),dtype=np.uint8))
    assert ok
    jpeg=data.tobytes()
    record=dict(evidence=dict(frame_indices=[10,20],jpeg_sha256=[hashlib.sha256(jpeg).hexdigest()]*2))
    class FakeEncoder:
        calls=0
        def encode(self,image,detections):
            self.calls+=1
            assert image.shape==(400,640,3)
            return [np.ones(512,dtype=np.float32)/np.sqrt(512)]
    return FrozenFeatures({10:jpeg,20:jpeg},record,FakeEncoder())


def test_feature_cache_keys_include_frame_and_box():
    cache=image_features()
    assert np.array_equal(cache(0,BOX),cache(0,list(BOX)))
    cache(1,BOX)
    assert cache.encoder.calls==2 and len(cache.entries)==2


def test_feature_hash_guard_and_no_coordinate_clamping():
    cache=image_features(); cache.originals[10]=b'changed'
    with pytest.raises(ValueError,match='RGB changed'): cache(0,BOX)
    with pytest.raises(ValueError,match='no clamping'): cache(0,(-.1,.1,.8,.9))
    with pytest.raises(ValueError,match='outside'): cache(-1,BOX)


def test_existing_encoder_skips_tiny_crop_without_network():
    from malbut_reid.reid.osnet_encoder import OsNetPersonEncoder
    from malbut_reid.models import BoundingBox, ImageDetection
    class Network:
        def setPreferableBackend(self,_): pass
        def setPreferableTarget(self,_): pass
        def setInput(self,_): raise AssertionError('tiny crop must not run inference')
    encoder=OsNetPersonEncoder('unused',network=Network(),dnn_target='cpu')
    assert encoder.encode(np.zeros((400,640,3),np.uint8),[ImageDetection(BoundingBox(0,0,10,20),1.)])==[None]
