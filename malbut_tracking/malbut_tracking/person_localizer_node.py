"""Project shared person identity observations using aligned RGB-D data."""

from dataclasses import dataclass
import math
import sys
import time
from typing import Dict, Optional, Tuple

import cv2
from cv_bridge import CvBridge, CvBridgeError
from geometry_msgs.msg import Quaternion, TransformStamped
from malbut_interfaces.msg import SensorProcessingTrace
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
    Detection2DArray,
    Detection3D,
    Detection3DArray,
    ObjectHypothesisWithPose,
)

from .depth.observation import BoundingBox, PersonBox
from .depth.projector import (
    CameraIntrinsics,
    project_pixel,
    projected_box_size,
)
from .depth.roi_depth import estimate_roi_depth
from .depth.timing import CameraTiming


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


@dataclass(frozen=True)
class AlignedImages:
    """Hold an aligned RGB-D pair under the original RGB timestamp."""

    rgb: Image
    depth: Image

    @property
    def header(self):
        """Expose the source header for the ROS exact-time synchronizer."""
        return self.rgb.header


class PersonLocalizerNode(Node):
    """Localize observed person identities without detection or Re-ID models."""

    def __init__(self) -> None:
        """Subscribe to shared identities and aligned sensor observations."""
        super().__init__('person_localizer')
        self._declare_parameters()
        self._validate_parameters()
        cv2.setNumThreads(self.get_parameter('opencv_num_threads').value)
        self._bridge = CvBridge()
        self._camera_info: Optional[CameraInfo] = None
        self._warning_times: Dict[str, int] = {}
        self._output_frame = self.get_parameter('output_frame').value
        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self._timing = CameraTiming()

        self._detections_3d_publisher = self.create_publisher(
            Detection3DArray,
            self.get_parameter('detections_3d_topic').value, 10,
        )
        self._health_publisher = self.create_publisher(
            Bool, self.get_parameter('health_topic').value, 10,
        )
        self._processing_trace_publisher = self.create_publisher(
            SensorProcessingTrace,
            self.get_parameter('processing_trace_topic').value, 100,
        )
        self._trace_subscription = self.create_subscription(
            SensorProcessingTrace,
            self.get_parameter('yolo_processing_trace_topic').value,
            self._on_yolo_trace, 100,
        )
        self._debug_publisher = None
        self._compressed_debug_publisher = None
        debug_transport = self.get_parameter('debug_image_transport').value
        if debug_transport in {'raw', 'both'}:
            self._debug_publisher = self.create_publisher(
                Image, self.get_parameter('debug_image_topic').value,
                qos_profile_sensor_data,
            )
        if debug_transport in {'compressed', 'both'}:
            self._compressed_debug_publisher = self.create_publisher(
                CompressedImage,
                self.get_parameter('compressed_debug_image_topic').value,
                qos_profile_sensor_data,
            )
        self._camera_info_subscription = self.create_subscription(
            CameraInfo, self.get_parameter('camera_info_topic').value,
            self._on_camera_info, qos_profile_sensor_data,
        )
        self._rgb_subscription = message_filters.Subscriber(
            self, Image, self.get_parameter('rgb_topic').value,
            qos_profile=qos_profile_sensor_data,
        )
        self._depth_subscription = message_filters.Subscriber(
            self, Image, self.get_parameter('depth_topic').value,
            qos_profile=qos_profile_sensor_data,
        )
        self._rgb_depth_sync = message_filters.ApproximateTimeSynchronizer(
            [self._rgb_subscription, self._depth_subscription],
            queue_size=self.get_parameter('sync_queue_size').value,
            slop=self.get_parameter('sync_slop_sec').value,
        )
        self._aligned_images = message_filters.SimpleFilter()
        self._rgb_depth_sync.registerCallback(self._on_aligned_images)
        self._identities = message_filters.Subscriber(
            self, Detection2DArray,
            self.get_parameter('detections_2d_topic').value,
            qos_profile=qos_profile_sensor_data,
        )
        self._identity_sync = message_filters.TimeSynchronizer(
            [self._aligned_images, self._identities],
            queue_size=self.get_parameter('identity_sync_queue_size').value,
        )
        self._identity_sync.registerCallback(self._on_observation)
        self.get_logger().info(
            'Person localizer ready: shared Re-ID observations + aligned '
            'RGB-D. No detector or identity gallery runs in this node.'
        )

    def _declare_parameters(self) -> None:
        defaults = {
            'rgb_topic': '/camera/color/image_raw',
            'depth_topic': '/camera/depth/image_raw',
            'camera_info_topic': '/camera/color/camera_info',
            'detections_2d_topic': '/perception/person/detections_2d',
            'detections_3d_topic': '/perception/person/detections_3d',
            'debug_image_topic': '/perception/person/debug_image',
            'compressed_debug_image_topic':
                '/perception/person/debug_image/compressed',
            'health_topic': '/perception/person/healthy',
            'processing_trace_topic': '/perception/sensor_processing_trace',
            'yolo_processing_trace_topic': '/perception/yolo_processing_trace',
            'projection_frame': '',
            'output_frame': '',
            'opencv_num_threads': 4,
            'depth_roi_scale': 0.45,
            'minimum_depth_m': 0.30,
            'maximum_depth_m': 3.0,
            'minimum_depth_samples': 20,
            'fallback_depth_scale': 1.0,
            'enable_bearing_only_fallback': True,
            'bearing_only_uncertainty_m': 2.0,
            'person_thickness_m': 0.35,
            'sync_queue_size': 3,
            'sync_slop_sec': 0.08,
            'identity_sync_queue_size': 60,
            'publish_debug_image': True,
            'debug_image_transport': 'compressed',
            'debug_jpeg_quality': 80,
        }
        for name, value in defaults.items():
            self.declare_parameter(name, value)

    def _validate_parameters(self) -> None:
        for name in (
            'rgb_topic', 'depth_topic', 'camera_info_topic',
            'detections_2d_topic', 'detections_3d_topic', 'debug_image_topic',
            'compressed_debug_image_topic', 'health_topic',
            'processing_trace_topic', 'yolo_processing_trace_topic',
        ):
            if not self.get_parameter(name).value:
                raise ValueError(f'{name} must not be empty')
        for name in (
            'opencv_num_threads', 'sync_queue_size',
            'identity_sync_queue_size', 'minimum_depth_samples',
        ):
            if self.get_parameter(name).value < 1:
                raise ValueError(f'{name} must be positive')
        minimum = self.get_parameter('minimum_depth_m').value
        maximum = self.get_parameter('maximum_depth_m').value
        if not all(math.isfinite(v) for v in (minimum, maximum)) or (
            minimum < 0.0 or maximum <= minimum
        ):
            raise ValueError('depth range is invalid')
        if self.get_parameter('bearing_only_uncertainty_m').value <= 0.0:
            raise ValueError('bearing_only_uncertainty_m must be positive')
        if not 0.0 < self.get_parameter('depth_roi_scale').value <= 1.0:
            raise ValueError('depth_roi_scale must be in (0, 1]')
        if self.get_parameter('debug_image_transport').value not in {
            'raw', 'compressed', 'both',
        }:
            raise ValueError('debug_image_transport must be raw/compressed/both')
        if not 1 <= self.get_parameter('debug_jpeg_quality').value <= 100:
            raise ValueError('debug_jpeg_quality must be in [1, 100]')

    def _on_aligned_images(self, rgb: Image, depth: Image) -> None:
        self._aligned_images.signalMessage(AlignedImages(rgb, depth))

    def _on_yolo_trace(self, trace: SensorProcessingTrace) -> None:
        # A diagnostic topic can arrive after the result. Neither delivery order
        # nor a missing diagnostic message may block localization itself.
        complete = self._timing.received(trace)
        if complete is not None:
            self._processing_trace_publisher.publish(complete)

    def _on_observation(
        self, images: AlignedImages, detections: Detection2DArray,
    ) -> None:
        if images.header != detections.header:
            self._warn_periodically(
                'identity_header',
                'Identity/RGB headers differ; refusing mismatched projection.',
            )
            return
        if self._camera_info is None:
            self._publish_health(False)
            self._warn_periodically(
                'missing_camera_info',
                'Waiting for CameraInfo before localizing people',
            )
            return
        try:
            people = [
                PersonBox.from_message(detection)
                for detection in detections.detections
                if detection.results
                and detection.results[0].hypothesis.class_id == 'person'
            ]
            depth = np.asarray(self._bridge.imgmsg_to_cv2(
                images.depth, desired_encoding='passthrough',
            ))
            rgb = images.rgb
            output = self._make_detections_3d(
                rgb, images.depth, (rgb.height, rgb.width, 3), depth, people,
            )
            self._detections_3d_publisher.publish(output)
            published_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
            complete = self._timing.published(rgb.header.stamp, published_ns)
            if complete is not None:
                self._processing_trace_publisher.publish(complete)
            self._publish_health(True)
            if self.get_parameter('publish_debug_image').value:
                bgr = self._bridge.imgmsg_to_cv2(
                    rgb, desired_encoding='bgr8',
                )
                self._publish_debug(rgb, images.depth, bgr, depth, people)
        except (CvBridgeError, RuntimeError, ValueError, cv2.error) as error:
            self._publish_health(False)
            self._warn_periodically(
                'processing', f'Person localization frame failed: {error}',
            )

    def _publish_debug(self, rgb_message, depth_message, bgr, depth, people):
        debug_image = self._draw_debug_image(
            bgr, depth, depth_message.encoding, people,
        )
        if self._debug_publisher is not None:
            debug_message = self._bridge.cv2_to_imgmsg(
                debug_image, encoding='bgr8',
            )
            debug_message.header = rgb_message.header
            self._debug_publisher.publish(debug_message)
        if self._compressed_debug_publisher is not None:
            quality = self.get_parameter('debug_jpeg_quality').value
            success, encoded = cv2.imencode(
                '.jpg', debug_image, [cv2.IMWRITE_JPEG_QUALITY, quality],
            )
            if not success:
                raise RuntimeError('debug JPEG encoding failed')
            compressed = CompressedImage()
            compressed.header = rgb_message.header
            compressed.format = 'jpeg'
            compressed.data = encoded.tobytes()
            self._compressed_debug_publisher.publish(compressed)

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
            box = track.bbox
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
        track: PersonBox,
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
        hypothesis.hypothesis.class_id = track.class_id
        hypothesis.hypothesis.score = track.score
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
            box = track.bbox
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
            label = f'person #{track.track_id} {track.score:.2f}'
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
