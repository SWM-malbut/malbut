"""Spatial place matching, furniture height filtering, and same-person checks."""

import contextlib
import math
from typing import Any, Optional, Sequence, Tuple

from malbut_agent_server.application.cloud_fall.constants import (
    CLEARED_PLACE_MAX_S,
    CLEARED_REASONS,
    FURNITURE_HEIGHT_M,
    SAME_PERSON_GAP_S,
    SAME_PERSON_INSIDE,
    SAME_PERSON_IOU,
    SAME_PLACE_CASE_S,
    SPOT_CHECK_MAX_S,
    SUPPORT_BAND,
    _inside,
)
from malbut_agent_server.application.fall_cloud_association import box_iou
from malbut_agent_server.application.fall_scene_place import (
    CASE_GAP_S,
    SAME_PLACE_M,
    near_on_map,
    same_place,
)
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind,
    FallCandidate,
    FallIncident,
    IncidentState,
    SubjectCheckState,
    SubjectFrame,
    VideoAssessment,
)


def emit_place_log(monitor: Any, message: str) -> None:
    """Best-effort place diagnostic log; never interrupts fall handling."""
    if monitor._place_log is None:
        return
    with contextlib.suppress(Exception):
        monitor._place_log(message)


def spot_under_check(monitor: Any, subject_key: str) -> Optional[str]:
    """The incident checking where this Pose subject is, or None (farther than 1 m)."""
    now = monitor._now()
    for iid, (_, since) in tuple(monitor._spot_checks.items()):
        if iid not in monitor._incidents or now - since > SPOT_CHECK_MAX_S:
            del monitor._spot_checks[iid]
    if not monitor._spot_checks:
        return None
    latest = monitor._subject_evidence.latest(subject_key)
    if latest is None or latest[2].box is None:
        # Not measured yet while the robot faces the spot; it comes again with a box.
        return next(iter(monitor._spot_checks))
    stamp, _, pose = latest
    point = None
    if monitor._place is not None:
        try:
            point = monitor._place.locate_near(stamp, pose.box)
        except Exception:
            point = None
    for iid, (target, _) in monitor._spot_checks.items():
        if point is not None:
            if math.dist(point, target) <= SAME_PLACE_M:
                return iid
        elif 0.25 <= (pose.box[0] + pose.box[2]) / 2 <= 0.75:
            return iid  # Same fallback as the close look: the robot faces the spot.
    return None


def subject_places(monitor: Any, subject_key: Optional[str]) -> Tuple[Any, ...]:
    """The Pose subject's latest map point, as a tuple of at most one."""
    if subject_key is None:
        return ()
    latest = monitor._subject_evidence.latest(subject_key)
    if monitor._place is None or latest is None or latest[2].box is None:
        return ()
    try:
        point = monitor._place.locate_near(latest[0], latest[2].box)
    except Exception:
        return ()  # Auxiliary: never stops fall handling.
    return () if point is None else (point,)


def remember_case_place(monitor: Any, incident: FallIncident) -> None:
    """Remember a Pose incident's latest measured map point."""
    places = subject_places(monitor, incident.subject_key)
    if places:
        monitor._case_places[incident.incident_id] = places[0]


def incident_place(monitor: Any, incident: FallIncident) -> Any:
    """Where the case is on the map: the spot it was checked at, else measured."""
    for point in (incident.revisit_target, incident.approach_target):
        if point is not None:
            return point
    if incident.subject_key is None:
        return incident.scene_points[-1] if incident.scene_points else None
    places = subject_places(monitor, incident.subject_key)
    return places[0] if places else monitor._case_places.get(incident.incident_id)


def remember_cleared_place(monitor: Any, incident: FallIncident) -> None:
    """Record a verified cleared place to suppress repeat non-fall reopens."""
    if incident.close_reason in CLEARED_REASONS:
        place = incident_place(monitor, incident)
        if place is not None:
            monitor._cleared_places[incident.incident_id] = (place, monitor._now())


def cleared_place(monitor: Any, points: Sequence[Any]) -> Optional[str]:
    """The closed case whose cleared place one of these map points is at, or None."""
    now = monitor._now()
    for iid, (_, since) in tuple(monitor._cleared_places.items()):
        if now - since > CLEARED_PLACE_MAX_S:
            del monitor._cleared_places[iid]
    return next((
        iid for iid, (place, _) in monitor._cleared_places.items()
        if any(math.dist(place, p) <= SAME_PLACE_M for p in points)
    ), None)


