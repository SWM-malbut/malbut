"""Offline tests for the device-free continuous voice session seam."""

import hashlib
import json
import inspect
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from typing import Optional

import pytest

import malbut_agent_server.continuous_voice as continuous_voice_module
from malbut_agent_server.continuous_voice import (
    AWAITING_CONFIRMATION,
    AWAITING_WAKE,
    CLOSED,
    LISTENING,
    MISSION_WAIT,
    PROCESSING,
    SPEAKING,
    ContinuousVoiceSession,
    SpeechOutputResult,
    ToolConfirmationRequest,
    WakeWordEvent,
)
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gateway import (
    production_registry,
    simulation_registry,
)
from malbut_agent_server.local_stt import LocalSTTResult, WavMetadata
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import RobotState, ValidationError
from malbut_agent_server.speech import (
    SPEECH_SCHEMA_VERSION,
    SpeechActivityEvent,
    SpeechConversationCoordinator,
    TrustedSpeechBinding,
)


class RecordingMockProvider(MockProvider):
    """Record offline provider calls and their runtime state."""

    def __init__(self) -> None:
        """Initialize content kept only inside the test process."""
        super().__init__()
        self.calls = 0
        self.requests = []
        self.histories = []
        self.state_reader = None
        self.observed_states = []

    def complete(
        self,
        request,
        memories,
        conversation_turns,
        tools,
        conversation_summary=None,
    ):
        """Record one call and delegate to deterministic local rules."""
        self.calls += 1
        self.requests.append(request)
        self.histories.append(list(conversation_turns))
        if self.state_reader is not None:
            self.observed_states.append(self.state_reader())
        return super().complete(
            request,
            memories,
            conversation_turns,
            tools,
            conversation_summary,
        )


class ScriptedWakeSource:
    """Return a finite sequence of content-free wake detections."""

    def __init__(self, events=()) -> None:
        """Store scripted detections without audio input."""
        self.events = deque(events)
        self.calls = 0
        self.stop_values = []
        self.state_reader = None
        self.observed_states = []

    def wait_for_wake(
        self,
        stop_event: threading.Event,
    ) -> Optional[WakeWordEvent]:
        """Return the next event or an idle sentinel."""
        self.calls += 1
        self.stop_values.append(stop_event.is_set())
        if self.state_reader is not None:
            self.observed_states.append(self.state_reader())
        if not self.events:
            return None
        return self.events.popleft()


class ScriptedTranscriptSource:
    """Return one validated final STT result for each accepted wake."""

    def __init__(self, results=()) -> None:
        """Store validated in-memory results without WAV paths."""
        self.results = deque(results)
        self.calls = []
        self.state_reader = None
        self.observed_states = []

    def capture_final(
        self,
        wake_event: WakeWordEvent,
        stop_event: threading.Event,
    ) -> LocalSTTResult:
        """Return exactly one result and record no transcript content."""
        self.calls.append((wake_event.event_id, stop_event.is_set()))
        if self.state_reader is not None:
            self.observed_states.append(self.state_reader())
        if not self.results:
            raise RuntimeError('test transcript source exhausted')
        value = self.results.popleft()
        if isinstance(value, BaseException):
            raise value
        return value


class RecordingSpeechOutput:
    """Acknowledge safe text without audio or external I/O."""

    def __init__(self, status: str = 'completed') -> None:
        """Select the terminal result returned by every play call."""
        self.status = status
        self.requests = []
        self.cancel_requests = []
        self.state_reader = None
        self.observed_states = []

    def play(self, request, stop_event) -> SpeechOutputResult:
        """Record a typed request and terminally acknowledge it."""
        self.requests.append(request)
        if self.state_reader is not None:
            self.observed_states.append(self.state_reader())
        return SpeechOutputResult(
            request_id=request.request_id,
            status=self.status,
        )

    def cancel(self, request) -> None:
        """Record one idempotent cancellation request."""
        self.cancel_requests.append(request)


class BlockingSpeechOutput(RecordingSpeechOutput):
    """Hold one output active until the test sends cancellation."""

    def __init__(self) -> None:
        """Create deterministic synchronization barriers."""
        super().__init__(status='cancelled')
        self.started = threading.Event()
        self.release = threading.Event()

    def play(self, request, stop_event) -> SpeechOutputResult:
        """Block until cancel or close releases the output."""
        self.requests.append(request)
        if self.state_reader is not None:
            self.observed_states.append(self.state_reader())
        self.started.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError('test output release timed out')
        return SpeechOutputResult(
            request_id=request.request_id,
            status='cancelled',
        )

    def cancel(self, request) -> None:
        """Record cancellation and release the blocked play call."""
        self.cancel_requests.append(request)
        self.release.set()


