#!/usr/bin/env python3
"""Cached Cloud -> SAM forward tracking -> experimental Pose alias, offline only.

Prepare/run do not load human classifications or boxes. Score is a separate
command, after inference completes. No provider, network, runtime or DB writes.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import html
import json
from pathlib import Path
import statistics
import subprocess
import time

from replay_reviewed_pose_cloud import (
    digest, frozen_jpegs, intermediate_jpeg, read, require, save, stamp,
)
from replay_pose_view_comparison import Pipeline
from evaluate_visual_person_linking import cpu_sam_predictor, mask_box, identify
from experimental_visual_subject_bridge import Policy, VisualSubjectBridge, sample_schedule, valid_box
from homecam_detector.pose import PersonPoseEstimator
from malbut_agent_server.application.fall_subject_evidence import FallSubjectEvidence
from malbut_agent_server.domain.fall_monitoring import SubjectFrame, SubjectPose, SubjectCheckState

CASES = {
    'SYN050': 'motion_partial_occlusion', 'SYN059': 'motion_partial_occlusion',
    'SYN075': 'camera_motion', 'SYN078': 'motion_partial_occlusion',
    'SYN083': 'motion_partial_occlusion', 'SYN084': 'motion_camera_rotation',
    'SYN063': 'multiperson_control', 'SYN067': 'multiperson_control',
    'SYN046': 'normal_no_cloud_seed_control',
}


def code_hashes():
    # Include local helpers and production Pose/subject code, not just this runner.
    import sys
    root = Path(__file__).resolve().parents[2]
    return {str(Path(m.__file__).resolve()): digest(m.__file__)
            for m in list(sys.modules.values()) if getattr(m, '__file__', None)
            and str(Path(m.__file__).resolve()).startswith(str(root)+'/')
            and Path(m.__file__).is_file()
            and str(m.__file__).endswith('.py')}


def prepare(args):
    require(not args.output.exists(), 'preserve old outputs')
    metas = {m['case_id']: m for m in read(args.spatial/'media.json')['cases']}
    cases, sources = {}, {str(args.spatial/'media.json'): digest(args.spatial/'media.json')}
    for cid, group in CASES.items():
        inp = args.cached/'inputs'/f'{cid}.input.json'
        response = args.cached/'run'/f'{int(cid[3:]):05d}.result.json'
        rec, result = read(inp), read(response)
        require(result['case_id'] == cid and result['outcome'] == 'classified', 'unusable cached reply')
        findings = result['normalized_response']['findings']
        require(len(findings) <= 1, 'one-object experiment; do not silently drop findings')
        seed = None
        if findings:
            region = min(findings[0]['regions'], key=lambda r: r['frame_index'])
            require(valid_box(region['box']), 'invalid seed')
            seed = dict(source_frame=rec['evidence']['frame_indices'][region['frame_index']],
                        box=region['box'], input_index=region['frame_index'])
        meta = metas[cid]
        originals = frozen_jpegs(rec, meta)
        if seed: require(seed['source_frame'] in originals, 'seed missing')
        video = args.videos/meta['source_path']
        require(digest(video) == meta['sha256'], 'source video changed')
        for p in (inp, response, video): sources[str(p)] = digest(p)
        cases[cid] = dict(group=group, meta=meta, video=str(video), input=str(inp),
                         response=str(response), seed=seed,
                         schedule=sample_schedule(meta['frames'], meta['fps'],
                                                  seed['source_frame'] if seed else None))
    args.output.mkdir(mode=0o700)
    sources[str(args.pose_model)] = digest(args.pose_model)
    sources[str(args.checkpoint)] = digest(args.checkpoint)
    save(args.output/'plan.json', dict(cases=cases, sources=sources, code=code_hashes(),
        policy=asdict(Policy()), pose_model=str(args.pose_model), checkpoint=str(args.checkpoint),
        sam_source=str(args.sam_source), seed_policy='earliest valid cached Gemma region; never reseed',
        schedule='4 Hz neutral grid + exact seed, minimum interval 0.2 s',
        new_api_calls=0, human_boxes_for_inference=False, phase='development, not held-out',
        cached_cloud_saw_whole_clip=True, live_latency_simulated=False,
        common_id_is_not_incident_id=True, production_changed=False))
    print(json.dumps(dict(prepared=str(args.output), clips=len(cases),
                          sam_seeds=sum(c['seed'] is not None for c in cases.values()))), flush=True)


def verify(plan):
    for p, expected in {**plan['sources'], **plan['code']}.items():
        require(digest(p) == expected, f'input or code changed: {p}')


def subject_candidates(row, evidence):
    p = row['candidate_payload']
    subjects, candidates = [], []
    for t in p['tracks']:
        usable = t['associationUsable']
        require(not usable or (t['trackingState'] == 'tracked' and
                (t['features'] or {}).get('usable') is True and p['unassignedCount'] == 0),
                'unsupported usable Pose observation')
        subjects.append(SubjectPose(t['targetTrackId'], tuple(t['box']) if t['box'] else None,
                                    SubjectCheckState.UNKNOWN, usable))
    evidence.append(SubjectFrame(row['captured_at'], tuple(subjects), p['subjectCheckMaxGapSec']))
    for t in p['tracks']:
        if t['trackingState'] == 'tracked' and valid_box(t['box']):
            candidates.append(dict(id=t['targetTrackId'], box=t['box'], usable=t['associationUsable'],
                token=evidence.token_at(t['targetTrackId'], row['captured_at'])))
    # Unassigned detections still block an ambiguous match. Do not hide them.
    for i, u in enumerate(row['unassigned']):
        box = [u['pose']['box'][k] for k in ('left', 'top', 'right', 'bottom')]
        if valid_box(box):
            candidates.append(dict(id=f'unassigned-{i}', box=box, usable=False, token=None))
    return candidates


def run(args):
    import cv2
    import numpy as np
    from PIL import Image
    import torch
    plan = read(args.output/'plan.json')
    verify(plan)
    dest = args.output/'run'
    require(not dest.exists(), 'preserve partial/finished runs; choose a new output for rerun')
    dest.mkdir(mode=0o700)
    torch.set_num_threads(4)
    start = time.perf_counter()
    sam = cpu_sam_predictor(plan['checkpoint'])
    estimator = PersonPoseEstimator(plan['pose_model'], .45, .5, input_size=640, keep_aspect=True)
    save(dest/'environment.json', dict(torch=torch.__version__, device='cpu', threads=4,
        fill_hole_area=sam.fill_hole_area, precision='float32', model_load_s=time.perf_counter()-start,
        sam_commit=subprocess.check_output(['git','-C',plan['sam_source'],'rev-parse','HEAD'],text=True).strip(),
        plan_sha256=digest(args.output/'plan.json')))
    results = []
    for cid, case in plan['cases'].items():
        folder = dest/cid
        images = folder/'images'; images.mkdir(parents=True)
        sam_images = folder/'sam-input'; sam_images.mkdir()
        masks = folder/'masks'; masks.mkdir()
        originals = frozen_jpegs(read(case['input']), case['meta'])
        pipeline = Pipeline()
        evidence = FallSubjectEvidence(retention_s=10, max_frames=64)
        rows, sam_indices = [], []
        cap = cv2.VideoCapture(case['video'])
        require(cap.isOpened(), 'cannot open source video')
        try:
            for index in range(case['schedule'][-1]+1):
                ok, bgr = cap.read(); require(ok, 'source decode failed')
                if index not in case['schedule']: continue
                jpeg = originals[index] if index in originals else intermediate_jpeg(bgr)
                path = images/f'{index:05d}.jpg'
                with path.open('xb') as f: f.write(jpeg)
                path.chmod(0o600)
                if case['seed'] and index >= case['seed']['source_frame']:
                    (sam_images/f'{len(sam_indices):05d}.jpg').hardlink_to(path)
                    sam_indices.append(index)
                pixels = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                require(pixels.shape == (400,640,3), 'wrong image dimensions')
                now = stamp(index, case['meta']['fps'])
                started = time.perf_counter()
                poses = estimator.estimate_all(pixels, confidence_threshold=.10)
                row = pipeline.step(poses, index, now, jpeg, index in originals,
                                    time.perf_counter()-started, [])
                row['link_candidates'] = subject_candidates(row, evidence)
                rows.append(row)
        finally: cap.release()
        save(folder/'pose.json', rows)
        print(json.dumps(dict(pose=cid, frames=len(rows))), flush=True)
        samples = []; sam_elapsed = 0
        if case['seed']:
            bridge = VisualSubjectBridge(cid, Policy(**plan['policy']))
            lookup = {r['source_frame']: r for r in rows}
            started = time.perf_counter()
            with torch.inference_mode():
                state = sam.init_state(str(sam_images), offload_video_to_cpu=True, offload_state_to_cpu=True)
                seed = np.array([x*(640 if i%2==0 else 400) for i,x in enumerate(case['seed']['box'])], dtype=np.float32)
                sam.add_new_points_or_box(state, frame_idx=0, obj_id=1, box=seed)
                for local, ids, logits in sam.propagate_in_video(state, start_frame_idx=0):
                    require(ids == [1], 'unexpected SAM object')
                    index = sam_indices[local]; row = lookup[index]
                    mask = (logits[0,0] > 0).cpu().numpy()
                    require(mask.shape == (400,640), 'wrong mask shape')
                    path = masks/f'{index:05d}.png'
                    Image.fromarray(mask.astype(np.uint8)*255).save(path)
                    box = mask_box(mask)
                    samples.append(dict(source_frame=index, captured_at=row['captured_at'], box=box,
                        mask_sha256=digest(path), mask_pixels=int(mask.sum()),
                        bridge=bridge.step(row['captured_at'], box, row['link_candidates'])))
                del state
            sam_elapsed = time.perf_counter()-started
            require(len(samples) == len(sam_indices), 'SAM did not finish every frame')
        value = dict(case_id=cid, samples=samples, sam_elapsed_s=sam_elapsed,
                     pose_elapsed_s=sum(r['inference_s'] for r in rows),
                     no_seed=case['seed'] is None, pose_frames=len(rows))
        save(folder/'result.json', value)
        results.append(dict(case_id=cid, result_sha256=digest(folder/'result.json'), pose_sha256=digest(folder/'pose.json')))
        print(json.dumps(dict(sam=cid, frames=len(samples), seconds=round(sam_elapsed,2),
            linked_observations=sum(s['bridge']['active'] is not None for s in samples))), flush=True)
    verify(plan)
    save(dest/'completed.json', dict(complete=True, results=results, new_api_calls=0))


def score(args):
    from audit_paid_vlm_localization import exact_gt
    plan = read(args.output/'plan.json'); verify(plan)
    done = read(args.output/'run/completed.json'); require(done['complete'], 'incomplete run')
    freeze = read(args.spatial/'freeze.json')
    for name, expected in freeze['files'].items():
        require(digest(args.spatial/name) == expected, 'review labels changed')
    annotations = {c['case_id']: c for c in read(args.spatial/'evaluation_labels.json')['annotations']['cases']}
    cases = []; panels = []
    for record in done['results']:
        cid = record['case_id']; case = plan['cases'][cid]; folder = args.output/'run'/cid
        for name in ('result', 'pose'):
            require(digest(folder/f'{name}.json') == record[f'{name}_sha256'], 'inference output changed')
        result = read(folder/'result.json'); rows = read(folder/'pose.json')
        for row in rows:
            require(digest(folder/'images'/f'{row["source_frame"]:05d}.jpg') == row['jpeg_sha256'], 'image changed')
        lookup = {r['source_frame']: r for r in rows}
        annotation = annotations[cid]; target = annotation['target_person_id']
        audited = []; events = []
        for sample in result['samples']:
            index = sample['source_frame']; row = lookup[index]
            require(digest(folder/'masks'/f'{index:05d}.png') == sample['mask_sha256'], 'mask changed')
            events.extend(dict(source_frame=index, **e) for e in sample['bridge']['events'])
            gt = exact_gt(annotation, index, case['meta'])
            if not gt or target not in {g['person_id'] for g in gt}: continue
            sam_person = identify(sample['box'], gt, freeze['match'])
            static_person = identify(case['seed']['box'], gt, freeze['match'])
            active = sample['bridge']['active']
            c = next((c for c in row['link_candidates'] if active and c['id'] == active['pose_id']), None)
            pose_person = identify(c['box'], gt, freeze['match']) if c else None
            usable = [identify(c['box'],gt,freeze['match']) for c in row['link_candidates'] if c['usable']]
            audited.append(dict(source_frame=index, seed_frame=index==case['seed']['source_frame'],
                target=target, sam_person=sam_person, static_person=static_person,
                pose_target_available=target in usable, linked=active is not None,
                linked_pose_person=pose_person,
                linked_correct=bool(active and sam_person==pose_person==target),
                linked_wrong_person=bool(active and ((sam_person is not None and sam_person!=target)
                    or (pose_person is not None and pose_person!=target))),
                common_id=sample['bridge']['common_id'], all_people_reviewed=len(gt)==len(annotation['persons'])))
            marks = []
            def rect(box, color, label, pixel=False):
                if box is None: return
                l,t,r,b = box if pixel else [x*(640 if i%2==0 else 400) for i,x in enumerate(box)]
                marks.append(f'<rect x="{l}" y="{t}" width="{r-l}" height="{b-t}" fill="none" stroke="{color}" stroke-width="2"/><text x="{l}" y="{max(12,t-3)}" fill="{color}" font-size="12">{html.escape(label)}</text>')
            for g in gt: rect(g['box'], '#66ff66', 'GT '+g['person_id'], True)
            rect(case['seed']['box'], '#888888', 'static seed')
            rect(sample['box'], '#00ddff', 'SAM')
            if c: rect(c['box'], '#ffff00', 'LINKED '+c['id'])
            panels.append(f'<section><h3>{cid} f{index}: {sample["bridge"]["reason"]}</h3><svg viewBox="0 0 640 400"><image href="run/{cid}/images/{index:05d}.jpg" width="640" height="400"/>{"".join(marks)}</svg></section>')
        post = [a for a in audited if not a['seed_frame']]
        summary = dict(case_id=cid, group=case['group'], no_seed=result['no_seed'],
            sam_frames=len(result['samples']), reviewed_postseed=len(post),
            sam_target=sum(a['sam_person']==target for a in post),
            static_target=sum(a['static_person']==target for a in post),
            sam_wrong_person=sum(a['sam_person'] is not None and a['sam_person']!=target for a in post),
            pose_target_available=sum(a['pose_target_available'] for a in post),
            linked_reviewed=sum(a['linked'] for a in post), correct_links=sum(a['linked_correct'] for a in post),
            wrong_links=sum(a['linked_wrong_person'] for a in post),
            linked_observations=sum(s['bridge']['active'] is not None for s in result['samples']),
            bridge_reasons=dict(Counter(s['bridge']['reason'] for s in result['samples'])),
            visual_segments=len({s['bridge']['common_id'] for s in result['samples'] if s['bridge']['common_id']}),
            sam_elapsed_s=result['sam_elapsed_s'], pose_elapsed_s=result['pose_elapsed_s'], events=events, audited=audited)
        cases.append(summary)
    save(args.output/'scores.json', dict(cases=cases, criteria=freeze['match'],
        labels_sha256=digest(args.spatial/'evaluation_labels.json'),
        caveat='Sparse exact-frame box checks, not full trajectory IDF1 or production incident merges. Development clips.'))
    page='<!doctype html><meta charset="utf-8"><style>body{background:#151b24;color:white;font:14px sans-serif}main{display:grid;grid-template-columns:repeat(3,1fr)}svg{width:100%}section{padding:6px}</style><h1>Motion / Pose bridge development pilot</h1><p>Green: reviewed GT. Blue: SAM. Gray: fixed seed baseline. Yellow: active Pose alias. No GT interpolation.</p><main>'+''.join(panels)+'</main>'
    with (args.output/'index.html').open('x') as f: f.write(page)
    print(json.dumps([dict(case_id=c['case_id'], sam=f"{c['sam_target']}/{c['reviewed_postseed']}",
        static=c['static_target'], links=c['correct_links'], wrong_links=c['wrong_links'],
        linked_observations=c['linked_observations']) for c in cases]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare','run','score'])
    parser.add_argument('--output', type=Path, required=True)
    for name in ('spatial','videos','cached','pose-model','checkpoint','sam-source'):
        parser.add_argument('--'+name, type=Path)
    args = parser.parse_args()
    {'prepare':prepare, 'run':run, 'score':score}[args.command](args)


if __name__ == '__main__': main()