def forget_cleared_places(monitor: Any, points: Sequence[Any]) -> None:
    """Forget cleared places near any of the given map points."""
    for iid, (place, _) in tuple(monitor._cleared_places.items()):
        if any(math.dist(place, p) <= SAME_PLACE_M for p in points):
            del monitor._cleared_places[iid]


def refresh_cleared_places(monitor: Any, frame: SubjectFrame) -> None:
    """Someone standing at a cleared place: the usual rules apply there again."""
    if not monitor._cleared_places or monitor._place is None:
        return
    for pose in frame.subjects:
        if pose.state is not SubjectCheckState.CLEAR or pose.box is None:
            continue
        try:
            point = monitor._place.locate_near(frame.observed_at, pose.box)
        except Exception:
            point = None
        if point is not None:
            forget_cleared_places(monitor, (point,))


def map_points(monitor: Any, times: Sequence[float], finding: Any) -> Tuple[Any, ...]:
    """Resolve distinct map coordinates for a Cloud finding's regions."""
    if monitor._place is None:
        return ()
    points = []
    locate3d = getattr(monitor._place, 'locate3d', None)
    for region in finding.regions:
        if region.frame_index < len(times):
            try:
                if locate3d is not None:
                    spot = locate3d(times[region.frame_index], region.box)
                    log_place(monitor, 'Cloud box', spot)
                    point = None if spot is None else spot[:2]
                else:
                    point = monitor._place.locate(times[region.frame_index], region.box)
            except Exception:
                point = None  # Auxiliary: never stops fall handling.
            if point is not None:
                points.append(point)
    return tuple(dict.fromkeys(points))


def held_by_place(monitor: Any, times: Sequence[float], finding: Any) -> Optional[str]:
    """Why a Cloud finding opens no case of its own, or None (2026-10-12).

    At an open person case's place within two minutes it is that fall: its
    window joins the case's clips. At a place the robot found no fall at,
    Cloud's look decides and only a fall seen happening opens a case.
    """
    if finding.assessment is VideoAssessment.OBSERVED_FALL:
        forget_cleared_places(monitor, map_points(monitor, times, finding))
        return None
    points = map_points(monitor, times, finding)
    if not points:
        return None
    for incident in monitor._incidents.values():
        if (incident.subject_key is None or incident.state is IncidentState.RESOLVED
                or times[0] - incident.last_observed_at > SAME_PLACE_CASE_S):
            continue
        place = incident_place(monitor, incident)
        if place is not None and any(math.dist(place, p) <= SAME_PLACE_M for p in points):
            monitor._record_clip(
                incident, times[0], times[-1], anchor_kind='cloud_window',
                found_down=finding.kind is CandidateKind.ALREADY_DOWN,
            )
            monitor._record_cloud_boxes(incident, times, finding)
            return 'same_place_open_incident'
    if cleared_place(monitor, points) is not None:
        return 'cleared_place'
    return None


def apart_on_map(monitor: Any, a: Tuple[float, Any], b: Tuple[float, Any]) -> bool:
    """Both (stamp, box) measured on the map, farther apart than one place."""
    if monitor._place is None:
        return False
    try:
        points = [monitor._place.locate_near(stamp, box) for stamp, box in (a, b)]
    except Exception:
        return False  # Auxiliary: never stops fall handling.
    return None not in points and math.dist(*points) > SAME_PLACE_M