class ReentrantCancelOutput(BlockingSpeechOutput):
    """Synchronously re-enter the session from the cancel callback."""

    def __init__(self) -> None:
        """Leave session and activity wiring to the owning test."""
        super().__init__()
        self.session = None
        self.activity = None
        self.reentrant_result = None

    def cancel(self, request) -> None:
        """Release playback and replay the same trusted activity inline."""
        self.cancel_requests.append(request)
        self.release.set()
        self.reentrant_result = self.session.handle_barge_in(self.activity)


class CancelFailingSpeechOutput(BlockingSpeechOutput):
    """Keep playback active while cancellation reports an adapter failure."""

    def cancel(self, request) -> None:
        """Record one attempt without releasing the active playback."""
        self.cancel_requests.append(request)
        raise RuntimeError('private adapter failure detail')


class CloseReentrantCancelOutput(BlockingSpeechOutput):
    """Synchronously re-enter close from its own cancel callback."""

    def __init__(self) -> None:
        """Leave session wiring to the owning test."""
        super().__init__()
        self.session = None
        self.reentrant_result = None

    def cancel(self, request) -> None:
        """Release output, then call close again on the same thread."""
        self.cancel_requests.append(request)
        self.release.set()
        self.reentrant_result = self.session.close()


def _wake(
    event_id: str,
    *,
    sequence: int = 1,
    confidence: float = 0.99,
) -> WakeWordEvent:
    """Build one bounded wake event with no audio or phrase."""
    return WakeWordEvent(
        event_id=event_id,
        source='fake-wake',
        source_sequence=sequence,
        source_timestamp_ns=1000 + sequence,
        confidence=confidence,
    )


def _transcript(
    text: str = '안녕',
    *,
    confidence: float = 0.98,
) -> LocalSTTResult:
    """Build one validated final result without a real audio device."""
    return LocalSTTResult(
        text=text,
        confidence=confidence,
        language='ko',
        audio_metadata=WavMetadata(
            duration_ms=1000,
            sample_rate_hz=16000,
            channel_count=1,
            sample_width_bytes=2,
            frame_count=16000,
            file_size_bytes=32044,
        ),
        backend='fake-stt',
        model='fake-stt-v1',
    )


@contextmanager
def _runtime(
    wake_events=(),
    transcripts=(),
    *,
    output=None,
    trusted_state: bool = False,
    robot_state: Optional[RobotState] = None,
    available_tools=(),
):
    """Yield one fully offline continuous session and its fakes."""
    provider = RecordingMockProvider()
    registry = (
        simulation_registry() if available_tools else production_registry()
    )
    memory = SQLiteMemoryStore(':memory:')
    conversations = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=provider,
        memory_store=memory,
        conversation_store=conversations,
        safety_policy=SafetyPolicy(),
        trusted_robot_state=trusted_state,
        capability_registry=registry,
    )
    coordinator = SpeechConversationCoordinator(
        orchestrator,
        minimum_confidence=0.60,
    )
    binding = TrustedSpeechBinding(
        user_id='voice-user',
        speaker_id='voice-speaker',
        speech_session_id='voice-session',
        conversation_id='voice-conversation',
        source='local-stt',
    )
    wake_source = ScriptedWakeSource(wake_events)
    transcript_source = ScriptedTranscriptSource(transcripts)
    speech_output = output or RecordingSpeechOutput()
    session = ContinuousVoiceSession(
        coordinator,
        binding,
        wake_source,
        transcript_source,
        speech_output,
        robot_state=robot_state,
        available_tools=available_tools,
        clock_ns=lambda: 1234567890,
    )
    provider.state_reader = lambda: session.state
    wake_source.state_reader = lambda: session.state
    transcript_source.state_reader = lambda: session.state
    speech_output.state_reader = lambda: session.state
    try:
        yield {
            'session': session,
            'provider': provider,
            'registry': registry,
            'wake': wake_source,
            'transcript': transcript_source,
            'output': speech_output,
            'binding': binding,
            'conversations': conversations,
        }
    finally:
        session.close()
        conversations.close()
        memory.close()


