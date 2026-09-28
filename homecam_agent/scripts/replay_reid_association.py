#!/usr/bin/env python3
"""Cached RGB/Pose/Cloud comparison with existing OSNet; no new Cloud calls.

Only same-frame region assignment is changed. Pose confidence, tracker, continuity
tokens, candidates, all-region checks and actual incident guards stay unchanged.
Human annotations are loaded only after all association/replay decisions.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import time

from replay_reviewed_pose_cloud import REPO, digest, frozen_jpegs, read, replay_scene, require, save, score_tracks
from replay_box_association_ablation import outcome, score_linked_people, verify_frozen
from replay_pose_view_comparison import CASES
from experimental_reid_association import ARMS, AssociationExperiment, ReIDConfig, cosine, offline_associator, valid_box
from malbut_reid.models import BoundingBox, ImageDetection
from malbut_reid.reid.osnet_encoder import OsNetPersonEncoder


class FrozenFeatures:
    """Bind each embedding to the exact frozen frame/hash/box; no past gallery."""
    def __init__(self, originals, record, encoder):
        self.originals, self.record, self.encoder = originals, record, encoder
        self.cache, self.images, self.entries = {}, {}, []

    def __call__(self, index, box):
        import cv2
        import numpy as np
        if type(index) is not int or not 0 <= index < len(self.record['evidence']['frame_indices']):
            raise ValueError('feature frame outside frozen request')
        if not valid_box(box):
            raise ValueError('invalid feature box; no clamping/coordinate repair')
        key = (index, tuple(box))
        if key in self.cache:
            return self.cache[key]
        source = self.record['evidence']['frame_indices'][index]
        jpeg = self.originals[source]
        expected = self.record['evidence']['jpeg_sha256'][index]
        require(hashlib.sha256(jpeg).hexdigest() == expected, 'feature RGB changed')
        if index not in self.images:
            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            require(image is not None and image.shape == (400, 640, 3), 'bad frozen image')
            self.images[index] = image
        pixel_box = BoundingBox(*(v * (640 if i % 2 == 0 else 400) for i, v in enumerate(box)))
        started = time.perf_counter()
        feature = self.encoder.encode(self.images[index], [ImageDetection(pixel_box, 1.0)])[0]
        elapsed = time.perf_counter() - started
        # Score=1.0 above is an unused encoder argument, never detector confidence.
        if feature is not None:
            require(feature.shape == (512,) and cosine(feature, feature) is not None, 'bad OSNet output')
        self.cache[key] = feature
        self.entries.append(dict(frame_index=index, source_frame=source, jpeg_sha256=expected,
            box=box, crop_encode_s=elapsed, feature=feature.tolist() if feature is not None else None))
        return feature


def deduplicated_diagnostics(experiment):
    """Exclude repeated monitor delivery used only for idempotence testing."""
    import json
    seen, values = set(), []
    for record in experiment.records:
        fingerprint = json.dumps(record, sort_keys=True)
        if fingerprint not in seen:
            values.append(record)
            seen.add(fingerprint)
    return values


def pair_audit(diagnostics, features, annotations, meta, criteria):
    """Posthoc geometric GT correspondence, not independent ReID identity GT."""
    from audit_paid_vlm_localization import exact_gt
    from score_fall_baseline import match_boxes

    def identity(box, gt):
        pixels = [v * (640 if i % 2 == 0 else 400) for i, v in enumerate(box)]
        matches = match_boxes([g['box'] for g in gt], [pixels], criteria)
        people = [g for g, (status, _) in zip(gt, matches) if status == 'matched']
        return people[0]['person_id'] if len(people) == 1 else None

    # Only already computed features are used; no GT crop or inference after GT.
    encoded = {(e['frame_index'], tuple(e['box'])): e['feature'] for e in features.entries}
    records = []
    for finding in diagnostics:
        for region in finding['regions']:
            index = region['frame_index']
            source = features.record['evidence']['frame_indices'][index]
            gt = exact_gt(annotations, source, meta)
            query_id = identity(region['cloud_box'], gt)
            for candidate in region['candidates']:
                score = cosine(encoded.get((index, tuple(region['cloud_box']))),
                               encoded.get((index, tuple(candidate['box']))))
                if score is None:
                    continue
                person_id = identity(candidate['box'], gt)
                relation = ('unverified' if query_id is None or person_id is None else
                            'same_gt_person' if query_id == person_id else 'different_gt_people')
                records.append(dict(source_frame=source, frame_index=index,
                    cloud_box=region['cloud_box'], pose_box=candidate['box'],
                    subject_key=candidate['subject_key'], cloud_person=query_id, pose_person=person_id,
                    relation=relation, cosine=score, iou=candidate['iou'], token_usable=candidate['token'] is not None))
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'audit', 'spatial', 'model', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve previous outputs; choose new output directory')
    verify_frozen(args.baseline)
    require(args.model.is_file(), 'model missing; no automatic download')
    args.output.mkdir(parents=True, mode=0o700)
    sources = dict(read(args.baseline/'provenance.json')['sources'])
    sources.update({str(args.baseline/n): h for n, h in read(args.baseline/'completed.json')['files'].items()})
    paths = [args.baseline/'completed.json', Path(__file__),
        Path(__file__).with_name('experimental_reid_association.py'),
        REPO/'homecam_agent/test/test_reid_association.py', args.model,
        args.audit/'cases.json', args.spatial/'media.json', args.spatial/'freeze.json']
    paths += list((REPO/'malbut_reid/malbut_reid').rglob('*.py'))
    paths.append(REPO/'malbut_reid/scripts/prepare_osnet_model.sh')
    sources.update({str(p): digest(p) for p in paths})
    save(args.output/'plan.json', dict(cases=CASES, arms=ARMS, config=asdict(ReIDConfig()),
        baseline='actual IoU .60 + .15 margin', control='IoU .20 + .15 margin',
        reid='IoU .20 candidates + cosine .80 + .10 margin, all competing crops required',
        pose='frozen full-frame production association only', minimum_crop_pixels=[16,32],
        encoder='existing OSNet-AIN x1.0 MSMT17; existing RGB/ImageNet preprocessing',
        model_sha256=digest(args.model), scope='same-frame assignment only; not temporal ReID/gallery',
        unchanged=['confidence/anatomy/observation guards', 'tracker', 'tokens', 'all returned regions',
                   'latest subject/incident guards', 'Cloud inputs/replies', 'candidates', 'sampling'],
        new_api_calls=0, ground_truth_only_posthoc=True, production_code_changed=False,
        thresholds_selected_before_run=True, no_threshold_search=True))
    import cv2
    import numpy as np
    import onnxruntime as ort
    print('loading cached OSNet', flush=True)
    encoder = OsNetPersonEncoder(str(args.model), dnn_target='cpu', inference_backend='onnxruntime')
    warm = np.zeros((400,640,3), dtype=np.uint8)
    started = time.perf_counter()
    encoder.encode(warm, [ImageDetection(BoundingBox(0,0,128,256),1.0)])
    warm_s = time.perf_counter()-started
    save(args.output/'environment.json', dict(python=sys.version, platform=platform.platform(),
        opencv=cv2.__version__, numpy=np.__version__, onnxruntime=ort.__version__,
        resolved_target=encoder.resolved_target, warmup_s=warm_s,
        timing='encode includes pixel crop/resize/normalization/inference/L2; excludes JPEG decode/load',
        timing_not_jetson=True))
    metas = {m['case_id']: m for m in read(args.spatial/'media.json')['cases']}
    config = read(REPO/'malbut_agent_server/config/fall_runtime.example.json')
    prior = {(r['model'], r['case_id']): r for r in read(args.baseline/'comparison.json')
             if r['arm']=='full' and r['mode']=='production'}
    jobs, inputs, features = {}, {}, {}
    for model, cases in read(args.audit/'cases.json').items():
        for case in cases:
            cid = case['case_id']
            if cid not in CASES:
                continue
            path = Path(case['result_path']); ipath = path.parent.parent/'inputs'/f'{cid}.input.json'
            record, result = read(ipath), read(path)
            require(result['case_id']==cid, 'wrong response')
            if cid in inputs:
                require(inputs[cid] == record, 'providers used different input')
            else:
                inputs[cid] = record
                features[cid] = FrozenFeatures(frozen_jpegs(record, metas[cid]), record, encoder)
            jobs[model,cid] = result
            sources.update({str(p): digest(p) for p in (path,ipath)})
    require(len(jobs)==18 and set(inputs)==set(CASES), 'missing model/case')
    replays, comparisons, diagnostics = {}, [], {}
    for cid in CASES:
        print('replaying', cid, flush=True)
        rows = read(args.baseline/f'full-{cid}.pose.json')
        for model in ('gemma','gemini'):
            for arm in ARMS:
                experiment = AssociationExperiment(arm, features[cid])
                stem = f'{arm}-{model}-{cid}'
                with offline_associator(experiment):
                    replay = replay_scene(rows, features[cid].originals, inputs[cid], jobs[model,cid],
                                          config, args.output/f'{stem}.sqlite')
                summary = outcome(replay)
                if arm == 'baseline':
                    require(all(summary[k]==prior[model,cid][k] for k in summary), 'baseline changed')
                replays[arm,model,cid] = replay
                diagnostics[arm,model,cid] = deduplicated_diagnostics(experiment)
                save(args.output/f'{stem}.replay.json', replay)
                save(args.output/f'{stem}.diagnostic.json', diagnostics[arm,model,cid])
                comparisons.append(dict(arm=arm, model=model, case_id=cid, **summary))
        save(args.output/f'{cid}.features.json', features[cid].entries)
        print(cid, 'unique crops', len(features[cid].entries), flush=True)
    save(args.output/'comparison.json', comparisons)
    # From here on: only audit previous decisions; never re-run a routing function.
    freeze = read(args.spatial/'freeze.json')
    for name, expected in freeze['files'].items():
        path = args.spatial/name
        require(digest(path)==expected, 'approved annotations changed'); sources[str(path)] = expected
    annotations = {c['case_id']: c for c in read(args.spatial/'evaluation_labels.json')['annotations']['cases']}
    links, pairs = [], []
    for arm in ARMS:
        for model in ('gemma','gemini'):
            score = score_tracks({c: replays[arm,model,c]['pose_timeline'] for c in CASES},
                                 annotations, metas, freeze['match'])
            save(args.output/f'{arm}-{model}.gt-score.json', score)
            for cid in CASES:
                links.append(dict(arm=arm,model=model,case_id=cid,
                    findings=score_linked_people(replays[arm,model,cid],score[cid])))
                if arm == 'wider_iou_reid':
                    pairs.append(dict(model=model,case_id=cid,pairs=pair_audit(
                        diagnostics[arm,model,cid], features[cid], annotations[cid], metas[cid], freeze['match'])))
    save(args.output/'linked-person-score.json', links)
    save(args.output/'pair-audit.json', pairs)
    entries = [e for f in features.values() for e in f.entries]
    times = [e['crop_encode_s'] for e in entries if e['feature'] is not None]
    summary = dict(multiperson_cases=CASES[:4], new_api_calls=0, new_pose_inference_calls=0,
        unique_crop_requests=len(entries), osnet_calls=sum(e['feature'] is not None for e in entries),
        warmup_calls=1, insufficient_crop_count=sum(e['feature'] is None for e in entries),
        crop_encode_median_ms=statistics.median(times)*1000 if times else None,
        crop_encode_sum_s=sum(times), arms={})
    for arm in ARMS:
        cc = [c for c in comparisons if c['arm']==arm and c['case_id'] in CASES[:4]]
        summary['arms'][arm] = dict(valid_replies=sum(c['reply_usable'] for c in cc),
            linked=sum(c['linked'] for c in cc),
            reasons=dict(sum((Counter(c['reasons']) for c in cc), Counter())))
    for path in args.output.glob('*.sqlite'):
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as connection:
            require(connection.execute('PRAGMA integrity_check').fetchone()==('ok',), 'SQLite corruption')
    require(all(digest(p)==h for p,h in sources.items()), 'frozen inputs/code changed')
    save(args.output/'summary.json', summary)
    save(args.output/'provenance.json', dict(sources=sources, new_api_calls=0, production_code_changed=False,
        baseline_controls_reproduced=True, ground_truth_used_for_routing=False, replay_count=len(replays)))
    save(args.output/'completed.json', dict(files={str(p.relative_to(args.output)):digest(p)
        for p in sorted(args.output.rglob('*')) if p.is_file()}))
    print(summary, flush=True)


if __name__ == '__main__':
    main()
