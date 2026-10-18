"""One transient RGB snapshot and small observations on the existing Agent node."""

from dataclasses import dataclass, field
import hashlib
import io
import math
import os
from threading import RLock
import time


MAX_FRAME_AGE_S = 2.0
MAX_SETTINGS_AGE_S = 30.0
MAX_JPEG_BYTES = 256 * 1024


@dataclass(frozen=True)
class TurnImage:
    """Keep bytes out of request JSON, SQLite, traces and conversation history."""

    jpeg: bytes = field(repr=False)
    allowed: object = field(repr=False, compare=False)

    def usable(self):
        """Recheck freshness and permission at the actual provider boundary."""
        return (isinstance(self.jpeg, bytes) and 0 < len(self.jpeg) <= MAX_JPEG_BYTES
                and bool(self.allowed()))


class LatestScene:
    """Retain at most one RGB frame; compress only when a speech turn needs it."""

    def __init__(self, *, cloud_settings_required, enabled=True, clock=time.monotonic):
        self.clock = clock
        self.enabled = enabled
        self.required = cloud_settings_required
        self._lock = RLock()
        self._frame = None
        self._permission = None
        self._epoch = 0

    def observe_settings(self, message):
        """Camera/cloud consent is independent of the fall-monitor enabled switch."""
        now = self.clock()
        checked = message.server_checked_at
        valid = (message.check_state == 'confirmed'
                 and math.isfinite(checked) and 0 <= now - checked <= MAX_SETTINGS_AGE_S
                 and bool(message.camera_enabled) and bool(message.cloud_consent))
        with self._lock:
            if self._permission is not None and valid:
                previous_valid, previous_checked = self._permission
                if checked < previous_checked or (checked == previous_checked and not previous_valid):
                    return  # A replay must not undo an observed OFF/failed check.
            previous = self._permitted(now)
            self._permission = (valid, checked)
            if not valid or previous != valid:
                self._epoch += 1
                self._frame = None

    def _permitted(self, now):
        if self._permission is None:
            return self.enabled and not self.required
        return (self.enabled and self._permission[0]
                and 0 <= now - self._permission[1] <= MAX_SETTINGS_AGE_S)

    def observe_image(self, image, source_stamp, source_age):
        """Never accumulate camera frames or accept stale/future source timestamps."""
        now = self.clock()
        with self._lock:
            if (self._permitted(now) and math.isfinite(source_age)
                    and 0 <= source_age <= MAX_FRAME_AGE_S):
                self._frame = (image, now, source_stamp, self._epoch, source_age)
            else:
                self._frame = None

    def capture(self, encode):
        """Freeze this turn's image; retries may reuse it but never silently recapture."""
        with self._lock:
            frame = self._frame
            if frame is None or not self._permitted(self.clock()):
                return {'available': False, 'reason': 'camera_or_consent_unavailable'}, None
        raw, received, stamp, epoch, initial_age = frame

        def allowed():
            with self._lock:
                age = self.clock() - received
                return (self._permitted(self.clock()) and self._epoch == epoch
                        and 0 <= age and initial_age + age <= MAX_FRAME_AGE_S)

        if not allowed():
            return {'available': False, 'reason': 'stale_frame'}, None
        try:
            jpeg = encode(raw)
        except Exception:
            return {'available': False, 'reason': 'encoding_failed'}, None
        if not isinstance(jpeg, bytes) or not 0 < len(jpeg) <= MAX_JPEG_BYTES or not allowed():
            return {'available': False, 'reason': 'frame_unavailable'}, None
        return {
            'available': True, 'source_stamp_s': stamp,
            'age_s': round(initial_age + self.clock() - received, 3),
            'sha256': hashlib.sha256(jpeg).hexdigest(),
        }, TurnImage(jpeg, allowed)


