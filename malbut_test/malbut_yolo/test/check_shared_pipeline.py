#!/usr/bin/env python3
"""
Opt-in real-GPU topic integration probe, not a robot-following benchmark.

Publish the installed Ultralytics bus photo with synthetic pinhole intrinsics
and aligned synthetic depth: first 2 m, then invalid (NaN). This exercises actual
YOLO/OSNet inference and message connections, not camera calibration accuracy,
identity accuracy on real people, obstacle avoidance, or following performance.
No downloads, GUI, simulator, default ROS domain, or system configuration edits.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

import cv2
from cv_bridge import CvBridge
from malbut_interfaces.msg import SensorProcessingTrace
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, CompressedImage, Image
from vision_msgs.msg import Detection2DArray, Detection3DArray
from yolo_msgs.msg import DetectionArray


def stamp_key(stamp):
    """Use the exact source timestamp, never a nearest-time match."""
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


class PipelineProbe(Node):
    """Publish known sensor inputs and record contracts at every output stage."""

    def __init__(self, image):
        """Create only probe publishers/subscribers in the isolated domain."""
        super().__init__(f'shared_pipeline_probe_{os.getpid()}')
        self.bridge = CvBridge()
        self.image = image
        self.sent = {}
        self.yolo = {}
        self.reid = {}
        self.positions = {}
        self.debug = set()
        self.traces = {}
        self.rgb_pub = self.create_publisher(
            Image, '/camera/color/image_raw', qos_profile_sensor_data,
        )
        self.depth_pub = self.create_publisher(
            Image, '/camera/depth/image_raw', qos_profile_sensor_data,
        )
        self.info_pub = self.create_publisher(
            CameraInfo, '/camera/color/camera_info', qos_profile_sensor_data,
        )
        self.subscriptions_owned = [
            self.create_subscription(
                DetectionArray, '/yolo/detections', self._on_yolo, 10,
            ),
            self.create_subscription(
                Detection2DArray, '/perception/person/detections_2d',
                self._on_reid, 10,
            ),
            self.create_subscription(
                Detection3DArray, '/perception/person/detections_3d',
                self._on_positions, 10,
            ),
            self.create_subscription(
                CompressedImage, '/perception/person/debug_image/compressed',
                self._on_debug, qos_profile_sensor_data,
            ),
            self.create_subscription(
                SensorProcessingTrace, '/perception/sensor_processing_trace',
                self._on_trace, 100,
            ),
        ]

    def ready(self):
        """Wait for the actual three consumers of this probe's RGB stream."""
        return (
            self.rgb_pub.get_subscription_count() >= 3
            and self.depth_pub.get_subscription_count() >= 1
            and self.info_pub.get_subscription_count() >= 1
        )

    def publish_frame(self, phase):
        """Send one source-stamped RGB frame with its aligned synthetic depth."""
        height, width = self.image.shape[:2]
        rgb = self.bridge.cv2_to_imgmsg(self.image, encoding='bgr8')
        rgb.header.stamp = self.get_clock().now().to_msg()
        rgb.header.frame_id = 'probe_camera_optical_frame'
        depth_value = 2.0 if phase == 'metric_2m' else np.nan
        depth = self.bridge.cv2_to_imgmsg(
            np.full((height, width), depth_value, dtype=np.float32),
            encoding='32FC1',
        )
        depth.header = rgb.header
        info = CameraInfo()
        info.header = rgb.header
        info.width, info.height = width, height
        focal = float(width)
        cx, cy = width / 2.0, height / 2.0
        info.k = [focal, 0.0, cx, 0.0, focal, cy, 0.0, 0.0, 1.0]
        info.p = [focal, 0.0, cx, 0.0, 0.0, focal, cy, 0.0,
                  0.0, 0.0, 1.0, 0.0]
        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        info.distortion_model = 'plumb_bob'
        info.d = [0.0] * 5
        self.sent[stamp_key(rgb.header.stamp)] = phase
        self.info_pub.publish(info)
        self.depth_pub.publish(depth)
        self.rgb_pub.publish(rgb)

    def _on_yolo(self, message):
        self.yolo[stamp_key(message.header.stamp)] = sum(
            det.class_name == 'person' or det.class_id == 0
            for det in message.detections
        )

    def _on_reid(self, message):
        self.reid[stamp_key(message.header.stamp)] = {
            det.id for det in message.detections
        }

    def _on_positions(self, message):
        self.positions[stamp_key(message.header.stamp)] = {
            det.id: (
                det.bbox.center.position.z,
                det.results[0].pose.covariance[14],
            )
            for det in message.detections if det.results
        }

    def _on_debug(self, message):
        if message.format == 'jpeg' and message.data:
            self.debug.add(stamp_key(message.header.stamp))

    def _on_trace(self, message):
        self.traces[stamp_key(message.source_stamp)] = message

    def result(self):
        """Check inference results and exact source-time joins across stages."""
        common = (
            set(self.sent) & set(self.yolo) & set(self.reid)
            & set(self.positions) & self.debug & set(self.traces)
        )
        valid = sorted(key for key in common if (
            self.yolo[key] > 0 and self.reid[key] and self.positions[key]
        ))
        phase_counts = {
            phase: sum(self.sent[key] == phase for key in valid)
            for phase in ('metric_2m', 'bearing_3m')
        }
        for phase, count in phase_counts.items():
            if count < 3:
                raise AssertionError(
                    f'{phase}: only {count} complete person observations; '
                    f'stage counts={self.counts()}'
                )
        reference_ids = self.reid[valid[0]]
        delays_ms = []
        for key in valid:
            if self.reid[key] != reference_ids:
                raise AssertionError('identity set changed on the static image')
            if set(self.positions[key]) != self.reid[key]:
                raise AssertionError('3D output changed the upstream identity')
            metric = self.sent[key] == 'metric_2m'
            expected_z, expected_variance = (2.0, 0.0001) if metric else (3.0, 4.0)
            for z_value, variance in self.positions[key].values():
                if not np.isclose(z_value, expected_z):
                    raise AssertionError(f'wrong projected depth: {z_value}')
                if not np.isclose(variance, expected_variance):
                    raise AssertionError(f'wrong depth covariance: {variance}')
            trace = self.traces[key]
            if not (0 < trace.receipt_steady_time_ns
                    <= trace.publish_steady_time_ns):
                raise AssertionError('invalid monotonic timing order')
            if trace.source != 'camera':
                raise AssertionError('wrong trace source')
            delays_ms.append(
                (trace.publish_steady_time_ns - trace.receipt_steady_time_ns)
                / 1_000_000.0
            )
        return {
            'passed': True,
            'fixture': 'Ultralytics bus RGB + synthetic intrinsics/depth',
            'robot_following_test': False,
            'stage_counts': self.counts(),
            'complete_observations_by_phase': phase_counts,
            'stable_person_ids': sorted(reference_ids),
            'trace_boundary': 'YOLO callback receipt -> 3D publication',
            'trace_latency_ms': {
                'median': float(np.median(delays_ms)),
                'p95': float(np.percentile(delays_ms, 95)),
                'max': float(max(delays_ms)),
            },
        }

    def counts(self):
        """Report stage delivery counts even when validation fails."""
        return {
            'rgb_sent': len(self.sent), 'yolo': len(self.yolo),
            'reid': len(self.reid), 'positions_3d': len(self.positions),
            'debug_jpeg': len(self.debug), 'trace': len(self.traces),
        }


