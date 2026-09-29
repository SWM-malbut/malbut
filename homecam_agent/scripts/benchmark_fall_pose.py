#!/usr/bin/env python3
"""Replay frozen RGB clips through the production fall-only image callback.

No ROS graph, camera, network, speech or robot commands are created. `fixed`
uses capture time for the rate gate (semantic comparison, not realtime FPS).
`realtime` uses wall time and delivers only the newest due frame after a slow
callback; this is an explicit simulator policy, not a measurement of DDS.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from types import MethodType, SimpleNamespace


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def camera_bgr(frame):
    """Freeze a robot-sized 640x400 input; preserve aspect with black padding."""
    import cv2
    import numpy as np
    h, w = frame.shape[:2]
    if (h, w) == (400, 640):
        return frame
    scale = min(640/w, 400/h)
    resized = cv2.resize(frame, (round(w*scale), round(h*scale)),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    canvas = np.zeros((400,640,3), dtype=np.uint8)
    y, x = (400-resized.shape[0])//2, (640-resized.shape[1])//2
    canvas[y:y+resized.shape[0], x:x+resized.shape[1]] = resized
    return canvas


def stats(values):
    ordered = sorted(values)
    return {"n": len(ordered), "median": statistics.median(ordered),
            "p95": ordered[min(len(ordered)-1, int(.95*len(ordered)))],
            "max": max(ordered)} if ordered else {"n": 0}


def cpu_interval(previous, current):
    """Process CPU delta / wall delta; one logical core is 100%, not a cap."""
    wall_s = current[0] - previous[0]
    cpu_s = current[1] - previous[1]
    if wall_s <= 0 or cpu_s < 0:
        raise ValueError('resource clocks must advance monotonically')
    return 100 * cpu_s / wall_s


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=['fixed', 'realtime'], default='realtime')
    parser.add_argument('--cases', default='balanced3', help='balanced3, all, or comma-separated IDs')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--provider', choices=['cpu', 'cuda'])
    parser.add_argument('--threads', type=int)
    parser.add_argument('--no-spinning', action='store_true')
    parser.add_argument('--opencv-threads', type=int)
    args = parser.parse_args()
    if args.repeats < 1 or args.output.exists():
        parser.error('positive repeats and a NEW output directory are required')
    sys.path.insert(0, str(args.source_root / 'homecam_agent/homecam_detector'))
    import cv2
    import onnxruntime as ort
    import psutil
    from cv_bridge import CvBridge
    import homecam_detector.detector_node as module
    from homecam_detector.config import DetectorConfig
    from homecam_detector.fall_candidate import FallCandidateDetector
    from homecam_detector.pose import PersonPoseEstimator, PersonPoseGate
    from homecam_detector.pose_tracker import PersonPoseTracker

    if args.provider == 'cuda':
        ort.preload_dlls()
    if args.opencv_threads is not None:
        cv2.setNumThreads(args.opencv_threads)
    media = json.loads((args.dataset / 'media.json').read_text())['cases']
    labels = {c['case_id']: c['label'] for c in json.loads(
        (args.dataset / 'evaluation_labels.json').read_text())['classifications']['cases']}
    if args.cases == 'balanced3':
        ids = [next(c['case_id'] for c in media if labels[c['case_id']] == label)
               for label in ['observed_fall', 'suspected_fall', 'normal_activity']]
    elif args.cases == 'all':
        ids = [c['case_id'] for c in media]
    else:
        ids = args.cases.split(',')
    selected = [c for c in media if c['case_id'] in ids]
    if len(selected) != len(ids):
        raise ValueError('unknown or duplicate case IDs')
    kwargs = {}
    if args.provider is not None:
        kwargs['execution_provider'] = args.provider
    if args.threads is not None:
        kwargs['intra_op_num_threads'] = args.threads
    if args.no_spinning:
        kwargs['allow_spinning'] = False
    started = time.perf_counter()
    estimator = PersonPoseEstimator(str(args.model), keep_aspect=True, **kwargs)
    load_s = time.perf_counter() - started
    session = estimator._session
    process = psutil.Process()
    bridge = CvBridge()
    args.output.mkdir(parents=True, mode=0o700)
    manifest = {
        'source_root': str(args.source_root), 'mode': args.mode,
        'runner_sha256': digest(__file__), 'camera_input': '640x400_aspect_preserved',
        'model_sha256': digest(args.model), 'media_sha256': digest(args.dataset/'media.json'),
        'source_sha256': {p: digest(args.source_root/'homecam_agent/homecam_detector/homecam_detector'/p)
                          for p in ['pose.py', 'detector_node.py', 'config.py']},
        'ort': ort.__version__, 'opencv': cv2.__version__, 'opencv_threads': cv2.getNumThreads(),
        'providers': session.get_providers(), 'options': kwargs, 'load_s': load_s,
        'cases': ids, 'repeats': args.repeats, 'encoding': 'rgb8',
        'scope': 'local callback replay, not ROS/DDS or Jetson; absent depth and odometry',
        'cpu_percent_definition': 'process CPU seconds / wall seconds * 100; 100% = one core',
        'gpu_scope': 'whole GPU, includes other processes',
        'resource_sample_interval_s': 0.5,
        'timeline_scope': 'elapsed replay time only; model load, warm-up and gaps excluded',
    }
    (args.output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    real_run = session.run
    real_estimate = estimator.estimate_all
    record = {}
    current_frame = [-1]

    def run(*a, **kw):
        begin = time.perf_counter()
        result = real_run(*a, **kw)
        record['inference_ms'].append((time.perf_counter()-begin)*1000)
        record['inferred_frame_indices'].append(current_frame[0])
        return result

    def estimate(*a, **kw):
        begin = time.perf_counter()
        result = real_estimate(*a, **kw)
        record['estimate_ms'].append((time.perf_counter()-begin)*1000)
        record['poses'].append({'frame_index': current_frame[0], 'poses': [asdict(p) for p in result]})
        return result

    def convert(*a, **kw):
        begin = time.perf_counter()
        result = bridge.imgmsg_to_cv2(*a, **kw)
        record['conversion_ms'].append((time.perf_counter()-begin)*1000)
        return result

    class Publisher:
        def __init__(self):
            self.messages = []
        def publish(self, message):
            self.messages.append(json.loads(message.data))

    errors = []
    logger = SimpleNamespace(error=errors.append, warning=errors.append, info=lambda _: None)
    results = []
    for case in selected:
        path = args.dataset / case['source_path']
        if digest(path) != case['sha256']:
            raise ValueError('frozen media checksum mismatch')
        cap = cv2.VideoCapture(str(path))
        messages, first = [], None
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame = camera_bgr(frame)
            if first is None:
                first = frame
            message = bridge.cv2_to_imgmsg(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), encoding='rgb8')
            ns = round((100 + len(messages)/case['fps'])*1e9)
            message.header.stamp.sec, message.header.stamp.nanosec = divmod(ns, 10**9)
            message.header.frame_id = case['case_id']
            messages.append(message)
        cap.release()
        if len(messages) != case['frames']:
            raise ValueError('decoded frame count mismatch')
        # Warm-up excluded, same five calls in each condition.
        session.run = real_run
        for _ in range(5):
            real_estimate(first)
        session.run = run
        estimator.estimate_all = estimate
        for repeat in range(args.repeats):
            record = {k: [] for k in ['inference_ms', 'estimate_ms', 'conversion_ms',
                                     'callback_ms', 'inferred_frame_indices', 'poses', 'frame_age_ms']}
            node = SimpleNamespace(
                _config=DetectorConfig(fall_only=True), _refresh_fall_control=lambda: True,
                _bridge=SimpleNamespace(imgmsg_to_cv2=convert), _pose_estimator=estimator,
                _pose_gate=PersonPoseGate(5), _pose_tracker=PersonPoseTracker(),
                _fall_detector=FallCandidateDetector(), _pose_frame_context=None,
                _last_pose_source_stamp=None, _pose_failure_count=0, _pose_present=False,
                _pose_publisher=Publisher(), _poses_publisher=Publisher(),
                _fall_candidates_publisher=Publisher(),
                _depth_evidence=lambda *a: {'usable': False, 'reason': 'benchmark_no_depth'},
                _motion_gate=SimpleNamespace(pose_motion_state=lambda _: 'unknown'),
                _stamp_seconds=module.HomecamDetectorNode._stamp_seconds, get_logger=lambda: logger)
            for name in ['_on_image', '_observe_person_pose', '_publish_pose_absent',
                         '_publish_tracked_poses', '_publish_fall_candidates']:
                setattr(node, name, MethodType(getattr(module.HomecamDetectorNode, name), node))
            virtual_now = [100.0]
            module.time = SimpleNamespace(monotonic=(lambda: virtual_now[0])
                if args.mode == 'fixed' else time.monotonic, time=time.time)
            stop = threading.Event()
            gpu, rss = [], []
            resource_samples = []
            begin, cpu_begin = time.perf_counter(), time.process_time()

            def sample():
                previous = (begin, cpu_begin)
                while not stop.wait(.5):
                    current = (time.perf_counter(), time.process_time())
                    memory = process.memory_info().rss
                    rss.append(memory)
                    point = {
                        'elapsed_s': current[0] - begin,
                        'interval_s': current[0] - previous[0],
                        'cpu_percent': cpu_interval(previous, current),
                        'rss_mib': memory / 2**20,
                        'gpu_percent': None,
                        'gpu_memory_mib': None,
                    }
                    previous = current
                    try:
                        output = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used',
                            '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=2)
                        if output.returncode == 0:
                            value = [float(x) for x in output.stdout.splitlines()[0].split(',')]
                            if len(value) != 2:
                                raise ValueError('invalid GPU sample')
                            gpu.append(value)
                            point['gpu_percent'], point['gpu_memory_mib'] = value
                    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                        pass
                    point['gpu_read_finished_s'] = time.perf_counter() - begin
                    resource_samples.append(point)
            thread = threading.Thread(target=sample, daemon=True)
            thread.start()
            index, dropped = 0, 0
            while index < len(messages):
                if args.mode == 'realtime':
                    due = begin + index/case['fps']
                    if due > time.perf_counter():
                        time.sleep(due-time.perf_counter())
                    newest = min(len(messages)-1, int((time.perf_counter()-begin)*case['fps']))
                    dropped += max(0, newest-index)
                    index = max(index, newest)
                    record['frame_age_ms'].append(max(0, (time.perf_counter()-begin-index/case['fps'])*1000))
                virtual_now[0] = 100 + index/case['fps']
                current_frame[0] = index
                tick = time.perf_counter()
                node._on_image(messages[index])
                record['callback_ms'].append((time.perf_counter()-tick)*1000)
                index += 1
            wall_s, cpu_s = time.perf_counter()-begin, time.process_time()-cpu_begin
            stop.set()
            thread.join(timeout=3)
            if thread.is_alive():
                raise RuntimeError('resource sampler did not stop')
            row = {'case_id': case['case_id'], 'repeat': repeat, 'wall_s': wall_s,
                   'cpu_percent': 100*cpu_s/wall_s, 'rss_max_mib': max(rss, default=0)/2**20,
                   'input_frames': len(messages), 'dropped_before_callback': dropped,
                   'callback_count': len(record['callback_ms']),
                   'conversion_count': len(record['conversion_ms']),
                   'inference_count': len(record['inference_ms']),
                   'processed_fps': len(record['inference_ms'])/wall_s if args.mode=='realtime' else None,
                   'gpu_percent': stats([v[0] for v in gpu]), 'gpu_memory_mib': stats([v[1] for v in gpu]),
                   'timing': {k: stats(v) for k,v in record.items() if k.endswith('_ms')},
                   'errors': list(errors)}
            stem = args.output / f"{case['case_id']}-{repeat}"
            stem.with_suffix('.json').write_text(json.dumps({**row, 'raw': record,
                'resource_samples': resource_samples,
                'person_messages': node._poses_publisher.messages,
                'candidate_messages': node._fall_candidates_publisher.messages}, indent=2)+'\n')
            results.append(row)
            (args.output/'summary.json').write_text(json.dumps(results, indent=2)+'\n')
            print(json.dumps(row), flush=True)
    session.run = real_run


if __name__ == '__main__':
    main()
