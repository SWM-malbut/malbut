"""Replay plumbing tests: synthetic contracts, no inference/API/real-person GT."""
import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from replay_reviewed_pose_cloud import (
    NoNetworkProvider, aligned_schedule, cached_reply, frozen_jpegs,
    replay_scene, score_tracks, stamp,
)


@pytest.mark.parametrize('mandatory,fps', [
    ([3,8,14,19,24,30,35,41,46,51,57,62],12),
    ([2,13,24,35,46,57,67,78,89,100,111,122],24),
    ([0,30,60],30), ([0],30),
])
def test_sample_plan_keeps_every_input_without_exceeding_five_hz(mandatory,fps):
    result=aligned_schedule(mandatory,fps)
    assert set(mandatory)<=set(result)
    assert all((b-a)/fps>=.2-1e-10 for a,b in zip(result,result[1:]))
    assert result==sorted(set(result))


def test_no_too_close_warmup_or_cloud_frame_replacement():
    assert aligned_schedule([2,13],24)==[2,8,13]
    with pytest.raises(ValueError,match='rate limit'):
        aligned_schedule([2,3],24)


@pytest.mark.parametrize('values', [[],[1,1],[3,2],[-1,4],[True,5]])
def test_bad_input_schedule_rejected(values):
    with pytest.raises(ValueError):
        aligned_schedule(values,12)


def fixture():
    box=[.1,.3,.7,.8]
    originals={i:b'\xff\xd8'+f'jpeg-{i}'.encode()+b'\xff\xd9' for i in range(3)}
    record=dict(common=dict(images=[base64.b64encode(v).decode() for v in originals.values()]),
        evidence=dict(source_sha256='video',dimensions=[640,400],frame_indices=[0,1,2],
            source_times_s=[0,.5,1],jpeg_sha256=[hashlib.sha256(v).hexdigest() for v in originals.values()]))
    meta=dict(sha256='video',fps=2,frames=3)
    rows=[]
    for index in originals:
        now=stamp(index,2)
        candidate=dict(source='yolo_pose',requiresVerification=True,revision=1,
            targetTrackId='raw-id',candidateId='candidate',candidateKind='found_down',
            evidenceStartSec=100,evidenceEndSec=now,evidence=dict(pose={}))
        rows.append(dict(source_frame=index,captured_at=now,candidate_payload=dict(
            schemaVersion=1,timeBase='ros_image_stamp',status='ok',frameId='offline_rgb',
            captureTimeSec=now,subjectCheckVersion=1,subjectCheckMaxGapSec=.5,
            unassignedCount=0,robotMotion='unknown',candidates=[candidate],tracks=[dict(
                targetTrackId='raw-id',associationUsable=True,trackingState='tracked',
                features=dict(usable=True),box=box,subjectCheck=dict(state='suspected'))])))
    result=dict(case_id='synthetic',outcome='classified',response_issue_codes=[],normalized_response=dict(
        assessment='suspected_fall',explanation='synthetic test',findings=[dict(
            assessment='suspected_fall',kind='already_down',regions=[dict(frame_index=0,box=box),
                                                                   dict(frame_index=2,box=box)])]))
    config=dict(retention_s=10,buffer_bytes=10000,buffer_frames=64,max_source_age_s=1,
        policy=dict(retry_interval_s=3,max_person_observation_age_s=2,clip_window_s=5,
                    max_frame_age_s=2,max_calls_per_minute=5,max_incidents=10,max_images=12))
    return rows,originals,record,result,config,meta


def test_frozen_jpeg_identity_is_verified():
    _,originals,record,_,_,meta=fixture()
    assert frozen_jpegs(record,meta)==originals
    record['common']['images'][0]=base64.b64encode(b'changed').decode()
    with pytest.raises(ValueError,match='JPEG hash'):
        frozen_jpegs(record,meta)


@pytest.mark.parametrize('change', ['time','source','duplicate','count','bounds'])
def test_incorrect_input_binding_rejected(change):
    _,_,record,_,_,meta=fixture()
    if change=='time': record['evidence']['source_times_s'][1]=.6
    elif change=='source': record['evidence']['source_sha256']='other'
    elif change=='duplicate': record['evidence']['frame_indices']=[0,0,2]
    elif change=='count': record['evidence']['jpeg_sha256'].pop()
    else: record['evidence']['frame_indices']=[0,1,3]
    with pytest.raises(ValueError): frozen_jpegs(record,meta)


