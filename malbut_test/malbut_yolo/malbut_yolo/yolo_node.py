"""Add monotonic diagnostics around the unmodified upstream detector."""

import time

import cv2
from malbut_interfaces.msg import SensorProcessingTrace
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.lifecycle import TransitionCallbackReturn
from sensor_msgs.msg import Image
from std_srvs.srv import SetBool
from yolo_ros.yolo_node import YoloNode


class TimedYoloNode(YoloNode):
    """Use upstream inference and messages; add no second detector/tracker."""

    def __init__(self):
        """Create optional diagnostics without changing the image contract."""
        super().__init__()
        self.declare_parameter('start_enabled', True)
        self._processing_enabled = True
        self.create_service(SetBool, '~/set_processing', self._set_processing)
        self.declare_parameter('opencv_num_threads', 4)
        self.declare_parameter(
            'processing_trace_topic', '/perception/yolo_processing_trace'
        )
        cv2.setNumThreads(int(self.get_parameter('opencv_num_threads').value))
        self._timing_sequence = 0
        self._timing_pub = self.create_publisher(
            SensorProcessingTrace,
            str(self.get_parameter('processing_trace_topic').value), 100,
        )

    def on_activate(self, state):
        """Load once, then optionally stop RGB input without unloading CUDA."""
        result = super().on_activate(state)
        if result == TransitionCallbackReturn.SUCCESS:
            self._change_processing(bool(self.get_parameter('start_enabled').value))
        return result

    def _change_processing(self, enabled):
        if enabled and self._sub is None:
            self._sub = self.create_subscription(
                Image, 'image_raw', self.image_cb, self.image_qos_profile)
        elif not enabled and self._sub is not None:
            self.destroy_subscription(self._sub)
            self._sub = None
        self._processing_enabled = enabled

    def _set_processing(self, request, response):
        if not hasattr(self, 'yolo'):
            response.success, response.message = False, 'detector is not configured'
            return response
        self._change_processing(request.data)
        response.success = True
        return response

    def image_cb(self, message):
        """Measure before inference, retaining the exact source RGB stamp."""
        if not self._processing_enabled or not self.enable:
            return
        received_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        super().image_cb(message)
        self._timing_sequence += 1
        trace = SensorProcessingTrace()
        trace.source_stamp = message.header.stamp
        trace.receipt_steady_time_ns = received_ns
        trace.publish_steady_time_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        trace.sequence = self._timing_sequence
        trace.source = 'camera'
        self._timing_pub.publish(trace)


def main(args=None):
    """Start one detector for the lifetime of this Bringup process."""
    rclpy.init(args=args)
    node = None
    try:
        node = TimedYoloNode()
        if node.trigger_configure() != TransitionCallbackReturn.SUCCESS:
            raise RuntimeError('upstream YOLO configure failed')
        if node.trigger_activate() != TransitionCallbackReturn.SUCCESS:
            raise RuntimeError('upstream YOLO activation failed')
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
