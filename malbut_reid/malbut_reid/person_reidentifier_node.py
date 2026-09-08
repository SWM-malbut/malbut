"""Publish shared person identities from matching RGB and YOLO messages."""

from pathlib import Path
import sys

import cv2
from cv_bridge import CvBridge, CvBridgeError
import message_filters
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image
from vision_msgs.msg import Detection2DArray
from yolo_msgs.msg import DetectionArray

from .adapter import identified_detections, person_detections
from .reid import HistogramPersonEncoder, OsNetPersonEncoder
from .tracker import ByteTrackTracker


class PersonReidentifierNode(Node):
    """Keep a process-lifetime identity gallery independent of follow goals."""

    def __init__(self) -> None:
        """Load the encoder once and subscribe to shared detections."""
        super().__init__('person_reidentifier')
        defaults = {
            'rgb_topic': '/camera/color/image_raw',
            'yolo_detections_topic': '/yolo/detections',
            'detections_2d_topic': '/perception/person/detections_2d',
            'sync_queue_size': 60,
            'opencv_num_threads': 4,
            'reid_backend': 'auto',
            'reid_model_path': str(
                Path.home() / '.cache' / 'malbut_perception'
                / 'osnet_ain_x1_0_msmt17.onnx'
            ),
            'inference_backend': 'auto',
            'dnn_target': 'auto',
            'reid_cosine_threshold': 0.35,
            'reid_max_inactive_frames': 0,
            'reid_feature_budget': 30,
            'reid_refresh_interval_frames': 3,
            'reid_minimum_crop_width': 16,
            'reid_minimum_crop_height': 32,
            'tracker_high_threshold': 0.45,
            'tracker_low_threshold': 0.15,
            'tracker_iou_threshold': 0.30,
            'tracker_max_missed_frames': 15,
            'tracker_min_confirmed_hits': 2,
            'tracker_appearance_threshold': 0.35,
            'tracker_appearance_weight': 0.65,
        }
        for name, value in defaults.items():
            self.declare_parameter(name, value)
        self._validate_parameters()
        cv2.setNumThreads(self.get_parameter('opencv_num_threads').value)
        self._bridge = CvBridge()
        self._encoder = self._create_encoder()
        self._tracker = ByteTrackTracker(
            high_threshold=self.get_parameter('tracker_high_threshold').value,
            low_threshold=self.get_parameter('tracker_low_threshold').value,
            match_iou_threshold=self.get_parameter(
                'tracker_iou_threshold').value,
            max_missed_frames=self.get_parameter(
                'tracker_max_missed_frames').value,
            min_confirmed_hits=self.get_parameter(
                'tracker_min_confirmed_hits').value,
            appearance_threshold=self.get_parameter(
                'tracker_appearance_threshold').value,
            appearance_weight=self.get_parameter(
                'tracker_appearance_weight').value,
            reid_threshold=self.get_parameter('reid_cosine_threshold').value,
            reid_max_inactive_frames=self.get_parameter(
                'reid_max_inactive_frames').value,
            feature_budget=self.get_parameter('reid_feature_budget').value,
        )
        self._frame_index = 0
        self._refresh_interval = self.get_parameter(
            'reid_refresh_interval_frames').value
        self._publisher = self.create_publisher(
            Detection2DArray,
            self.get_parameter('detections_2d_topic').value, 10,
        )
        self._rgb = message_filters.Subscriber(
            self, Image, self.get_parameter('rgb_topic').value,
            qos_profile=qos_profile_sensor_data,
        )
        self._detections = message_filters.Subscriber(
            self, DetectionArray,
            self.get_parameter('yolo_detections_topic').value,
            qos_profile=qos_profile_sensor_data,
        )
        # YOLO keeps the original RGB stamp. Exact synchronization retains that
        # image while inference runs; it never crops a newer, unrelated frame.
        self._synchronizer = message_filters.TimeSynchronizer(
            [self._rgb, self._detections],
            queue_size=self.get_parameter('sync_queue_size').value,
        )
        self._synchronizer.registerCallback(self._on_rgb_detections)
        self.get_logger().info(
            'Person re-identification ready: YOLO boxes + matching RGB; '
            'identity memory is independent of tracking missions.'
        )

    def _validate_parameters(self) -> None:
        for name in ('sync_queue_size', 'opencv_num_threads',
                     'reid_refresh_interval_frames', 'reid_minimum_crop_width',
                     'reid_minimum_crop_height'):
            if self.get_parameter(name).value < 1:
                raise ValueError(f'{name} must be positive')
        if self.get_parameter('reid_backend').value not in {
            'auto', 'osnet', 'histogram',
        }:
            raise ValueError('reid_backend must be auto, osnet, or histogram')
        for name in (
            'rgb_topic', 'yolo_detections_topic', 'detections_2d_topic',
        ):
            if not self.get_parameter(name).value:
                raise ValueError(f'{name} must not be empty')

    def _create_encoder(self):
        backend = self.get_parameter('reid_backend').value
        model_path = self.get_parameter('reid_model_path').value.strip()
        model_exists = (
            bool(model_path) and Path(model_path).expanduser().is_file()
        )
        minimum_width = self.get_parameter('reid_minimum_crop_width').value
        minimum_height = self.get_parameter('reid_minimum_crop_height').value
        if backend == 'osnet' or (backend == 'auto' and model_exists):
            try:
                encoder = OsNetPersonEncoder(
                    model_path=model_path,
                    dnn_target=self.get_parameter('dnn_target').value,
                    inference_backend=self.get_parameter(
                        'inference_backend').value,
                    minimum_width=minimum_width,
                    minimum_height=minimum_height,
                )
                self.get_logger().info(
                    f'OSNet loaded: {model_path} ({encoder.resolved_target})'
                )
                return encoder
            except (FileNotFoundError, RuntimeError, ValueError) as error:
                if backend == 'osnet':
                    raise
                self.get_logger().warning(f'OSNet unavailable: {error}')
        if backend == 'auto':
            self.get_logger().warning(
                'Using the existing HSV fallback; prepare OSNet and select '
                'reid_backend=osnet to require learned appearance features.'
            )
        return HistogramPersonEncoder(
            minimum_width=minimum_width, minimum_height=minimum_height,
        )

    def _on_rgb_detections(
        self, image: Image, message: DetectionArray,
    ) -> None:
        if image.header != message.header:
            self.get_logger().warning(
                'YOLO/RGB headers differ; refusing mismatched image crops.',
                throttle_duration_sec=5.0,
            )
            return
        try:
            detections = person_detections(message)
            self._frame_index += 1
            refresh_due = (self._frame_index - 1) % self._refresh_interval == 0
            features = None
            if detections and (
                refresh_due
                or self._tracker.needs_appearance_features(detections)
            ):
                bgr = self._bridge.imgmsg_to_cv2(
                    image, desired_encoding='bgr8',
                )
                features = self._encoder.encode(bgr, detections)
            tracks = self._tracker.update(detections, features)
            self._publisher.publish(
                identified_detections(image.header, tracks)
            )
        except (CvBridgeError, RuntimeError, ValueError, cv2.error) as error:
            self.get_logger().warning(
                f'Person re-identification frame failed: {error}',
                throttle_duration_sec=5.0,
            )


def main(args=None) -> int:
    """Run the shared person re-identification node."""
    rclpy.init(args=args)
    node = None
    try:
        node = PersonReidentifierNode()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f'person_reidentifier startup failed: {error}', file=sys.stderr)
        return 2
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
