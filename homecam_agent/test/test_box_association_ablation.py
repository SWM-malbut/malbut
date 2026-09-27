"""Offline prototype tests. Synthetic inputs are not measured accuracy samples."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_box_association import BoxAssociationConfig, box_subject_frame
from replay_box_association_ablation import outcome, replay_box_only, score_linked_people
from replay_reviewed_pose_cloud import replay_scene
from test_reviewed_pose_cloud_replay import fixture as base_fixture


def fixture(*, anatomy=False, score=.8):
    rows,originals,record,result,config,meta=base_fixture()
    for row in rows:
        diagnostic=row['candidate_payload']['tracks'][0]
        diagnostic['features']['usable']=anatomy
        diagnostic['associationUsable']=bool(anatomy and score>=.45)
        diagnostic['subjectCheck']['state']='unknown'
        row['tracks']=[dict(track_id='raw-id',state='tracked',
            confidence='strong' if score>=.45 else 'weak',pose=dict(boxConfidence=score,
                box=dict(zip(('left','top','right','bottom'),diagnostic['box']))))]
        row['unassigned']=[]
    return rows,originals,record,result,config,meta


def discovery(replay):
    return next(e['discovery'] for e in replay['events'] if e['discovery'])


def test_location_no_longer_requires_usable_anatomy_and_never_claims_clear():
    rows,*_=fixture()
    before=copy.deepcopy(rows)
    frame,decisions=box_subject_frame(rows[0])
    assert frame.subjects[0].association_usable
    assert frame.subjects[0].state.value=='unknown'
    assert decisions[0]['baseline_usable'] is False
    assert decisions[0]['feature_usable'] is False
    assert rows==before


@pytest.mark.parametrize('score,allowed',[(.1,False),(.449999,False),(.45,True),(.9,True)])
def test_score_threshold_is_unchanged(score,allowed):
    rows,*_=fixture(score=score)
    frame,_=box_subject_frame(rows[0])
    assert frame.subjects[0].association_usable is allowed


@pytest.mark.parametrize('state',['tentative','ambiguous','missing'])
def test_track_must_be_confirmed_even_with_strong_box(state):
    rows,*_=fixture()
    rows[0]['tracks'][0]['state']=state
    rows[0]['candidate_payload']['tracks'][0]['trackingState']=state
    frame,decisions=box_subject_frame(rows[0])
    assert not frame.subjects[0].association_usable
    assert 'track_not_confirmed' in decisions[0]['reasons']


def test_global_unassigned_guard_is_not_silently_removed():
    rows,*_=fixture()
    rows[0]['unassigned']=[dict(reason='association_ambiguous')]
    rows[0]['candidate_payload']['unassignedCount']=1
    frame,decisions=box_subject_frame(rows[0])
    assert not frame.subjects[0].association_usable
    assert 'unassigned_detections' in decisions[0]['reasons']


@pytest.mark.parametrize('change',['time','count','id','state','box','confidence','missing_box','invalid_geometry'])
def test_inconsistent_or_invented_observation_is_rejected(change):
    rows,*_=fixture()
    row=rows[0];diagnostic=row['candidate_payload']['tracks'][0];track=row['tracks'][0]
    if change=='time': row['captured_at']+=1
    elif change=='count': row['candidate_payload']['unassignedCount']=1
    elif change=='id': track['track_id']='invented'
    elif change=='state': track['state']='ambiguous'
    elif change=='box': diagnostic['box']=[.2,.2,.3,.3]
    elif change=='confidence': track['confidence']='weak'
    elif change=='missing_box': track['pose']=None
    else:
        diagnostic['box'][0]=-.1
        track['pose']['box']['left']=-.1
    with pytest.raises(ValueError): box_subject_frame(row)


@pytest.mark.parametrize('value',[True,0,-1,1.1,float('inf'),float('nan')])
def test_invalid_threshold_rejected(value):
    with pytest.raises(ValueError): BoxAssociationConfig(value)


def test_prototype_merges_where_only_anatomy_blocked_without_changing_candidates(tmp_path):
    rows,originals,record,result,config,_=fixture()
    before=copy.deepcopy((rows,record,result))
    baseline=replay_scene(rows,originals,record,result,config,tmp_path/'baseline.sqlite')
    proposed=replay_box_only(rows,originals,record,result,config,tmp_path/'prototype.sqlite')
    assert discovery(baseline)['reason']=='track_unusable'
    assert discovery(proposed)['reason']=='matched'
    assert proposed['before'][0]['incident_id']==proposed['after'][0]['incident_id']
    assert proposed['after'][0]['sources']==('yolo_pose','cloud_crosscheck')
    assert proposed['repeat_incidents_unchanged'] and proposed['journal_reopen_verified']
    assert proposed['before'][0]['rechecks']==proposed['after'][0]['rechecks']==0
    assert not any(e['kind'] in ('notification_requested','incident_resolved') for e in proposed['events'])
    assert (rows,record,result)==before


def test_production_ros_contract_is_not_covertly_relaxed(tmp_path):
    rows,originals,record,result,config,_=fixture()
    for row in rows:
        row['candidate_payload']['tracks'][0]['associationUsable']=True
    # Current production adapter still requires anatomy for this old field.
    with pytest.raises(ValueError,match='unsupported subject association'):
        replay_scene(rows,originals,record,result,config,tmp_path/'bad-contract.sqlite')


def add_person(rows,box):
    for row in rows:
        diagnostic=copy.deepcopy(row['candidate_payload']['tracks'][0])
        diagnostic.update(targetTrackId='helper',box=list(box))
        row['candidate_payload']['tracks'].append(diagnostic)
        track=copy.deepcopy(row['tracks'][0]);track['track_id']='helper'
        track['pose']['box']=dict(zip(('left','top','right','bottom'),box))
        row['tracks'].append(track)


@pytest.mark.parametrize('condition,reason',[
    ('weak','track_unusable'),('unassigned','track_unusable'),
    ('disappeared','track_changed'),('person_switch','track_changed'),
    ('overlap','ambiguous_tracks'),('no_target','no_matching_track')])
def test_rejections_are_preserved_in_actual_merge(tmp_path,condition,reason):
    rows,originals,record,result,config,_=fixture(score=.2 if condition=='weak' else .8)
    if condition=='unassigned':
        rows[0]['unassigned']=[dict(reason='association_ambiguous')]
        rows[0]['candidate_payload']['unassignedCount']=1
    elif condition=='disappeared':
        rows[1]['tracks'][0].update(state='missing',pose=None)
        rows[1]['candidate_payload']['tracks'][0].update(trackingState='missing',box=None,features=None)
    elif condition=='person_switch':
        helper=[.8,.1,.95,.9];add_person(rows,helper)
        result['normalized_response']['findings'][0]['regions'][-1]['box']=helper
    elif condition=='overlap': add_person(rows,rows[0]['candidate_payload']['tracks'][0]['box'])
    elif condition=='no_target':
        for region in result['normalized_response']['findings'][0]['regions']: region['box']=[.8,.1,.95,.9]
    replay=replay_box_only(rows,originals,record,result,config,tmp_path/'trial.sqlite')
    assert discovery(replay)['reason']==reason
    assert discovery(replay)['subject_key'] is None
    assert discovery(replay)['association_status']=='unidentified'
    assert replay['before']==[i for i in replay['after'] if i['subject_key'] is not None]
    assert sum(i['subject_key'] is None for i in replay['after'])==1
    assert replay['journal_reopen_verified']


def test_scene_incident_is_not_scored_as_a_person_link():
    replay = dict(reply_usable=True, before=[], after=[dict(subject_key=None)], events=[dict(
        discovery=dict(incident_id='scene-case', subject_key=None, reason='no_matching_track'))])
    assert outcome(replay)['linked'] == 0
    assert outcome(replay)['after_incidents'] == 1
    assert score_linked_people(replay, []) == []


def test_location_only_observation_and_scene_normal_do_not_close_existing_case(tmp_path):
    rows,originals,record,result,config,_=fixture()
    for row in rows:
        row['candidate_payload']['robotMotion']='stationary'
        # Even a stale upstream clear flag cannot be reused as box-only clearance.
        row['candidate_payload']['tracks'][0]['subjectCheck']={'state':'clear','reason':'stable_upright'}
    result['normalized_response'].update(assessment='normal_activity',findings=[])
    replay=replay_box_only(rows,originals,record,result,config,tmp_path/'trial.sqlite')
    assert replay['before']==replay['after']
    assert all(i['state']!='resolved' for i in replay['after'])
    assert all(s['clearance']=='unknown' for f in replay['snapshot'] for s in f)


def test_invalid_json_is_not_repaired_for_the_experiment(tmp_path):
    rows,originals,record,result,config,_=fixture()
    result.update(outcome='invalid_response',normalized_response=None)
    replay=replay_box_only(rows,originals,record,result,config,tmp_path/'trial.sqlite')
    assert not replay['reply_usable'] and not replay['findings']
    assert replay['before']==replay['after'] and replay['persisted_discoveries']==0


@pytest.mark.parametrize('roles,expected',[
    (['target','target'],'verified_target_on_returned_regions'),
    (['target','other'],'wrong_person'),(['target',None],'unverified')])
def test_incomplete_or_wrong_person_gt_is_not_scored_as_target(roles,expected):
    replay=dict(events=[dict(discovery=dict(incident_id='i',subject_key='track',finding_index=0))],
                findings=[dict(regions=[dict(source_frame=0),dict(source_frame=2)])])
    scored=[dict(source_frame=f,people=[dict(subject={'subject_key':'track'},role=r,
                                            person_id='same-person' if r=='target' else 'other')]
                 if r else []) for f,r in zip([0,2],roles)]
    assert score_linked_people(replay,scored)[0]['status']==expected
