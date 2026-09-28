#!/usr/bin/env python3
"""Development-only marked-ID and Cloud-box-seeded SAM experiments.

No robot/incident writes. Inference never reads human labels. Exact-frame human
boxes are read in a separate score command, after both experiments finish.
"""
import argparse
import asyncio
import base64
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import time

from replay_reviewed_pose_cloud import digest, read, require, save

CASES = ('SYN012', 'SYN045', 'SYN063', 'SYN067')
ASSESSMENTS = {'observed_fall', 'suspected_fall', 'normal_activity', 'unobservable'}
PROMPT = '''Examine these chronological RGB frames for falls or possible falls.
Person candidates have temporary IDs such as T1. Their per-frame boxes are in
the supplied table, in 0..1000 [left, top, right, bottom] coordinates. Some images
also show these same IDs visually. IDs and boxes are fallible detector outputs,
NOT ground truth and NOT evidence of a fall. A candidate may be absent or wrong.
Assess visible motion: observed_fall requires a visible unintentional falling
transition. Already lying down without clear cause is suspected_fall; clearly
intentional rest or controlled movement is normal_activity. Do not confuse a
helper or bystander with a person lying down. For each concerning person choose
one candidate ID only if it actually refers to that person. Use track_id=null if
the person has no suitable candidate or cannot be associated confidently. Do not
invent IDs or merge different IDs. evidence_frames must be zero-based input
indices where that chosen ID is present and visibly corresponds to that person;
for null it identifies frames showing the unassociated person. Do not extrapolate
identity across missing observations. An ID is a reference, not proof of identity.
Return exactly one JSON object, no commentary:
{"assessment":"observed_fall|suspected_fall|normal_activity|unobservable",
 "explanation":"visible reasons",
 "findings":[{"assessment":"observed_fall|suspected_fall",
 "track_id":"T1 or null (JSON null, not string)",
 "evidence_frames":[0,1],"explanation":"visible reasons for association"}]}.
findings=[] for normal_activity or unobservable. If unsure, preserve uncertainty.
'''


def byte_hash(data):
    return hashlib.sha256(data).hexdigest()


def valid_box(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(type(x) in (int, float) and math.isfinite(x) for x in box)
            and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)


def candidate_frames(rows, indices):
    """Only actual observed boxes; missing-track boxes are never copied forward."""
    lookup = {r['source_frame']: r for r in rows}
    mapping, frames = {}, []
    for index, source in enumerate(indices):
        candidates = []
        for track in lookup[source]['candidate_payload']['tracks']:
            box = track.get('box')
            if track['trackingState'] != 'tracked' or not valid_box(box):
                continue
            tid = track['targetTrackId']
            mapping.setdefault(tid, f'T{len(mapping)+1}')
            candidates.append(dict(id=mapping[tid], track_id=tid, box=box,
                                   association_usable=track['associationUsable']))
        require(len({c['id'] for c in candidates}) == len(candidates), 'duplicate candidate ID')
        frames.append(dict(frame_index=index, source_frame=source, candidates=candidates))
    return frames


def draw_marks(jpeg, candidates):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.open(io.BytesIO(jpeg)).convert('RGB')
    require(image.size == (640, 400), 'unexpected canvas')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 14)
    colors = ('#00ffff', '#ffff00', '#ff80ff', '#80ff80', '#ffad55', '#a0bfff')
    for c in candidates:
        require(valid_box(c['box']), 'invalid overlay box')
        l, t, r, b = [v*(640 if i % 2 == 0 else 400) for i, v in enumerate(c['box'])]
        color = colors[(int(c['id'][1:])-1) % len(colors)]
        draw.rectangle((l, t, r, b), outline=color, width=2)
        label = c['id']
        width = draw.textbbox((0, 0), label, font=font)[2] + 6
        x, y = min(l, 640-width), max(0, t-20)
        draw.rectangle((x, y, x+width, y+20), fill='black', outline=color)
        draw.text((x+3, y), label, fill=color, font=font)
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=90)
    return output.getvalue()


