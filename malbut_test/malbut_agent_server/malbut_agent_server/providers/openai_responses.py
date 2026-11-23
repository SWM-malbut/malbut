"""OpenAI Responses API adapter using only the Python standard library."""

from collections import OrderedDict
import hashlib
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import weakref

from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.endpoint_policy import (
    OFFICIAL_OPENAI_BASE_URL,
    is_official_openai_base_url,
)
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.prompting import (
    MAX_MODEL_INPUT_CHARS,
    PreparedModelInput,
)
from malbut_agent_server.providers.base import AgentProvider, ProviderError
from malbut_agent_server.providers.http_pool import (
    HTTPConnectionPool,
    uses_environment_proxy,
)
from malbut_agent_server.providers.openai import (
    MAX_PROVIDER_RESPONSE_BYTES,
    PayloadConfig,
    REASONING_EFFORTS,
    SPEECH_END_INSTRUCTIONS,
    TEXT_DECISION_SCHEMA,
    Transport,
    _FAST_SPEECH_COMPLEX_PATTERN,
    _NoRedirectHandler,
    _TWO_PASS_FOLLOWUP_DIRECTIVE,
    _TWO_PASS_PREFIX_CACHE_MAX,
    _TWO_PASS_READONLY_TOOLS,
    _TwoPassPrefix,
    _insufficient_quota,
    _is_fast_speech_call1,
    decode_response_body,
    extract_two_pass_prefix,
    parse_output,
    parse_usage,
    pooled_post,
    prepare_payload,
    preserve_conversation,
    reject_json_constant,
    resolve_turn_image,
    safety_identifier,
    urllib_transport,
)
from malbut_agent_server.providers.reliable import (
    NormalizedProviderError,
    ProviderFailureCode,
    classify_exception,
)
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderAPITiming,
    ProviderResult,
    ProviderUsage,
)
from malbut_agent_server.service_keys import key_value
from malbut_agent_server.tools import ToolSpec

__all__ = [
    'MAX_PROVIDER_RESPONSE_BYTES',
    'OpenAIResponsesProvider',
    'PooledOpenAITransport',
    'REASONING_EFFORTS',
    'SPEECH_END_INSTRUCTIONS',
    'TEXT_DECISION_SCHEMA',
    'Transport',
    '_DEFAULT_URLLIB_TRANSPORT',
    '_FAST_SPEECH_COMPLEX_PATTERN',
    '_NoRedirectHandler',
    '_TWO_PASS_FOLLOWUP_DIRECTIVE',
    '_TWO_PASS_PREFIX_CACHE_MAX',
    '_TWO_PASS_READONLY_TOOLS',
    '_TwoPassPrefix',
    '_insufficient_quota',
    '_is_fast_speech_call1',
]


