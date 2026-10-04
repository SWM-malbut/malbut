"""Runtime contracts for cloud-only fall checks; independent of evaluation GT.

Times are monotonic seconds in one device boot, not wall-clock timestamps.
Subject keys are supplied by a separate association adapter, not VLM identities.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Optional, Tuple


def positive(value: float, name: str) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f'{name} must be finite and positive')


def timestamp(value: float) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError('invalid monotonic timestamp')


def identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError('invalid identifier')


class CandidateKind(str, Enum):
    MOTION_SEEN = 'motion_seen'
    ALREADY_DOWN = 'already_down'
    UNKNOWN = 'unknown'


class IncidentState(str, Enum):
    VERIFYING = 'verifying'
    RECHECK_REQUIRED = 'recheck_required'
    HELP_REQUIRED = 'help_required'
    RESOLVED = 'resolved'


class VideoAssessment(str, Enum):
    OBSERVED_FALL = 'observed_fall'
    SUSPECTED_FALL = 'suspected_fall'
    NORMAL_ACTIVITY = 'normal_activity'
    UNOBSERVABLE = 'unobservable'


class VoiceAnswer(str, Enum):
    HELP = 'help_request'
    OKAY = 'okay'
    UNCLEAR = 'unclear'
    NO_RESPONSE = 'no_response'
    FAILED = 'failed'


class PersonVisibility(str, Enum):
    SEEN = 'seen'
    NOT_SEEN = 'not_seen'
    UNKNOWN = 'unknown'


class SubjectCheckState(str, Enum):
    CLEAR = 'clear'
    SUSPECTED = 'suspected'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class SubjectObservation:
    """Trusted subject check, NOT a missing YOLO candidate or a model identity.

    CLEAR requires healthy camera/detector input and an actual observation of
    the associated person. The producer verifies association to the referenced
    analysis as well as the current observation; a track ID alone is not proof.
    Times use this runtime's monotonic clock. Missing/occluded input is UNKNOWN.
    """

    incident_id: str
    subject_key: str
    evidence_revision: int
    request_id: str
    observed_at: float
    state: SubjectCheckState
    association_verified: bool

    def __post_init__(self) -> None:
        for value in (self.incident_id, self.subject_key, self.request_id):
            identifier(value)
        if type(self.evidence_revision) is not int or self.evidence_revision < 1:
            raise ValueError('invalid evidence revision')
        timestamp(self.observed_at)
        if (not isinstance(self.state, SubjectCheckState)
                or type(self.association_verified) is not bool):
            raise ValueError('invalid subject observation')


@dataclass(frozen=True)
class NormalVideoCheck:
    request_id: str
    evidence_revision: int
    window_end: float
    target_token: Optional[str] = None


class NotificationLevel(str, Enum):
    INFO = 'info'
    CHECK = 'check'
    URGENT = 'urgent'


@dataclass(frozen=True)
class PersonObservation:
    """Detector observation, not proof of an empty room or a person's identity."""

    observed_at: float
    visibility: PersonVisibility

    def __post_init__(self) -> None:
        timestamp(self.observed_at)
        if not isinstance(self.visibility, PersonVisibility):
            raise ValueError('invalid person visibility')


@dataclass(frozen=True)
class AgentCheckReply:
    incident_id: str
    question_id: str
    subject_key: str
    evidence_revision: int
    answer: VoiceAnswer
    question_played: bool

    def __post_init__(self) -> None:
        for value in (self.incident_id, self.question_id, self.subject_key):
            identifier(value)
        if type(self.evidence_revision) is not int or self.evidence_revision < 1:
            raise ValueError('invalid evidence revision')
        if not isinstance(self.answer, VoiceAnswer) or type(self.question_played) is not bool:
            raise ValueError('invalid Agent answer')
        if self.answer is VoiceAnswer.NO_RESPONSE and not self.question_played:
            raise ValueError('no_response requires a played question')


