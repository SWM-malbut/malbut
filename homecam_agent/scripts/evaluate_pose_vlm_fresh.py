#!/usr/bin/env python3
"""Fresh, bounded Gemma scene/first-Pose-request comparison on frozen clips.

This is NOT a 60/300-second continuous-monitoring or guardian-alert benchmark.
Scene evidence is shared in the paired OR comparison; it is called once, not
twice. A separate real call evaluates the first production Pose request prefix.
No answer, depth, identity, future frame, or uncalled normal label is invented.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import stat
import statistics
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO/'malbut_agent_server'))
from malbut_agent_server.adapters.outbound.ollama_cloud_fall import build_payload, parse_reply, NATIVE_BOX_FORMAT
from malbut_agent_server.application.cloud_fall_monitor import CloudFallMonitor
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.application.fall_detector_input import FallDetectorInput
from malbut_agent_server.domain.fall_monitoring import CloudFallReply, CloudFallRequest, FallRuntimePolicy, VideoAssessment
from paid_vlm.inputs import save
from paid_vlm.providers import MODELS, headers, normalize
from paid_vlm.runner import https_post
from paid_vlm.metrics import estimate_cost, money
from benchmark_fall_pose import camera_bgr

MODEL = MODELS['gemma4:31b']
RATE = dict(currency='USD', source='https://ollama.com/pricing', checked_on='2026-09-29',
            per_million_tokens=dict(input='.14', cached_input='.05', output='.40',
                                    cache_write_5m='0', cache_write_1h='0'))
RISK = {'observed_fall', 'suspected_fall'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Budget:
    """One shared budget; reserve before dispatch and retain unknown charges."""
    def __init__(self, maximum):
        self.maximum = money(maximum)
        if not 0 < self.maximum <= 1:
            raise ValueError('this experiment permits at most $1 total')
        self.known = Decimal(0)
        self.held = Decimal(0)
        self.reserve = Decimal('.05')
        self.blocked = False

    def acquire(self):
        if self.blocked or self.known + self.held + self.reserve > self.maximum:
            raise RuntimeError('budget_or_provider_blocked')
        self.held += self.reserve

    def settle(self, cost):
        if cost is not None:
            amount = money(cost)
            self.held -= self.reserve
            self.known += amount
            if amount > self.reserve:
                self.blocked = True

    def state(self):
        return dict(maximum_usd=str(self.maximum), known_usd=str(self.known),
                    held_unknown_usd=str(self.held), reserve_per_call_usd=str(self.reserve),
                    blocked=self.blocked)


async def prepare_case(dataset, case, trace_dir):
    import cv2
    cv2.setNumThreads(1)
    path = dataset/case['source_path']
    if sha(path) != case['sha256']:
        raise ValueError('media checksum mismatch')
    trace = json.loads((trace_dir/f"{case['case_id']}-0.json").read_text())
    if trace['errors']:
        raise ValueError('pose inference errors must be resolved before comparison')
    by_index = dict(zip(trace['raw']['inferred_frame_indices'], trace['candidate_messages']))
    if len(by_index) != len(trace['candidate_messages']):
        raise ValueError('pose trace alignment mismatch')
    now = [100.0]
    class RequestRecorder:
        execution_target = 'cloud'
        request = None
        async def analyze(self, request):
            self.request = request
            # Preparation only. This fixture is NEVER included in scoring.
            return CloudFallReply(VideoAssessment.UNOBSERVABLE, 'request preparation only')
    recorder = RequestRecorder()
    monitor = CloudFallMonitor(device_id='evaluation', boot_id='fresh-replay',
        policy=FallRuntimePolicy.agreed(retry_interval_s=3, max_person_observation_age_s=2,
            clip_window_s=5, max_frame_age_s=2, max_calls_per_minute=5,
            max_incidents=10, max_images=12),
        buffer=FallFrameBuffer(retention_s=10, max_frames=64, max_bytes=16*1024*1024),
        provider=recorder, clock=lambda: now[0])
    adapter = FallDetectorInput(monitor, max_source_age_s=2)
    adapter.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    cap = cv2.VideoCapture(str(path))
    last_rgb = -1.0
    any_candidate = False
    for index in range(case['frames']):
        ok, frame = cap.read()
        if not ok or frame.shape[:2] != (case['height'], case['width']):
            raise ValueError('unexpected video input')
        frame = camera_bgr(frame)
        sec, nano = divmod(round((100 + index/case['fps'])*1e9), 10**9)
        now[0] = sec + nano/1e9  # Match ROS image stamp, including rounding.
        if now[0]-last_rgb+1e-9 >= .2:
            ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY,90])
            if not ok:
                raise ValueError('JPEG encoding failed')
            adapter.rgb(encoded.tobytes(), capture=now[0], frame_id=case['case_id'],
                        source_now=now[0], now=now[0])
            last_rgb = now[0]
        if index in by_index:
            candidate_message = by_index[index]
            ids = adapter.candidates(json.dumps(candidate_message), source_now=now[0], now=now[0])
            any_candidate |= bool(candidate_message['candidates'])
            if ids and recorder.request is None:
                await monitor.run_once()
    cap.release()
    scene = CloudFallRequest(request_id='scene-'+case['case_id'], purpose='crosscheck',
        device_id='evaluation', boot_id='fresh-replay', incident_id=None, subject_key=None,
        evidence_revision=0, window=monitor.buffer.window(end=now[0], duration_s=5,
            max_images=12, max_age_s=2), sensors=None)
    await monitor.close()
    return scene, recorder.request, any_candidate


def request_metadata(request):
    return dict(purpose=request.purpose, target_supplied=request.target is not None,
                frame_count=len(request.window.frames),
                window_start=request.window.requested_start,
                window_end=request.window.requested_end,
                history_incomplete=request.window.history_incomplete,
                frame_times=[f.captured_at for f in request.window.frames],
                frame_sha256=[hashlib.sha256(f.jpeg).hexdigest() for f in request.window.frames])


async def execute_call(request, *, root, stem, key, budget):
    body = build_payload(request, model=MODEL.model, box_format=NATIVE_BOX_FORMAT)
    metadata = {**request_metadata(request), 'body_sha256': hashlib.sha256(body).hexdigest()}
    budget.acquire()
    # Durable reservation before the first network byte, no automatic resume.
    save(root/(stem+'-reservation.json'), {'budget': budget.state(), 'request': metadata})
    row = dict(request=metadata, assessment=None, localization_failed=None,
               cost_usd=None, usage=None, outcome='unknown', latency_s=None)
    started = time.monotonic()
    try:
        status, raw = await https_post(MODEL.endpoint, body, headers(MODEL,key), timeout_s=20)
        row['latency_s'] = time.monotonic()-started
        normalized = normalize(MODEL, status, raw)
        row['usage'] = normalized['usage']
        row['model_returned'] = normalized['model_returned']
        row['cost_usd'] = estimate_cost(normalized['usage'], RATE)
        row['http_status'] = status
        row['outcome'] = normalized['outcome']
        if status == 200:
            # Preserve the original response, never store request headers/keys.
            if key.encode() in raw:
                budget.blocked = True
                raise ValueError('unexpected credential in response')
            save(root/(stem+'-response.json'), json.loads(raw))
            reply = parse_reply(raw, request, box_format=NATIVE_BOX_FORMAT)
            row.update(assessment=reply.assessment.value, localization_failed=reply.localization_failed,
                       findings=len(reply.findings), outcome='classified')
        if status in (401,402,403,429):
            budget.blocked = True
    except asyncio.TimeoutError:
        row['outcome'] = 'timeout'
    except Exception as error:
        # Never copy arbitrary transport messages (may contain sensitive data).
        row['outcome'] = 'failed'
        row['error_type'] = type(error).__name__
    finally:
        row['elapsed_s'] = time.monotonic()-started
        budget.settle(row['cost_usd'])
        save(root/(stem+'-result.json'), {**row, 'budget': budget.state()})
    return row


def summary(rows, labels, budget):
    scene = [r for r in rows if r['purpose'] == 'crosscheck']
    incident = [r for r in rows if r['purpose'] == 'incident']
    def count(group, predicate):
        return sum(predicate(r) for r in group)
    report = dict(complete_scene=len(scene)==len(labels), planned_videos=len(labels),
        scene_calls=len(scene), first_pose_incident_calls=len(incident), budget=budget.state(),
        scene_correct=count(scene, lambda r: r['assessment']==labels[r['case_id']]),
        scene_outcomes=dict(Counter(r['outcome'] for r in scene)),
        localization_failed=count(scene, lambda r: r['localization_failed'] is True),
        scene_latency_median_s=statistics.median([r['latency_s'] for r in scene
            if r['latency_s'] is not None]) if any(r['latency_s'] is not None for r in scene) else None,
        first_pose_assessments=dict(Counter(r['assessment'] or r['outcome'] for r in incident)),
        limitations=['clip-level OR is not event merging, 60/300s scheduling, or final decision accuracy',
                     'first Pose prefix cannot be graded against a future full-clip event',
                     'unknown/uncalled/timeouts are not normal',
                     'only first Pose request per video, not all runtime/recheck calls',
                     'one synthetic-data run; no real robot, voice, depth or unseen holdout'])
    for category, label_set in [('risk', RISK), ('normal', {'normal_activity'})]:
        group = [r for r in scene if labels[r['case_id']] in label_set]
        report[category] = dict(evaluated=len(group),
            vlm_flag=count(group, lambda r: r['assessment'] in RISK),
            pose_flag=count(group, lambda r: r['pose_candidate']),
            either_flag=count(group, lambda r: r['pose_candidate'] or r['assessment'] in RISK),
            vlm_normal=count(group, lambda r: r['assessment']=='normal_activity'),
            vlm_unknown=count(group, lambda r: r['assessment'] not in RISK|{'normal_activity'}))
    return report


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--pose-traces', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--key-file', type=Path)
    p.add_argument('--budget-usd', default='1')
    p.add_argument('--execute', action='store_true')
    args = p.parse_args()
    if args.output.exists() or args.output.resolve().is_relative_to(REPO):
        p.error('use a NEW private artifact directory outside Git; no automatic resume')
    budget = Budget(args.budget_usd)
    args.output.mkdir(mode=0o700, parents=True)
    media = json.loads((args.dataset/'media.json').read_text())['cases']
    labels = {c['case_id']: c['label'] for c in json.loads(
        (args.dataset/'evaluation_labels.json').read_text())['classifications']['cases']}
    manifest = json.loads((args.pose_traces/'manifest.json').read_text())
    if (manifest['mode'] != 'fixed' or manifest.get('camera_input') != '640x400_aspect_preserved'
            or manifest['media_sha256'] != sha(args.dataset/'media.json')
            or set(manifest['cases']) != set(labels)):
        raise ValueError('need complete frozen fixed-input pose traces')
    save(args.output/'protocol.json', dict(runner_sha256=sha(__file__), model=MODEL.model, rate=RATE,
        authorized_maximum_usd=str(budget.maximum), source='user approved new test maximum $1',
        dataset_sha256=sha(args.dataset/'media.json'), labels_sha256=sha(args.dataset/'evaluation_labels.json'),
        pose_manifest=manifest, adapter_sha256=sha(REPO/'malbut_agent_server/malbut_agent_server/adapters/outbound/ollama_cloud_fall.py'),
        scope=__doc__, no_retries=True, response_deadline_s=20, no_key_in_dry_run=True))
    key = None
    if args.execute:
        if args.key_file is None or args.key_file.is_symlink():
            raise ValueError('private credential file required')
        info = args.key_file.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('credential must be owned by current user, mode 600')
        key = json.loads(args.key_file.read_text())[MODEL.key_env]
        headers(MODEL,key)
    rows = []
    for index, case in enumerate(media):
        scene, incident, pose_flag = await prepare_case(args.dataset, case, args.pose_traces)
        requests = [scene] + ([incident] if incident is not None else [])
        # Alternate provider order across videos; no wall-clock streaming claim.
        if index % 2:
            requests.reverse()
        plans = []
        for request in requests:
            body = build_payload(request, model=MODEL.model, box_format=NATIVE_BOX_FORMAT)
            plans.append({**request_metadata(request), 'body_bytes': len(body),
                          'body_sha256': hashlib.sha256(body).hexdigest()})
        save(args.output/(case['case_id']+'-plan.json'), dict(pose_candidate=pose_flag,
            requests=plans))
        if args.execute:
            for request in requests:
                try:
                    row = await execute_call(request, root=args.output,
                        stem=case['case_id']+'-'+request.purpose, key=key, budget=budget)
                except RuntimeError:
                    save(args.output/'summary.json', summary(rows,labels,budget))
                    print(json.dumps({'stopped': 'budget_or_provider_blocked', 'budget': budget.state()}), flush=True)
                    return
                rows.append({**row, 'case_id': case['case_id'], 'purpose': request.purpose,
                             'pose_candidate': pose_flag})
                print(json.dumps({'case_id': case['case_id'], 'purpose': request.purpose,
                    'assessment': row['assessment'], 'outcome': row['outcome'], 'budget': budget.state()}), flush=True)
    if args.execute:
        save(args.output/'summary.json', summary(rows,labels,budget))
        save(args.output/'scored_rows.json', rows)
    print(json.dumps({'prepared': len(media), 'executed': len(rows), 'budget': budget.state()}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
