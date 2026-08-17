"""Interactive LAB color-threshold viewer for a ROS 2 image topic."""

from typing import Dict, Tuple

import cv2
from cv_bridge import CvBridge, CvBridgeError
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image


class LabThresholdNode(Node):
    """Display the source image, LAB mask, and masked result."""

    WINDOW_NAME = 'Malbut LAB Threshold'
    PANEL_SIZE = (320, 200)
    TRACKBARS = (
        ('L min', 0),
        ('L max', 255),
        ('A min', 0),
        ('A max', 255),
        ('B min', 0),
        ('B max', 255),
    )

    def __init__(self) -> None:
        super().__init__('lab_threshold')
        self.declare_parameter('image_topic', '/depth_cam/depth_cam')
        image_topic = (
            self.get_parameter('image_topic')
            .get_parameter_value()
            .string_value
        )

        self.bridge = CvBridge()
        self._create_window()
        self.subscription = self.create_subscription(
            Image,
            image_topic,
            self._image_callback,
            qos_profile_sensor_data,
        )

        self.get_logger().info(f'Subscribed to {image_topic}')
        self.get_logger().info(
            'Adjust the six sliders. Press P to print values; Q or Esc to quit.'
        )

    @staticmethod
    def _trackbar_callback(_value: int) -> None:
        """Provide the callback OpenCV requires when polling trackbar values."""

    def _create_window(self) -> None:
        cv2.namedWindow(self.WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.WINDOW_NAME, 1000, 520)
        for name, initial_value in self.TRACKBARS:
            cv2.createTrackbar(
                name,
                self.WINDOW_NAME,
                initial_value,
                255,
                self._trackbar_callback,
            )

    def _thresholds(self) -> Dict[str, int]:
        return {
            name: cv2.getTrackbarPos(name, self.WINDOW_NAME)
            for name, _initial_value in self.TRACKBARS
        }

    @staticmethod
    def _label(panel: np.ndarray, text: str) -> np.ndarray:
        labelled = panel.copy()
        cv2.rectangle(labelled, (0, 0), (320, 34), (0, 0, 0), -1)
        cv2.putText(
            labelled,
            text,
            (10, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        return labelled

    def _make_display(
        self,
        source: np.ndarray,
        mask: np.ndarray,
        result: np.ndarray,
        thresholds: Dict[str, int],
    ) -> np.ndarray:
        source_panel = cv2.resize(source, self.PANEL_SIZE)
        mask_panel = cv2.resize(mask, self.PANEL_SIZE)
        mask_panel = cv2.cvtColor(mask_panel, cv2.COLOR_GRAY2BGR)
        result_panel = cv2.resize(result, self.PANEL_SIZE)

        panels = (
            self._label(source_panel, 'Original'),
            self._label(mask_panel, 'LAB mask'),
            self._label(result_panel, 'Result'),
        )
        display = np.hstack(panels)
        threshold_text = (
            f"L [{thresholds['L min']}, {thresholds['L max']}]   "
            f"A [{thresholds['A min']}, {thresholds['A max']}]   "
            f"B [{thresholds['B min']}, {thresholds['B max']}]"
        )
        cv2.putText(
            display,
            threshold_text,
            (10, display.shape[0] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
        return display

    @staticmethod
    def _bounds(
        thresholds: Dict[str, int],
    ) -> Tuple[np.ndarray, np.ndarray]:
        lower = np.array(
            [
                thresholds['L min'],
                thresholds['A min'],
                thresholds['B min'],
            ],
            dtype=np.uint8,
        )
        upper = np.array(
            [
                thresholds['L max'],
                thresholds['A max'],
                thresholds['B max'],
            ],
            dtype=np.uint8,
        )
        return lower, upper

    def _image_callback(self, message: Image) -> None:
        try:
            source = self.bridge.imgmsg_to_cv2(
                message,
                desired_encoding='bgr8',
            )
        except CvBridgeError as error:
            self.get_logger().error(f'Image conversion failed: {error}')
            return

        thresholds = self._thresholds()
        lower, upper = self._bounds(thresholds)
        lab_image = cv2.cvtColor(source, cv2.COLOR_BGR2LAB)
        mask = cv2.inRange(lab_image, lower, upper)
        result = cv2.bitwise_and(source, source, mask=mask)

        display = self._make_display(source, mask, result, thresholds)
        cv2.imshow(self.WINDOW_NAME, display)
        key = cv2.waitKey(1) & 0xFF
        if key in (ord('p'), ord('P')):
            self.get_logger().info(
                f'LAB thresholds: lower={lower.tolist()}, '
                f'upper={upper.tolist()}'
            )
        elif key in (ord('q'), ord('Q'), 27):
            self.get_logger().info('Closing LAB threshold viewer')
            rclpy.shutdown()

    def destroy_node(self) -> None:
        cv2.destroyAllWindows()
        super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = LabThresholdNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if rclpy.ok():
            rclpy.shutdown()
        node.destroy_node()


if __name__ == '__main__':
    main()
