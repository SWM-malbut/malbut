#!/usr/bin/env python3
"""Real-time JPEG playback + live GPU tracking + actual local incident core.

Pose is cached. Cloud is either an explicit early synthetic signal OR the
unchanged cached full-clip answer delivered only after its real elapsed time.
No network, ROS, audio, robot movement, or live deployment.
"""

import argparse
import asyncio
from collections import deque
from dataclasses import asdict
import json
import math
from pathlib import Path
import subprocess
import time

from experimental_streaming_sam import IncrementalSam, ReceivedFrame, StreamQueue
from evaluate_motion_subject_linking import code_hashes
from replay_reviewed_pose_cloud import (
    REPO, NoNetworkProvider, cached_reply, digest, event_metadata, frozen_jpegs,
    incident_metadata, read, require, save, stamp,
    CloudFallMonitor, CloudFallRequest, FallDetectorInput, FallFrameBuffer,
    FallRuntimePolicy, FrameWindow, RgbFrame, SqliteFallJournal,
    CloudFallReply, CloudPersonFinding, CloudPersonRegion, CandidateKind, VideoAssessment,
)


def response_due(case, record, response, mode):
    require(mode in {'cached_causal', 'early_fixture', 'late_delivery', 'camera_off'},
            'unknown playback mode')
    if mode == 'cached_causal':
        require(math.isfinite(response['elapsed_s']) and response['elapsed_s'] >= 0,
                'invalid cached response time')
        return max(record['evidence']['source_times_s']) + response['elapsed_s']
    if case['seed'] is None:
        return None
    return case['seed']['source_frame'] / case['meta']['fps'] + .75