def parse_reply(text, frames):
    from paid_vlm.providers import strict_json
    text = text.strip()
    if text.startswith('```') and text.endswith('```'):
        lines = text.splitlines()
        require(lines[0] in ('```', '```json') and lines[-1] == '```', 'invalid fence')
        text = '\n'.join(lines[1:-1])
    obj = strict_json(text)
    require(isinstance(obj, dict) and obj.get('assessment') in ASSESSMENTS, 'invalid assessment')
    require(isinstance(obj.get('explanation'), str), 'missing explanation')
    require(isinstance(obj.get('findings'), list), 'invalid findings')
    for f in obj['findings']:
        require(isinstance(f, dict), 'invalid finding')
        require(f.get('assessment') in {'observed_fall', 'suspected_fall'}, 'invalid finding assessment')
        require(isinstance(f.get('explanation'), str), 'missing finding explanation')
        require('track_id' in f and 'evidence_frames' in f, 'missing association fields')
        indices = f['evidence_frames']
        require(isinstance(indices, list) and indices, 'empty evidence')
        require(all(type(i) is int and 0 <= i < len(frames) for i in indices), 'invalid evidence index')
        require(indices == sorted(set(indices)), 'duplicate/unordered evidence')
        tid = f['track_id']
        require(tid is None or isinstance(tid, str), 'invalid track ID')
        if tid is not None:
            require(all(any(c['id'] == tid for c in frames[i]['candidates']) for i in indices),
                    'ID absent at cited frame')
    require(obj['assessment'] not in {'normal_activity', 'unobservable'} or not obj['findings'],
            'contradictory findings')
    require(obj['assessment'] not in {'observed_fall', 'suspected_fall'} or obj['findings'],
            'missing positive finding')
    return obj


def prepare(args):
    require(not args.output.exists(), 'preserve previous output')
    args.output.mkdir(mode=0o700)
    sources, cases, sam_jobs = {}, {}, []
    audit = read(args.audit/'cases.json')
    for cid in CASES:
        entry = next(e for e in audit['gemma'] if e['case_id'] == cid)
        result_path = Path(entry['result_path'])
        input_path = result_path.parent.parent/'inputs'/f'{cid}.input.json'
        pose_path = args.baseline/f'{cid}.pose.json'
        record, rows = read(input_path), read(pose_path)
        indices = record['evidence']['frame_indices']
        require(len(indices) == len(record['common']['images']) == 12, 'unexpected input count')
        frames = candidate_frames(rows, indices)
        dest = args.output/cid
        (dest/'original').mkdir(parents=True)
        (dest/'marked').mkdir()
        frame_hashes = []
        for i, encoded in enumerate(record['common']['images']):
            jpeg = base64.b64decode(encoded, validate=True)
            require(byte_hash(jpeg) == record['evidence']['jpeg_sha256'][i], 'source JPEG changed')
            marked = draw_marks(jpeg, frames[i]['candidates'])
            # Exact frozen source JPEG is not re-encoded in the unmarked arm.
            for name, data in [('original', jpeg), ('marked', marked)]:
                with (dest/name/f'{i:05d}.jpg').open('xb') as stream:
                    stream.write(data)
            frame_hashes.append(dict(original=byte_hash(jpeg), marked=byte_hash(marked)))
        text = json.dumps(dict(frame_times_s=record['evidence']['source_times_s'],
            candidates=[dict(frame_index=f['frame_index'], people=[
                dict(id=c['id'], box_xyxy_1000=[round(x*1000) for x in c['box']])
                for c in f['candidates']]) for f in frames]), separators=(',', ':'))
        cases[cid] = dict(frames=frames, text=text, hashes=frame_hashes,
                          source_sha256=record['evidence']['source_sha256'])
        for p in (pose_path, input_path):
            sources[str(p)] = digest(p)
        for model in ('gemma', 'gemini'):
            e = next(e for e in audit[model] if e['case_id'] == cid)
            path = Path(e['result_path'])
            original_input = path.parent.parent/'inputs'/f'{cid}.input.json'
            require(read(original_input)['evidence'] == record['evidence'], 'different cached evidence')
            sources.update({str(p): digest(p) for p in (path, original_input)})
            result = read(path)
            if result['outcome'] != 'classified':
                sam_jobs.append(dict(model=model, case_id=cid, skip='unusable_cached_reply'))
                continue
            for n, finding in enumerate(result['normalized_response']['findings']):
                require(finding['regions'], 'missing seed')
                seed = min(finding['regions'], key=lambda r: r['frame_index'])
                require(valid_box(seed['box']), 'invalid seed box')
                sam_jobs.append(dict(model=model, case_id=cid, finding_index=n,
                                     seed=seed, assessment=finding['assessment']))
    save(args.output/'plan.json', dict(cases=cases, sam_jobs=sam_jobs, sources=sources,
        system=PROMPT, script_sha256=digest(__file__), model='gemma4:31b',
        budget_usd='0.10', request_reserve_usd='0.01', max_calls=8,
        request_timeout_s=20, auto_retry=False, gt_used_for_inference=False,
        purpose='development components, not incident merge or live delay evaluation',
        sam_seed='earliest region from each valid cached finding, no human corrections',
        sam_forward_only=True))
    print(json.dumps(dict(prepared=str(args.output), cases=len(cases), calls=8,
                          valid_sam_seeds=sum('seed' in j for j in sam_jobs))))