@dataclass(frozen=True)
class FallRuntimePolicy:
    """Required settings: no unapproved operating thresholds as defaults."""

    scan_interval_s: float
    idle_scan_interval_s: float
    person_hold_s: float
    max_person_observation_age_s: float
    cloud_timeout_s: float
    retry_interval_s: float
    clip_window_s: float
    max_frame_age_s: float
    max_rechecks: int
    max_calls_per_minute: int
    max_incidents: int
    max_images: int

    @classmethod
    def agreed(cls, *, retry_interval_s: float,
               max_person_observation_age_s: float,
               clip_window_s: float, max_frame_age_s: float,
               max_calls_per_minute: int, max_incidents: int,
               max_images: int):
        """2026-09-18 user decisions; remaining settings are still required."""
        return cls(
            scan_interval_s=60.0, idle_scan_interval_s=300.0,
            person_hold_s=120.0,
            max_person_observation_age_s=max_person_observation_age_s,
            cloud_timeout_s=20.0, max_rechecks=2,
            retry_interval_s=retry_interval_s,
            clip_window_s=clip_window_s,
            max_frame_age_s=max_frame_age_s,
            max_calls_per_minute=max_calls_per_minute,
            max_incidents=max_incidents, max_images=max_images)

    def __post_init__(self) -> None:
        for name in (
            'scan_interval_s', 'cloud_timeout_s', 'retry_interval_s',
            'idle_scan_interval_s', 'max_person_observation_age_s',
            'clip_window_s', 'max_frame_age_s',
        ):
            positive(getattr(self, name), name)
        timestamp(self.person_hold_s)
        if self.idle_scan_interval_s < self.scan_interval_s:
            raise ValueError('idle scan interval must not be shorter')
        for name, minimum in (
            ('max_rechecks', 0), ('max_calls_per_minute', 1),
            ('max_incidents', 1), ('max_images', 1),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= 10000:
                raise ValueError(f'{name} is out of bounds')


@dataclass(frozen=True)
class RgbFrame:
    captured_at: float
    jpeg: bytes = field(repr=False)

    def __post_init__(self) -> None:
        timestamp(self.captured_at)
        if (not isinstance(self.jpeg, bytes)
                or not self.jpeg.startswith(b'\xff\xd8')
                or not self.jpeg.endswith(b'\xff\xd9')):
            raise ValueError('expected JPEG bytes')


@dataclass(frozen=True)
class FrameWindow:
    frames: Tuple[RgbFrame, ...] = field(repr=False)
    requested_start: float
    requested_end: float
    history_incomplete: bool


@dataclass(frozen=True)
class SubjectPose:
    subject_key: str
    box: Optional[Tuple[float, float, float, float]]
    state: SubjectCheckState
    association_usable: bool

    def __post_init__(self):
        identifier(self.subject_key)
        if (not isinstance(self.state, SubjectCheckState)
                or type(self.association_usable) is not bool):
            raise ValueError('invalid subject pose')
        if self.box is not None and (
                not isinstance(self.box, tuple) or len(self.box) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                       for v in self.box)
                or self.box[0] >= self.box[2] or self.box[1] >= self.box[3]):
            raise ValueError('invalid subject box')
        if self.association_usable and self.box is None:
            raise ValueError('association requires an observed box')


@dataclass(frozen=True)
class SubjectFrame:
    observed_at: float
    subjects: Tuple[SubjectPose, ...]
    max_gap_s: float

    def __post_init__(self):
        timestamp(self.observed_at)
        positive(self.max_gap_s, 'max_gap_s')
        if (not isinstance(self.subjects, tuple) or len(self.subjects) > 256
                or any(not isinstance(p, SubjectPose) for p in self.subjects)
                or len({p.subject_key for p in self.subjects}) != len(self.subjects)):
            raise ValueError('invalid subject frame')


@dataclass(frozen=True)
class SubjectVideoTarget:
    """Measured boxes aligned one-to-one to request frames; token stays local."""

    subject_key: str
    association_token: str
    sample_times: Tuple[float, ...]
    boxes: Tuple[Tuple[float, float, float, float], ...]

    def __post_init__(self):
        identifier(self.subject_key)
        identifier(self.association_token)
        if (not isinstance(self.sample_times, tuple) or not isinstance(self.boxes, tuple)
                or not 1 <= len(self.boxes) == len(self.sample_times) <= 64):
            raise ValueError('invalid target samples')
        previous = -1.0
        for stamp, box in zip(self.sample_times, self.boxes):
            timestamp(stamp)
            if stamp <= previous:
                raise ValueError('target timestamps must increase')
            SubjectPose(self.subject_key, box, SubjectCheckState.UNKNOWN, True)
            previous = stamp