async def play_case(case, folder, destination, predictor, config, mode):
    from PIL import Image

    rows = read(folder / 'pose.json')
    record, response = read(case['input']), read(case['response'])
    originals = frozen_jpegs(record, case['meta'])
    # Validate bytes before starting the clock; no model reads future images.
    for row in rows:
        require(digest(folder / 'images' / f'{row["source_frame"]:05d}.jpg') == row['jpeg_sha256'],
                'input image changed')
    origin = time.monotonic()
    clock = lambda: 100.0 + time.monotonic() - origin
    journal = SqliteFallJournal(destination / 'events.sqlite', device_id='stream-test', wall_clock=clock)
    monitor = CloudFallMonitor(
        device_id='stream-test', boot_id='stream', clock=clock, journal=journal,
        policy=FallRuntimePolicy.agreed(**config['policy']), provider=NoNetworkProvider(),
        buffer=FallFrameBuffer(retention_s=config['retention_s'], max_bytes=config['buffer_bytes'],
                               max_frames=config['buffer_frames']))
    adapter = FallDetectorInput(monitor, max_source_age_s=config['max_source_age_s'])
    adapter.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    history = deque(maxlen=64)
    queue = StreamQueue()
    delivered, events, samples = [], [], []
    producer_done, worker_started = asyncio.Event(), asyncio.Event()
    sid = None
    session = None
    worker_task = None
    camera_enabled = True
    discovery_received_at = None
    camera_off_at = None
    tracking_start_block = None
    seed_received = asyncio.Event()
    seed_frame = None
    max_dispatch_lag = 0.0
    linked_at = None

    def drain_events():
        events.extend(dict(received_at=clock(), **e) for e in event_metadata(monitor.drain_events()))

    async def producer():
        nonlocal seed_frame, max_dispatch_lag
        try:
            for row in rows:
                captured = row['captured_at']
                await asyncio.sleep(max(0, captured - clock()))
                if not camera_enabled:
                    continue
                now = clock()
                max_dispatch_lag = max(max_dispatch_lag, now - captured)
                jpeg = (folder / 'images' / f'{row["source_frame"]:05d}.jpg').read_bytes()
                adapter.rgb(jpeg, capture=captured, frame_id='offline_rgb', source_now=now, now=now)
                now = clock()
                adapter.candidates(json.dumps(row['candidate_payload']), source_now=now, now=now)
                # Use the exact timestamp conversion shared by RGB and Pose.
                converted = adapter._capture_times[('offline_rgb', captured)]
                frame = ReceivedFrame(row['source_frame'], converted, jpeg)
                history.append(frame)
                delivered.append(dict(source_frame=row['source_frame'], captured_at=converted,
                                      delivered_at=clock(), jpeg_sha256=row['jpeg_sha256']))
                if case['seed'] and frame.source_frame == case['seed']['source_frame']:
                    seed_frame = frame
                    seed_received.set()
                if sid is not None:
                    queue.put(frame, now=clock())
                drain_events()
        finally:
            producer_done.set()

    async def worker():
        nonlocal linked_at
        hold_used = False
        try:
            while not queue.closed:
                frame = queue.pop()
                if frame is None:
                    if producer_done.is_set():
                        break
                    await asyncio.sleep(.005)
                    continue
                worker_started.set()
                started = clock()
                outcome = await asyncio.to_thread(session.step, frame)
                ready_at = clock()
                if mode == 'late_delivery' and not hold_used:
                    hold_used = True
                    # Hold the first completed GPU output while RGB keeps arriving.
                    # The source clip ends; do not repeat its last frame as new input.
                    await producer_done.wait()
                    await asyncio.sleep(2.2)
                received_at = clock()
                # The monitor's own epoch/freshness gates must reject stopped or
                # stale sessions. No timestamp is rewritten to receipt time.
                decision = monitor.ingest_discovery_track(
                    sid, observed_at=frame.observed_at,
                    box=tuple(outcome['box']) if outcome['box'] is not None else None)
                mask_path = destination / 'masks' / f'{frame.source_frame:05d}.png'
                Image.fromarray(outcome.pop('mask').astype('uint8') * 255).save(mask_path)
                samples.append(dict(**outcome, started_at=started, gpu_ready_at=ready_at,
                    delivered_at=received_at, age_s=received_at - frame.observed_at,
                    mask_sha256=digest(mask_path), decision=asdict(decision),
                    canceled=queue.closed is not None))
                if decision.reason == 'matched_after_tracking':
                    linked_at = received_at
                drain_events()
                if decision.reason == 'visual_track_broken':
                    queue.stop('visual_track_broken')
        except Exception as error:
            queue.stop('worker_error')
            raise RuntimeError(f'stream worker failed: {type(error).__name__}: {error}') from error
        finally:
            session.close()

    async def response_task():
        nonlocal sid, session, worker_task, discovery_received_at, tracking_start_block
        due = response_due(case, record, response, mode)
        if due is None:
            return
        if mode == 'cached_causal':
            dispatch = max(record['evidence']['source_times_s'])
            await asyncio.sleep(max(0, 100 + dispatch - clock()))
            frames = tuple(RgbFrame(stamp(i, case['meta']['fps']), jpeg)
                           for i, jpeg in sorted(originals.items()))
            window = FrameWindow(frames, max(0, frames[-1].captured_at - 5),
                                 frames[-1].captured_at, False)
            reply = cached_reply(response)
        else:
            await seed_received.wait()
            window = FrameWindow((RgbFrame(seed_frame.observed_at, seed_frame.jpeg),),
                                 seed_frame.observed_at, seed_frame.observed_at, True)
            # Controlled discovery input, NOT the full-clip model prediction.
            reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL,
                'Synthetic early discovery signal for streaming plumbing, not accuracy evaluation',
                (CloudPersonFinding(VideoAssessment.SUSPECTED_FALL, CandidateKind.UNKNOWN,
                    (CloudPersonRegion(0, tuple(case['seed']['box'])),)),))
        request = CloudFallRequest('stream-' + response['case_id'], 'crosscheck', 'stream-test',
                                   'stream', None, None, 0, window, None)
        snapshot, versions = monitor._subject_evidence.snapshot(window), monitor._scene_incident_versions()
        await asyncio.sleep(max(0, 100 + due - clock()))
        discovery_received_at = clock()
        monitor._record_crosscheck(request, reply, snapshot, versions)
        discoveries = [e.discovery for e in monitor._events if e.discovery]
        drain_events()
        if not discoveries or discoveries[0].subject_key is not None:
            return
        require(len(discoveries) == 1, 'one-discovery stream experiment')
        try:
            sid = monitor.begin_discovery_tracking(discoveries[0].discovery_id)
        except ValueError as error:
            tracking_start_block = str(error)
            return
        first_region = discoveries[0].finding.regions[0]
        seed_time = discoveries[0].sample_times[first_region.frame_index]
        session = IncrementalSam(predictor, seed_box=first_region.box, seed_time=seed_time)
        seed_history = [f for f in history if f.observed_at >= seed_time]
        require(seed_history and seed_history[0].observed_at == seed_time, 'seed history unavailable')
        for frame in seed_history:
            queue.put(frame, now=clock())
        worker_task = asyncio.create_task(worker())

    async def switch_camera_off():
        nonlocal camera_enabled, camera_off_at, seed_frame
        await worker_started.wait()
        camera_enabled = False
        camera_off_at = clock()
        adapter.configure(enabled=True, camera_enabled=False, cloud_consent=True, connected=True)
        queue.stop('camera_off')
        history.clear()
        seed_frame = None
        require(monitor.buffer.stored_bytes == queue.bytes == 0, 'camera-off buffer not cleared')
        drain_events()

    producer_task = asyncio.create_task(producer())
    cloud_task = asyncio.create_task(response_task())
    off_task = asyncio.create_task(switch_camera_off()) if mode == 'camera_off' else None
    try:
        await asyncio.gather(producer_task, cloud_task)
        if worker_task:
            await worker_task
        if off_task:
            await off_task
        drain_events()
        persisted, unresolved = journal.discoveries(), journal.unresolved()
        after = incident_metadata(monitor)
    finally:
        tasks = [task for task in (producer_task, cloud_task, off_task) if task is not None]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if worker_task is not None and not worker_task.done():
            queue.stop('experiment_exit')
            await worker_task  # Do not free SAM state while GPU work is in flight.
        journal.close()
    reopened = SqliteFallJournal(destination / 'events.sqlite', device_id='stream-test', wall_clock=clock)
    require(reopened.discoveries() == persisted and reopened.unresolved() == unresolved,
            'journal reopen mismatch')
    reopened.close()
    result = dict(case_id=response['case_id'], mode=mode, samples=samples,
        delivered=delivered, events=events, after=after, linked_at=linked_at,
        discovery_received_at=discovery_received_at, camera_off_at=camera_off_at,
        tracking_start_block=tracking_start_block,
        final_buffer_bytes=monitor.buffer.stored_bytes, final_queue_bytes=queue.bytes,
        final_history_frames=len(history), sam_state_released=session is None or session.state is None,
        max_dispatch_lag_s=max_dispatch_lag, queue_peak_frames=queue.peak_frames,
        queue_closed=queue.closed, journal_reopen_verified=True, new_api_calls=0,
        camera_playback=True, pose_cached=True, elapsed_real_time_s=clock() - 100,
        accuracy_evaluation=False, cloud_synthetic=mode != 'cached_causal')
    save(destination / 'result.json', result)
    return dict(case_id=response['case_id'], mode=mode, gpu_frames=len(samples),
        links=sum(e['kind'] == 'cloud_discovery_linked' for e in events),
        linked_at=linked_at, discovery_received_at=discovery_received_at,
        final_reason=samples[-1]['decision']['reason'] if samples else tracking_start_block or 'no_tracking',
        max_dispatch_lag_s=max_dispatch_lag, elapsed_real_time_s=result['elapsed_real_time_s'])


