"""Safe one-shot microphone demo for the local speech-agent pipeline.

The command keeps raw audio inside the temporary capture boundary, sends
only the validated transcript to the conversation coordinator, disables
tools, and acknowledges the text-only TTS request without synthesizing it.
The :func:`main` command is the only supported provenance boundary; private
runtime helpers exist solely to keep that flow testable in-process.
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import urllib.request
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import (
    Any,
    Callable,
    Mapping,
    Optional,
    Sequence,
)

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.local_stt import (
    AudioValidationError,
    BackendUnavailableError,
    CaptureError,
    FasterWhisperBackend,
    LocalSTTError,
    LocalSTTResult,
    NoSpeechError,
    TranscriptionError,
    build_transcript_event,
    capture_microphone,
    transcribe_wav,
)
from malbut_agent_server.orchestrator import (
    AgentOrchestrator,
    OrchestrationResult,
)
from malbut_agent_server.schemas import (
    ProviderResult,
    ProviderUsage,
    RobotState,
)
from malbut_agent_server.speech import (
    SpeechControlResult,
    SpeechConversationCoordinator,
    SpeechPipelineResult,
    TrustedSpeechBinding,
)


VOICE_USER_ID = 'local-voice-user'
VOICE_SPEAKER_ID = 'local-voice-speaker'
VOICE_SOURCE = 'local-stt'
VOICE_DEMO_EXIT_CODE = 7
VOICE_DEMO_RETRY_EXIT_CODE = 8
RECOMMENDED_ENV_FILE = str(
    Path(__file__).resolve().parent.parent / '.env.local'
)
DEFAULT_ENV_FILE = RECOMMENDED_ENV_FILE
DEFAULT_STT_MODEL = 'small'
DEFAULT_STT_DEVICE = 'cpu'
DEFAULT_STT_COMPUTE_TYPE = 'int8'
DEFAULT_LANGUAGE = 'ko'
DEFAULT_CAPTURE_SECONDS = 5
VOICE_DEMO_MINIMUM_CONFIDENCE = 0.60
MAX_ARGUMENT_LENGTH = 128
MAX_ENV_PATH_LENGTH = 4096
MAX_ENV_FILE_BYTES = 64 * 1024
MAX_OPENAI_KEY_LENGTH = 4096
OPENAI_ENV_KEYS = frozenset(
    {
        'OPENAI_API_KEY',
        'OPENAI_BASE_URL',
        'OPENAI_MODEL',
    }
)
OPENAI_REQUEST_TIMEOUT_SECONDS = 15
OPENAI_TOTAL_TIMEOUT_SECONDS = 20
LOW_CONFIDENCE_MESSAGE = (
    'retry: speech confidence is too low; please retry'
)
RETRYABLE_PIPELINE_MESSAGE = (
    'retry: local voice pipeline is retryable; please retry'
)
RETRYABLE_PIPELINE_RESULTS = frozenset(
    {
        ('retryable', 'conversation_conflict'),
        ('retryable', 'inference_in_progress'),
        ('discarded', 'capture_epoch_changed_during_inference'),
        ('discarded', 'inference_cancelled_before_commit'),
        ('discarded', 'inference_reservation_lost'),
    }
)
STT_EXIT_CODES = {
    LocalSTTError: 1,
    AudioValidationError: 2,
    CaptureError: 3,
    BackendUnavailableError: 4,
    NoSpeechError: 5,
    TranscriptionError: 6,
}


class VoiceDemoError(RuntimeError):
    """Content-free terminal failure at the local demo boundary."""

    exit_code = VOICE_DEMO_EXIT_CODE
    public_message = 'local voice demo failed'

    def __init__(self) -> None:
        """Create a fixed diagnostic with no caller-controlled content."""
        super().__init__(self.public_message)


class VoiceDemoEnvironmentError(VoiceDemoError):
    """Raised when an explicitly selected OpenAI env file is unsafe."""

    public_message = 'local voice demo environment is unavailable'


@dataclass(frozen=True, repr=False)
class VoiceDemoOutcome:
    """One coordinator result and its immediate no-audio TTS terminal ack."""

    message: Optional[str]
    pipeline_result: SpeechPipelineResult
    tts_terminal_result: Optional[SpeechControlResult]

    def __post_init__(self) -> None:
        """Reject forged result objects at this public local boundary."""
        if self.message is not None:
            if (
                type(self.message) is not str
                or not self.message
                or _contains_control_character(self.message)
            ):
                raise VoiceDemoError()
        if type(self.pipeline_result) is not SpeechPipelineResult:
            raise VoiceDemoError()
        if (
            not _is_safe_result_label(self.pipeline_result.status)
            or not _is_safe_result_label(self.pipeline_result.code)
            or type(self.pipeline_result.capture_epoch) is not int
            or self.pipeline_result.capture_epoch < 0
        ):
            raise VoiceDemoError()
        if (
            self.tts_terminal_result is not None
            and type(self.tts_terminal_result) is not SpeechControlResult
        ):
            raise VoiceDemoError()
        if self.tts_terminal_result is not None:
            if (
                not _is_safe_result_label(
                    self.tts_terminal_result.status
                )
                or not _is_safe_result_label(
                    self.tts_terminal_result.code
                )
                or type(self.tts_terminal_result.capture_epoch) is not int
                or self.tts_terminal_result.capture_epoch < 0
            ):
                raise VoiceDemoError()

    def __repr__(self) -> str:
        """Represent only content-free state and response measurements."""
        return 'VoiceDemoOutcome({!r})'.format(self.to_audit_dict())

    def to_audit_dict(self) -> dict:
        """Return status metadata without transcript or response content."""
        return {
            'status': self.pipeline_result.status,
            'code': self.pipeline_result.code,
            'capture_epoch': self.pipeline_result.capture_epoch,
            'message_chars': (
                len(self.message) if self.message is not None else 0
            ),
            'tts_status': (
                self.tts_terminal_result.status
                if self.tts_terminal_result is not None
                else None
            ),
            'tts_code': (
                self.tts_terminal_result.code
                if self.tts_terminal_result is not None
                else None
            ),
        }


def _default_id_factory() -> str:
    """Return one server-owned opaque identifier token."""
    return uuid.uuid4().hex


def _contains_control_character(value: str) -> bool:
    """Return whether a string can alter terminal or transport framing."""
    return any(
        unicodedata.category(character) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'}
        for character in value
    )


def _is_safe_result_label(value: Any) -> bool:
    """Return whether a coordinator label is safe for audit output."""
    return (
        type(value) is str
        and 1 <= len(value) <= 64
        and value.isascii()
        and all(
            character.isalnum() or character == '_'
            for character in value
        )
    )


def _safe_server_id(prefix: str, factory: Callable[[], str]) -> str:
    """Create an allowlisted ID without reflecting factory output."""
    failed = False
    try:
        token = factory()
    except Exception:
        failed = True
        token = None
    if (
        failed
        or type(token) is not str
        or not token
        or len(token) > 64
        or not token.isascii()
        or any(
            not (character.isalnum() or character in {'-', '_'})
            for character in token
        )
    ):
        raise VoiceDemoError()
    return '{}-{}'.format(prefix, token)


def _safe_timestamp(clock_ns: Callable[[], int]) -> int:
    """Read one bounded timestamp from the injected server clock."""
    failed = False
    try:
        value = clock_ns()
    except Exception:
        failed = True
        value = None
    if (
        failed
        or type(value) is not int
        or value < 0
        or value > (1 << 63) - 1
    ):
        raise VoiceDemoError()
    return value


class _LocalVoiceDemo:
    """Internal microphone binding used only by the supported CLI flow."""

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        *,
        capture_capability: object,
        id_factory: Optional[Callable[[], str]] = None,
        clock_ns: Optional[Callable[[], int]] = None,
    ) -> None:
        """Open one server-owned speech and conversation session."""
        self.orchestrator = orchestrator
        if type(capture_capability) is not object:
            raise VoiceDemoError()
        self._capture_capability = capture_capability
        self._id_factory = id_factory or _default_id_factory
        self._clock_ns = clock_ns or time.time_ns
        if not callable(self._id_factory) or not callable(self._clock_ns):
            raise VoiceDemoError()

        initialization_failed = False
        try:
            self.coordinator = SpeechConversationCoordinator(
                orchestrator,
                minimum_confidence=VOICE_DEMO_MINIMUM_CONFIDENCE,
            )
            self.binding = TrustedSpeechBinding(
                user_id=VOICE_USER_ID,
                speaker_id=VOICE_SPEAKER_ID,
                speech_session_id=_safe_server_id(
                    'voice-session',
                    self._id_factory,
                ),
                conversation_id=_safe_server_id(
                    'voice-conversation',
                    self._id_factory,
                ),
                source=VOICE_SOURCE,
            )
            opened = self.coordinator.open_session(self.binding)
            if (
                type(opened) is not SpeechControlResult
                or opened.status != 'ready'
                or opened.code not in {
                    'session_opened',
                    'session_already_open',
                }
            ):
                initialization_failed = True
            self._capture_epoch = opened.capture_epoch
        except Exception:
            initialization_failed = True
        if initialization_failed:
            raise VoiceDemoError()

        self._next_sequence = 1
        self._last_sequence = 0
        self._closed = False
        self._close_result: Optional[SpeechControlResult] = None

    @property
    def capture_epoch(self) -> int:
        """Return the coordinator epoch to use for the next capture."""
        return self._capture_epoch

    @property
    def sequence(self) -> int:
        """Return the greatest transcript sequence processed so far."""
        return self._last_sequence

    def _process_trusted_microphone_result(
        self,
        result: LocalSTTResult,
        *,
        capture_capability: object,
        utterance_id: Optional[str] = None,
        sequence: Optional[int] = None,
        capture_epoch: Optional[int] = None,
        timestamp_ns: Optional[int] = None,
    ) -> VoiceDemoOutcome:
        """Process a result owned by this CLI's private capture flow."""
        failed = False
        outcome = None
        try:
            if (
                self._closed
                or capture_capability is not self._capture_capability
                or type(result) is not LocalSTTResult
            ):
                raise VoiceDemoError()
            event_sequence = (
                self._next_sequence if sequence is None else sequence
            )
            event_epoch = (
                self._capture_epoch
                if capture_epoch is None
                else capture_epoch
            )
            event_id = (
                _safe_server_id('voice-utterance', self._id_factory)
                if utterance_id is None
                else utterance_id
            )
            event_timestamp = (
                _safe_timestamp(self._clock_ns)
                if timestamp_ns is None
                else timestamp_ns
            )
            event = build_transcript_event(
                result,
                self.binding,
                utterance_id=event_id,
                sequence=event_sequence,
                capture_epoch=event_epoch,
                timestamp_ns=event_timestamp,
                capture_origin='microphone',
            )
            pipeline = self.coordinator.handle_transcript(
                event,
                robot_state=RobotState(),
                available_tools=(),
            )
            if type(pipeline) is not SpeechPipelineResult:
                raise VoiceDemoError()

            if type(event_sequence) is int:
                self._last_sequence = max(
                    self._last_sequence,
                    event_sequence,
                )
                self._next_sequence = max(
                    self._next_sequence,
                    event_sequence + 1,
                )
            self._capture_epoch = pipeline.capture_epoch

            terminal = None
            message = None
            if pipeline.status == 'responded':
                if (
                    pipeline.code != 'final_transcript_processed'
                    or pipeline.agent_result is None
                    or pipeline.tts_request is None
                ):
                    raise VoiceDemoError()
                message = pipeline.agent_result.decision.message
                if (
                    type(message) is not str
                    or not message
                    or _contains_control_character(message)
                ):
                    raise VoiceDemoError()
                terminal = self.coordinator.mark_tts_terminal(
                    self.binding.speech_session_id,
                    pipeline.tts_request.request_id,
                )
                if (
                    type(terminal) is not SpeechControlResult
                    or terminal.status != 'ready'
                    or terminal.code not in {
                        'tts_terminal',
                        'tts_already_terminal',
                    }
                ):
                    raise VoiceDemoError()
                self._capture_epoch = terminal.capture_epoch

            outcome = VoiceDemoOutcome(
                message=message,
                pipeline_result=pipeline,
                tts_terminal_result=terminal,
            )
        except Exception:
            failed = True
        if failed or outcome is None:
            raise VoiceDemoError()
        return outcome

    def close(self) -> SpeechControlResult:
        """Idempotently close the server-owned voice session."""
        if self._closed and self._close_result is not None:
            return self._close_result
        failed = False
        close_result = None
        try:
            close_result = self.coordinator.close_session(
                self.binding.speech_session_id,
                _safe_server_id('voice-close', self._id_factory),
            )
            if (
                type(close_result) is not SpeechControlResult
                or close_result.status != 'closed'
            ):
                failed = True
        except Exception:
            failed = True
        if failed or close_result is None:
            raise VoiceDemoError()
        self._closed = True
        self._close_result = close_result
        self._capture_epoch = close_result.capture_epoch
        return close_result

    def __enter__(self) -> '_LocalVoiceDemo':
        """Return this already-open local session."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        """Close the session without hiding an active body exception."""
        del traceback
        try:
            self.close()
        except VoiceDemoError:
            if exc_type is None:
                raise
        return False


class _SanitizedArgumentParser(argparse.ArgumentParser):
    """Suppress caller-provided values in command-line parse errors."""

    def error(self, message: str) -> None:
        """Exit with a fixed diagnostic that cannot echo private input."""
        del message
        self.exit(2, '{}: error: invalid arguments\n'.format(self.prog))


def _parse_seconds(value: str) -> int:
    """Parse an exact integer microphone duration from one through 30."""
    failed = False
    try:
        result = int(value, 10)
    except (TypeError, ValueError):
        failed = True
        result = 0
    if failed or not 1 <= result <= 30:
        raise argparse.ArgumentTypeError('invalid duration')
    return result


def _parse_label(value: str, *, maximum: int) -> str:
    """Parse one bounded, non-path, printable configuration label."""
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or not value.isascii()
        or any(
            not (character.isalnum() or character in {'-', '_', '.'})
            for character in value
        )
    ):
        raise argparse.ArgumentTypeError('invalid label')
    return value


def _parse_language(value: str) -> str:
    """Parse a short language tag before loading a model or recording."""
    return _parse_label(value, maximum=16)


def _parse_agent_model(value: str) -> str:
    """Parse one model ID under the existing Settings character policy."""
    return _parse_label(value, maximum=MAX_ARGUMENT_LENGTH)


def _parse_audio_device(value: str) -> str:
    """Parse one bounded ALSA device passed as a single argv element."""
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MAX_ARGUMENT_LENGTH
        or _contains_control_character(value)
    ):
        raise argparse.ArgumentTypeError('invalid audio device')
    return value.strip()


def _parse_env_file(value: str) -> str:
    """Parse a bounded local env path without ever echoing its value."""
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_ENV_PATH_LENGTH
        or _contains_control_character(value)
    ):
        raise argparse.ArgumentTypeError('invalid env file')
    return value


def _argument_parser() -> argparse.ArgumentParser:
    """Create the explicit-consent one-shot microphone parser."""
    parser = _SanitizedArgumentParser(
        prog='malbut-voice-demo',
        description=(
            'Capture one local utterance and run the safe agent pipeline.'
        ),
    )
    parser.add_argument(
        '--microphone',
        action='store_true',
        required=True,
        help='explicitly consent to one temporary microphone capture',
    )
    parser.add_argument(
        '--env-file',
        type=_parse_env_file,
        default=None,
        help='explicit owner-only mode-0600 env file for OpenAI mode',
    )
    parser.add_argument(
        '--provider',
        choices=('mock', 'openai'),
        default='mock',
    )
    parser.add_argument('--agent-model', type=_parse_agent_model)
    parser.add_argument(
        '--seconds',
        type=_parse_seconds,
        default=DEFAULT_CAPTURE_SECONDS,
    )
    parser.add_argument(
        '--language',
        type=_parse_language,
        default=DEFAULT_LANGUAGE,
    )
    parser.add_argument(
        '--stt-model',
        choices=('tiny', 'base', 'small'),
        default=DEFAULT_STT_MODEL,
    )
    parser.add_argument(
        '--stt-device',
        choices=('auto', 'cpu', 'cuda'),
        default=DEFAULT_STT_DEVICE,
    )
    parser.add_argument(
        '--stt-compute-type',
        choices=('default', 'int8', 'float16', 'int8_float16'),
        default=DEFAULT_STT_COMPUTE_TYPE,
    )
    parser.add_argument('--audio-device', type=_parse_audio_device)
    parser.add_argument(
        '--allow-model-download',
        action='store_true',
        help='explicitly allow a missing STT model to be downloaded',
    )
    return parser


def _parse_env_content(content: str) -> dict:
    """Parse bounded KEY=VALUE text without exposing names or values."""
    result = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if (
            not key
            or len(key) > 128
            or not key.isascii()
            or not key.replace('_', '').isalnum()
        ):
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            value = value[1:-1]
        result[key] = value
    return result


def _read_secure_env_file(path_value: str) -> dict:
    """Read one owner-only regular env file through a stable descriptor."""
    failed = False
    file_descriptor = -1
    content = None
    try:
        env_path = Path(path_value).expanduser()
        path_status = os.lstat(env_path)
        flags = os.O_RDONLY
        for option in ('O_CLOEXEC', 'O_NOFOLLOW', 'O_NONBLOCK'):
            flags |= getattr(os, option, 0)
        file_descriptor = os.open(str(env_path), flags)
        opened_status = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(path_status.st_mode)
            or not stat.S_ISREG(opened_status.st_mode)
            or path_status.st_dev != opened_status.st_dev
            or path_status.st_ino != opened_status.st_ino
            or opened_status.st_uid != os.geteuid()
            or stat.S_IMODE(opened_status.st_mode) != 0o600
            or opened_status.st_size > MAX_ENV_FILE_BYTES
        ):
            failed = True
        chunks = []
        byte_count = 0
        while not failed:
            chunk = os.read(file_descriptor, 8192)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count > MAX_ENV_FILE_BYTES:
                failed = True
                break
            chunks.append(chunk)
        final_status = os.fstat(file_descriptor)
        if (
            final_status.st_dev != opened_status.st_dev
            or final_status.st_ino != opened_status.st_ino
            or final_status.st_size != opened_status.st_size
            or final_status.st_mtime_ns != opened_status.st_mtime_ns
            or final_status.st_ctime_ns != opened_status.st_ctime_ns
        ):
            failed = True
        if not failed:
            content = b''.join(chunks).decode('utf-8')
    except Exception:
        failed = True
    finally:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                failed = True
    if failed or type(content) is not str:
        raise VoiceDemoEnvironmentError()
    return _parse_env_content(content)


def _demo_settings(
    arguments: argparse.Namespace,
    environ: Mapping[str, str],
) -> Settings:
    """Use only an approved file, then force non-actuating demo bounds."""
    del environ
    if arguments.provider == 'mock':
        settings = Settings(provider='mock')
    else:
        if type(arguments.env_file) is not str:
            raise VoiceDemoEnvironmentError()
        file_environment = _read_secure_env_file(arguments.env_file)
        api_key = file_environment.get('OPENAI_API_KEY')
        if (
            type(api_key) is not str
            or not 1 <= len(api_key) <= MAX_OPENAI_KEY_LENGTH
            or not api_key.isascii()
            or any(
                ord(character) < 33 or ord(character) > 126
                for character in api_key
            )
        ):
            raise VoiceDemoEnvironmentError()
        environment = {
            key: value
            for key, value in file_environment.items()
            if key in OPENAI_ENV_KEYS
        }
        environment['MALBUT_AGENT_PROVIDER'] = 'openai'
        settings = Settings.from_env(environment)
    overrides = {
        'provider': arguments.provider,
        'host': '127.0.0.1',
        'database_path': ':memory:',
        'user_id': VOICE_USER_ID,
        'tool_mode': 'proposal',
        'auth_token': 'local-voice-demo',
        'openai_fallback_model': '',
        'provider_max_retries': 0,
        'request_timeout_seconds': OPENAI_REQUEST_TIMEOUT_SECONDS,
        'provider_total_timeout_seconds': OPENAI_TOTAL_TIMEOUT_SECONDS,
        'openai_reasoning_effort': 'none',
        'openai_max_output_tokens': 500,
    }
    if arguments.agent_model is not None:
        overrides['openai_model'] = arguments.agent_model
    settings = replace(settings, **overrides)
    settings.validate_for_server()
    return settings


def _prepare_stt_backend(backend: Any) -> None:
    """Finish any local model setup before microphone capture starts."""
    lookup_failed = False
    try:
        prepare = getattr(backend, 'prepare', None)
    except Exception:
        lookup_failed = True
        prepare = None
    if lookup_failed or not callable(prepare):
        raise BackendUnavailableError()
    failed = False
    try:
        prepare()
    except Exception:
        failed = True
    if failed:
        raise BackendUnavailableError()


def _require_direct_openai_connection() -> None:
    """Fail closed when urllib would inherit any proxy configuration."""
    failed = False
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        failed = True
        proxies = None
    if failed or type(proxies) is not dict:
        raise VoiceDemoError()
    for key, value in proxies.items():
        if type(key) is not str or type(value) is not str:
            raise VoiceDemoError()
        if key.lower() in {'http', 'https', 'all'} and value.strip():
            raise VoiceDemoError()


def _build_stt_backend(
    builder: Callable[..., Any],
    arguments: argparse.Namespace,
) -> Any:
    """Construct an STT backend behind a fresh sanitized error boundary."""
    failed = False
    try:
        backend = builder(
            model=arguments.stt_model,
            device=arguments.stt_device,
            compute_type=arguments.stt_compute_type,
            allow_model_download=arguments.allow_model_download,
        )
    except Exception:
        failed = True
        backend = None
    if failed or backend is None:
        raise BackendUnavailableError()
    return backend


def _close_runtime(
    demo: Optional[_LocalVoiceDemo],
    orchestrator: Optional[AgentOrchestrator],
) -> bool:
    """Best-effort close every local store and report complete cleanup."""
    succeeded = True
    if demo is not None:
        try:
            demo.close()
        except BaseException:
            succeeded = False
    if orchestrator is not None:
        lookup_failed = False
        try:
            stores = (
                orchestrator.conversation_store,
                orchestrator.memory_store,
            )
        except BaseException:
            lookup_failed = True
            stores = ()
        if lookup_failed:
            succeeded = False
        for store in stores:
            try:
                store.close()
            except BaseException:
                succeeded = False
    return succeeded


def _openai_outcome_is_approved(
    outcome: VoiceDemoOutcome,
    expected_model: str,
) -> bool:
    """Require a real primary OpenAI response with complete token usage."""
    if type(outcome) is not VoiceDemoOutcome:
        return False
    pipeline = outcome.pipeline_result
    agent_result = pipeline.agent_result
    if (
        type(agent_result) is not OrchestrationResult
        or agent_result.state_trusted is not False
        or type(agent_result.provider_result) is not ProviderResult
    ):
        return False
    provider_result = agent_result.provider_result
    if (
        type(provider_result.provider) is not str
        or provider_result.provider != 'openai'
    ):
        return False
    model = provider_result.model
    if (
        type(model) is not str
        or type(expected_model) is not str
        or not model
        or model == 'safe-non-action'
        or len(model) > MAX_ARGUMENT_LENGTH
        or not model.isascii()
        or _contains_control_character(model)
        or not (
            model == expected_model
            or model.startswith(expected_model + '-')
        )
    ):
        return False
    usage = provider_result.usage
    if type(usage) is not ProviderUsage:
        return False
    counters = (
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
    )
    if any(type(value) is not int or value <= 0 for value in counters):
        return False
    return (
        usage.total_tokens > 0
        and usage.total_tokens
        >= usage.input_tokens + usage.output_tokens
    )


def _pipeline_retry_message(
    outcome: VoiceDemoOutcome,
) -> Optional[str]:
    """Map only approved retry states to content-free diagnostics."""
    state = (
        outcome.pipeline_result.status,
        outcome.pipeline_result.code,
    )
    if state == ('rejected', 'low_confidence'):
        return LOW_CONFIDENCE_MESSAGE
    if state in RETRYABLE_PIPELINE_RESULTS:
        return RETRYABLE_PIPELINE_MESSAGE
    return None


def _emit_stdout(message: str) -> bool:
    """Write one safe final answer without leaking output exceptions."""
    failed = False
    try:
        sys.stdout.write(message + '\n')
        sys.stdout.flush()
    except Exception:
        failed = True
    return not failed


def _emit_stderr(message: str) -> None:
    """Best-effort write one fixed content-free diagnostic."""
    try:
        sys.stderr.write(message + '\n')
        sys.stderr.flush()
    except Exception:
        pass


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    stt_backend_factory: Optional[Callable[..., Any]] = None,
    orchestrator_factory: Optional[
        Callable[[Settings], AgentOrchestrator]
    ] = None,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], Optional[str]] = shutil.which,
    id_factory: Optional[Callable[[], str]] = None,
    clock_ns: Optional[Callable[[], int]] = None,
) -> int:
    """Capture once and print only the safety-filtered agent message."""
    arguments = _argument_parser().parse_args(argv)
    source_environment = os.environ if environ is None else environ
    backend_builder = (
        FasterWhisperBackend
        if stt_backend_factory is None
        else stt_backend_factory
    )
    runtime_builder = (
        build_orchestrator
        if orchestrator_factory is None
        else orchestrator_factory
    )

    orchestrator = None
    demo = None
    answer = None
    retry_message = None
    failure_kind = None
    failure_exit_code = VOICE_DEMO_EXIT_CODE
    capture_capability = object()
    try:
        try:
            settings = _demo_settings(arguments, source_environment)
            if settings.provider == 'openai':
                _require_direct_openai_connection()
            orchestrator = runtime_builder(settings)
            demo = _LocalVoiceDemo(
                orchestrator,
                capture_capability=capture_capability,
                id_factory=id_factory,
                clock_ns=clock_ns,
            )
            backend = _build_stt_backend(backend_builder, arguments)
            _prepare_stt_backend(backend)
            with capture_microphone(
                seconds=arguments.seconds,
                audio_device=arguments.audio_device,
                runner=runner,
                which=which,
            ) as wav_path:
                stt_result = transcribe_wav(
                    wav_path,
                    backend,
                    language=arguments.language,
                )
            outcome = demo._process_trusted_microphone_result(
                stt_result,
                capture_capability=capture_capability,
            )
            if outcome.message is None:
                retry_message = _pipeline_retry_message(outcome)
                if retry_message is None:
                    raise VoiceDemoError()
                failure_kind = 'retry'
                failure_exit_code = VOICE_DEMO_RETRY_EXIT_CODE
            else:
                if (
                    settings.provider == 'openai'
                    and not _openai_outcome_is_approved(
                        outcome,
                        settings.openai_model,
                    )
                ):
                    raise VoiceDemoError()
                answer = outcome.message
        except LocalSTTError as error:
            failure_kind = 'stt'
            failure_exit_code = STT_EXIT_CODES.get(
                type(error),
                VOICE_DEMO_EXIT_CODE,
            )
        except KeyboardInterrupt:
            failure_kind = 'interrupt'
            failure_exit_code = 130
        except Exception:
            failure_kind = 'demo'
    finally:
        cleanup_succeeded = _close_runtime(demo, orchestrator)

    if not cleanup_succeeded:
        failure_kind = 'demo'
        failure_exit_code = VOICE_DEMO_EXIT_CODE

    if failure_kind == 'interrupt':
        return failure_exit_code
    if failure_kind == 'stt':
        _emit_stderr('error: local speech recognition failed')
        return failure_exit_code
    if failure_kind == 'retry' and retry_message is not None:
        _emit_stderr(retry_message)
        return failure_exit_code
    if failure_kind is not None or type(answer) is not str:
        _emit_stderr('error: local voice demo failed')
        return VOICE_DEMO_EXIT_CODE

    if not _emit_stdout(answer):
        _emit_stderr('error: local voice demo failed')
        return VOICE_DEMO_EXIT_CODE
    return 0


__all__ = [
    'VOICE_DEMO_MINIMUM_CONFIDENCE',
    'VoiceDemoError',
    'VoiceDemoEnvironmentError',
    'VoiceDemoOutcome',
    'main',
]


if __name__ == '__main__':
    raise SystemExit(main())