@dataclass(frozen=True)
class SensorSummary:
    """Optional measurements; absent data is not replaced by zero."""

    observed_at: float
    floor_distance_m: Optional[float] = None
    linear_speed_m_s: Optional[float] = None
    angular_speed_rad_s: Optional[float] = None

    def __post_init__(self) -> None:
        timestamp(self.observed_at)
        for name in ('floor_distance_m', 'linear_speed_m_s',
                     'angular_speed_rad_s'):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or (name == 'floor_distance_m' and value < 0)
            ):
                raise ValueError(f'invalid {name}')


@dataclass(frozen=True)
class FallCandidate:
    candidate_id: str
    subject_key: str
    source: str
    kind: CandidateKind
    observed_at: float
    significant_change: bool = False
    sensors: Optional[SensorSummary] = None
    # When the suspicious motion began (same clock); None means observed_at.
    evidence_started_at: Optional[float] = None

    def __post_init__(self) -> None:
        identifier(self.candidate_id)
        identifier(self.subject_key)
        timestamp(self.observed_at)
        if self.evidence_started_at is not None:
            timestamp(self.evidence_started_at)
            if self.evidence_started_at > self.observed_at:
                raise ValueError('evidence starts after it was observed')
        if self.source not in {'yolo_pose', 'cloud_crosscheck'}:
            raise ValueError('unsupported source')
        if not isinstance(self.kind, CandidateKind):
            raise ValueError('unsupported candidate kind')
        if type(self.significant_change) is not bool:
            raise ValueError('significant_change must be bool')
        if self.sensors is not None and (
            not isinstance(self.sensors, SensorSummary)
            or self.sensors.observed_at > self.observed_at
        ):
            raise ValueError('invalid sensor observation')


@dataclass(frozen=True)
class CloudFallRequest:
    request_id: str
    purpose: str
    device_id: str
    boot_id: str
    incident_id: Optional[str]
    subject_key: Optional[str]
    evidence_revision: int
    window: FrameWindow
    sensors: Optional[SensorSummary]
    target: Optional[SubjectVideoTarget] = None


@dataclass(frozen=True)
class CloudPersonRegion:
    """A model-proposed location, not verified identity or sensor geometry."""

    frame_index: int
    box: Tuple[float, float, float, float]

    def __post_init__(self):
        if type(self.frame_index) is not int or not 0 <= self.frame_index < 64:
            raise ValueError('invalid frame index')
        SubjectPose('region', self.box, SubjectCheckState.UNKNOWN, True)


@dataclass(frozen=True)
class CloudPersonFinding:
    assessment: VideoAssessment
    kind: CandidateKind
    regions: Tuple[CloudPersonRegion, ...] = ()

    def __post_init__(self):
        if (self.assessment not in (VideoAssessment.OBSERVED_FALL,
                                    VideoAssessment.SUSPECTED_FALL)
                or not isinstance(self.assessment, VideoAssessment)
                or not isinstance(self.kind, CandidateKind)):
            raise ValueError('invalid person finding')
        if (self.assessment is VideoAssessment.OBSERVED_FALL
                and self.kind is not CandidateKind.MOTION_SEEN):
            raise ValueError('observed fall requires visible motion')
        if (not isinstance(self.regions, tuple) or len(self.regions) > 4
                or any(not isinstance(r, CloudPersonRegion) for r in self.regions)
                or any(a.frame_index >= b.frame_index
                       for a, b in zip(self.regions, self.regions[1:]))):
            raise ValueError('invalid person regions')


@dataclass(frozen=True)
class CloudAssociationEvidence:
    """Measured availability used for association, not proof of person identity."""

    scope: str
    samples: int
    pose_box_samples: int
    usable_pose_samples: int

    def __post_init__(self):
        if (self.scope not in ('finding_frames', 'request_window', 'invalid_sample_reference')
                or any(type(n) is not int or n < 0 for n in (
                    self.samples, self.pose_box_samples, self.usable_pose_samples))
                or not 0 <= self.usable_pose_samples <= self.pose_box_samples <= self.samples):
            raise ValueError('invalid association evidence')

    @property
    def unlinked_case(self):
        # An invalid reference is unknown, not evidence of detector absence.
        if self.scope != 'invalid_sample_reference' and self.usable_pose_samples == 0:
            return 'pose_evidence_missing'
        return 'identity_unverified'

    def metadata(self):
        return dict(scope=self.scope, samples=self.samples,
                    pose_box_samples=self.pose_box_samples,
                    usable_pose_samples=self.usable_pose_samples)


