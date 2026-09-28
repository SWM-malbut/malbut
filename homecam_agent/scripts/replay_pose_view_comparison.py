#!/usr/bin/env python3
"""Fixed rotation/tiles versus full-frame Pose, then frozen Cloud-merge replay.

Nine known development scenes, not independent evaluation. No network, weights
download, threshold tuning, runtime edits, GT crops or cached-response rewriting.
"""
import argparse
import base64
from collections import Counter
from dataclasses import asdict
import hashlib
import html
import json
from pathlib import Path
import statistics
import sys
import time

from replay_reviewed_pose_cloud import (
    REPO, aligned_schedule, cached_reply, digest, frozen_jpegs, intermediate_jpeg,
    read, replay_scene, require, save, score_tracks, stamp,
)
from replay_box_association_ablation import outcome, replay_box_only, score_linked_people, verify_frozen
from experimental_pose_views import fixed_tiles, merge_views, project_tile, restore_rotation
from homecam_detector.fall_candidate import FallCandidateConfig, FallCandidateDetector
from homecam_detector.pose import PersonPoseEstimator
from homecam_detector.pose_tracker import PersonPoseTracker

CASES=['SYN012','SYN045','SYN063','SYN067','SYN011','SYN023','SYN028','SYN033','SYN036']
ARMS=('full','rotations','tiles')


class Pipeline:
    def __init__(self):
        self.tracker=PersonPoseTracker(); self.detector=FallCandidateDetector()

    def step(self,poses,index,now,jpeg,cloud_input,elapsed,raw):
        tracked=self.tracker.update(poses,now=now)
        payload=self.detector.update(tracked,capture_time=now,image_size=(640,400),robot_motion='unknown')
        payload.update(frameId='offline_rgb',expiredTrackIds=list(tracked.expired_track_ids))
        return dict(source_frame=index,captured_at=now,cloud_input=cloud_input,
            jpeg_sha256=hashlib.sha256(jpeg).hexdigest(),inference_s=elapsed,
            poses=[p.as_dict() for p in poses],tracks=[dict(track_id=t.track_id,state=t.state,
                confidence=t.confidence_level,observations=t.observation_count,
                consecutive=t.consecutive_observations,pose=t.pose.as_dict() if t.pose else None)
                for t in tracked.tracks],
            unassigned=[dict(reason=u.reason,pose=u.pose.as_dict()) for u in tracked.unassigned],
            candidate_payload=payload,view_predictions=raw)