class RosSpeechScene:
    """Add subscriptions to the existing communication node, not another ROS node."""

    def __init__(self, node, missions):
        from malbut_interfaces.msg import FallSettingsSnapshot, SystemState
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Image
        from tf2_ros import Buffer, TransformListener

        self.node, self.missions = node, missions
        self._lock = RLock()
        self._system = None
        self._buffer = Buffer()
        self._listener = TransformListener(self._buffer, node)
        self.robot_frame = node.declare_parameter('scene_robot_frame', 'base_footprint').value
        self._images = LatestScene(
            cloud_settings_required=bool(os.environ.get('HOMECAM_BACKEND_URL')),
            enabled=bool(node.declare_parameter('scene_images_enabled', True).value),
        )
        topic = node.declare_parameter('scene_rgb_topic', '/depth_cam/rgb0/image_raw').value
        self._subscriptions = [
            node.create_subscription(SystemState, '/malbut/state', self._state,
                                     QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)),
            node.create_subscription(
                FallSettingsSnapshot, '/malbut/falls/settings/snapshot',
                self._images.observe_settings,
                QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)),
        ]
        if self._images.enabled:
            self._subscriptions.append(node.create_subscription(
                Image, topic, self._image,
                QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)))

    def _image(self, message):
        stamp = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
        now = self.node.get_clock().now().nanoseconds / 1e9
        # Keep one bounded original; no image decoding/inference in this callback.
        if not (0 < message.width <= 4096 and 0 < message.height <= 4096
                and len(message.data) <= 16 * 1024 * 1024):
            return
        self._images.observe_image(message, stamp, now - stamp)

    def _state(self, message):
        groups = ('active_foreground_missions', 'active_background_missions',
                  'pending_missions', 'suspended_missions')
        value = {'system_state': int(message.system_state), 'control_mode': int(message.control_mode)}
        for name in groups:
            value[name] = [{'capability_id': item.capability_id[:128], 'state': int(item.state)}
                           for item in getattr(message, name)[:16]]
        with self._lock:
            self._system = (time.monotonic(), value)

    @staticmethod
    def _encode(message):
        from cv_bridge import CvBridge
        from PIL import Image

        rgb = CvBridge().imgmsg_to_cv2(message, desired_encoding='rgb8')
        image = Image.fromarray(rgb)
        image.thumbnail((640, 400))
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=75)
        return output.getvalue()

    def snapshot(self):
        """Read bounded observations without waiting for a new camera/TF sample."""
        image, attachment = self._images.capture(self._encode)
        scene = {'image': image, 'map': None, 'pose': None, 'missions': None}
        if self.missions is not None:
            scene['map'] = self.missions.map_context()
        if scene['map'] is not None:
            try:
                from rclpy.time import Time

                transform = self._buffer.lookup_transform('map', self.robot_frame, Time())
                stamp = transform.header.stamp.sec + transform.header.stamp.nanosec / 1e9
                age = self.node.get_clock().now().nanoseconds / 1e9 - stamp
                p, q = transform.transform.translation, transform.transform.rotation
                yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y*q.y + q.z*q.z))
                if 0 <= age <= MAX_FRAME_AGE_S and all(math.isfinite(v) for v in (p.x, p.y, yaw)):
                    scene['pose'] = {'frame_id': 'map', 'x': p.x, 'y': p.y,
                                     'yaw_deg': math.degrees(yaw), 'age_s': round(age, 3)}
            except Exception:
                pass  # Unknown TF is not a fabricated (0, 0) position.
        with self._lock:
            # Manager publishes transitions, not heartbeats. Keep its latched
            # observation with age, and never present it as proof of liveness.
            if self._system is not None and self.node.count_publishers('/malbut/state') > 0:
                scene['missions'] = {
                    **self._system[1], 'source': 'latched_transition',
                    'observed_age_s': round(max(0.0, time.monotonic() - self._system[0]), 3),
                }
        return scene, attachment

    def close(self):
        """Release TF subscriptions before their existing owning node is destroyed."""
        self._images.enabled = False
        self._listener.unregister()
