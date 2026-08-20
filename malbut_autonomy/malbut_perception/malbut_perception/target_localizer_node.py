"""ROS 2 RGB-D person detector and camera-relative localizer."""

import math
from pathlib import Path
import sys
from typing import Dict, Optional, Tuple

import cv2
from cv_bridge import CvBridge, CvBridgeError
from geometry_msgs.msg import Quaternion, TransformStamped
import message_filters
import numpy as np
import rclpy
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, CompressedImage, Image
from std_msgs.msg import Bool
from tf2_ros import Buffer, TransformException, TransformListener
from vision_msgs.msg import (
    Detection2D,
    Detection2DArray,
    Detection3D,
    Detection3DArray,
    ObjectHypothesisWithPose,
)

from .depth.projector import (
    CameraIntrinsics,
    project_pixel,
    projected_box_size,
)
from .depth.roi_depth import estimate_roi_depth
from .detector import HogPersonDetector, YoloPersonDetector
from .detector.base import BoundingBox, PersonDetector
from .reid import (
    HistogramPersonEncoder,
    OsNetPersonEncoder,
    PersonAppearanceEncoder,
)
from .tracker import ByteTrackTracker, TrackedDetection


class TimestampRateLimiter:
    """Select source timestamps without drifting on discrete camera periods."""

    _MAX_TOLERANCE_NS = 5_000_000

    def __init__(self) -> None:
        self._interval_ns: Optional[int] = None
        self._last_input_ns: Optional[int] = None
        self._next_due_ns: Optional[int] = None

    def should_process(self, stamp_ns: int, rate_hz: float) -> bool:
        """Return whether a frame belongs to the requested sampling cadence."""
        if rate_hz <= 0.0:
            self._interval_ns = None
            self._last_input_ns = stamp_ns
            self._next_due_ns = None
            return True

        interval_ns = max(1, int(round(1_000_000_000 / rate_hz)))
        clock_reset = (
            self._last_input_ns is not None
            and stamp_ns < self._last_input_ns
        )
        rate_changed = self._interval_ns != interval_ns
        self._last_input_ns = stamp_ns
        if clock_reset or rate_changed or self._next_due_ns is None:
            self._interval_ns = interval_ns
            self._next_due_ns = stamp_ns + interval_ns
            return True

        tolerance_ns = min(
            self._MAX_TOLERANCE_NS,
            max(1, interval_ns // 20),
        )
        if stamp_ns + tolerance_ns < self._next_due_ns:
            return False

        overdue_ns = stamp_ns + tolerance_ns - self._next_due_ns
        elapsed_periods = overdue_ns // interval_ns + 1
        self._next_due_ns += elapsed_periods * interval_ns
        return True


def _rotate_vector(
    point: Tuple[float, float, float],
    rotation: Quaternion,
) -> Tuple[float, float, float]:
    """Rotate a vector by a normalized quaternion."""
    qx, qy, qz, qw = (
        float(rotation.x),
        float(rotation.y),
        float(rotation.z),
        float(rotation.w),
    )
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if norm <= 1e-12:
        raise ValueError('transform quaternion has zero norm')
    qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm
    x, y, z = point
    tx = 2.0 * (qy * z - qz * y)
    ty = 2.0 * (qz * x - qx * z)
    tz = 2.0 * (qx * y - qy * x)
    return (
        x + qw * tx + (qy * tz - qz * ty),
        y + qw * ty + (qz * tx - qx * tz),
        z + qw * tz + (qx * ty - qy * tx),
    )


def transform_point(
    point: Tuple[float, float, float],
    transform: TransformStamped,
) -> Tuple[float, float, float]:
    """Apply a geometry_msgs transform to a 3D point."""
    rotated = _rotate_vector(point, transform.transform.rotation)
    translation = transform.transform.translation
    return (
        rotated[0] + float(translation.x),
        rotated[1] + float(translation.y),
        rotated[2] + float(translation.z),
    )


class PersonLocalizerNode(Node):
    """Detect and localize people using only aligned RGB-D sensor messages."""

    def __init__(self) -> None:
        """Initialize detector, tracker, RGB-D subscribers, and publishers."""
        super().__init__('person_localizer')
        self._declare_parameters()
        self._validate_parameters()
        cv2.setNumThreads(
            int(self.get_parameter('opencv_num_threads').value)
        )
        self._bridge = CvBridge()
        self._detector = self._create_detector()
        self._reidentifier = self._create_reidentifier()
        self._tracker = ByteTrackTracker(
            high_threshold=float(
                self.get_parameter('tracker_high_threshold').value
            ),
            low_threshold=float(
                self.get_parameter('tracker_low_threshold').value
            ),
            match_iou_threshold=float(
                self.get_parameter('tracker_iou_threshold').value
            ),
            max_missed_frames=int(
                self.get_parameter('tracker_max_missed_frames').value
            ),
            min_confirmed_hits=int(
                self.get_parameter('tracker_min_confirmed_hits').value
            ),
            appearance_threshold=float(
                self.get_parameter('tracker_appearance_threshold').value
            ),
            appearance_weight=float(
                self.get_parameter('tracker_appearance_weight').value
            ),
            reid_threshold=float(
                self.get_parameter('reid_cosine_threshold').value
            ),
            reid_max_inactive_frames=int(
                self.get_parameter('reid_max_inactive_frames').value
            ),
            feature_budget=int(
                self.get_parameter('reid_feature_budget').value
            ),
        )
        self._camera_info: Optional[CameraInfo] = None
        self._rate_limiter = TimestampRateLimiter()
        self._inference_frame_index = 0
        self._warning_times: Dict[str, int] = {}

        self._output_frame = str(self.get_parameter('output_frame').value)
        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self)

        self._detections_2d_publisher = self.create_publisher(
            Detection2DArray,
            str(self.get_parameter('detections_2d_topic').value),
            10,
        )
        self._detections_3d_publisher = self.create_publisher(
            Detection3DArray,
            str(self.get_parameter('detections_3d_topic').value),
            10,
        )
        self._debug_publisher = None
        self._compressed_debug_publisher = None
        debug_transport = str(
            self.get_parameter('debug_image_transport').value
        )
        if debug_transport in {'raw', 'both'}:
            self._debug_publisher = self.create_publisher(
                Image,
                str(self.get_parameter('debug_image_topic').value),
                qos_profile_sensor_data,
            )
        if debug_transport in {'compressed', 'both'}:
            self._compressed_debug_publisher = self.create_publisher(
                CompressedImage,
                str(
                    self.get_parameter(
                        'compressed_debug_image_topic'
                    ).value
                ),
                qos_profile_sensor_data,
            )
        self._health_publisher = self.create_publisher(
            Bool,
            str(self.get_parameter('health_topic').value),
            10,
        )

        self._camera_info_subscription = self.create_subscription(
            CameraInfo,
            str(self.get_parameter('camera_info_topic').value),
            self._on_camera_info,
            qos_profile_sensor_data,
        )
        self._rgb_subscription = message_filters.Subscriber(
            self,
            Image,
            str(self.get_parameter('rgb_topic').value),
            qos_profile=qos_profile_sensor_data,
        )
        self._depth_subscription = message_filters.Subscriber(
            self,
            Image,
            str(self.get_parameter('depth_topic').value),
            qos_profile=qos_profile_sensor_data,
        )
        self._synchronizer = message_filters.ApproximateTimeSynchronizer(
            [self._rgb_subscription, self._depth_subscription],
            queue_size=int(self.get_parameter('sync_queue_size').value),
            slop=float(self.get_parameter('sync_slop_sec').value),
        )
        self._synchronizer.registerCallback(self._on_rgb_depth)

        detector_backend = self._detector.__class__.__name__
        reid_backend = self._reidentifier.__class__.__name__
        self.get_logger().info(
            f'Person perception ready with {detector_backend} and '
            f'{reid_backend}; RGB and depth messages are the only target '
            'observations.'
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter('rgb_topic', '/camera/color/image_raw')
        self.declare_parameter('depth_topic', '/camera/depth/image_raw')
        self.declare_parameter(
            'camera_info_topic', '/camera/color/camera_info'
        )
        self.declare_parameter(
            'detections_2d_topic', '/perception/person/detections_2d'
        )
        self.declare_parameter(
            'detections_3d_topic', '/perception/person/detections_3d'
        )
        self.declare_parameter(
            'debug_image_topic', '/perception/person/debug_image'
        )
        self.declare_parameter(
            'compressed_debug_image_topic',
            '/perception/person/debug_image/compressed',
        )
        self.declare_parameter('health_topic', '/perception/person/healthy')
        self.declare_parameter('projection_frame', '')
        self.declare_parameter('output_frame', '')
        self.declare_parameter('detector_backend', 'auto')
        self.declare_parameter('model_path', '')
        self.declare_parameter('confidence_threshold', 0.20)
        self.declare_parameter('nms_threshold', 0.45)
        self.declare_parameter('yolo_input_size', 640)
        self.declare_parameter('inference_backend', 'auto')
        self.declare_parameter('dnn_target', 'auto')
        self.declare_parameter('opencv_num_threads', 4)
        self.declare_parameter('hog_hit_threshold', 0.0)
        self.declare_parameter('tracker_high_threshold', 0.45)
        self.declare_parameter('tracker_low_threshold', 0.15)
        self.declare_parameter('tracker_iou_threshold', 0.30)
        self.declare_parameter('tracker_max_missed_frames', 15)
        self.declare_parameter('tracker_min_confirmed_hits', 2)
        self.declare_parameter('tracker_appearance_threshold', 0.35)
        self.declare_parameter('tracker_appearance_weight', 0.65)
        self.declare_parameter('reid_backend', 'auto')
        self.declare_parameter('reid_model_path', '')
        self.declare_parameter('reid_cosine_threshold', 0.35)
        self.declare_parameter('reid_max_inactive_frames', 2400)
        self.declare_parameter('reid_feature_budget', 30)
        self.declare_parameter('reid_refresh_interval_frames', 3)
        self.declare_parameter('reid_minimum_crop_width', 16)
        self.declare_parameter('reid_minimum_crop_height', 32)
        self.declare_parameter('depth_roi_scale', 0.45)
        self.declare_parameter('minimum_depth_m', 0.30)
        self.declare_parameter('maximum_depth_m', 3.0)
        self.declare_parameter('minimum_depth_samples', 20)
        self.declare_parameter('fallback_depth_scale', 1.0)
        # When RGB still sees a person beyond the depth camera's measurable
        # range, publish a deliberately uncertain point on the same image ray.
        # This is a lower-bound pursuit cue, not a fabricated metric distance.
        self.declare_parameter('enable_bearing_only_fallback', True)
        self.declare_parameter('bearing_only_uncertainty_m', 2.0)
        self.declare_parameter('person_thickness_m', 0.35)
        self.declare_parameter('sync_queue_size', 10)
        self.declare_parameter('sync_slop_sec', 0.08)
        self.declare_parameter('max_inference_rate_hz', 8.0)
        self.declare_parameter('publish_debug_image', True)
        self.declare_parameter('debug_image_transport', 'compressed')
        self.declare_parameter('debug_jpeg_quality', 80)

    def _validate_parameters(self) -> None:
        topic_parameters = (
            'rgb_topic',
            'depth_topic',
            'camera_info_topic',
            'detections_2d_topic',
            'detections_3d_topic',
            'debug_image_topic',
            'compressed_debug_image_topic',
            'health_topic',
        )
        for name in topic_parameters:
            if not str(self.get_parameter(name).value).startswith('/'):
                raise ValueError(f'{name} must be an absolute ROS topic')
        backend = str(self.get_parameter('detector_backend').value)
        if backend not in {'auto', 'yolo', 'hog'}:
            raise ValueError('detector_backend must be auto, yolo, or hog')
        reid_backend = str(self.get_parameter('reid_backend').value)
        if reid_backend not in {'auto', 'osnet', 'histogram'}:
            raise ValueError(
                'reid_backend must be auto, osnet, or histogram'
            )
        inference_backend = str(
            self.get_parameter('inference_backend').value
        )
        if inference_backend not in {'auto', 'onnxruntime', 'opencv'}:
            raise ValueError(
                'inference_backend must be auto, onnxruntime, or opencv'
            )
        minimum = float(self.get_parameter('minimum_depth_m').value)
        maximum = float(self.get_parameter('maximum_depth_m').value)
        if minimum < 0.0 or maximum <= minimum:
            raise ValueError('depth range is invalid')
        if float(
            self.get_parameter('bearing_only_uncertainty_m').value
        ) <= 0.0:
            raise ValueError('bearing_only_uncertainty_m must be positive')
        rate = float(self.get_parameter('max_inference_rate_hz').value)
        if rate < 0.0 or not math.isfinite(rate):
            raise ValueError(
                'max_inference_rate_hz must be finite and nonnegative'
            )
        debug_transport = str(
            self.get_parameter('debug_image_transport').value
        )
        if debug_transport not in {'raw', 'compressed', 'both'}:
            raise ValueError(
                'debug_image_transport must be raw, compressed, or both'
            )
        jpeg_quality = int(self.get_parameter('debug_jpeg_quality').value)
        if not 1 <= jpeg_quality <= 100:
            raise ValueError('debug_jpeg_quality must be in [1, 100]')
        opencv_threads = int(
            self.get_parameter('opencv_num_threads').value
        )
        if opencv_threads < 1:
            raise ValueError('opencv_num_threads must be positive')
        if int(
            self.get_parameter('reid_refresh_interval_frames').value
        ) < 1:
            raise ValueError(
                'reid_refresh_interval_frames must be positive'
            )

    def _create_detector(self) -> PersonDetector:
        backend = str(self.get_parameter('detector_backend').value)
        model_path = str(self.get_parameter('model_path').value).strip()
        model_exists = (
            bool(model_path) and Path(model_path).expanduser().is_file()
        )
        if backend == 'yolo' or (backend == 'auto' and model_exists):
            try:
                detector = YoloPersonDetector(
                    model_path=model_path,
                    confidence_threshold=float(
                        self.get_parameter('confidence_threshold').value
                    ),
                    nms_threshold=float(
                        self.get_parameter('nms_threshold').value
                    ),
                    input_size=int(
                        self.get_parameter('yolo_input_size').value
                    ),
                    dnn_target=str(self.get_parameter('dnn_target').value),
                    inference_backend=str(
                        self.get_parameter('inference_backend').value
                    ),
                )
                self.get_logger().info(
                    'Loaded YOLO person model: '
                    f'{Path(model_path).expanduser()} '
                    f'(target={detector.resolved_target})'
                )
                return detector
            except (FileNotFoundError, RuntimeError, ValueError) as error:
                if backend == 'yolo':
                    raise
                self.get_logger().warning(
                    f'YOLO unavailable ({error}); using image-only HOG '
                    'fallback'
                )
        elif backend == 'auto':
            self.get_logger().warning(
                'No YOLO model_path found; using image-only HOG fallback. '
                'Provide a compatible COCO ONNX model for production accuracy.'
            )
        return HogPersonDetector(
            hit_threshold=float(self.get_parameter('hog_hit_threshold').value),
            nms_threshold=float(self.get_parameter('nms_threshold').value),
        )

    def _create_reidentifier(self) -> PersonAppearanceEncoder:
        backend = str(self.get_parameter('reid_backend').value)
        model_path = str(self.get_parameter('reid_model_path').value).strip()
        model_exists = (
            bool(model_path) and Path(model_path).expanduser().is_file()
        )
        minimum_width = int(
            self.get_parameter('reid_minimum_crop_width').value
        )
        minimum_height = int(
            self.get_parameter('reid_minimum_crop_height').value
        )
        if backend == 'osnet' or (backend == 'auto' and model_exists):
            try:
                encoder = OsNetPersonEncoder(
                    model_path=model_path,
                    dnn_target=str(self.get_parameter('dnn_target').value),
                    inference_backend=str(
                        self.get_parameter('inference_backend').value
                    ),
                    minimum_width=minimum_width,
                    minimum_height=minimum_height,
                )
                self.get_logger().info(
                    'Loaded OSNet person Re-ID model: '
                    f'{Path(model_path).expanduser()} '
                    f'(target={encoder.resolved_target})'
                )
                return encoder
            except (FileNotFoundError, RuntimeError, ValueError) as error:
                if backend == 'osnet':
                    raise
                self.get_logger().warning(
                    f'OSNet unavailable ({error}); using HSV appearance '
                    'fallback'
                )
        elif backend == 'auto':
            self.get_logger().warning(
                'No OSNet reid_model_path found; using HSV appearance '
                'fallback. Prepare OSNet for reliable long-term IDs.'
            )
        return HistogramPersonEncoder(
            minimum_width=minimum_width,
            minimum_height=minimum_height,
        )

    def _on_camera_info(self, message: CameraInfo) -> None:
        try:
            CameraIntrinsics.from_camera_matrix(
                message.k,
                message.width,
                message.height,
            )
        except ValueError as error:
            self._warn_periodically(
                'camera_info',
                f'Invalid CameraInfo: {error}',
            )
            return
        self._camera_info = message

    def _should_process(self, message: Image) -> bool:
        rate = float(self.get_parameter('max_inference_rate_hz').value)
        stamp_ns = (
            int(message.header.stamp.sec) * 1_000_000_000
            + int(message.header.stamp.nanosec)
        )
        if stamp_ns <= 0:
            stamp_ns = self.get_clock().now().nanoseconds
        return self._rate_limiter.should_process(stamp_ns, rate)

    def _on_rgb_depth(self, rgb_message: Image, depth_message: Image) -> None:
        if not self._should_process(rgb_message):
            return
        if self._camera_info is None:
            self._publish_health(False)
            self._warn_periodically(
                'missing_camera_info',
                'Waiting for CameraInfo before localizing people',
            )
            return
        try:
            bgr_image = self._bridge.imgmsg_to_cv2(
                rgb_message,
                desired_encoding='bgr8',
            )
            depth_image = self._bridge.imgmsg_to_cv2(
                depth_message,
                desired_encoding='passthrough',
            )
            detections = self._detector.detect(bgr_image)
            self._inference_frame_index += 1
            refresh_interval = int(
                self.get_parameter('reid_refresh_interval_frames').value
            )
            refresh_due = (
                (self._inference_frame_index - 1) % refresh_interval == 0
            )
            appearance_required = (
                refresh_due
                or self._tracker.needs_appearance_features(detections)
            )
            appearance_features = None
            if appearance_required:
                appearance_features = self._reidentifier.encode(
                    bgr_image, detections
                )
            tracks = self._tracker.update(
                detections, appearance_features
            )
            self._publish_detections(
                rgb_message,
                depth_message,
                bgr_image,
                np.asarray(depth_image),
                tracks,
            )
            self._publish_health(True)
        except (CvBridgeError, RuntimeError, ValueError, cv2.error) as error:
            self._publish_health(False)
            self._warn_periodically(
                'processing',
                f'Perception frame failed: {error}',
            )

    def _publish_detections(
        self,
        rgb_message: Image,
        depth_message: Image,
        bgr_image: np.ndarray,
        depth_image: np.ndarray,
        tracks,
    ) -> None:
        detections_2d = Detection2DArray()
        detections_2d.header = rgb_message.header
        for track in tracks:
            detections_2d.detections.append(
                self._make_detection_2d(track, rgb_message)
            )
        self._detections_2d_publisher.publish(detections_2d)

        detections_3d = self._make_detections_3d(
            rgb_message,
            depth_message,
            bgr_image.shape,
            depth_image,
            tracks,
        )
        self._detections_3d_publisher.publish(detections_3d)

        if bool(self.get_parameter('publish_debug_image').value):
            debug_image = self._draw_debug_image(
                bgr_image,
                depth_image,
                depth_message.encoding,
                tracks,
            )
            if self._debug_publisher is not None:
                debug_message = self._bridge.cv2_to_imgmsg(
                    debug_image,
                    encoding='bgr8',
                )
                debug_message.header = rgb_message.header
                self._debug_publisher.publish(debug_message)
            if self._compressed_debug_publisher is not None:
                quality = int(
                    self.get_parameter('debug_jpeg_quality').value
                )
                success, encoded = cv2.imencode(
                    '.jpg',
                    debug_image,
                    [cv2.IMWRITE_JPEG_QUALITY, quality],
                )
                if not success:
                    raise RuntimeError('debug JPEG encoding failed')
                compressed_message = CompressedImage()
                compressed_message.header = rgb_message.header
                compressed_message.format = 'jpeg'
                compressed_message.data = encoded.tobytes()
                self._compressed_debug_publisher.publish(
                    compressed_message
                )

    @staticmethod
    def _make_detection_2d(
        track: TrackedDetection,
        source_message: Image,
    ) -> Detection2D:
        detection = Detection2D()
        detection.header = source_message.header
        detection.id = str(track.track_id)
        box = track.detection.bbox
        center_x, center_y = box.center
        detection.bbox.center.position.x = center_x
        detection.bbox.center.position.y = center_y
        detection.bbox.size_x = box.width
        detection.bbox.size_y = box.height
        hypothesis = ObjectHypothesisWithPose()
        hypothesis.hypothesis.class_id = track.detection.class_id
        hypothesis.hypothesis.score = track.detection.score
        hypothesis.pose.pose.orientation.w = 1.0
        detection.results.append(hypothesis)
        return detection

    def _make_detections_3d(
        self,
        rgb_message: Image,
        depth_message: Image,
        rgb_shape,
        depth_image: np.ndarray,
        tracks,
    ) -> Detection3DArray:
        output = Detection3DArray()
        output.header.stamp = rgb_message.header.stamp
        message_frame = (
            depth_message.header.frame_id
            or rgb_message.header.frame_id
            or self._camera_info.header.frame_id
        )
        # Pixel projection always produces REP-103 optical coordinates even
        # when a simulator labels all RGB-D products with its body frame for
        # PointCloud compatibility.
        source_frame = (
            str(self.get_parameter('projection_frame').value).strip()
            or message_frame
        )
        target_frame = self._output_frame or source_frame
        output.header.frame_id = target_frame
        if not source_frame:
            self._warn_periodically(
                'missing_frame',
                'RGB-D messages have no frame_id; 3D detections are '
                'unavailable',
            )
            return output

        transform = None
        if target_frame != source_frame:
            try:
                transform = self._tf_buffer.lookup_transform(
                    target_frame,
                    source_frame,
                    Time.from_msg(rgb_message.header.stamp),
                    timeout=Duration(seconds=0.08),
                )
            except TransformException as error:
                self._warn_periodically(
                    'transform',
                    f'Cannot transform people from {source_frame} to '
                    f'{target_frame}: {error}',
                )
                return output

        intrinsics = CameraIntrinsics.from_camera_matrix(
            self._camera_info.k,
            self._camera_info.width,
            self._camera_info.height,
        )
        rgb_height, rgb_width = rgb_shape[:2]
        depth_height, depth_width = depth_image.shape[:2]
        scale_x = depth_width / float(rgb_width)
        scale_y = depth_height / float(rgb_height)

        for track in tracks:
            box = track.detection.bbox
            depth_box = BoundingBox(
                box.left * scale_x,
                box.top * scale_y,
                box.right * scale_x,
                box.bottom * scale_y,
            )
            estimate = estimate_roi_depth(
                depth_image,
                depth_message.encoding,
                depth_box,
                roi_scale=float(self.get_parameter('depth_roi_scale').value),
                minimum_depth_m=float(
                    self.get_parameter('minimum_depth_m').value
                ),
                maximum_depth_m=float(
                    self.get_parameter('maximum_depth_m').value
                ),
                minimum_samples=int(
                    self.get_parameter('minimum_depth_samples').value
                ),
                fallback_scale=float(
                    self.get_parameter('fallback_depth_scale').value
                ),
            )
            bearing_only = estimate is None
            if bearing_only and not bool(
                self.get_parameter('enable_bearing_only_fallback').value
            ):
                continue
            if bearing_only:
                # A missing ROI depth while RGB still detects the person is
                # represented at the sensor's far limit. The large covariance
                # tells downstream users that only its bearing and a minimum
                # range are trustworthy.
                distance_m = float(
                    self.get_parameter('maximum_depth_m').value
                )
                depth_dispersion_m = float(
                    self.get_parameter('bearing_only_uncertainty_m').value
                )
            else:
                distance_m = estimate.distance_m
                depth_dispersion_m = estimate.dispersion_m
            center_x, center_y = box.center
            point = project_pixel(
                intrinsics,
                center_x,
                center_y,
                distance_m,
            )
            orientation = Quaternion()
            orientation.w = 1.0
            if transform is not None:
                point = transform_point(point, transform)
                orientation = transform.transform.rotation
            size = projected_box_size(
                intrinsics,
                box,
                distance_m,
                thickness_m=float(
                    self.get_parameter('person_thickness_m').value
                ),
            )
            output.detections.append(
                self._make_detection_3d(
                    track,
                    rgb_message,
                    point,
                    size,
                    depth_dispersion_m,
                    orientation,
                    target_frame,
                )
            )
        return output

    @staticmethod
    def _make_detection_3d(
        track: TrackedDetection,
        source_message: Image,
        point,
        size,
        depth_dispersion: float,
        orientation: Quaternion,
        frame_id: str,
    ) -> Detection3D:
        detection = Detection3D()
        detection.header.stamp = source_message.header.stamp
        detection.header.frame_id = frame_id
        detection.id = str(track.track_id)
        detection.bbox.center.position.x = point[0]
        detection.bbox.center.position.y = point[1]
        detection.bbox.center.position.z = point[2]
        detection.bbox.center.orientation = orientation
        detection.bbox.size.x = size[0]
        detection.bbox.size.y = size[1]
        detection.bbox.size.z = size[2]
        hypothesis = ObjectHypothesisWithPose()
        hypothesis.hypothesis.class_id = track.detection.class_id
        hypothesis.hypothesis.score = track.detection.score
        hypothesis.pose.pose.position = detection.bbox.center.position
        hypothesis.pose.pose.orientation = orientation
        variance = max(0.01, depth_dispersion) ** 2
        hypothesis.pose.covariance[0] = variance
        hypothesis.pose.covariance[7] = variance
        hypothesis.pose.covariance[14] = variance
        detection.results.append(hypothesis)
        return detection

    def _draw_debug_image(
        self,
        bgr_image: np.ndarray,
        depth_image: np.ndarray,
        depth_encoding: str,
        tracks,
    ) -> np.ndarray:
        output = bgr_image.copy()
        image_height, image_width = output.shape[:2]
        depth_height, depth_width = depth_image.shape[:2]
        for track in tracks:
            box = track.detection.bbox
            left = int(max(0, min(image_width - 1, round(box.left))))
            top = int(max(0, min(image_height - 1, round(box.top))))
            right = int(max(0, min(image_width - 1, round(box.right))))
            bottom = int(max(0, min(image_height - 1, round(box.bottom))))
            scaled_box = BoundingBox(
                box.left * depth_width / float(image_width),
                box.top * depth_height / float(image_height),
                box.right * depth_width / float(image_width),
                box.bottom * depth_height / float(image_height),
            )
            estimate = estimate_roi_depth(
                depth_image,
                depth_encoding,
                scaled_box,
                roi_scale=float(self.get_parameter('depth_roi_scale').value),
                minimum_depth_m=float(
                    self.get_parameter('minimum_depth_m').value
                ),
                maximum_depth_m=float(
                    self.get_parameter('maximum_depth_m').value
                ),
                minimum_samples=int(
                    self.get_parameter('minimum_depth_samples').value
                ),
                fallback_scale=float(
                    self.get_parameter('fallback_depth_scale').value
                ),
            )
            label = f'person #{track.track_id} {track.detection.score:.2f}'
            if estimate is not None:
                label += f' {estimate.distance_m:.2f}m'
            else:
                maximum_depth = float(
                    self.get_parameter('maximum_depth_m').value
                )
                label += f' >{maximum_depth:.1f}m RGB-bearing'
            cv2.rectangle(output, (left, top), (right, bottom), (0, 220, 0), 2)
            cv2.putText(
                output,
                label,
                (left, max(18, top - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 220, 0),
                2,
                cv2.LINE_AA,
            )
        return output

    def _publish_health(self, healthy: bool) -> None:
        message = Bool()
        message.data = healthy
        self._health_publisher.publish(message)

    def _warn_periodically(self, key: str, message: str) -> None:
        now_ns = self.get_clock().now().nanoseconds
        last_ns = self._warning_times.get(key)
        if last_ns is None or now_ns - last_ns >= 5_000_000_000:
            self.get_logger().warning(message)
            self._warning_times[key] = now_ns


def main(args=None) -> int:
    """Run the person localizer node."""
    rclpy.init(args=args)
    node = None
    try:
        node = PersonLocalizerNode()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f'person_localizer startup failed: {error}', file=sys.stderr)
        return 2
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
