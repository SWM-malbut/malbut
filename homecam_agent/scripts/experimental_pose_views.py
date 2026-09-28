"""Fixed multi-view Pose inference geometry; offline experiment only.

No GT, previous IDs, Cloud regions or frame labels choose a view. Coordinates
are restored before tracking; anatomical joint names are never swapped.
"""
import math

from replay_reviewed_pose_cloud import require
from homecam_detector.pose import PersonPose, PoseKeypoint, box_iou
from experimental_roi_pose import project_pose


def restore_rotation(pose, direction):
    require(direction in ('cw','ccw'), 'unknown rotation')
    require(len(pose.box)==4 and all(math.isfinite(v) and 0<=v<=1 for v in pose.box)
            and pose.box[0]<pose.box[2] and pose.box[1]<pose.box[3], 'invalid rotated box')
    def point(x,y):
        require(math.isfinite(x) and math.isfinite(y) and 0<=x<=1 and 0<=y<=1,'invalid joint')
        return (y,1-x) if direction=='cw' else (1-y,x)
    corners=[point(x,y) for x in (pose.box[0],pose.box[2]) for y in (pose.box[1],pose.box[3])]
    box=(min(p[0] for p in corners),min(p[1] for p in corners),
         max(p[0] for p in corners),max(p[1] for p in corners))
    points=tuple(PoseKeypoint(p.name,*point(p.x,p.y),p.confidence) for p in pose.keypoints)
    return PersonPose(pose.box_confidence,box,points,pose.visible_keypoints)


def fixed_tiles(image_size):
    width,height=image_size
    require((width,height)==(640,400),'experiment requires frozen 640x400 canvas')
    return ((0,0,400,400),(240,0,640,400))


def project_tile(pose, rectangle, image_size):
    """Reject a clipped box touching an internal crop edge; record it upstream."""
    restored=project_pose(pose,rectangle,image_size)
    left,top,right,bottom=rectangle; width,height=image_size
    cut=((left>0 and pose.box[0]<=0) or (top>0 and pose.box[1]<=0)
         or (right<width and pose.box[2]>=1) or (bottom<height and pose.box[3]>=1))
    return (None,'internal_crop_edge') if cut else (restored,None)


def merge_views(observations, duplicate_iou=.85):
    """Highest measured score wins near-identical boxes; no coordinate averaging.

    Full view wins an exact score tie. All raw/suppressed predictions are kept.
    Partial overlaps remain competing observations for the unchanged tracker.
    """
    require(0<duplicate_iou<=1,'invalid duplicate threshold')
    ordered=sorted(enumerate(observations),key=lambda pair:(
        -pair[1]['pose'].box_confidence,pair[1]['source']!='full',pair[0]))
    poses,records=[],[]
    for raw_index,observation in ordered:
        pose=observation['pose']
        duplicate=next((i for i,p in enumerate(poses) if box_iou(pose.box,p.box)>=duplicate_iou),None)
        record=dict(raw_index=raw_index,source=observation['source'],pose=pose.as_dict(),
                    duplicate_of=duplicate,fused_index=None)
        if duplicate is None:
            record['fused_index']=len(poses);poses.append(pose)
        records.append(record)
    return tuple(poses),records
