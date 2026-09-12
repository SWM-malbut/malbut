"""Check preserved RGB-D projection and independent diagnostic timing."""

from types import MethodType, SimpleNamespace

from cv_bridge import CvBridge
from malbut_interfaces.msg import SensorProcessingTrace
import numpy as np
import pytest
from sensor_msgs.msg import CameraInfo
from vision_msgs.msg import Detection2D, Detection2DArray, ObjectHypothesisWithPose

from malbut_tracking.depth.observation import BoundingBox, PersonBox
from malbut_tracking.depth.timing import CameraTiming
from malbut_tracking.person_localizer_node import AlignedImages, PersonLocalizerNode


def _fixture(depth_value=2.0):
    bridge = CvBridge()
    rgb = bridge.cv2_to_imgmsg(
        np.zeros((480, 640, 3), dtype=np.uint8), encoding='bgr8',
    )
    rgb.header.frame_id = 'camera_body'
    rgb.header.stamp.sec = 7
    rgb.header.stamp.nanosec = 123
    depth = bridge.cv2_to_imgmsg(
        np.full((480, 640), depth_value, dtype=np.float32), encoding='32FC1',
    )
    depth.header = rgb.header
    info = CameraInfo()
    info.width, info.height = 640, 480
    info.k = [500.0, 0.0, 320.0, 0.0, 500.0, 240.0, 0.0, 0.0, 1.0]
    params = {
        'projection_frame': 'camera_optical_frame',
        'depth_roi_scale': 0.45,
        'minimum_depth_m': 0.30,
        'maximum_depth_m': 3.0,
        'minimum_depth_samples': 20,
        'fallback_depth_scale': 1.0,
        'enable_bearing_only_fallback': True,
        'bearing_only_uncertainty_m': 2.0,
        'person_thickness_m': 0.35,
        'publish_debug_image': False,
    }
    published, traces, healthy, warnings = [], [], [], []
    node = SimpleNamespace(
        _camera_info=info, _output_frame='', _bridge=bridge,
        _timing=CameraTiming(),
        get_parameter=lambda name: SimpleNamespace(value=params[name]),
        _warn_periodically=lambda key, message: warnings.append((key, message)),
        _publish_health=healthy.append,
        _detections_3d_publisher=SimpleNamespace(publish=published.append),
        _processing_trace_publisher=SimpleNamespace(publish=traces.append),
        _make_detection_3d=PersonLocalizerNode._make_detection_3d,
    )
    node._make_detections_3d = MethodType(
        PersonLocalizerNode._make_detections_3d, node,
    )
    return node, AlignedImages(rgb, depth), params, published, traces, healthy


def _identity(images):
    output = Detection2DArray()
    output.header = images.header
    detection = Detection2D()
    detection.header = images.header
    detection.id = 'person-7'
    detection.bbox.center.position.x = 330.0
    detection.bbox.center.position.y = 250.0
    detection.bbox.size_x = 40.0
    detection.bbox.size_y = 100.0
    hypothesis = ObjectHypothesisWithPose()
    hypothesis.hypothesis.class_id = 'person'
    hypothesis.hypothesis.score = 0.9
    detection.results.append(hypothesis)
    output.detections.append(detection)
    return output


def test_projection_keeps_upstream_identity_optical_axes_and_covariance():
    node, images, _, output, _, _ = _fixture()
    PersonLocalizerNode._on_observation(node, images, _identity(images))
    result = output[0]
    assert result.header.stamp == images.header.stamp
    assert result.header.frame_id == 'camera_optical_frame'
    detection = result.detections[0]
    assert detection.id == 'person-7'
    assert detection.header == result.header
    assert detection.bbox.center.position.x == pytest.approx(0.04)
    assert detection.bbox.center.position.y == pytest.approx(0.04)
    assert detection.bbox.center.position.z == 2.0
    assert detection.results[0].hypothesis.score == 0.9
    assert detection.results[0].pose.covariance[0] == pytest.approx(0.0001)


def test_far_depth_preserves_existing_bearing_only_observation():
    node, images, params, output, _, _ = _fixture(depth_value=3.5)
    PersonLocalizerNode._on_observation(node, images, _identity(images))
    detection = output[0].detections[0]
    assert detection.bbox.center.position.z == 3.0
    assert detection.results[0].pose.covariance[14] == 4.0
    params['enable_bearing_only_fallback'] = False
    PersonLocalizerNode._on_observation(node, images, _identity(images))
    assert output[1].detections == []


@pytest.mark.parametrize('trace_first', [True, False])
def test_original_yolo_receipt_is_joined_without_blocking_output(
    monkeypatch, trace_first,
):
    node, images, _, output, traces, health = _fixture()
    monkeypatch.setattr(
        'malbut_tracking.person_localizer_node.time.clock_gettime_ns',
        lambda clock: 5000,
    )
    trace = SensorProcessingTrace()
    trace.source = 'camera'
    trace.source_stamp = images.header.stamp
    trace.receipt_steady_time_ns = 1000
    trace.publish_steady_time_ns = 2000
    trace.sequence = 17
    if trace_first:
        PersonLocalizerNode._on_yolo_trace(node, trace)
    PersonLocalizerNode._on_observation(node, images, _identity(images))
    assert len(output) == 1
    assert health == [True]
    if not trace_first:
        assert traces == []
        PersonLocalizerNode._on_yolo_trace(node, trace)
    assert len(traces) == 1
    assert traces[0].receipt_steady_time_ns == 1000
    assert traces[0].publish_steady_time_ns == 5000
    assert traces[0].sequence == 17
    assert traces[0].source_stamp == images.header.stamp


def test_frame_mismatch_does_not_project_a_different_camera():
    node, images, _, output, _, _ = _fixture()
    detections = _identity(images)
    detections.header = _fixture()[1].header
    detections.header.frame_id = 'other_camera'
    PersonLocalizerNode._on_observation(node, images, detections)
    assert output == []


def test_debug_pixels_show_the_upstream_person_id():
    node, images, _, _, _, _ = _fixture()
    bgr = node._bridge.imgmsg_to_cv2(images.rgb, desired_encoding='bgr8')
    depth = node._bridge.imgmsg_to_cv2(images.depth)
    people = [PersonBox('person-7', BoundingBox(310, 200, 350, 300), 0.9)]
    drawn = PersonLocalizerNode._draw_debug_image(
        node, bgr, depth, images.depth.encoding, people,
    )
    assert np.count_nonzero(drawn) > 0
    assert np.count_nonzero(bgr) == 0