def infer_scene(estimator,source,meta,record,prior,destination):
    import cv2
    import numpy as np
    require(digest(source)==meta['sha256'],'source video changed')
    originals=frozen_jpegs(record,meta)
    schedule=aligned_schedule(list(originals),meta['fps'])
    prior_by_frame={r['source_frame']:r for r in prior} if prior else {}
    pipelines={arm:Pipeline() for arm in ARMS}; rows={arm:[] for arm in ARMS}
    cap=cv2.VideoCapture(str(source)); require(cap.isOpened(),'cannot open source video')
    image_dir=destination/'images'/meta['case_id']; image_dir.mkdir(parents=True,mode=0o700)
    calls=0
    try:
        for index in range(schedule[-1]+1):
            ok,source_frame=cap.read();require(ok,'source decode failed')
            if index not in schedule: continue
            jpeg=originals[index] if index in originals else intermediate_jpeg(source_frame)
            pixels=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
            require(pixels is not None and pixels.shape==(400,640,3),'unexpected image dimensions')
            image_path=image_dir/f'{index:04d}.jpg'
            with image_path.open('xb') as stream: stream.write(jpeg)
            image_path.chmod(0o600)
            started=time.perf_counter();full=estimator.estimate_all(pixels,confidence_threshold=.10)
            full_time=time.perf_counter()-started;calls+=1
            if prior_by_frame:
                require(index in prior_by_frame and prior_by_frame[index]['jpeg_sha256']==digest(image_path),
                        'different baseline image')
                require([p.as_dict() for p in full]==prior_by_frame[index]['poses'],
                        f'baseline inference changed: {meta["case_id"]}:{index}')
            obs=[dict(source='full',pose=p) for p in full]
            now=stamp(index,meta['fps'])
            rows['full'].append(pipelines['full'].step(full,index,now,jpeg,index in originals,full_time,
                [dict(source='full',pose=p.as_dict()) for p in full]))
            for arm in ('rotations','tiles'):
                added=[]; discarded=[]; extra_time=0
                views= [('cw',cv2.rotate(pixels,cv2.ROTATE_90_CLOCKWISE)),
                        ('ccw',cv2.rotate(pixels,cv2.ROTATE_90_COUNTERCLOCKWISE))] if arm=='rotations' else [
                            (rect,pixels[rect[1]:rect[3],rect[0]:rect[2]]) for rect in fixed_tiles((640,400))]
                for spec,image in views:
                    started=time.perf_counter()
                    predictions=estimator.estimate_all(image,confidence_threshold=.10)
                    extra_time+=time.perf_counter()-started;calls+=1
                    for p in predictions:
                        if arm=='rotations':
                            restored=restore_rotation(p,spec);reason=None
                        else: restored,reason=project_tile(p,spec,(640,400))
                        if restored is not None: added.append(dict(source=str(spec),pose=restored))
                        else: discarded.append(dict(source=str(spec),reason=reason,pose=p.as_dict()))
                fused,provenance=merge_views(obs+added)
                row=pipelines[arm].step(fused,index,now,jpeg,index in originals,full_time+extra_time,provenance)
                row.update(extra_inference_s=extra_time,discarded_predictions=discarded)
                rows[arm].append(row)
    finally: cap.release()
    for arm,data in rows.items(): save(destination/f'{arm}-{meta["case_id"]}.pose.json',data)
    return rows,originals,calls


def detection_score(rows,annotation,meta,criteria):
    from audit_paid_vlm_localization import exact_gt
    from score_fall_baseline import match_boxes
    output=[]
    for row in rows:
        gt=exact_gt(annotation,row['source_frame'],meta)
        if not gt: continue
        scores={}
        for name,floor in [('all',.1),('strong',.45)]:
            predictions=[p for p in row['poses'] if p['boxConfidence']>=floor]
            boxes=[[p['box'][k]*(640 if i%2==0 else 400)
                    for i,k in enumerate(('left','top','right','bottom'))] for p in predictions]
            scores[name]=match_boxes([p['box'] for p in gt],boxes,criteria)
        output.extend(dict(source_frame=row['source_frame'],person_id=g['person_id'],role=g['role'],
                           all_status=scores['all'][i][0],strong_status=scores['strong'][i][0])
                      for i,g in enumerate(gt))
    return output