def same_person_incident(
    monitor: Any,
    candidate: FallCandidate,
) -> Optional[FallIncident]:
    """The open person incident this track is the same person as, or None.

    Same moment: boxes overlap at SAME_PERSON_IOU. One after another: the
    track appears where an incident track vanished, within SAME_PERSON_GAP_S
    and without camera movement. Map points over SAME_PLACE_M apart are two
    people. A newly seen fall still opens its own incident.
    """
    key = candidate.subject_key
    if candidate.kind is CandidateKind.MOTION_SEEN:
        monitor._same_person.pop(key, None)
        return None
    members: dict[str, FallIncident] = {}
    for track, iid in tuple(monitor._same_person.items()):
        incident = monitor._incidents.get(iid)
        if incident is None or incident.state is IncidentState.RESOLVED:
            del monitor._same_person[track]
        else:
            members[track] = incident
    if key in members:
        return members[key]
    for incident in monitor._incidents.values():
        if incident.subject_key is not None and incident.state is not IncidentState.RESOLVED:
            members[incident.subject_key] = incident
    if not members:
        return None
    tracks: dict[str, dict[float, Any]] = {}
    wanted = members.keys() | {key}
    for stamp, poses in monitor._subject_evidence.frames_since(-math.inf):
        for pose in poses:
            if pose.box is not None and pose.subject_key in wanted:
                tracks.setdefault(pose.subject_key, {})[stamp] = pose.box
    mine = tracks.pop(key, None)
    if not mine:
        return None
    first = min(mine)
    for track, theirs in tracks.items():
        together = mine.keys() & theirs.keys()
        if together:
            stamp = max(together)
            a, b = (stamp, theirs[stamp]), (stamp, mine[stamp])
            same = box_iou(a[1], b[1]) >= SAME_PERSON_IOU
        else:
            stamp = max(theirs)
            a, b = (stamp, theirs[stamp]), (first, mine[first])
            same = (
                stamp < first and first - stamp <= SAME_PERSON_GAP_S
                and not monitor._camera_motion.moved_between(stamp, first)
                and (box_iou(a[1], b[1]) >= SAME_PERSON_IOU
                     or _inside(a[1], b[1]) >= SAME_PERSON_INSIDE)
            )
        if same and not apart_on_map(monitor, a, b):
            incident = members[track]
            monitor._same_person[key] = incident.incident_id
            emit_place_log(
                monitor,
                f'fall place: Pose track {key} is the same person as '
                f'case {incident.incident_id[:8]}: no new case',
            )
            return incident
    return None


def on_furniture(monitor: Any, times: Sequence[float], finding: Any) -> bool:
    """Every box rests clearly above the floor and no fall was seen; else as before."""
    locate3d = getattr(monitor._place, 'locate3d', None)
    if locate3d is None or finding.assessment is VideoAssessment.OBSERVED_FALL:
        return False
    heights = []
    for region in finding.regions:
        if region.frame_index >= len(times):
            continue
        left, top, right, bottom = region.box
        band = (left, bottom - (bottom - top) * SUPPORT_BAND, right, bottom)
        try:
            spot = locate3d(times[region.frame_index], band)
        except Exception:
            spot = None
        if spot is None:
            return False  # Not measured: a fall cannot be ruled out.
        heights.append(spot[2])
    if not heights or min(heights) <= FURNITURE_HEIGHT_M:
        return False
    emit_place_log(
        monitor,
        f'fall place: Cloud box rests {min(heights) * 100:.0f} cm above '
        'the floor, on a bed or sofa: not a fall case',
    )
    return True


def log_place(monitor: Any, label: str, spot: Any) -> None:
    """Log one 3D map height reading if place logging is configured."""
    if monitor._place_log is None or spot is None or len(spot) != 3:
        return
    emit_place_log(
        monitor,
        f'fall place: {label} at map ({spot[0]:.2f}, {spot[1]:.2f}), '
        f'{spot[2] * 100:.0f} cm above the floor',
    )


def log_pose_place(monitor: Any, incident: FallIncident) -> None:
    """Log a Pose incident's 3D map height when available."""
    locate3d_near = getattr(monitor._place, 'locate3d_near', None)
    latest = monitor._subject_evidence.latest(incident.subject_key)
    if locate3d_near is None or latest is None or latest[2].box is None:
        return
    try:
        spot = locate3d_near(latest[0], latest[2].box)
    except Exception:
        return
    log_place(monitor, f'Pose case {incident.incident_id[:8]}', spot)


def scene_case_for(
    monitor: Any,
    times: Sequence[float],
    boxes: Sequence[Any],
    points: Sequence[Any],
    request_id: Optional[str],
) -> Optional[FallIncident]:
    """The open scene case this finding continues, or None for a new case."""
    open_scenes = [
        i for i in monitor._incidents.values()
        if i.subject_key is None and i.state is not IncidentState.RESOLVED
    ]
    if request_id is not None:
        for incident in open_scenes:
            if incident.scene_request_id == request_id:
                return incident
    # After CASE_GAP_S without findings the situation ended. The case stays
    # open for its answer and history but takes no new findings.
    active = [
        i for i in open_scenes
        if i.scene_seen_until is not None and times[0] - i.scene_seen_until <= CASE_GAP_S
    ]
    for incident in reversed(active):
        if incident.scene_points and points:
            same = near_on_map(incident.scene_points, points)
        else:
            same = (
                not monitor._camera_motion.moved_between(incident.scene_seen_from, times[-1])
                and same_place(incident.scene_boxes, boxes)
            )
        if same:
            return incident
    if active and (len(active) >= monitor.max_open_scene_cases or not monitor._make_room()):
        # Full: keep the earlier single-case behaviour rather than dropping it.
        return active[-1]
    return None
