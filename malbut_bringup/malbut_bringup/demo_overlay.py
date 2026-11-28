"""Align existing pose observations to camera exposure timestamps for the demo."""

from array import array
from collections import deque
import json
import math
import time

BONES = (
    ('left_shoulder', 'right_shoulder'), ('left_shoulder', 'left_hip'),
    ('right_shoulder', 'right_hip'), ('left_hip', 'right_hip'),
    ('left_shoulder', 'left_elbow'), ('left_elbow', 'left_wrist'),
    ('right_shoulder', 'right_elbow'), ('right_elbow', 'right_wrist'),
    ('left_hip', 'left_knee'), ('left_knee', 'left_ankle'),
    ('right_hip', 'right_knee'), ('right_knee', 'right_ankle'),
)


def observed_people(payload):
    """Keep observed tracks in source-image coordinates, including only visible joints."""
    people = {}
    for person in payload.get('persons', []):
        pose = person.get('pose')
        if not person.get('observed') or not pose:
            continue
        points = {p['name']: (p['x'], p['y']) for p in pose['keypoints']
                  if p['confidence'] >= 0.35}
        box = pose['box']
        points['box_min'] = (box['left'], box['top'])
        points['box_max'] = (box['right'], box['bottom'])
        people[person['trackId']] = points
    return people


def aligned_people(samples, stamp):
    """Interpolate shared track IDs only; with no close observation omit the skeleton."""
    before = max((s for s in samples if s[0] <= stamp), key=lambda s: s[0], default=None)
    after = min((s for s in samples if s[0] >= stamp), key=lambda s: s[0], default=None)
    if before and after and 0 < after[0]-before[0] <= 0.45:
        f = (stamp-before[0]) / (after[0]-before[0])
        result = {}
        for track in before[1].keys() & after[1].keys():
            a, b = before[1][track], after[1][track]
            result[track] = {
                name: tuple(x+(y-x)*f for x, y in zip(a[name], b[name]))
                for name in a.keys() & b.keys()
            }
        return result
    nearest = min((s for s in (before, after) if s), key=lambda s: abs(s[0]-stamp),
                  default=None)
    return nearest[1] if nearest and abs(nearest[0]-stamp) <= 0.075 else {}


def object_boxes(message):
    """Read the detector's class names and boxes in original RGB pixels."""
    boxes = []
    for detection in message.detections:
        center, size = detection.bbox.center.position, detection.bbox.size
        if not all(math.isfinite(v) for v in (center.x, center.y, size.x, size.y)):
            continue
        if size.x > 0 and size.y > 0:
            boxes.append((detection.class_name, center.x-size.x/2, center.y-size.y/2,
                          center.x+size.x/2, center.y+size.y/2))
    return boxes


def overlaps_person(box, poses):
    """Suppress a duplicate person label when both detectors see the same person."""
    x1, y1, x2, y2 = box
    for a, b, c, d in poses:
        overlap = max(0, min(x2, c)-max(x1, a)) * max(0, min(y2, d)-max(y1, b))
        union = (x2-x1)*(y2-y1) + (c-a)*(d-b) - overlap
        if overlap > 0.35 * union:
            return True
    return False