def _activity(runtime, event_id: str = 'barge-1') -> SpeechActivityEvent:
    """Build a trusted activity event for the runtime's current epoch."""
    binding = runtime['binding']
    session = runtime['session']
    return SpeechActivityEvent(
        schema_version=SPEECH_SCHEMA_VERSION,
        event_id=event_id,
        speech_session_id=binding.speech_session_id,
        speaker_id=binding.speaker_id,
        source=binding.source,
        capture_epoch=session.capture_epoch,
        source_timestamp_ns=987654321,
    )


def test_wake_event_contract_contains_no_audio_or_phrase() -> None:
    """Wake adapters cross the boundary with content-free metadata only."""
    event = _wake('wake-1')

    assert event.to_dict() == {
        'event_id': 'wake-1',
        'source': 'fake-wake',
        'source_sequence': 1,
        'source_timestamp_ns': 1001,
        'confidence': 0.99,
    }
    serialized = json.dumps(event.to_audit_dict(), sort_keys=True)
    for forbidden in ('audio', 'pcm', 'path', 'phrase', 'keyword'):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ('utterance', 'expected_type'),
    (
        ('안녕', 'message'),
        ('API 키를 알려줘', 'refusal'),
        ('이 요청은 잘 모르겠어', 'clarification'),
    ),
)
def test_non_action_decisions_are_output_and_return_to_wake(
    utterance: str,
    expected_type: str,
) -> None:
    """Message, refusal, and clarification all finish their TTS epoch."""
    with _runtime([_wake('wake-1')], [_transcript(utterance)]) as runtime:
        result = runtime['session'].run_once()

        assert result.status == 'responded'
        assert result.code == expected_type
        assert result.state == AWAITING_WAKE
        assert (
            result.pipeline_result.agent_result.decision.type
            == expected_type
        )
        assert len(runtime['output'].requests) == 1
        assert result.output_result.status == 'completed'
        assert result.tts_terminal_result.code == 'tts_terminal'
        assert runtime['session'].capture_epoch == 2


def test_two_wakes_share_one_conversation_and_exact_states() -> None:
    """A completed answer resumes wake listening with durable history."""
    with _runtime(
        [_wake('wake-1'), _wake('wake-2', sequence=2)],
        [_transcript('안녕'), _transcript('아까 뭐라고 했어?')],
    ) as runtime:
        first = runtime['session'].run_once()
        second = runtime['session'].run_once()

        assert first.status == second.status == 'responded'
        assert runtime['session'].state == AWAITING_WAKE
        assert runtime['provider'].calls == 2
        assert runtime['provider'].histories[0] == []
        assert len(runtime['provider'].histories[1]) == 1
        assert '안녕' in runtime['output'].requests[1].text
        assert runtime['wake'].observed_states == [
            AWAITING_WAKE,
            AWAITING_WAKE,
        ]
        assert runtime['transcript'].observed_states == [
            LISTENING,
            LISTENING,
        ]
        assert runtime['provider'].observed_states == [
            PROCESSING,
            PROCESSING,
        ]
        assert runtime['output'].observed_states == [SPEAKING, SPEAKING]


def test_no_wake_performs_no_capture_inference_or_output() -> None:
    """Idle polling cannot start STT, a provider, or output."""
    with _runtime() as runtime:
        result = runtime['session'].run_once()

        assert result.status == 'idle'
        assert result.code == 'awaiting_wake'
        assert result.state == AWAITING_WAKE
        assert runtime['transcript'].calls == []
        assert runtime['provider'].calls == 0
        assert runtime['output'].requests == []


def test_duplicate_wake_is_idempotent_and_mutation_conflicts() -> None:
    """A wake ID captures and infers once while changed replay is rejected."""
    event = _wake('wake-1')
    changed = _wake('wake-1', confidence=0.8)
    with _runtime(
        [event, event, changed],
        [_transcript('안녕')],
    ) as runtime:
        first = runtime['session'].run_once()
        duplicate = runtime['session'].run_once()
        conflict = runtime['session'].run_once()

        assert first.cached is False
        assert duplicate.cached is True
        assert duplicate.pipeline_result is first.pipeline_result
        assert conflict.status == 'rejected'
        assert conflict.code == 'wake_event_conflict'
        assert len(runtime['transcript'].calls) == 1
        assert runtime['provider'].calls == 1
        assert len(runtime['output'].requests) == 1


