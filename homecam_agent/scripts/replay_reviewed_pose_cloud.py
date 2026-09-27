#!/usr/bin/env python3
"""Offline, aligned Pose -> subject association -> cached crosscheck merge replay.

This is a component-boundary replay, NOT an end-to-end scheduler/ROS test.
The original Cloud JPEGs define mandatory samples; no answer or GT chooses
Pose samples. Additional real source frames fill gaps without exceeding 5 Hz.
No network provider exists. GT is loaded only after inference and merge finish.
"""
import argparse
import base64
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / 'homecam_agent/homecam_detector'),
                str(REPO / 'malbut_fall_coordinator'),
                str(REPO / 'malbut_agent_server')]

from homecam_detector.fall_candidate import FallCandidateConfig, FallCandidateDetector
from homecam_detector.pose import PersonPoseEstimator
from homecam_detector.pose_tracker import PersonPoseTracker
from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.application.cloud_fall_monitor import CloudFallMonitor
from malbut_agent_server.application.fall_cloud_association import associate_finding, box_iou
from malbut_agent_server.application.fall_detector_input import FallDetectorInput
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, CloudFallReply, CloudFallRequest, CloudPersonFinding,
    CloudPersonRegion, FallRuntimePolicy, FrameWindow, RgbFrame, VideoAssessment,
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_bytes())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    path.chmod(0o600)


def aligned_schedule(mandatory, fps, max_fps=5):
    """All original inputs plus evenly spaced *real* intermediate frames.

    Rate is a minimum inter-sample interval, not an average. No prehistory is
    manufactured. Include source frame zero only when its distance permits it.
    """
    require(math.isfinite(fps) and fps > 0 and max_fps > 0, 'invalid sampling rate')
    require(mandatory and all(type(i) is int and i >= 0 for i in mandatory), 'invalid frame index')
    require(all(b > a for a, b in zip(mandatory, mandatory[1:])), 'unordered input')
    minimum = math.ceil(fps / max_fps - 1e-10)
    require(all(b-a >= minimum for a, b in zip(mandatory, mandatory[1:])),
            'Cloud samples cannot be aligned within the Pose rate limit')
    anchors = ([0] if mandatory[0] >= minimum else []) + list(mandatory)
    result = [anchors[0]]
    for a, b in zip(anchors, anchors[1:]):
        steps = (b-a) // minimum
        result.extend(a + round(i*(b-a)/steps) for i in range(1, steps+1))
    require(set(mandatory) <= set(result), 'lost mandatory sample')
    require(all((b-a)/fps >= 1/max_fps-1e-10 for a, b in zip(result, result[1:])), 'sampling too fast')
    return result


def stamp(frame_index, fps):
    # Same quantized source stamp for RGB and Pose, on a virtual clock.
    return 100 + round(frame_index / fps * 1e9) / 1e9


def frozen_jpegs(record, meta):
    evidence = record['evidence']
    indices = evidence['frame_indices']
    require(evidence['source_sha256'] == meta['sha256'], 'source hash mismatch')
    require(evidence['dimensions'] == [640, 400], 'unexpected image dimensions')
    require(len(indices) == len(set(indices)) == len(record['common']['images'])
            == len(evidence['jpeg_sha256']) == len(evidence['source_times_s']), 'input mapping mismatch')
    require(all(type(i) is int and 0 <= i < meta['frames'] for i in indices), 'input frame out of range')
    require(all(abs(i / meta['fps'] - t) < 1e-9
                for i, t in zip(indices, evidence['source_times_s'])), 'input time mismatch')
    result = {}
    for i, encoded, expected in zip(indices, record['common']['images'], evidence['jpeg_sha256']):
        jpeg = base64.b64decode(encoded, validate=True)
        require(hashlib.sha256(jpeg).hexdigest() == expected, 'JPEG hash mismatch')
        result[i] = jpeg
    return result


