#!/usr/bin/env python3
"""Offline SAM device comparison on immutable JPEGs and cached Pose observations.

No Cloud calls, labels, robot settings, or identity-threshold changes. Inference
artifacts remain compatible with the separate exact-frame score and incident
replay commands. GPU work is synchronized before recording elapsed time.
"""

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import platform
import shutil
import subprocess
import time

from evaluate_motion_subject_linking import code_hashes, verify
from evaluate_visual_person_linking import mask_box
from experimental_visual_subject_bridge import Policy, VisualSubjectBridge
from replay_reviewed_pose_cloud import digest, read, require, save


PROFILES = {
    'cpu-fp32': ('cpu', 'float32'),
    'cuda-fp32': ('cuda', 'float32'),
    'cuda-bf16': ('cuda', 'bfloat16'),
}


def profile_settings(name):
    if name not in PROFILES:
        raise ValueError('unsupported profile')
    device, precision = PROFILES[name]
    return dict(device=device, precision=precision, threads=4, fill_hole_area=0,
                offload_video_to_cpu=True, offload_state_to_cpu=True,
                allow_tf32=False, compiled=False)


def rows_after_seed(case, rows):
    """Preserve exact source times; never choose frames using annotations."""
    require([r['source_frame'] for r in rows] == case['schedule'], 'schedule changed')
    require(all(a['captured_at'] < b['captured_at'] for a, b in zip(rows, rows[1:])),
            'timestamps must increase')
    if case['seed'] is None:
        return []
    selected = [r for r in rows if r['source_frame'] >= case['seed']['source_frame']]
    require(selected and selected[0]['source_frame'] == case['seed']['source_frame'],
            'exact seed frame missing')
    return selected


def prepare(args):
    require(not args.output.exists(), 'preserve old outputs')
    plan = read(args.baseline / 'plan.json')
    done = read(args.baseline / 'run/completed.json')
    require(done['complete'], 'baseline incomplete')
    require({r['case_id'] for r in done['results']} == set(plan['cases']),
            'baseline case set mismatch')
    sources = dict(plan['sources'])
    for name in ('plan.json', 'run/completed.json', 'run/environment.json'):
        path = args.baseline / name
        sources[str(path)] = digest(path)
    for entry in done['results']:
        folder = args.baseline / 'run' / entry['case_id']
        for name in ('pose', 'result'):
            path = folder / f'{name}.json'
            require(digest(path) == entry[f'{name}_sha256'], 'baseline output changed')
            sources[str(path)] = digest(path)
        rows = read(folder / 'pose.json')
        rows_after_seed(plan['cases'][entry['case_id']], rows)
        for row in rows:
            path = folder / 'images' / f'{row["source_frame"]:05d}.jpg'
            require(digest(path) == row['jpeg_sha256'], 'JPEG changed')
            sources[str(path)] = digest(path)
    plan.update(sources=sources, code=code_hashes(), baseline=str(args.baseline),
                profile=args.profile, device_policy=profile_settings(args.profile),
                pose_recomputed=False, sam_recomputed=True, new_api_calls=0,
                live_latency_simulated=False, production_changed=False)
    verify(plan)
    args.output.mkdir(mode=0o700, parents=True)
    save(args.output / 'plan.json', plan)
    print(json.dumps(dict(prepared=str(args.output), profile=args.profile)), flush=True)


