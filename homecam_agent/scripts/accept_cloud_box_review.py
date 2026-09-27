#!/usr/bin/env python3
"""Import an explicitly user-confirmed review as a NEW spatial overlay, never overwrite GT."""
import argparse
import copy
from datetime import datetime, timezone
from pathlib import Path

from paid_vlm.inputs import private_dir, require, save, sha
from review_cloud_box_frames import read, validate, verify_packet
from audit_paid_vlm_localization import canvas_box


def source_box(box, meta):
    """Invert the frozen resize/padding transform; never clamp annotations in padding."""
    width,height=meta['width'],meta['height']
    scale=min(640/width,400/height)
    rw,rh=round(width*scale),round(height*scale)
    dx,dy=(640-rw)//2,(400-rh)//2
    x1,y1,x2,y2=box
    require(dx<=x1<x2<=dx+rw and dy<=y1<y2<=dy+rh, 'box includes non-image padding')
    result=[(x1-dx)*width/rw,(y1-dy)*height/rh,(x2-dx)*width/rw,(y2-dy)*height/rh]
    result=[int(v) if v.is_integer() else v for v in result]
    require(all(abs(a-b)<1e-9 for a,b in zip(canvas_box(result,meta),box)), 'coordinate roundtrip failed')
    return result


def augment(bundle, metas, review, packet):
    validate(review,packet)
    require(all(f.get('human_reviewed') is True for f in review['frames']), 'human review incomplete')
    require(all(p['visibility']=='visible' for f in review['frames'] for p in f['persons']),
            'absent or uncertain people require a visibility-aware overlay; do not invent boxes')
    result=copy.deepcopy(bundle)
    cases={c['case_id']:c for c in result['annotations']['cases']}
    labels={c['case_id']:c for c in result['classifications']['cases']}
    changes=[]
    for frame in review['frames']:
        require(type(frame['source_frame']) is int and type(frame['image_index']) is int,
                'integer source indices required')
        cid=frame['case_id'];case=cases[cid];meta=metas[cid]
        # Legacy41 puts the binding in the classification record, not annotations.
        binding=case.get('source_sha256',labels[cid].get('source_sha256'))
        require(binding==meta['sha256'], 'different annotation source')
        require(0<=frame['source_frame']<meta['frames'], 'source frame outside video')
        people={p['person_id']:p for p in case['persons']}
        require(set(people)=={p['person_id'] for p in frame['persons']}, 'person set changed')
        for person in frame['persons']:
            old=people[person['person_id']]
            require(old['role']==person['role'], 'person role changed')
            require(not any(b[0]==frame['source_frame'] for b in old['boxes']),
                    'refusing to overwrite an existing exact-frame annotation')
            box=source_box(person['box'],meta)
            old['boxes'].append([frame['source_frame'],*box]);old['boxes'].sort(key=lambda b:b[0])
            changes.append(dict(case_id=cid,person_id=person['person_id'],source_frame=frame['source_frame'],
                cloud_canvas_box=person['box'],source_box=box,jpeg_sha256=frame['jpeg_sha256']))
        if isinstance(case['reviewed_frames'],list):
            case['reviewed_frames']=sorted(set(case['reviewed_frames'])|{frame['source_frame']})
        else:
            require(case['reviewed_frames']=='all','unknown legacy review scope')
        case['spatial_supplement_reviewed_frames']=sorted(
            set(case.get('spatial_supplement_reviewed_frames',[]))|{frame['source_frame']})
        case['spatial_review_status']='user_approved'
        case['spatial_supplement_approval_record']='approval.json'
    require(result['classifications']==bundle['classifications'], 'classification labels changed')
    # Preserve all original boxes, IDs, descriptions and temporal annotations.
    for old in bundle['annotations']['cases']:
        new=cases[old['case_id']]
        for p in old['persons']:
            replacement=next(v for v in new['persons'] if v['person_id']==p['person_id'])
            require(all(b in replacement['boxes'] for b in p['boxes']), 'existing GT lost')
            require(all(replacement[k]==v for k,v in p.items() if k!='boxes'), 'person metadata changed')
        require(all(new[k]==v for k,v in old.items() if k not in
                    ('persons','reviewed_frames','spatial_review_status','spatial_supplement_approval_record')),
                'unrelated annotation changed')
    return result,changes


