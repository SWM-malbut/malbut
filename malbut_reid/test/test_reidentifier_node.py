"""Exercise the re-identification callback without loading a model."""

from types import SimpleNamespace

import numpy as np
from sensor_msgs.msg import Image
from yolo_msgs.msg import Detection, DetectionArray

from malbut_reid.person_reidentifier_node import PersonReidentifierNode
from malbut_reid.tracker import ByteTrackTracker


def _fixture():
    features, images, output, warnings = [], [], [], []

    def encode(image, detections):
        features.append((image, detections))
        return [np.array([1.0, 0.0], dtype=np.float32) for _ in detections]

    def convert(image, desired_encoding):
        images.append(image)
        return np.zeros((200, 100, 3), dtype=np.uint8)

    node = SimpleNamespace(
        _frame_index=0, _refresh_interval=3,
        _tracker=ByteTrackTracker(min_confirmed_hits=1),
        _encoder=SimpleNamespace(encode=encode),
        _bridge=SimpleNamespace(imgmsg_to_cv2=convert),
        _publisher=SimpleNamespace(publish=output.append),
        get_logger=lambda: SimpleNamespace(
            warning=lambda message, **kwargs: warnings.append(message)),
    )
    return node, features, images, output, warnings


def _messages(second, with_person=True):
    image = Image()
    image.header.frame_id = 'camera'
    image.header.stamp.sec = second
    array = DetectionArray()
    array.header = image.header
    if with_person:
        person = Detection()
        person.class_id = 0
        person.class_name = 'person'
        person.score = 0.9
        person.bbox.center.position.x = 50.0
        person.bbox.center.position.y = 100.0
        person.bbox.size.x = 40.0
        person.bbox.size.y = 100.0
        array.detections.append(person)
    return image, array


def test_stable_person_reuses_identity_and_encodes_every_third_frame():
    node, features, images, output, warnings = _fixture()
    for second in range(1, 5):
        PersonReidentifierNode._on_rgb_detections(node, *_messages(second))
    assert len(features) == len(images) == 2
    assert len(output) == 4
    assert all(message.detections[0].id == '1' for message in output)
    assert output[-1].header.stamp.sec == 4
    assert warnings == []


def test_no_person_publishes_empty_result_without_decoding_or_encoding_rgb():
    node, features, images, output, warnings = _fixture()
    PersonReidentifierNode._on_rgb_detections(node, *_messages(1, False))
    assert features == images == warnings == []
    assert len(output) == 1
    assert output[0].detections == []


def test_wrong_frame_is_rejected_even_when_source_timestamps_match():
    node, features, images, output, warnings = _fixture()
    image, detections = _messages(1)
    # Assign a new Header rather than changing the shared test object.
    detections.header = _messages(1)[0].header
    detections.header.frame_id = 'different_camera'
    PersonReidentifierNode._on_rgb_detections(node, image, detections)
    assert features == images == output == []
    assert len(warnings) == 1
