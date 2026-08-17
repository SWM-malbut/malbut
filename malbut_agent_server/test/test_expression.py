"""Offline contract tests for the SWM25-77 expression boundary."""

import math
import threading
import time
from dataclasses import replace
from typing import List

import pytest

from malbut_agent_server.expression import (
    DETERMINISTIC_SOURCE,
    MAX_DISPATCH_TTL_MS,
    VISUAL_MODALITY,
    ExpressionArbiter,
    ExpressionConflictError,
    ExpressionCue,
    ExpressionDispatchContext,
    ExpressionPolicy,
    ExpressionValidationError,
    NoopVisualExpressionRenderer,
    RecordingVisualExpressionRenderer,
    TrustedExpressionState,
    map_final_decision_to_expression,
)
from malbut_agent_server.safety import SafetyResult
from malbut_agent_server.schemas import AgentDecision
from malbut_agent_server.tools import TOOL_SPECS


class FakeClock:
    """Deterministic monotonic clock used without sleeping."""

    def __init__(self, value: float = 100.0) -> None:
        """Start the monotonic test clock at one explicit value."""
        self.value = value

    def __call__(self) -> float:
        """Return the current test time."""
        return self.value

    def advance(self, seconds: float) -> None:
        """Advance test time without sleeping."""
        self.value += seconds


def _decision(
    *,
    decision_type: str = 'message',
    reason: str = 'greeting',
    message: str = '안녕!',
) -> AgentDecision:
    return AgentDecision(
        type=decision_type,
        message=message,
        reason=reason,
        confidence=1.0,
    )


def _safety(
    allowed: bool = True,
    code: str = 'not_an_action',
) -> SafetyResult:
    return SafetyResult(allowed, code, 'test policy result')


def _cue(
    clock: FakeClock,
    request_id: str = 'expression-request-1',
    *,
    emotion: str = 'happy',
    intensity: float = 0.5,
    duration_ms: int = 1500,
    ttl_ms: int = MAX_DISPATCH_TTL_MS,
) -> ExpressionCue:
    mapped = map_final_decision_to_expression(
        request_id,
        _decision(),
        _safety(),
        issued_at=clock(),
    )
    return replace(
        mapped,
        emotion=emotion,
        intensity=(0.0 if emotion == 'neutral' else intensity),
        duration_ms=duration_ms,
        ttl_ms=ttl_ms,
    )


def test_cue_round_trip_is_strict_and_content_free() -> None:
    """Only the bounded visual contract crosses the renderer boundary."""
    clock = FakeClock()
    cue = _cue(clock)

    assert ExpressionCue.from_dict(cue.to_dict()) == cue
    assert cue.modality == VISUAL_MODALITY
    assert cue.source == DETERMINISTIC_SOURCE
    assert 'message' not in cue.to_dict()
    parsed = ExpressionCue.from_dict(cue.to_dict())
    with pytest.raises(ExpressionValidationError, match='local'):
        ExpressionArbiter(
            RecordingVisualExpressionRenderer(),
            clock=clock,
        ).submit(parsed, TrustedExpressionState())

    extra = cue.to_dict()
    extra['priority'] = 999
    with pytest.raises(ExpressionValidationError, match='unknown fields'):
        ExpressionCue.from_dict(extra)

    missing = cue.to_dict()
    missing.pop('ttl_ms')
    with pytest.raises(ExpressionValidationError, match='missing required'):
        ExpressionCue.from_dict(missing)


@pytest.mark.parametrize(
    ('field_name', 'value'),
    [
        ('emotion', 'angry'),
        ('emotion', []),
        ('modality', 'audio'),
        ('source', 'model_selected'),
        ('intensity', True),
        ('intensity', math.nan),
        ('intensity', 0.8),
        ('duration_ms', True),
        ('duration_ms', 249),
        ('duration_ms', 5001),
        ('ttl_ms', True),
        ('ttl_ms', 0),
        ('ttl_ms', 1001),
        ('issued_at', math.inf),
    ],
)
def test_cue_rejects_unsupported_or_unbounded_values(
    field_name: str,
    value: object,
) -> None:
    """Enums, numeric ranges, modality, source, and TTL fail closed."""
    data = _cue(FakeClock()).to_dict()
    data[field_name] = value
    with pytest.raises(ExpressionValidationError):
        ExpressionCue.from_dict(data)


def test_neutral_and_non_neutral_intensities_are_not_ambiguous() -> None:
    """Neutral is zero intensity and expressive cues are strictly positive."""
    clock = FakeClock()
    with pytest.raises(ExpressionValidationError, match='neutral'):
        replace(_cue(clock), emotion='neutral')
    with pytest.raises(ExpressionValidationError, match='non-neutral'):
        replace(_cue(clock), intensity=0.0)


def test_trusted_state_rejects_truthy_non_booleans() -> None:
    """Strings or integers cannot impersonate trusted local overrides."""
    with pytest.raises(ExpressionValidationError, match='boolean'):
        TrustedExpressionState(emergency_active=1)  # type: ignore[arg-type]
    with pytest.raises(ExpressionValidationError, match='boolean'):
        TrustedExpressionState(privacy_mode='false')  # type: ignore[arg-type]
    with pytest.raises(ExpressionValidationError, match='revision'):
        TrustedExpressionState(revision=True)  # type: ignore[arg-type]
    with pytest.raises(ExpressionValidationError, match='revision'):
        TrustedExpressionState(revision=-1)