def verify_inputs(root, plan):
    require(plan['script_sha256'] == digest(__file__), 'experiment code changed after preparation')
    for p, expected in plan['sources'].items():
        require(digest(p) == expected, 'frozen source changed')
    for cid, case in plan['cases'].items():
        for i, hashes in enumerate(case['hashes']):
            for arm, expected in hashes.items():
                require(digest(root/cid/arm/f'{i:05d}.jpg') == expected, 'prepared JPEG changed')


async def run_cloud(args):
    from paid_vlm.providers import MODELS, endpoint, headers, normalize, payload
    from paid_vlm.runner import https_post
    from paid_vlm.metrics import estimate_cost, validate_rate
    require(args.execute and args.approve_upload, 'explicit upload/execute required')
    plan = read(args.output/'plan.json')
    verify_inputs(args.output, plan)
    dest = args.output/'cloud'
    require(not dest.exists(), 'do not repeat or overwrite paid run')
    rate = read(args.rate_run)['rates']['gemma4:31b']
    validate_rate(rate)
    key = read(args.key_file)['OLLAMA_API_KEY']
    model = MODELS['gemma4:31b']
    request_headers = headers(model, key)
    dest.mkdir(mode=0o700)
    save(dest/'run.json', dict(rates=rate, pricing_rechecked_on='2026-09-27',
                             plan_sha256=digest(args.output/'plan.json')))
    spent, rows, reserve = Decimal(0), [], Decimal(plan['request_reserve_usd'])
    for case_no, (cid, case) in enumerate(plan['cases'].items()):
        arms = ('original', 'marked') if case_no % 2 == 0 else ('marked', 'original')
        for arm in arms:
            if spent + reserve > Decimal(plan['budget_usd']):
                save(dest/'completed.json', dict(complete=False, calls=len(rows), known_cost_usd=str(spent),
                     stopped='budget exhausted; no further calls'))
                return
            common = dict(system=plan['system'], text=case['text'], images=[
                base64.b64encode((args.output/cid/arm/f'{i:05d}.jpg').read_bytes()).decode()
                for i in range(12)])
            body = json.dumps(payload(model, common), ensure_ascii=False).encode()
            require(len(body) <= 16*1024*1024, 'request too large')
            save(dest/f'{cid}-{arm}.started.json', dict(case_id=cid, arm=arm,
                 request_sha256=byte_hash(body), max_wait_s=20))
            started = time.monotonic()
            row = dict(case_id=cid, arm=arm, parsed=None, outcome='request_failed')
            try:
                status, raw = await https_post(endpoint(model), body, request_headers, timeout_s=20)
                row.update(normalize(model, status, raw), http_status=status,
                           response_sha256=byte_hash(raw))
                require(key not in (row.get('text') or ''), 'credential echoed')
                if row['outcome'] == 'response':
                    try:
                        row['parsed'] = parse_reply(row['text'], case['frames'])
                        row['outcome'] = 'classified'
                    except (ValueError, TypeError, KeyError):
                        row['outcome'] = 'invalid_response'
            except asyncio.TimeoutError:
                row['outcome'] = 'timeout'
            except Exception:
                row.update(outcome='request_failed', text=None)
            row['elapsed_s'] = time.monotonic()-started
            row['cost_estimate_usd'] = estimate_cost(row.get('usage'), rate)
            save(dest/f'{cid}-{arm}.result.json', row)
            rows.append(row)
            print(json.dumps({k: row[k] for k in ('case_id','arm','outcome','elapsed_s','cost_estimate_usd')}), flush=True)
            if row['cost_estimate_usd'] is None or row['outcome'] in {'auth_error','quota_error','rate_or_quota_error'}:
                save(dest/'completed.json', dict(complete=False, calls=len(rows), known_cost_usd=str(spent),
                     stopped='unknown cost or authorization/quota error; no retry'))
                return
            spent += Decimal(row['cost_estimate_usd'])
    save(dest/'completed.json', dict(complete=True, calls=len(rows), known_cost_usd=str(spent)))