def stop_owned_launch(process):
    """Signal only this launch; let ROS launch stop its own children once."""
    if process is None or process.poll() is not None:
        return
    # Group SIGINT would reach both launch and children, while launch forwards
    # another SIGINT. The duplicate shutdown was the source of earlier issues.
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=12.0)
    except subprocess.TimeoutExpired:
        # The new-session group belongs only to this probe. Fall back only if
        # normal ROS launch shutdown did not finish, not as the first signal.
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5.0)


def main():
    """Run a bounded opt-in probe and save its log/result in a temp directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', type=int, default=161)
    parser.add_argument('--duration-s', type=float, default=30.0)
    parser.add_argument('--startup-timeout-s', type=float, default=10.0)
    parser.add_argument('--image', type=Path, default=(
        Path.home() / '.cache/malbut_yolo/runtime/lib/python3.10/site-packages'
        / 'ultralytics/assets/bus.jpg'
    ))
    args = parser.parse_args()
    if not 1 <= args.domain <= 232:
        parser.error('use an isolated non-default ROS domain in [1, 232]')
    if not 2.0 <= args.duration_s <= 60.0:
        parser.error('duration must be between 2 and 60 seconds')
    if not 1.0 <= args.startup_timeout_s <= 30.0:
        parser.error('startup timeout must be between 1 and 30 seconds')
    image = cv2.imread(str(args.image))
    if image is None:
        parser.error(f'cannot read installed test image: {args.image}')
    width = 640
    height = round(image.shape[0] * width / image.shape[1])
    image = cv2.resize(image, (width, height))
    os.environ['ROS_DOMAIN_ID'] = str(args.domain)
    os.environ['ROS_LOCALHOST_ONLY'] = '1'
    output_dir = Path(tempfile.mkdtemp(prefix='malbut-shared-pipeline-'))
    process, node = None, None
    result = None
    rclpy.init()
    try:
        node = PipelineProbe(image)
        discovery_end = time.monotonic() + 1.0
        while time.monotonic() < discovery_end:
            rclpy.spin_once(node, timeout_sec=0.05)
        others = [name for name, _ in node.get_node_names_and_namespaces()
                  if name != node.get_name()]
        if others:
            raise RuntimeError(f'ROS domain already in use: {others}')
        command = [
            'ros2', 'launch', 'malbut_tracking', 'person_detection.launch.py',
            'use_sim_time:=false', 'device:=cuda:0', 'reid_backend:=osnet',
            'projection_frame:=probe_camera_optical_frame',
            'publish_debug_image:=true', 'debug_image_transport:=compressed',
        ]
        with (output_dir / 'launch.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, env=dict(os.environ),
            )
            startup_end = time.monotonic() + args.startup_timeout_s
            while not node.ready():
                if process.poll() is not None:
                    raise RuntimeError('perception launch exited during startup')
                if time.monotonic() >= startup_end:
                    raise TimeoutError('RGB-D consumers did not become ready')
                rclpy.spin_once(node, timeout_sec=0.05)
            start = time.monotonic()
            next_frame = start
            end = start + args.duration_s
            while time.monotonic() < end:
                if process.poll() is not None:
                    raise RuntimeError('perception launch exited during probe')
                now = time.monotonic()
                if now >= next_frame:
                    phase = ('metric_2m' if now - start < args.duration_s / 2
                             else 'bearing_3m')
                    node.publish_frame(phase)
                    next_frame = now + 0.2
                rclpy.spin_once(node, timeout_sec=0.02)
            drain_end = time.monotonic() + 3.0
            while time.monotonic() < drain_end:
                rclpy.spin_once(node, timeout_sec=0.05)
            result = node.result()
    except (AssertionError, RuntimeError, TimeoutError) as error:
        result = {'passed': False, 'error': str(error),
                  'stage_counts': node.counts() if node is not None else {}}
    finally:
        stop_owned_launch(process)
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    result['domain'] = args.domain
    result['image'] = str(args.image)
    result['artifacts'] = str(output_dir)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    (output_dir / 'result.json').write_text(encoded + '\n', encoding='utf-8')
    print(encoded, flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