def run(args):
    import numpy as np
    from PIL import Image
    import torch
    from sam2.build_sam import build_sam2_video_predictor

    plan = read(args.output / 'plan.json')
    verify(plan)
    settings = profile_settings(plan['profile'])
    require(settings == plan['device_policy'], 'device policy changed')
    gpu = settings['device'] == 'cuda'
    require(not gpu or torch.cuda.is_available(), 'CUDA unavailable; no CPU fallback')
    require(settings['precision'] != 'bfloat16' or torch.cuda.is_bf16_supported(),
            'bfloat16 unsupported')
    destination = args.output / 'run'
    require(not destination.exists(), 'preserve partial/finished runs')
    destination.mkdir(mode=0o700)
    torch.set_num_threads(settings['threads'])
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False

    def sync():
        if gpu:
            torch.cuda.synchronize()

    sync()
    started = time.perf_counter()
    predictor = build_sam2_video_predictor(
        'configs/sam2.1/sam2.1_hiera_t.yaml', plan['checkpoint'], device=settings['device'])
    # Keep the optional postprocessor disabled just as in the CPU reference.
    predictor.fill_hole_area = settings['fill_hole_area']
    sync()
    save(destination / 'environment.json', dict(
        torch=torch.__version__, cuda=torch.version.cuda,
        gpu=torch.cuda.get_device_name() if gpu else None,
        platform=platform.platform(), settings=settings,
        model_load_s=time.perf_counter() - started,
        sam_commit=subprocess.check_output(
            ['git', '-C', plan['sam_source'], 'rev-parse', 'HEAD'], text=True).strip(),
        plan_sha256=digest(args.output / 'plan.json'),
        warmup='none; first case includes first-use kernel overhead',
        timing='synchronized wall time; JPEG loading, seed, propagation, PNG audit writes',
        pose_recomputed=False, cloud_recomputed=False))
    records = []
    for cid, case in plan['cases'].items():
        original = Path(plan['baseline']) / 'run' / cid
        folder = destination / cid
        images, sam_images, masks = (folder / n for n in ('images', 'sam-input', 'masks'))
        for path in (images, sam_images, masks):
            path.mkdir(mode=0o700, parents=True)
        rows = read(original / 'pose.json')
        selected = rows_after_seed(case, rows)
        # Copies, not hard links: no later output edit can modify the baseline.
        shutil.copyfile(original / 'pose.json', folder / 'pose.json')
        for row in rows:
            name = f'{row["source_frame"]:05d}.jpg'
            shutil.copyfile(original / 'images' / name, images / name)
        for i, row in enumerate(selected):
            shutil.copyfile(images / f'{row["source_frame"]:05d}.jpg', sam_images / f'{i:05d}.jpg')
        bridge = VisualSubjectBridge(cid, Policy(**plan['policy']))
        samples, elapsed, init_s, seed_s = [], 0.0, 0.0, 0.0
        if selected:
            if gpu:
                torch.cuda.reset_peak_memory_stats()
            context = (torch.autocast('cuda', dtype=torch.bfloat16)
                       if settings['precision'] == 'bfloat16' else nullcontext())
            sync()
            started = time.perf_counter()
            with torch.inference_mode(), context:
                state = predictor.init_state(
                    str(sam_images), offload_video_to_cpu=True, offload_state_to_cpu=True)
                sync()
                init_s = time.perf_counter() - started
                seeded_at = time.perf_counter()
                seed = np.array([v * (640 if i % 2 == 0 else 400)
                                 for i, v in enumerate(case['seed']['box'])], dtype=np.float32)
                predictor.add_new_points_or_box(state, frame_idx=0, obj_id=1, box=seed)
                sync()
                seed_s = time.perf_counter() - seeded_at
                iterator = iter(predictor.propagate_in_video(state, start_frame_idx=0))
                for expected, row in enumerate(selected):
                    sync()
                    frame_start = time.perf_counter()
                    local, ids, logits = next(iterator)
                    require(local == expected and ids == [1], 'unexpected SAM object/frame')
                    mask = (logits[0, 0] > 0).cpu().numpy()
                    sync()
                    ready = time.perf_counter()
                    require(mask.shape == (400, 640), 'wrong mask dimensions')
                    path = masks / f'{row["source_frame"]:05d}.png'
                    Image.fromarray(mask.astype(np.uint8) * 255).save(path)
                    box = mask_box(mask)
                    samples.append(dict(
                        source_frame=row['source_frame'], captured_at=row['captured_at'], box=box,
                        mask_sha256=digest(path), mask_pixels=int(mask.sum()),
                        propagate_s=ready - frame_start, available_after_start_s=ready - started,
                        bridge=bridge.step(row['captured_at'], box, row['link_candidates'])))
                require(next(iterator, None) is None, 'unexpected extra frame')
                del iterator, state
            sync()
            elapsed = time.perf_counter() - started
        save(folder / 'result.json', dict(
            case_id=cid, samples=samples, sam_elapsed_s=elapsed, sam_init_s=init_s,
            sam_seed_s=seed_s, gpu_peak_allocated_bytes=(torch.cuda.max_memory_allocated()
                if gpu and selected else None), no_seed=not selected,
            pose_elapsed_s=0, pose_frames=len(rows), pose_recomputed=False,
            original_pose_elapsed_s=read(original / 'result.json')['pose_elapsed_s']))
        records.append(dict(case_id=cid, result_sha256=digest(folder / 'result.json'),
                            pose_sha256=digest(folder / 'pose.json')))
        print(json.dumps(dict(case_id=cid, frames=len(samples), seconds=round(elapsed, 3),
            active=sum(s['bridge']['active'] is not None for s in samples))), flush=True)
    verify(plan)
    save(destination / 'completed.json', dict(complete=True, results=records, new_api_calls=0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--profile', choices=tuple(PROFILES), default='cuda-fp32')
    args = parser.parse_args()
    if args.command == 'prepare':
        require(args.baseline is not None, 'baseline required')
        prepare(args)
    else:
        run(args)


if __name__ == '__main__':
    main()