def test_source_sequence_fences_replay_after_cache_eviction() -> None:
    """A bounded result cache cannot make an old detector event new again."""
    first = _wake('wake-1', sequence=1)
    second = _wake('wake-2', sequence=2)
    with _runtime(
        [first, second, first],
        [_transcript('첫 번째'), _transcript('두 번째')],
    ) as runtime:
        runtime['session'].wake_cache_size = 1
        runtime['session'].coordinator.event_cache_size = 1

        one = runtime['session'].run_once()
        two = runtime['session'].run_once()
        replay = runtime['session'].run_once()

        assert one.status == two.status == 'responded'
        assert replay.status == 'rejected'
        assert replay.code == 'stale_wake_sequence'
        assert len(runtime['transcript'].calls) == 2
        assert runtime['provider'].calls == 2
        assert len(runtime['output'].requests) == 2


def test_low_confidence_returns_to_wake_before_provider() -> None:
    """The existing coordinator gate rejects low-confidence final text."""
    with _runtime(
        [_wake('wake-1')],
        [_transcript('불확실한 발화', confidence=0.59)],
    ) as runtime:
        result = runtime['session'].run_once()

        assert result.status == 'rejected'
        assert result.code == 'low_confidence'
        assert result.state == AWAITING_WAKE
        assert runtime['provider'].calls == 0
        assert runtime['output'].requests == []


def test_tool_call_becomes_non_executing_confirmation_handoff() -> None:
    """A safe proposal is handed off, never sent to a Tool adapter."""
    state = RobotState.from_dict(
        {
            'battery_percent': 80,
            'navigation_available': True,
            'localization_ok': True,
        }
    )
    with _runtime(
        [_wake('wake-1')],
        [_transcript('거실로 이동해줘')],
        trusted_state=True,
        robot_state=state,
        available_tools=('navigate',),
    ) as runtime:
        adapter = runtime['registry'].get('navigate').adapter
        result = runtime['session'].run_once()

        assert result.status == 'confirmation_required'
        assert result.code == 'confirmation_required'
        assert result.state == AWAITING_CONFIRMATION
        assert result.pipeline_result.agent_result.decision.type == 'tool_call'
        assert result.pipeline_result.agent_result.safety.allowed is True
        assert result.confirmation_request is not None
        confirmation = result.confirmation_request
        assert confirmation.tool_name == 'navigate'
        assert confirmation.arguments == {'location': '거실'}
        assert confirmation.decision_id == (
            result.pipeline_result.agent_result.decision_id
        )
        assert confirmation.to_audit_dict()['authorized'] is False
        assert confirmation.to_audit_dict()['tool_call_id'] is None
        assert runtime['output'].requests == []
        assert adapter.calls == 0
        assert result.tts_terminal_result.code == 'tts_terminal'
        assert runtime['session'].pending_confirmation_request is confirmation


def test_confirmation_arguments_are_canonical_and_deeply_immutable() -> None:
    """A later caller cannot mutate values bound to the decision digest."""
    request = ToolConfirmationRequest(
        request_id='confirmation-1',
        decision_id='decision-1',
        user_id='voice-user',
        conversation_id='voice-conversation',
        turn_id='turn-1',
        tool_name='future_tool',
        arguments={
            'second': {'items': ['one', 'two']},
            'first': 'value',
        },
        issued_at=1.0,
        expires_at=2.0,
    )
    canonical = (
        '{"first":"value","second":{"items":["one","two"]}}'
        .encode('utf-8')
    )

    assert request.arguments_digest == hashlib.sha256(canonical).hexdigest()
    with pytest.raises(TypeError):
        request.arguments['first'] = 'mutated'
    with pytest.raises(TypeError):
        request.arguments['second']['items'] = ()
    with pytest.raises(AttributeError):
        request.arguments['second']['items'].append('mutated')

    detached = request.arguments_dict()
    detached['first'] = 'mutated'
    detached['second']['items'].append('mutated')
    assert request.arguments['first'] == 'value'
    assert request.arguments['second']['items'] == ('one', 'two')
    assert request.to_audit_dict()['arguments_digest'] == (
        request.arguments_digest
    )


