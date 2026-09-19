"""Opt-in ROS input/output wiring for the Cloud-only fall runtime.

Manager control/reply topics below are a proposed JSON bridge, not an approved
replacement for malbut_interfaces. No TTS, drive commands or push HTTP here.
"""

import argparse
import asyncio
import json
from pathlib import Path
import time
from uuid import uuid4

from malbut_agent_server.application.cloud_fall_monitor import CloudFallMonitor
from malbut_agent_server.application.fall_detector_input import FallDetectorInput, ros_stamp
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.fall_runtime import (
    FallNodeSettings, FallRuntimeControl, apply_decision, event_metadata, parse_agent_reply,
    parse_subject_observation,
)
from malbut_agent_server.ports.fall_event_journal import FallJournalError


def create_fall_node(settings, *, provider, journal, clock=time.monotonic):
    import cv2
    from cv_bridge import CvBridge, CvBridgeError
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy, qos_profile_sensor_data
    from sensor_msgs.msg import Image
    from std_msgs.msg import Bool, String

    class FallNode(Node):
        def __init__(self):
            super().__init__('malbut_cloud_fall_monitor')
            self.monitor = CloudFallMonitor(
                device_id=settings.device_id, boot_id=str(uuid4()), policy=settings.policy,
                buffer=FallFrameBuffer(retention_s=settings.retention_s,
                                       max_bytes=settings.buffer_bytes,
                                       max_frames=settings.buffer_frames),
                provider=provider, journal=journal, clock=clock)
            self.inputs = FallDetectorInput(
                self.monitor, max_source_age_s=settings.max_source_age_s)
            self.control = FallRuntimeControl(self.inputs, lease_s=settings.control_lease_s,
                                              clock=clock)
            self._bridge = CvBridge()
            self._last_image = -float('inf')
            self._logs = {}
            self._events = self.create_publisher(String, '/malbut/falls/runtime/events', 50)
            self._status = self.create_publisher(String, '/malbut/falls/runtime/status', 10)
            privacy_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                                     reliability=ReliabilityPolicy.RELIABLE)
            self.create_subscription(Bool, '/homecam/monitoring_enabled',
                                     lambda msg: self.control.media_permission(msg.data),
                                     privacy_qos)
            self.create_subscription(Image, settings.image_topic, self.on_image,
                                     qos_profile_sensor_data)
            self.create_subscription(String, '/homecam/person_poses',
                                     lambda msg: self.on_detector('poses', msg),
                                     qos_profile_sensor_data)
            self.create_subscription(String, '/homecam/fall_candidates',
                                     lambda msg: self.on_detector('candidates', msg), 10)
            self.create_subscription(String, '/malbut/falls/runtime/settings',
                                     lambda msg: self.guarded('settings_invalid',
                                                              self.control.settings, msg.data), 10)
            self.create_subscription(String, '/malbut/falls/runtime/agent_reply',
                                     self.on_answer, 10)
            self.create_subscription(String, '/malbut/falls/runtime/subject_observation',
                                     self.on_subject, 10)
            self.create_subscription(String, '/malbut/falls/runtime/decision',
                                     lambda msg: self.guarded('decision_rejected', apply_decision,
                                                              self.monitor, msg.data), 10)
            self.create_timer(1.0, self.publish_status)

        def guarded(self, code, operation, *args, **kwargs):
            try:
                return operation(*args, **kwargs)
            except FallJournalError:
                self.control.close()
                self.log_code('journal_failed_runtime_stopped')
                raise
            except (ValueError, TypeError, KeyError, OverflowError, RuntimeError,
                    CvBridgeError, cv2.error):
                # Never log image contents, model text, IDs, settings or credentials.
                self.log_code(code)

        def log_code(self, code):
            now = clock()
            if now - self._logs.get(code, -float('inf')) >= 5:
                self.get_logger().warning(code)
                self._logs[code] = now

        def source_now(self):
            return self.get_clock().now().nanoseconds / 1e9

        def on_image(self, message):
            self.control.refresh()
            if (not self.control.accepting_images
                    or clock() - self._last_image < 1 / settings.input_fps):
                return
            self._last_image = clock()
            self.guarded('rgb_invalid', self.convert_image, message)

        def convert_image(self, message):
            # Aurora input dimensions are explicit, not silently stretched.
            if (message.width != 640 or message.height != 400
                    or len(message.data) > 4 * 1024 * 1024):
                raise ValueError('unexpected RGB dimensions')
            frame = self._bridge.imgmsg_to_cv2(message, desired_encoding='bgr8')
            ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                raise ValueError('JPEG encoding failed')
            self.inputs.rgb(bytes(encoded), capture=ros_stamp(
                dict(sec=message.header.stamp.sec, nanosec=message.header.stamp.nanosec)),
                frame_id=message.header.frame_id, source_now=self.source_now(), now=clock())

        def on_detector(self, kind, message):
            self.control.refresh()
            if self.control.accepting_images:
                self.guarded('detector_input_invalid', getattr(self.inputs, kind), message.data,
                             source_now=self.source_now(), now=clock())

        def on_answer(self, message):
            self.control.refresh()
            reply = self.guarded('agent_reply_invalid', parse_agent_reply, message.data)
            if reply is not None:
                self.guarded('agent_reply_rejected', self.monitor.agent_reply, reply)

        def on_subject(self, message):
            self.control.refresh()
            observation = self.guarded('subject_observation_invalid',
                                       parse_subject_observation, message.data)
            if observation is not None:
                self.guarded('subject_observation_rejected',
                             self.monitor.observe_subject, observation)

        def publish_status(self):
            self.control.refresh()
            self._status.publish(String(data=json.dumps(dict(
                runtimeId=self.control.runtime_id, acceptingImages=self.control.accepting_images,
                scanIntervalSec=self.monitor.periodic_interval_s(),
                managerBridge='experimental', automaticSceneAssociation=False))))

        def publish_events(self):
            for event in self.monitor.drain_events():
                self._events.publish(String(data=json.dumps(
                    event_metadata(event), allow_nan=False)))

    return FallNode()