@dataclass(frozen=True)
class CloudDiscoveryLink:
    """Append-only local provenance; a source scene is NOT a merged person."""

    source_incident_id: str
    source_revision: int
    target_token: str
    seed_time: float
    confirmed_at: float
    visual_samples: int
    pose_samples: int

    def __post_init__(self):
        identifier(self.source_incident_id)
        identifier(self.target_token)
        timestamp(self.seed_time)
        timestamp(self.confirmed_at)
        if (any(type(n) is not int or n < 1 for n in (
                self.source_revision, self.visual_samples, self.pose_samples))
                or not 3 <= self.pose_samples <= self.visual_samples
                or self.confirmed_at - self.seed_time < .5 - 1e-9):
            raise ValueError('invalid discovery link proof')

    def metadata(self):
        return dict(source_incident_id=self.source_incident_id,
                    source_revision=self.source_revision, target_token=self.target_token,
                    seed_time=self.seed_time, confirmed_at=self.confirmed_at,
                    visual_samples=self.visual_samples, pose_samples=self.pose_samples,
                    method='continuous_visual_track_pose_v1')


@dataclass(frozen=True)
class CloudPoseLink:
    """Measured timestamp association, distinct from visual tracking proof."""

    source_incident_id: str
    source_revision: int
    target_token: str
    confirmed_at: float
    rgb_times: Tuple[float, ...]
    pose_times: Tuple[Tuple[float, ...], ...]

    def __post_init__(self):
        identifier(self.source_incident_id)
        identifier(self.target_token)
        timestamp(self.confirmed_at)
        if (type(self.source_revision) is not int or self.source_revision < 1
                or len(self.rgb_times) < 2 or len(self.rgb_times) != len(self.pose_times)):
            raise ValueError('invalid timestamp association proof')
        for rgb, poses in zip(self.rgb_times, self.pose_times):
            timestamp(rgb)
            if len(poses) not in (1, 2):
                raise ValueError('invalid timestamp association samples')
            for pose in poses:
                timestamp(pose)
                if pose > self.confirmed_at or abs(pose - rgb) > .1 + 1e-6:
                    raise ValueError('timestamp association outside bound')
            if (len(poses) == 1 and poses[0] != rgb
                    or len(poses) == 2 and not poses[0] < rgb < poses[1]):
                raise ValueError('timestamp association requires exact or both neighbors')

    def metadata(self):
        return dict(source_incident_id=self.source_incident_id,
                    source_revision=self.source_revision, target_token=self.target_token,
                    confirmed_at=self.confirmed_at, method='measured_pose_timestamps_v1',
                    max_offset_s=.1, rgb_times=list(self.rgb_times),
                    pose_times=[list(times) for times in self.pose_times])


@dataclass(frozen=True)
class CloudDiscovery:
    discovery_id: str
    request_id: str
    finding_index: int
    finding: CloudPersonFinding
    sample_times: Tuple[float, ...]
    reason: str
    subject_key: Optional[str] = None
    incident_id: Optional[str] = None
    association_evidence: Optional[CloudAssociationEvidence] = None
    association_link: Optional[CloudDiscoveryLink | CloudPoseLink] = None

    def metadata(self):
        """Private local record; excludes RGB, model prose and Cloud credentials."""
        return dict(discovery_id=self.discovery_id, request_id=self.request_id,
                    finding_index=self.finding_index, assessment=self.finding.assessment.value,
                    candidate_kind=self.finding.kind.value, reason=self.reason,
                    association_status='matched' if self.subject_key else 'unidentified',
                    association_case=('matched' if self.subject_key else
                        self.association_evidence.unlinked_case
                        if self.association_evidence is not None else 'unknown'),
                    association_evidence=(self.association_evidence.metadata()
                        if self.association_evidence is not None else None),
                    association_link=(self.association_link.metadata()
                        if self.association_link is not None else None),
                    subject_key=self.subject_key, incident_id=self.incident_id,
                    sample_times=list(self.sample_times),
                    regions=[dict(frame_index=r.frame_index, box=list(r.box))
                             for r in self.finding.regions])