def test_pending_confirmation_and_mission_wait_do_not_consume_voice() -> None:
    """Only an external exact handle can unblock the next wake cycle."""
    state = RobotState.from_dict(
        {
            'battery_percent': 80,
            'navigation_available': True,
            'localization_ok': True,
        }
    )
    with _runtime(
        [_wake('wake-1'), _wake('wake-2', sequence=2)],
        [_transcript('거실로 이동해줘'), _transcript('응')],
        trusted_state=True,
        robot_state=state,
        available_tools=('navigate',),
    ) as runtime:
        proposal = runtime['session'].run_once()
        request = proposal.confirmation_request
        adapter = runtime['registry'].get('navigate').adapter

        assert proposal.state == AWAITING_CONFIRMATION
        assert runtime['session'].pending_confirmation_request is request
        pending = runtime['session'].run_once()
        assert pending.status == 'busy'
        assert pending.code == 'confirmation_pending'
        assert pending.state == AWAITING_CONFIRMATION
        assert pending.confirmation_request is request
        assert runtime['wake'].calls == 1
        assert len(runtime['transcript'].calls) == 1
        assert runtime['provider'].calls == 1
        assert len(runtime['wake'].events) == 1
        assert len(runtime['transcript'].results) == 1
        assert adapter.calls == 0

        reconstructed = replace(request)
        reconstructed_result = runtime['session'].accept_confirmation(
            reconstructed
        )
        assert reconstructed_result.status == 'rejected'
        assert reconstructed_result.code == 'confirmation_mismatch'

        forged = replace(request, arguments={'location': '침실'})
        mismatch = runtime['session'].accept_confirmation(forged)
        assert mismatch.status == 'rejected'
        assert mismatch.code == 'confirmation_mismatch'
        assert runtime['session'].state == AWAITING_CONFIRMATION

        accepted = runtime['session'].accept_confirmation(request)
        duplicate = runtime['session'].accept_confirmation(request)
        assert accepted.status == 'ready'
        assert accepted.code == 'mission_wait'
        assert duplicate.status == 'rejected'
        assert duplicate.code == 'confirmation_already_accepted'
        assert runtime['session'].state == MISSION_WAIT
        assert runtime['session'].pending_confirmation_request is None
        assert runtime['session'].active_mission_request is request

        mission_busy = runtime['session'].run_once()
        assert mission_busy.status == 'busy'
        assert mission_busy.code == 'mission_in_progress'
        assert mission_busy.state == MISSION_WAIT
        assert runtime['wake'].calls == 1
        assert len(runtime['transcript'].calls) == 1
        assert runtime['provider'].calls == 1
        assert adapter.calls == 0

        reconstructed_terminal = runtime['session'].complete_mission(
            reconstructed,
            outcome='succeeded',
        )
        assert reconstructed_terminal.status == 'rejected'
        assert reconstructed_terminal.code == 'mission_request_mismatch'

        forged_terminal = runtime['session'].complete_mission(
            forged,
            outcome='succeeded',
        )
        assert forged_terminal.status == 'rejected'
        assert forged_terminal.code == 'mission_request_mismatch'
        assert runtime['session'].state == MISSION_WAIT

        wrong_terminal = runtime['session'].complete_mission(
            request,
            outcome='denied',
        )
        assert wrong_terminal.status == 'rejected'
        assert wrong_terminal.code == 'mission_outcome_invalid_for_state'
        assert runtime['session'].state == MISSION_WAIT

        terminal = runtime['session'].complete_mission(
            request,
            outcome='succeeded',
        )
        replay = runtime['session'].complete_mission(
            request,
            outcome='succeeded',
        )
        assert terminal.status == 'ready'
        assert terminal.code == 'mission_succeeded'
        assert replay.status == 'rejected'
        assert replay.code == 'mission_terminal_replay'
        assert runtime['session'].state == AWAITING_WAKE
        assert runtime['session'].active_mission_request is None

        next_cycle = runtime['session'].run_once()
        assert next_cycle.status == 'responded'
        assert runtime['wake'].calls == 2
        assert len(runtime['transcript'].calls) == 2
        assert runtime['provider'].calls == 2
        assert adapter.calls == 0


def test_pending_denial_rejects_forgery_and_replay() -> None:
    """A denial consumes only the exact displayed request and rearms once."""
    state = RobotState.from_dict(
        {
            'battery_percent': 80,
            'navigation_available': True,
            'localization_ok': True,
        }
    )
    wake = _wake('wake-1')
    with _runtime(
        [wake, wake],
        [_transcript('거실로 이동해줘')],
        trusted_state=True,
        robot_state=state,
        available_tools=('navigate',),
    ) as runtime:
        proposal = runtime['session'].run_once()
        request = proposal.confirmation_request
        forged = replace(request, conversation_id='different-conversation')

        mismatch = runtime['session'].complete_mission(
            forged,
            outcome='denied',
        )
        denied = runtime['session'].complete_mission(
            request,
            outcome='denied',
        )
        accept_replay = runtime['session'].accept_confirmation(request)
        deny_replay = runtime['session'].complete_mission(
            request,
            outcome='denied',
        )
        wake_replay = runtime['session'].run_once()

        assert mismatch.status == 'rejected'
        assert mismatch.code == 'confirmation_mismatch'
        assert denied.status == 'ready'
        assert denied.code == 'mission_denied'
        assert accept_replay.code == 'mission_terminal_replay'
        assert deny_replay.code == 'mission_terminal_replay'
        assert wake_replay.status == 'rejected'
        assert wake_replay.code == 'stale_wake_sequence'
        assert runtime['session'].state == AWAITING_WAKE
        assert runtime['provider'].calls == 1
        assert len(runtime['transcript'].calls) == 1
        assert runtime['registry'].get('navigate').adapter.calls == 0


