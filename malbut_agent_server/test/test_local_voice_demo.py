"""Offline integration tests for the one-shot local voice demo."""

import json
import os
import stat
import sys
import wave
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

import malbut_agent_server.local_voice_demo as voice_demo_module
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.local_stt import (
    BackendTranscript,
    LocalSTTResult,
    WavMetadata,
)
from malbut_agent_server.local_voice_demo import (
    VOICE_DEMO_MINIMUM_CONFIDENCE,
    VoiceDemoError,
    main,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    AgentDecision,
    ProviderUsage,
    RobotState,
)


SAFE_ANSWER = '안녕! 오늘도 안전하게 같이 해보자.'


class CapturingMockProvider(MockProvider):
    """Retain safe request metadata while staying fully offline."""

    def __init__(self) -> None:
        """Initialize the deterministic provider and its call records."""
        super().__init__()
        self.calls = 0
        self.requests = []
        self.memories = []
        self.turns = []
        self.tools = []

    def complete(
        self,
        request,
        memories,
        conversation_turns,
        tools,
        conversation_summary=None,
    ):
        """Record one request and delegate to the network-free provider."""
        self.calls += 1
        self.requests.append(request)
        self.memories.append(list(memories))
        self.turns.append(list(conversation_turns))
        self.tools.append(list(tools))
        return super().complete(
            request,
            memories,
            conversation_turns,
            tools,
            conversation_summary,
        )


class SecretFailureMockProvider(CapturingMockProvider):
    """Raise one hostile local provider exception for CLI redaction tests."""

    def complete(self, *args, **kwargs):
        """Fail without making a network request."""
        del args, kwargs
        self.calls += 1
        raise RuntimeError(
            'provider-secret-label /private/voice/capture.wav '
            'raw-secret-transcript'
        )


class FixedMessageMockProvider(CapturingMockProvider):
    """Return one caller-selected message through the offline provider path."""

    def __init__(self, message: str) -> None:
        """Store the adversarial response message."""
        super().__init__()
        self.message = message

    def complete(self, *args, **kwargs):
        """Return one normalized message result without network access."""
        baseline = super().complete(*args, **kwargs)
        return replace(
            baseline,
            decision=AgentDecision(
                type='message',
                message=self.message,
                reason='offline-fixed-message',
                confidence=1.0,
            ),
        )


class OfflineOpenAIProvider(CapturingMockProvider):
    """Return OpenAI-shaped metadata without using an API or network."""

    def __init__(self, usage: ProviderUsage = None) -> None:
        """Store explicit offline token counters for provenance checks."""
        super().__init__()
        self.usage = usage or ProviderUsage(
            input_tokens=10,
            output_tokens=4,
            total_tokens=14,
        )

    def complete(self, *args, **kwargs):
        """Adapt the deterministic mock answer to a complete live result."""
        baseline = super().complete(*args, **kwargs)
        return replace(
            baseline,
            provider='openai',
            model='offline-openai-model',
            usage=self.usage,
            response_id='offline-openai-response',
        )


class FakeSTTBackend:
    """Return one fixed transcript without loading a model or network."""

    def __init__(
        self,
        text: str = '안녕',
        confidence: float = 0.98,
    ) -> None:
        """Store deterministic output and lifecycle observations."""
        self.text = text
        self.confidence = confidence
        self.prepare_calls = 0
        self.transcribe_calls = 0
        self.paths = []
        self.languages = []

    def prepare(self) -> None:
        """Record model preparation without loading any model."""
        self.prepare_calls += 1

    def transcribe(
        self,
        wav_path: Path,
        *,
        language: str,
    ) -> BackendTranscript:
        """Return a deterministic validated backend transcript."""
        self.transcribe_calls += 1
        self.paths.append(Path(wav_path))
        self.languages.append(language)
        assert Path(wav_path).is_file()
        return BackendTranscript(
            text=self.text,
            confidence=self.confidence,
            language=language,
            model='fake-stt-v1',
        )


class MissingPrepareBackend:
    """Expose transcription but deliberately omit model preparation."""

    def transcribe(self, wav_path: Path, *, language: str):
        """Fail the test if capture reaches this incomplete backend."""
        del wav_path, language
        raise AssertionError('transcription ran without prepare support')


class NonCallablePrepareBackend(MissingPrepareBackend):
    """Expose a hostile non-callable preparation attribute."""

    prepare = 'not-callable-private-value'


class FailingTextStream:
    """Inject one deterministic terminal write or flush failure."""

    encoding = 'utf-8'

    def __init__(self, error: Exception, *, fail_on: str) -> None:
        """Select the stream operation that raises the error."""
        self.error = error
        self.fail_on = fail_on
        self.values = []

    def write(self, value: str) -> int:
        """Record output or raise the configured write failure."""
        if self.fail_on == 'write':
            raise self.error
        self.values.append(value)
        return len(value)

    def flush(self) -> None:
        """Raise the configured flush failure when requested."""
        if self.fail_on == 'flush':
            raise self.error


