"""Opt-in ROS input/output wiring for the Cloud-only fall runtime.

Settings and health use malbut_interfaces. The Manager owns confirmation
handoffs; the conversation Agent never subscribes to these runtime topics.
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
from malbut_agent_server.application.fall_place_locator import FallPlaceLocator, FrameGeometry
from malbut_agent_server.fall_runtime import (
    FallNodeSettings, apply_decision, event_metadata, parse_agent_reply,
    parse_subject_observation,
)
from malbut_agent_server.ports.fall_event_journal import FallJournalError
from malbut_agent_server.fall_control import (
    FallSettingsControl, SETTINGS_FIELDS, HEARTBEAT_FIELDS,
)


MISSION_STATE_TOPIC = '/malbut/state'
MAPPING_CAPABILITY = 'autoslam'
# Depth paired with an RGB frame; stored every 4th pixel to bound memory.
DEPTH_MAX_STAMP_DELTA_S = 0.1
DEPTH_STEP = 4


def mapping_active(state):
    """True while an AutoSLAM mission runs or waits to run."""
    missions = (*state.active_foreground_missions, *state.active_background_missions,
                *state.pending_missions)
    return any(mission.capability_id == MAPPING_CAPABILITY for mission in missions)


def create_fall_node(settings, *, provider, journal, clock=time.monotonic,
                     tracker_factory=None):
    from collections import deque

    import cv2
    from cv_bridge import CvBridge, CvBridgeError
    import numpy as np
    from rcl_interfaces.msg import ParameterDescriptor
    from rclpy.clock import Clock, ClockType
    from rclpy.duration import Duration
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data,
    )
    from rclpy.time import Time
    from malbut_interfaces.msg import FallRuntimeStatus, FallControlHeartbeat, SystemState
    from malbut_interfaces.srv import ApplyFallSettings
    from sensor_msgs.msg import CameraInfo, Image
    from std_msgs.msg import String

    class FallNode(Node):
        def __init__(self):
            super().__init__('malbut_cloud_fall_monitor')
            # Scene cases compare map points when depth, intrinsics and the
            # map pose (AMCL) exist; any missing piece falls back to the image.
            depth_topic = self.declare_parameter(
                'depth_topic', '', descriptor=ParameterDescriptor(read_only=True)).value
            info_topic = self.declare_parameter(
                'camera_info_topic', '', descriptor=ParameterDescriptor(read_only=True)).value
            self._global_frame = self.declare_parameter(
                'global_frame', 'map', descriptor=ParameterDescriptor(read_only=True)).value
            self._projection_frame = self.declare_parameter(
                'place_projection_frame', '',
                descriptor=ParameterDescriptor(read_only=True)).value
            self.place = FallPlaceLocator() if depth_topic and info_topic else None
            approach = self.declare_parameter(
                'approach_enabled', False, descriptor=ParameterDescriptor(read_only=True)).value
            self.monitor = CloudFallMonitor(
                device_id=settings.device_id, boot_id=str(uuid4()), policy=settings.policy,
                buffer=FallFrameBuffer(retention_s=settings.retention_s,
                                       max_bytes=settings.buffer_bytes,
                                       max_frames=settings.buffer_frames),
                provider=provider, journal=journal, clock=clock, place_locator=self.place)
            self.monitor.approach_enabled = bool(approach)
            self.inputs = FallDetectorInput(
                self.monitor, max_source_age_s=settings.max_source_age_s)
            manager = self.declare_parameter(
                'manager_runtime_id', '', descriptor=ParameterDescriptor(read_only=True)).value
            runtime = self.declare_parameter(
                'runtime_id', '', descriptor=ParameterDescriptor(read_only=True)).value
            self.runtime_id = runtime
            self.control = FallSettingsControl(
                self.inputs, manager_runtime_id=manager, runtime_id=runtime, clock=clock)
            self._bridge = CvBridge()
            self._last_image = -float('inf')
            self._last_processed_image = None
            self._status_sequence = 0
            self._logs = {}
            from malbut_agent_server.application.fall_live_tracking import LiveDiscoveryTracking
            self.tracking = (LiveDiscoveryTracking(
                self.monitor, tracker_factory, clock=clock, report=self.log_code)
                if tracker_factory is not None else None)
            self._last_question_handoff = -float('inf')
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
            # Map making moves the camera around the home; its frames read as false falls.
            self.create_subscription(
                SystemState, MISSION_STATE_TOPIC, self.on_system_state,
                QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                           reliability=ReliabilityPolicy.RELIABLE))
            self._depths = deque(maxlen=10)
            self._camera_info = None
            self._tf_errors = ()
            if self.place is not None:
                from tf2_ros import Buffer, TransformException, TransformListener
                self._tf_errors = (TransformException,)
                self._tf = Buffer(cache_time=Duration(seconds=30))
                self._tf_listener = TransformListener(self._tf, self)
                self.create_subscription(Image, depth_topic, self._depths.append,
                                         qos_profile_sensor_data)
                self.create_subscription(CameraInfo, info_topic, self.on_camera_info,
                                         qos_profile_sensor_data)
            self._status_clock = Clock(clock_type=ClockType.STEADY_TIME)
            self.create_timer(1.0, self.publish_status, clock=self._status_clock)

        def on_camera_info(self, message):
            k = message.k
            if (message.width > 0 and message.height > 0 and k[0] > 0 and k[4] > 0
                    and all(np.isfinite(k))):
                self._camera_info = message

        def on_system_state(self, message):
            self.guarded('mission_state_invalid', self.control.set_mapping,
                         mapping_active(message))
            self.monitor.set_running_missions(
                mission.capability_id for mission in (*message.active_foreground_missions,
                                                      *message.active_background_missions))

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
            geometry = self.frame_geometry(message)
            accepted = self.inputs.rgb(bytes(encoded), capture=ros_stamp(
                dict(sec=message.header.stamp.sec, nanosec=message.header.stamp.nanosec)),
                frame_id=message.header.frame_id, source_now=self.source_now(), now=clock(),
                on_accepted=(None if geometry is None
                             else lambda observed: self.place.add(observed, geometry)))
            if accepted:
                self._last_processed_image = clock()

        def frame_geometry(self, message):
            """Aligned depth and the map pose of this RGB frame, or None (place unknown)."""
            info = self._camera_info
            if self.place is None or info is None or not self._depths:
                return None

            def seconds(stamp):
                return stamp.sec + stamp.nanosec / 1e9
            stamp = seconds(message.header.stamp)
            depth = min(self._depths, key=lambda d: abs(seconds(d.header.stamp) - stamp))
            if (abs(seconds(depth.header.stamp) - stamp) > DEPTH_MAX_STAMP_DELTA_S
                    or depth.encoding.upper() not in ('16UC1', 'MONO16', '32FC1')):
                return None
            source = self._projection_frame or info.header.frame_id or depth.header.frame_id
            if not source:
                return None
            try:
                image = np.asarray(self._bridge.imgmsg_to_cv2(
                    depth, desired_encoding='passthrough'))[::DEPTH_STEP, ::DEPTH_STEP]
                if depth.encoding.upper() == '32FC1':
                    image = np.clip(np.nan_to_num(image * 1000.0, nan=0.0, posinf=0.0,
                                                  neginf=0.0), 0, 65535)
                try:
                    transform = self._tf.lookup_transform(
                        self._global_frame, source, Time.from_msg(message.header.stamp))
                except self._tf_errors:
                    # The newest map pose: the frame arrived within a second.
                    transform = self._tf.lookup_transform(self._global_frame, source, Time())
                t, r = transform.transform.translation, transform.transform.rotation
                return FrameGeometry(
                    np.ascontiguousarray(image, dtype=np.uint16), info.k[0], info.k[4],
                    info.k[2], info.k[5], info.width, info.height,
                    (t.x, t.y, t.z), (r.x, r.y, r.z, r.w))
            except (*self._tf_errors, CvBridgeError, ValueError, TypeError):
                return None  # No map or depth: scene cases use image positions.

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
            # The latched state outlives a stopped Manager; never stay paused for it.
            if self.count_publishers(MISSION_STATE_TOPIC) == 0:
                self.control.set_mapping(False)
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
            self.monitor.flush_people()
            events = list(self.monitor.drain_events())
            if clock() - self._last_question_handoff >= 1.0:
                self._last_question_handoff = clock()
                sent = {event.question_id for event in events
                        if event.kind == 'question_requested'}
                events.extend(event for event in self.monitor.pending_questions()
                              if event.question_id not in sent)
            for event in events:
                self._events.publish(String(data=json.dumps(
                    dict(event_metadata(event), boot_id=self.monitor.boot_id,
                         runtime_id=self.runtime_id), allow_nan=False)))
                if (self.tracking is not None and event.kind == 'cloud_discovery'
                        and self.control.accepting_images):
                    self.tracking.offer(event.discovery)

    return FallNode()


async def spin_runtime(node, *, key_sync=None, key_sync_interval_s=None, clock=time.monotonic):
    """ROS callbacks and monitor state share one loop; network waits yield it."""
    import rclpy

    check = None
    key_fetch, next_key_sync = None, clock()
    try:
        while rclpy.ok():
            if key_sync is not None:
                # HTTP off the loop; the key is applied on the loop thread.
                if key_fetch is not None and key_fetch.done():
                    key_sync.apply(key_fetch.result())
                    key_fetch = None
                if key_fetch is None and clock() >= next_key_sync:
                    key_fetch = asyncio.ensure_future(asyncio.to_thread(key_sync.fetch))
                    next_key_sync = clock() + key_sync_interval_s
            rclpy.spin_once(node, timeout_sec=0)
            node.control.refresh()
            node.monitor.maintain_associations()
            if node.tracking is not None:
                node.tracking.maintain(accepting_images=node.control.accepting_images)
            if check is not None and check.done():
                check.result()  # Persistence/internal errors must stop the process.
                check = None
            if check is None and node.control.cloud_block_reason is None:
                check = asyncio.create_task(node.monitor.run_once())
            node.publish_events()
            await asyncio.sleep(0.01)
    finally:
        node.control.close()
        try:
            if node.tracking is not None:
                await node.tracking.close()
        finally:
            await node.monitor.close()
            if check is not None:
                check.cancel()
                await asyncio.gather(check, return_exceptions=True)
            if key_fetch is not None:
                await asyncio.gather(key_fetch, return_exceptions=True)
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

        key_sync = None
        if settings.key_sync is None:
            api_key = _read_token(settings.cloud_key_file)
        else:
            # With server key sync the robot may start before any key exists.
            api_key = (_read_token(settings.cloud_key_file)
                       if settings.cloud_key_file.exists() else None)
        provider = OllamaCloudFallProvider(model=settings.model, api_key=api_key)
        if settings.key_sync is not None:
            from malbut_agent_server.adapters.outbound.homecam_fall_key import HomecamFallKeyClient
            from malbut_agent_server.application.fall_key_sync import FallCloudKeySync
            sync = settings.key_sync
            key_sync = FallCloudKeySync(
                client=HomecamFallKeyClient(
                    base_url=sync.base_url, device_id=settings.device_id,
                    device_token=_read_token(sync.token_file),
                    allowed_hosts=set(sync.allow_hosts), timeout_s=5),
                key_file=settings.cloud_key_file, model=settings.model,
                apply_key=provider.replace_key)
        tracker_factory = None
        if settings.tracking is not None:
            from functools import partial
            from malbut_agent_server.adapters.outbound.sam_tracking import SamTrackingWorker
            tracker_factory = partial(SamTrackingWorker, settings.tracking)
        journal = SqliteFallJournal(settings.journal_path, device_id=settings.device_id)
        node = None
        try:
            rclpy.init(args=ros_args)

            async def run():
                nonlocal node
                node = create_fall_node(settings, provider=provider, journal=journal,
                                        tracker_factory=tracker_factory)
                await spin_runtime(node, key_sync=key_sync,
                                   key_sync_interval_s=settings.key_sync.interval_s
                                   if settings.key_sync else None)
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
