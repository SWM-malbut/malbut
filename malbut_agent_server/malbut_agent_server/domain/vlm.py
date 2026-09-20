"""Provider-neutral contracts for RGB-D assisted home-camera analysis."""

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple


class FallCandidateKind(str, Enum):
    """How local perception entered active fall verification."""

    GENERAL_EVENT = 'event_candidate'
    OBSERVED_FALL = 'observed_fall_candidate'
    FOUND_DOWN = 'found_down_candidate'


class FallAssessment(str, Enum):
    """Safety-relevant incident outcomes shared by every VLM."""

    CONFIRMED_FALL = 'confirmed_fall'
    FOUND_DOWN = 'found_down'
    NORMAL_ACTIVITY = 'normal_activity'
    UNOBSERVABLE = 'unobservable'


class RecoveryState(str, Enum):
    """Whether the person recovered during the available observation."""

    RECOVERED = 'recovered'
    NOT_RECOVERED = 'not_recovered'
    UNKNOWN = 'unknown'


class ResponseState(str, Enum):
    """Result of the optional on-device voice check."""

    RESPONSIVE = 'responsive'
    UNRESPONSIVE = 'unresponsive'
    NOT_ASKED = 'not_asked'
    UNKNOWN = 'unknown'


_S3_URI = re.compile(
    r's3://[a-z0-9][.\-a-z0-9]{1,61}[a-z0-9](?:/.*)?'
)
_VIDEO_FORMATS = {
    'avi',
    'mp4',
    'mov',
    'mkv',
    'webm',
    'flv',
    'mpeg',
    'mpg',
    'wmv',
    'three_gp',
}


def _bounded(value: float, name: str, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(f'{name} must be between {minimum} and {maximum}')
    return float(value)


@dataclass(frozen=True)
class VlmMedia:
    """One immutable video reference; adapters decide how to upload it."""

    video_format: str
    local_path: Optional[str] = None
    s3_uri: Optional[str] = None
    sha256: Optional[str] = None

    def __post_init__(self) -> None:
        normalized = self.video_format.strip().lower()
        if normalized == '3gp':
            normalized = 'three_gp'
        if normalized not in _VIDEO_FORMATS:
            raise ValueError('video_format is unsupported')
        object.__setattr__(self, 'video_format', normalized)
        if (self.local_path is None) == (self.s3_uri is None):
            raise ValueError('exactly one of local_path or s3_uri is required')
        if self.local_path is not None:
            path = Path(self.local_path).expanduser()
            if not path.is_file() or path.is_symlink():
                raise ValueError('local_path must be a regular video file')
            object.__setattr__(self, 'local_path', str(path.resolve()))
        if self.s3_uri is not None and not _S3_URI.fullmatch(self.s3_uri):
            raise ValueError('s3_uri must be a valid s3:// object URI')
        if self.sha256 is not None and (
            len(self.sha256) != 64
            or any(
                character not in '0123456789abcdef'
                for character in self.sha256
            )
        ):
            raise ValueError('sha256 must be a lowercase SHA-256 digest')


@dataclass(frozen=True)
class AuroraDepthEvidence:
    """Small, non-image summary computed from aligned Aurora depth data."""

    aligned_to_rgb: bool
    stale: bool
    valid_torso_ratio: float
    torso_floor_distance_m: Optional[float]
    person_distance_m: Optional[float]
    sampled_torso_points: int

    def __post_init__(self) -> None:
        _bounded(self.valid_torso_ratio, 'valid_torso_ratio', 0.0, 1.0)
        if self.sampled_torso_points < 0:
            raise ValueError('sampled_torso_points must be non-negative')
        for name in ('torso_floor_distance_m', 'person_distance_m'):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f'{name} must be a non-negative distance')

    @property
    def usable(self) -> bool:
        return (
            self.aligned_to_rgb
            and not self.stale
            and self.valid_torso_ratio >= 0.5
            and self.torso_floor_distance_m is not None
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            'sensor': 'aurora930',
            'alignedToRgb': self.aligned_to_rgb,
            'stale': self.stale,
            'validTorsoRatio': round(self.valid_torso_ratio, 4),
            'torsoFloorDistanceM': self.torso_floor_distance_m,
            'personDistanceM': self.person_distance_m,
            'sampledTorsoPoints': self.sampled_torso_points,
            'usable': self.usable,
        }