class RecordingRunner:
    """Synthesize a bounded PCM WAV in place of ``arecord``."""

    def __init__(self) -> None:
        """Initialize argv and temporary-path observations."""
        self.calls = []
        self.capture_paths = []

    def __call__(self, argv, **kwargs):
        """Write one second of silence to the requested output path."""
        self.calls.append((list(argv), dict(kwargs)))
        path = Path(argv[-1])
        self.capture_paths.append(path)
        _write_wav(path)


class FactoryRecorder:
    """Return a prebuilt object while recording factory invocations."""

    def __init__(self, value) -> None:
        """Store the value returned by every invocation."""
        self.value = value
        self.calls = []

    def __call__(self, *args, **kwargs):
        """Record arguments and return the configured value."""
        self.calls.append((args, kwargs))
        return self.value


class ForbiddenSideEffect:
    """Fail a test if parsing or configuration reaches a side effect."""

    def __init__(self) -> None:
        """Initialize a visible invocation count."""
        self.calls = 0

    def __call__(self, *args, **kwargs):
        """Reject every unexpected invocation."""
        del args, kwargs
        self.calls += 1
        raise AssertionError('side effect happened before validation')


def _write_wav(path: Path, *, frame_count: int = 16000) -> None:
    """Write one valid mono PCM16 fixture using only the standard library."""
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\x00\x00' * frame_count)


def _write_secure_openai_env(
    path: Path,
    *,
    api_key: str = 'file-only-test-key',
    extra: str = '',
) -> None:
    """Write an owner-only regular OpenAI env fixture."""
    path.write_text(
        'OPENAI_API_KEY={}\nOPENAI_MODEL=offline-openai-model\n{}'.format(
            api_key,
            extra,
        ),
        encoding='utf-8',
    )
    path.chmod(0o600)


