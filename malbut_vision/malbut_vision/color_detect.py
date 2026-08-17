"""Detect the largest red object in a ROS 2 camera image."""

import cv2
from cv_bridge import CvBridge, CvBridgeError
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image


class ColorDetectNode(Node):
    """Apply a LAB mask and highlight the largest valid red contour."""

    WINDOW_NAME = 'Malbut Color Detection'
    PROCESS_SIZE = (320, 240)

    def __init__(self) -> None:
        super().__init__('color_detect')
        self.declare_parameter('image_topic', '/depth_cam/depth_cam')
        self.declare_parameter(
            'output_topic',
            '/malbut_vision/red_detection/image',
        )
        self.declare_parameter('lab_lower', [33, 151, 130])
        self.declare_parameter('lab_upper', [255, 255, 255])
        self.declare_parameter('min_area', 50.0)

        self.image_topic = self.get_parameter('image_topic').value
        self.output_topic = self.get_parameter('output_topic').value
        self.lab_lower = self._array_parameter('lab_lower')
        self.lab_upper = self._array_parameter('lab_upper')
        self.min_area = float(self.get_parameter('min_area').value)

        self.bridge = CvBridge()
        self._was_detected = False
        self.publisher = self.create_publisher(
            Image,
            self.output_topic,
            qos_profile_sensor_data,
        )
        self.subscription = self.create_subscription(
            Image,
            self.image_topic,
            self._image_callback,
            qos_profile_sensor_data,
        )

        cv2.namedWindow(self.WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.WINDOW_NAME, 700, 360)
        self.get_logger().info(
            f'Detecting red objects on {self.image_topic}; '
            f'LAB {self.lab_lower.tolist()} to {self.lab_upper.tolist()}'
        )
        self.get_logger().info('Press Q or Esc in the image window to quit.')

    def _array_parameter(self, name: str) -> np.ndarray:
        values = self.get_parameter(name).value
        if len(values) != 3:
            raise ValueError(f'{name} must contain exactly three integers')
        return np.array(values, dtype=np.uint8)

    def _largest_contour(self, mask: np.ndarray):
        contours = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_NONE,
        )[-2]
        if not contours:
            return None, 0.0

        contour = max(contours, key=cv2.contourArea)
        area = float(abs(cv2.contourArea(contour)))
        if area <= self.min_area:
            return None, area
        return contour, area

    @staticmethod
    def _draw_detection(
        image: np.ndarray,
        contour,
        area: float,
    ) -> None:
        (center_x, center_y), radius = cv2.minEnclosingCircle(contour)
        center = (int(center_x), int(center_y))
        radius = max(1, int(radius))

        cv2.circle(image, center, radius, (0, 0, 255), 2)
        cv2.circle(image, center, 4, (255, 255, 255), -1)
        cv2.putText(
            image,
            f'RED  center={center}  area={area:.0f}',
            (8, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )

    def _process(self, source: np.ndarray):
        resized = cv2.resize(
            source,
            self.PROCESS_SIZE,
            interpolation=cv2.INTER_NEAREST,
        )
        blurred = cv2.GaussianBlur(resized, (3, 3), 3)
        lab_image = cv2.cvtColor(blurred, cv2.COLOR_BGR2LAB)
        mask = cv2.inRange(lab_image, self.lab_lower, self.lab_upper)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        eroded = cv2.erode(mask, kernel)
        cleaned_mask = cv2.dilate(eroded, kernel)
        contour, area = self._largest_contour(cleaned_mask)

        annotated = resized.copy()
        if contour is not None:
            self._draw_detection(annotated, contour, area)
        else:
            cv2.putText(
                annotated,
                'RED not detected',
                (8, 24),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (180, 180, 180),
                2,
                cv2.LINE_AA,
            )
        return annotated, cleaned_mask, contour is not None, area

    def _log_state_change(self, detected: bool, area: float) -> None:
        if detected and not self._was_detected:
            self.get_logger().info(f'Red object detected, area={area:.0f}')
        elif not detected and self._was_detected:
            self.get_logger().info('Red object lost')
        self._was_detected = detected

    def _publish_result(self, image: np.ndarray, source: Image) -> None:
        result_message = self.bridge.cv2_to_imgmsg(image, encoding='bgr8')
        result_message.header = source.header
        self.publisher.publish(result_message)

    def _image_callback(self, message: Image) -> None:
        try:
            source = self.bridge.imgmsg_to_cv2(
                message,
                desired_encoding='bgr8',
            )
        except CvBridgeError as error:
            self.get_logger().error(f'Image conversion failed: {error}')
            return

        annotated, mask, detected, area = self._process(source)
        self._log_state_change(detected, area)
        self._publish_result(annotated, message)

        mask_bgr = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        display = np.hstack((annotated, mask_bgr))
        cv2.imshow(self.WINDOW_NAME, display)
        key = cv2.waitKey(1) & 0xFF
        if key in (ord('q'), ord('Q'), 27):
            self.get_logger().info('Closing color detector')
            rclpy.shutdown()

    def destroy_node(self) -> None:
        cv2.destroyAllWindows()
        super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = ColorDetectNode()
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
