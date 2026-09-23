"""Opt-in ROS input/output wiring for the Cloud-only fall runtime.

Settings and health use malbut_interfaces. Agent/event handoffs remain an
experimental JSON bridge. No TTS, drive commands or push HTTP here.
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
    FallNodeSettings, apply_decision, event_metadata, parse_agent_reply,
    parse_subject_observation,
)
from malbut_agent_server.ports.fall_event_journal import FallJournalError
from malbut_agent_server.fall_control import (
    FallSettingsControl, SETTINGS_FIELDS, HEARTBEAT_FIELDS,
)


def create_fall_node(settings, *, provider, journal, clock=time.monotonic):
    import cv2
    from cv_bridge import CvBridge, CvBridgeError
    from rcl_interfaces.msg import ParameterDescriptor
    from rclpy.clock import Clock, ClockType
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from malbut_interfaces.msg import FallRuntimeStatus, FallControlHeartbeat
    from malbut_interfaces.srv import ApplyFallSettings
    from sensor_msgs.msg import Image
    from std_msgs.msg import String

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
            manager = self.declare_parameter(
                'manager_runtime_id', '', descriptor=ParameterDescriptor(read_only=True)).value
            runtime = self.declare_parameter(
                'runtime_id', '', descriptor=ParameterDescriptor(read_only=True)).value
            self.control = FallSettingsControl(
                self.inputs, manager_runtime_id=manager, runtime_id=runtime, clock=clock)
            self._bridge = CvBridge()
            self._last_image = -float('inf')
            self._last_processed_image = None
            self._status_sequence = 0
            self._logs = {}
            self._events = self.create_publisher(String, '/malbut/falls/runtime/events', 50)
            self._status = self.create_publisher(FallRuntimeStatus, '/malbut/falls/status', 10)
            self.create_service(ApplyFallSettings, '/malbut/falls/settings/apply',
                                self.on_settings)
            self.create_subscription(FallControlHeartbeat, '/malbut/falls/control/heartbeat',
                                     self.on_heartbeat, 10)
            self.create_subscription(Image, settings.image_topic, self.on_image,
                                     qos_profile_sensor_data)
            self.create_subscription(String, '/homecam/person_poses',
                                     lambda msg: self.on_detector('poses', msg),
                                     qos_profile_sensor_data)
            self.create_subscription(String, '/homecam/fall_candidates',
                                     lambda msg: self.on_detector('candidates', msg), 10)
            self.create_subscription(String, '/malbut/falls/runtime/agent_reply',
                                     self.on_answer, 10)
            self.create_subscription(String, '/malbut/falls/runtime/subject_observation',
                                     self.on_subject, 10)
            self.create_subscription(String, '/malbut/falls/runtime/decision',
                                     lambda msg: self.guarded('decision_rejected', apply_decision,
                                                              self.monitor, msg.data), 10)
            self._status_clock = Clock(clock_type=ClockType.STEADY_TIME)
            self.create_timer(1.0, self.publish_status, clock=self._status_clock)

        def on_settings(self, request, response):
            result = self.control.apply_settings(**{
                key: getattr(request, key) for key in SETTINGS_FIELDS})
            for key, value in result.items():
                setattr(response, key, value)
            return response

        def on_heartbeat(self, message):
            if not self.control.heartbeat(**{
                    key: getattr(message, key) for key in HEARTBEAT_FIELDS}):
                self.log_code('control_heartbeat_rejected')

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
            accepted = self.inputs.rgb(bytes(encoded), capture=ros_stamp(
                dict(sec=message.header.stamp.sec, nanosec=message.header.stamp.nanosec)),
                frame_id=message.header.frame_id, source_now=self.source_now(), now=clock())
            if accepted:
                self._last_processed_image = clock()

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
            fields = self.control.status()
            analysis = self.monitor.analysis_status
            self._status_sequence += 1
            self._status.publish(FallRuntimeStatus(
                **fields, sequence=self._status_sequence,
                last_frame_age_s=(-1.0 if self._last_processed_image is None
                                  else max(0.0, clock() - self._last_processed_image)),
                analysis_state=analysis.state, request_id=analysis.request_id,
                request_purpose=analysis.request_purpose,
                last_error_code=analysis.last_error_code))

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
            if check is None and node.control.cloud_block_reason is None:
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
        if settings.device_id == 'REPLACE_WITH_REGISTERED_DEVICE_ID':
            print('replace example device_id with the registered robot ID '
                  'before execution')
            return 2
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
