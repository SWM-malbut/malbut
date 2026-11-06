"""Manage detector demand and monotonic diagnostics around upstream inference."""

import gc
import math
import time

import cv2
from malbut_interfaces.msg import SensorProcessingTrace
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn
from sensor_msgs.msg import Image
from std_srvs.srv import SetBool
from yolo_ros.yolo_node import YoloNode


class TimedYoloNode(YoloNode):
    """Use upstream inference and messages; add no second detector/tracker."""

    def __init__(self):
        """Create optional diagnostics without changing the image contract."""
        super().__init__()
        self.declare_parameter('start_enabled', True)
        timeout = self.declare_parameter('idle_model_timeout_s', 60.0).value
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError('idle_model_timeout_s must be finite and nonnegative')
        self._idle_timeout = float(timeout)
        self._idle_since = None
        self._activated = False
        self._processing_enabled = False
        self._sub = None
        self._enable_srv = None
        self._set_classes_srv = None
        self._classes = None
        self._idle_timer = self.create_timer(1.0, self._expire_model)
        self._idle_timer.cancel()
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
        """Activate ROS endpoints; defer weights until processing is requested."""
        self._enable_srv = self.create_service(SetBool, 'enable', self.enable_cb)
        LifecycleNode.on_activate(self, state)
        self._activated = True
        try:
            self._change_processing(bool(self.get_parameter('start_enabled').value))
        except Exception as error:
            self.get_logger().error(f'Detector activation failed: {error}')
            self.on_deactivate(state)
            return TransitionCallbackReturn.ERROR
        return TransitionCallbackReturn.SUCCESS

    def _load_model(self):
        if hasattr(self, 'yolo'):
            return
        # Reuse the upstream model factory, prediction settings and result parser.
        model = self.type_to_model[self.model_type](self.model)
        model.to(self.device)
        if self.fuse_model and self.model_type != 'YOLOE':
            try:
                model.fuse()
            except TypeError as error:
                self.get_logger().warning(f'Error while fuse: {error}')
        if self._classes is not None:
            model.set_classes(self._classes)
        if hasattr(model, 'set_classes') and self._set_classes_srv is None:
            from yolo_msgs.srv import SetClasses

            self._set_classes_srv = self.create_service(
                SetClasses, 'set_classes', self.set_classes_cb)
        self.yolo = model

    def _release_model(self):
        if hasattr(self, 'yolo'):
            del self.yolo
            gc.collect()
            if 'cuda' in self.device:
                import torch

                torch.cuda.empty_cache()

    def _expire_model(self):
        if (self._idle_since is not None
                and not (self._processing_enabled and self.enable)
                and time.monotonic() - self._idle_since >= self._idle_timeout):
            self._release_model()
            self._idle_timer.cancel()

    def enable_cb(self, request, response):
        """Preserve upstream enable service without overriding Manager demand."""
        previous = self.enable
        self.enable = request.data
        try:
            self._change_processing(self._processing_enabled)
        except Exception as error:
            self.enable = previous
            response.success, response.message = False, str(error)
            return response
        response.success = True
        return response

    def set_classes_cb(self, request, response):
        """Retain explicit class selection across an idle model release."""
        try:
            self._load_model()
            self.yolo.set_classes(request.classes)
            self._classes = list(request.classes)
        except Exception as error:
            self.get_logger().error(f'Error setting classes: {error}')
        finally:
            if not (self._processing_enabled and self.enable):
                self._idle_since = time.monotonic()
                self._idle_timer.reset()
        return response

    def on_deactivate(self, state):
        """Release weights and endpoints without assuming they were ever loaded."""
        self._change_processing(False)
        self._idle_timer.cancel()
        self._release_model()
        for name in ('_enable_srv', '_set_classes_srv'):
            service = getattr(self, name)
            if service is not None:
                self.destroy_service(service)
                setattr(self, name, None)
        self._activated = False
        return LifecycleNode.on_deactivate(self, state)

    def _change_processing(self, enabled):
        wanted = enabled and self.enable
        if wanted:
            self._load_model()
            self._idle_timer.cancel()
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = time.monotonic()
            if hasattr(self, 'yolo'):
                self._idle_timer.reset()
        if wanted and self._sub is None:
            self._sub = self.create_subscription(
                Image, 'image_raw', self.image_cb, self.image_qos_profile)
        elif not wanted and self._sub is not None:
            self.destroy_subscription(self._sub)
            self._sub = None
        self._processing_enabled = enabled

    def _set_processing(self, request, response):
        if not self._activated:
            response.success, response.message = False, 'detector is not configured'
            return response
        try:
            self._change_processing(request.data)
        except Exception as error:
            response.success, response.message = False, f'detector load failed: {error}'
            return response
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