def test_close_terminally_clears_pending_confirmation() -> None:
    """A closed session cannot later accept its displayed proposal."""
    state = RobotState.from_dict(
        {
            'battery_percent': 80,
            'navigation_available': True,
            'localization_ok': True,
        }
    )
    with _runtime(
        [_wake('wake-1')],
        [_transcript('거실로 이동해줘')],
        trusted_state=True,
        robot_state=state,
        available_tools=('navigate',),
    ) as runtime:
        proposal = runtime['session'].run_once()
        request = proposal.confirmation_request

        runtime['session'].close()
        late = runtime['session'].accept_confirmation(request)

        assert runtime['session'].state == CLOSED
        assert runtime['session'].pending_confirmation_request is None
        assert late.status == 'rejected'
        assert late.code == 'session_closed'
        assert runtime['registry'].get('navigate').adapter.calls == 0


def test_barge_in_cancels_once_and_reopens_wake_epoch() -> None:
    """A duplicate VAD event cannot duplicate output cancellation."""
    output = BlockingSpeechOutput()
    with _runtime(
        [_wake('wake-1')],
        [_transcript('안녕')],
        output=output,
    ) as runtime:
        outcome = {}

        def run_cycle() -> None:
            outcome['result'] = runtime['session'].run_once()

        thread = threading.Thread(target=run_cycle)
        thread.start()
        assert output.started.wait(timeout=1)
        assert runtime['session'].state == SPEAKING

        activity = _activity(runtime)
        first = runtime['session'].handle_barge_in(activity)
        duplicate = runtime['session'].handle_barge_in(activity)
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert first.code == 'tts_cancel_requested'
        assert duplicate == first
        assert len(output.cancel_requests) == 1
        assert outcome['result'].status == 'cancelled'
        assert outcome['result'].tts_terminal_result.code == (
            'tts_already_terminal'
        )
        assert runtime['session'].capture_epoch == 2
        assert runtime['session'].state == AWAITING_WAKE


def test_barge_in_after_pipeline_fences_output_before_play() -> None:
    """A cancel registered before adapter entry prevents late playback."""
    output = RecordingSpeechOutput(status='completed')
    with _runtime(
        [_wake('wake-1')],
        [_transcript('안녕')],
        output=output,
    ) as runtime:
        session = runtime['session']
        original = session._responded_result
        pipeline_ready = threading.Event()
        release_response = threading.Event()
        outcome = {}

        def paused_response(wake_event, pipeline):
            pipeline_ready.set()
            assert release_response.wait(timeout=2)
            return original(wake_event, pipeline)

        session._responded_result = paused_response
        thread = threading.Thread(
            target=lambda: outcome.setdefault('result', session.run_once())
        )
        thread.start()
        assert pipeline_ready.wait(timeout=1)
        assert output.requests == []

        control = session.handle_barge_in(_activity(runtime))
        release_response.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert control.code == 'tts_cancel_requested'
        assert len(output.cancel_requests) == 1
        assert output.requests == []
        assert outcome['result'].status == 'cancelled'
        assert outcome['result'].code == 'speech_output_cancelled'
        assert outcome['result'].state == AWAITING_WAKE