async def run(args):
    import torch
    from sam2.build_sam import build_sam2_video_predictor
    require(not args.output.exists(), 'preserve previous outputs')
    plan = read(args.cached / 'plan.json')
    done = read(args.cached / 'run/completed.json')
    require(done['complete'], 'inference incomplete')
    require(torch.cuda.is_available() and torch.cuda.is_bf16_supported(), 'CUDA BF16 required')
    sources = dict(plan['sources'])
    for entry in done['results']:
        cid = entry['case_id']
        path = args.cached / 'run' / cid / 'pose.json'
        require(digest(path) == entry['pose_sha256'], 'Pose changed')
        sources[str(path)] = digest(path)
    for p, h in sources.items():
        require(digest(p) == h, 'source changed: ' + p)
    args.output.mkdir(mode=0o700, parents=True)
    code = code_hashes()
    config_path = REPO / 'malbut_agent_server/config/fall_runtime.example.json'
    config = read(config_path)
    sources[str(config_path)] = digest(config_path)
    jobs = [(cid, 'early_fixture') for cid in plan['cases']]
    jobs += [(cid, 'cached_causal') for cid in plan['cases']]
    jobs += [('SYN059', mode) for mode in ('late_delivery', 'camera_off')]
    if args.mode:
        jobs = [(cid, mode) for cid, mode in jobs if mode == args.mode]
    save(args.output / 'plan.json', dict(sources=sources, code=code, jobs=jobs,
        early_delay_s=.75, late_hold_after_eof_s=2.2, device='RTX 3070 BF16',
        pose_cached=True, max_session_frames=64, production_changed=False,
        accuracy_evaluation=False, causal_cached_reply=True, new_api_calls=0))
    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    predictor = build_sam2_video_predictor('configs/sam2.1/sam2.1_hiera_t.yaml',
                                          plan['checkpoint'], device='cuda')
    predictor.fill_hole_area = 0
    save(args.output / 'environment.json', dict(
        torch=torch.__version__, cuda=torch.version.cuda, gpu=torch.cuda.get_device_name(),
        sam_commit=subprocess.check_output(['git', '-C', plan['sam_source'], 'rev-parse', 'HEAD'],
                                           text=True).strip(),
        sam_source=plan['sam_source'], checkpoint=plan['checkpoint'], threads=4,
        dtype='bfloat16 autocast', tf32=False, video_offload=True, state_offload=True,
        private_incremental_adapter=True, full_video_length_given_to_model=False))
    summary = []
    for cid, mode in jobs:
        destination = args.output / mode / cid
        (destination / 'masks').mkdir(mode=0o700, parents=True)
        destination.chmod(0o700)
        value = await play_case(plan['cases'][cid], args.cached / 'run' / cid,
                                destination, predictor, config, mode)
        summary.append(value)
        print(json.dumps(value), flush=True)
    for p, h in {**sources, **code}.items():
        require(digest(p) == h, 'source changed during execution: ' + p)
    save(args.output / 'summary.json', summary)
    save(args.output / 'completed.json', dict(complete=True, new_api_calls=0,
        files={str(p.relative_to(args.output)): digest(p)
               for p in args.output.rglob('*') if p.is_file()}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cached', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--mode', choices=('early_fixture', 'cached_causal', 'late_delivery', 'camera_off'))
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
