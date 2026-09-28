import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from accept_cloud_box_review import augment, source_box
from test_cloud_box_review import fixture


def inputs():
    review,packet=fixture();review['frames'][0]['human_reviewed']=True
    meta=dict(width=640,height=400,frames=20,sha256='source')
    case=dict(case_id='case',source_sha256='source',target_person_id='P1',
        persons=[dict(person_id='P1',role='target',description='person',boxes=[[0,10,20,100,150]])],
        reviewed_frames=[0],spatial_review_status='user_approved',onset_frames=None)
    return dict(annotations=dict(cases=[case]),classifications=dict(cases=[dict(case_id='case',label='suspected_fall')])),{'case':meta},review,packet


def test_append_does_not_change_original_labels_or_annotations():
    args=inputs();before=copy.deepcopy(args)
    new,changes=augment(*args)
    assert args==before
    assert new['classifications']==before[0]['classifications']
    assert new['annotations']['cases'][0]['persons'][0]['boxes']==[[0,10,20,100,150],[2,10,20,100,150]]
    assert len(changes)==1


def test_inverse_letterbox_does_not_stretch_or_clamp():
    assert source_box([100,20,500,380],dict(width=1280,height=720))==[200,0,1000,720]
    with pytest.raises(ValueError,match='padding'):
        source_box([100,19,500,380],dict(width=1280,height=720))


def test_legacy_binding_and_all_review_scope_preserved():
    b,m,r,p=inputs();c=b['annotations']['cases'][0]
    del c['source_sha256'];c['reviewed_frames']='all'
    b['classifications']['cases'][0]['source_sha256']='source'
    result,_=augment(b,m,r,p)
    assert result['annotations']['cases'][0]['reviewed_frames']=='all'
    assert result['annotations']['cases'][0]['spatial_supplement_reviewed_frames']==[2]
    b['classifications']['cases'][0]['source_sha256']='wrong'
    with pytest.raises(ValueError):augment(b,m,r,p)


@pytest.mark.parametrize('reason',['unchecked','integer_flag','old_frame','uncertain','source','role','index'])
def test_incomplete_or_modified_review_is_not_promoted(reason):
    b,m,r,p=inputs()
    if reason=='unchecked':r['frames'][0]['human_reviewed']=False
    elif reason=='integer_flag':r['frames'][0]['human_reviewed']=1
    elif reason=='old_frame':b['annotations']['cases'][0]['persons'][0]['boxes'].append([2,10,20,100,150])
    elif reason=='uncertain':r['frames'][0]['persons'][0].update(visibility='uncertain',box=None)
    elif reason=='source':m['case']['sha256']='wrong'
    elif reason=='role':b['annotations']['cases'][0]['persons'][0]['role']='other'
    elif reason=='index':r['frames'][0]['source_frame']=2.0
    with pytest.raises(ValueError):augment(b,m,r,p)
