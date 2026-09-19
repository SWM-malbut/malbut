"""Letterbox contract: original pixels, padding, source restoration, no fake joints."""
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import replay_fall_baseline  # noqa: E402,F401
from experimental_pose_input import geometry, letterbox, restore  # noqa: E402
from homecam_detector.pose import PersonPose, PoseKeypoint  # noqa: E402


def test_640_by_400_pixels_unchanged_with_120_rows_each_side():
    raw = np.arange(640*400*3, dtype=np.uint8).reshape(400, 640, 3)
    before = raw.copy()
    image, g = letterbox(raw)
    assert image.shape == (640, 640, 3)
    assert g.gain == 1 and g.left == 0 and g.top == 120
    assert np.array_equal(image[120:520], raw)
    assert np.all(image[:120] == 114) and np.all(image[520:] == 114)
    assert np.array_equal(raw, before)


@pytest.mark.parametrize('width,height', [(640, 400), (1280, 720), (321, 239),
                                          (400, 640), (640, 640)])
def test_box_and_keypoint_round_trip(width, height):
    g = geometry(width, height)

    def forward(x, y):
        return (x*width*g.gain+g.left)/g.size, (y*height*g.gain+g.top)/g.size

    p = PersonPose(.11, (*forward(.1, .2), *forward(.8, .9)),
                   (PoseKeypoint('nose', *forward(.3, .4), .8),), 1)
    restored = restore((p,), g)[0]
    assert restored.box == pytest.approx((.1, .2, .8, .9))
    assert (restored.keypoints[0].x, restored.keypoints[0].y) == pytest.approx((.3, .4))
    assert restored.box_confidence == .11 and restored.visible_keypoints == 1


def test_padding_detection_removed_and_padding_joint_cannot_become_body_evidence():
    g = geometry(640, 400)
    outside = PersonPose(.9, (.1, .01, .3, .1), (), 0)
    partial = PersonPose(.1, (.1, .1, .3, .6),
                         (PoseKeypoint('nose', .2, .1, .99),
                          PoseKeypoint('left_hip', .2, .4, .8)), 2)
    result = restore((outside, partial), g)
    assert len(result) == 1 and result[0].box[1] == 0
    assert result[0].keypoints[0].confidence == 0
    assert result[0].visible_keypoints == 1
    assert partial.keypoints[0].confidence == .99


@pytest.mark.parametrize('width,height,size', [
    (0, 400, 640), (640, -1, 640), (True, 400, 640),
    (640, 400, 0), (640, 400, 640.0), (100000, 1, 640)])
def test_invalid_dimensions_rejected(width, height, size):
    with pytest.raises(ValueError):
        geometry(width, height, size)


def test_empty_input_rejected():
    with pytest.raises(ValueError):
        letterbox(np.zeros((0, 640, 3), dtype=np.uint8))
