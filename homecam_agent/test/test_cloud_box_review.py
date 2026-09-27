"""Offline review must never silently promote drafts or rebind source data."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from review_cloud_box_frames import (SCHEMA, build_html, probe, seed_draft, validate, verify_packet)


def fixture():
    frame=dict(key='case-i00',case_id='case',image_index=0,source_frame=2,jpeg_sha256='hash',
        persons=[dict(person_id='P1',role='target',description='person')],image='frame.jpg')
    packet=dict(frames=[frame],case_ids=['case'],match_criteria=dict(visible_coverage=.5,prediction_coverage=.25,margin=.1))
    packet['packet_sha256']=hashlib.sha256(json.dumps(packet,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    f={k:frame[k] for k in ('key','case_id','image_index','source_frame','jpeg_sha256')}
    f['persons']=[dict(person_id='P1',role='target',visibility='visible',box=[10,20,100,150])]
    draft=dict(schema_version=SCHEMA,approval_status='pending_human_review',approved_gt_updated=False,
        packet_sha256=packet['packet_sha256'],coordinate_format='cloud_canvas_pixel_xyxy_640x400',
        interpolation='forbidden',frames=[f])
    return draft,packet


def test_valid_draft_does_not_mutate_data():
    draft,packet=fixture()
    before=copy.deepcopy((draft,packet))
    verify_packet(packet)
    assert validate(draft,packet)==dict(frames=1,boxes=1,approved_gt_updated=False)
    assert (draft,packet)==before


@pytest.mark.parametrize('change',['approval','gt','packet','coordinates','interpolation','missing_frame',
    'duplicate_frame','source','jpeg','person','role','absent_box','uncertain_box','visibility',
    'float','bool','range','flipped','no_box'])
def test_bad_draft_is_rejected(change):
    d,p=fixture(); f=d['frames'][0]; person=f['persons'][0]
    if change=='approval': d['approval_status']='approved'
    elif change=='gt': d['approved_gt_updated']=True
    elif change=='packet': d['packet_sha256']='wrong'
    elif change=='coordinates': d['coordinate_format']='normalized'
    elif change=='interpolation': d['interpolation']='allowed'
    elif change=='missing_frame': d['frames']=[]
    elif change=='duplicate_frame': d['frames'].append(copy.deepcopy(f))
    elif change=='source': f['source_frame']=3
    elif change=='jpeg': f['jpeg_sha256']='wrong'
    elif change=='person': person['person_id']='P2'
    elif change=='role': person['role']='other'
    elif change=='absent_box': person['visibility']='not_visible'
    elif change=='uncertain_box': person['visibility']='uncertain'
    elif change=='visibility': person['visibility']='unreviewed'
    elif change=='float': person['box'][0]=1.5
    elif change=='bool': person['box'][0]=True
    elif change=='range': person['box'][2]=641
    elif change=='flipped': person['box'][2]=5
    elif change=='no_box': person['box']=None
    with pytest.raises(ValueError): validate(d,p)


def test_unknown_person_is_not_guessed():
    d,p=fixture();d['frames'][0]['persons'][0].update(visibility='uncertain',box=None)
    assert validate(d,p)['boxes']==0


def test_modified_packet_rejected():
    _,p=fixture();p['frames'][0]['source_frame']=12
    with pytest.raises(ValueError,match='changed packet'):verify_packet(p)


def test_html_embeds_source_not_model_predictions_and_escapes_script(tmp_path):
    d,p=fixture();(tmp_path/'frame.jpg').write_bytes(b'jpeg')
    d['frames'][0]['persons'][0]['note']='</script><script>bad</script>'
    text=build_html(d,p,tmp_path)
    assert 'data:image/jpeg;base64,anBlZw==' in text
    assert '</script><script>bad' not in text
    assert "connect-src 'none'" in text and 'fetch(' not in text
    assert 'pending_human_review' in text and '__DRAFT_DATA__' not in text
    assert 'image_data' not in d['frames'][0]


def test_seed_preserves_explicit_manual_coordinates(tmp_path):
    _,p=fixture();(tmp_path/'packet.json').write_text(json.dumps(p))
    manual=dict(annotation_basis='independent draft',frames={'case-i00':{'P1':dict(visibility='visible',box=[11,22,101,151])}})
    path=tmp_path/'coords.json';path.write_text(json.dumps(manual))
    seed_draft(path,tmp_path)
    result=json.loads((tmp_path/'manual.json').read_text())
    assert result['frames'][0]['persons'][0]['box']==[11,22,101,151]
    assert result['approved_gt_updated'] is False
    with pytest.raises(FileExistsError): seed_draft(path,tmp_path)


def probe_fixture(tmp_path, switch=False, invalid=False):
    d,p=fixture();d['frames'][0]['persons'].append(dict(person_id='P2',role='other',visibility='visible',box=[200,20,300,150]))
    f=copy.deepcopy(d['frames'][0]);f.update(key='case-i01',image_index=1,source_frame=4);d['frames'].append(f)
    case=dict(case_id='case',outcome='invalid_response' if invalid else 'classified',regions=[] if invalid else [
        dict(image_index=0,source_frame=2,finding_index=0,box=[10,20,100,150]),
        dict(image_index=1,source_frame=4,finding_index=0,box=[200,20,300,150] if switch else [10,20,100,150])])
    path=tmp_path/'cases.json';path.write_text(json.dumps({'gemma':[case]}))
    p.update(audit=str(tmp_path),source_audit_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return d,p


def test_probe_is_not_official_identity_success(tmp_path):
    d,p=probe_fixture(tmp_path)
    result=probe(d,p)
    assert result['human_approved'] is result['official_scores_updated'] is result['runtime_tracking_tested'] is False
    assert result['results'][0]['findings'][0]['at_least_two_target_samples'] is True


def test_probe_detects_helper_switch(tmp_path):
    d,p=probe_fixture(tmp_path,switch=True)
    f=probe(d,p)['results'][0]['findings'][0]
    assert f['different_matched_people'] is True
    assert f['all_returned_draft_samples_target'] is False


def test_uncertain_other_person_blocks_identity_claim(tmp_path):
    d,p=probe_fixture(tmp_path)
    for frame in d['frames']:frame['persons'][1].update(visibility='uncertain',box=None)
    f=probe(d,p)['results'][0]['findings'][0]
    assert f['draft_matched_samples']==0


def test_invalid_response_is_not_salvaged(tmp_path):
    d,p=probe_fixture(tmp_path,invalid=True)
    row=probe(d,p)['results'][0]
    assert row['response_error_preserved'] is True
    assert row['findings']==row['samples']==[]


def test_changed_audit_rejected(tmp_path):
    d,p=probe_fixture(tmp_path);(tmp_path/'cases.json').write_text('{}')
    with pytest.raises(ValueError,match='changed audit'):probe(d,p)


def test_duplicate_predictions_do_not_get_two_person_assignments(tmp_path):
    d,p=probe_fixture(tmp_path)
    path=tmp_path/'cases.json';source=json.loads(path.read_text())
    duplicate=copy.deepcopy(source['gemma'][0]['regions'][0]);duplicate['finding_index']=1
    source['gemma'][0]['regions'].append(duplicate)
    path.write_text(json.dumps(source));p['source_audit_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    rows=probe(d,p)['results'][0]['samples']
    assert all(r['person_id'] is None for r in rows if r['image_index']==0)


def test_no_predictions_is_not_identity_success(tmp_path):
    d,p=probe_fixture(tmp_path)
    path=tmp_path/'cases.json';source=json.loads(path.read_text());source['gemma'][0]['regions']=[]
    path.write_text(json.dumps(source));p['source_audit_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    row=probe(d,p)['results'][0]
    assert row['findings']==[] and row['response_error_preserved'] is False
