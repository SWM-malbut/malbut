"""Contract tests for the versioned RAI sidecar protocol."""

import json

import pytest

from malbut_agent_server.rai_sidecar_protocol import (
    ActionProposal,
    MAX_REQUEST_BYTES,
    ProposalRequest,
    ProposalResponse,
    RaiSidecarProtocolError,
    RuntimeErrorResponse,
    SidecarUsage,
    TextReply,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    project_tool_specs,
)
from malbut_agent_server.tools import TOOL_SPECS


SENSITIVE = 'private-user-text-SWM25-131'


def _request() -> ProposalRequest:
    return ProposalRequest(
        request_id='request-1',
        instructions='Return one proposal.',
        model_input='거실로 이동해 줘',
        tools=project_tool_specs([TOOL_SPECS['navigate']]),
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8')


def test_request_round_trip_uses_neutral_tool_projection() -> None:
    request = _request()

    decoded = decode_request(encode_request(request))

    assert decoded == request
    tool = decoded.tools[0]
    assert set(tool) == {'name', 'description', 'parameters'}
    assert tool['name'] == 'navigate'
    assert 'strict' not in tool
    assert 'function' not in tool
    assert 'executor' not in tool


@pytest.mark.parametrize(
    'output',
    [
        TextReply(
            response_type='message',
            message='알겠습니다.',
            reason='direct_reply',
            confidence=0.9,
        ),
        ActionProposal(
            tool_name='navigate',
            arguments={'location': '거실'},
            message='거실로 이동할까요?',
            reason='named_destination',
            confidence=0.8,
            expires_in_ms=5000,
        ),
    ],
)
def test_success_response_has_exactly_one_typed_output(output: object) -> None:
    response = ProposalResponse(
        request_id='request-1',
        model='test-model',
        response_id='response-1',
        usage=SidecarUsage(4, 5, 9),
        output=output,
    )

    decoded = decode_response(encode_response(response))

    assert decoded == response


def test_content_free_runtime_error_round_trip() -> None:
    response = RuntimeErrorResponse('runtime_failed')

    assert decode_response(encode_response(response)) == response


@pytest.mark.parametrize(
    'mutation',
    [
        lambda value: value.update({'unknown': True}),
        lambda value: value.update({'schema_version': 2}),
        lambda value: value.update({'kind': 'other'}),
        lambda value: value.update({'tools': {}}),
    ],
)
def test_request_rejects_unknown_or_unsupported_envelope(
    mutation,
) -> None:
    value = _request().to_dict()
    mutation(value)

    with pytest.raises(RaiSidecarProtocolError):
        decode_request(_json_bytes(value))


def test_request_rejects_duplicate_fields_invalid_utf8_and_size() -> None:
    with pytest.raises(RaiSidecarProtocolError):
        decode_request(
            b'{"schema_version":1,"schema_version":1}'
        )
    with pytest.raises(RaiSidecarProtocolError):
        decode_request(b'\xff')
    with pytest.raises(RaiSidecarProtocolError):
        decode_request(encode_request(_request()).replace(
            '거실'.encode('utf-8'),
            b'\\ud800',
        ))
    with pytest.raises(RaiSidecarProtocolError):
        decode_request(b'{' + (b'x' * MAX_REQUEST_BYTES) + b'}')


def test_non_finite_numbers_are_rejected() -> None:
    raw = encode_response(
        ProposalResponse(
            request_id='request-1',
            model='test-model',
            output=TextReply('message', '', '', None),
        )
    ).replace(b'"confidence":null', b'"confidence":NaN')

    with pytest.raises(RaiSidecarProtocolError):
        decode_response(raw)


@pytest.mark.parametrize(
    'invalid_output',
    [
        [],
        [
            {
                'kind': 'text_reply',
                'response_type': 'message',
                'message': 'one',
                'reason': '',
                'confidence': None,
            },
            {
                'kind': 'text_reply',
                'response_type': 'message',
                'message': 'two',
                'reason': '',
                'confidence': None,
            },
        ],
        {
            'kind': 'action_proposal',
            'tool_name': 'navigate',
            'arguments': {'location': '거실'},
            'message': '',
            'reason': '',
            'confidence': None,
            'expires_in_ms': 5000,
            'approved': True,
        },
    ],
)
def test_response_rejects_multiple_or_authoritative_output_fields(
    invalid_output,
) -> None:
    value = ProposalResponse(
        request_id='request-1',
        model='test-model',
        output=TextReply('message', '', '', None),
    ).to_dict()
    value['output'] = invalid_output

    with pytest.raises(RaiSidecarProtocolError):
        decode_response(_json_bytes(value))


def test_protocol_exceptions_never_echo_rejected_content() -> None:
    value = _request().to_dict()
    value[SENSITIVE] = SENSITIVE

    with pytest.raises(RaiSidecarProtocolError) as raised:
        decode_request(_json_bytes(value))

    assert SENSITIVE not in str(raised.value)
    assert SENSITIVE not in repr(raised.value)
