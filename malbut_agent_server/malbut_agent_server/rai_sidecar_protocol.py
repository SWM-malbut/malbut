"""Strict, versioned JSON protocol for the isolated RAI sidecar."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence, Union

from malbut_agent_server.tools import ToolSpec


SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_IDENTIFIER_LENGTH = 256
MAX_INSTRUCTIONS_LENGTH = 16 * 1024
MAX_MODEL_INPUT_LENGTH = 64 * 1024
MAX_MESSAGE_LENGTH = 4000
MAX_REASON_LENGTH = 1000
MAX_TOOL_DESCRIPTION_LENGTH = 4000
MAX_ARGUMENT_BYTES = 16 * 1024
MAX_TOOLS = 32

TEXT_REPLY_TYPES = frozenset({'message', 'clarification', 'refusal'})
RUNTIME_ERROR_CODES = frozenset({
    'invalid_request',
    'runtime_unavailable',
    'runtime_failed',
    'invalid_runtime_output',
})


class RaiSidecarProtocolError(ValueError):
    """Reject malformed protocol data without echoing its contents."""

    def __init__(self, code: str) -> None:
        """Create one content-free protocol failure."""
        self.code = code
        super().__init__(f'RAI sidecar protocol error: {code}')


def _fail(code: str) -> None:
    raise RaiSidecarProtocolError(code)


def _strict_object(
    value: Any,
    expected_fields: Iterable[str],
    code: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        _fail(code)
    expected = frozenset(expected_fields)
    if frozenset(value) != expected:
        _fail(code)
    return value


def _bounded_text(
    value: Any,
    *,
    maximum: int,
    code: str,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        _fail(code)
    if (not value and not allow_empty) or len(value) > maximum:
        _fail(code)
    if any(
        ord(character) < 32 and character not in '\n\t\r'
        for character in value
    ) or any(
        ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        _fail(code)
    return value


def _identifier(value: Any, code: str) -> str:
    return _bounded_text(
        value,
        maximum=MAX_IDENTIFIER_LENGTH,
        code=code,
    )


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float}:
        _fail('invalid_confidence')
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        _fail('invalid_confidence')
    return result


def _optional_count(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        _fail('invalid_usage')
    return value


def _reject_duplicate_fields(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail('duplicate_field')
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    _fail('non_finite_number')


def _decode_json(raw: Any, *, maximum: int, size_code: str) -> Any:
    if type(raw) is not bytes:
        _fail('invalid_encoding')
    if not raw or len(raw) > maximum:
        _fail(size_code)
    try:
        text = raw.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        _fail('invalid_encoding')
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_fields,
            parse_constant=_reject_constant,
        )
    except RaiSidecarProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError):
        _fail('invalid_json')


def _encode_json(value: Any, *, maximum: int, size_code: str) -> bytes:
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    except (TypeError, UnicodeEncodeError, ValueError, RecursionError):
        _fail('invalid_json_value')
    if not result or len(result) > maximum:
        _fail(size_code)
    return result


def _validate_property_schema(value: Any) -> dict[str, Any]:
    schema = _strict_object(
        value,
        {'type', 'description'},
        'invalid_tool_schema',
    )
    raw_type = schema['type']
    if type(raw_type) is str:
        types = (raw_type,)
    elif type(raw_type) is list and raw_type:
        if any(type(item) is not str for item in raw_type):
            _fail('invalid_tool_schema')
        types = tuple(raw_type)
    else:
        _fail('invalid_tool_schema')
    if len(set(types)) != len(types) or not set(types).issubset(
        {'string', 'null'}
    ) or 'string' not in types:
        _fail('invalid_tool_schema')
    _bounded_text(
        schema['description'],
        maximum=MAX_TOOL_DESCRIPTION_LENGTH,
        code='invalid_tool_schema',
        allow_empty=True,
    )
    return copy.deepcopy(schema)


def _validate_parameters(value: Any) -> dict[str, Any]:
    parameters = _strict_object(
        value,
        {'type', 'properties', 'required', 'additionalProperties'},
        'invalid_tool_schema',
    )
    if parameters['type'] != 'object':
        _fail('invalid_tool_schema')
    if parameters['additionalProperties'] is not False:
        _fail('invalid_tool_schema')
    properties = parameters['properties']
    if type(properties) is not dict or len(properties) > 32:
        _fail('invalid_tool_schema')
    normalized_properties: dict[str, Any] = {}
    for name, schema in properties.items():
        normalized_name = _identifier(name, 'invalid_tool_schema')
        normalized_properties[normalized_name] = (
            _validate_property_schema(schema)
        )
    required = parameters['required']
    if type(required) is not list or any(
        type(item) is not str for item in required
    ):
        _fail('invalid_tool_schema')
    if len(required) != len(set(required)) or not set(required).issubset(
        normalized_properties
    ):
        _fail('invalid_tool_schema')
    return {
        'type': 'object',
        'properties': normalized_properties,
        'required': list(required),
        'additionalProperties': False,
    }


def project_tool_spec(tool: ToolSpec) -> dict[str, Any]:
    """Project one ToolSpec without provider- or executor-specific fields."""
    if type(tool) is not ToolSpec:
        raise TypeError('tool must be a ToolSpec')
    return _validate_tool_projection({
        'name': tool.name,
        'description': tool.description,
        'parameters': copy.deepcopy(tool.parameters),
    })


def project_tool_specs(
    tools: Sequence[ToolSpec],
) -> tuple[dict[str, Any], ...]:
    """Return an ordered, duplicate-free neutral ToolSpec projection."""
    if len(tools) > MAX_TOOLS:
        _fail('too_many_tools')
    result = tuple(project_tool_spec(tool) for tool in tools)
    names = [tool['name'] for tool in result]
    if len(names) != len(set(names)):
        _fail('duplicate_tool')
    return result


def _validate_tool_projection(value: Any) -> dict[str, Any]:
    tool = _strict_object(
        value,
        {'name', 'description', 'parameters'},
        'invalid_tool',
    )
    return {
        'name': _identifier(tool['name'], 'invalid_tool'),
        'description': _bounded_text(
            tool['description'],
            maximum=MAX_TOOL_DESCRIPTION_LENGTH,
            code='invalid_tool',
            allow_empty=True,
        ),
        'parameters': _validate_parameters(tool['parameters']),
    }


def _validate_tools(value: Any) -> tuple[dict[str, Any], ...]:
    if type(value) is not list or len(value) > MAX_TOOLS:
        _fail('invalid_tools')
    tools = tuple(_validate_tool_projection(item) for item in value)
    names = [tool['name'] for tool in tools]
    if len(names) != len(set(names)):
        _fail('duplicate_tool')
    return tools


def _canonical_arguments(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        _fail('invalid_arguments')
    encoded = _encode_json(
        value,
        maximum=MAX_ARGUMENT_BYTES,
        size_code='arguments_too_large',
    )
    decoded = _decode_json(
        encoded,
        maximum=MAX_ARGUMENT_BYTES,
        size_code='arguments_too_large',
    )
    if type(decoded) is not dict:
        _fail('invalid_arguments')
    return decoded


@dataclass(frozen=True)
class ProposalRequest:
    """One bounded single-turn request sent to the sidecar."""

    request_id: str
    instructions: str
    model_input: str
    tools: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        """Normalize all fields before the request crosses the process edge."""
        object.__setattr__(
            self,
            'request_id',
            _identifier(self.request_id, 'invalid_request_id'),
        )
        object.__setattr__(
            self,
            'instructions',
            _bounded_text(
                self.instructions,
                maximum=MAX_INSTRUCTIONS_LENGTH,
                code='invalid_instructions',
            ),
        )
        object.__setattr__(
            self,
            'model_input',
            _bounded_text(
                self.model_input,
                maximum=MAX_MODEL_INPUT_LENGTH,
                code='invalid_model_input',
            ),
        )
        object.__setattr__(
            self,
            'tools',
            _validate_tools(list(self.tools)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a detached strict request envelope."""
        return {
            'schema_version': SCHEMA_VERSION,
            'kind': 'proposal_request',
            'request_id': self.request_id,
            'instructions': self.instructions,
            'model_input': self.model_input,
            'tools': copy.deepcopy(list(self.tools)),
        }