def render_pages(rows,annotations,metas,output):
    """Native HTML/SVG diagnostic, no changes to images or approved boxes."""
    from audit_paid_vlm_localization import exact_gt
    index=[]
    for cid in CASES:
        panels=[]
        for i,base in enumerate(rows['full',cid]):
            frame=base['source_frame']; gt=exact_gt(annotations[cid],frame,metas[cid])
            for arm in ARMS:
                row=rows[arm,cid][i]; marks=[]
                for t in row['candidate_payload']['tracks']:
                    if t['box'] is None:continue
                    l,top,r,b=t['box'];color='#00ddff' if t['associationUsable'] else '#ffb000'
                    key=t['targetTrackId'].split('-')[-1]
                    features=t['features'] or {}; score=features.get('box_confidence',0)
                    marks.append(f'<rect x="{l*640}" y="{top*400}" width="{(r-l)*640}" height="{(b-top)*400}" fill="none" stroke="{color}" stroke-width="2"/>'
                                 f'<text x="{l*640}" y="{max(12,top*400-3)}" fill="{color}" font-size="12">T{key} {score:.2f} {html.escape(t["trackingState"])}</text>')
                for person in gt:
                    l,top,r,b=person['box']
                    marks.append(f'<rect x="{l}" y="{top}" width="{r-l}" height="{b-top}" fill="none" stroke="#66ff66" stroke-dasharray="5,3"/>'
                                 f'<text x="{l}" y="{min(398,b+12)}" fill="#66ff66" font-size="12">GT {html.escape(person["person_id"])}</text>')
                candidates=row['candidate_payload']['candidates']
                panels.append(f'<section><h3>{arm} f{frame} — candidates {len(candidates)} / unassigned {len(row["unassigned"])}</h3>'
                    f'<svg viewBox="0 0 640 400"><image href="images/{cid}/{frame:04d}.jpg" width="640" height="400"/>{"".join(marks)}</svg></section>')
        content='<!doctype html><meta charset="utf-8"><style>body{background:#151b24;color:white;font:14px sans-serif}main{display:grid;grid-template-columns:repeat(3,1fr)}svg{width:100%}section{padding:6px}h3{font-size:13px}</style>'
        content+=f'<h1>{cid}</h1><p>Blue: association usable / orange: unusable. Green dashed: approved exact-frame GT only. No interpolation.</p><main>'+''.join(panels)+'</main>'
        with (output/f'{cid}.html').open('x') as stream:stream.write(content)
        index.append(f'<li><a href="{cid}.html">{cid}</a></li>')
    with (output/'index.html').open('x') as stream:
        stream.write('<!doctype html><meta charset="utf-8"><h1>Pose view comparison — offline development experiment</h1><ul>'+''.join(index)+'</ul>')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('baseline','audit','spatial','dataset','model','output'):
        parser.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args();require(not args.output.exists(),'preserve old results')
    verify_frozen(args.baseline)
    sources=dict(read(args.baseline/'provenance.json')['sources'])
    for p in [args.model,args.baseline/'completed.json',args.audit/'cases.json',args.spatial/'media.json',
              args.spatial/'freeze.json',Path(__file__),Path(__file__).with_name('experimental_pose_views.py'),
              Path(__file__).with_name('experimental_roi_pose.py'),Path(__file__).with_name('replay_box_association_ablation.py'),
              Path(__file__).with_name('experimental_box_association.py')]:sources[str(p)]=digest(p)
    args.output.mkdir(parents=True,mode=0o700)
    config=read(REPO/'malbut_agent_server/config/fall_runtime.example.json')
    metas={m['case_id']:m for m in read(args.spatial/'media.json')['cases']}
    records,jobs={},{}
    for model,entries in read(args.audit/'cases.json').items():
        for entry in entries:
            cid=entry['case_id']
            if cid not in CASES:continue
            path=Path(entry['result_path']);inp=path.parent.parent/'inputs'/f'{cid}.input.json'
            result,record=read(path),read(inp)
            require(result['case_id']==cid,'wrong cached response')
            if cid in records:require(record==records[cid],'models used different inputs')
            records[cid]=record;jobs[model,cid]=result
            sources.update({str(p):digest(p) for p in (path,inp)})
    require(len(jobs)==18 and set(records)==set(CASES),'missing paired cases')
    save(args.output/'plan.json',dict(cases=CASES,arms=ARMS,
        rotations=['90 degrees CW','90 degrees CCW'],tiles=fixed_tiles((640,400)),
        fusion='highest score, near-duplicate IoU >= .85, full wins score tie',
        crop_border='exclude detections touching an internal crop edge exactly',
        pose_candidate_threshold=.10,strong_threshold=.45,fall_config=asdict(FallCandidateConfig()),
        max_sample_fps=5,association_conditions=['production','box_only_experimental'],
        selection='4 known multiperson failures/control + 413/308/410/425/435 stress controls',
        model_sha256=digest(args.model),new_api_calls=0,gt_only_posthoc=True,
        scope='known development scenes; fixed aligned inputs; no live ROS/scheduler/delay',
        timing='inference only on PC CPU, excludes transforms/decode/tracking; shared full inference; not Jetson'))
    import cv2,numpy as np,onnxruntime
    estimator=PersonPoseEstimator(str(args.model),.45,.5,input_size=640,keep_aspect=True)
    estimator.estimate_all(np.zeros((400,640,3),np.uint8),confidence_threshold=.10)
    rows,originals={},{};calls=1
    for cid in CASES:
        prior_path=args.baseline/f'{cid}.pose.json'
        prior=read(prior_path) if prior_path.exists() else None
        if prior:sources[str(prior_path)]=digest(prior_path)
        source=args.dataset/metas[cid]['source_path'];sources[str(source)]=digest(source)
        print('inference',cid,flush=True)
        scene,originals[cid],count=infer_scene(estimator,source,metas[cid],records[cid],prior,args.output)
        calls+=count
        for arm,data in scene.items():rows[arm,cid]=data
    print('inference complete; cached merge replay',flush=True)
    replays={}; comparisons=[]
    for arm in ARMS:
        for mode,run in [('production',replay_scene),('box_only',replay_box_only)]:
            for (model,cid),result in jobs.items():
                name=f'{arm}-{mode}-{model}-{cid}'
                value=run(rows[arm,cid],originals[cid],records[cid],result,config,args.output/f'{name}.sqlite')
                save(args.output/f'{name}.replay.json',value);replays[arm,mode,model,cid]=value
                comparisons.append(dict(arm=arm,mode=mode,model=model,case_id=cid,**outcome(value)))
    save(args.output/'comparison.json',comparisons)
    # Read GT only after all inference, tracking, candidate generation and merges.
    freeze=read(args.spatial/'freeze.json')
    for name,h in freeze['files'].items():
        p=args.spatial/name;require(digest(p)==h,'approved GT changed');sources[str(p)]=h
    bundle=read(args.spatial/'evaluation_labels.json')
    annotations={c['case_id']:c for c in bundle['annotations']['cases']}
    labels={c['case_id']:c['label'] for c in bundle['classifications']['cases']}
    scores=[];links=[];detections={};metrics=[]
    for arm in ARMS:
        for cid in CASES:
            detections[arm+'-'+cid]=detection_score(rows[arm,cid],annotations[cid],metas[cid],freeze['match'])
            candidates=[dict(source_frame=r['source_frame'],**c) for r in rows[arm,cid]
                        for c in r['candidate_payload']['candidates']]
            anchor=[d for d in detections[arm+'-'+cid] if d['role']=='target']
            metrics.append(dict(arm=arm,case_id=cid,label=labels[cid],frames=len(rows[arm,cid]),
                target_gt_samples=len(anchor),target_box_matched=sum(d['all_status']=='matched' for d in anchor),
                strong_target_box_matched=sum(d['strong_status']=='matched' for d in anchor),
                candidate_count=len(candidates),candidate_frames=[c['source_frame'] for c in candidates],
                unassigned_frames=sum(bool(r['unassigned']) for r in rows[arm,cid]),
                inference_median_ms=statistics.median(r['inference_s']*1000 for r in rows[arm,cid]),
                all_pose_detections=sum(len(r['poses']) for r in rows[arm,cid])))
            save(args.output/f'{arm}-{cid}.candidates.json',candidates)
        for mode in ('production','box_only'):
            for model in ('gemma','gemini'):
                scored=score_tracks({cid:replays[arm,mode,model,cid]['pose_timeline'] for cid in CASES},
                                    annotations,metas,freeze['match'])
                save(args.output/f'{arm}-{mode}-{model}.gt-score.json',scored)
                for cid in CASES:
                    links.append(dict(arm=arm,mode=mode,model=model,case_id=cid,
                        findings=score_linked_people(replays[arm,mode,model,cid],scored[cid])))
    save(args.output/'detection-score.json',detections)
    save(args.output/'metrics.json',metrics);save(args.output/'linked-person-score.json',links)
    render_pages(rows,annotations,metas,args.output)
    require(all(digest(p)==h for p,h in sources.items()),'sources changed during run')
    save(args.output/'provenance.json',dict(sources=sources,new_api_calls=0,inference_calls=calls,
        warmup_calls=1,sampled_frames=sum(len(rows['full',cid]) for cid in CASES),
        python=sys.version,cv2=cv2.__version__,numpy=np.__version__,onnxruntime=onnxruntime.__version__,
        frozen_four_scene_inference_reproduced=True,production_code_changed=False,gt_used_for_inference=False))
    save(args.output/'completed.json',dict(files={str(p.relative_to(args.output)):digest(p)
        for p in sorted(args.output.rglob('*')) if p.is_file()}))
    print(json.dumps(metrics,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