def annotate(msg, people, suspects, objects=()):
    """Draw on a private image copy, preserving the camera encoding and timestamp."""
    import cv2
    import numpy as np
    channels = {'bgr8': 3, 'rgb8': 3, 'rgba8': 4, 'bgra8': 4}.get(msg.encoding)
    if not (people or objects) or not channels:
        return msg
    w, h = msg.width, msg.height
    if not w or not h or msg.step < w*channels or len(msg.data) < h*msg.step:
        return msg
    pixels = np.ndarray((h, w, channels), np.uint8, buffer=msg.data,
                        strides=(msg.step, channels, 1)).copy()
    rgb = msg.encoding.startswith('rgb')
    red = (248, 77, 68) if rgb else (68, 77, 248)
    green = (62, 205, 156, 255) if rgb else (156, 205, 62, 255)
    pose_boxes = []
    for track, joints in people.items():
        color = (*red, 255) if track in suspects else (247, 249, 250, 255)
        pts = {k: (round(max(0, min(1, x))*(w-1)), round(max(0, min(1, y))*(h-1)))
               for k, (x, y) in joints.items() if math.isfinite(x) and math.isfinite(y)}
        if 'box_min' in pts and 'box_max' in pts:
            left, top = pts['box_min']
            right, bottom = pts['box_max']
            pose_boxes.append((left, top, right, bottom))
            box_color = color if track in suspects else green
            cv2.rectangle(pixels, (left, top), (right, bottom), box_color, 2)
            cv2.putText(pixels, 'person', (left, max(14, top-4)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.45, box_color, 1, cv2.LINE_AA)
        for a, b in BONES:
            if a in pts and b in pts:
                cv2.line(pixels, pts[a], pts[b], color, 3, cv2.LINE_AA)
        for name, point in pts.items():
            if not name.startswith('box_'):
                cv2.circle(pixels, point, 3, color, -1, cv2.LINE_AA)
    for label, x1, y1, x2, y2 in objects:
        left, right = round(max(0, x1)), round(min(w-1, x2))
        top, bottom = round(max(0, y1)), round(min(h-1, y2))
        if left >= right or top >= bottom:
            continue
        if label == 'person' and overlaps_person((left, top, right, bottom), pose_boxes):
            continue
        cv2.rectangle(pixels, (left, top), (right, bottom), green, 2)
        cv2.putText(pixels, label, (left, max(14, top-4)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, green, 1, cv2.LINE_AA)
    msg.step = w*channels
    data = array('B')
    data.frombytes(memoryview(pixels).cast('B'))
    msg.data = data
    return msg


def main(args=None):
    """Subscribe to camera data only while the local cloud bridge holds a viewer lease."""
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image
    from std_msgs.msg import String
    from yolo_msgs.msg import DetectionArray

    class Overlay(Node):
        """Buffer camera frames and combine observations from the same optical frame."""

        def __init__(self):
            super().__init__('malbut_demo_overlay')
            self.image_topic = self.declare_parameter(
                'image_topic', '/camera/color/image_raw').value
            self.output = self.create_publisher(
                Image, '/malbut/demo/image', qos_profile_sensor_data)
            self.status = self.create_publisher(String, '/malbut/demo/pose_status', 1)
            self.create_subscription(String, '/malbut/demo/lease', self._lease, 1)
            self.session, self.deadline = '', 0
            self.inputs = []
            self.frames = deque(maxlen=12)
            self.objects = deque(maxlen=32)
            self.poses = deque(maxlen=12)
            self.candidates = deque(maxlen=12)
            self.last_frame = -math.inf
            self.camera = None
            self.last_pose_at = -math.inf
            self.last_objects_at = -math.inf
            self.timer = None
            self.create_timer(0.25, self._check)

        def _lease(self, msg):
            try:
                session, deadline = json.loads(msg.data)
                if not isinstance(session, str) or not isinstance(deadline, (float, int)):
                    return
            except (ValueError, TypeError):
                return
            if session != self.session:
                self._reset()
            self.session, self.deadline = session, min(time.monotonic()+20, deadline)
            self._check()

        def _reset(self):
            for sub in self.inputs:
                self.destroy_subscription(sub)
            self.inputs.clear()
            self._stop_camera()

        def _stop_camera(self):
            if self.camera is not None:
                self.destroy_subscription(self.camera)
                self.camera = None
            if self.timer is not None:
                self.destroy_timer(self.timer)
                self.timer = None
            self.frames.clear()
            self.objects.clear()
            self.last_objects_at = self.last_pose_at = -math.inf
            self.poses.clear()
            self.candidates.clear()
            self.last_frame = -math.inf

        def _check(self):
            if not self.session or time.monotonic() >= self.deadline:
                if self.inputs:
                    self._reset()
            elif not self.inputs:
                for kind, topic, callback in (
                        (String, '/homecam/person_poses', self._pose),
                        (String, '/homecam/fall_candidates', self._candidate),
                        (DetectionArray, '/yolo/detections', self._objects),
                        (DetectionArray, '/yolo/yolo_node/demo_detections', self._objects)):
                    self.inputs.append(self.create_subscription(
                        kind, topic, callback, qos_profile_sensor_data))
            elif time.monotonic()-max(self.last_pose_at, self.last_objects_at) >= 2:
                if self.camera is not None:
                    self._stop_camera()

        def _start_camera(self):
            if self.camera is None:
                self.camera = self.create_subscription(
                    Image, self.image_topic, self._frame, qos_profile_sensor_data)
                self.timer = self.create_timer(1/45, self._render)

        def _objects(self, msg):
            if not self.inputs or time.monotonic() >= self.deadline:
                return
            stamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1_000_000_000.0
            self.objects.append((stamp, object_boxes(msg), msg.header.frame_id))
            self.last_objects_at = time.monotonic()
            self._start_camera()

        def _frame(self, msg):
            stamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1_000_000_000.0
            if stamp < self.last_frame:
                self.frames.clear()
                self.objects.clear()
                self.poses.clear()
                self.candidates.clear()
                self.last_frame = -math.inf
            if stamp-self.last_frame >= 1/15-0.003:
                self.last_frame = stamp
                self.frames.append((time.monotonic(), stamp, msg.header.frame_id, msg))

        def _pose(self, msg):
            if not self.inputs or time.monotonic() >= self.deadline:
                return
            try:
                data = json.loads(msg.data)
                if data['status'] != 'ok':
                    self.poses.clear()
                    self.candidates.clear()
                    self.last_pose_at = -math.inf
                    self._check()
                    return
                capture = data['captureStamp']
                stamp = capture['sec'] + capture['nanosec'] / 1_000_000_000.0
                observation = (stamp, observed_people(data), data['frameId'])
            except (ValueError, TypeError, KeyError, AttributeError):
                return
            self.last_pose_at = time.monotonic()
            self.poses.append(observation)
            self._start_camera()

        def _candidate(self, msg):
            try:
                data = json.loads(msg.data)
                if data['status'] != 'ok':
                    self.candidates.clear()
                    return
                stamp = data['captureTimeSec']
                if not isinstance(stamp, (float, int)) or not math.isfinite(stamp):
                    return
                suspects = {t['targetTrackId'] for t in data['tracks']
                            if t.get('subjectCheck', {}).get('state') == 'suspected'}
                self.candidates.append((stamp, suspects, data['frameId']))
            except (ValueError, TypeError, KeyError, AttributeError):
                return

        def _render(self):
            now = time.monotonic()
            if now >= self.deadline:
                return
            frame = None
            while self.frames and now-self.frames[0][0] >= 0.4:
                frame = self.frames.popleft()
            if frame is None:
                return
            _, stamp, frame_id, msg = frame
            observations = [p for p in self.poses if p[2] == frame_id]
            people = aligned_people(observations, stamp)
            nearest = min((c for c in self.candidates if c[2] == frame_id),
                          key=lambda c: abs(c[0]-stamp), default=None)
            suspects = nearest[1] if nearest and abs(nearest[0]-stamp) <= .2 else set()
            detected = min((o for o in self.objects if o[2] == frame_id),
                           key=lambda o: abs(o[0]-stamp), default=None)
            boxes = detected[1] if detected and abs(detected[0]-stamp) <= 0.15 else ()
            self.output.publish(annotate(msg, people, suspects, boxes))
            # Report a clear only from a detector frame that has both observations.
            # Interpolation can be empty when two tracks do not overlap.
            if nearest and abs(nearest[0]-stamp) <= .2:
                observed = next((p[1] for p in observations if p[0] == nearest[0]), None)
                if observed is not None:
                    status = [len(observed), bool(nearest[1] & observed.keys())]
                    self.status.publish(String(data=json.dumps(status)))

    rclpy.init(args=args)
    node = Overlay()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