async def spin_runtime(node):
    """ROS callbacks and monitor state share one loop; network waits yield it."""
    import rclpy

    check = None
    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0)
            node.control.refresh()
            if check is not None and check.done():
                check.result()  # Persistence/internal errors must stop the process.
                check = None
            if check is None:
                check = asyncio.create_task(node.monitor.run_once())
            node.publish_events()
            await asyncio.sleep(0.01)
    finally:
        node.control.close()
        await node.monitor.close()
        if check is not None:
            check.cancel()
            await asyncio.gather(check, return_exceptions=True)
        node.publish_events()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args, ros_args = parser.parse_known_args(argv)
    try:
        if args.config.stat().st_size > 16384:
            raise ValueError('config too large')
        settings = FallNodeSettings.parse(args.config.read_text())
        if not args.execute:
            print('configuration: ok (no ROS, no credential read, no Cloud call)')
            return 0
        from malbut_agent_server.adapters.outbound.ollama_cloud_fall import OllamaCloudFallProvider
        from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
        from malbut_agent_server.fall_upload_worker import _read_token
        import aiohttp  # noqa: F401: check optional dependency before opening the journal
        import rclpy

        provider = OllamaCloudFallProvider(model=settings.model,
                                           api_key=_read_token(settings.cloud_key_file))
        journal = SqliteFallJournal(settings.journal_path, device_id=settings.device_id)
        node = None
        try:
            rclpy.init(args=ros_args)

            async def run():
                nonlocal node
                node = create_fall_node(settings, provider=provider, journal=journal)
                await spin_runtime(node)
            asyncio.run(run())
        finally:
            if node is not None:
                node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()
            journal.close()
    except KeyboardInterrupt:
        return 0
    except (ValueError, TypeError, OSError, ImportError, RuntimeError):
        print('fall runtime stopped; check dependencies, protected paths and configuration')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
