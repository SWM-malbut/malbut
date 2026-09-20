"""Versioned, provider-neutral contracts for Malbut VLM evaluation."""

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


SUBJECTS = ('person', 'pet', 'other')
GT_SUBJECTS = (
    'person',
    'child',
    'pet_dog',
    'pet_cat',
    'pet_other',
    'screen_or_reflection',
    'doll_or_mannequin',
)
PREDICTED_EVENT_TYPES = (
    'fall',
    'near_fall',
    'lie_down_floor',
    'lie_down_bed_sofa',
    'sit_down_floor',
    'pick_up_object',
    'squat_kneel',
    'exercise',
    'play',
    'enter',
    'leave',
    'other',
)
GT_EVENT_TYPES = (
    'fall',
    'near_fall',
    'lie_down_floor_intentional',
    'lie_down_bed_sofa',
    'sit_down_floor',
    'pick_up_object',
    'squat_kneel',
    'exercise',
    'play_with_pet_or_child',
    'enter',
    'leave',
    'assisted_to_floor',
    'other',
)
HARD_NEGATIVE_EVENTS = frozenset(
    {
        'near_fall',
        'lie_down_floor_intentional',
        'lie_down_bed_sofa',
        'sit_down_floor',
        'pick_up_object',
        'squat_kneel',
        'exercise',
        'play_with_pet_or_child',
        'assisted_to_floor',
    }
)
PREDICTED_POSTURES = (
    'standing',
    'sitting',
    'lying_floor',
    'lying_bed_sofa',
    'unknown',
)
GT_POSTURES = (
    'standing',
    'walking',
    'sitting_chair',
    'sitting_floor',
    'lying_bed_sofa',
    'lying_floor',
    'crouching',
    'unknown',
)
RISKS = ('urgent', 'attention', 'none')
MOTION_STATES = (
    'static',
    'teleop_translate',
    'teleop_rotate',
    'nav_autonomous',
    'docking',
    'bump_vibration',
)
MOTION_SUMMARIES = ('none', 'partial', 'whole')
UNCERTAINTY_FLAGS = (
    'occluded',
    'low_light',
    'partial_body',
    'far',
    'short_clip',
    'depth_unavailable',
    'depth_unaligned',
    'sensor_stale',
)
FALL_ASSESSMENTS = (
    'confirmed_fall',
    'found_down',
    'normal_activity',
    'unobservable',
)
RECOVERY_STATES = ('recovered', 'not_recovered', 'unknown')
TRACKS = ('V', 'F', 'A')
CONTEXT_VARIANTS = ('C0', 'C1', 'C2')
TRAFFIC_CLASSES = (
    'fall',
    'found_down',
    'hard_negative',
    'no_event',
    'robot_motion_only',
    'screen_or_reflection',
    'other_non_fall',
)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _required_object(
    value: Mapping[str, Any],
    key: str,
    where: str,
) -> Mapping[str, Any]:
    child = value.get(key)
    if not isinstance(child, dict):
        raise ValueError(f'{where}.{key} must be an object')
    return child


def _required_string(
    value: Mapping[str, Any],
    key: str,
    where: str,
) -> str:
    child = value.get(key)
    if not isinstance(child, str) or not child.strip():
        raise ValueError(f'{where}.{key} must be a non-empty string')
    return child.strip()


