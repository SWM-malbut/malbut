"""Fail-closed subprocess client and AgentProvider for the RAI sidecar."""

from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Any, Callable, List, Mapping, Optional, Sequence

from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.prompting import (
    MAX_CONVERSATION_TURNS,
    MAX_MODEL_INPUT_CHARS,
    SYSTEM_INSTRUCTIONS,
    prepare_model_input,
)
from malbut_agent_server.providers.base import AgentProvider, ProviderError
from malbut_agent_server.rai_sidecar_protocol import (
    ActionProposal,
    MAX_MODEL_INPUT_LENGTH,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ProposalRequest,
    ProposalResponse,
    RaiSidecarProtocolError,
    RuntimeErrorResponse,
    TextReply,
    decode_response,
    encode_request,
    project_tool_specs,
)
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    ProviderUsage,
    ValidationError,
)
from malbut_agent_server.tools import ToolSpec, validate_tool_arguments


SidecarTransport = Callable[[bytes, float], bytes]

SIDECAR_ENVIRONMENT_ALLOWLIST = frozenset({
    'LANG',
    'LC_ALL',
    'OPENAI_API_KEY',
    'MALBUT_RAI_MODEL',
    'SSL_CERT_DIR',
    'SSL_CERT_FILE',
})

_FORCED_ENVIRONMENT = {
    'LANG': 'C.UTF-8',
    'LC_ALL': 'C.UTF-8',
    # rai-core imports helpers that resolve optional binaries.  Give them a
    # fixed system-only search path instead of inheriting the server PATH.
    'PATH': '/usr/bin:/bin',
    'PYTHONHASHSEED': '0',
    'PYTHONIOENCODING': 'utf-8',
    'PYTHONUTF8': '1',
    'LANGCHAIN_TRACING_V2': 'false',
    'LANGFUSE_TRACING': 'false',
    'LANGSMITH_TRACING': 'false',
}


class RaiSidecarError(ProviderError):
    """Base content-free provider error for one sidecar attempt."""

    code = 'sidecar_error'

    def __init__(self) -> None:
        """Avoid retaining exception text, request data, or credentials."""
        super().__init__(f'RAI sidecar failed closed: {self.code}')


class RaiSidecarLaunchError(RaiSidecarError):
    """The fixed sidecar process could not be started."""

    code = 'sidecar_launch_failed'


class RaiSidecarTimeoutError(RaiSidecarError):
    """The one permitted sidecar attempt exceeded its deadline."""

    code = 'sidecar_timeout'


class RaiSidecarCrashError(RaiSidecarError):
    """The sidecar terminated without one successful response."""

    code = 'sidecar_crash'


class RaiSidecarOutputLimitError(RaiSidecarError):
    """The sidecar wrote more bytes than the protocol permits."""

    code = 'sidecar_output_too_large'


class RaiSidecarMalformedResponseError(RaiSidecarError):
    """The sidecar returned partial or invalid protocol data."""

    code = 'sidecar_malformed_response'


class RaiSidecarRequestError(RaiSidecarError):
    """The local request could not be represented by the protocol."""

    code = 'sidecar_invalid_request'


class RaiSidecarRuntimeError(RaiSidecarError):
    """The isolated runtime returned a documented content-free error."""

    code = 'sidecar_runtime_error'

    def __init__(self, runtime_code: str) -> None:
        """Retain only the protocol's bounded error code."""
        self.runtime_code = runtime_code
        super().__init__()


