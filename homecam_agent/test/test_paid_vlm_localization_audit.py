"""No API/keys: exact-frame audit must never invent ground truth or salvage replies."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from audit_paid_vlm_localization import audit_case, canvas_box, exact_gt, overlap, summarize

CRITERIA = dict(visible_coverage=.5, prediction_coverage=.25, margin=.1)


def fixture():
    meta = dict(width=640,height=400,frames=25,fps=12,sha256='source')
    ann = dict(target_person_id='target', persons=[dict(person_id='target',role='target',
                boxes=[[0,64,40,320,320],[24,64,40,320,320]])])
    record = dict(common=dict(images=['unused']*3), evidence=dict(source_sha256='source',
        dimensions=[640,400],frame_indices=[0,12,24],source_times_s=[0,1,2]))
    row = dict(case_id='case',outcome='classified',reported_assessment='suspected_fall',
        response_issue_codes=[], normalized_response=dict(findings=[dict(regions=[
            dict(frame_index=i,box=[.1,.1,.5,.8]) for i in range(3)])]))
    return row,record,ann,meta


def test_no_interpolation_or_carry_forward():
    row,record,ann,meta = fixture()
    assert exact_gt(ann,12,meta)==[]
    case = audit_case(row,record,ann,meta,CRITERIA)
    assert [r['association'] for r in case['regions']]==['matched','no_exact_gt','matched']
    assert case['findings'][0]['status']=='same_person_in_at_least_two_exact_samples'
    summary = summarize([case])
    assert summary['returned_regions']==3 and summary['with_exact_gt']==2
    assert summary['without_exact_gt']==1 and summary['exact_target_regions']==2


def test_letterbox_matches_frozen_extractor():
    assert canvas_box([0,0,1280,720],dict(width=1280,height=720))==[0,20,640,380]
    assert canvas_box([128,72,640,576],dict(width=1280,height=720))==[64,56,320,308]


def test_iou_and_coverages_are_distinct():
    measurements = overlap([0,0,10,10],[0,0,5,10])
    assert measurements==dict(iou=.5,gt_box_coverage=.5,prediction_box_coverage=1)


def test_wrong_helper_is_not_target():
    row,record,ann,meta = fixture()
    ann['persons'].append(dict(person_id='helper',role='other',boxes=[[0,400,40,600,320]]))
    row['normalized_response']['findings'][0]['regions']=[dict(frame_index=0,box=[.625,.1,.9375,.8])]
    result = audit_case(row,record,ann,meta,CRITERIA)
    assert result['regions'][0]['matched_person']=='helper'
    assert result['regions'][0]['target']['iou']==0
    assert result['frames'][0]['target_status']=='unlinked'


def test_ambiguous_overlapping_people_never_assigned():
    row,record,ann,meta = fixture()
    ann['persons'].append(dict(person_id='helper',role='other',boxes=[[0,64,40,320,320]]))
    result = audit_case(row,record,ann,meta,CRITERIA)
    assert result['regions'][0]['association']=='unlinked_or_ambiguous'
    assert result['regions'][0]['matched_person'] is None


def test_invalid_json_is_not_normal_or_localized():
    row,record,ann,meta = fixture()
    row.update(outcome='invalid_response',reported_assessment=None,normalized_response=None,
               response_issue_codes=['invalid_json'])
    result = audit_case(row,record,ann,meta,CRITERIA)
    assert result['label'] is None and result['regions']==[]
    assert result['frames'][0]['target_status']=='unusable_response'


def test_normal_response_is_not_localization_success():
    row,record,ann,meta = fixture()
    row.update(reported_assessment='normal_activity',normalized_response=dict(findings=[]))
    result = audit_case(row,record,ann,meta,CRITERIA)
    assert summarize([result])['matched_any_person']==0
    assert result['frames'][0]['target_status']=='no_region_for_this_frame'


@pytest.mark.parametrize('change', ['source', 'duplicate_frame', 'time', 'index', 'salvage'])
def test_wrong_bindings_rejected(change):
    row,record,ann,meta = fixture()
    if change=='source': record['evidence']['source_sha256']='changed'
    elif change=='duplicate_frame': record['evidence']['frame_indices']=[0,0,24]
    elif change=='time': record['evidence']['source_times_s'][1]=1.1
    elif change=='index': row['normalized_response']['findings'][0]['regions'][0]['frame_index']=3
    else: row['outcome']='invalid_response'
    with pytest.raises(ValueError): audit_case(row,record,ann,meta,CRITERIA)


def test_records_not_mutated():
    args = fixture()
    before = copy.deepcopy(args)
    audit_case(*args,CRITERIA)
    assert args==before


def test_identity_switch_needs_two_exact_matches():
    row,record,ann,meta = fixture()
    ann['persons'].append(dict(person_id='helper',role='other',boxes=[[24,400,40,600,320]]))
    row['normalized_response']['findings'][0]['regions'][2]['box']=[.625,.1,.9375,.8]
    result = audit_case(row,record,ann,meta,CRITERIA)
    assert result['findings'][0]['status']=='different_persons_in_exact_samples'
    assert result['findings'][0]['person_ids']==['helper','target']


def test_missing_other_person_annotation_is_not_absence():
    row,record,ann,meta = fixture()
    ann['persons'].append(dict(person_id='helper',role='other',boxes=[[24,400,40,600,320]]))
    result = audit_case(row,record,ann,meta,CRITERIA)
    assert result['regions'][0]['all_persons_annotated'] is False
    assert result['frames'][0]['unannotated_person_ids']==['helper']
    assert result['frames'][2]['unannotated_person_ids']==[]


def test_whole_frame_box_does_not_force_identity():
    row,record,ann,meta = fixture()
    ann['persons'][0]['boxes']=[[0,64,40,128,80]]
    row['normalized_response']['findings'][0]['regions'][0]['box']=[0,0,1,1]
    result = audit_case(row,record,ann,meta,CRITERIA)
    assert result['regions'][0]['target']['gt_box_coverage']==1
    assert result['regions'][0]['matched_person'] is None