def test_reentrant_cancel_callback_does_not_deadlock_or_duplicate() -> None:
    """No session lock is held while an adapter handles cancellation."""
    output = ReentrantCancelOutput()
    with _runtime(
        [_wake('wake-1')],
        [_transcript('안녕')],
        output=output,
    ) as runtime:
        output.session = runtime['session']
        output.activity = _activity(runtime)
        outcome = {}
        cycle = threading.Thread(
            target=lambda: outcome.setdefault(
                'cycle',
                runtime['session'].run_once(),
            )
        )
        cycle.start()
        assert output.started.wait(timeout=1)

        control_box = {}
        cancel = threading.Thread(
            target=lambda: control_box.setdefault(
                'result',
                runtime['session'].handle_barge_in(output.activity),
            )
        )
        cancel.start()
        cancel.join(timeout=2)
        cycle.join(timeout=2)

        assert not cancel.is_alive()
        assert not cycle.is_alive()
        assert control_box['result'].code == 'tts_cancel_requested'
        assert output.reentrant_result == control_box['result']
        assert len(output.cancel_requests) == 1
        assert outcome['cycle'].status == 'cancelled'


def test_cancel_adapter_failure_is_typed_and_fails_closed() -> None:
    """A failed adapter cancel is not reported as successful cancellation."""
    output = CancelFailingSpeechOutput()
    with _runtime(
        [_wake('wake-1')],
        [_transcript('안녕')],
        output=output,
    ) as runtime:
        outcome = {}
        cycle = threading.Thread(
            target=lambda: outcome.setdefault(
                'cycle',
                runtime['session'].run_once(),
            )
        )
        cycle.start()
        assert output.started.wait(timeout=1)

        control = runtime['session'].handle_barge_in(_activity(runtime))
        assert control.status == 'failed'
        assert control.code == 'tts_cancel_delivery_failed'
        assert runtime['session'].state == CLOSED
        assert len(output.cancel_requests) == 1
        assert cycle.is_alive()

        output.release.set()
        cycle.join(timeout=2)
        assert not cycle.is_alive()
        assert outcome['cycle'].status == 'failed'
        assert outcome['cycle'].code == 'tts_cancel_delivery_failed'
        assert outcome['cycle'].state == CLOSED


def test_close_is_idempotent_and_stops_future_wake_reads() -> None:
    """Repeated close returns one terminal result and never polls again."""
    with _runtime([_wake('unused')], [_transcript()]) as runtime:
        first = runtime['session'].close()
        duplicate = runtime['session'].close()
        result = runtime['session'].run_once()

        assert duplicate is first
        assert first.status == 'closed'
        assert runtime['session'].state == CLOSED
        assert result.status == 'closed'
        assert runtime['wake'].calls == 0
        assert runtime['transcript'].calls == []
        assert runtime['provider'].calls == 0


def test_closed_result_wins_while_uncooperative_wake_adapter_drains() -> None:
    """A stuck sync adapter is isolated from later closed-state callers."""
    class UncooperativeWakeSource:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def wait_for_wake(self, stop_event):
            self.calls += 1
            self.started.set()
            self.release.wait(timeout=2)
            return None

    with _runtime() as runtime:
        source = UncooperativeWakeSource()
        runtime['session'].wake_source = source
        worker = threading.Thread(
            target=runtime['session'].run_once,
            daemon=True,
        )
        worker.start()
        assert source.started.wait(timeout=1)

        runtime['session'].close()
        later = runtime['session'].run_once()

        assert later.status == 'closed'
        assert later.code == 'session_closed'
        assert source.calls == 1
        assert worker.is_alive()
        source.release.set()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_close_during_output_delivers_one_cancel() -> None:
    """Concurrent repeated close cannot duplicate an active TTS cancel."""
    output = BlockingSpeechOutput()
    with _runtime(
        [_wake('wake-1')],
        [_transcript('안녕')],
        output=output,
    ) as runtime:
        outcome = {}
        thread = threading.Thread(
            target=lambda: outcome.setdefault(
                'result',
                runtime['session'].run_once(),
            )
        )
        thread.start()
        assert output.started.wait(timeout=1)

        first = runtime['session'].close()
        duplicate = runtime['session'].close()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert duplicate is first
        assert first.cancel_request is not None
        assert len(output.cancel_requests) == 1
        assert runtime['session'].state == CLOSED
        assert outcome['result'].state == CLOSED


def test_cancel_callback_can_reenter_close_without_deadlock() -> None:
    """The close serialization lock is released before adapter callbacks."""
    output = CloseReentrantCancelOutput()
    with _runtime(
        [_wake('wake-1')],
        [_transcript('안녕')],
        output=output,
    ) as runtime:
        output.session = runtime['session']
        cycle = threading.Thread(target=runtime['session'].run_once)
        cycle.start()
        assert output.started.wait(timeout=1)

        outcome = {}
        closer = threading.Thread(
            target=lambda: outcome.setdefault(
                'result',
                runtime['session'].close(),
            )
        )
        closer.start()
        closer.join(timeout=2)
        cycle.join(timeout=2)

        assert not closer.is_alive()
        assert not cycle.is_alive()
        assert output.reentrant_result is outcome['result']
        assert len(output.cancel_requests) == 1
        assert outcome['result'].status == 'closed'