class SubprocessRaiSidecarTransport:
    """Start one fixed, isolated subprocess for exactly one request."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        environment: Optional[Mapping[str, str]] = None,
        working_directory: str | None = None,
    ) -> None:
        """Freeze executable arguments, cwd, and a minimal environment."""
        if type(argv) not in {list, tuple} or not argv:
            raise ValueError('sidecar argv must be a non-empty sequence')
        normalized_argv = tuple(
            self._configuration_text(item, maximum=8192)
            for item in argv
        )
        executable = Path(normalized_argv[0])
        if not executable.is_absolute() or not executable.is_file() or (
            not os.access(executable, os.X_OK)
        ):
            raise ValueError(
                'sidecar executable must be an absolute executable file'
            )
        self.argv = normalized_argv

        supplied = dict(environment or {})
        if not set(supplied).issubset(SIDECAR_ENVIRONMENT_ALLOWLIST):
            raise ValueError('sidecar environment contains unsupported names')
        normalized_environment = {
            name: self._configuration_text(value, maximum=16384)
            for name, value in supplied.items()
        }
        normalized_environment.update(_FORCED_ENVIRONMENT)
        self.environment = normalized_environment

        if working_directory is None:
            self.working_directory = None
        else:
            directory = Path(working_directory)
            if not directory.is_absolute() or not directory.is_dir():
                raise ValueError(
                    'sidecar working directory must be an absolute directory'
                )
            self.working_directory = str(directory.resolve())

    def __repr__(self) -> str:
        """Expose no argv, path, environment value, or credential."""
        return 'SubprocessRaiSidecarTransport(<redacted>)'

    @staticmethod
    def _configuration_text(value: Any, *, maximum: int) -> str:
        if type(value) is not str or not value or len(value) > maximum:
            raise ValueError('sidecar configuration text is invalid')
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        ):
            raise ValueError('sidecar configuration text is invalid')
        return value

    def __call__(self, payload: bytes, timeout_seconds: float) -> bytes:
        """Exchange one bounded envelope without retries or shell parsing."""
        if type(payload) is not bytes or not payload or (
            len(payload) > MAX_REQUEST_BYTES
        ):
            raise RaiSidecarRequestError()
        if type(timeout_seconds) not in {int, float} or not (
            0.05 <= float(timeout_seconds) <= 120.0
        ):
            raise ValueError('sidecar timeout is outside the supported range')
        try:
            process = subprocess.Popen(
                self.argv,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.working_directory,
                env=dict(self.environment),
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise RaiSidecarLaunchError() from None
        try:
            return self._bounded_exchange(
                process,
                payload,
                float(timeout_seconds),
            )
        except BaseException:
            self._stop(process)
            raise
        finally:
            self._close_pipe(process.stdin)
            self._close_pipe(process.stdout)

    def _bounded_exchange(
        self,
        process: subprocess.Popen,
        payload: bytes,
        timeout_seconds: float,
    ) -> bytes:
        if process.stdin is None or process.stdout is None:
            self._stop(process)
            raise RaiSidecarLaunchError()
        stdin_fd = process.stdin.fileno()
        stdout_fd = process.stdout.fileno()
        os.set_blocking(stdin_fd, False)
        os.set_blocking(stdout_fd, False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        pending = memoryview(payload)
        output = bytearray()
        deadline = time.monotonic() + timeout_seconds
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop(process)
                    raise RaiSidecarTimeoutError()
                events = selector.select(min(remaining, 0.05))
                for key, _mask in events:
                    if key.data == 'stdin':
                        try:
                            written = os.write(stdin_fd, pending)
                        except (BrokenPipeError, OSError):
                            written = 0
                            pending = pending[len(pending):]
                        else:
                            pending = pending[written:]
                        if not pending:
                            selector.unregister(process.stdin)
                            self._close_pipe(process.stdin)
                    else:
                        try:
                            chunk = os.read(stdout_fd, 8192)
                        except BlockingIOError:
                            continue
                        except OSError:
                            chunk = b''
                        if not chunk:
                            selector.unregister(process.stdout)
                            self._close_pipe(process.stdout)
                            continue
                        output.extend(chunk)
                        if len(output) > MAX_RESPONSE_BYTES:
                            self._stop(process)
                            raise RaiSidecarOutputLimitError()
                if process.poll() is not None:
                    stdin_key = self._selector_key(
                        selector,
                        process.stdin,
                    )
                    if stdin_key is not None:
                        selector.unregister(process.stdin)
                        self._close_pipe(process.stdin)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop(process)
                raise RaiSidecarTimeoutError()
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._stop(process)
                raise RaiSidecarTimeoutError() from None
        finally:
            selector.close()
        if return_code != 0:
            raise RaiSidecarCrashError()
        return bytes(output)

    @staticmethod
    def _selector_key(
        selector: selectors.BaseSelector,
        stream: Any,
    ) -> selectors.SelectorKey | None:
        try:
            return selector.get_key(stream)
        except (KeyError, OSError, ValueError):
            return None

    @staticmethod
    def _close_pipe(stream: Any) -> None:
        if stream is None or stream.closed:
            return
        try:
            stream.close()
        except OSError:
            pass

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


class RaiSidecarClient:
    """Encode, exchange, and validate one sidecar request."""

    def __init__(
        self,
        transport: SidecarTransport,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        """Inject one transport with one bounded attempt per call."""
        if not callable(transport):
            raise TypeError('transport must be callable')
        if type(timeout_seconds) not in {int, float} or not (
            0.05 <= float(timeout_seconds) <= 120.0
        ):
            raise ValueError('timeout_seconds is outside the supported range')
        self._transport = transport
        self.timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        """Avoid exposing a transport that may contain credentials."""
        return (
            'RaiSidecarClient('
            f'timeout_seconds={self.timeout_seconds!r}, '
            'transport=<redacted>)'
        )

    def propose(self, request: ProposalRequest) -> ProposalResponse:
        """Perform exactly one request and fail closed on every ambiguity."""
        try:
            encoded = encode_request(request)
        except RaiSidecarProtocolError:
            raise RaiSidecarRequestError() from None
        try:
            raw = self._transport(encoded, self.timeout_seconds)
        except RaiSidecarError:
            raise
        except TimeoutError:
            raise RaiSidecarTimeoutError() from None
        except Exception:
            raise RaiSidecarCrashError() from None
        try:
            response = decode_response(raw)
        except RaiSidecarProtocolError:
            raise RaiSidecarMalformedResponseError() from None
        if type(response) is RuntimeErrorResponse:
            raise RaiSidecarRuntimeError(response.code)
        if type(response) is not ProposalResponse or (
            response.request_id != request.request_id
        ):
            raise RaiSidecarMalformedResponseError()
        return response


class RaiSidecarProvider(AgentProvider):
    """Adapt one isolated RAI proposal into the existing Provider contract."""

    name = 'rai-sidecar'

    def __init__(
        self,
        client: RaiSidecarClient,
        *,
        max_model_input_chars: int = MAX_MODEL_INPUT_CHARS,
    ) -> None:
        """Bind a client without importing or initializing rai-core."""
        if type(client) is not RaiSidecarClient:
            raise TypeError('client must be a RaiSidecarClient')
        if type(max_model_input_chars) is not int or not (
            4096 <= max_model_input_chars <= MAX_MODEL_INPUT_LENGTH
        ):
            raise ValueError(
                'max_model_input_chars must fit the sidecar protocol'
            )
        self.client = client
        self.max_model_input_chars = max_model_input_chars

    def __repr__(self) -> str:
        """Return content-free provider configuration."""
        return (
            'RaiSidecarProvider('
            f'max_model_input_chars={self.max_model_input_chars!r}, '
            'client=<redacted>)'
        )

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        """Request one proposal, revalidate it locally, and never retry."""
        prepared = prepare_model_input(
            request,
            memories,
            conversation_turns,
            conversation_summary,
            self.max_model_input_chars,
            MAX_CONVERSATION_TURNS,
        )
        try:
            proposal_request = ProposalRequest(
                request_id=request.request_id,
                instructions=SYSTEM_INSTRUCTIONS,
                model_input=prepared.text,
                tools=project_tool_specs(tools),
            )
        except (RaiSidecarProtocolError, TypeError, ValueError):
            raise RaiSidecarRequestError() from None
        started = time.perf_counter()
        response = self.client.propose(proposal_request)
        latency_ms = (time.perf_counter() - started) * 1000.0
        decision = self._decision(response.output, tools)
        return ProviderResult(
            decision=decision,
            provider=self.name,
            model=response.model,
            latency_ms=latency_ms,
            usage=ProviderUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.total_tokens,
            ),
            response_id=response.response_id,
            input_chars=prepared.metrics.model_input_chars,
            context_metrics=prepared.metrics,
        )

    @staticmethod
    def _decision(
        output: TextReply | ActionProposal,
        tools: Sequence[ToolSpec],
    ) -> AgentDecision:
        if type(output) is TextReply:
            decision = AgentDecision(
                type=output.response_type,
                message=output.message,
                reason=output.reason,
                confidence=output.confidence,
            )
        elif type(output) is ActionProposal:
            allowed_names = {tool.name for tool in tools}
            if output.tool_name not in allowed_names:
                raise RaiSidecarMalformedResponseError()
            try:
                arguments = validate_tool_arguments(
                    output.tool_name,
                    output.arguments_dict(),
                )
            except (TypeError, ValidationError):
                raise RaiSidecarMalformedResponseError() from None
            decision = AgentDecision(
                type='tool_call',
                message=output.message,
                tool_name=output.tool_name,
                arguments=arguments,
                reason=output.reason,
                confidence=output.confidence,
                expires_in_ms=output.expires_in_ms,
            )
        else:
            raise RaiSidecarMalformedResponseError()
        try:
            decision.validate()
        except (TypeError, ValidationError, ValueError):
            raise RaiSidecarMalformedResponseError() from None
        return decision
