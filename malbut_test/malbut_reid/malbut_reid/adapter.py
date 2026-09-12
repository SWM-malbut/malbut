"""Convert public YOLO boxes to the existing person identity contract."""

from typing import List

from vision_msgs.msg import (
    Detection2D,
    Detection2DArray,
    ObjectHypothesisWithPose,
)

from .models import BoundingBox, ImageDetection


def person_detections(message) -> List[ImageDetection]:
    """Extract COCO people without using the detector's ephemeral track IDs."""
    output = []
    for detection in message.detections:
        if detection.class_name != 'person' and detection.class_id != 0:
            continue
        box = detection.bbox
        x, y = float(box.center.position.x), float(box.center.position.y)
        half_width, half_height = float(box.size.x) / 2, float(box.size.y) / 2
        output.append(ImageDetection(
            bbox=BoundingBox(
                x - half_width, y - half_height,
                x + half_width, y + half_height,
            ),
            score=float(detection.score),
            class_id='person',
        ))
    return output


def identified_detections(header, tracks) -> Detection2DArray:
    """Preserve sensor time and boxes while publishing the gallery identity."""
    output = Detection2DArray()
    output.header = header
    for track in tracks:
        detection = Detection2D()
        detection.header = header
        detection.id = str(track.track_id)
        box = track.detection.bbox
        center_x, center_y = box.center
        detection.bbox.center.position.x = center_x
        detection.bbox.center.position.y = center_y
        detection.bbox.size_x = box.width
        detection.bbox.size_y = box.height
        hypothesis = ObjectHypothesisWithPose()
        hypothesis.hypothesis.class_id = track.detection.class_id
        hypothesis.hypothesis.score = track.detection.score
        hypothesis.pose.pose.orientation.w = 1.0
        detection.results.append(hypothesis)
        output.detections.append(detection)
    return output
