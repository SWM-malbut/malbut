"""Shared constants, thresholds, and runtime records for CloudFallMonitor."""

from dataclasses import dataclass
from typing import Any, Tuple

from malbut_agent_server.domain.fall_monitoring import NotificationLevel


RUNTIME_CLOUD_BLOCKS = frozenset({
    'waiting_settings', 'disabled', 'camera_off', 'mapping', 'control_unavailable',
    'runtime_error', 'cloud_consent_missing', 'settings_pending',
    'server_settings_unavailable', 'server_settings_stale',
})
# Consent, connection and settings stop Cloud on purpose: such a failure is
# neither sent again nor turned into a question.
GUARD_FAILURES = RUNTIME_CLOUD_BLOCKS | {
    'monitoring_disabled', 'cloud_disconnected', 'cloud_permission_changed', 'worker_cancelled',
}

APPROACH_OUTCOMES = frozenset({'arrived', 'no_map', 'no_path', 'timeout', 'failed', 'rejected'})
# After arriving 1 m in front: Pose looks this long, then Cloud once.
PERSON_POSE_WAIT_S = 3.0
# While the robot drives to a spot, looks and drives back, that spot belongs to
# the incident being checked (2026-10-09): from 1 m, Pose read the bag pile it
# had just been sent to look at as a person lying down and opened a second
# incident. A check normally ends with its result or return; this bounds it.
SPOT_CHECK_MAX_S = 240.0
# 2026-10-12 21:15 one fall became two cases: the Pose case, and 28 s later a
# Cloud finding whose box filled the frame so no track matched it. A finding
# at an open person case's map place within two minutes is that fall (the
# user, 2026-10-12); a fall seen happening still opens its own case.
SAME_PLACE_CASE_S = 120.0
# 2026-10-12 21:13 the robot saw someone sitting off the floor, closed the case
# and drove back; from there Pose read them as lying and a new case began. A
# place the robot checked is now checked by Cloud only: a case opens there
# only for a fall seen happening (the user, 2026-10-12). Standing up there ends
# it, and this bounds it.
CLEARED_PLACE_MAX_S = 1800.0
CLEARED_REASONS = frozenset({
    'off_floor_verified', 'floor_activity_verified', 'normal_verified',
    'not_a_person', 'risk_cleared', 'response_completed',
})
# A body resting higher than this is on a bed or sofa, not on the floor: the
# 30 cm the Pose detector already applies to the torso (maximum_floor_height_m).
# 2026-10-09: a person lying on the bed became a Cloud scene case every minute.
FURNITURE_HEIGHT_M = 0.30
# The lowest quarter of a Cloud box is what the body rests on; its centre would
# rise to 30-40 cm for someone slumped against a wall on the floor.
SUPPORT_BAND = 0.25
# fall_full_view (2026-10-11): the 12 cm camera showed only the legs of someone
# sitting on a bed, so a close look said "person" and the robot asked. When the
# look finds a person, the robot moves back to where the whole body is visible
# (at most VIEW_LIMIT_S, the user's one minute) and asks Cloud where the body
# rests, twice POSTURE_GAP_S apart on new frames. It closes without asking only
# when both looks say furniture or standing; anything else asks. Pose keypoints
# are not used for this: from 12 cm a seated person's thighs hide the hips.
VIEW_LIMIT_S = 60.0
POSTURE_SETTLE_S = 1.0
POSTURE_GAP_S = 2.5
OFF_FLOOR_LOOKS = frozenset({'furniture', 'standing'})
OFF_FLOOR_REASONS = frozenset({'sitting_off_floor', 'standing'})
# 2026-10-14 (the user): someone studying at a low table on the floor was asked
# every time, since sitting on the floor counted as floor. Sitting upright and
# busy (reading, writing, eating...) twice closes without asking too, but only
# when no fall was seen and not on a revisit; anything else on the floor asks.
FLOOR_ACTIVE_LOOK = 'floor_active'
FLOOR_ACTIVE_REASON = 'active_on_floor'
# The robot could not ask for a minute (2026-10-12): it goes back to the spot and
# looks again, but never closes the case itself; the guardian does. What it saw
# goes to the guardian, compared by clothing with the first scene for reference.
WAITING_REASONS = frozenset({'manual_drive', 'busy'})
NOT_CONFIRMED_REASONS = frozenset({'waited_manual_drive', 'waited_busy'})
CHECK_FAILURE_REASONS = frozenset({'speech_unavailable', 'question_error', 'time_limit'})
# "그만/멈춰" during the check stops it like 직접 움직이기 would (the user, 2026-10-13):
# no question; the guardian is told to check (check level, once).
STOP_REASONS = frozenset({'user_stop'})
# A comparison not done by then (Cloud off meanwhile) is recorded as unclear.
COMPARE_WAIT_S = 60.0
FIRST_SCENES_MAX = 16
# One person can come out of YOLO-pose as several tracks (2026-10-10 18:44: one
# person lying on the floor became three tracks and three incidents). Tracks seen
# together were one person at IoU 0.54-0.82; another person's box was at most
# 0.12. A track appearing 2.2 s after another vanished sat 87% inside its last
# box. So open person incidents never outnumber the people in view.
SAME_PERSON_IOU = 0.5
SAME_PERSON_INSIDE = 0.8
SAME_PERSON_GAP_S = 5.0

LEVEL_ORDER = {
    NotificationLevel.INFO: 1,
    NotificationLevel.CHECK: 2,
    NotificationLevel.URGENT: 3,
}


def _inside(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    """How much of the smaller box lies inside the other."""
    area = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1]),
    )
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return area / smaller if smaller else 0.0


@dataclass
class PersonCheck:
    """Looking from close range after the robot drove near an uncertain suspicion."""

    question_id: str
    target: Tuple[Any, ...]
    started: float
    pose_deadline: float
    cloud: bool = False
    asked: bool = False
    # 'person' (1 m look), 'view' (moving to see the whole body) or 'posture'.
    stage: str = 'person'
    deadline: float = 0.0
    looks: Tuple[str, ...] = ()  # Cloud posture verdicts so far
    # Back at the spot after waiting: report what is seen, never close the case.
    revisit: bool = False


@dataclass(frozen=True)
class CloudAnalysisStatus:
    """Observable status snapshot for the single active Cloud request."""

    state: str = 'idle'
    request_id: str = ''
    request_purpose: str = ''
    last_error_code: str = ''