def _stt_result(text: str = '안녕') -> LocalSTTResult:
    """Build one validated local transcript without retaining audio."""
    return LocalSTTResult(
        text=text,
        confidence=0.98,
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


def _orchestrator(provider) -> AgentOrchestrator:
    """Create one fully in-memory, non-actuating agent runtime."""
    return AgentOrchestrator(
        provider=provider,
        memory_store=SQLiteMemoryStore(':memory:'),
        conversation_store=SQLiteConversationStore(':memory:'),
        safety_policy=SafetyPolicy(),
        trusted_robot_state=False,
    )


def _close_orchestrator(orchestrator: AgentOrchestrator) -> None:
    """Close both in-memory stores owned by a test orchestrator."""
    orchestrator.conversation_store.close()
    orchestrator.memory_store.close()


def _process_microphone_result(
    demo: voice_demo_module._LocalVoiceDemo,
    result: LocalSTTResult,
    **kwargs,
):
    """Invoke the trusted test capture boundary with its identity token."""
    return demo._process_trusted_microphone_result(
        result,
        capture_capability=demo.test_capture_capability,
        **kwargs,
    )


@contextmanager
def _demo_runtime(provider=None):
    """Yield one demo and close every local resource afterward."""
    selected_provider = provider or CapturingMockProvider()
    orchestrator = _orchestrator(selected_provider)
    capture_capability = object()
    try:
        with voice_demo_module._LocalVoiceDemo(
            orchestrator,
            capture_capability=capture_capability,
            clock_ns=lambda: 1234567890,
        ) as demo:
            demo.test_capture_capability = capture_capability
            yield demo, selected_provider, orchestrator
    finally:
        _close_orchestrator(orchestrator)


def _run_cli(
    capsys,
    tmp_path: Path,
    *,
    backend=None,
    provider=None,
    extra_argv=(),
):
    """Run the injected microphone CLI and return observations."""
    selected_backend = backend or FakeSTTBackend()
    selected_provider = provider or CapturingMockProvider()
    orchestrator = _orchestrator(selected_provider)
    runner = RecordingRunner()
    backend_factory = FactoryRecorder(selected_backend)
    orchestrator_factory = FactoryRecorder(orchestrator)
    missing_env = tmp_path / 'does-not-exist.env'
    argv = [
        '--microphone',
        '--env-file',
        str(missing_env),
        '--provider',
        'mock',
        *extra_argv,
    ]
    return_code = main(
        argv,
        environ={},
        stt_backend_factory=backend_factory,
        orchestrator_factory=orchestrator_factory,
        runner=runner,
        which=lambda name: '/usr/bin/arecord',
        clock_ns=lambda: 1234567890,
    )
    output = capsys.readouterr()
    return {
        'return_code': return_code,
        'output': output,
        'backend': selected_backend,
        'provider': selected_provider,
        'runner': runner,
        'backend_factory': backend_factory,
        'orchestrator_factory': orchestrator_factory,
    }


def _run_openai_cli(
    capsys,
    env_path: Path,
    *,
    provider=None,
    environ=None,
    extra_argv=(),
):
    """Run an entirely offline OpenAI-shaped demo configuration."""
    selected_provider = provider or OfflineOpenAIProvider()
    selected_backend = FakeSTTBackend()
    orchestrator = _orchestrator(selected_provider)
    runner = RecordingRunner()
    backend_factory = FactoryRecorder(selected_backend)
    orchestrator_factory = FactoryRecorder(orchestrator)
    with patch.object(
        voice_demo_module.urllib.request,
        'getproxies',
        return_value={},
    ):
        return_code = main(
            [
                '--microphone',
                '--provider',
                'openai',
                '--env-file',
                str(env_path),
                *extra_argv,
            ],
            environ={} if environ is None else environ,
            stt_backend_factory=backend_factory,
            orchestrator_factory=orchestrator_factory,
            runner=runner,
            which=lambda name: '/usr/bin/arecord',
            clock_ns=lambda: 1234567890,
        )
    output = capsys.readouterr()
    return {
        'return_code': return_code,
        'output': output,
        'backend': selected_backend,
        'provider': selected_provider,
        'runner': runner,
        'backend_factory': backend_factory,
        'orchestrator_factory': orchestrator_factory,
    }


def _run_openai_preflight_failure(
    capsys,
    env_path: Path,
    *,
    environ=None,
    proxies=None,
):
    """Run an OpenAI preflight that must stop before every side effect."""
    forbidden = ForbiddenSideEffect()
    with patch.object(
        voice_demo_module.urllib.request,
        'getproxies',
        return_value={} if proxies is None else proxies,
    ):
        return_code = main(
            [
                '--microphone',
                '--provider',
                'openai',
                '--env-file',
                str(env_path),
            ],
            environ={} if environ is None else environ,
            stt_backend_factory=forbidden,
            orchestrator_factory=forbidden,
            runner=forbidden,
            which=forbidden,
        )
    return return_code, capsys.readouterr(), forbidden


def test_only_cli_main_is_supported_microphone_provenance_boundary(
) -> None:
    """The internal capture capability is not exported as a public API."""
    assert 'LocalVoiceDemo' not in voice_demo_module.__all__
    assert 'OneShotVoiceDemo' not in voice_demo_module.__all__
    assert not hasattr(voice_demo_module, 'LocalVoiceDemo')
    assert not hasattr(voice_demo_module, 'OneShotVoiceDemo')


def test_microphone_result_reaches_agent_once_with_safe_defaults() -> None:
    """A final microphone transcript reaches one offline safe completion."""
    with _demo_runtime() as (demo, provider, _orchestrator_value):
        outcome = _process_microphone_result(
            demo,
            _stt_result(),
            utterance_id='voice-utterance-fixed',
            sequence=1,
            capture_epoch=1,
            timestamp_ns=1234567890,
        )

        assert outcome.message == SAFE_ANSWER
        assert provider.calls == 1
        assert len(provider.requests) == 1
        request = provider.requests[0]
        assert request.utterance == '안녕'
        assert request.user_id == demo.binding.user_id
        assert request.conversation_id == demo.binding.conversation_id
        assert request.request_id == outcome.pipeline_result.request_id
        assert request.turn_id == outcome.pipeline_result.turn_id
        assert request.robot_state == RobotState()
        assert request.available_tools == ()
        assert provider.memories == [[]]
        assert provider.turns == [[]]
        assert provider.tools == [[]]
        assert demo.coordinator.minimum_confidence == 0.60
        assert VOICE_DEMO_MINIMUM_CONFIDENCE == 0.60

        pipeline = outcome.pipeline_result
        assert pipeline.status == 'responded'
        assert pipeline.code == 'final_transcript_processed'
        assert pipeline.capture_epoch == 1
        assert pipeline.agent_result is not None
        assert pipeline.agent_result.state_trusted is False
        execution = pipeline.agent_result.to_dict()['execution']
        assert execution['authorized'] is False
        assert execution['proposal_authorized'] is False
        assert execution['state_trusted'] is False


def test_demo_confidence_gate_rejects_before_provider() -> None:
    """The provisional demo threshold fails closed below 0.60."""
    with _demo_runtime() as (demo, provider, _orchestrator_value):
        result = replace(_stt_result(), confidence=0.59)

        outcome = _process_microphone_result(demo, result)

        assert outcome.message is None
        assert outcome.pipeline_result.status == 'rejected'
        assert outcome.pipeline_result.code == 'low_confidence'
        assert provider.calls == 0


def test_untrusted_local_result_cannot_claim_microphone_provenance() -> None:
    """A result without the capture-owner capability is never promoted."""
    with _demo_runtime() as (demo, provider, _orchestrator_value):
        with pytest.raises(VoiceDemoError) as caught:
            demo._process_trusted_microphone_result(
                _stt_result('untrusted-private-transcript'),
                capture_capability=object(),
            )

        error = caught.value
        assert error.__cause__ is None
        assert error.__context__ is None
        assert 'untrusted-private-transcript' not in str(error)
        assert provider.calls == 0


def test_binding_and_ids_are_server_owned_and_internally_consistent() -> None:
    """The demo owns identity while every downstream ID remains consistent."""
    provider = CapturingMockProvider()
    orchestrator = _orchestrator(provider)
    tokens = iter(
        ('session-token', 'conversation-token', 'close-token')
    )
    capture_capability = object()
    try:
        demo = voice_demo_module._LocalVoiceDemo(
            orchestrator,
            capture_capability=capture_capability,
            id_factory=lambda: next(tokens),
            clock_ns=lambda: 1234567890,
        )
        demo.test_capture_capability = capture_capability
        binding = demo.binding
        outcome = _process_microphone_result(
            demo,
            _stt_result(),
            utterance_id='voice-utterance-fixed',
            sequence=1,
            capture_epoch=1,
            timestamp_ns=1234567890,
        )

        assert binding.to_dict() == {
            'user_id': 'local-voice-user',
            'speaker_id': 'local-voice-speaker',
            'speech_session_id': 'voice-session-session-token',
            'conversation_id': (
                'voice-conversation-conversation-token'
            ),
            'source': 'local-stt',
        }
        pipeline = outcome.pipeline_result
        assert pipeline.tts_request is not None
        tts = pipeline.tts_request
        assert tts.speech_session_id == binding.speech_session_id
        assert tts.conversation_id == binding.conversation_id
        assert tts.turn_id == pipeline.turn_id
        assert tts.source_utterance_id == 'voice-utterance-fixed'
        assert tts.text == outcome.message
        assert pipeline.request_id.startswith('speech-request-')
        assert pipeline.turn_id.startswith('speech-turn-')
        assert tts.request_id.startswith('speech-tts-')
        assert demo.close().code == 'session_closed'
        with pytest.raises(StopIteration):
            next(tokens)
    finally:
        _close_orchestrator(orchestrator)


def test_tts_is_immediately_acknowledged_and_advances_capture_epoch() -> None:
    """The one-shot boundary terminally acknowledges its TTS request."""
    with _demo_runtime() as (demo, _provider, _orchestrator_value):
        outcome = _process_microphone_result(
            demo,
            _stt_result(),
            utterance_id='voice-utterance-fixed',
            sequence=1,
            capture_epoch=1,
            timestamp_ns=1234567890,
        )

        terminal = outcome.tts_terminal_result
        assert terminal is not None
        assert terminal.status == 'ready'
        assert terminal.code == 'tts_terminal'
        assert terminal.capture_epoch == 2
        assert demo.capture_epoch == 2
        assert demo.sequence == 1


def test_duplicate_is_cached_and_conflicting_utterance_is_rejected() -> None:
    """Replay is idempotent while a mutated utterance fails closed."""
    with _demo_runtime() as (demo, provider, _orchestrator_value):
        first = _process_microphone_result(
            demo,
            _stt_result(),
            utterance_id='voice-utterance-fixed',
            sequence=1,
            capture_epoch=1,
            timestamp_ns=1234567890,
        )
        duplicate = _process_microphone_result(
            demo,
            _stt_result(),
            utterance_id='voice-utterance-fixed',
            sequence=1,
            capture_epoch=1,
            timestamp_ns=1234567890,
        )
        conflict = _process_microphone_result(
            demo,
            replace(_stt_result(), text='안녕 변조된 원문'),
            utterance_id='voice-utterance-fixed',
            sequence=1,
            capture_epoch=1,
            timestamp_ns=1234567890,
        )

        assert duplicate.pipeline_result == first.pipeline_result
        assert duplicate.message == first.message
        assert duplicate.tts_terminal_result is not None
        assert duplicate.tts_terminal_result.code == 'tts_already_terminal'
        assert conflict.message is None
        assert conflict.tts_terminal_result is None
        assert conflict.pipeline_result.status == 'rejected'
        assert conflict.pipeline_result.code == 'utterance_conflict'
        assert provider.calls == 1


def test_outcome_audit_and_repr_exclude_raw_transcript_and_paths() -> None:
    """Public results retain the answer but not private STT provenance."""
    raw_transcript = '안녕 raw-secret-transcript'
    private_path = '/private/voice/capture.wav'
    with _demo_runtime() as (demo, _provider, _orchestrator_value):
        outcome = _process_microphone_result(
            demo,
            _stt_result(raw_transcript),
            utterance_id='voice-utterance-fixed',
            sequence=1,
            capture_epoch=1,
            timestamp_ns=1234567890,
        )

        public = json.dumps(
            outcome.pipeline_result.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )
        diagnostics = repr(outcome)
        audit = outcome.to_audit_dict()
        assert outcome.message == SAFE_ANSWER
        assert audit == {
            'status': 'responded',
            'code': 'final_transcript_processed',
            'capture_epoch': 1,
            'message_chars': len(SAFE_ANSWER),
            'tts_status': 'ready',
            'tts_code': 'tts_terminal',
        }
        for forbidden in (
            raw_transcript,
            'raw-secret-transcript',
            private_path,
            'capture.wav',
        ):
            assert forbidden not in public
            assert forbidden not in diagnostics


def test_provider_exception_becomes_fresh_content_free_demo_error() -> None:
    """A hostile provider exception is replaced at the public boundary."""
    provider = SecretFailureMockProvider()
    with _demo_runtime(provider) as (
        demo,
        _provider,
        _orchestrator_value,
    ):
        with pytest.raises(VoiceDemoError) as caught:
            _process_microphone_result(
                demo,
                _stt_result('안녕 raw-secret-transcript'),
                utterance_id='voice-utterance-fixed',
                sequence=1,
                capture_epoch=1,
                timestamp_ns=1234567890,
            )

        error = caught.value
        assert error.__cause__ is None
        assert error.__context__ is None
        diagnostics = repr(error) + str(error)
        for forbidden in (
            'raw-secret-transcript',
            'provider-secret-label',
            '/private/voice/capture.wav',
            'RuntimeError',
        ):
            assert forbidden not in diagnostics


def test_cli_runs_offline_pipeline_and_prints_only_safe_answer(
    capsys,
    tmp_path: Path,
) -> None:
    """The injected CLI captures once and emits only the final safe answer."""
    observations = _run_cli(capsys, tmp_path)

    assert observations['return_code'] == 0
    assert observations['output'].out == SAFE_ANSWER + '\n'
    assert observations['output'].err == ''
    backend = observations['backend']
    provider = observations['provider']
    runner = observations['runner']
    assert backend.prepare_calls == 1
    assert backend.transcribe_calls == 1
    assert backend.languages == ['ko']
    assert provider.calls == 1
    assert len(runner.calls) == 1
    assert runner.calls[0][1]['shell'] is False
    assert len(runner.capture_paths) == 1
    assert not runner.capture_paths[0].exists()
    assert not runner.capture_paths[0].parent.exists()
    assert len(backend.paths) == 1
    assert not backend.paths[0].exists()
    assert not backend.paths[0].parent.exists()


def test_cli_failure_is_sanitized_and_cleans_private_audio(
    capsys,
    tmp_path: Path,
) -> None:
    """Keep hostile provider details out of public CLI diagnostics."""
    raw_transcript = '안녕 raw-secret-transcript'
    observations = _run_cli(
        capsys,
        tmp_path,
        backend=FakeSTTBackend(raw_transcript),
        provider=SecretFailureMockProvider(),
    )

    assert observations['return_code'] != 0
    assert observations['output'].out == ''
    stderr = observations['output'].err
    for forbidden in (
        raw_transcript,
        'raw-secret-transcript',
        'provider-secret-label',
        '/private/voice/capture.wav',
        'Traceback',
        'RuntimeError',
    ):
        assert forbidden not in stderr
    runner = observations['runner']
    assert len(runner.capture_paths) == 1
    assert not runner.capture_paths[0].exists()
    assert not runner.capture_paths[0].parent.exists()
    backend = observations['backend']
    assert len(backend.paths) == 1
    assert not backend.paths[0].exists()
    assert not backend.paths[0].parent.exists()


@pytest.mark.parametrize(
    'argv',
    [
        [],
        ['--wav', '/private/voice/raw-secret.wav'],
        ['--microphone', '--seconds', '5.0'],
        ['--microphone', '--seconds', '31'],
        ['--microphone', '--language', 'ko\nraw-secret-label'],
        [
            '--microphone',
            '--audio-device',
            'device\n/private/voice/raw-secret.wav',
        ],
        ['--microphone', '--stt-model', 'unsupported-secret-model'],
        ['--microphone', '--provider', 'unsupported-secret-provider'],
    ],
)
def test_cli_rejects_invalid_arguments_before_any_side_effect(
    argv,
    capsys,
) -> None:
    """Parse safely before config, model, microphone, or DB use."""
    forbidden = ForbiddenSideEffect()
    with pytest.raises(SystemExit) as caught:
        main(
            argv,
            environ={},
            stt_backend_factory=forbidden,
            orchestrator_factory=forbidden,
            runner=forbidden,
            which=forbidden,
        )

    assert caught.value.code == 2
    assert forbidden.calls == 0
    output = capsys.readouterr()
    assert output.out == ''
    assert 'invalid arguments' in output.err
    for secret in (
        '/private/voice/raw-secret.wav',
        'raw-secret-label',
        'unsupported-secret-model',
        'unsupported-secret-provider',
    ):
        assert secret not in output.err


def test_openai_without_api_key_fails_before_stt_or_capture(
    capsys,
    tmp_path: Path,
) -> None:
    """Missing OpenAI credentials fail closed without external side effects."""
    forbidden = ForbiddenSideEffect()
    missing_env = tmp_path / 'missing-openai.env'
    return_code = main(
        [
            '--microphone',
            '--env-file',
            str(missing_env),
            '--provider',
            'openai',
        ],
        environ={'MALBUT_AGENT_AUTH_TOKEN': 'local-test-token'},
        stt_backend_factory=forbidden,
        orchestrator_factory=forbidden,
        runner=forbidden,
        which=forbidden,
    )

    assert return_code != 0
    assert forbidden.calls == 0
    output = capsys.readouterr()
    assert output.out == ''
    assert 'Traceback' not in output.err
    assert 'local-test-token' not in output.err
    assert str(missing_env) not in output.err


def test_secure_file_without_key_ignores_process_key_before_capture(
    capsys,
    tmp_path: Path,
) -> None:
    """A process key cannot fill a missing private-file credential."""
    env_path = tmp_path / 'openai-without-key.env'
    env_path.write_text(
        'OPENAI_MODEL=offline-openai-model\n',
        encoding='utf-8',
    )
    env_path.chmod(0o600)

    return_code, output, forbidden = _run_openai_preflight_failure(
        capsys,
        env_path,
        environ={'OPENAI_API_KEY': 'process-key-must-not-be-used'},
    )

    assert return_code == 7
    assert forbidden.calls == 0
    assert output.out == ''
    assert output.err == 'error: local voice demo failed\n'
    assert 'process-key-must-not-be-used' not in output.err
    assert str(env_path) not in output.err


@pytest.mark.parametrize(
    ('case_name', 'api_key'),
    [
        ('nul', 'visible\x00private'),
        ('space', 'visible private'),
        ('non_ascii', '비밀-key'),
        ('too_long', 'x' * 4097),
    ],
)
def test_secure_file_rejects_non_visible_or_unbounded_api_keys(
    case_name: str,
    api_key: str,
    capsys,
    tmp_path: Path,
) -> None:
    """Malformed file credentials fail before provider or microphone use."""
    env_path = tmp_path / ('invalid-key-' + case_name + '.env')
    _write_secure_openai_env(env_path, api_key=api_key)

    return_code, output, forbidden = _run_openai_preflight_failure(
        capsys,
        env_path,
    )

    assert return_code == 7
    assert forbidden.calls == 0
    assert output.out == ''
    assert output.err == 'error: local voice demo failed\n'
    assert api_key not in output.err
    assert str(env_path) not in output.err


def test_audio_device_punctuation_is_one_non_shell_argv_value(
    capsys,
    tmp_path: Path,
) -> None:
    """A valid ALSA label remains one literal non-shell recorder argument."""
    device = 'plughw:1,0;not-a-command'
    observations = _run_cli(
        capsys,
        tmp_path,
        extra_argv=('--audio-device', device),
    )

    assert observations['return_code'] == 0
    argv, kwargs = observations['runner'].calls[0]
    assert kwargs['shell'] is False
    index = argv.index('--device')
    assert argv[index + 1] == device
    assert argv.count(device) == 1


def test_mock_mode_never_reads_an_env_file(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    """Offline mock mode must not touch a credential-bearing env path."""
    forbidden_loader = ForbiddenSideEffect()
    monkeypatch.setattr(
        voice_demo_module,
        '_read_secure_env_file',
        forbidden_loader,
    )

    observations = _run_cli(capsys, tmp_path)

    assert observations['return_code'] == 0
    assert observations['output'].out == SAFE_ANSWER + '\n'
    assert forbidden_loader.calls == 0


def test_openai_requires_an_explicit_secure_env_file(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    """Process environment credentials alone never enable OpenAI mode."""
    monkeypatch.chdir(tmp_path)
    forbidden = ForbiddenSideEffect()

    return_code = main(
        ['--microphone', '--provider', 'openai'],
        environ={'OPENAI_API_KEY': 'process-only-secret-key'},
        stt_backend_factory=forbidden,
        orchestrator_factory=forbidden,
        runner=forbidden,
        which=forbidden,
    )

    assert return_code == 7
    assert forbidden.calls == 0
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'error: local voice demo failed\n'
    assert 'process-only-secret-key' not in output.err


@pytest.mark.parametrize('file_kind', ['symlink', 'fifo', 'world_readable'])
def test_openai_rejects_non_private_or_non_regular_env_files(
    file_kind: str,
    capsys,
    tmp_path: Path,
) -> None:
    """Reject links, FIFOs, and broad permissions for OpenAI config."""
    env_path = tmp_path / 'openai-private.env'
    if file_kind == 'symlink':
        target = tmp_path / 'real-private.env'
        _write_secure_openai_env(target)
        env_path.symlink_to(target)
    elif file_kind == 'fifo':
        os.mkfifo(env_path, mode=0o600)
    else:
        _write_secure_openai_env(env_path)
        env_path.chmod(0o644)
    forbidden = ForbiddenSideEffect()

    return_code = main(
        [
            '--microphone',
            '--provider',
            'openai',
            '--env-file',
            str(env_path),
        ],
        environ={},
        stt_backend_factory=forbidden,
        orchestrator_factory=forbidden,
        runner=forbidden,
        which=forbidden,
    )

    assert return_code == 7
    assert forbidden.calls == 0
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'error: local voice demo failed\n'
    assert str(env_path) not in output.err


def test_openai_rejects_env_file_not_owned_by_current_user(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    """An otherwise private config fails when its owner is not the caller."""
    env_path = tmp_path / 'wrong-owner.env'
    _write_secure_openai_env(env_path)
    actual_uid = env_path.stat().st_uid
    forbidden = ForbiddenSideEffect()
    with monkeypatch.context() as scoped:
        scoped.setattr(os, 'getuid', lambda: actual_uid + 1)
        scoped.setattr(os, 'geteuid', lambda: actual_uid + 1)
        return_code = main(
            [
                '--microphone',
                '--provider',
                'openai',
                '--env-file',
                str(env_path),
            ],
            environ={},
            stt_backend_factory=forbidden,
            orchestrator_factory=forbidden,
            runner=forbidden,
            which=forbidden,
        )

    assert return_code == 7
    assert forbidden.calls == 0
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'error: local voice demo failed\n'


def test_openai_rejects_urllib_proxy_before_factory_or_capture(
    capsys,
    tmp_path: Path,
) -> None:
    """Inherited urllib proxies cannot redirect the approved OpenAI call."""
    env_path = tmp_path / 'openai-private.env'
    _write_secure_openai_env(env_path)
    proxy_value = 'http://proxy-private-value.invalid:8123'

    return_code, output, forbidden = _run_openai_preflight_failure(
        capsys,
        env_path,
        proxies={'https': proxy_value},
    )

    assert return_code == 7
    assert forbidden.calls == 0
    assert output.out == ''
    assert output.err == 'error: local voice demo failed\n'
    assert proxy_value not in output.err
    assert str(env_path) not in output.err


def test_no_proxy_bypass_list_alone_does_not_block_direct_openai(
    capsys,
    tmp_path: Path,
) -> None:
    """A NO_PROXY bypass list is not itself a redirecting proxy."""
    env_path = tmp_path / 'openai-private.env'
    _write_secure_openai_env(env_path)
    with patch.object(
        voice_demo_module.urllib.request,
        'getproxies',
        return_value={'no': 'localhost,127.0.0.1'},
    ):
        observations = _run_openai_cli(capsys, env_path)

    assert observations['return_code'] == 0
    assert observations['output'].out == SAFE_ANSWER + '\n'
    assert observations['output'].err == ''


def test_openai_env_file_wins_and_hidden_reliability_knobs_are_forced(
    capsys,
    tmp_path: Path,
) -> None:
    """Only the private file supplies secrets while demo bounds stay fixed."""
    env_path = tmp_path / 'openai-private.env'
    _write_secure_openai_env(env_path, api_key='file-only-test-key')
    assert stat.S_ISREG(env_path.stat().st_mode)
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert env_path.stat().st_uid == os.getuid()
    hostile_environment = {
        'OPENAI_API_KEY': 'process-secret-must-lose',
        'OPENAI_MODEL': 'process-model-must-lose',
        'OPENAI_FALLBACK_MODEL': 'hidden-fallback-model',
        'MALBUT_AGENT_PROVIDER_MAX_RETRIES': '3',
        'MALBUT_AGENT_TIMEOUT_SECONDS': '120',
        'MALBUT_AGENT_PROVIDER_TOTAL_TIMEOUT_SECONDS': '300',
        'OPENAI_REASONING_EFFORT': 'high',
        'OPENAI_MAX_OUTPUT_TOKENS': '4096',
    }

    observations = _run_openai_cli(
        capsys,
        env_path,
        environ=hostile_environment,
    )

    assert observations['return_code'] == 0
    assert observations['output'].out == SAFE_ANSWER + '\n'
    factory_calls = observations['orchestrator_factory'].calls
    assert len(factory_calls) == 1
    settings = factory_calls[0][0][0]
    assert settings.openai_api_key == 'file-only-test-key'
    assert settings.openai_model == 'offline-openai-model'
    assert settings.openai_fallback_model == ''
    assert settings.provider_max_retries == 0
    assert settings.request_timeout_seconds == 15
    assert settings.provider_total_timeout_seconds == 20
    assert settings.openai_reasoning_effort == 'none'
    assert settings.openai_max_output_tokens == 500
    rendered = observations['output'].out + observations['output'].err
    assert 'file-only-test-key' not in rendered
    assert 'process-secret-must-lose' not in rendered


@pytest.mark.parametrize(
    ('input_tokens', 'output_tokens', 'total_tokens', 'expected_exit'),
    [
        (0, 0, 0, 7),
        (0, 0, 1, 7),
        (1, 0, 1, 7),
        (0, 1, 1, 7),
        (1, 1, 2, 0),
    ],
)
def test_openai_requires_all_positive_usage_counters(
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    expected_exit: int,
    capsys,
    tmp_path: Path,
) -> None:
    """Zero-valued usage cannot prove a successful billable response."""
    env_path = tmp_path / 'openai-private.env'
    _write_secure_openai_env(env_path)
    provider = OfflineOpenAIProvider(
        ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
    )

    observations = _run_openai_cli(
        capsys,
        env_path,
        provider=provider,
    )

    assert observations['return_code'] == expected_exit
    assert provider.calls == 1
    if expected_exit == 0:
        assert observations['output'].out == SAFE_ANSWER + '\n'
        assert observations['output'].err == ''
    else:
        assert observations['output'].out == ''
        assert observations['output'].err == (
            'error: local voice demo failed\n'
        )


def test_openai_reliable_safe_fallback_with_no_usage_exits_seven(
    capsys,
    tmp_path: Path,
) -> None:
    """Provider exhaustion cannot masquerade as a successful voice reply."""
    env_path = tmp_path / 'openai-private.env'
    _write_secure_openai_env(env_path)
    failing_provider = SecretFailureMockProvider()
    reliable = ReliableProvider(
        [failing_provider],
        max_retries=0,
        attempt_timeout_seconds=15,
        total_timeout_seconds=20,
    )

    observations = _run_openai_cli(
        capsys,
        env_path,
        provider=reliable,
    )

    assert observations['return_code'] == 7
    assert observations['output'].out == ''
    assert observations['output'].err == (
        'error: local voice demo failed\n'
    )
    assert failing_provider.calls == 1
    assert 'safe-non-action' not in observations['output'].err
    assert 'raw-secret-transcript' not in observations['output'].err


@pytest.mark.parametrize(
    'unsafe_character',
    [
        '\u0085',
        '\u200b',
        '\ud800',
        '\u202e',
        '\u2066',
        '\u2028',
        '\u2029',
    ],
)
def test_unicode_controls_and_bidi_never_reach_stdout(
    unsafe_character: str,
    capsys,
    tmp_path: Path,
) -> None:
    """Cc, Cf, Cs, and bidi controls fail before terminal output."""
    message = 'safe-prefix' + unsafe_character + 'private-suffix'

    observations = _run_cli(
        capsys,
        tmp_path,
        provider=FixedMessageMockProvider(message),
    )

    assert observations['return_code'] == 7
    assert observations['output'].out == ''
    assert observations['output'].err == (
        'error: local voice demo failed\n'
    )
    assert unsafe_character not in observations['output'].err
    assert 'private-suffix' not in observations['output'].err


@pytest.mark.parametrize(
    ('error', 'fail_on'),
    [
        (BrokenPipeError('private broken pipe path'), 'write'),
        (
            UnicodeEncodeError(
                'ascii',
                '\ud55c',
                0,
                1,
                'private terminal codec',
            ),
            'write',
        ),
        (BrokenPipeError('private flush failure'), 'flush'),
    ],
)
def test_terminal_output_failures_are_sanitized(
    error: Exception,
    fail_on: str,
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    """Terminal write and flush errors return seven without a traceback."""
    stream = FailingTextStream(error, fail_on=fail_on)
    with monkeypatch.context() as scoped:
        scoped.setattr(sys, 'stdout', stream)
        observations = _run_cli(capsys, tmp_path)

    assert observations['return_code'] == 7
    assert observations['output'].out == ''
    assert observations['output'].err == (
        'error: local voice demo failed\n'
    )
    assert 'private' not in observations['output'].err
    assert 'Traceback' not in observations['output'].err


@pytest.mark.parametrize(
    'backend',
    [MissingPrepareBackend(), NonCallablePrepareBackend()],
)
def test_missing_or_noncallable_prepare_fails_before_capture(
    backend,
    capsys,
    tmp_path: Path,
) -> None:
    """An unprepared backend fails closed before microphone activation."""
    observations = _run_cli(
        capsys,
        tmp_path,
        backend=backend,
    )

    assert observations['return_code'] == 4
    assert observations['output'].out == ''
    assert observations['output'].err == (
        'error: local speech recognition failed\n'
    )
    assert observations['runner'].calls == []


def test_low_confidence_cli_emits_only_allowlisted_retry_code(
    capsys,
    tmp_path: Path,
) -> None:
    """A weak transcript asks for retry without exposing its text."""
    transcript = 'raw-low-confidence-private-transcript'
    observations = _run_cli(
        capsys,
        tmp_path,
        backend=FakeSTTBackend(transcript, confidence=0.54),
    )

    assert observations['return_code'] == 8
    assert observations['output'].out == ''
    assert observations['output'].err == (
        'retry: speech confidence is too low; please retry\n'
    )
    assert transcript not in observations['output'].err
    assert observations['provider'].calls == 0