def _number(
    value: Mapping[str, Any],
    key: str,
    where: str,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    child = value.get(key)
    if not _is_number(child):
        raise ValueError(f'{where}.{key} must be a finite number')
    result = float(child)
    if minimum is not None and result < minimum:
        raise ValueError(f'{where}.{key} must be at least {minimum}')
    if maximum is not None and result > maximum:
        raise ValueError(f'{where}.{key} must be at most {maximum}')
    return result


def _integer(
    value: Mapping[str, Any],
    key: str,
    where: str,
    *,
    minimum: int = 0,
    maximum: Optional[int] = None,
) -> int:
    child = value.get(key)
    if isinstance(child, bool) or not isinstance(child, int):
        raise ValueError(f'{where}.{key} must be an integer')
    if child < minimum:
        raise ValueError(f'{where}.{key} must be at least {minimum}')
    if maximum is not None and child > maximum:
        raise ValueError(f'{where}.{key} must be at most {maximum}')
    return child


def _enum(
    value: Mapping[str, Any],
    key: str,
    allowed: Sequence[str],
    where: str,
) -> str:
    child = value.get(key)
    if child not in allowed:
        choices = ', '.join(allowed)
        raise ValueError(f'{where}.{key} must be one of: {choices}')
    return str(child)


def _optional_bool(
    value: Mapping[str, Any],
    key: str,
    where: str,
) -> Optional[bool]:
    child = value.get(key)
    if child is None:
        return None
    if not isinstance(child, bool):
        raise ValueError(f'{where}.{key} must be a boolean or null')
    return child


def _assert_keys(
    value: Mapping[str, Any],
    allowed: Sequence[str],
    where: str,
) -> None:
    unknown = set(value) - set(allowed)
    if unknown:
        names = ', '.join(sorted(unknown))
        raise ValueError(f'{where} has unknown fields: {names}')


def canonical_sha256(value: Any) -> str:
    """Return a stable digest for a JSON-compatible contract."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def normalize_gt_subject(subject: str) -> str:
    """Map detailed annotation subjects onto the public VLM schema."""
    if subject in {'person', 'child'}:
        return 'person'
    if subject.startswith('pet_'):
        return 'pet'
    return 'other'


def normalize_gt_event(event_type: str) -> str:
    """Map rich annotation event names onto the model output enum."""
    aliases = {
        'lie_down_floor_intentional': 'lie_down_floor',
        'play_with_pet_or_child': 'play',
        'assisted_to_floor': 'other',
    }
    return aliases.get(event_type, event_type)


def normalize_gt_posture(posture: str) -> str:
    """Map detailed annotation posture onto the five output classes."""
    aliases = {
        'walking': 'standing',
        'sitting_chair': 'sitting',
        'sitting_floor': 'sitting',
        'crouching': 'unknown',
    }
    return aliases.get(posture, posture)


@dataclass(frozen=True)
class GroundTruthEvent:
    """One adjudicated temporal event in a clip."""

    event_id: str
    subject: str
    event_type: str
    start_s: float
    end_s: float
    fall_tier: Optional[str] = None
    recovered: Optional[bool] = None

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        duration_s: float,
        index: int,
    ) -> 'GroundTruthEvent':
        where = f'events[{index}]'
        if not isinstance(value, dict):
            raise ValueError(f'{where} must be an object')
        event_type = _enum(value, 'type', GT_EVENT_TYPES, where)
        subject = _required_string(value, 'subject_ref', where)
        if subject not in GT_SUBJECTS:
            raise ValueError(f'{where}.subject_ref is unsupported')
        start_s = _number(value, 'start_s', where, minimum=0)
        end_s = _number(value, 'end_s', where, minimum=0)
        if end_s < start_s or end_s > duration_s:
            raise ValueError(
                f'{where} must satisfy 0 <= start_s <= end_s '
                '<= clip.duration_s'
            )
        fall_tier = value.get('fall_tier')
        if event_type == 'fall':
            if fall_tier not in {'clear', 'ambiguous'}:
                raise ValueError(
                    f'{where}.fall_tier is required for fall'
                )
        elif fall_tier is not None:
            raise ValueError(
                f'{where}.fall_tier is only valid for fall'
            )
        recovery = value.get('recovery_within_s')
        if recovery is not None and not _is_number(recovery):
            raise ValueError(
                f'{where}.recovery_within_s must be a number or null'
            )
        recovered = None
        if event_type == 'fall':
            recovered = recovery is not None
        event_id = value.get('event_id', f'event-{index + 1}')
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError(f'{where}.event_id must be a string')
        return cls(
            event_id=event_id.strip(),
            subject=normalize_gt_subject(subject),
            event_type=normalize_gt_event(event_type),
            start_s=start_s,
            end_s=end_s,
            fall_tier=fall_tier,
            recovered=recovered,
        )


@dataclass(frozen=True)
class VlmEvaluationCase:
    """One clip and its adjudicated ground truth."""

    case_id: str
    duration_s: float
    media_path: Optional[str]
    media_sha256: Optional[str]
    has_audio: bool
    source: str
    robot_motion: str
    subjects: Dict[str, int]
    events: Tuple[GroundTruthEvent, ...]
    fall_assessment: str
    posture_end: str
    risk: str
    traffic_class: str
    conditions: Dict[str, str]

    @property
    def has_fall(self) -> bool:
        return any(event.event_type == 'fall' for event in self.events)

    @property
    def has_clear_fall(self) -> bool:
        return any(
            event.event_type == 'fall'
            and event.fall_tier == 'clear'
            for event in self.events
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> 'VlmEvaluationCase':
        """Validate a version 1 annotation record."""
        if not isinstance(value, dict):
            raise ValueError('evaluation case must be an object')
        schema_version = value.get('schema_version')
        if schema_version not in (1, 2):
            raise ValueError('evaluation schema_version must be 1 or 2')
        case_id = _required_string(value, 'case_id', 'case')
        clip = _required_object(value, 'clip', 'case')
        duration_s = _number(clip, 'duration_s', 'clip', minimum=0.001)
        media_path = clip.get('path')
        if media_path is not None and (
            not isinstance(media_path, str) or not media_path.strip()
        ):
            raise ValueError('clip.path must be a non-empty string')
        media_sha256 = clip.get('sha256')
        if media_sha256 is not None and (
            not isinstance(media_sha256, str)
            or len(media_sha256) != 64
            or any(ch not in '0123456789abcdef' for ch in media_sha256)
        ):
            raise ValueError('clip.sha256 must be lowercase SHA-256')
        has_audio = clip.get('has_audio', False)
        if not isinstance(has_audio, bool):
            raise ValueError('clip.has_audio must be a boolean')
        source = clip.get('source', 'robot_capture')
        if not isinstance(source, str) or not source:
            raise ValueError('clip.source must be a non-empty string')

        motion = _required_object(value, 'robot_motion', 'case')
        motion_summary = _enum(
            motion,
            'summary',
            MOTION_SUMMARIES,
            'robot_motion',
        )
        timeline = motion.get('timeline', [])
        if not isinstance(timeline, list):
            raise ValueError('robot_motion.timeline must be a list')
        for index, interval in enumerate(timeline):
            where = f'robot_motion.timeline[{index}]'
            if not isinstance(interval, dict):
                raise ValueError(f'{where} must be an object')
            t0 = _number(interval, 't0', where, minimum=0)
            t1 = _number(interval, 't1', where, minimum=0)
            _enum(interval, 'state', MOTION_STATES, where)
            if t1 < t0 or t1 > duration_s:
                raise ValueError(f'{where} is outside the clip')

        present = value.get('subjects_present')
        if not isinstance(present, list):
            raise ValueError('subjects_present must be a list')
        if any(subject not in GT_SUBJECTS for subject in present):
            raise ValueError('subjects_present contains an unknown subject')
        if len(set(present)) != len(present):
            raise ValueError('subjects_present contains duplicates')
        counts = _required_object(value, 'counts', 'case')
        n_person = _integer(counts, 'n_person', 'counts')
        n_pet = _integer(counts, 'n_pet', 'counts')
        other_count = sum(
            1
            for subject in present
            if normalize_gt_subject(subject) == 'other'
        )
        expected_person = any(
            normalize_gt_subject(subject) == 'person'
            for subject in present
        )
        expected_pet = any(
            normalize_gt_subject(subject) == 'pet'
            for subject in present
        )
        if expected_person != (n_person > 0):
            raise ValueError(
                'subjects_present and counts.n_person disagree'
            )
        if expected_pet != (n_pet > 0):
            raise ValueError('subjects_present and counts.n_pet disagree')

        raw_events = value.get('events')
        if not isinstance(raw_events, list):
            raise ValueError('events must be a list')
        events = tuple(
            GroundTruthEvent.from_dict(event, duration_s, index)
            for index, event in enumerate(raw_events)
        )
        posture_end = _enum(
            value,
            'posture_end',
            GT_POSTURES,
            'case',
        )
        risk = _enum(value, 'risk_gt', RISKS, 'case')
        fall_assessment = value.get('fall_assessment_gt')
        if fall_assessment is None and schema_version == 1:
            fall_assessment = (
                'confirmed_fall'
                if any(event.event_type == 'fall' for event in events)
                else 'normal_activity'
            )
        if fall_assessment not in FALL_ASSESSMENTS:
            raise ValueError(
                'case.fall_assessment_gt must be one of: '
                + ', '.join(FALL_ASSESSMENTS)
            )
        if fall_assessment == 'confirmed_fall' and not any(
            event.event_type == 'fall' for event in events
        ):
            raise ValueError(
                'confirmed_fall assessment needs a fall event'
            )
        if fall_assessment == 'found_down' and not (
            n_person > 0 and normalize_gt_posture(posture_end) == 'lying_floor'
        ):
            raise ValueError(
                'found_down assessment needs a person lying on the floor'
            )
        if fall_assessment == 'found_down' and any(
            event.event_type == 'fall' for event in events
        ):
            raise ValueError(
                'found_down assessment cannot contain an observed fall event'
            )
        traffic_class = value.get('traffic_class')
        if traffic_class is None:
            traffic_class = (
                'found_down'
                if fall_assessment == 'found_down'
                else infer_traffic_class(present, events, motion_summary)
            )
        if traffic_class not in TRAFFIC_CLASSES:
            raise ValueError('case.traffic_class is unsupported')
        if traffic_class == 'fall' and not any(
            event.event_type == 'fall' for event in events
        ):
            raise ValueError('fall traffic_class needs a fall event')
        if (traffic_class == 'found_down') != (
            fall_assessment == 'found_down'
        ):
            raise ValueError(
                'found_down traffic_class and assessment must match'
            )
        conditions = value.get('conditions', {})
        if not isinstance(conditions, dict) or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in conditions.items()
        ):
            raise ValueError('conditions must be a string map')
        return cls(
            case_id=case_id,
            duration_s=duration_s,
            media_path=media_path.strip() if media_path else None,
            media_sha256=media_sha256,
            has_audio=has_audio,
            source=source,
            robot_motion=motion_summary,
            subjects={
                'person': n_person,
                'pet': n_pet,
                'other': other_count,
            },
            events=events,
            fall_assessment=str(fall_assessment),
            posture_end=normalize_gt_posture(posture_end),
            risk=risk,
            traffic_class=str(traffic_class),
            conditions=dict(conditions),
        )


def infer_traffic_class(
    subjects_present: Sequence[str],
    events: Sequence[GroundTruthEvent],
    motion_summary: str,
) -> str:
    """Infer one mutually exclusive class for operational FPR estimates."""
    if any(event.event_type == 'fall' for event in events):
        return 'fall'
    if any(
        original.event_type
        in {normalize_gt_event(item) for item in HARD_NEGATIVE_EVENTS}
        for original in events
    ):
        return 'hard_negative'
    if any(
        subject in {'screen_or_reflection', 'doll_or_mannequin'}
        for subject in subjects_present
    ):
        return 'screen_or_reflection'
    if not subjects_present and motion_summary != 'none':
        return 'robot_motion_only'
    if not events:
        return 'no_event'
    return 'other_non_fall'


@dataclass(frozen=True)
class PredictedEvent:
    """One structurally and semantically valid model event."""

    event_type: str
    subject: str
    start_s: float
    end_s: float
    confidence: float


@dataclass(frozen=True)
class VlmPrediction:
    """Validated common VLM output."""

    subjects: Dict[str, int]
    events: Tuple[PredictedEvent, ...]
    fall_assessment: str
    fall_confidence: float
    fall_recovery: str
    posture_end: str
    risk: str
    risk_confidence: float
    camera_motion: str
    explanation_ko: str
    evidence_ko: Tuple[str, ...]
    uncertainty_flags: Tuple[str, ...]

    @property
    def fall_detected(self) -> bool:
        """Retain the v1 binary view for existing fall metrics."""
        return self.fall_assessment == 'confirmed_fall'

    @property
    def fall_recovered(self) -> Optional[bool]:
        """Map the explicit recovery state onto the legacy nullable form."""
        if self.fall_recovery == 'recovered':
            return True
        if self.fall_recovery == 'not_recovered':
            return False
        return None


@dataclass(frozen=True)
class PredictionRecord:
    """One model attempt and non-sensitive execution telemetry."""

    request_id: str
    case_id: str
    repetition: int
    provider: str
    model_id: str
    model_version: str
    model_region: str
    model_runtime: str
    model_contract_sha256: str
    track: str
    context_variant: str
    prompt_version: str
    prompt_sha256: Optional[str]
    observation_sha256: str
    input_spec: Dict[str, Any]
    input_spec_sha256: str
    prediction: Optional[VlmPrediction]
    schema_errors: Tuple[str, ...]
    semantic_errors: Tuple[str, ...]
    request_succeeded: bool
    error_type: Optional[str]
    total_latency_ms: Optional[float]
    first_token_latency_ms: Optional[float]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cached_input_tokens: Optional[int]
    retry_count: int

    @property
    def config_key(self) -> Tuple[str, ...]:
        return (
            self.provider,
            self.model_id,
            self.model_version,
            self.model_contract_sha256,
            self.track,
            self.context_variant,
            self.prompt_version,
            self.prompt_sha256 or '',
            self.input_spec_sha256,
        )

    @property
    def schema_valid(self) -> bool:
        return not self.schema_errors

    @property
    def semantic_valid(self) -> bool:
        return self.schema_valid and not self.semantic_errors

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        case: VlmEvaluationCase,
    ) -> 'PredictionRecord':
        """Validate the envelope and retain invalid model output as data."""
        if not isinstance(value, dict):
            raise ValueError('prediction record must be an object')
        _assert_keys(
            value,
            (
                'schema_version',
                'request_id',
                'case_id',
                'repetition',
                'model',
                'input',
                'prediction',
                'request_succeeded',
                'error_type',
                'telemetry',
            ),
            'record',
        )
        if value.get('schema_version') != 1:
            raise ValueError('prediction schema_version must be 1')
        request_id = _required_string(value, 'request_id', 'record')
        case_id = _required_string(value, 'case_id', 'record')
        if case_id != case.case_id:
            raise ValueError('prediction case_id does not match ground truth')
        repetition = _integer(value, 'repetition', 'record', minimum=1)
        model = _required_object(value, 'model', 'record')
        _assert_keys(
            model,
            (
                'provider',
                'id',
                'version',
                'region',
                'runtime',
                'hardware',
                'quantization',
                'weights_sha256',
            ),
            'model',
        )
        provider = _required_string(model, 'provider', 'model')
        model_id = _required_string(model, 'id', 'model')
        model_version = _required_string(model, 'version', 'model')
        model_region = _required_string(model, 'region', 'model')
        model_runtime = _required_string(model, 'runtime', 'model')
        for field in ('hardware', 'quantization'):
            if model.get(field) is not None and (
                not isinstance(model[field], str) or not model[field].strip()
            ):
                raise ValueError(f'model.{field} must be a string or null')
        weights_sha256 = model.get('weights_sha256')
        if weights_sha256 is not None and (
            not isinstance(weights_sha256, str)
            or len(weights_sha256) != 64
            or any(
                char not in '0123456789abcdef'
                for char in weights_sha256
            )
        ):
            raise ValueError('model.weights_sha256 is invalid')
        model_contract_sha256 = canonical_sha256(model)
        model_input = _required_object(value, 'input', 'record')
        _assert_keys(
            model_input,
            (
                'track',
                'context_variant',
                'prompt_version',
                'prompt_sha256',
                'observation_sha256',
                'input_spec',
            ),
            'input',
        )
        track = _enum(model_input, 'track', TRACKS, 'input')
        context_variant = _enum(
            model_input,
            'context_variant',
            CONTEXT_VARIANTS,
            'input',
        )
        prompt_version = _required_string(
            model_input,
            'prompt_version',
            'input',
        )
        prompt_sha256 = model_input.get('prompt_sha256')
        if prompt_sha256 is not None and (
            not isinstance(prompt_sha256, str)
            or len(prompt_sha256) != 64
            or any(
                char not in '0123456789abcdef'
                for char in prompt_sha256
            )
        ):
            raise ValueError('input.prompt_sha256 must be lowercase SHA-256')
        observation_sha256 = model_input.get('observation_sha256')
        if (
            not isinstance(observation_sha256, str)
            or len(observation_sha256) != 64
            or any(
                char not in '0123456789abcdef'
                for char in observation_sha256
            )
        ):
            raise ValueError('input.observation_sha256 must be SHA-256')
        input_spec = _required_object(model_input, 'input_spec', 'input')
        _assert_keys(
            input_spec,
            (
                'sampling',
                'effective_fps',
                'frame_count',
                'resolution',
                'audio_included',
                'preprocessing_sha256',
                'structured_output_mode',
                'decoding',
            ),
            'input.input_spec',
        )
        _required_string(input_spec, 'sampling', 'input.input_spec')
        if input_spec.get('effective_fps') is not None:
            _number(
                input_spec,
                'effective_fps',
                'input.input_spec',
                minimum=0.001,
            )
        if input_spec.get('frame_count') is not None:
            _integer(
                input_spec,
                'frame_count',
                'input.input_spec',
                minimum=1,
            )
        _required_string(input_spec, 'resolution', 'input.input_spec')
        if not isinstance(input_spec.get('audio_included'), bool):
            raise ValueError(
                'input.input_spec.audio_included must be a boolean'
            )
        preprocessing_sha256 = input_spec.get('preprocessing_sha256')
        if preprocessing_sha256 is not None and (
            not isinstance(preprocessing_sha256, str)
            or len(preprocessing_sha256) != 64
            or any(
                char not in '0123456789abcdef'
                for char in preprocessing_sha256
            )
        ):
            raise ValueError(
                'input.input_spec.preprocessing_sha256 is invalid'
            )
        _required_string(
            input_spec,
            'structured_output_mode',
            'input.input_spec',
        )
        decoding = input_spec.get('decoding')
        if not isinstance(decoding, dict):
            raise ValueError('input.input_spec.decoding must be an object')
        _assert_keys(
            decoding,
            (
                'temperature',
                'top_p',
                'seed',
                'max_output_tokens',
                'reasoning_mode',
                'thinking_budget',
            ),
            'input.input_spec.decoding',
        )
        input_spec_dict = dict(input_spec)
        input_configuration = {
            key: item
            for key, item in input_spec_dict.items()
            if key not in {'effective_fps', 'frame_count'}
        }
        input_spec_sha256 = canonical_sha256(input_configuration)

        telemetry = value.get('telemetry', {})
        if not isinstance(telemetry, dict):
            raise ValueError('record.telemetry must be an object')
        error_type = value.get('error_type')
        if error_type is not None and (
            not isinstance(error_type, str) or not error_type.strip()
        ):
            raise ValueError('record.error_type must be a string or null')
        request_succeeded = value.get('request_succeeded', error_type is None)
        if not isinstance(request_succeeded, bool):
            raise ValueError('record.request_succeeded must be a boolean')
        retry_count = telemetry.get('retry_count', 0)
        if (
            isinstance(retry_count, bool)
            or not isinstance(retry_count, int)
            or retry_count < 0
        ):
            raise ValueError('telemetry.retry_count must be a nonnegative int')
        token_fields: Dict[str, Optional[int]] = {}
        for field in (
            'input_tokens',
            'output_tokens',
            'cached_input_tokens',
        ):
            item = telemetry.get(field)
            if item is not None and (
                isinstance(item, bool)
                or not isinstance(item, int)
                or item < 0
            ):
                raise ValueError(f'telemetry.{field} is invalid')
            token_fields[field] = item
        latency_fields: Dict[str, Optional[float]] = {}
        for field in ('total_latency_ms', 'first_token_latency_ms'):
            item = telemetry.get(field)
            if item is not None and (
                not _is_number(item) or float(item) < 0
            ):
                raise ValueError(f'telemetry.{field} is invalid')
            latency_fields[field] = (
                float(item) if item is not None else None
            )

        raw_prediction = value.get('prediction')
        prediction = None
        schema_errors: List[str] = []
        semantic_errors: List[str] = []
        if raw_prediction is None:
            schema_errors.append('prediction:missing')
        elif not isinstance(raw_prediction, dict):
            schema_errors.append('prediction:type')
        else:
            prediction, schema_errors, semantic_errors = (
                validate_prediction(raw_prediction, case.duration_s)
            )
        return cls(
            request_id=request_id,
            case_id=case_id,
            repetition=repetition,
            provider=provider,
            model_id=model_id,
            model_version=model_version,
            model_region=model_region,
            model_runtime=model_runtime,
            model_contract_sha256=model_contract_sha256,
            track=track,
            context_variant=context_variant,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256,
            observation_sha256=observation_sha256,
            input_spec=input_spec_dict,
            input_spec_sha256=input_spec_sha256,
            prediction=prediction,
            schema_errors=tuple(schema_errors),
            semantic_errors=tuple(semantic_errors),
            request_succeeded=request_succeeded,
            error_type=error_type.strip() if error_type else None,
            total_latency_ms=latency_fields['total_latency_ms'],
            first_token_latency_ms=(
                latency_fields['first_token_latency_ms']
            ),
            input_tokens=token_fields['input_tokens'],
            output_tokens=token_fields['output_tokens'],
            cached_input_tokens=token_fields['cached_input_tokens'],
            retry_count=retry_count,
        )


def validate_prediction(
    value: Mapping[str, Any],
    duration_s: float,
) -> Tuple[Optional[VlmPrediction], List[str], List[str]]:
    """Validate strict output shape, then cross-field semantics."""
    schema_errors: List[str] = []
    semantic_errors: List[str] = []
    allowed = (
        'subjects',
        'events',
        'fall',
        'posture_end',
        'risk',
        'risk_confidence',
        'camera_motion_observed',
        'explanation_ko',
        'evidence_ko',
        'uncertainty_flags',
    )
    try:
        _assert_keys(value, allowed, 'prediction')
        subjects = _required_object(value, 'subjects', 'prediction')
        _assert_keys(subjects, SUBJECTS, 'prediction.subjects')
        parsed_subjects = {
            subject: _integer(
                subjects,
                subject,
                'prediction.subjects',
                maximum=20,
            )
            for subject in SUBJECTS
        }
        raw_events = value.get('events')
        if not isinstance(raw_events, list):
            raise ValueError('prediction.events must be a list')
        if len(raw_events) > 32:
            raise ValueError('prediction.events must contain at most 32 items')
        parsed_events = []
        for index, event in enumerate(raw_events):
            where = f'prediction.events[{index}]'
            if not isinstance(event, dict):
                raise ValueError(f'{where} must be an object')
            _assert_keys(
                event,
                ('type', 'subject', 'start_s', 'end_s', 'confidence'),
                where,
            )
            parsed_events.append(
                PredictedEvent(
                    event_type=_enum(
                        event,
                        'type',
                        PREDICTED_EVENT_TYPES,
                        where,
                    ),
                    subject=_enum(event, 'subject', SUBJECTS, where),
                    start_s=_number(event, 'start_s', where, minimum=0),
                    end_s=_number(event, 'end_s', where, minimum=0),
                    confidence=_number(
                        event,
                        'confidence',
                        where,
                        minimum=0,
                        maximum=1,
                    ),
                )
            )
        fall = _required_object(value, 'fall', 'prediction')
        _assert_keys(fall, ('assessment', 'confidence', 'recovery'), 'fall')
        fall_assessment = _enum(
            fall,
            'assessment',
            FALL_ASSESSMENTS,
            'prediction.fall',
        )
        fall_confidence = _number(
            fall,
            'confidence',
            'prediction.fall',
            minimum=0,
            maximum=1,
        )
        fall_recovery = _enum(
            fall,
            'recovery',
            RECOVERY_STATES,
            'prediction.fall',
        )
        posture_end = _enum(
            value,
            'posture_end',
            PREDICTED_POSTURES,
            'prediction',
        )
        risk = _enum(value, 'risk', RISKS, 'prediction')
        risk_confidence = _number(
            value,
            'risk_confidence',
            'prediction',
            minimum=0,
            maximum=1,
        )
        camera_motion = _enum(
            value,
            'camera_motion_observed',
            MOTION_SUMMARIES,
            'prediction',
        )
        explanation = value.get('explanation_ko')
        if not isinstance(explanation, str) or len(explanation) > 500:
            raise ValueError(
                'prediction.explanation_ko must be a string up to 500 chars'
            )
        evidence = value.get('evidence_ko')
        if not isinstance(evidence, list) or any(
            not isinstance(item, str) or not item.strip() or len(item) > 300
            for item in evidence
        ):
            raise ValueError(
                'prediction.evidence_ko must contain non-empty strings '
                'up to 300 chars'
            )
        if len(evidence) > 8:
            raise ValueError(
                'prediction.evidence_ko must contain at most 8 items'
            )
        flags = value.get('uncertainty_flags')
        if not isinstance(flags, list) or any(
            item not in UNCERTAINTY_FLAGS for item in flags
        ):
            raise ValueError(
                'prediction.uncertainty_flags contains an unknown value'
            )
        if len(set(flags)) != len(flags):
            raise ValueError(
                'prediction.uncertainty_flags contains duplicates'
            )
    except ValueError as error:
        schema_errors.append(str(error))
        return None, schema_errors, semantic_errors

    for event in parsed_events:
        if event.end_s < event.start_s or event.end_s > duration_s:
            semantic_errors.append('event_time:outside_clip')
        if parsed_subjects[event.subject] < 1:
            semantic_errors.append('event_subject:count_zero')
    has_fall_event = any(
        event.event_type == 'fall' for event in parsed_events
    )
    if (fall_assessment == 'confirmed_fall') != has_fall_event:
        semantic_errors.append('fall:event_disagreement')
    if ((fall_confidence >= 0.5) != (
        fall_assessment == 'confirmed_fall'
    )):
        semantic_errors.append('fall:probability_assessment_disagreement')
    if (
        fall_assessment == 'normal_activity'
        and fall_recovery != 'unknown'
    ):
        semantic_errors.append('fall:recovered_without_detection')
    if (
        fall_assessment == 'found_down'
        and (parsed_subjects['person'] < 1 or posture_end != 'lying_floor')
    ):
        semantic_errors.append('fall:found_down_without_floor_person')
    if not explanation.strip():
        semantic_errors.append('explanation_ko:empty')

    prediction = VlmPrediction(
        subjects=parsed_subjects,
        events=tuple(parsed_events),
        fall_assessment=fall_assessment,
        fall_confidence=fall_confidence,
        fall_recovery=fall_recovery,
        posture_end=posture_end,
        risk=risk,
        risk_confidence=risk_confidence,
        camera_motion=camera_motion,
        explanation_ko=explanation,
        evidence_ko=tuple(evidence),
        uncertainty_flags=tuple(flags),
    )
    return prediction, schema_errors, semantic_errors


def _load_jsonl(path: Path) -> List[Mapping[str, Any]]:
    rows: List[Mapping[str, Any]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding='utf-8').splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f'{path}: invalid JSONL at line {line_number}'
            ) from error
        if not isinstance(value, dict):
            raise ValueError(
                f'{path}: line {line_number} must be an object'
            )
        rows.append(value)
    if not rows:
        raise ValueError(f'{path}: JSONL file is empty')
    return rows


def load_vlm_cases(
    path: Path,
    *,
    require_media: bool = False,
) -> List[VlmEvaluationCase]:
    """Load a unique, versioned ground-truth manifest."""
    cases = [VlmEvaluationCase.from_dict(row) for row in _load_jsonl(path)]
    seen = set()
    base = path.resolve().parent
    for case in cases:
        if case.case_id in seen:
            raise ValueError(f'duplicate evaluation case: {case.case_id}')
        seen.add(case.case_id)
        if require_media:
            if not case.media_path:
                raise ValueError(f'{case.case_id}: clip.path is missing')
            if not case.media_sha256:
                raise ValueError(f'{case.case_id}: clip.sha256 is required')
            media = Path(case.media_path).expanduser()
            if not media.is_absolute():
                media = base / media
            if not media.is_file():
                raise ValueError(f'{case.case_id}: media does not exist')
            actual = hashlib.sha256(media.read_bytes()).hexdigest()
            if actual != case.media_sha256:
                raise ValueError(
                    f'{case.case_id}: media SHA-256 does not match'
                )
    return cases


def load_prediction_records(
    path: Path,
    cases: Sequence[VlmEvaluationCase],
) -> List[PredictionRecord]:
    """Load attempts and reject ambiguous duplicates or unknown cases."""
    by_id = {case.case_id: case for case in cases}
    records = []
    seen = set()
    request_ids = set()
    for row in _load_jsonl(path):
        case_id = row.get('case_id')
        if case_id not in by_id:
            raise ValueError(f'prediction references unknown case: {case_id}')
        record = PredictionRecord.from_dict(row, by_id[str(case_id)])
        if record.request_id in request_ids:
            raise ValueError(
                f'duplicate prediction request_id: {record.request_id}'
            )
        request_ids.add(record.request_id)
        identity = record.config_key + (
            record.case_id,
            str(record.repetition),
        )
        if identity in seen:
            raise ValueError(
                'duplicate prediction for configuration, case, repetition'
            )
        seen.add(identity)
        records.append(record)
    return records
