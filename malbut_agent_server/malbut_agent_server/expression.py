"""Non-actuating visual-expression contract for SWM25-77.

This module deliberately contains no ROS, network, file, audio, or motion
adapter.  It turns a final agent decision into a bounded presentation cue and
offers a process-local arbiter that can be exercised with no-op or recording
renderers.  A future frontend or ROS bridge must remain a separate, trusted
consumer of this contract.
"""

import hashlib
import json
import math
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Deque, Dict, Optional, Protocol, Tuple

from malbut_agent_server.safety import SafetyResult
from malbut_agent_server.schemas import AgentDecision


ALLOWED_EMOTIONS = frozenset(
    {
        'neutral',
        'happy',
        'concerned',
        'excited',
        'apologetic',
    }
)
VISUAL_MODALITY = 'visual'
DETERMINISTIC_SOURCE = 'deterministic_final_decision'
MIN_DURATION_MS = 250
MAX_DURATION_MS = 5000
MAX_DISPATCH_TTL_MS = 1000
MAX_ASSISTANT_INTENSITY = 0.7

_MAPPED_REASONS = {
    'greeting': ('happy', 0.5),
    'thanks': ('happy', 0.5),
    'apology': ('apologetic', 0.35),
    'celebration': ('excited', 0.65),
}
_LOCAL_CUE_AUTHORITY = object()


class ExpressionValidationError(ValueError):
    """Raised when an expression boundary value is invalid."""


class ExpressionConflictError(ExpressionValidationError):
    """Raised when one request ID is reused for a different cue."""


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ExpressionValidationError(f'{field_name} must be a string')
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ExpressionValidationError(f'{field_name} is invalid')
    if any(ord(item) < 32 or ord(item) == 127 for item in normalized):
        raise ExpressionValidationError(
            f'{field_name} must not contain control characters'
        )
    return normalized


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExpressionValidationError(f'{field_name} must be a number')
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ExpressionValidationError(f'{field_name} must be finite')
    return normalized