class PooledOpenAITransport:
    """Keep-Alive pooled JSON transport for auxiliary official OpenAI clients."""

    def __init__(self, *, max_connections: int = 2) -> None:
        self._http_pool = HTTPConnectionPool(max_connections=max_connections)
        self._finalizer = weakref.finalize(self, self._http_pool.close)

    @property
    def pool(self) -> HTTPConnectionPool:
        return self._http_pool

    def close(self) -> None:
        """Close idle connections and reject subsequent requests."""
        self._finalizer()

    def __call__(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        return pooled_post(
            self._http_pool,
            url,
            headers,
            payload,
            timeout_seconds,
            max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
            fallback_transport=OpenAIResponsesProvider._urllib_transport,
            decode_body=OpenAIResponsesProvider._decode_response_body,
            force_fallback=(
                OpenAIResponsesProvider._urllib_transport is not _DEFAULT_URLLIB_TRANSPORT
            ),
        )


class OpenAIResponsesProvider(AgentProvider):
    """Provider that maps Responses API output into an AgentDecision."""

    name = 'openai'
    supports_memory = True

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = OFFICIAL_OPENAI_BASE_URL,
        timeout_seconds: int = 30,
        transport: Optional[Transport] = None,
        max_model_input_chars: int = MAX_MODEL_INPUT_CHARS,
        max_output_tokens: int = 500,
        reasoning_effort: str = 'none',
        include_reasoning: bool = True,
        semantic_context: bool = False,
        max_input_tokens: int = 32768,
        token_counter: Optional[Callable[[Any], int]] = None,
        api_timing_enabled: bool = False,
        prompt_cache_enabled: bool = False,
        *,
        is_fallback: bool = False,
        fast_turn_reasoning_effort: str = '',
    ) -> None:
        """Initialize a lazy adapter without performing a network call.

        ``api_key`` is a string, or a service_keys.ManagedKey: then the key is
        read on every request, so a key the owner sets on the web is used
        without restarting, and how it did is reported back.
        """
        if isinstance(api_key, str):
            if not api_key.strip():
                raise ValueError('api_key must not be empty')
            api_key = api_key.strip()
        elif not callable(getattr(api_key, 'current', None)):
            raise ValueError('api_key must not be empty')
        if not model or not model.strip():
            raise ValueError('model must not be empty')
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds < 1
            or timeout_seconds > 120
        ):
            raise ValueError('timeout_seconds must be between 1 and 120')
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens < 64
            or max_output_tokens > 4096
        ):
            raise ValueError('max_output_tokens must be between 64 and 4096')
        normalized_effort = reasoning_effort.strip().lower()
        if normalized_effort not in REASONING_EFFORTS:
            raise ValueError('reasoning_effort is unsupported')
        normalized_fast_effort = fast_turn_reasoning_effort.strip().lower()
        if normalized_fast_effort and normalized_fast_effort not in REASONING_EFFORTS:
            raise ValueError('fast_turn_reasoning_effort is unsupported')
        if type(include_reasoning) is not bool:
            raise ValueError('include_reasoning must be a boolean')
        if type(semantic_context) is not bool:
            raise ValueError('semantic_context must be a boolean')
        if type(max_input_tokens) is not int or max_input_tokens < 1:
            raise ValueError('max_input_tokens must be a positive integer')
        if token_counter is not None and not callable(token_counter):
            raise ValueError('token_counter must be callable')
        if type(api_timing_enabled) is not bool or type(prompt_cache_enabled) is not bool:
            raise ValueError('API timing and prompt cache options must be boolean')
        self._key_source = api_key
        self.model = model.strip()
        self.base_url = base_url.strip().rstrip('/')
        self.timeout_seconds = timeout_seconds
        self.max_model_input_chars = max_model_input_chars
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = normalized_effort
        self.fast_turn_reasoning_effort = normalized_fast_effort
        self.include_reasoning = include_reasoning
        self.semantic_context = semantic_context
        self.is_fallback = is_fallback
        self.max_input_tokens = max_input_tokens
        self.token_counter = token_counter
        self.api_timing_enabled = api_timing_enabled
        self.prompt_cache_enabled = prompt_cache_enabled
        self._validate_base_url()
        self._http_pool = HTTPConnectionPool() if transport is None else None
        self._transport_finalizer = (
            weakref.finalize(self, self._http_pool.close)
            if self._http_pool is not None else None
        )
        self.transport: Transport = transport if transport is not None else self._pooled_transport
        self._prefix_lock = threading.Lock()
        self._two_pass_prefixes: 'OrderedDict[tuple[str, str], _TwoPassPrefix]' = OrderedDict()

    def _payload_config(self) -> PayloadConfig:
        return PayloadConfig(
            model=self.model,
            max_model_input_chars=self.max_model_input_chars,
            max_output_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort,
            fast_turn_reasoning_effort=self.fast_turn_reasoning_effort,
            include_reasoning=self.include_reasoning,
            semantic_context=self.semantic_context,
            is_fallback=self.is_fallback,
            max_input_tokens=self.max_input_tokens,
            token_counter=self.token_counter,
            prompt_cache_enabled=self.prompt_cache_enabled,
        )

    def _record_two_pass_prefix(
        self,
        user_id: str,
        request_id: str,
        payload: Dict[str, Any],
        *,
        include_weather: bool,
        has_homecam_rules: bool,
        has_patrol_rules: bool,
    ) -> None:
        """Cache content-free Call-1 static prefix for an immediate Call-2 hit."""
        prefix = extract_two_pass_prefix(
            payload,
            model=self.model,
            include_weather=include_weather,
            has_homecam_rules=has_homecam_rules,
            has_patrol_rules=has_patrol_rules,
        )
        if prefix is None:
            return
        key = (user_id, request_id)
        with self._prefix_lock:
            self._two_pass_prefixes[key] = prefix
            self._two_pass_prefixes.move_to_end(key)
            while len(self._two_pass_prefixes) > _TWO_PASS_PREFIX_CACHE_MAX:
                self._two_pass_prefixes.popitem(last=False)

    def _pop_two_pass_prefix(
        self,
        user_id: str,
        request_id: str,
    ) -> Optional[_TwoPassPrefix]:
        with self._prefix_lock:
            prefix = self._two_pass_prefixes.pop((user_id, request_id), None)
        if prefix is None or prefix.model != self.model:
            return None
        return prefix

    def close(self) -> None:
        """Release owned idle connections; injected transports stay caller-owned."""
        if self._transport_finalizer is not None:
            self._transport_finalizer()

    @property
    def _api_key(self) -> str:
        """The key to send now ('' when there is none)."""
        return key_value(self._key_source)

    def _report_key(self, state: str, code: Optional[str] = None) -> None:
        report = getattr(self._key_source, 'report', None)
        if report is not None:
            report(state, code)

    def __repr__(self) -> str:
        """Return diagnostics without exposing the API credential."""
        return (
            'OpenAIResponsesProvider('
            f'model={self.model!r}, '
            f'base_url={self.base_url!r}, '
            f'timeout_seconds={self.timeout_seconds!r}, '
            f'reasoning_effort={self.reasoning_effort!r}, '
            f'include_reasoning={self.include_reasoning!r}, '
            'api_key=<redacted>)'
        )

    def _validate_base_url(self) -> None:
        if not is_official_openai_base_url(self.base_url):
            raise ValueError(
                'OpenAI credentials may use only the official API origin'
            )

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
        *,
        memory_context: Optional[dict] = None,
        weather_context: Optional[dict] = None,
    ) -> ProviderResult:
        """Call the API once and normalize either a tool call or text."""
        prepare_started = time.perf_counter()
        api_key = self._api_key
        if not api_key:
            self._report_key('missing', 'missing_api_key')
            raise NormalizedProviderError(
                ProviderFailureCode.MISSING_CREDENTIALS
            )
        memory_mode = memory_context.get('mode') if memory_context else None
        two_pass_prefix = None
        if (not tools and not request.available_tools
                and memory_mode == 'answer_only'
                and self.prompt_cache_enabled
                and self._preserve_conversation(memory_mode)):
            two_pass_prefix = self._pop_two_pass_prefix(
                request.user_id, request.request_id,
            )
        prepared, payload, forecast_selection = self._prepare_payload(
            request, memories, conversation_turns, tools, conversation_summary,
            memory_context=memory_context, weather_context=weather_context,
            two_pass_prefix=two_pass_prefix,
        )
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'User-Agent': 'malbut-agent-server/0.4',
            'X-Client-Request-Id': self._client_request_id(request.request_id),
        }
        from malbut_agent_server.memory_trace import model_input_ready
        model_input_ready(prepared.text, provider='openai', model=self.model)
        from malbut_agent_server.speech_prefetch import current_prefetch

        prefetch = current_prefetch(request.request_id)
        if prefetch is not None and not prefetch.begin_attempt():
            prefetch = None
        started = time.perf_counter()
        preparation_ms = (started - prepare_started) * 1000
        events: Dict[str, Any] = {}

        def observe(name: str) -> None:
            events.setdefault(name + '_ms', (time.perf_counter() - started) * 1000)

        from malbut_agent_server.offline_fallback import current_timeout_cap

        timeout_cap = current_timeout_cap()
        effective_timeout = (
            min(float(self.timeout_seconds), timeout_cap)
            if timeout_cap is not None
            else self.timeout_seconds
        )
        try:
            url = f'{self.base_url}/responses'
            if ((prefetch is not None or self.api_timing_enabled) and self._http_pool is not None
                    and self.transport == self._pooled_transport
                    and not uses_environment_proxy(url)):
                response = self._pooled_transport(
                    url, headers, dict(payload, stream=True), effective_timeout,
                    stream_turn=prefetch,
                    observer=observe if self.api_timing_enabled else None,
                )
            else:
                response = self.transport(url, headers, payload, effective_timeout)
        except Exception as error:
            if prefetch is not None:
                prefetch.fail_attempt()
            failure = classify_exception(error).code
            if failure is ProviderFailureCode.AUTHENTICATION:
                self._report_key('invalid', 'authentication_failed')
            elif failure is ProviderFailureCode.QUOTA:
                self._report_key('quota', 'insufficient_quota')
            raise
        self._report_key('ok')
        latency_ms = (time.perf_counter() - started) * 1000
        parse_started = time.perf_counter()
        memory_enabled = (
            memory_context is not None and memory_mode != 'answer_only' and not forecast_selection
        )
        try:
            decision, memory_proposal = self._parse_output(
                response,
                memory_enabled=memory_enabled,
                automatic_extraction=memory_mode == 'automatic_extraction',
                mapping_clarification=(
                    'clarification_for' in payload['text']['format']['schema']['properties']
                ),
            )
            decision.validate()
            if payload.get('tool_choice') == 'none' and decision.type == 'tool_call':
                raise ProviderError('provider returned a tool call when tool_choice was none')
            if (forecast_selection and decision.type == 'tool_call'
                    and decision.tool_name != 'get_weather'):
                raise ProviderError('forecast selection returned an unoffered tool')
        except (ProviderError, ValueError, TypeError) as error:
            if prefetch is not None:
                prefetch.fail_attempt()
            if isinstance(error, ProviderError):
                raise
            raise ProviderError(
                'OpenAI returned an invalid normalized decision'
            ) from error
        if (not forecast_selection and decision.type == 'tool_call'
                and decision.tool_name in _TWO_PASS_READONLY_TOOLS
                and self.prompt_cache_enabled
                and self._preserve_conversation(memory_mode)):
            self._record_two_pass_prefix(
                request.user_id,
                request.request_id,
                payload,
                include_weather=(
                    not (memory_mode == 'answer_only' and not tools)
                    or weather_context is not None
                    or any(
                        tool.name in {'get_weather', 'set_weather_location'}
                        for tool in tools
                    )
                ),
                has_homecam_rules='homecam_query_result_untrusted' in (memory_context or {}),
                has_patrol_rules='patrol_observations_untrusted' in (memory_context or {}),
            )
        if prefetch is not None:
            prefetch.approve_provider(decision)
        return ProviderResult(
            decision=decision,
            provider=self.name,
            model=str(response.get('model') or self.model),
            latency_ms=latency_ms,
            usage=self._parse_usage(response.get('usage')),
            api_timing=(ProviderAPITiming(
                preparation_ms=preparation_ms,
                transport_ms=latency_ms,
                parse_ms=(time.perf_counter() - parse_started) * 1000,
                **events,
            ) if self.api_timing_enabled else None),
            response_id=(
                str(response['id'])
                if isinstance(response.get('id'), str)
                else None
            ),
            input_chars=prepared.metrics.model_input_chars,
            context_metrics=prepared.metrics,
            memory_proposal=memory_proposal,
            memory_supported=memory_enabled,
        )

    def build_payload(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
        prepared: Optional[PreparedModelInput] = None,
        *,
        memory_context: Optional[dict] = None,
        weather_context: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Build the same Responses API request used by complete()."""
        _, payload, _ = self._prepare_payload(
            request, memories, conversation_turns, tools, conversation_summary,
            prepared=prepared, memory_context=memory_context, weather_context=weather_context,
        )
        return payload

    def _prepare_payload(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary],
        *,
        prepared: Optional[PreparedModelInput] = None,
        memory_context: Optional[Dict[str, Any]] = None,
        weather_context: Optional[Dict[str, Any]] = None,
        two_pass_prefix: Optional[_TwoPassPrefix] = None,
    ) -> Tuple[PreparedModelInput, Dict[str, Any], bool]:
        return prepare_payload(
            self._payload_config(),
            request,
            memories,
            conversation_turns,
            tools,
            conversation_summary,
            prepared=prepared,
            memory_context=memory_context,
            weather_context=weather_context,
            two_pass_prefix=two_pass_prefix,
            resolve_turn_image_fn=self._resolve_turn_image,
        )

    @staticmethod
    def _resolve_turn_image(
        request: AgentRequest,
        memory_mode: Optional[str],
    ) -> AgentRequest:
        return resolve_turn_image(request, memory_mode)

    def _preserve_conversation(self, memory_mode: Optional[str]) -> bool:
        return preserve_conversation(self.semantic_context, memory_mode)

    @staticmethod
    def _safety_identifier(user_id: str) -> str:
        return safety_identifier(user_id)

    def _client_request_id(self, request_id: str) -> str:
        """Create a trace identifier without sending the local ID."""
        source = f'{self.model}\0{request_id}'.encode('utf-8')
        return 'malbut-' + hashlib.sha256(source).hexdigest()[:32]

    @staticmethod
    def _parse_decision(response: Dict[str, Any]) -> AgentDecision:
        return OpenAIResponsesProvider._parse_output(response)[0]

    @staticmethod
    def _parse_output(
        response: Dict[str, Any],
        *,
        memory_enabled: bool = False,
        automatic_extraction: bool = False,
        mapping_clarification: bool = False,
    ) -> Tuple[AgentDecision, Any]:
        return parse_output(
            response,
            memory_enabled=memory_enabled,
            automatic_extraction=automatic_extraction,
            mapping_clarification=mapping_clarification,
            reject_constant=OpenAIResponsesProvider._reject_json_constant,
        )

    @staticmethod
    def _parse_usage(value: Any) -> ProviderUsage:
        return parse_usage(value)

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        reject_json_constant(value)

    @staticmethod
    def _urllib_transport(
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        return urllib_transport(
            url,
            headers,
            payload,
            timeout_seconds,
            max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
            decode_body=OpenAIResponsesProvider._decode_response_body,
        )

    def _pooled_transport(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout_seconds: float,
        *,
        stream_turn: Any = None,
        observer: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        if self._http_pool is None:
            raise RuntimeError('HTTP connection pool is not initialized')
        return pooled_post(
            self._http_pool,
            url,
            headers,
            payload,
            timeout_seconds,
            max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
            fallback_transport=self._urllib_transport,
            decode_body=self._decode_response_body,
            stream_turn=stream_turn,
            observer=observer,
        )

    @staticmethod
    def _decode_response_body(response_body: bytes) -> Dict[str, Any]:
        return decode_response_body(
            response_body,
            max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
            reject_constant=OpenAIResponsesProvider._reject_json_constant,
        )


_DEFAULT_URLLIB_TRANSPORT = OpenAIResponsesProvider._urllib_transport
