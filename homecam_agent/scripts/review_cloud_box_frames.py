#!/usr/bin/env python3
"""Exact Cloud-input box drafts and offline review UI; no provider or GT promotion."""
import argparse
import base64
import copy
import hashlib
import io
import json
from pathlib import Path

from paid_vlm.inputs import private_dir, require, save, sha
from audit_paid_vlm_localization import overlap
from score_fall_baseline import match_boxes

SCHEMA = 'malbut.cloud-frame-box-draft.v1'


def read(path):
    return json.loads(path.read_bytes())


def verify_packet(packet):
    unsigned = {k:v for k,v in packet.items() if k!='packet_sha256'}
    require(hashlib.sha256(json.dumps(unsigned,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            ==packet['packet_sha256'], 'changed packet')


def seed_draft(coordinates, folder):
    """Bind independently drawn pixel boxes to immutable source metadata, never to predictions."""
    packet=read(folder/'packet.json')
    verify_packet(packet)
    manual=read(coordinates)
    require(set(manual['frames'])=={f['key'] for f in packet['frames']}, 'wrong manual frame set')
    frames=[]
    for ref in packet['frames']:
        boxes=manual['frames'][ref['key']]
        require(set(boxes)=={p['person_id'] for p in ref['persons']}, 'wrong manual people')
        frame={k:ref[k] for k in ('key','case_id','image_index','source_frame','jpeg_sha256')}
        frame['persons']=[dict(person_id=p['person_id'],role=p['role'],**boxes[p['person_id']])
                          for p in ref['persons']]
        frames.append(frame)
    draft=dict(schema_version=SCHEMA,approval_status='pending_human_review',approved_gt_updated=False,
        packet_sha256=packet['packet_sha256'],coordinate_format='cloud_canvas_pixel_xyxy_640x400',
        interpolation='forbidden',annotation_basis=manual['annotation_basis'],
        manual_coordinates_sha256=sha(coordinates),frames=frames)
    validate(draft,packet)
    save(folder/'manual.json',draft)


def validate(draft, packet):
    require(draft['schema_version']==SCHEMA and draft['approval_status']=='pending_human_review'
            and draft['approved_gt_updated'] is False, 'cannot self-approve GT')
    require(draft['packet_sha256']==packet['packet_sha256'], 'different packet')
    require(draft['coordinate_format']=='cloud_canvas_pixel_xyxy_640x400'
            and draft['interpolation']=='forbidden', 'coordinate contract changed')
    expected = {f['key']:f for f in packet['frames']}
    require(len(draft['frames'])==len(expected) and {f['key'] for f in draft['frames']}==set(expected),
            'missing/duplicate frames')
    count = 0
    for frame in draft['frames']:
        ref = expected[frame['key']]
        require(all(frame[k]==ref[k] for k in ('case_id','image_index','source_frame','jpeg_sha256')),
                'changed source binding')
        people = {p['person_id']:p for p in ref['persons']}
        require(len(frame['persons'])==len(people)
                and {p['person_id'] for p in frame['persons']}==set(people), 'changed people')
        for person in frame['persons']:
            require(person['role']==people[person['person_id']]['role'], 'changed role')
            state, box = person['visibility'], person['box']
            require(state in ('visible','not_visible','uncertain'), 'unreviewed person')
            if state=='visible':
                require(isinstance(box,list) and len(box)==4 and all(type(v) is int for v in box),
                        'integer xyxy required')
                x1,y1,x2,y2 = box
                require(0<=x1<x2<=640 and 0<=y1<y2<=400, 'invalid canvas bounds')
                count += 1
            else:
                require(box is None, 'unknown/absent person cannot have a guessed box')
    return dict(frames=len(draft['frames']),boxes=count,approved_gt_updated=False)


def prepare(audit, spatial, output, selected):
    require(len(selected)==len(set(selected)) and selected, 'invalid selection')
    for rel,digest in read(audit/'completed.json')['files'].items():
        require(sha(audit/rel)==digest, 'changed audit artifact')
    provenance = read(audit/'provenance.json')['source_files']
    require(all(sha(Path(p))==h for p,h in provenance.items()), 'changed audit source')
    require(str(spatial/'evaluation_labels.json') in provenance, 'wrong GT overlay')
    annotations = {r['case_id']:r for r in read(spatial/'evaluation_labels.json')['annotations']['cases']}
    audits = {m:{c['case_id']:c for c in rows} for m,rows in read(audit/'cases.json').items()}
    require(set(audits)=={'gemma','gemini'} and set(selected)<=set(annotations), 'unknown scope')
    private_dir(output)
    (output/'frames').mkdir(mode=0o700)
    (output/'source_sheets').mkdir(mode=0o700)
    frames, anchors = [], []
    from PIL import Image, ImageDraw
    for cid in selected:
        inputs = [read(Path(audits[m][cid]['result_path']).parent.parent/'inputs'/(cid+'.input.json'))
                  for m in ('gemma','gemini')]
        require(inputs[0]['evidence']==inputs[1]['evidence'] and
                inputs[0]['common']['images']==inputs[1]['common']['images'], 'model input differs')
        annotation = annotations[cid]
        people = [{k:p[k] for k in ('person_id','role','description')} for p in annotation['persons']]
        selected_indices = sorted({r['image_index'] for m in audits for r in audits[m][cid]['regions']})
        missing = [i for i in selected_indices if not audits['gemma'][cid]['frames'][i]['gt']]
        for i in missing:
            blob = base64.b64decode(inputs[0]['common']['images'][i],validate=True)
            digest = hashlib.sha256(blob).hexdigest()
            require(digest==inputs[0]['evidence']['jpeg_sha256'][i], 'changed JPEG')
            key = f'{cid}-i{i:02d}'
            with (output/'frames'/(key+'.jpg')).open('xb') as stream:
                stream.write(blob)
            frames.append(dict(key=key,case_id=cid,image_index=i,
                source_frame=inputs[0]['evidence']['frame_indices'][i],jpeg_sha256=digest,
                image='frames/'+key+'.jpg',persons=people))
        # Frozen reference only; never use an anchor as the box for another frame.
        anchors.extend(dict(case_id=cid,**f) for f in audits['gemma'][cid]['frames'] if f['gt'])
        sheet = Image.new('RGB',(1280,440*((len(missing)+1)//2)),'#151a22')
        draw = ImageDraw.Draw(sheet)
        for pos,i in enumerate(missing):
            img = Image.open(io.BytesIO(base64.b64decode(inputs[0]['common']['images'][i]))).convert('RGB')
            require(img.size==(640,400), 'unexpected canvas')
            grid = ImageDraw.Draw(img)
            for x in range(50,640,50):
                grid.line((x,0,x,400),fill='#5e6770',width=1)
                grid.text((x+2,2),str(x),fill='white')
            for y in range(50,400,50):
                grid.line((0,y,640,y),fill='#5e6770',width=1)
                grid.text((2,y+2),str(y),fill='white')
            x,y = pos%2*640,pos//2*440
            draw.text((x+8,y+8),f'{cid} image {i} / source f{inputs[0]["evidence"]["frame_indices"][i]} / NO MODEL BOXES',fill='white')
            sheet.paste(img,(x,y+32))
        sheet.save(output/'source_sheets'/(cid+'.jpg'),quality=94)
    packet = dict(schema_version='malbut.cloud-frame-review-packet.v1',case_ids=selected,frames=frames,
        anchors=anchors,match_criteria=read(spatial/'freeze.json')['match'],
        audit=str(audit),source_audit_sha256=sha(audit/'cases.json'),
        spatial_freeze_sha256=sha(spatial/'freeze.json'),gt_updated=False,api_calls=0,
        annotation_bias_note='Assistant has already seen model predictions; review is not blinded. Draft boxes are drawn from source JPEGs, not copied from model outputs.')
    packet['packet_sha256']=hashlib.sha256(json.dumps(packet,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    save(output/'packet.json',packet)
    save(output/'source_hashes.json',dict(audit_provenance=provenance,
        preparation_script_sha256=sha(Path(__file__))))
    print(json.dumps(dict(cases=len(selected),new_frames=len(frames),people_boxes=sum(len(f['persons']) for f in frames))))


def render_draft(draft, packet, folder):
    from PIL import Image,ImageDraw
    (folder/'draft_sheets').mkdir(mode=0o700)
    refs = {f['key']:f for f in packet['frames']}
    for cid in packet['case_ids']:
        frames = [f for f in draft['frames'] if f['case_id']==cid]
        sheet=Image.new('RGB',(1280,440*((len(frames)+1)//2)),'#151a22')
        draw=ImageDraw.Draw(sheet)
        for pos,f in enumerate(frames):
            img=Image.open(folder/refs[f['key']]['image']).convert('RGB')
            overlay=ImageDraw.Draw(img)
            for n,p in enumerate(f['persons']):
                if p['box'] is None: continue
                color=('#72ff67','#ffe060','#f795ed')[n%3]
                overlay.rectangle(p['box'],outline=color,width=2)
                overlay.text((p['box'][0]+3,p['box'][1]+3),p['person_id']+' DRAFT',fill=color)
            x,y=pos%2*640,pos//2*440
            draw.text((x+8,y+8),f'{cid} image {f["image_index"]} / f{f["source_frame"]} / NOT APPROVED GT',fill='white')
            sheet.paste(img,(x,y+32))
        sheet.save(folder/'draft_sheets'/(cid+'.jpg'),quality=94)


def probe(draft, packet):
    """Exploratory identity check against an explicitly unapproved draft; not a score update."""
    require(draft['approval_status']=='pending_human_review', 'not a draft')
    audit_file=Path(packet['audit'])/'cases.json'
    require(sha(audit_file)==packet['source_audit_sha256'], 'changed audit')
    source = read(audit_file)
    lookup = {(f['case_id'],f['image_index']):f for f in draft['frames']}
    results=[]
    for model,cases in source.items():
        for case in cases:
            if case['case_id'] not in packet['case_ids']: continue
            rows=[]
            for index in sorted({r['image_index'] for r in case['regions']}):
                frame=lookup.get((case['case_id'],index))
                if frame is None: continue
                gt=[p for p in frame['persons'] if p['visibility']=='visible']
                regions=[r for r in case['regions'] if r['image_index']==index]
                matches=match_boxes([p['box'] for p in gt],[r['box'] for r in regions],packet['match_criteria'])
                assigned={pi:p for p,(status,pi) in zip(gt,matches) if status=='matched'}
                target=next((p for p in gt if p['role']=='target'),None)
                complete=all(p['visibility']!='uncertain' for p in frame['persons'])
                for pi,r in enumerate(regions):
                    person=assigned.get(pi) if complete else None
                    rows.append(dict(image_index=r['image_index'],source_frame=r['source_frame'],
                        finding_index=r['finding_index'],person_id=person['person_id'] if person else None,
                        role=person['role'] if person else None,all_people_reviewed=complete,
                        target_overlap=overlap(target['box'],r['box']) if target else None))
            findings=[]
            for fi in sorted({r['finding_index'] for r in rows}):
                samples=[r for r in rows if r['finding_index']==fi]
                pids=sorted({r['person_id'] for r in samples if r['person_id'] is not None})
                findings.append(dict(finding_index=fi,person_ids=pids,
                    draft_matched_samples=sum(r['person_id'] is not None for r in samples),
                    all_returned_draft_samples_target=bool(samples) and all(r['role']=='target' for r in samples),
                    at_least_two_target_samples=sum(r['role']=='target' for r in samples)>=2,
                    different_matched_people=len(pids)>1))
            results.append(dict(model=model,case_id=case['case_id'],outcome=case['outcome'],
                                response_error_preserved=case['outcome']!='classified',samples=rows,findings=findings))
    return dict(status='exploratory_against_unapproved_assistant_draft',human_approved=False,
        official_scores_updated=False,runtime_tracking_tested=False,results=results)


def build_html(draft, packet, folder):
    data=copy.deepcopy(draft)
    refs={f['key']:f for f in packet['frames']}
    for frame in data['frames']:
        frame['image_data']='data:image/jpeg;base64,'+base64.b64encode((folder/refs[frame['key']]['image']).read_bytes()).decode()
        descriptions={p['person_id']:p['description'] for p in refs[frame['key']]['persons']}
        for p in frame['persons']: p['description']=descriptions[p['person_id']]
    payload=json.dumps(data,ensure_ascii=False).replace('<','\\u003c')
    template=(Path(__file__).parent/'cloud_box_review.html').read_text(encoding='utf-8')
    return template.replace('__DRAFT_DATA__',payload)


def compile_review(manual, folder):
    packet=read(folder/'packet.json')
    verify_packet(packet)
    sources=read(folder/'source_hashes.json')['audit_provenance']
    require(all(sha(Path(p))==h for p,h in sources.items()), 'changed source or approved GT')
    require(all(not (folder/n).exists() for n in
                ('draft.json','draft_sheets','draft-probe.json','review.html','review-completed.json')),
            'review already compiled or incomplete; preserve it and use a fresh folder')
    for frame in packet['frames']:
        require(sha(folder/frame['image'])==frame['jpeg_sha256'], 'changed review frame')
    draft=read(manual)
    summary=validate(draft,packet)
    html_page=build_html(draft,packet,folder)
    tentative=probe(draft,packet)
    save(folder/'draft.json',draft)
    render_draft(draft,packet,folder)
    save(folder/'draft-probe.json',tentative)
    with (folder/'review.html').open('x',encoding='utf-8') as stream:
        stream.write(html_page)
    save(folder/'review-completed.json',dict(summary=summary,files={str(p.relative_to(folder)):sha(p)
        for p in folder.rglob('*') if p.is_file()},approval_status='pending_human_review',api_calls=0,
        tool_sha256=sha(Path(__file__)),template_sha256=sha(Path(__file__).with_name('cloud_box_review.html'))))
    print(json.dumps(summary))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare')
    prep.add_argument('--audit',type=Path,required=True)
    prep.add_argument('--spatial',type=Path,required=True)
    prep.add_argument('--output',type=Path,required=True)
    prep.add_argument('--cases',nargs='+',required=True)
    seed=sub.add_parser('seed')
    seed.add_argument('--coordinates',type=Path,required=True)
    seed.add_argument('--folder',type=Path,required=True)
    comp=sub.add_parser('compile')
    comp.add_argument('--manual',type=Path,required=True)
    comp.add_argument('--folder',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='prepare': prepare(args.audit,args.spatial,args.output,args.cases)
    elif args.command=='seed': seed_draft(args.coordinates,args.folder)
    else: compile_review(args.manual,args.folder)


if __name__=='__main__':
    main()