def test_invalid_reply_is_never_rescued():
    _,_,_,result,_,_=fixture()
    result['outcome']='invalid_response'
    with pytest.raises(ValueError,match='salvaged'): cached_reply(result)
    result['normalized_response']=None
    assert cached_reply(result) is None


def test_no_provider_can_be_called():
    with pytest.raises(AssertionError,match='must not call'):
        asyncio.run(NoNetworkProvider().analyze(None))


def test_real_merge_keeps_original_incident_and_repeat_budget(tmp_path):
    rows,originals,record,result,config,_=fixture()
    before=copy.deepcopy((rows,originals,record,result,config))
    replay=replay_scene(rows,originals,record,result,config,tmp_path/'journal.sqlite')
    assert replay['findings'][0]['association']['reason']=='matched'
    assert len(replay['before'])==len(replay['after'])==1
    assert replay['before'][0]['incident_id']==replay['after'][0]['incident_id']
    assert replay['after'][0]['sources']==('yolo_pose','cloud_crosscheck')
    assert replay['repeat_incidents_unchanged']
    assert replay['after'][0]['rechecks']==replay['before'][0]['rechecks']==0
    assert replay['journal_reopen_verified'] and replay['persisted_discoveries']==2
    assert not any(e['kind']=='notification_requested' for e in replay['events'])
    assert (rows,originals,record,result,config)==before


@pytest.mark.parametrize('condition,reason', [('weak','track_unusable'),('lost','track_changed'),
                                           ('wrong_box','no_matching_track'),('ambiguous','ambiguous_tracks')])
def test_unsafe_links_remain_unidentified(tmp_path,condition,reason):
    rows,originals,record,result,config,_=fixture()
    if condition=='weak': rows[0]['candidate_payload']['tracks'][0]['associationUsable']=False
    elif condition=='lost': rows[1]['candidate_payload']['tracks']=[]
    elif condition=='wrong_box':
        for r in result['normalized_response']['findings'][0]['regions']: r['box']=[.8,.1,.9,.2]
    else:
        for row in rows:
            other=copy.deepcopy(row['candidate_payload']['tracks'][0])
            other['targetTrackId']='competitor'
            other['associationUsable']=False
            row['candidate_payload']['tracks'].append(other)
    replay=replay_scene(rows,originals,record,result,config,tmp_path/'journal.sqlite')
    discovery=next(e['discovery'] for e in replay['events'] if e['discovery'])
    assert discovery['reason']==reason and discovery['subject_key'] is None
    assert replay['before']==[i for i in replay['after'] if i['subject_key'] is not None]
    assert sum(i['subject_key'] is None for i in replay['after'])==1
    assert sum(e['kind']=='question_requested' and e['confirmation_scope']=='scene'
               for e in replay['events'])==1
    assert replay['journal_reopen_verified']


def test_invalid_json_is_no_association_not_normal(tmp_path):
    rows,originals,record,result,config,_=fixture()
    result.update(outcome='invalid_response',normalized_response=None,response_issue_codes=['invalid_json'])
    replay=replay_scene(rows,originals,record,result,config,tmp_path/'journal.sqlite')
    assert not replay['reply_usable'] and replay['findings']==[]
    assert replay['before']==replay['after'] and replay['events']==[]
    assert replay['persisted_discoveries']==0


def test_gt_scoring_is_posthoc_and_requires_exact_frames():
    subject=dict(subject_key='raw-id',box=[.1,.1,.5,.8],usable=True,token='actual-token')
    timeline={'c':[dict(source_frame=0,subjects=[subject]),dict(source_frame=1,subjects=[subject])]}
    ann={'c':dict(persons=[dict(person_id='reviewed-person',role='target',boxes=[[0,64,40,320,320]])])}
    before=copy.deepcopy(timeline)
    scored=score_tracks(timeline,ann,{'c':dict(width=640,height=400)},
                        dict(visible_coverage=.5,prediction_coverage=.25,margin=.1))
    assert timeline==before
    assert len(scored['c'])==1
    assert scored['c'][0]['people'][0]['subject']['subject_key']=='raw-id'
    assert scored['c'][0]['people'][0]['person_id']=='reviewed-person'