@dataclass(frozen=True)
class PhysicalFallEvidence:
    """Local evidence that a cloud or local VLM is never allowed to erase."""

    candidate_kind: FallCandidateKind
    descent_score: float
    floor_proximity_score: float
    body_visibility: str
    robot_motion: str
    recovery: RecoveryState = RecoveryState.UNKNOWN
    response: ResponseState = ResponseState.NOT_ASKED
    depth: Optional[AuroraDepthEvidence] = None
    pose_summary: Mapping[str, Any] = field(default_factory=dict)
    yolo_summary: Mapping[str, Any] = field(default_factory=dict)
    source_provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _bounded(self.descent_score, 'descent_score', 0.0, 1.0)
        _bounded(
            self.floor_proximity_score,
            'floor_proximity_score',
            0.0,
            1.0,
        )
        if self.body_visibility not in {
            'full',
            'upper_only',
            'lower_only',
            'head_only',
            'partial',
            'none',
        }:
            raise ValueError('body_visibility is unsupported')
        if self.robot_motion not in {'none', 'partial', 'whole'}:
            raise ValueError('robot_motion is unsupported')
        if not isinstance(self.pose_summary, Mapping):
            raise ValueError('pose_summary must be a mapping')
        if not isinstance(self.yolo_summary, Mapping):
            raise ValueError('yolo_summary must be a mapping')
        if not isinstance(self.source_provenance, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.source_provenance.items()
        ):
            raise ValueError('source_provenance must be a string mapping')

    @property
    def strong_observed_fall(self) -> bool:
        return (
            self.candidate_kind is FallCandidateKind.OBSERVED_FALL
            and self.descent_score >= 0.7
            and self.floor_proximity_score >= 0.7
        )

    @property
    def strong_found_down(self) -> bool:
        return (
            self.candidate_kind is FallCandidateKind.FOUND_DOWN
            and self.floor_proximity_score >= 0.7
        )

    @property
    def unresponsive_down_candidate(self) -> bool:
        """Return evidence that a model must never downgrade to normal.

        Voice non-response alone is not proof of an incident: microphones,
        speakers, and occupants can all be unavailable.  It becomes a safety
        boundary only after local perception entered a fall/found-down
        verification flow and no local recovery was observed.
        """
        return (
            self.response is ResponseState.UNRESPONSIVE
            and self.recovery is not RecoveryState.RECOVERED
            and self.candidate_kind
            in {FallCandidateKind.OBSERVED_FALL, FallCandidateKind.FOUND_DOWN}
        )

    @property
    def needs_safety_verification(self) -> bool:
        """Whether an inconclusive observation warrants guardian attention."""
        return (
            self.candidate_kind is not FallCandidateKind.GENERAL_EVENT
            or self.strong_observed_fall
            or self.strong_found_down
            or self.unresponsive_down_candidate
        )

    @property
    def observation_usable(self) -> bool:
        # A 9--12 cm mobile camera commonly sees only the lower body.  That
        # view can still contain the temporal descent needed by the VLM; lack
        # of a full torso must be represented as uncertainty, not discarded
        # before analysis.  A completely absent person still requires usable
        # aligned depth evidence.
        visual = self.body_visibility != 'none'
        return visual or (self.depth is not None and self.depth.usable)

    def as_prompt_context(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            'candidateKind': self.candidate_kind.value,
            'descentScore': round(self.descent_score, 4),
            'floorProximityScore': round(self.floor_proximity_score, 4),
            'bodyVisibility': self.body_visibility,
            'robotMotion': self.robot_motion,
            'recovery': self.recovery.value,
            'response': self.response.value,
            'pose': dict(self.pose_summary),
            'yolo': dict(self.yolo_summary),
        }
        if self.depth is not None:
            result['depth'] = self.depth.as_dict()
        return result

    @property
    def contract_sha256(self) -> str:
        """Bind label-free evidence and its producer without prompting it."""
        value = {
            'evidence': self.as_prompt_context(),
            'source': dict(self.source_provenance),
        }
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class VlmAnalysisRequest:
    """One bounded VLM call independent of provider SDKs."""

    request_id: str
    incident_id: str
    duration_s: float
    media: VlmMedia
    evidence: PhysicalFallEvidence
    context_variant: str = 'C2'

    def __post_init__(self) -> None:
        if not self.request_id.strip() or len(self.request_id) > 128:
            raise ValueError('request_id must be 1..128 characters')
        if not self.incident_id.strip() or len(self.incident_id) > 128:
            raise ValueError('incident_id must be 1..128 characters')
        _bounded(self.duration_s, 'duration_s', 0.001, 3600.0)
        if self.context_variant not in {'C0', 'C1', 'C2'}:
            raise ValueError('context_variant must be C0, C1, or C2')

    def prompt_contexts(self) -> Dict[str, Optional[Mapping[str, Any]]]:
        """Return the fixed ablation context without exposing GT labels."""
        context = self.evidence.as_prompt_context()
        if self.context_variant == 'C0':
            return {'yolo': None, 'rgbd': None, 'robot_motion': None}
        if self.context_variant == 'C1':
            return {
                'yolo': context.get('yolo'),
                'rgbd': None,
                'robot_motion': None,
            }
        return {
            'yolo': context.get('yolo'),
            'rgbd': {
                'candidateKind': context['candidateKind'],
                'descentScore': context['descentScore'],
                'floorProximityScore': context['floorProximityScore'],
                'bodyVisibility': context['bodyVisibility'],
                'recovery': context['recovery'],
                'response': context['response'],
                'pose': context['pose'],
                'depth': context.get('depth'),
            },
            'robot_motion': {'summary': context['robotMotion']},
        }


@dataclass(frozen=True)
class VlmProviderResult:
    """Raw provider result plus non-sensitive metering data."""

    prediction: Mapping[str, Any]
    provider: str
    model_id: str
    model_version: str
    region: str
    latency_ms: float
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    response_id: Optional[str] = None


@dataclass(frozen=True)
class FallVerificationDecision:
    """Safety-policy output consumed by incident and notification workers."""

    assessment: FallAssessment
    recovery: RecoveryState
    risk: str
    confidence: float
    provider: str
    model_id: str
    explanation_ko: str
    evidence_ko: Tuple[str, ...]
    policy_reasons: Tuple[str, ...]
    notify_guardian: bool
