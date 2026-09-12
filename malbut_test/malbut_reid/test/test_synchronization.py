"""Verify exact source-frame matching across delayed detector delivery."""

from types import SimpleNamespace

from message_filters import SimpleFilter, TimeSynchronizer
from sensor_msgs.msg import Image
from std_msgs.msg import Header


def _image(second):
    message = Image()
    message.header = Header(frame_id='camera')
    message.header.stamp.sec = second
    return message


def _detections(second):
    return SimpleNamespace(header=_image(second).header, detections=[])


def test_delayed_detection_uses_its_original_frame_not_the_latest_image():
    rgb, boxes = SimpleFilter(), SimpleFilter()
    synchronizer = TimeSynchronizer([rgb, boxes], queue_size=3)
    paired = []
    synchronizer.registerCallback(lambda image, det: paired.append((image, det)))
    original, latest = _image(1), _image(2)
    rgb.signalMessage(original)
    rgb.signalMessage(latest)
    detection = _detections(1)
    boxes.signalMessage(detection)
    assert paired == [(original, detection)]


def test_detection_can_arrive_before_rgb_without_pairing_wrong_frame():
    rgb, boxes = SimpleFilter(), SimpleFilter()
    synchronizer = TimeSynchronizer([rgb, boxes], queue_size=3)
    paired = []
    synchronizer.registerCallback(lambda image, det: paired.append((image, det)))
    detection = _detections(2)
    boxes.signalMessage(detection)
    rgb.signalMessage(_image(1))
    assert paired == []
    image = _image(2)
    rgb.signalMessage(image)
    assert paired == [(image, detection)]


def test_evicted_rgb_is_not_replaced_by_a_newer_image():
    rgb, boxes = SimpleFilter(), SimpleFilter()
    synchronizer = TimeSynchronizer([rgb, boxes], queue_size=2)
    paired = []
    synchronizer.registerCallback(lambda image, det: paired.append((image, det)))
    for second in (1, 2, 3):
        rgb.signalMessage(_image(second))
    boxes.signalMessage(_detections(1))
    assert paired == []
    boxes.signalMessage(_detections(3))
    assert len(paired) == 1
    assert paired[0][0].header.stamp.sec == 3