@pytest.mark.parametrize(
    ('reason', 'emotion'),
    [
        ('greeting', 'happy'),
        ('thanks', 'happy'),
        ('apology', 'apologetic'),
        ('celebration', 'excited'),
        ('arbitrary_provider_label', 'neutral'),
    ],
)
def test_final_decision_mapper_is_deterministic(
    reason: str,
    emotion: str,
) -> None:
    """Only fixed final-decision labels map to bounded robot expressions."""
    first = map_final_decision_to_expression(
        'mapper-request',
        _decision(reason=reason),
        _safety(),
        issued_at=100.0,
    )
    retry = map_final_decision_to_expression(
        'mapper-request',
        _decision(reason=reason),
        _safety(),
        issued_at=101.0,
    )

    assert first.emotion == emotion
    assert first.cue_id == retry.cue_id
    assert first.issued_at != retry.issued_at


def test_mapper_uses_final_refusal_without_reading_diagnostic_text() -> None:
    """The cue describes Malbut and never classifies message subjects."""
    diagnostic_text = '사용자나 강아지가 우울증이라고 단정하는 문장'
    ordinary = map_final_decision_to_expression(
        'ordinary-request',
        _decision(reason='unknown', message=diagnostic_text),
        _safety(),
        issued_at=100.0,
    )
    refusal = map_final_decision_to_expression(
        'refusal-request',
        _decision(
            decision_type='refusal',
            reason='policy_refusal',
            message=diagnostic_text,
        ),
        _safety(),
        issued_at=100.0,
    )
    safety_block = map_final_decision_to_expression(
        'blocked-request',
        _decision(reason='greeting'),
        _safety(False, 'emergency_stop'),
        issued_at=100.0,
    )

    assert ordinary.emotion == 'neutral'
    assert refusal.emotion == 'concerned'
    assert safety_block.emotion == 'concerned'
    assert diagnostic_text not in str(ordinary.to_dict())


def test_mapper_rejects_malformed_safety_metadata() -> None:
    """A truthy string cannot masquerade as the final local safety result."""
    malformed = SafetyResult('false', 'allowed', 'bad fixture')
    with pytest.raises(ExpressionValidationError, match='boolean'):
        map_final_decision_to_expression(
            'malformed-safety',
            _decision(),
            malformed,
            issued_at=100.0,
        )


def test_policy_priority_is_emergency_then_privacy_then_availability() -> None:
    """Model cues cannot choose or overtake trusted override priority."""
    clock = FakeClock()
    cue = _cue(clock)
    policy = ExpressionPolicy()

    both = TrustedExpressionState(
        emergency_active=True,
        privacy_mode=True,
        renderer_available=False,
    )
    assert policy.evaluate(cue, both, clock()).code == 'emergency_override'
    privacy = TrustedExpressionState(
        privacy_mode=True,
        renderer_available=False,
    )
    assert policy.evaluate(cue, privacy, clock()).code == 'privacy_override'
    unavailable = TrustedExpressionState(renderer_available=False)
    assert (
        policy.evaluate(cue, unavailable, clock()).code
        == 'renderer_unavailable'
    )


def test_stale_cue_is_rejected_at_the_exact_deadline() -> None:
    """A dispatch TTL has no inclusive grace period."""
    clock = FakeClock()
    cue = _cue(clock, ttl_ms=500)
    policy = ExpressionPolicy()
    state = TrustedExpressionState()

    assert policy.evaluate(cue, state, 100.499).allowed is True
    at_deadline = policy.evaluate(cue, state, 100.5)
    assert at_deadline.allowed is False
    assert at_deadline.code == 'stale_cue'

    future = replace(cue, issued_at=100.501)
    future_result = policy.evaluate(future, state, 100.5)
    assert future_result.allowed is False
    assert future_result.code == 'future_cue'


def test_arbiter_renders_then_returns_to_neutral_at_ttl() -> None:
    """An active expression expires once and never becomes sticky."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    cue = _cue(clock)

    result = arbiter.submit(cue, TrustedExpressionState())
    assert result.status == 'succeeded'
    assert result.rendered_emotion == 'happy'
    assert result.expires_at == 101.5
    assert arbiter.active is not None
    assert arbiter.tick(TrustedExpressionState()) is None

    clock.advance(1.5)
    expired = arbiter.tick(TrustedExpressionState())
    assert expired is not None
    assert expired.code == 'expired_to_neutral'
    assert arbiter.active is None
    assert [call.emotion for call in renderer.calls] == [
        'happy',
        'neutral',
    ]
    assert arbiter.tick(TrustedExpressionState()) is None


def test_retry_is_idempotent_without_extending_expression_ttl() -> None:
    """A replay returns the first result and never calls the renderer again."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    cue = _cue(clock)

    first = arbiter.submit(cue, TrustedExpressionState())
    clock.advance(0.2)
    retry = arbiter.submit(
        replace(cue, issued_at=clock()),
        TrustedExpressionState(),
    )

    assert retry.cached is True
    assert retry.result_id == first.result_id
    assert retry.expires_at == first.expires_at
    assert len(renderer.calls) == 1