@dataclass(frozen=True)
class TextReply:
    """Exactly one non-action response from the sidecar."""

    response_type: str
    message: str
    reason: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        """Validate a bounded user-facing reply without hidden reasoning."""
        if self.response_type not in TEXT_REPLY_TYPES:
            _fail('invalid_text_reply')
        object.__setattr__(
            self,
            'message',
            _bounded_text(
                self.message,
                maximum=MAX_MESSAGE_LENGTH,
                code='invalid_text_reply',
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            'reason',
            _bounded_text(
                self.reason,
                maximum=MAX_REASON_LENGTH,
                code='invalid_text_reply',
                allow_empty=True,
            ),
        )
        object.__setattr__(self, 'confidence', _confidence(self.confidence))

    def to_dict(self) -> dict[str, Any]:
        """Return the strict text output object."""
        return {
            'kind': 'text_reply',
            'response_type': self.response_type,
            'message': self.message,
            'reason': self.reason,
            'confidence': self.confidence,
        }


@dataclass(frozen=True)
class ActionProposal:
    """Exactly one high-level, non-authorizing Tool proposal."""

    tool_name: str
    arguments: Mapping[str, Any]
    message: str
    reason: str
    confidence: float | None = None
    expires_in_ms: int = 5000

    def __post_init__(self) -> None:
        """Validate shape and JSON safety before local policy revalidation."""
        object.__setattr__(
            self,
            'tool_name',
            _identifier(self.tool_name, 'invalid_action_proposal'),
        )
        object.__setattr__(
            self,
            'arguments',
            _canonical_arguments(self.arguments),
        )
        for name in ('message', 'reason'):
            maximum = (
                MAX_MESSAGE_LENGTH if name == 'message' else MAX_REASON_LENGTH
            )
            object.__setattr__(
                self,
                name,
                _bounded_text(
                    getattr(self, name),
                    maximum=maximum,
                    code='invalid_action_proposal',
                    allow_empty=True,
                ),
            )
        object.__setattr__(self, 'confidence', _confidence(self.confidence))
        if type(self.expires_in_ms) is not int or not (
            1 <= self.expires_in_ms <= 60000
        ):
            _fail('invalid_action_proposal')

    def arguments_dict(self) -> dict[str, Any]:
        """Return detached proposal arguments."""
        return copy.deepcopy(dict(self.arguments))

    def to_dict(self) -> dict[str, Any]:
        """Return the strict proposal output object."""
        return {
            'kind': 'action_proposal',
            'tool_name': self.tool_name,
            'arguments': self.arguments_dict(),
            'message': self.message,
            'reason': self.reason,
            'confidence': self.confidence,
            'expires_in_ms': self.expires_in_ms,
        }


SidecarOutput = Union[TextReply, ActionProposal]


@dataclass(frozen=True)
class SidecarUsage:
    """Optional content-free token counters from the isolated runtime."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        """Reject invalid or inconsistent usage metadata."""
        for name in ('input_tokens', 'output_tokens', 'total_tokens'):
            object.__setattr__(
                self,
                name,
                _optional_count(getattr(self, name)),
            )
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens < self.input_tokens + self.output_tokens
        ):
            _fail('invalid_usage')

    def to_dict(self) -> dict[str, Any]:
        """Return strict token usage fields."""
        return {
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'total_tokens': self.total_tokens,
        }


@dataclass(frozen=True)
class ProposalResponse:
    """One successful response containing exactly one terminal output."""

    request_id: str
    model: str
    output: SidecarOutput
    response_id: str | None = None
    usage: SidecarUsage = SidecarUsage()

    def __post_init__(self) -> None:
        """Validate response identity and exact output type."""
        object.__setattr__(
            self,
            'request_id',
            _identifier(self.request_id, 'invalid_request_id'),
        )
        object.__setattr__(
            self,
            'model',
            _identifier(self.model, 'invalid_model'),
        )
        if type(self.output) not in {TextReply, ActionProposal}:
            _fail('invalid_output')
        if self.response_id is not None:
            object.__setattr__(
                self,
                'response_id',
                _identifier(self.response_id, 'invalid_response_id'),
            )
        if type(self.usage) is not SidecarUsage:
            _fail('invalid_usage')

    def to_dict(self) -> dict[str, Any]:
        """Return one strict success envelope."""
        return {
            'schema_version': SCHEMA_VERSION,
            'kind': 'proposal_response',
            'request_id': self.request_id,
            'model': self.model,
            'response_id': self.response_id,
            'usage': self.usage.to_dict(),
            'output': self.output.to_dict(),
        }


@dataclass(frozen=True)
class RuntimeErrorResponse:
    """Content-free sidecar failure returned instead of partial output."""

    code: str

    def __post_init__(self) -> None:
        """Limit runtime failures to a documented, non-sensitive set."""
        if self.code not in RUNTIME_ERROR_CODES:
            _fail('invalid_runtime_error')

    def to_dict(self) -> dict[str, Any]:
        """Return the strict error envelope."""
        return {
            'schema_version': SCHEMA_VERSION,
            'kind': 'error_response',
            'code': self.code,
        }


SidecarResponse = Union[ProposalResponse, RuntimeErrorResponse]


def encode_request(request: ProposalRequest) -> bytes:
    """Encode one request with the protocol byte limit."""
    if type(request) is not ProposalRequest:
        raise TypeError('request must be a ProposalRequest')
    return _encode_json(
        request.to_dict(),
        maximum=MAX_REQUEST_BYTES,
        size_code='request_too_large',
    )


def decode_request(raw: bytes) -> ProposalRequest:
    """Decode one exact version-1 request and reject unknown fields."""
    value = _decode_json(
        raw,
        maximum=MAX_REQUEST_BYTES,
        size_code='request_too_large',
    )
    envelope = _strict_object(
        value,
        {
            'schema_version',
            'kind',
            'request_id',
            'instructions',
            'model_input',
            'tools',
        },
        'invalid_request_envelope',
    )
    if envelope['schema_version'] != SCHEMA_VERSION or (
        envelope['kind'] != 'proposal_request'
    ):
        _fail('unsupported_request_envelope')
    return ProposalRequest(
        request_id=envelope['request_id'],
        instructions=envelope['instructions'],
        model_input=envelope['model_input'],
        tools=_validate_tools(envelope['tools']),
    )


def _decode_output(value: Any) -> SidecarOutput:
    if type(value) is not dict:
        _fail('invalid_output')
    kind = value.get('kind')
    if kind == 'text_reply':
        output = _strict_object(
            value,
            {'kind', 'response_type', 'message', 'reason', 'confidence'},
            'invalid_text_reply',
        )
        return TextReply(
            response_type=output['response_type'],
            message=output['message'],
            reason=output['reason'],
            confidence=output['confidence'],
        )
    if kind == 'action_proposal':
        output = _strict_object(
            value,
            {
                'kind',
                'tool_name',
                'arguments',
                'message',
                'reason',
                'confidence',
                'expires_in_ms',
            },
            'invalid_action_proposal',
        )
        return ActionProposal(
            tool_name=output['tool_name'],
            arguments=output['arguments'],
            message=output['message'],
            reason=output['reason'],
            confidence=output['confidence'],
            expires_in_ms=output['expires_in_ms'],
        )
    _fail('invalid_output')


def encode_response(response: SidecarResponse) -> bytes:
    """Encode one success or content-free error response."""
    if type(response) not in {ProposalResponse, RuntimeErrorResponse}:
        raise TypeError('response has an unsupported type')
    return _encode_json(
        response.to_dict(),
        maximum=MAX_RESPONSE_BYTES,
        size_code='response_too_large',
    )


def decode_response(raw: bytes) -> SidecarResponse:
    """Decode one exact response; mixed or multiple outputs are invalid."""
    value = _decode_json(
        raw,
        maximum=MAX_RESPONSE_BYTES,
        size_code='response_too_large',
    )
    if type(value) is not dict:
        _fail('invalid_response_envelope')
    kind = value.get('kind')
    if kind == 'error_response':
        envelope = _strict_object(
            value,
            {'schema_version', 'kind', 'code'},
            'invalid_response_envelope',
        )
        if envelope['schema_version'] != SCHEMA_VERSION:
            _fail('unsupported_response_envelope')
        return RuntimeErrorResponse(code=envelope['code'])
    if kind != 'proposal_response':
        _fail('invalid_response_envelope')
    envelope = _strict_object(
        value,
        {
            'schema_version',
            'kind',
            'request_id',
            'model',
            'response_id',
            'usage',
            'output',
        },
        'invalid_response_envelope',
    )
    if envelope['schema_version'] != SCHEMA_VERSION:
        _fail('unsupported_response_envelope')
    usage_value = _strict_object(
        envelope['usage'],
        {'input_tokens', 'output_tokens', 'total_tokens'},
        'invalid_usage',
    )
    return ProposalResponse(
        request_id=envelope['request_id'],
        model=envelope['model'],
        response_id=envelope['response_id'],
        usage=SidecarUsage(
            input_tokens=usage_value['input_tokens'],
            output_tokens=usage_value['output_tokens'],
            total_tokens=usage_value['total_tokens'],
        ),
        output=_decode_output(envelope['output']),
    )
