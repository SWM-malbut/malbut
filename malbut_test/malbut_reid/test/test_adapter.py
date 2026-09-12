"""Test the public YOLO-to-person identity message contract."""

from types import SimpleNamespace

from std_msgs.msg import Header

from malbut_reid.adapter import identified_detections, person_detections
from malbut_reid.tracker import ByteTrackTracker


def _yolo_detection(class_id=0, class_name='person', detection_id='42'):
    return SimpleNamespace(
        class_id=class_id, class_name=class_name, id=detection_id, score=0.9,
        bbox=SimpleNamespace(
            center=SimpleNamespace(position=SimpleNamespace(x=100.0, y=150.0)),
            size=SimpleNamespace(x=40.0, y=100.0),
        ),
    )


def test_person_filter_and_bbox_conversion_preserve_geometry_and_score():
    message = SimpleNamespace(detections=[
        _yolo_detection(), _yolo_detection(56, 'chair'),
    ])
    boxes = person_detections(message)
    assert len(boxes) == 1
    assert boxes[0].bbox.center == (100.0, 150.0)
    assert boxes[0].bbox.left == 80.0
    assert boxes[0].bbox.top == 100.0
    assert boxes[0].score == 0.9
    assert boxes[0].class_id == 'person'


def test_public_identity_is_gallery_id_not_upstream_tracking_id():
    tracker = ByteTrackTracker(min_confirmed_hits=1)
    first = person_detections(SimpleNamespace(
        detections=[_yolo_detection(detection_id='999')],
    ))
    tracker.update(first)
    second = person_detections(SimpleNamespace(
        detections=[_yolo_detection(detection_id='1000')],
    ))
    header = Header(frame_id='camera_color_optical_frame')
    header.stamp.sec = 7
    header.stamp.nanosec = 123
    output = identified_detections(header, tracker.update(second))
    assert output.header == header
    assert output.detections[0].header == header
    assert output.detections[0].id == '1'
    assert output.detections[0].bbox.size_x == 40.0
    assert output.detections[0].bbox.size_y == 100.0
    hypothesis = output.detections[0].results[0]
    assert hypothesis.hypothesis.class_id == 'person'
    assert hypothesis.hypothesis.score == 0.9


def test_empty_observation_publishes_empty_array_with_source_stamp():
    header = Header(frame_id='camera')
    assert person_detections(SimpleNamespace(detections=[])) == []
    output = identified_detections(header, [])
    assert output.header == header
    assert output.detections == []