def test_same_request_id_with_changed_cue_is_a_conflict() -> None:
    """Idempotency keys cannot be reused to alter presentation intent."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    cue = _cue(clock)
    arbiter.submit(cue, TrustedExpressionState())

    with pytest.raises(ExpressionConflictError):
        arbiter.submit(
            replace(cue, duration_ms=2000),
            TrustedExpressionState(),
        )
    assert len(renderer.calls) == 1


def test_trusted_override_clears_active_before_reporting_conflict() -> None:
    """A changed duplicate cannot delay an emergency neutral override."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    cue = _cue(clock)
    arbiter.submit(cue, TrustedExpressionState())

    with pytest.raises(ExpressionConflictError):
        arbiter.submit(
            replace(cue, duration_ms=2000),
            TrustedExpressionState(emergency_active=True),
        )

    assert arbiter.active is None
    assert [call.emotion for call in renderer.calls] == [
        'happy',
        'neutral',
    ]


def test_concurrent_duplicate_is_rendered_once() -> None:
    """The process-local lock serializes duplicate submissions."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    cue = _cue(clock)
    results = []
    barrier = threading.Barrier(8)

    def submit() -> None:
        barrier.wait()
        results.append(
            arbiter.submit(cue, TrustedExpressionState())
        )

    threads = [threading.Thread(target=submit) for _index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(0.5)

    assert all(not thread.is_alive() for thread in threads)

    assert len(renderer.calls) == 1
    assert len({result.result_id for result in results}) == 1
    assert sum(result.cached for result in results) == 7


def test_emergency_and_privacy_clear_the_assistant_lane() -> None:
    """Trusted overrides suppress a cue and explicitly restore neutral."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    arbiter.submit(_cue(clock, 'active-request'), TrustedExpressionState())

    clock.advance(0.1)
    privacy = arbiter.submit(
        _cue(clock, 'privacy-request'),
        TrustedExpressionState(privacy_mode=True),
    )
    both = arbiter.submit(
        _cue(clock, 'emergency-request'),
        TrustedExpressionState(
            emergency_active=True,
            privacy_mode=True,
        ),
    )

    assert privacy.code == 'privacy_override'
    assert privacy.rendered_emotion == 'neutral'
    assert both.code == 'emergency_override'
    assert both.rendered_emotion is None
    assert arbiter.active is None
    assert [call.emotion for call in renderer.calls] == [
        'happy',
        'neutral',
    ]


def test_trusted_override_rechecks_an_idempotent_retry() -> None:
    """A cached success cannot keep an expression active during emergency."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    cue = _cue(clock)
    success = arbiter.submit(cue, TrustedExpressionState())

    clock.advance(0.1)
    overridden = arbiter.submit(
        replace(cue, issued_at=clock()),
        TrustedExpressionState(emergency_active=True),
    )

    assert success.code == 'rendered'
    assert overridden.code == 'emergency_override'
    assert overridden.cached is True
    assert overridden.rendered_emotion == 'neutral'
    assert arbiter.active is None
    assert [call.emotion for call in renderer.calls] == [
        'happy',
        'neutral',
    ]


def test_tick_rechecks_trusted_state_without_a_new_cue() -> None:
    """A local safety-state change clears an active assistant expression."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    arbiter.submit(_cue(clock), TrustedExpressionState())

    override = arbiter.tick(
        TrustedExpressionState(privacy_mode=True)
    )

    assert override is not None
    assert override.code == 'privacy_override'
    assert override.rendered_emotion == 'neutral'
    assert arbiter.active is None


def test_unavailable_renderer_and_stale_cue_do_not_render() -> None:
    """Availability and freshness failures are fail-closed and cached."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)

    unavailable = arbiter.submit(
        _cue(clock, 'unavailable-request'),
        TrustedExpressionState(renderer_available=False),
    )
    stale_cue = _cue(clock, 'stale-request', ttl_ms=100)
    clock.advance(0.1)
    stale = arbiter.submit(
        stale_cue,
        TrustedExpressionState(revision=1),
    )

    assert unavailable.code == 'renderer_unavailable'
    assert unavailable.renderer_error is True
    assert stale.code == 'stale_cue'
    assert renderer.calls == []


def test_rate_limits_minimum_interval_and_window() -> None:
    """Unique IDs cannot flash expressions faster than local policy."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        minimum_interval_seconds=1.0,
        rate_window_seconds=10.0,
        max_non_neutral_per_window=2,
    )

    first = arbiter.submit(
        _cue(clock, 'rate-1', duration_ms=250),
        TrustedExpressionState(),
    )
    clock.advance(0.5)
    too_soon = arbiter.submit(
        _cue(clock, 'rate-2', duration_ms=250),
        TrustedExpressionState(),
    )
    clock.advance(0.5)
    second = arbiter.submit(
        _cue(clock, 'rate-3', duration_ms=250),
        TrustedExpressionState(),
    )
    clock.advance(1.0)
    window_full = arbiter.submit(
        _cue(clock, 'rate-4', duration_ms=250),
        TrustedExpressionState(),
    )
    clock.advance(9.0)
    after_window = arbiter.submit(
        _cue(clock, 'rate-5', duration_ms=250),
        TrustedExpressionState(),
    )

    assert first.code == 'rendered'
    assert too_soon.code == 'rate_limited'
    assert second.code == 'rendered'
    assert window_full.code == 'rate_limited'
    assert after_window.code == 'rendered'