def mask_box(mask):
    import numpy as np
    require(mask.ndim == 2, 'mask must be 2D')
    y, x = np.where(mask)
    if not len(x):
        return None
    h, w = mask.shape
    return [int(x.min())/w, int(y.min())/h, (int(x.max())+1)/w, (int(y.max())+1)/h]


def cpu_sam_predictor(checkpoint):
    from sam2.build_sam import build_sam2_video_predictor
    predictor = build_sam2_video_predictor('configs/sam2.1/sam2.1_hiera_t.yaml',
        str(checkpoint), device='cpu')
    # The upstream builder appends fill_hole_area=8 AFTER user Hydra overrides.
    # Explicitly disable only this optional CUDA postprocessor for CPU runs;
    # retain the upstream stability and memory-binarization options.
    predictor.fill_hole_area = 0
    return predictor


def run_sam(args):
    import numpy as np
    from PIL import Image
    import torch
    plan = read(args.output/'plan.json')
    verify_inputs(args.output, plan)
    dest = args.output/'sam'
    require(not dest.exists(), 'preserve SAM run')
    dest.mkdir(mode=0o700)
    torch.set_num_threads(4)
    start = time.perf_counter()
    predictor = cpu_sam_predictor(args.checkpoint)
    save(dest/'run.json', dict(model='sam2.1_hiera_tiny', checkpoint_sha256=digest(args.checkpoint),
        source_commit=subprocess.check_output(['git','-C',str(args.sam_source),'rev-parse','HEAD'],text=True).strip(),
        torch=torch.__version__, device='cpu', precision='float32', threads=4,
        fill_hole_area=predictor.fill_hole_area, load_s=time.perf_counter()-start,
        temporal_input='12 frozen Cloud frames, not full-rate video', forward_only=True,
        plan_sha256=digest(args.output/'plan.json')))
    results = []
    with torch.inference_mode():
        for job in plan['sam_jobs']:
            if 'skip' in job:
                results.append(job)
                continue
            cid = job['case_id']
            name = f"{job['model']}-{cid}-{job['finding_index']}"
            masks_dir = dest/name
            masks_dir.mkdir()
            started = time.perf_counter()
            state = predictor.init_state(str(args.output/cid/'original'),
                offload_video_to_cpu=True, offload_state_to_cpu=True)
            seed = job['seed']
            box = np.array([v*(640 if i % 2 == 0 else 400) for i,v in enumerate(seed['box'])], dtype=np.float32)
            predictor.add_new_points_or_box(state, frame_idx=seed['frame_index'], obj_id=1, box=box)
            samples = []
            for index, ids, logits in predictor.propagate_in_video(state, start_frame_idx=seed['frame_index']):
                require(ids == [1], 'unexpected SAM object ID')
                mask = (logits[0,0] > 0).cpu().numpy()
                require(mask.shape == (400,640), 'wrong mask shape')
                Image.fromarray((mask.astype(np.uint8)*255)).save(masks_dir/f'{index:05d}.png')
                samples.append(dict(frame_index=index, box=mask_box(mask), mask_pixels=int(mask.sum()),
                    mask_sha256=digest(masks_dir/f'{index:05d}.png')))
            value = dict(**job, samples=samples, elapsed_s=time.perf_counter()-started)
            save(dest/f'{name}.json', value)
            results.append(value)
            print(json.dumps(dict(sam=name, frames=len(samples), elapsed_s=value['elapsed_s'])),flush=True)
            del state
    save(dest/'completed.json', dict(complete=True, results=results, new_api_calls=0))


