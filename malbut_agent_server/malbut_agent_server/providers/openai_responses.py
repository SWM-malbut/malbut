"""OpenAI Responses API adapter using only the Python standard library."""

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

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
    MAX_CONVERSATION_TURNS,
    MAX_MODEL_INPUT_CHARS,
    SYSTEM_INSTRUCTIONS,
    PreparedModelInput,
    prepare_model_input,
)
from malbut_agent_server.providers.base import AgentProvider, ProviderError
from malbut_agent_server.schemas import (
    MAX_UTTERANCE_LENGTH,
    AgentDecision,
    AgentRequest,
    ProviderResult,
    ProviderUsage,
)
from malbut_agent_server.tools import ToolSpec


Transport = Callable[
    [str, Dict[str, str], Dict[str, Any], int],
    Dict[str, Any],
]
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024
REASONING_EFFORTS = frozenset(
    {'none', 'low', 'medium', 'high', 'xhigh', 'max'}
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent Authorization headers from following redirects."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        """Return no follow-up request for every redirect status."""
        del request, file_pointer, code, message, headers, new_url
        return None


TEXT_DECISION_SCHEMA: Dict[str, Any] = {
    'type': 'object',
    'properties': {
        'type': {
            'type': 'string',
            'enum': ['message', 'clarification', 'refusal'],
            'description': (
                'Use message for ordinary conversation or safe answers; '
                'clarification only when required information is missing; '
                'refusal only when safety, authorization, or privacy policy '
                'requires rejecting the request.'
            ),
        },
        'message': {
            'type': 'string',
            'minLength': 1,
            'maxLength': MAX_UTTERANCE_LENGTH,
            'description': 'A concise user-facing Korean response.',
        },
        'reason': {
            'type': 'string',
            'description': (
                'A short policy or intent label, never hidden reasoning.'
            ),
        },
        'confidence': {
            'type': ['number', 'null'],
            'description': (
                'Calibrated confidence from 0 to 1, or null if unknown.'
            ),
        },
    },
    'required': ['type', 'message', 'reason', 'confidence'],
    'additionalProperties': False,
}


class OpenAIResponsesProvider(AgentProvider):
    """Provider that maps Responses API output into an AgentDecision."""

    name = 'openai'

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
    ) -> None:
        """Initialize a lazy adapter without performing a network call."""
        if not api_key or not api_key.strip():
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
        self._api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = base_url.strip().rstrip('/')
        self.timeout_seconds = timeout_seconds
        self.max_model_input_chars = max_model_input_chars
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = normalized_effort
        self._validate_base_url()
        self.transport = transport or self._urllib_transport

    def __repr__(self) -> str:
        """Return diagnostics without exposing the API credential."""
        return (
            'OpenAIResponsesProvider('
            f'model={self.model!r}, '
            f'base_url={self.base_url!r}, '
            f'timeout_seconds={self.timeout_seconds!r}, '
            f'reasoning_effort={self.reasoning_effort!r}, '
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
    ) -> ProviderResult:
        """Call the API once and normalize either a tool call or text."""
        prepared = prepare_model_input(
            request,
            memories,
            conversation_turns,
            conversation_summary,
            self.max_model_input_chars,
            MAX_CONVERSATION_TURNS,
        )
        payload = self.build_payload(
            request,
            memories,
            conversation_turns,
            tools,
            conversation_summary,
            prepared=prepared,
        )
        headers = {
            'Authorization': f'Bearer {self._api_key}',
            'Content-Type': 'application/json',
            'User-Agent': 'malbut-agent-server/0.4',
            'X-Client-Request-Id': self._client_request_id(
                request.request_id
            ),
        }
        started = time.perf_counter()
        response = self.transport(
            f'{self.base_url}/responses',
            headers,
            payload,
            self.timeout_seconds,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        decision = self._parse_decision(response)
        try:
            decision.validate()
        except (ValueError, TypeError) as error:
            raise ProviderError(
                'OpenAI returned an invalid normalized decision'
            ) from error
        return ProviderResult(
            decision=decision,
            provider=self.name,
            model=str(response.get('model') or self.model),
            latency_ms=latency_ms,
            usage=self._parse_usage(response.get('usage')),
            response_id=(
                str(response['id'])
                if isinstance(response.get('id'), str)
                else None
            ),
            input_chars=prepared.metrics.model_input_chars,
            context_metrics=prepared.metrics,
        )

    def build_payload(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
        prepared: Optional[PreparedModelInput] = None,
    ) -> Dict[str, Any]:
        """Build the documented Responses API request body."""
        prepared_context = prepared or prepare_model_input(
            request,
            memories,
            conversation_turns,
            conversation_summary,
            self.max_model_input_chars,
            MAX_CONVERSATION_TURNS,
        )
        payload: Dict[str, Any] = {
            'model': self.model,
            'instructions': SYSTEM_INSTRUCTIONS,
            'input': prepared_context.text,
            'parallel_tool_calls': False,
            'tool_choice': 'auto',
            'store': False,
            'max_output_tokens': self.max_output_tokens,
            'reasoning': {
                'effort': self.reasoning_effort,
                'context': 'current_turn',
            },
            'safety_identifier': self._safety_identifier(
                request.user_id
            ),
            'text': {
                'format': {
                    'type': 'json_schema',
                    'name': 'malbut_text_decision',
                    'strict': True,
                    'schema': TEXT_DECISION_SCHEMA,
                },
            },
        }
        if tools:
            payload['tools'] = [
                tool.to_openai_dict()
                for tool in tools
            ]
        else:
            payload.pop('tool_choice')
        return payload

    @staticmethod
    def _safety_identifier(user_id: str) -> str:
        digest = hashlib.sha256(user_id.encode('utf-8')).hexdigest()
        return f'malbut-{digest[:32]}'

    def _client_request_id(self, request_id: str) -> str:
        """Create a trace identifier without sending the local ID."""
        source = f'{self.model}\0{request_id}'.encode('utf-8')
        return 'malbut-' + hashlib.sha256(source).hexdigest()[:32]

    @staticmethod
    def _parse_decision(response: Dict[str, Any]) -> AgentDecision:
        if not isinstance(response, dict):
            raise ProviderError('provider response must be an object')
        status = response.get('status')
        if status != 'completed':
            raise ProviderError(
                'provider response was not completed'
            )
        output = response.get('output')
        if not isinstance(output, list):
            raise ProviderError('provider response output must be a list')

        function_calls = [
            item
            for item in output
            if isinstance(item, dict)
            and item.get('type') == 'function_call'
        ]
        if len(function_calls) > 1:
            raise ProviderError(
                'provider returned multiple function calls'
            )
        text_parts: List[str] = []
        refusal_parts: List[str] = []
        for item in output:
            if (
                not isinstance(item, dict)
                or item.get('type') != 'message'
            ):
                continue
            content = item.get('content')
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if (
                    part.get('type') == 'output_text'
                    and isinstance(part.get('text'), str)
                ):
                    text_parts.append(part['text'])
                elif (
                    part.get('type') == 'refusal'
                    and isinstance(part.get('refusal'), str)
                ):
                    refusal_parts.append(part['refusal'])
        if function_calls and (text_parts or refusal_parts):
            raise ProviderError(
                'provider mixed an action with terminal text'
            )
        if function_calls:
            item = function_calls[0]
            name = item.get('name')
            raw_arguments = item.get('arguments')
            if not isinstance(name, str) or not name:
                raise ProviderError('function call name is invalid')
            if not isinstance(raw_arguments, str):
                raise ProviderError(
                    'function call arguments must be JSON text'
                )
            try:
                arguments = json.loads(
                    raw_arguments,
                    parse_constant=(
                        OpenAIResponsesProvider._reject_json_constant
                    ),
                )
            except (json.JSONDecodeError, ValueError) as error:
                raise ProviderError(
                    'function call arguments are invalid JSON'
                ) from error
            if not isinstance(arguments, dict):
                raise ProviderError(
                    'function call arguments must decode to an object'
                )
            return AgentDecision(
                type='tool_call',
                message=f'{name} 작업을 안전 계층에 요청할게.',
                tool_name=name,
                arguments=arguments,
                reason='model_tool_call',
                confidence=None,
                expires_in_ms=5000,
            )
        if refusal_parts:
            return AgentDecision(
                type='refusal',
                message=' '.join(refusal_parts).strip(),
                reason='provider_refusal',
                confidence=None,
            )
        if not text_parts:
            raise ProviderError(
                'provider returned neither a tool call nor text'
            )
        raw_text = ''.join(text_parts)
        try:
            parsed = json.loads(
                raw_text,
                parse_constant=(
                    OpenAIResponsesProvider._reject_json_constant
                ),
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise ProviderError(
                'structured text decision is invalid JSON'
            ) from error
        if not isinstance(parsed, dict):
            raise ProviderError(
                'structured text decision must be an object'
            )
        allowed = {'type', 'message', 'reason', 'confidence'}
        if set(parsed) != allowed:
            raise ProviderError(
                'structured text decision fields do not match the schema'
            )
        return AgentDecision(
            type=parsed.get('type'),
            message=parsed.get('message'),
            reason=parsed.get('reason'),
            confidence=parsed.get('confidence'),
        )

    @staticmethod
    def _parse_usage(value: Any) -> ProviderUsage:
        if not isinstance(value, dict):
            return ProviderUsage()

        def optional_int(name: str) -> Optional[int]:
            raw = value.get(name)
            if isinstance(raw, int) and not isinstance(raw, bool):
                return raw
            return None

        return ProviderUsage(
            input_tokens=optional_int('input_tokens'),
            output_tokens=optional_int('output_tokens'),
            total_tokens=optional_int('total_tokens'),
        )

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(
            f'non-finite provider JSON number is invalid: {value}'
        )

    @staticmethod
    def _urllib_transport(
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode('utf-8')
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method='POST',
        )
        try:
            opener = urllib.request.build_opener(
                _NoRedirectHandler()
            )
            with opener.open(
                request,
                timeout=timeout_seconds,
            ) as response:
                response_body = response.read(
                    MAX_PROVIDER_RESPONSE_BYTES + 1
                )
        except urllib.error.HTTPError as error:
            raise ProviderError(
                'OpenAI request failed with HTTP status '
                f'{error.code}'
            ) from error
        except urllib.error.URLError as error:
            raise ProviderError(
                'OpenAI request failed due to a network error'
            ) from error
        if len(response_body) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ProviderError(
                'OpenAI response exceeded the size limit'
            )
        try:
            decoded = json.loads(
                response_body.decode('utf-8'),
                parse_constant=(
                    OpenAIResponsesProvider._reject_json_constant
                ),
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            raise ProviderError(
                'OpenAI response was not valid JSON'
            ) from error
        if not isinstance(decoded, dict):
            raise ProviderError('OpenAI response must be an object')
        return decoded