def test_close_after_pipeline_fences_output_before_play() -> None:
    """Closing in the coordinator-to-adapter gap prevents output start."""
    output = RecordingSpeechOutput(status='completed')
    with _runtime(
        [_wake('wake-1')],
        [_transcript('안녕')],
        output=output,
    ) as runtime:
        session = runtime['session']
        original = session._responded_result
        pipeline_ready = threading.Event()
        release_response = threading.Event()
        outcome = {}

        def paused_response(wake_event, pipeline):
            pipeline_ready.set()
            assert release_response.wait(timeout=2)
            return original(wake_event, pipeline)

        session._responded_result = paused_response
        thread = threading.Thread(
            target=lambda: outcome.setdefault('result', session.run_once())
        )
        thread.start()
        assert pipeline_ready.wait(timeout=1)

        closed = session.close()
        release_response.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert closed.status == 'closed'
        assert len(output.cancel_requests) == 1
        assert output.requests == []
        assert outcome['result'].status == 'cancelled'
        assert outcome['result'].state == CLOSED


def test_raw_text_paths_and_tool_values_are_absent_from_diagnostics() -> None:
    """Representations and audit projections never log content values."""
    secret = 'raw-secret-transcript /private/audio/capture.wav'
    with _runtime(
        [_wake('wake-1')],
        [_transcript(secret)],
    ) as runtime:
        result = runtime['session'].run_once()

        diagnostics = repr(result) + json.dumps(
            result.to_audit_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )
        assert secret not in diagnostics
        assert 'raw-secret-transcript' not in diagnostics
        assert '/private/audio/capture.wav' not in diagnostics

    confirmation = ToolConfirmationRequest(
        request_id='confirmation-1',
        decision_id='decision-1',
        user_id='voice-user',
        conversation_id='voice-conversation',
        turn_id='turn-1',
        tool_name='send_notification',
        arguments={'message': secret, 'image_id': None},
        issued_at=1.0,
        expires_at=2.0,
    )
    confirmation_diagnostics = repr(confirmation) + json.dumps(
        confirmation.to_audit_dict(),
        ensure_ascii=False,
        sort_keys=True,
    )
    assert secret not in confirmation_diagnostics
    assert 'raw-secret-transcript' not in confirmation_diagnostics
    assert '/private/audio/capture.wav' not in confirmation_diagnostics


def test_transcript_source_failure_is_content_free_and_cached() -> None:
    """One failing wake does not retry a hostile source or leak its error."""
    event = _wake('wake-1')
    private_error = RuntimeError(
        'raw-secret-transcript /private/audio/capture.wav'
    )
    with _runtime([event, event], [private_error]) as runtime:
        first = runtime['session'].run_once()
        duplicate = runtime['session'].run_once()

        diagnostics = repr(first) + repr(duplicate)
        assert first.status == 'failed'
        assert first.code == 'transcript_source_failed'
        assert duplicate.cached is True
        assert len(runtime['transcript'].calls) == 1
        assert runtime['provider'].calls == 0
        assert 'raw-secret-transcript' not in diagnostics
        assert '/private/audio/capture.wav' not in diagnostics


def test_invalid_wake_and_output_contracts_fail_closed() -> None:
    """Malformed injected boundaries cannot silently advance the session."""
    terminal = SpeechOutputResult(
        request_id='tts-request-1',
        status='completed',
    )
    assert terminal.request_id == 'tts-request-1'
    assert terminal.status == 'completed'

    with pytest.raises(ValidationError):
        WakeWordEvent(
            event_id='wake',
            source='fake',
            source_sequence=1,
            source_timestamp_ns=1,
            confidence=float('nan'),
        )
    with pytest.raises(ValidationError):
        SpeechOutputResult(request_id='tts', status='playing')


def test_module_has_no_device_or_network_imports() -> None:
    """The continuous seam remains a pure orchestration module."""
    source = inspect.getsource(continuous_voice_module)
    for forbidden in (
        'import subprocess',
        'import socket',
        'import urllib',
        'import requests',
        'faster_whisper',
        'arecord',
    ):
        assert forbidden not in source
