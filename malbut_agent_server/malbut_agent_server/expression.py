"""
Non-actuating visual-expression contract for SWM25-77.

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
DEFAULT_RENDER_TIMEOUT_SECONDS = 0.25

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
    """
    One bounded, visual-only presentation suggestion.

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
    """
    Trusted local overrides that never originate in model output.

    ``revision`` is a monotonic sequence owned by the trusted state source.
    A newer revision replaces the previous snapshot.  Within one revision,
    unsafe values may only become more restrictive, so a delayed normal
    snapshot cannot clear an emergency, privacy, or availability override.
    """

    emergency_active: bool = False
    privacy_mode: bool = False
    renderer_available: bool = True
    revision: int = 0

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
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or not 0 <= self.revision <= 2**63 - 1
        ):
            raise ExpressionValidationError(
                'revision must be a non-negative 64-bit integer'
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


@dataclass(frozen=True)
class ExpressionDispatchContext:
    """
    Cooperative cancellation and ordering fence for one renderer call.

    ``lane_id`` scopes monotonically increasing ``generation`` values to one
    arbiter lifetime.  An asynchronous adapter must propagate both values to
    its receiver, and that receiver must reject a generation lower than the
    highest one already observed for the lane.  The adapter must also check
    ``cancelled`` immediately before committing any local side effect.
    """

    lane_id: str
    generation: int
    _cancelled: threading.Event = field(repr=False, compare=False)

    @property
    def cancelled(self) -> bool:
        """Return whether a newer or safety-critical dispatch superseded it."""
        return self._cancelled.is_set()

    def to_metadata(self) -> Dict[str, object]:
        """Return content-free receiver-fence metadata."""
        return {
            'lane_id': self.lane_id,
            'generation': self.generation,
        }


class VisualExpressionRenderer(Protocol):
    """Narrow visual-only boundary with a cooperative generation fence."""

    def render_visual(
        self,
        request_id: str,
        emotion: str,
        intensity: float,
        duration_ms: int,
        dispatch: ExpressionDispatchContext,
    ) -> None:
        """Accept one validated cue if ``dispatch`` remains current."""


class NoopVisualExpressionRenderer:
    """Renderer that intentionally performs no I/O or state change."""

    def render_visual(
        self,
        request_id: str,
        emotion: str,
        intensity: float,
        duration_ms: int,
        dispatch: ExpressionDispatchContext,
    ) -> None:
        """Discard one validated command without side effects."""
        del request_id, emotion, intensity, duration_ms, dispatch


class RecordingVisualExpressionRenderer:
    """In-memory renderer for deterministic tests and demonstrations."""

    def __init__(self) -> None:
        """Create an empty in-memory command list."""
        self.calls = []
        self._highest_generation: Dict[str, int] = {}
        self._lock = threading.Lock()

    def render_visual(
        self,
        request_id: str,
        emotion: str,
        intensity: float,
        duration_ms: int,
        dispatch: ExpressionDispatchContext,
    ) -> None:
        """Record one command without ROS, files, network, GUI, or hardware."""
        with self._lock:
            highest = self._highest_generation.get(dispatch.lane_id, 0)
            if dispatch.cancelled or dispatch.generation < highest:
                return
            self._highest_generation[dispatch.lane_id] = dispatch.generation
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


@dataclass
class _PendingSubmission:
    """One in-flight cue shared by concurrent idempotent callers."""

    cue: ExpressionCue
    fingerprint: str
    token: ExpressionDispatchContext
    done: threading.Event = field(default_factory=threading.Event)
    result: Optional[ExpressionResult] = None
    cancel_code: Optional[str] = None


def map_final_decision_to_expression(
    request_id: str,
    decision: AgentDecision,
    safety: SafetyResult,
    *,
    issued_at: Optional[float] = None,
) -> ExpressionCue:
    """
    Map only final decision metadata, never user text, into one cue.

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
    """
    Thread-safe, process-local visual expression state machine.

    Renderer code always runs in a daemon worker outside ``_lock``.  Each
    invocation has a bounded wait and a generation token, so an emergency or
    privacy transition can cancel its logical result without waiting for a
    defective renderer to return.  Python cannot forcibly stop a blocked
    thread; a production adapter must additionally enforce the generation at
    its receiver before applying an external visual effect.
    """

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
        renderer_timeout_seconds: float = DEFAULT_RENDER_TIMEOUT_SECONDS,
    ) -> None:
        """Configure replay, rate, clock, and renderer timeout bounds."""
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
        self.renderer_timeout_seconds = self._positive_number(
            renderer_timeout_seconds,
            'renderer_timeout_seconds',
            maximum=5.0,
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
        self._lane_id = 'expression-lane-' + str(uuid.uuid4())
        self._generation = 0
        self._pending: Optional[_PendingSubmission] = None
        self._control_token: Optional[ExpressionDispatchContext] = None
        self._trusted_state: Optional[TrustedExpressionState] = None
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
        """Apply policy and dispatch a cue with bounded renderer waiting."""
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
        # Expiry and trusted overrides are handled first.  ``tick`` invokes
        # renderer code outside the arbiter lock and may cancel an in-flight
        # normal submission.
        transition = self.tick(state)
        fingerprint = self._fingerprint(cue)
        waiter: Optional[_PendingSubmission] = None
        cancelled_pending = False
        with self._lock:
            effective_state = self._merge_trusted_state_locked(state)
            cached = self._cache.get(cue.request_id)
            conflict = False
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if fingerprint != cached_fingerprint:
                    conflict = True
            now = self._now()
            policy_result = self.policy.evaluate(cue, effective_state, now)
            if policy_result.code in {
                'emergency_override',
                'privacy_override',
                'renderer_unavailable',
            }:
                result = self._result(
                    cue,
                    status='suppressed',
                    code=policy_result.code,
                    now=now,
                    rendered_emotion=(
                        transition.rendered_emotion
                        if transition is not None
                        else None
                    ),
                    fallback_used=(
                        transition.fallback_used
                        if transition is not None
                        else False
                    ),
                    renderer_error=(
                        policy_result.code == 'renderer_unavailable'
                        or (
                            transition.renderer_error
                            if transition is not None
                            else False
                        )
                    ),
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
            if cached is not None:
                self._cache.move_to_end(cue.request_id)
                return replace(cached_result, cached=True)
            if self._renderer_disabled:
                self._active = None
                result = self._result(
                    cue,
                    status='suppressed',
                    code='renderer_unavailable',
                    now=now,
                    renderer_error=True,
                )
                return self._remember(cue, fingerprint, result)
            if not policy_result.allowed:
                result = self._result(
                    cue,
                    status='suppressed',
                    code=policy_result.code,
                    now=now,
                )
                return self._remember(cue, fingerprint, result)
            if self._control_token is not None:
                result = self._result(
                    cue,
                    status='suppressed',
                    code='renderer_busy',
                    now=now,
                )
                return self._remember(cue, fingerprint, result)
            pending = self._pending
            if pending is not None:
                if pending.cue.request_id == cue.request_id:
                    if pending.fingerprint != fingerprint:
                        raise ExpressionConflictError(
                            'request_id was already used with a different cue'
                        )
                    waiter = pending
                elif cue.emotion == 'neutral':
                    self._cancel_pending_locked('superseded_to_neutral')
                    cancelled_pending = True
                else:
                    result = self._result(
                        cue,
                        status='suppressed',
                        code='renderer_busy',
                        now=now,
                    )
                    return self._remember(cue, fingerprint, result)
            if waiter is None and (
                cue.emotion == 'neutral' and self._active is None
                and not cancelled_pending
            ):
                result = self._result(
                    cue,
                    status='succeeded',
                    code='already_neutral',
                    now=now,
                    rendered_emotion='neutral',
                )
                return self._remember(cue, fingerprint, result)
            if waiter is None and (
                cue.emotion != 'neutral' and self._rate_limited(now)
            ):
                result = self._result(
                    cue,
                    status='suppressed',
                    code='rate_limited',
                    now=now,
                )
                return self._remember(cue, fingerprint, result)
            if waiter is None:
                if cue.emotion != 'neutral':
                    self._recent_dispatches.append(now)
                token = self._next_token_locked()
                pending = _PendingSubmission(cue, fingerprint, token)
                self._pending = pending
            else:
                pending = waiter

        if waiter is not None:
            wait_bound = self.renderer_timeout_seconds * 2.0 + 0.25
            if not waiter.done.wait(wait_bound):
                return self._result(
                    cue,
                    status='suppressed',
                    code='renderer_busy',
                    now=self._now(),
                )
            if waiter.result is None:
                raise RuntimeError('pending renderer result disappeared')
            return replace(waiter.result, cached=True)
        return self._execute_pending(pending)

    def tick(
        self,
        state: TrustedExpressionState,
    ) -> Optional[ExpressionResult]:
        """Apply overrides or expiry without holding the renderer lock."""
        if not isinstance(state, TrustedExpressionState):
            raise ExpressionValidationError(
                'state must be TrustedExpressionState'
            )
        with self._lock:
            state = self._merge_trusted_state_locked(state)
            now = self._now()
            active = self._active
            pending = self._pending
            code: Optional[str] = None
            if state.emergency_active:
                code = 'emergency_override'
            elif state.privacy_mode:
                code = 'privacy_override'
            elif not state.renderer_available or self._renderer_disabled:
                code = 'renderer_unavailable'
            elif active is not None and now >= active.expires_at:
                code = 'expired_to_neutral'
            if code is None:
                return None
            if active is None and pending is None:
                return None
            subject = active or self._active_from_pending(pending, now)
            self._active = None
            if pending is not None:
                self._cancel_pending_locked(code)
            can_render_neutral = (
                code != 'renderer_unavailable'
                and state.renderer_available
                and not self._renderer_disabled
            )
            token = None
            if can_render_neutral:
                if self._control_token is not None:
                    self._control_token._cancelled.set()
                token = self._next_token_locked()
                self._control_token = token

        if token is None:
            return self._active_result(
                subject,
                status=(
                    'suppressed'
                    if code != 'expired_to_neutral'
                    else 'failed'
                ),
                code=code,
                now=now,
                renderer_error=(code == 'renderer_unavailable'),
            )
        outcome = self._invoke_renderer(
            self._neutral_request_id(subject.request_id),
            'neutral',
            0.0,
            MIN_DURATION_MS,
            token,
        )
        with self._lock:
            owns_control = (
                self._control_token is token
                and self._generation == token.generation
            )
            if self._control_token is token:
                self._control_token = None
            if outcome in {'error', 'timeout'} and owns_control:
                self._renderer_disabled = True
            rendered = (
                outcome == 'succeeded'
                and owns_control
                and not token.cancelled
            )
        return self._active_result(
            subject,
            status=(
                'suppressed'
                if code != 'expired_to_neutral'
                else ('succeeded' if rendered else 'failed')
            ),
            code=(
                code
                if code != 'expired_to_neutral' or rendered
                else 'renderer_unavailable'
            ),
            now=now,
            rendered_emotion=('neutral' if rendered else None),
            fallback_used=True,
            renderer_error=(outcome in {'error', 'timeout'}),
        )

    def _execute_pending(
        self,
        pending: _PendingSubmission,
    ) -> ExpressionResult:
        """Execute one reserved submission and safely publish its result."""
        cue = pending.cue
        outcome = self._invoke_renderer(
            cue.request_id,
            cue.emotion,
            cue.intensity,
            cue.duration_ms,
            pending.token,
        )
        with self._lock:
            if pending.result is not None:
                return pending.result
            if not self._pending_is_current(pending):
                return self._finish_cancelled_pending_locked(pending, outcome)
            if outcome == 'succeeded':
                now = self._now()
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
                result = self._result(
                    cue,
                    status='succeeded',
                    code='rendered',
                    now=now,
                    rendered_emotion=cue.emotion,
                    expires_at=expires_at,
                )
                return self._finish_pending_locked(pending, result)
            if cue.emotion == 'neutral':
                self._active = None
                self._renderer_disabled = True
                result = self._result(
                    cue,
                    status='failed',
                    code='renderer_unavailable',
                    now=self._now(),
                    renderer_error=True,
                )
                return self._finish_pending_locked(pending, result)
            timed_out = outcome == 'timeout'
            fallback_token = self._next_token_locked()
            pending.token = fallback_token

        fallback_outcome = self._invoke_renderer(
            self._neutral_request_id(cue.request_id),
            'neutral',
            0.0,
            MIN_DURATION_MS,
            fallback_token,
        )
        with self._lock:
            if pending.result is not None:
                return pending.result
            if not self._pending_is_current(pending):
                return self._finish_cancelled_pending_locked(
                    pending,
                    fallback_outcome,
                )
            self._active = None
            rendered = fallback_outcome == 'succeeded'
            if timed_out or not rendered:
                # A timed-out adapter is unhealthy even if its one permitted
                # neutral fallback succeeds.  This bounds leaked daemon
                # workers to the failing request.
                self._renderer_disabled = True
            result = self._result(
                cue,
                status=('fallback' if rendered else 'failed'),
                code=(
                    'renderer_failed_neutral_fallback'
                    if rendered
                    else 'renderer_unavailable'
                ),
                now=self._now(),
                rendered_emotion=('neutral' if rendered else None),
                fallback_used=True,
                renderer_error=True,
            )
            return self._finish_pending_locked(pending, result)

    def _invoke_renderer(
        self,
        request_id: str,
        emotion: str,
        intensity: float,
        duration_ms: int,
        token: ExpressionDispatchContext,
    ) -> str:
        """Run an untrusted renderer in a bounded daemon-thread boundary."""
        completed = threading.Event()
        outcome = {'value': 'error'}

        def invoke() -> None:
            try:
                if token.cancelled:
                    outcome['value'] = 'cancelled'
                    return
                self.renderer.render_visual(
                    request_id,
                    emotion,
                    intensity,
                    duration_ms,
                    token,
                )
                outcome['value'] = 'succeeded'
            except BaseException:
                outcome['value'] = 'error'
            finally:
                completed.set()

        try:
            worker = threading.Thread(
                target=invoke,
                name=f'expression-render-{token.generation}',
                daemon=True,
            )
            worker.start()
        except BaseException:
            # Thread construction/start is part of the untrusted dispatch
            # boundary too.  Cancel a partially started worker and report a
            # normal renderer error so the pending reservation is completed.
            token._cancelled.set()
            return 'error'
        deadline = time.monotonic() + self.renderer_timeout_seconds
        while not completed.is_set():
            if token.cancelled:
                return 'cancelled'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                token._cancelled.set()
                return 'timeout'
            completed.wait(min(remaining, 0.01))
        if token.cancelled:
            return 'cancelled'
        return outcome['value']

    def _pending_is_current(self, pending: _PendingSubmission) -> bool:
        return (
            self._pending is pending
            and self._generation == pending.token.generation
            and pending.cancel_code is None
        )

    def _finish_cancelled_pending_locked(
        self,
        pending: _PendingSubmission,
        outcome: str,
    ) -> ExpressionResult:
        if pending.result is not None:
            return pending.result
        result = self._result(
            pending.cue,
            status='suppressed',
            code=pending.cancel_code or 'superseded',
            now=self._now(),
            renderer_error=(outcome in {'error', 'timeout'}),
        )
        return self._finish_pending_locked(pending, result)

    def _finish_pending_locked(
        self,
        pending: _PendingSubmission,
        result: ExpressionResult,
    ) -> ExpressionResult:
        if self._pending is pending:
            self._pending = None
        existing = self._cache.get(pending.cue.request_id)
        if existing is not None and existing[0] == pending.fingerprint:
            result = existing[1]
        elif existing is None:
            result = self._remember(
                pending.cue,
                pending.fingerprint,
                result,
            )
        pending.result = result
        pending.done.set()
        return result

    def _cancel_pending_locked(self, code: str) -> None:
        pending = self._pending
        if pending is None:
            return
        pending.cancel_code = code
        pending.token._cancelled.set()
        self._pending = None
        self._generation += 1
        result = self._result(
            pending.cue,
            status='suppressed',
            code=code,
            now=self._now(),
        )
        self._remember(pending.cue, pending.fingerprint, result)
        pending.result = result
        pending.done.set()

    def _next_token_locked(self) -> ExpressionDispatchContext:
        self._generation += 1
        return ExpressionDispatchContext(
            self._lane_id,
            self._generation,
            threading.Event(),
        )

    def _merge_trusted_state_locked(
        self,
        incoming: TrustedExpressionState,
    ) -> TrustedExpressionState:
        """Merge one ordered trusted snapshot without unsafe downgrades."""
        current = self._trusted_state
        if current is None or incoming.revision > current.revision:
            self._trusted_state = incoming
        elif incoming.revision == current.revision:
            self._trusted_state = TrustedExpressionState(
                emergency_active=(
                    current.emergency_active or incoming.emergency_active
                ),
                privacy_mode=current.privacy_mode or incoming.privacy_mode,
                renderer_available=(
                    current.renderer_available
                    and incoming.renderer_available
                ),
                revision=current.revision,
            )
        return self._trusted_state

    @staticmethod
    def _active_from_pending(
        pending: Optional[_PendingSubmission],
        now: float,
    ) -> ActiveExpression:
        if pending is None:
            raise RuntimeError('expression lane subject disappeared')
        cue = pending.cue
        return ActiveExpression(
            request_id=cue.request_id,
            cue_id=cue.cue_id,
            emotion=cue.emotion,
            intensity=cue.intensity,
            started_at=now,
            expires_at=now,
        )

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