def test_neutral_cue_bypasses_rate_limit_to_reduce_expression() -> None:
    """Returning to neutral is never blocked by expressive rate limits."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    arbiter.submit(_cue(clock, 'happy-request'), TrustedExpressionState())

    neutral = arbiter.submit(
        _cue(
            clock,
            'neutral-request',
            emotion='neutral',
            duration_ms=250,
        ),
        TrustedExpressionState(),
    )

    assert neutral.status == 'succeeded'
    assert neutral.rendered_emotion == 'neutral'
    assert arbiter.active is None
    assert [call.emotion for call in renderer.calls] == [
        'happy',
        'neutral',
    ]


def test_renderer_failure_attempts_neutral_exactly_once() -> None:
    """A failed expressive render gets one bounded neutral fallback."""

    class FailHappyRenderer:
        def __init__(self) -> None:
            self.calls: List[str] = []

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms, dispatch
            self.calls.append(emotion)
            if emotion == 'happy':
                raise RuntimeError('synthetic render failure')

    clock = FakeClock()
    renderer = FailHappyRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    result = arbiter.submit(_cue(clock), TrustedExpressionState())

    assert result.status == 'fallback'
    assert result.code == 'renderer_failed_neutral_fallback'
    assert result.rendered_emotion == 'neutral'
    assert result.fallback_used is True
    assert result.renderer_error is True
    assert renderer.calls == ['happy', 'neutral']
    assert arbiter.active is None


def test_neutral_failure_disables_renderer_without_a_fallback_loop() -> None:
    """If neutral also fails, later cues stay suppressed until restart."""

    class AlwaysFailRenderer:
        def __init__(self) -> None:
            self.calls = 0

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, emotion, intensity, duration_ms, dispatch
            self.calls += 1
            raise RuntimeError('synthetic renderer outage')

    clock = FakeClock()
    renderer = AlwaysFailRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    first_cue = _cue(clock, 'failure-1')
    first = arbiter.submit(first_cue, TrustedExpressionState())
    retry = arbiter.submit(first_cue, TrustedExpressionState())
    clock.advance(0.1)
    second = arbiter.submit(
        _cue(clock, 'failure-2'),
        TrustedExpressionState(),
    )

    assert first.status == 'failed'
    assert first.code == 'renderer_unavailable'
    assert first.fallback_used is True
    assert first.renderer_error is True
    assert retry.cached is True
    assert retry.result_id == first.result_id
    assert second.status == 'suppressed'
    assert second.code == 'renderer_unavailable'
    assert renderer.calls == 2


def test_explicit_neutral_failure_is_not_retried() -> None:
    """A failed neutral render must not recursively request neutral again."""

    class FailNeutralRenderer:
        def __init__(self) -> None:
            self.calls = 0

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms, dispatch
            self.calls += 1
            if emotion == 'neutral':
                raise RuntimeError('synthetic neutral failure')

    clock = FakeClock()
    renderer = FailNeutralRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    arbiter.submit(_cue(clock), TrustedExpressionState())
    clock.advance(0.1)

    result = arbiter.submit(
        _cue(
            clock,
            'neutral-failure',
            emotion='neutral',
            duration_ms=250,
        ),
        TrustedExpressionState(),
    )

    assert result.status == 'failed'
    assert result.code == 'renderer_unavailable'
    assert renderer.calls == 2


def test_noop_boundary_is_non_actuating_and_not_a_model_tool() -> None:
    """The MVP ships no motion/audio API and no executable model Tool."""
    renderer = NoopVisualExpressionRenderer()
    assert hasattr(renderer, 'render_visual')
    assert not hasattr(renderer, 'move')
    assert not hasattr(renderer, 'play_audio')
    assert 'express_emotion' not in TOOL_SPECS

    clock = FakeClock()
    result = ExpressionArbiter(renderer, clock=clock).submit(
        _cue(clock),
        TrustedExpressionState(),
    )
    assert result.status == 'succeeded'


def test_thread_start_failure_completes_and_caches_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker startup failure cannot wedge the pending expression lane."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError('synthetic thread startup failure')

    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    cue = _cue(clock, 'thread-start-failure')
    first = arbiter.submit(cue, TrustedExpressionState())
    retry = arbiter.submit(cue, TrustedExpressionState())
    another = arbiter.submit(
        _cue(clock, 'after-thread-start-failure'),
        TrustedExpressionState(),
    )

    assert first.status == 'failed'
    assert first.code == 'renderer_unavailable'
    assert first.renderer_error is True
    assert retry.cached is True
    assert retry.result_id == first.result_id
    assert another.code == 'renderer_unavailable'
    assert renderer.calls == []


def test_newer_emergency_wins_submit_precheck_reservation_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale normal snapshot cannot erase a newer emergency revision."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    stale_normal = TrustedExpressionState(revision=10)
    precheck_done = threading.Event()
    allow_reservation = threading.Event()
    original_tick = arbiter.tick

    def gated_tick(
        state: TrustedExpressionState,
    ) -> object:
        result = original_tick(state)
        if state is stale_normal:
            precheck_done.set()
            allow_reservation.wait(0.5)
        return result

    monkeypatch.setattr(arbiter, 'tick', gated_tick)
    results: List[object] = []
    submitter = threading.Thread(
        target=lambda: results.append(
            arbiter.submit(_cue(clock), stale_normal)
        )
    )
    submitter.start()
    assert precheck_done.wait(0.2)

    assert original_tick(
        TrustedExpressionState(
            emergency_active=True,
            revision=11,
        )
    ) is None
    allow_reservation.set()
    submitter.join(0.5)

    assert not submitter.is_alive()
    assert len(results) == 1
    assert results[0].code == 'emergency_override'
    assert renderer.calls == []

    same_revision = arbiter.submit(
        _cue(clock, 'same-revision-normal'),
        TrustedExpressionState(revision=11),
    )
    recovered = arbiter.submit(
        _cue(clock, 'newer-normal'),
        TrustedExpressionState(revision=12),
    )

    assert same_revision.code == 'emergency_override'
    assert recovered.code == 'rendered'
    assert [call.emotion for call in renderer.calls] == ['happy']


def test_blocking_renderer_cannot_delay_emergency_neutral() -> None:
    """Emergency advances the lane while an expressive call is blocked."""

    class BlockingHappyRenderer:
        def __init__(self) -> None:
            self.calls: List[str] = []
            self.started = threading.Event()
            self.release = threading.Event()

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms, dispatch
            self.calls.append(emotion)
            if emotion == 'happy':
                self.started.set()
                self.release.wait(1.0)

    clock = FakeClock()
    renderer = BlockingHappyRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        renderer_timeout_seconds=0.5,
    )
    results: List[object] = []
    submitter = threading.Thread(
        target=lambda: results.append(
            arbiter.submit(_cue(clock), TrustedExpressionState())
        )
    )
    submitter.start()
    assert renderer.started.wait(0.2)

    started_at = time.monotonic()
    emergency = arbiter.tick(
        TrustedExpressionState(emergency_active=True)
    )
    elapsed = time.monotonic() - started_at

    assert emergency is not None
    assert emergency.code == 'emergency_override'
    assert emergency.rendered_emotion == 'neutral'
    assert elapsed < 0.2
    assert arbiter.active is None

    renderer.release.set()
    submitter.join(0.5)
    assert not submitter.is_alive()
    assert len(results) == 1
    assert results[0].status == 'suppressed'
    assert results[0].code == 'emergency_override'
    assert arbiter.active is None
    assert renderer.calls == ['happy', 'neutral']


def test_late_renderer_completion_cannot_restore_superseded_state() -> None:
    """A generation fence rejects success reported after a neutral cue."""

    class LateHappyRenderer:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms, dispatch
            if emotion == 'happy':
                self.started.set()
                self.release.wait(1.0)

    clock = FakeClock()
    renderer = LateHappyRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        renderer_timeout_seconds=0.5,
    )
    results: List[object] = []
    submitter = threading.Thread(
        target=lambda: results.append(
            arbiter.submit(_cue(clock), TrustedExpressionState())
        )
    )
    submitter.start()
    assert renderer.started.wait(0.2)
    override = arbiter.tick(
        TrustedExpressionState(privacy_mode=True)
    )
    assert override is not None
    assert override.rendered_emotion == 'neutral'

    renderer.release.set()
    submitter.join(0.5)
    assert not submitter.is_alive()
    assert results[0].code == 'privacy_override'
    assert results[0].rendered_emotion is None
    assert arbiter.active is None


def test_receiver_generation_fence_rejects_late_visual_effect() -> None:
    """A cooperative receiver does not apply a superseded generation."""

    class ReceiverFencedRenderer:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.happy_completed = threading.Event()
            self.applied: List[str] = []
            self.highest = 0
            self.lock = threading.Lock()

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms
            try:
                if emotion == 'happy':
                    self.started.set()
                    self.release.wait(1.0)
                with self.lock:
                    if dispatch.cancelled:
                        return
                    if dispatch.generation < self.highest:
                        return
                    self.highest = dispatch.generation
                    self.applied.append(emotion)
            finally:
                if emotion == 'happy':
                    self.happy_completed.set()

    clock = FakeClock()
    renderer = ReceiverFencedRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        renderer_timeout_seconds=0.5,
    )
    result: List[object] = []
    submitter = threading.Thread(
        target=lambda: result.append(
            arbiter.submit(_cue(clock), TrustedExpressionState())
        )
    )
    submitter.start()
    assert renderer.started.wait(0.2)

    emergency = arbiter.tick(
        TrustedExpressionState(emergency_active=True)
    )
    assert emergency is not None
    assert emergency.rendered_emotion == 'neutral'
    submitter.join(0.2)
    assert not submitter.is_alive()

    renderer.release.set()
    assert renderer.happy_completed.wait(0.2)
    assert renderer.applied == ['neutral']
    assert result[0].code == 'emergency_override'


def test_explicit_neutral_supersedes_pending_expression() -> None:
    """Neutral is dispatched when a pending expression may have started."""

    class ReceiverFencedRenderer:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.happy_completed = threading.Event()
            self.applied: List[str] = []

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms
            try:
                if emotion == 'happy':
                    self.started.set()
                    self.release.wait(1.0)
                if not dispatch.cancelled:
                    self.applied.append(emotion)
            finally:
                if emotion == 'happy':
                    self.happy_completed.set()

    clock = FakeClock()
    renderer = ReceiverFencedRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        renderer_timeout_seconds=0.5,
    )
    first_result: List[object] = []
    submitter = threading.Thread(
        target=lambda: first_result.append(
            arbiter.submit(_cue(clock), TrustedExpressionState())
        )
    )
    submitter.start()
    assert renderer.started.wait(0.2)

    neutral = arbiter.submit(
        _cue(
            clock,
            'explicit-neutral',
            emotion='neutral',
            duration_ms=250,
        ),
        TrustedExpressionState(),
    )
    submitter.join(0.2)
    renderer.release.set()
    assert renderer.happy_completed.wait(0.2)

    assert neutral.code == 'rendered'
    assert neutral.rendered_emotion == 'neutral'
    assert first_result[0].code == 'superseded_to_neutral'
    assert renderer.applied == ['neutral']
    assert arbiter.active is None


def test_renderer_timeout_is_bounded_and_disables_future_dispatch() -> None:
    """Two bounded attempts fail closed without leaking later calls."""

    class BlockingRenderer:
        def __init__(self) -> None:
            self.calls = 0
            self.release = threading.Event()

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, emotion, intensity, duration_ms, dispatch
            self.calls += 1
            self.release.wait(1.0)

    clock = FakeClock()
    renderer = BlockingRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        renderer_timeout_seconds=0.03,
    )

    started_at = time.monotonic()
    first = arbiter.submit(_cue(clock), TrustedExpressionState())
    elapsed = time.monotonic() - started_at
    second = arbiter.submit(
        _cue(clock, 'after-timeout'),
        TrustedExpressionState(),
    )

    assert elapsed < 0.2
    assert first.status == 'failed'
    assert first.code == 'renderer_unavailable'
    assert first.fallback_used is True
    assert first.renderer_error is True
    assert second.code == 'renderer_unavailable'
    assert renderer.calls == 2
    assert arbiter.active is None
    renderer.release.set()


def test_slow_concurrent_duplicates_share_one_dispatch() -> None:
    """Waiters share the reserved request while the lock stays available."""

    class SlowRenderer:
        def __init__(self) -> None:
            self.calls = 0
            self.started = threading.Event()
            self.release = threading.Event()

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, emotion, intensity, duration_ms, dispatch
            self.calls += 1
            self.started.set()
            self.release.wait(1.0)

    clock = FakeClock()
    renderer = SlowRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        renderer_timeout_seconds=0.5,
    )
    cue = _cue(clock)
    results: List[object] = []
    barrier = threading.Barrier(6)

    def submit() -> None:
        barrier.wait()
        results.append(arbiter.submit(cue, TrustedExpressionState()))

    threads = [threading.Thread(target=submit) for _index in range(6)]
    for thread in threads:
        thread.start()
    assert renderer.started.wait(0.2)
    renderer.release.set()
    for thread in threads:
        thread.join(0.5)

    assert all(not thread.is_alive() for thread in threads)
    assert renderer.calls == 1
    assert len({result.result_id for result in results}) == 1
    assert sum(result.cached for result in results) == 5


@pytest.mark.parametrize('value', [0, -0.1, 5.1, True, math.inf])
def test_renderer_timeout_configuration_is_bounded(value: object) -> None:
    """An invalid timeout cannot silently remove the dispatch bound."""
    with pytest.raises(ValueError, match='renderer_timeout_seconds'):
        ExpressionArbiter(
            NoopVisualExpressionRenderer(),
            renderer_timeout_seconds=value,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    'request_id',
    [None, '', 'x' * 129, 'request\x00id'],
)
def test_mapper_rejects_malformed_request_identifiers(
    request_id: object,
) -> None:
    """Untrusted identifiers cannot cross into renderer request metadata."""
    with pytest.raises(ExpressionValidationError, match='request_id'):
        map_final_decision_to_expression(
            request_id,  # type: ignore[arg-type]
            _decision(),
            _safety(),
            issued_at=100.0,
        )


def test_policy_rejects_malformed_boundary_objects_and_time() -> None:
    """The policy fails closed for forged objects and invalid local clocks."""
    clock = FakeClock()
    policy = ExpressionPolicy()
    cue = _cue(clock)

    with pytest.raises(ExpressionValidationError, match='cue'):
        policy.evaluate('forged', TrustedExpressionState(), clock())
    with pytest.raises(ExpressionValidationError, match='state'):
        policy.evaluate(cue, object(), clock())
    with pytest.raises(ExpressionValidationError, match='negative'):
        policy.evaluate(cue, TrustedExpressionState(), -0.1)
    with pytest.raises(ExpressionValidationError, match='issued_at'):
        replace(cue, issued_at=-0.1)


def test_recording_renderer_enforces_cancel_and_generation_fence() -> None:
    """The offline receiver rejects cancelled and out-of-order dispatches."""
    renderer = RecordingVisualExpressionRenderer()
    lane_id = 'coverage-generation-lane'
    current = ExpressionDispatchContext(
        lane_id,
        2,
        threading.Event(),
    )
    stale = ExpressionDispatchContext(
        lane_id,
        1,
        threading.Event(),
    )
    cancelled_event = threading.Event()
    cancelled_event.set()
    cancelled = ExpressionDispatchContext(
        lane_id,
        3,
        cancelled_event,
    )

    renderer.render_visual('current', 'happy', 0.5, 250, current)
    renderer.render_visual('stale', 'happy', 0.5, 250, stale)
    renderer.render_visual('cancelled', 'happy', 0.5, 250, cancelled)

    assert current.to_metadata() == {
        'lane_id': lane_id,
        'generation': 2,
    }
    assert [call.request_id for call in renderer.calls] == ['current']


@pytest.mark.parametrize(
    ('keyword', 'value', 'message'),
    [
        ('renderer', object(), 'renderer'),
        ('clock', None, 'clock'),
        ('max_non_neutral_per_window', True, 'max_non_neutral'),
        ('max_non_neutral_per_window', 0, 'max_non_neutral'),
        ('cache_size', True, 'cache_size'),
        ('cache_size', 0, 'cache_size'),
    ],
)
def test_arbiter_rejects_invalid_dependency_and_capacity_configuration(
    keyword: str,
    value: object,
    message: str,
) -> None:
    """Invalid dependencies and bounds cannot weaken local enforcement."""
    arguments = {keyword: value}
    if keyword != 'renderer':
        arguments['renderer'] = NoopVisualExpressionRenderer()

    with pytest.raises(ValueError, match=message):
        ExpressionArbiter(**arguments)  # type: ignore[arg-type]


def test_control_neutral_makes_normal_submission_renderer_busy() -> None:
    """A safety-neutral dispatch owns the lane until its bounded completion."""

    class BlockingNeutralRenderer:
        def __init__(self) -> None:
            self.neutral_started = threading.Event()
            self.release_neutral = threading.Event()

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms, dispatch
            if emotion == 'neutral':
                self.neutral_started.set()
                self.release_neutral.wait(1.0)

    clock = FakeClock()
    renderer = BlockingNeutralRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        renderer_timeout_seconds=0.5,
    )
    arbiter.submit(
        _cue(clock, 'control-active'),
        TrustedExpressionState(revision=1),
    )
    override_results: List[object] = []
    override = threading.Thread(
        target=lambda: override_results.append(
            arbiter.tick(
                TrustedExpressionState(
                    emergency_active=True,
                    revision=2,
                )
            )
        )
    )
    override.start()
    assert renderer.neutral_started.wait(0.2)

    busy = arbiter.submit(
        _cue(clock, 'while-control-neutral'),
        TrustedExpressionState(revision=3),
    )

    assert busy.code == 'renderer_busy'
    renderer.release_neutral.set()
    override.join(0.5)
    assert not override.is_alive()
    assert override_results[0].code == 'emergency_override'


def test_control_neutral_timeout_disables_future_dispatch() -> None:
    """A timed-out safety neutral fails closed for later expressions."""

    class BlockingNeutralRenderer:
        def __init__(self) -> None:
            self.calls: List[str] = []
            self.release = threading.Event()

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms, dispatch
            self.calls.append(emotion)
            if emotion == 'neutral':
                self.release.wait(1.0)

    clock = FakeClock()
    renderer = BlockingNeutralRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        minimum_interval_seconds=0.01,
        renderer_timeout_seconds=0.03,
    )
    arbiter.submit(
        _cue(clock, 'control-timeout-active'),
        TrustedExpressionState(revision=1),
    )

    override = arbiter.tick(
        TrustedExpressionState(
            emergency_active=True,
            revision=2,
        )
    )
    clock.advance(0.1)
    later = arbiter.submit(
        _cue(clock, 'after-control-timeout'),
        TrustedExpressionState(revision=3),
    )

    assert override is not None
    assert override.code == 'emergency_override'
    assert override.renderer_error is True
    assert later.code == 'renderer_unavailable'
    assert later.renderer_error is True
    assert renderer.calls == ['happy', 'neutral']
    renderer.release.set()


def test_pending_conflict_and_unrelated_request_are_rejected() -> None:
    """One pending render rejects mutation and suppresses unrelated work."""

    class BlockingHappyRenderer:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms, dispatch
            self.calls += 1
            if emotion == 'happy':
                self.started.set()
                self.release.wait(1.0)

    clock = FakeClock()
    renderer = BlockingHappyRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        renderer_timeout_seconds=0.5,
    )
    cue = _cue(clock, 'pending-original')
    first_results: List[object] = []
    first = threading.Thread(
        target=lambda: first_results.append(
            arbiter.submit(cue, TrustedExpressionState())
        )
    )
    first.start()
    assert renderer.started.wait(0.2)

    with pytest.raises(ExpressionConflictError, match='different cue'):
        arbiter.submit(
            replace(cue, duration_ms=2000),
            TrustedExpressionState(),
        )
    unrelated = arbiter.submit(
        _cue(clock, 'pending-unrelated'),
        TrustedExpressionState(),
    )

    assert unrelated.code == 'renderer_busy'
    assert renderer.calls == 1
    renderer.release.set()
    first.join(0.5)
    assert not first.is_alive()
    assert first_results[0].code == 'rendered'


def test_neutral_on_empty_lane_is_coalesced_without_rendering() -> None:
    """An explicit neutral cue is idempotent when no expression is active."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    result = ExpressionArbiter(renderer, clock=clock).submit(
        _cue(
            clock,
            'already-neutral',
            emotion='neutral',
            duration_ms=250,
        ),
        TrustedExpressionState(),
    )

    assert result.code == 'already_neutral'
    assert result.rendered_emotion == 'neutral'
    assert renderer.calls == []


