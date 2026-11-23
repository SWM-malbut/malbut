"""Shared constants, dataclasses, and field validators for FallFlowSensor."""

from dataclasses import dataclass, field
import math
from typing import Any, Dict, List, Optional, Tuple

from malbut_agent_server.domain.fall_monitoring import VideoAssessment


# Stopped on purpose (consent, connection, settings): the rules ask nothing then.
GUARD_REASONS = frozenset({
    'waiting_settings', 'disabled', 'camera_off', 'mapping', 'control_unavailable',
    'runtime_error', 'cloud_consent_missing', 'settings_pending', 'server_settings_unavailable',
    'server_settings_stale', 'monitoring_disabled', 'cloud_disconnected',
    'cloud_permission_changed', 'worker_cancelled',
})
RUNTIME_CLOUD_BLOCKS = frozenset({
    'waiting_settings', 'disabled', 'camera_off', 'mapping', 'control_unavailable',
    'runtime_error', 'cloud_consent_missing', 'settings_pending',
    'server_settings_unavailable', 'server_settings_stale',
})
# The moment of a fall seen by YOLO-Pose: 4 s before to 1 s after, 12 frames (design 5).
MOMENT_BEFORE_S, MOMENT_AFTER_S, MOMENT_FRAMES = 4.0, 1.0, 12
# The current look of the whole body: 3 frames over 2 s (unchanged).
POSTURE_WINDOW_S, POSTURE_FRAMES = 2.0, 3
ARRIVAL_WINDOW_S, ARRIVAL_FRAMES = 1.0, 2
# The spot while the robot waits and drives there: once a second, when it is in
# view, for at most ten minutes per incident (design 6); thinned to stay bounded.
MIDDLE_EVERY_S, MIDDLE_LIMIT_S, MIDDLE_MAX, DEPARTURE_FRAMES = 1.0, 600.0, 120, 12
HFOV_RAD = 1.2915
VIEW_MARGIN = 0.2
VIEW_MAX_M = 8.0
# Photos of closed or never-opened suspicions are dropped after this.
PHOTO_KEEP_S = 1800.0
RECENT_S = 60.0
FURNITURE_HEIGHT_M = 0.30
SUPPORT_BAND = 0.25
LOOKS = ('filter', 'arrival', 'departure', 'posture')
# Which explanation each record may carry (fall_events.json analysisPurposes).
RECORD_PURPOSE = {
    'analysis_completed': ('incident', 'crosscheck'),
    'arrival_compared': ('arrival_check',),
    'departure_reviewed': ('departure_check',),
    'posture_check_completed': ('posture_check',),
}
# 처리 완료 arrives every second while the app lists it; tell the rules now and then.
GUARDIAN_AGAIN_S = 10.0
# The robot's check as the incident's video (SWM25-268, 목업 53번): from when the robot
# gets to work on a round until 5 s after the judgment, without its waits. The range
# grows every few seconds meanwhile (people boxes keep only 30 s of history) and stops
# growing after ten minutes.
CLIP_MARKS = ('start', 'pause', 'end')
CHECK_CLIP_AFTER_S, CHECK_CLIP_EVERY_S, CHECK_CLIP_MAX_S = 5.0, 5.0, 600.0
# A ReID question waits for the second look of the one who reappeared (a second later),
# then is answered with what there is (SWM25-267 decision 10).
COMPARE_WAIT_S = 3.0
# The web's assessment shown beside a look's explanation (display only).
SHOWN = {
    'same_person': VideoAssessment.SUSPECTED_FALL,
    'person_unsure': VideoAssessment.SUSPECTED_FALL,
    'different_person': VideoAssessment.SUSPECTED_FALL,
    'same_object': VideoAssessment.NORMAL_ACTIVITY,
    'gone': VideoAssessment.NORMAL_ACTIVITY,
    'not_visible': VideoAssessment.UNOBSERVABLE,
    'animal_left': VideoAssessment.NORMAL_ACTIVITY,
    'person_walked': VideoAssessment.NORMAL_ACTIVITY,
    'person_crawled': VideoAssessment.SUSPECTED_FALL,
    'not_seen': VideoAssessment.UNOBSERVABLE,
    'furniture': VideoAssessment.NORMAL_ACTIVITY,
    'standing': VideoAssessment.NORMAL_ACTIVITY,
    'floor_active': VideoAssessment.NORMAL_ACTIVITY,
    'floor': VideoAssessment.SUSPECTED_FALL,
    'unclear': VideoAssessment.UNOBSERVABLE,
}
RECORD_KINDS = frozenset({
    'incident_opened', 'analysis_completed', 'analysis_unavailable', 'approach_started',
    'approach_completed', 'arrival_compared', 'reverify_completed', 'departure_reviewed',
    'view_completed', 'posture_check_completed', 'fall_moment_reviewed', 'question_requested',
    'confirmation_completed', 'voice_result', 'agent_check_failed', 'check_waiting',
    'check_not_confirmed', 'revisit_started', 'check_stopped', 'incident_resolved',
    'recheck_started', 'approach_returned', 'candidate_dropped', 'notification_grouped',
})
STATES = ('verifying', 'recheck_required', 'help_required', 'resolved')
ANSWERS = ('help_request', 'okay', 'unclear', 'no_response', 'failed')


@dataclass(frozen=True)
class Photo:
    """In-memory frame snapshot retained for follow-up VLM comparisons."""

    frames: Tuple[Any, ...]
    box: Optional[Tuple[float, float, float, float]]
    at: float


@dataclass
class FlowIncident:
    """What the VLM keeps for an incident of the fall rules: photos and its clip."""

    incident_id: str
    boot_id: str
    first: Dict[str, Any]
    opened_at: float
    open: bool = True
    check_round: int = 1
    left: Optional[str] = None     # left open for the guardian: nobody or person (restart)
    middle: List[Any] = field(default_factory=list)
    middle_at: float = -math.inf
    person: Optional[str] = None   # the Agent's person number: the yellow box (SWM25-267)
    # While the robot works on a round: [check round, since, last grown] (SWM25-268).
    check_clip: Optional[List[Any]] = None
    # When a suspicion last joined it, without a journal (the journal keeps it otherwise).
    seen_at: Optional[float] = None


@dataclass
class Look:
    """Queued Cloud look requested by the fall rules."""

    look_id: str
    what: str
    sighting_id: Optional[str]
    incident_id: Optional[str]
    photo: Optional[str] = None
    box: Optional[Tuple[float, float, float, float]] = None
    track: Optional[str] = None
    fall_moments: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalysisStatus:
    """Observable status snapshot for the active FallFlowSensor Cloud request."""

    state: str = 'idle'
    request_id: str = ''
    request_purpose: str = ''
    last_error_code: str = ''


def _point(value: Any) -> Optional[List[float]]:
    return None if value is None else [round(float(value[0]), 3), round(float(value[1]), 3)]


def _box(value: Any) -> Optional[Tuple[float, float, float, float]]:
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) and 0 <= v <= 1 for v in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise ValueError('invalid box')
    return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _id(value: Any, *, optional: bool = True) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError('invalid identifier')
    return value


def _req_id(value: Any) -> str:
    parsed = _id(value, optional=False)
    assert parsed is not None
    return parsed