def accept(folder, review_path, spatial, output, confirmation):
    require(confirmation.strip(), 'explicit user confirmation required')
    packet=read(folder/'packet.json');verify_packet(packet)
    require(sha(spatial/'freeze.json')==packet['spatial_freeze_sha256'], 'different parent GT')
    sources=read(folder/'source_hashes.json')['audit_provenance']
    sources.update({str(folder/'packet.json'):sha(folder/'packet.json'),
        str(folder/'draft.json'):sha(folder/'draft.json'),str(review_path):sha(review_path)})
    require(all(sha(Path(p))==h for p,h in sources.items()), 'original sources changed')
    require(sha(Path(packet['audit'])/'cases.json')==packet['source_audit_sha256'], 'audit changed')
    for f in packet['frames']:
        image=folder/f['image'];require(sha(image)==f['jpeg_sha256'], 'different reviewed JPEG')
        sources[str(image)]=sha(image)
    parent=read(spatial/'freeze.json')
    require(all(sha(spatial/n)==h for n,h in parent['files'].items()), 'parent freeze changed')
    media=read(spatial/'media.json');metas={c['case_id']:c for c in media['cases']}
    review=read(review_path);bundle=read(spatial/'evaluation_labels.json')
    updated,changes=augment(bundle,metas,review,packet)
    initial={f['key']:{p['person_id']:p for p in f['persons']} for f in read(folder/'draft.json')['frames']}
    edits=[dict(frame=f['key'],person_id=p['person_id'],before=initial[f['key']][p['person_id']]['box'],
                after=p['box']) for f in review['frames'] for p in f['persons']
           if p['box']!=initial[f['key']][p['person_id']]['box']]
    private_dir(output)
    save(output/'evaluation_labels.json',updated);save(output/'media.json',media)
    # Retain the user's file byte-for-byte. The UI deliberately cannot self-approve.
    with (output/'user-review.json').open('xb') as stream:stream.write(review_path.read_bytes())
    save(output/'changes-from-user-review.json',dict(added=changes,edits_from_draft=edits,
        classification_labels_changed=False,temporal_annotations_changed=False,old_boxes_overwritten=0))
    save(output/'approval.json',dict(user_confirmed_complete=True,scope='15 exact Cloud frames in four selected multi-person scenes',
        confirmation=confirmation,reviewed_frames=len(review['frames']),added_boxes=len(changes),
        corrected_draft_boxes=len(edits),source_review_sha256=sha(review_path),
        review_exported_at=review.get('review_exported_at'),imported_at=datetime.now(timezone.utc).isoformat(),
        source_ui_status=review['approval_status'],
        approval_basis='All frame checkboxes plus explicit user completion and delivery in conversation; not automatic UI approval.',
        review_method='User reviewed and edited assistant-initialized boxes. Not independent/blinded annotations.',
        original_gt_unchanged=True,original_overlay=str(spatial),
        original_freeze_sha256=sha(spatial/'freeze.json'),packet_sha256=packet['packet_sha256']))
    save(output/'freeze.json',dict(schema_version=parent['schema_version'],match=parent['match'],
        parent_freeze_sha256=parent['parent_freeze_sha256'],parent_labels_sha256=parent['parent_labels_sha256'],
        previous_spatial_overlay=str(spatial),previous_spatial_freeze_sha256=sha(spatial/'freeze.json'),
        files={p.name:sha(p) for p in output.iterdir() if p.is_file()}))
    require(all(sha(Path(p))==h for p,h in sources.items()), 'source changed during import')
    save(output/'import-provenance.json',dict(source_files=sources,script_sha256=sha(Path(__file__)),
        original_gt_unchanged=True,api_calls=0))
    return dict(frames=len(review['frames']),added_boxes=len(changes),corrected_boxes=len(edits),
        total_boxes=sum(len(p['boxes']) for c in updated['annotations']['cases'] for p in c['persons']))


if __name__=='__main__':
    import json
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('folder','review','spatial','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--user-confirmation',required=True)
    a=parser.parse_args()
    print(json.dumps(accept(a.folder,a.review,a.spatial,a.output,a.user_confirmation)))