@dataclass(frozen=True)
class CloudFallReply:
    """Model observation, never an instruction to move or notify someone."""

    assessment: VideoAssessment
    explanation: str
    findings: Tuple[CloudPersonFinding, ...] = ()
    localization_failed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.assessment, VideoAssessment):
            raise ValueError('invalid assessment')
        if type(self.localization_failed) is not bool:
            raise ValueError('invalid localization status')
        if (not isinstance(self.explanation, str)
                or not self.explanation.strip()
                or len(self.explanation) > 4000):
            raise ValueError('invalid explanation')
        if (not isinstance(self.findings, tuple) or len(self.findings) > 8
                or any(not isinstance(f, CloudPersonFinding) for f in self.findings)):
            raise ValueError('invalid findings')
        if self.findings and self.assessment not in (
                VideoAssessment.OBSERVED_FALL, VideoAssessment.SUSPECTED_FALL):
            raise ValueError('scene assessment contradicts positive findings')
        if (any(f.assessment is VideoAssessment.OBSERVED_FALL for f in self.findings)
                and self.assessment is not VideoAssessment.OBSERVED_FALL):
            raise ValueError('scene assessment loses observed fall')
        if (self.findings and self.assessment is VideoAssessment.OBSERVED_FALL
                and not any(f.assessment is VideoAssessment.OBSERVED_FALL for f in self.findings)):
            raise ValueError('observed scene fall has no corresponding finding')


@dataclass(frozen=True)
class FallRuntimeEvent:
    """Intent/result for adapters. notification_requested is NOT delivered."""

    event_id: str
    kind: str
    incident_id: Optional[str] = None
    reason: Optional[str] = None
    question_id: Optional[str] = None
    subject_key: Optional[str] = None
    evidence_revision: Optional[int] = None
    notification_level: Optional[NotificationLevel] = None
    request: Optional[CloudFallRequest] = field(default=None, repr=False)
    reply: Optional[CloudFallReply] = None
    discovery: Optional[CloudDiscovery] = None
    confirmation_scope: str = 'subject'
    merged_into_incident_ids: Tuple[str, ...] = ()


@dataclass
class FallIncident:
    incident_id: str
    # None is a scene-level verification case, never a synthetic person ID.
    # Its discoveries may involve different people; sharing this case is only
    # a bounded confirmation queue, not a claim of person/event identity.
    subject_key: Optional[str]
    kind: CandidateKind
    opened_at: float
    last_observed_at: float
    revision: int = 1
    state: IncidentState = IncidentState.VERIFYING
    attempts: int = 0
    rechecks: int = 0
    pending: bool = True
    next_attempt_at: float = 0.0
    last_window_end: float = -1.0
    last_candidate_id: str = ''
    question_id: Optional[str] = None
    answer: Optional[VoiceAnswer] = None
    video: Optional[CloudFallReply] = None
    video_revision: Optional[int] = None
    sensors: Optional[SensorSummary] = None
    fall_seen: bool = False
    close_reason: Optional[str] = None
    notification_level: Optional[NotificationLevel] = None
    last_failure: Optional[str] = None
    candidate_sources: Tuple[str, ...] = ()
    answer_question_played: bool = False
    # At most two successful, distinct windows; attempts also count failures.
    normal_checks: Tuple[NormalVideoCheck, ...] = ()
    auto_normal_blocked: bool = False
    subject_observation: Optional[SubjectObservation] = None
    normal_evidence_after: float = 0.0
    subject_association_token: Optional[str] = None
    # The Manager's final confirmation is separate from the legacy voice enum.
    situation_assessment: Optional[str] = None
    help_needed: Optional[bool] = None
    # Scene completeness must outlive the bounded discovery retry cache. An
    # overflow fails closed: pruning a finding must never imply it was linked.
    unresolved_discovery_ids: Tuple[str, ...] = ()
    discovery_overflow: bool = False
    associated_incident_ids: Tuple[str, ...] = ()
    merged_into_incident_ids: Tuple[str, ...] = ()