def intermediate_jpeg(bgr):
    import cv2
    import numpy as np
    height, width = bgr.shape[:2]
    scale = min(640/width, 400/height)
    rw, rh = round(width*scale), round(height*scale)
    resized = cv2.resize(bgr, (rw, rh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    canvas = np.zeros((400, 640, 3), dtype=np.uint8)
    dx, dy = (640-rw)//2, (400-rh)//2
    canvas[dy:dy+rh, dx:dx+rw] = resized
    ok, encoded = cv2.imencode('.jpg', canvas, [cv2.IMWRITE_JPEG_QUALITY, 90])
    require(ok, 'JPEG encoding failed')
    return encoded.tobytes()


def infer_scene(estimator, source, meta, record, destination):
    """Only source pixels and neutral input timing enter inference/tracking."""
    import cv2
    import numpy as np
    require(digest(source) == meta['sha256'], 'video changed')
    originals = frozen_jpegs(record, meta)
    schedule = aligned_schedule(list(originals), meta['fps'])
    tracker = PersonPoseTracker()
    detector = FallCandidateDetector()
    cap = cv2.VideoCapture(str(source))
    require(cap.isOpened(), 'cannot open video')
    rows = []
    try:
        for index in range(schedule[-1]+1):
            ok, bgr = cap.read()
            require(ok, f'missing source frame {index}')
            if index not in schedule:
                continue
            jpeg = originals[index] if index in originals else intermediate_jpeg(bgr)
            pixels = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            require(pixels is not None and pixels.shape == (400, 640, 3), 'invalid decoded image')
            started = time.monotonic()
            poses = estimator.estimate_all(pixels, confidence_threshold=.10)
            elapsed = time.monotonic() - started
            now = stamp(index, meta['fps'])
            tracked = tracker.update(poses, now=now)
            payload = detector.update(tracked, capture_time=now, image_size=(640,400), robot_motion='unknown')
            payload.update(frameId='offline_rgb', expiredTrackIds=list(tracked.expired_track_ids))
            row = dict(source_frame=index, captured_at=now, cloud_input=index in originals,
                       jpeg_sha256=hashlib.sha256(jpeg).hexdigest(), inference_s=elapsed,
                       poses=[p.as_dict() for p in poses], tracks=[dict(
                           track_id=t.track_id, state=t.state, confidence=t.confidence_level,
                           observations=t.observation_count, consecutive=t.consecutive_observations,
                           pose=t.pose.as_dict() if t.pose else None) for t in tracked.tracks],
                       unassigned=[dict(reason=u.reason, pose=u.pose.as_dict()) for u in tracked.unassigned],
                       candidate_payload=payload)
            rows.append(row)
    finally:
        cap.release()
    save(destination, rows)
    return rows, originals


class NoNetworkProvider:
    execution_target = 'cloud'

    async def analyze(self, request):
        raise AssertionError('This replay must not call any provider or scheduler')


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


def cached_reply(row):
    normalized = row.get('normalized_response')
    if row['outcome'] != 'classified':
        require(normalized is None, 'failed response was salvaged')
        return None
    require(normalized is not None, 'classified response missing normalized fields')
    return CloudFallReply(VideoAssessment(normalized['assessment']), normalized['explanation'], tuple(
        CloudPersonFinding(VideoAssessment(f['assessment']), CandidateKind(f['kind']), tuple(
            CloudPersonRegion(r['frame_index'], tuple(r['box'])) for r in f['regions']))
        for f in normalized['findings']))


def incident_metadata(monitor):
    return [dict(incident_id=i.incident_id, subject_key=i.subject_key, state=i.state.value,
                 revision=i.revision, attempts=i.attempts, rechecks=i.rechecks, pending=i.pending,
                 subject_association_token=i.subject_association_token,
                 sources=i.candidate_sources, question_id=i.question_id)
            for i in monitor._incidents.values()]


def event_metadata(events):
    return [dict(kind=e.kind, incident_id=e.incident_id, subject_key=e.subject_key,
                 confirmation_scope=e.confirmation_scope, question_id=e.question_id,
                 evidence_revision=e.evidence_revision,
                 video_assessment=e.reply.assessment.value if e.reply else None,
                 reason=e.reason, discovery=e.discovery.metadata() if e.discovery else None)
            for e in events]


def replay_scene(rows, originals, record, result, config, journal_path):
    """Feed actual candidate JSON; inject a saved reply only at merge boundary.

    Do not run the priority scheduler: its incident inputs differ from the saved
    crosscheck request. Do not fabricate responses for those pending incidents.
    No network/Cloud delay is simulated; latest-target freshness is instantaneous.
    """
    clock = Clock()
    journal = SqliteFallJournal(journal_path, device_id='offline-replay', wall_clock=clock)
    monitor = CloudFallMonitor(device_id='offline-replay', boot_id='offline',
        policy=FallRuntimePolicy.agreed(**config['policy']),
        buffer=FallFrameBuffer(retention_s=config['retention_s'], max_bytes=config['buffer_bytes'],
                               max_frames=config['buffer_frames']),
        provider=NoNetworkProvider(), journal=journal, clock=clock)
    adapter = FallDetectorInput(monitor, max_source_age_s=config['max_source_age_s'])
    adapter.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    timeline = []
    for row in rows:
        clock.value = row['captured_at']
        if row['source_frame'] in originals:
            adapter.rgb(originals[row['source_frame']], capture=clock.value, frame_id='offline_rgb',
                        source_now=clock.value, now=clock.value)
        ids = adapter.candidates(json.dumps(row['candidate_payload']), source_now=clock.value, now=clock.value)
        timeline.append(dict(source_frame=row['source_frame'], incidents=list(ids), subjects=[dict(
            subject_key='pose:0:'+t['targetTrackId'], token=monitor._subject_evidence.token_at(
                'pose:0:'+t['targetTrackId'], clock.value), box=t['box'], usable=t['associationUsable'],
            state=t['trackingState'], feature_usable=(t['features'] or {}).get('usable'))
            for t in row['candidate_payload']['tracks']]))
    # Only the original 12 frozen Cloud JPEGs were supplied to the RGB buffer.
    # Additional samples are Pose-only, not silently substituted Cloud evidence.
    window = monitor.buffer.window(end=clock.value, duration_s=config['policy']['clip_window_s'],
                                   max_images=12, max_age_s=config['policy']['max_frame_age_s'])
    expected = record['evidence']['jpeg_sha256']
    require([hashlib.sha256(f.jpeg).hexdigest() for f in window.frames] == expected,
            'buffer no longer reproduces frozen Cloud inputs')
    snapshot = monitor._subject_evidence.snapshot(window)
    require(len(snapshot) == len(originals), 'missing snapshot sample')
    snapshot_rows = [[dict(key=k, token=t, box=p.box, usable=p.association_usable) for k,t,p in people]
                     for people in snapshot]
    before = incident_metadata(monitor)
    pose_events = event_metadata(monitor.drain_events())
    reply = cached_reply(result)
    findings = []
    repeated = []
    if reply is not None:
        require(all(r.frame_index < len(window.frames) for f in reply.findings for r in f.regions),
                'cached reply uses absent frame')
        for finding in reply.findings:
            comparisons = []
            for region in finding.regions:
                ranked = sorted([dict(subject_key=k, token=t, pose_box=p.box,
                    iou=box_iou(region.box,p.box)) for k,t,p in snapshot[region.frame_index]
                    if p.box is not None], key=lambda p:p['iou'], reverse=True)
                comparisons.append(dict(frame_index=region.frame_index,
                    source_frame=record['evidence']['frame_indices'][region.frame_index],
                    cloud_box=region.box, ranked=ranked))
            findings.append(dict(association=asdict(associate_finding(finding,snapshot)), regions=comparisons))
        request = CloudFallRequest('cached-'+result['case_id'], 'crosscheck', 'offline-replay', 'offline',
                                  None, None, 0, window, None)
        monitor._record_crosscheck(request, reply, snapshot, monitor._scene_incident_versions())
        events = event_metadata(monitor.drain_events())
        after = incident_metadata(monitor)
        # Same saved response delivered twice is a merge idempotence probe, NOT
        # another periodic scan or another API call. Keep it outside headline counts.
        monitor._record_crosscheck(request, reply, snapshot, monitor._scene_incident_versions())
        repeated = event_metadata(monitor.drain_events())
        repeated_after = incident_metadata(monitor)
        require(after == repeated_after, 'repeat changed incident identity/state/budget')
    else:
        events, after, repeated_after = [], before, before
    persisted = journal.discoveries()
    unresolved = journal.unresolved()
    journal.close()
    reopened = SqliteFallJournal(journal_path, device_id='offline-replay', wall_clock=clock)
    require(reopened.discoveries() == persisted and reopened.unresolved() == unresolved,
            'journal changed after reopen')
    reopened.close()
    return dict(case_id=result['case_id'], outcome=result['outcome'], reply_usable=reply is not None,
        issues=result.get('response_issue_codes'), pose_timeline=timeline, snapshot=snapshot_rows,
        pose_events=pose_events, before=before, findings=findings, events=events, after=after,
        repeat_events=repeated, repeat_incidents_unchanged=after==repeated_after,
        journal_reopen_verified=True, persisted_discoveries=len(persisted),
        extra_provider_calls=0, gt_used_by_runtime=False,
        scope='forced cached crosscheck merge boundary; no scheduler, transport latency, ROS or notifications')


def score_tracks(replays, annotations, metas, criteria):
    # Import/load GT only after all Pose inference and all merges are complete.
    from audit_paid_vlm_localization import exact_gt, overlap
    from score_fall_baseline import match_boxes
    scored = {}
    for cid, timeline in replays.items():
        case = annotations[cid]
        output = []
        for frame in timeline:
            gt = exact_gt(case, frame['source_frame'], metas[cid])
            if not gt:
                continue
            subjects = [s for s in frame['subjects'] if s['box'] is not None]
            boxes = [[v*(640 if i%2==0 else 400) for i,v in enumerate(s['box'])] for s in subjects]
            matched = match_boxes([g['box'] for g in gt], boxes, criteria)
            people = []
            for gi, (status, pi) in enumerate(matched):
                people.append(dict(person_id=gt[gi]['person_id'], role=gt[gi]['role'], status=status,
                    subject=subjects[pi] if status=='matched' else None,
                    comparisons=[dict(subject_key=s['subject_key'], **overlap(gt[gi]['box'],box))
                                 for s,box in zip(subjects,boxes)]))
            output.append(dict(source_frame=frame['source_frame'], people=people))
        scored[cid] = output
    return scored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--spatial', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', default=['SYN012','SYN045','SYN063','SYN067'])
    args = parser.parse_args()
    require(not args.output.exists(), 'output already exists; preserve prior run')
    args.output.mkdir(mode=0o700, parents=True)
    audit = read(args.audit/'cases.json')
    metas = {m['case_id']:m for m in read(args.spatial/'media.json')['cases']}
    config_path = REPO/'malbut_agent_server/config/fall_runtime.example.json'
    config = read(config_path)
    jobs = {}
    inputs = {}
    sources = {p: digest(p) for p in [args.audit/'cases.json', args.model, config_path,
                                    args.spatial/'freeze.json', args.spatial/'media.json']}
    for model, cases in audit.items():
        for case in cases:
            cid = case['case_id']
            if cid not in args.cases:
                continue
            path = Path(case['result_path'])
            input_path = path.parent.parent/'inputs'/f'{cid}.input.json'
            result, record = read(path), read(input_path)
            require(result['case_id'] == cid, 'case binding changed')
            require(record['evidence']['source_sha256'] == metas[cid]['sha256'], 'wrong video')
            if cid in inputs:
                require(record == inputs[cid], 'models used different input')
            inputs[cid] = record
            jobs[model,cid] = result
            sources.update({p:digest(p) for p in [path,input_path]})
    require(set(inputs) == set(args.cases), 'missing case')
    require(len(jobs) == len(audit)*len(args.cases), 'missing model/case result')
    for root in [REPO/'homecam_agent/homecam_detector/homecam_detector',
                 REPO/'malbut_agent_server/malbut_agent_server']:
        sources.update({p:digest(p) for p in root.rglob('*.py')})
    sources[Path(__file__)] = digest(__file__)
    save(args.output/'plan.json',dict(cases=args.cases,
        schedules={c:aligned_schedule(inputs[c]['evidence']['frame_indices'],metas[c]['fps']) for c in args.cases},
        policy=config['policy'], pose_config=asdict(FallCandidateConfig()), max_pose_fps=5,
        no_network=True, ground_truth_only_posthoc=True,
        frozen_cloud_jpegs=True, motion='unknown', depth=None,
        artificial_dispatch=True, cloud_delay_not_simulated=True,
        production_rgb_sampling_not_reproduced=True))
    estimator = PersonPoseEstimator(model_path=str(args.model), confidence_threshold=.45,
                                    keypoint_threshold=.5, input_size=640, keep_aspect=True)
    traces, jpegs = {}, {}
    for cid in args.cases:
        source = args.dataset/metas[cid]['source_path']
        sources[source] = digest(source)
        print(f'Pose {cid}: {len(aligned_schedule(inputs[cid]["evidence"]["frame_indices"],metas[cid]["fps"]))} frames',flush=True)
        traces[cid], jpegs[cid] = infer_scene(estimator,source,metas[cid],inputs[cid],args.output/f'{cid}.pose.json')
    results = []
    for (model,cid), result in jobs.items():
        replay = replay_scene(traces[cid],jpegs[cid],inputs[cid],result,config,args.output/f'{model}-{cid}.sqlite')
        replay['model'] = model
        save(args.output/f'{model}-{cid}.replay.json',replay)
        results.append(replay)
    # Scoring cannot affect inference, continuity tokens, or merge outcomes.
    freeze = read(args.spatial/'freeze.json')
    for name, expected in freeze['files'].items():
        p=args.spatial/name
        require(digest(p)==expected, 'approved overlay changed')
        sources[p]=expected
    annotation_path=args.spatial/'evaluation_labels.json'
    annotations={c['case_id']:c for c in read(annotation_path)['annotations']['cases']}
    scored=score_tracks({r['case_id']:r['pose_timeline'] for r in results},annotations,metas,freeze['match'])
    save(args.output/'gt-track-score.json',scored)
    summary=[]
    for r in results:
        ds=[e['discovery'] for e in r['events'] if e['discovery']]
        summary.append(dict(model=r['model'],case_id=r['case_id'],reply_usable=r['reply_usable'],
            reasons=dict(Counter(d['reason'] for d in ds)),
            linked=sum(d['incident_id'] is not None and d['subject_key'] is not None for d in ds),
            before_incidents=len(r['before']),after_incidents=len(r['after']),
            repeated_incidents_unchanged=r['repeat_incidents_unchanged'],journal_reopen_verified=True))
    save(args.output/'summary.json',summary)
    require(all(digest(p)==expected for p,expected in sources.items()), 'source changed during replay')
    import cv2, numpy, onnxruntime
    save(args.output/'provenance.json',dict(sources={str(p):h for p,h in sources.items()},
        python=sys.version,platform=platform.platform(),cv2=cv2.__version__,numpy=numpy.__version__,
        onnxruntime=onnxruntime.__version__,new_api_calls=0,
        inference_frames=sum(len(rows) for rows in traces.values()),ground_truth_used_for_inference=False))
    save(args.output/'completed.json',dict(new_api_calls=0,source_hashes_unchanged=True,
        files={p.name:digest(p) for p in sorted(args.output.iterdir()) if p.is_file()}))
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__':
    main()