@dataclass(frozen=True)
class ExpressionCue:
    """One bounded, visual-only presentation suggestion.

    ``issued_at`` uses the arbiter's monotonic clock.  It is intentionally not
    a wall-clock timestamp and must never be used as a cross-process lease.
    """

    request_id: str
    cue_id: str
    emotion: str
    intensity: float
    duration_ms: int
    issued_at: float
    ttl_ms: int = MAX_DISPATCH_TTL_MS
    modality: str = VISUAL_MODALITY
    source: str = DETERMINISTIC_SOURCE
    _authority: object = field(
        default=None,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_dict(cls, value: Any) -> 'ExpressionCue':
        """Parse one strict JSON-style cue without accepting extensions."""
        if not isinstance(value, dict):
            raise ExpressionValidationError('expression cue must be an object')
        allowed = {
            'request_id',
            'cue_id',
            'emotion',
            'intensity',
            'duration_ms',
            'issued_at',
            'ttl_ms',
            'modality',
            'source',
        }
        unknown = set(value) - allowed
        if unknown:
            names = ', '.join(sorted(unknown))
            raise ExpressionValidationError(
                f'expression cue contains unknown fields: {names}'
            )
        missing = allowed - set(value)
        if missing:
            names = ', '.join(sorted(missing))
            raise ExpressionValidationError(
                f'expression cue is missing required fields: {names}'
            )
        return cls(
            request_id=value['request_id'],
            cue_id=value['cue_id'],
            emotion=value['emotion'],
            intensity=value['intensity'],
            duration_ms=value['duration_ms'],
            issued_at=value['issued_at'],
            ttl_ms=value['ttl_ms'],
            modality=value['modality'],
            source=value['source'],
        )

    def __post_init__(self) -> None:
        """Normalize identifiers and reject unsafe presentation values."""
        object.__setattr__(
            self,
            'request_id',
            _identifier(self.request_id, 'request_id'),
        )
        object.__setattr__(
            self,
            'cue_id',
            _identifier(self.cue_id, 'cue_id'),
        )
        if (
            not isinstance(self.emotion, str)
            or self.emotion not in ALLOWED_EMOTIONS
        ):
            raise ExpressionValidationError('emotion is not supported')
        if self.modality != VISUAL_MODALITY:
            raise ExpressionValidationError('only visual modality is allowed')
        if self.source != DETERMINISTIC_SOURCE:
            raise ExpressionValidationError(
                'expression source is not locally authorized'
            )
        intensity = _finite_number(self.intensity, 'intensity')
        if intensity < 0 or intensity > MAX_ASSISTANT_INTENSITY:
            raise ExpressionValidationError(
                'intensity is outside the assistant presentation range'
            )
        if self.emotion == 'neutral' and intensity != 0:
            raise ExpressionValidationError(
                'neutral expression intensity must be zero'
            )
        if self.emotion != 'neutral' and intensity <= 0:
            raise ExpressionValidationError(
                'non-neutral expression intensity must be positive'
            )
        object.__setattr__(self, 'intensity', intensity)
        if isinstance(self.duration_ms, bool) or not isinstance(
            self.duration_ms, int
        ):
            raise ExpressionValidationError('duration_ms must be an integer')
        if not MIN_DURATION_MS <= self.duration_ms <= MAX_DURATION_MS:
            raise ExpressionValidationError('duration_ms is outside bounds')
        if isinstance(self.ttl_ms, bool) or not isinstance(self.ttl_ms, int):
            raise ExpressionValidationError('ttl_ms must be an integer')
        if not 1 <= self.ttl_ms <= MAX_DISPATCH_TTL_MS:
            raise ExpressionValidationError('ttl_ms is outside bounds')
        issued_at = _finite_number(self.issued_at, 'issued_at')
        if issued_at < 0:
            raise ExpressionValidationError('issued_at must not be negative')
        object.__setattr__(self, 'issued_at', issued_at)

    def to_dict(self) -> Dict[str, object]:
        """Return the bounded cue without any user text or model context."""
        return {
            'request_id': self.request_id,
            'cue_id': self.cue_id,
            'emotion': self.emotion,
            'intensity': self.intensity,
            'duration_ms': self.duration_ms,
            'issued_at': self.issued_at,
            'ttl_ms': self.ttl_ms,
            'modality': self.modality,
            'source': self.source,
        }


@dataclass(frozen=True)
class TrustedExpressionState:
    """Trusted local overrides that never originate in model output."""

    emergency_active: bool = False
    privacy_mode: bool = False
    renderer_available: bool = True

    def __post_init__(self) -> None:
        """Require exact booleans for every trusted override field."""
        for field_name in (
            'emergency_active',
            'privacy_mode',
            'renderer_available',
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ExpressionValidationError(
                    f'{field_name} must be a boolean'
                )


@dataclass(frozen=True)
class ExpressionPolicyResult:
    """Deterministic local policy result before rate limiting or rendering."""

    allowed: bool
    code: str


class ExpressionPolicy:
    """Apply trusted override priority and cue freshness fail-closed."""

    def evaluate(
        self,
        cue: ExpressionCue,
        state: TrustedExpressionState,
        now: float,
    ) -> ExpressionPolicyResult:
        """Evaluate emergency before privacy, availability, then freshness."""
        if not isinstance(cue, ExpressionCue):
            raise ExpressionValidationError('cue must be ExpressionCue')
        if not isinstance(state, TrustedExpressionState):
            raise ExpressionValidationError(
                'state must be TrustedExpressionState'
            )
        current = _finite_number(now, 'now')
        if current < 0:
            raise ExpressionValidationError('now must not be negative')
        if state.emergency_active:
            return ExpressionPolicyResult(False, 'emergency_override')
        if state.privacy_mode:
            return ExpressionPolicyResult(False, 'privacy_override')
        if not state.renderer_available:
            return ExpressionPolicyResult(False, 'renderer_unavailable')
        deadline = cue.issued_at + cue.ttl_ms / 1000.0
        if current < cue.issued_at:
            return ExpressionPolicyResult(False, 'future_cue')
        if current >= deadline:
            return ExpressionPolicyResult(False, 'stale_cue')
        return ExpressionPolicyResult(True, 'allowed')


@dataclass(frozen=True)
class RenderedExpression:
    """One content-free call captured by the offline recording renderer."""

    request_id: str
    emotion: str
    intensity: float
    duration_ms: int


class VisualExpressionRenderer(Protocol):
    """Narrow visual-only boundary; it exposes no motion or audio method."""

    def render_visual(
        self,
        request_id: str,
        emotion: str,
        intensity: float,
        duration_ms: int,
    ) -> None:
        """Accept one already validated visual expression."""


class NoopVisualExpressionRenderer:
    """Renderer that intentionally performs no I/O or state change."""

    def render_visual(
        self,
        request_id: str,
        emotion: str,
        intensity: float,
        duration_ms: int,
    ) -> None:
        """Discard one validated command without side effects."""
        del request_id, emotion, intensity, duration_ms


class RecordingVisualExpressionRenderer:
    """In-memory renderer for deterministic tests and demonstrations."""

    def __init__(self) -> None:
        """Create an empty in-memory command list."""
        self.calls = []

    def render_visual(
        self,
        request_id: str,
        emotion: str,
        intensity: float,
        duration_ms: int,
    ) -> None:
        """Record one command without ROS, files, network, GUI, or hardware."""
        self.calls.append(
            RenderedExpression(
                request_id=request_id,
                emotion=emotion,
                intensity=intensity,
                duration_ms=duration_ms,
            )
        )


@dataclass(frozen=True)
class ActiveExpression:
    """Current assistant expression in the process-local presentation lane."""

    request_id: str
    cue_id: str
    emotion: str
    intensity: float
    started_at: float
    expires_at: float


@dataclass(frozen=True)
class ExpressionResult:
    """Auditable result of one cue submission or expiry fallback."""

    result_id: str
    request_id: str
    cue_id: str
    status: str
    code: str
    requested_emotion: str
    rendered_emotion: Optional[str]
    started_at: float
    expires_at: Optional[float]
    fallback_used: bool = False
    renderer_error: bool = False
    cached: bool = False

    def to_dict(self) -> Dict[str, object]:
        """Return content-free diagnostics safe for local logs."""
        return {
            'result_id': self.result_id,
            'request_id': self.request_id,
            'cue_id': self.cue_id,
            'status': self.status,
            'code': self.code,
            'requested_emotion': self.requested_emotion,
            'rendered_emotion': self.rendered_emotion,
            'started_at': self.started_at,
            'expires_at': self.expires_at,
            'fallback_used': self.fallback_used,
            'renderer_error': self.renderer_error,
            'cached': self.cached,
        }


def map_final_decision_to_expression(
    request_id: str,
    decision: AgentDecision,
    safety: SafetyResult,
    *,
    issued_at: Optional[float] = None,
) -> ExpressionCue:
    """Map only final decision metadata, never user text, into one cue.

    The fixed mapping describes Malbut's presentation.  It does not inspect or
    label a user's or pet's emotion, psychology, health, face, voice, memory,
    or conversation history.
    """
    normalized_request = _identifier(request_id, 'request_id')
    if not isinstance(decision, AgentDecision):
        raise ExpressionValidationError('decision must be AgentDecision')
    if not isinstance(safety, SafetyResult):
        raise ExpressionValidationError('safety must be SafetyResult')
    if not isinstance(safety.allowed, bool):
        raise ExpressionValidationError('safety allowed must be a boolean')
    if not isinstance(safety.code, str) or not safety.code:
        raise ExpressionValidationError('safety code is invalid')
    decision.validate()
    if not safety.allowed or decision.type == 'refusal':
        emotion, intensity = 'concerned', 0.35
    else:
        emotion, intensity = _MAPPED_REASONS.get(
            decision.reason,
            ('neutral', 0.0),
        )
    current = time.monotonic() if issued_at is None else issued_at
    normalized_time = _finite_number(current, 'issued_at')
    identity = json.dumps(
        {
            'request_id': normalized_request,
            'decision_type': decision.type,
            'decision_reason': decision.reason,
            'safety_allowed': safety.allowed,
            'safety_code': safety.code,
            'emotion': emotion,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    cue_id = 'expr-' + hashlib.sha256(identity).hexdigest()[:32]
    return ExpressionCue(
        request_id=normalized_request,
        cue_id=cue_id,
        emotion=emotion,
        intensity=intensity,
        duration_ms=(1000 if emotion == 'neutral' else 1500),
        issued_at=normalized_time,
        _authority=_LOCAL_CUE_AUTHORITY,
    )


class ExpressionArbiter:
    """Thread-safe, process-local visual expression state machine."""

    def __init__(
        self,
        renderer: VisualExpressionRenderer,
        *,
        policy: Optional[ExpressionPolicy] = None,
        clock: Callable[[], float] = time.monotonic,
        minimum_interval_seconds: float = 2.0,
        rate_window_seconds: float = 60.0,
        max_non_neutral_per_window: int = 6,
        cache_size: int = 256,
    ) -> None:
        """Configure bounded replay cache, rate policy, and monotonic clock."""
        if not callable(getattr(renderer, 'render_visual', None)):
            raise ValueError('renderer must implement render_visual')
        if not callable(clock):
            raise ValueError('clock must be callable')
        self.minimum_interval_seconds = self._positive_number(
            minimum_interval_seconds,
            'minimum_interval_seconds',
            maximum=60.0,
        )
        self.rate_window_seconds = self._positive_number(
            rate_window_seconds,
            'rate_window_seconds',
            maximum=3600.0,
        )
        if (
            isinstance(max_non_neutral_per_window, bool)
            or not isinstance(max_non_neutral_per_window, int)
            or not 1 <= max_non_neutral_per_window <= 1000
        ):
            raise ValueError('max_non_neutral_per_window is invalid')
        if (
            isinstance(cache_size, bool)
            or not isinstance(cache_size, int)
            or not 1 <= cache_size <= 10000
        ):
            raise ValueError('cache_size is invalid')
        self.renderer = renderer
        self.policy = policy or ExpressionPolicy()
        self.clock = clock
        self.max_non_neutral_per_window = max_non_neutral_per_window
        self.cache_size = cache_size
        self._cache: OrderedDict[
            str, Tuple[str, ExpressionResult]
        ] = OrderedDict()
        self._recent_dispatches: Deque[float] = deque()
        self._active: Optional[ActiveExpression] = None
        self._renderer_disabled = False
        self._lock = threading.RLock()

    @staticmethod
    def _positive_number(
        value: object,
        field_name: str,
        *,
        maximum: float,
    ) -> float:
        try:
            normalized = _finite_number(value, field_name)
        except ExpressionValidationError as error:
            raise ValueError(str(error)) from error
        if normalized <= 0 or normalized > maximum:
            raise ValueError(f'{field_name} is invalid')
        return normalized

    @property
    def active(self) -> Optional[ActiveExpression]:
        """Return the current immutable snapshot without advancing time."""
        with self._lock:
            return self._active

    def submit(
        self,
        cue: ExpressionCue,
        state: TrustedExpressionState,
    ) -> ExpressionResult:
        """Apply local policy and render a cue at most once per request ID."""
        if not isinstance(cue, ExpressionCue):
            raise ExpressionValidationError('cue must be ExpressionCue')
        if not isinstance(state, TrustedExpressionState):
            raise ExpressionValidationError(
                'state must be TrustedExpressionState'
            )
        if cue._authority is not _LOCAL_CUE_AUTHORITY:
            raise ExpressionValidationError(
                'cue was not produced by the local decision mapper'
            )
        fingerprint = self._fingerprint(cue)
        with self._lock:
            cached = self._cache.get(cue.request_id)
            conflict = False
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if fingerprint != cached_fingerprint:
                    conflict = True
            now = self._now()
            policy_result = self.policy.evaluate(cue, state, now)
            if policy_result.code in {
                'emergency_override',
                'privacy_override',
                'renderer_unavailable',
            }:
                result = self._suppressed_result(
                    cue,
                    state,
                    policy_result.code,
                    now,
                )
                if conflict:
                    raise ExpressionConflictError(
                        'request_id was already used with a different cue'
                    )
                if cached is not None:
                    self._cache.move_to_end(cue.request_id)
                    return replace(result, cached=True)
                return self._remember(cue, fingerprint, result)
            if conflict:
                raise ExpressionConflictError(
                    'request_id was already used with a different cue'
                )
            if self._renderer_disabled:
                self._active = None
                result = self._result(
                    cue,
                    status='suppressed',
                    code='renderer_unavailable',
                    now=now,
                    renderer_error=True,
                )
                if cached is not None:
                    self._cache.move_to_end(cue.request_id)
                    return replace(result, cached=True)
                return self._remember(cue, fingerprint, result)
            self._expire_locked(now)
            if cached is not None:
                self._cache.move_to_end(cue.request_id)
                return replace(cached_result, cached=True)
            if not policy_result.allowed:
                result = self._result(
                    cue,
                    status='suppressed',
                    code=policy_result.code,
                    now=now,
                )
                return self._remember(cue, fingerprint, result)
            if cue.emotion == 'neutral' and self._active is None:
                result = self._result(
                    cue,
                    status='succeeded',
                    code='already_neutral',
                    now=now,
                    rendered_emotion='neutral',
                )
                return self._remember(cue, fingerprint, result)
            if cue.emotion != 'neutral' and self._rate_limited(now):
                result = self._result(
                    cue,
                    status='suppressed',
                    code='rate_limited',
                    now=now,
                )
                return self._remember(cue, fingerprint, result)

            if cue.emotion != 'neutral':
                self._recent_dispatches.append(now)
            result = self._render_cue(cue, now)
            return self._remember(cue, fingerprint, result)

    def tick(
        self,
        state: TrustedExpressionState,
    ) -> Optional[ExpressionResult]:
        """Apply trusted overrides and expire one active cue when due."""
        if not isinstance(state, TrustedExpressionState):
            raise ExpressionValidationError(
                'state must be TrustedExpressionState'
            )
        with self._lock:
            now = self._now()
            active = self._active
            if active is None:
                return None
            if state.emergency_active:
                return self._clear_active_locked(
                    active,
                    state,
                    now,
                    'emergency_override',
                )
            if state.privacy_mode:
                return self._clear_active_locked(
                    active,
                    state,
                    now,
                    'privacy_override',
                )
            if not state.renderer_available or self._renderer_disabled:
                self._active = None
                return self._active_result(
                    active,
                    status='suppressed',
                    code='renderer_unavailable',
                    now=now,
                    renderer_error=True,
                )
            return self._expire_locked(now)

    def _suppressed_result(
        self,
        cue: ExpressionCue,
        state: TrustedExpressionState,
        code: str,
        now: float,
    ) -> ExpressionResult:
        if code not in {'emergency_override', 'privacy_override'}:
            if code == 'renderer_unavailable':
                self._active = None
            return self._result(
                cue,
                status='suppressed',
                code=code,
                now=now,
                renderer_error=(code == 'renderer_unavailable'),
            )
        active = self._active
        had_active = active is not None
        self._active = None
        if not had_active:
            return self._result(
                cue,
                status='suppressed',
                code=code,
                now=now,
            )
        if not state.renderer_available or self._renderer_disabled:
            return self._result(
                cue,
                status='suppressed',
                code=code,
                now=now,
                renderer_error=True,
            )
        if active is None:
            raise RuntimeError('active expression disappeared')
        rendered, renderer_error = self._try_neutral(active.request_id)
        return self._result(
            cue,
            status='suppressed',
            code=code,
            now=now,
            rendered_emotion=('neutral' if rendered else None),
            fallback_used=True,
            renderer_error=renderer_error,
        )

    def _render_cue(
        self,
        cue: ExpressionCue,
        now: float,
    ) -> ExpressionResult:
        try:
            self.renderer.render_visual(
                cue.request_id,
                cue.emotion,
                cue.intensity,
                cue.duration_ms,
            )
        except Exception:
            self._active = None
            if cue.emotion == 'neutral':
                self._renderer_disabled = True
                return self._result(
                    cue,
                    status='failed',
                    code='renderer_unavailable',
                    now=now,
                    renderer_error=True,
                )
            rendered, renderer_error = self._try_neutral(cue.request_id)
            return self._result(
                cue,
                status=('fallback' if rendered else 'failed'),
                code=(
                    'renderer_failed_neutral_fallback'
                    if rendered
                    else 'renderer_unavailable'
                ),
                now=now,
                rendered_emotion=('neutral' if rendered else None),
                fallback_used=True,
                renderer_error=True,
            )

        if cue.emotion == 'neutral':
            self._active = None
            expires_at = None
        else:
            expires_at = now + cue.duration_ms / 1000.0
            self._active = ActiveExpression(
                request_id=cue.request_id,
                cue_id=cue.cue_id,
                emotion=cue.emotion,
                intensity=cue.intensity,
                started_at=now,
                expires_at=expires_at,
            )
        return self._result(
            cue,
            status='succeeded',
            code='rendered',
            now=now,
            rendered_emotion=cue.emotion,
            expires_at=expires_at,
        )

    def _expire_locked(self, now: float) -> Optional[ExpressionResult]:
        active = self._active
        if active is None or now < active.expires_at:
            return None
        self._active = None
        rendered, renderer_error = self._try_neutral(active.request_id)
        return self._active_result(
            active,
            status=('succeeded' if rendered else 'failed'),
            code=(
                'expired_to_neutral'
                if rendered
                else 'renderer_unavailable'
            ),
            now=now,
            rendered_emotion=('neutral' if rendered else None),
            fallback_used=True,
            renderer_error=renderer_error,
        )

    def _clear_active_locked(
        self,
        active: ActiveExpression,
        state: TrustedExpressionState,
        now: float,
        code: str,
    ) -> ExpressionResult:
        self._active = None
        if not state.renderer_available or self._renderer_disabled:
            return self._active_result(
                active,
                status='suppressed',
                code=code,
                now=now,
                renderer_error=True,
            )
        rendered, renderer_error = self._try_neutral(active.request_id)
        return self._active_result(
            active,
            status='suppressed',
            code=code,
            now=now,
            rendered_emotion=('neutral' if rendered else None),
            fallback_used=True,
            renderer_error=renderer_error,
        )

    def _try_neutral(self, source_request_id: str) -> Tuple[bool, bool]:
        neutral_request = self._neutral_request_id(source_request_id)
        try:
            self.renderer.render_visual(
                neutral_request,
                'neutral',
                0.0,
                MIN_DURATION_MS,
            )
            return True, False
        except Exception:
            self._renderer_disabled = True
            return False, True

    def _rate_limited(self, now: float) -> bool:
        cutoff = now - self.rate_window_seconds
        while self._recent_dispatches and (
            self._recent_dispatches[0] <= cutoff
        ):
            self._recent_dispatches.popleft()
        if self._recent_dispatches and (
            now - self._recent_dispatches[-1]
            < self.minimum_interval_seconds
        ):
            return True
        return (
            len(self._recent_dispatches)
            >= self.max_non_neutral_per_window
        )

    def _remember(
        self,
        cue: ExpressionCue,
        fingerprint: str,
        result: ExpressionResult,
    ) -> ExpressionResult:
        self._cache[cue.request_id] = (fingerprint, result)
        self._cache.move_to_end(cue.request_id)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return result

    @staticmethod
    def _fingerprint(cue: ExpressionCue) -> str:
        value = cue.to_dict()
        value.pop('issued_at')
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _neutral_request_id(source_request_id: str) -> str:
        digest = hashlib.sha256(
            source_request_id.encode('utf-8')
        ).hexdigest()[:24]
        return f'neutral-{digest}'

    @staticmethod
    def _result(
        cue: ExpressionCue,
        *,
        status: str,
        code: str,
        now: float,
        rendered_emotion: Optional[str] = None,
        expires_at: Optional[float] = None,
        fallback_used: bool = False,
        renderer_error: bool = False,
    ) -> ExpressionResult:
        return ExpressionResult(
            result_id=str(uuid.uuid4()),
            request_id=cue.request_id,
            cue_id=cue.cue_id,
            status=status,
            code=code,
            requested_emotion=cue.emotion,
            rendered_emotion=rendered_emotion,
            started_at=now,
            expires_at=expires_at,
            fallback_used=fallback_used,
            renderer_error=renderer_error,
        )

    @staticmethod
    def _active_result(
        active: ActiveExpression,
        *,
        status: str,
        code: str,
        now: float,
        rendered_emotion: Optional[str] = None,
        fallback_used: bool = False,
        renderer_error: bool = False,
    ) -> ExpressionResult:
        return ExpressionResult(
            result_id=str(uuid.uuid4()),
            request_id=active.request_id,
            cue_id=active.cue_id,
            status=status,
            code=code,
            requested_emotion=active.emotion,
            rendered_emotion=rendered_emotion,
            started_at=now,
            expires_at=None,
            fallback_used=fallback_used,
            renderer_error=renderer_error,
        )

    def _now(self) -> float:
        return _finite_number(self.clock(), 'clock result')