def identify(box, gt, criteria):
    from score_fall_baseline import match_boxes
    if box is None:
        return None
    pixels = [v*(640 if i % 2 == 0 else 400) for i,v in enumerate(box)]
    matches = match_boxes([g['box'] for g in gt], [pixels], criteria)
    people = [g['person_id'] for g,(status,_) in zip(gt,matches) if status == 'matched']
    return people[0] if len(people) == 1 else None


def pose_bridge(box, candidates, min_iou=.60, margin=.15):
    from malbut_agent_server.application.fall_cloud_association import box_iou
    if box is None:
        return None
    ranked = sorted([(box_iou(tuple(box),tuple(c['box'])), c['id'], c) for c in candidates],reverse=True)
    if not ranked or ranked[0][0] < min_iou:
        return None
    if len(ranked)>1 and ranked[0][0]-ranked[1][0]<margin:
        return None
    return ranked[0][2] if ranked[0][2]['association_usable'] else None


def score(args):
    from audit_paid_vlm_localization import exact_gt
    plan = read(args.output/'plan.json')
    verify_inputs(args.output, plan)
    cloud_done = read(args.output/'cloud/completed.json')
    sam_done = read(args.output/'sam/completed.json')
    require(sam_done['complete'], 'SAM incomplete')
    freeze = read(args.spatial/'freeze.json')
    for name, h in freeze['files'].items():
        require(digest(args.spatial/name) == h, 'human labels changed')
    criteria = freeze['match']
    annotations = {c['case_id']:c for c in read(args.spatial/'evaluation_labels.json')['annotations']['cases']}
    metas = {c['case_id']:c for c in read(args.spatial/'media.json')['cases']}
    frames_gt, availability, selected, sam_scores = {}, [], [], []
    for cid, case in plan['cases'].items():
        require(metas[cid]['sha256'] == case['source_sha256'], 'wrong GT video')
        target = annotations[cid]['target_person_id']
        for frame in case['frames']:
            i = frame['frame_index']
            gt = exact_gt(annotations[cid], frame['source_frame'], metas[cid])
            frames_gt[cid,i] = gt
            if not gt:
                continue
            candidates = [dict(**c, gt_person=identify(c['box'],gt,criteria)) for c in frame['candidates']]
            availability.append(dict(case_id=cid,frame_index=i,source_frame=frame['source_frame'],
                candidates=candidates, target_present=any(c['gt_person']==target for c in candidates),
                target_usable=any(c['gt_person']==target and c['association_usable'] for c in candidates)))
        for arm in ('original','marked'):
            path = args.output/'cloud'/f'{cid}-{arm}.result.json'
            if not path.exists():
                continue
            row = read(path)
            findings = []
            for f in (row.get('parsed') or {}).get('findings',[]):
                tid = f['track_id']
                observations = []
                if tid is not None:
                    # Audit ALL reviewed frames where the selected ID appears,
                    # not only the favorable frames cited by the model.
                    for frame in case['frames']:
                        gt = frames_gt[cid,frame['frame_index']]
                        c = next((c for c in frame['candidates'] if c['id']==tid), None)
                        if gt and c:
                            person = identify(c['box'],gt,criteria)
                            observations.append(dict(frame_index=frame['frame_index'], gt_person=person,
                                target=person==target, wrong_person=person is not None and person!=target,
                                association_usable=c['association_usable'],cited=frame['frame_index'] in f['evidence_frames']))
                findings.append(dict(**f, audited_observations=observations,
                    wrong_person=any(o['wrong_person'] for o in observations),
                    selected_target_on_reviewed_frames=bool(observations) and all(o['target'] for o in observations),
                    reviewed_frames=len(observations)))
            selected.append(dict(case_id=cid,arm=arm,outcome=row['outcome'],
                assessment=(row.get('parsed') or {}).get('assessment'),findings=findings,
                elapsed_s=row['elapsed_s'],cost_estimate_usd=row['cost_estimate_usd']))
    for result in sam_done['results']:
        if 'skip' in result:
            continue
        cid = result['case_id']; target = annotations[cid]['target_person_id']; audited=[]; bridges=[]
        for sample in result['samples']:
            i = sample['frame_index']; gt=frames_gt[cid,i]
            candidates=plan['cases'][cid]['frames'][i]['candidates']
            bridge=pose_bridge(sample['box'], candidates)
            if bridge:
                bridges.append(dict(frame_index=i,id=bridge['id'],track_id=bridge['track_id'],
                    gt_person=identify(bridge['box'],gt,criteria) if gt else None))
            if not gt:
                continue
            who = identify(sample['box'],gt,criteria)
            seed_who = identify(result['seed']['box'],gt,criteria)
            audited.append(dict(frame_index=i,seed_frame=i==result['seed']['frame_index'],
                gt_person=who,target=who==target,wrong_person=who is not None and who!=target,
                static_seed_box_target=seed_who==target))
        sam_scores.append(dict(case_id=cid,model=result['model'],finding_index=result['finding_index'],
            elapsed_s=result['elapsed_s'],audited=audited,pose_bridge_observations=bridges,
            bridge_track_ids=sorted({b['track_id'] for b in bridges}),
            # These are geometric observations, NOT runtime merges or proof of continuity.
            incident_merge_tested=False))
    save(args.output/'scores.json',dict(cloud_run=cloud_done,availability=availability,
        cloud_selections=selected,sam=sam_scores,criteria=criteria,
        gt_sha256=digest(args.spatial/'evaluation_labels.json'),
        warning='Sparse exact-frame bbox correspondence only. No continuous identity GT, no incident merges, no live delay.'))
    print(json.dumps(dict(scored=str(args.output/'scores.json'),cloud_rows=len(selected),sam_seeds=len(sam_scores))))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','cloud','sam','score'])
    parser.add_argument('--output',type=Path,required=True)
    for name in ('baseline','audit','spatial','rate-run','key-file','checkpoint','sam-source'):
        parser.add_argument('--'+name,type=Path)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--approve-upload',action='store_true')
    args=parser.parse_args()
    if args.command=='cloud':
        asyncio.run(run_cloud(args))
    else:
        {'prepare':prepare,'sam':run_sam,'score':score}[args.command](args)


if __name__=='__main__':
    main()