def test_unavailable_renderer_clears_active_without_neutral_dispatch() -> None:
    """Local unavailability drops the lane without calling the bad renderer."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    arbiter.submit(
        _cue(clock, 'unavailable-active'),
        TrustedExpressionState(revision=1),
    )

    unavailable = arbiter.tick(
        TrustedExpressionState(
            renderer_available=False,
            revision=2,
        )
    )

    assert unavailable is not None
    assert unavailable.status == 'suppressed'
    assert unavailable.code == 'renderer_unavailable'
    assert unavailable.rendered_emotion is None
    assert unavailable.renderer_error is True
    assert [call.emotion for call in renderer.calls] == ['happy']
    assert arbiter.active is None


def test_expiry_neutral_failure_disables_renderer() -> None:
    """A failed expiry fallback leaves no active or reusable renderer lane."""

    class FailNeutralRenderer:
        def __init__(self) -> None:
            self.calls: List[str] = []

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms, dispatch
            self.calls.append(emotion)
            if emotion == 'neutral':
                raise RuntimeError('synthetic neutral failure')

    clock = FakeClock()
    renderer = FailNeutralRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    arbiter.submit(
        _cue(clock, 'expiry-neutral-failure', duration_ms=250),
        TrustedExpressionState(),
    )
    clock.advance(0.25)

    expired = arbiter.tick(TrustedExpressionState())
    next_result = arbiter.submit(
        _cue(clock, 'after-expiry-neutral-failure'),
        TrustedExpressionState(),
    )

    assert expired is not None
    assert expired.status == 'failed'
    assert expired.code == 'renderer_unavailable'
    assert expired.renderer_error is True
    assert next_result.code == 'renderer_unavailable'
    assert renderer.calls == ['happy', 'neutral']


def test_emergency_cancels_renderer_failure_neutral_fallback() -> None:
    """A trusted override supersedes a blocked error-recovery neutral."""

    class BlockingFirstNeutralRenderer:
        def __init__(self) -> None:
            self.fallback_started = threading.Event()
            self.release_fallback = threading.Event()
            self.fallback_completed = threading.Event()
            self.neutral_calls = 0
            self.applied: List[str] = []

        def render_visual(
            self,
            request_id: str,
            emotion: str,
            intensity: float,
            duration_ms: int,
            dispatch: ExpressionDispatchContext,
        ) -> None:
            del request_id, intensity, duration_ms
            if emotion == 'happy':
                raise RuntimeError('synthetic expressive failure')
            self.neutral_calls += 1
            is_fallback = self.neutral_calls == 1
            try:
                if is_fallback:
                    self.fallback_started.set()
                    self.release_fallback.wait(1.0)
                if not dispatch.cancelled:
                    self.applied.append(emotion)
            finally:
                if is_fallback:
                    self.fallback_completed.set()

    clock = FakeClock()
    renderer = BlockingFirstNeutralRenderer()
    arbiter = ExpressionArbiter(
        renderer,
        clock=clock,
        renderer_timeout_seconds=0.5,
    )
    results: List[object] = []
    submitter = threading.Thread(
        target=lambda: results.append(
            arbiter.submit(_cue(clock), TrustedExpressionState())
        )
    )
    submitter.start()
    assert renderer.fallback_started.wait(0.2)

    emergency = arbiter.tick(
        TrustedExpressionState(
            emergency_active=True,
            revision=1,
        )
    )
    submitter.join(0.5)

    assert emergency is not None
    assert emergency.code == 'emergency_override'
    assert emergency.rendered_emotion == 'neutral'
    assert not submitter.is_alive()
    assert results[0].code == 'emergency_override'
    assert renderer.neutral_calls == 2
    renderer.release_fallback.set()
    assert renderer.fallback_completed.wait(0.2)
    assert renderer.applied == ['neutral']


def test_token_cancelled_before_worker_start_skips_expression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation before daemon startup prevents the stale render call."""
    clock = FakeClock()
    renderer = RecordingVisualExpressionRenderer()
    arbiter = ExpressionArbiter(renderer, clock=clock)
    original_start = threading.Thread.start
    first_start = True
    overrides: List[object] = []

    def cancel_then_start(worker: threading.Thread) -> None:
        nonlocal first_start
        if first_start:
            first_start = False
            overrides.append(
                arbiter.tick(
                    TrustedExpressionState(
                        emergency_active=True,
                        revision=1,
                    )
                )
            )
        original_start(worker)

    monkeypatch.setattr(threading.Thread, 'start', cancel_then_start)
    result = arbiter.submit(
        _cue(clock, 'cancel-before-worker'),
        TrustedExpressionState(),
    )

    assert result.code == 'emergency_override'
    assert overrides[0].code == 'emergency_override'
    assert [call.emotion for call in renderer.calls] == ['neutral']
    assert arbiter.active is None
