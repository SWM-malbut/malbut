"""Square letterbox and source-coordinate restoration; no model/threshold changes."""
from dataclasses import dataclass
import math

import cv2

from homecam_detector.pose import PersonPose, PoseKeypoint


@dataclass(frozen=True)
class LetterboxGeometry:
    width: int
    height: int
    size: int
    gain: float
    left: int
    top: int
    resized_width: int
    resized_height: int


def geometry(width, height, size=640):
    if any(type(v) is not int or v <= 0 for v in (width, height, size)):
        raise ValueError('positive integer dimensions required')
    gain = min(size/width, size/height)
    rw, rh = round(width*gain), round(height*gain)
    if min(rw, rh) < 1:
        raise ValueError('aspect ratio too extreme for this input size')
    return LetterboxGeometry(width, height, size, gain, round((size-rw)/2-.1),
                             round((size-rh)/2-.1), rw, rh)


def letterbox(frame, size=640):
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.size == 0:
        raise ValueError('nonempty BGR frame required')
    height, width = frame.shape[:2]
    g = geometry(width, height, size)
    resized = frame if (g.resized_width, g.resized_height) == (width, height) else cv2.resize(
        frame, (g.resized_width, g.resized_height), interpolation=cv2.INTER_LINEAR)
    image = cv2.copyMakeBorder(resized, g.top, size-g.resized_height-g.top,
                               g.left, size-g.resized_width-g.left,
                               cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return image, g


def restore(poses, g, keypoint_threshold=.5):
    def xy(x, y):
        return ((x*g.size-g.left)/(g.gain*g.width),
                (y*g.size-g.top)/(g.gain*g.height))

    def clip(v):
        return min(1.0, max(0.0, v))

    out = []
    for pose in poses:
        left, top = xy(*pose.box[:2])
        right, bottom = xy(*pose.box[2:])
        box = tuple(map(clip, (left, top, right, bottom)))
        if box[0] >= box[2] or box[1] >= box[3]:
            continue  # Prediction wholly in padding, not a person in the source.
        points = []
        for p in pose.keypoints:
            x, y = xy(p.x, p.y)
            if not all(math.isfinite(v) for v in (x, y, p.confidence)):
                raise ValueError('non-finite pose')
            confidence = p.confidence if 0 <= x <= 1 and 0 <= y <= 1 else 0.0
            points.append(PoseKeypoint(p.name, clip(x), clip(y), confidence))
        out.append(PersonPose(pose.box_confidence, box, tuple(points),
                              sum(p.confidence >= keypoint_threshold for p in points)))
    return tuple(out)
