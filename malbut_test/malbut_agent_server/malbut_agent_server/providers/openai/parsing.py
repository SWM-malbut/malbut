"""Response decoding, structured output normalization, and usage parsing."""

import json
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from malbut_agent_server.automatic_memory_extractor import validate_automatic_proposal
from malbut_agent_server.memory_contract import validate_memory_proposal
from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.providers.openai.constants import MAX_PROVIDER_RESPONSE_BYTES
from malbut_agent_server.schemas import AgentDecision, ProviderUsage


def reject_json_constant(value: str) -> None:
    """Reject non-finite JSON constants such as NaN and Infinity."""
    raise ValueError(
        f'non-finite provider JSON number is invalid: {value}'
    )


def decode_response_body(
    response_body: bytes,
    *,
    max_response_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    reject_constant: Callable[[str], None] = reject_json_constant,
) -> Dict[str, Any]:
    """Decode and size-bound a UTF-8 JSON object returned by OpenAI."""
    if len(response_body) > max_response_bytes:
        raise ProviderError(
            'OpenAI response exceeded the size limit'
        )
    try:
        decoded = json.loads(
            response_body.decode('utf-8'),
            parse_constant=reject_constant,
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


def _validate_automatic_extraction_envelope(output: List[Any]) -> None:
    if any(not isinstance(item, dict)
           or item.get('type') not in {'message', 'reasoning'}
           for item in output):
        raise ProviderError(
            'automatic extraction returned non-text output',
        )
    messages = [item for item in output if item['type'] == 'message']
    if (len(messages) != 1
            or not isinstance(messages[0].get('content'), list)
            or len(messages[0]['content']) != 1
            or not isinstance(messages[0]['content'][0], dict)
            or messages[0]['content'][0].get('type') != 'output_text'):
        raise ProviderError('automatic extraction envelope is invalid')


def _collect_output_parts(output: List[Any]) -> Tuple[List[str], List[str]]:
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
    return text_parts, refusal_parts


def _parse_function_call(
    item: Dict[str, Any],
    *,
    reject_constant: Callable[[str], None],
) -> Tuple[AgentDecision, Any]:
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
            parse_constant=reject_constant,
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
    ), None


def _parse_structured_text_decision(
    raw_text: str,
    *,
    memory_enabled: bool,
    automatic_extraction: bool,
    mapping_clarification: bool,
    reject_constant: Callable[[str], None],
) -> Tuple[AgentDecision, Any]:
    try:
        parsed = json.loads(
            raw_text,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise ProviderError(
            'structured text decision is invalid JSON'
        ) from error
    if not isinstance(parsed, dict):
        raise ProviderError(
            'structured text decision must be an object'
        )
    if automatic_extraction:
        if set(parsed) != {'memory_proposal'}:
            raise ProviderError('automatic extraction fields are invalid')
        proposal = validate_automatic_proposal(parsed['memory_proposal'])
        return AgentDecision(
            type='message', message='',
            reason='automatic_memory_extraction',
        ), proposal
    allowed = {'type', 'message', 'reason', 'confidence'}
    if mapping_clarification:
        allowed.add('clarification_for')
    if memory_enabled:
        allowed.add('memory_proposal')
    if set(parsed) != allowed:
        raise ProviderError(
            'structured text decision fields do not match the schema'
        )
    proposal = parsed.get('memory_proposal')
    if proposal is not None:
        try:
            proposal = validate_memory_proposal(proposal)
        except (ValueError, TypeError) as error:
            raise ProviderError('invalid memory proposal') from error
    return AgentDecision(
        type=cast(str, parsed.get('type')),
        message=cast(str, parsed.get('message')),
        reason=cast(str, parsed.get('reason')),
        confidence=parsed.get('confidence'),
        clarification_for=parsed.get('clarification_for'),
    ), proposal


def parse_output(
    response: Dict[str, Any],
    *,
    memory_enabled: bool = False,
    automatic_extraction: bool = False,
    mapping_clarification: bool = False,
    reject_constant: Callable[[str], None] = reject_json_constant,
) -> Tuple[AgentDecision, Any]:
    """Normalize a completed Responses API object into (AgentDecision, proposal)."""
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
    if automatic_extraction:
        _validate_automatic_extraction_envelope(output)

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
    text_parts, refusal_parts = _collect_output_parts(output)
    if function_calls and (text_parts or refusal_parts):
        raise ProviderError(
            'provider mixed an action with terminal text'
        )
    if function_calls:
        return _parse_function_call(
            function_calls[0],
            reject_constant=reject_constant,
        )
    if refusal_parts:
        return AgentDecision(
            type='refusal',
            message=' '.join(refusal_parts).strip(),
            reason='provider_refusal',
            confidence=None,
        ), None
    if not text_parts:
        raise ProviderError(
            'provider returned neither a tool call nor text'
        )
    return _parse_structured_text_decision(
        ''.join(text_parts),
        memory_enabled=memory_enabled,
        automatic_extraction=automatic_extraction,
        mapping_clarification=mapping_clarification,
        reject_constant=reject_constant,
    )


def parse_usage(value: Any) -> ProviderUsage:
    """Extract token usage counters from an OpenAI response usage object."""
    if not isinstance(value, dict):
        return ProviderUsage()

    def optional_int(name: str) -> Optional[int]:
        raw = value.get(name)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
        return None

    def detail(group: str, name: str) -> Optional[int]:
        values = value.get(group)
        raw = values.get(name) if isinstance(values, dict) else None
        return raw if type(raw) is int and raw >= 0 else None

    return ProviderUsage(
        input_tokens=optional_int('input_tokens'),
        output_tokens=optional_int('output_tokens'),
        total_tokens=optional_int('total_tokens'),
        cached_input_tokens=detail('input_tokens_details', 'cached_tokens'),
        reasoning_output_tokens=detail('output_tokens_details', 'reasoning_tokens'),
    )
