"""Memory-aware RAI contracts use real schemas without live model calls."""

import copy
import json
from types import SimpleNamespace

import pytest

from malbut_agent_server.rai_sidecar_client import (
    RaiSidecarClient,
    RaiSidecarMalformedResponseError,
    RaiSidecarProvider,
    RaiSidecarRuntimeError,
)
from malbut_agent_server.rai_sidecar_protocol import (
    ProposalRequest,
    ProposalResponse,
    RaiSidecarProtocolError,
    RuntimeErrorResponse,
    TextReply,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    project_tool_specs,
)
from malbut_agent_server.rai_sidecar_runtime import (
    _RaiStructuredProposalRuntime,
    run_once,
)
from malbut_agent_server.schemas import AgentRequest
from malbut_agent_server.tools import TOOL_SPECS


def _proposal():
    return {
        'operation': 'remember',
        'facts': [{
            'kind': 'pet',
            'subject': '반려견',
            'attribute': 'name',
            'value': '초코',
            'evidence': '우리 강아지 이름은 초코야',
        }],
        'target_ids': [],
        'query': '',
        'evidence': '우리 강아지 이름은 초코야',
    }


def _context():
    return {'enabled': True, 'candidate_memories': []}


def _request(memory_context=None):
    return ProposalRequest(
        request_id='memory-rai-1',
        instructions='Return a proposal only.',
        model_input='우리 강아지 이름은 초코야',
        tools=project_tool_specs([TOOL_SPECS['navigate']]),
        memory_context=memory_context,
    )


def _agent_request():
    return AgentRequest.from_dict({
        'request_id': 'memory-rai-1',
        'user_id': 'memory-user',
        'conversation_id': 'memory-conversation',
        'turn_id': 'memory-turn',
        'utterance': '우리 강아지 이름은 초코야',
        'robot_state': {},
        'available_tools': [],
    })


def _payload(memory_proposal=None):
    return {
        'kind': 'text_reply',
        'response_type': 'message',
        'message': '초코라는 이름이군요.',
        'reason': 'direct_fact',
        'confidence': None,
        'tool_name': None,
        'arguments': None,
        'expires_in_ms': None,
        'memory_proposal': memory_proposal,
    }


def _runtime(payload, calls):
    pydantic = pytest.importorskip('pydantic', minversion='2')

    class GraphFactory:
        @staticmethod
        def create_structured_output_runnable(**kwargs):
            calls.append(kwargs)

            def invoke(state):
                calls.append(state)
                parsed = kwargs['structured_output'].model_validate(payload)
                return {'messages': [{
                    'parsed': parsed, 'parsing_error': None,
                }]}

            return SimpleNamespace(invoke=invoke)

    return _RaiStructuredProposalRuntime(
        rai_langchain=GraphFactory,
        llm=object(),
        human_message=lambda **kwargs: SimpleNamespace(**kwargs),
        pydantic_module=pydantic,
        strict_config=pydantic.ConfigDict(extra='forbid', strict=True),
        model='fake-memory-model',
    )


def test_v1_envelopes_do_not_add_memory_fields():
    """Omitted memory context keeps the original version-one wire shape."""
    request = _request()
    assert set(request.to_dict()) == {
        'schema_version', 'kind', 'request_id', 'instructions',
        'model_input', 'tools',
    }
    response = ProposalResponse(
        request_id=request.request_id,
        model='fake-model',
        output=TextReply('message', '안녕', ''),
    )
    assert response.to_dict()['schema_version'] == 1
    assert 'memory_proposal' not in response.to_dict()['output']
    assert decode_request(encode_request(request)).memory_context is None


@pytest.mark.parametrize('proposal', [None, _proposal()])
def test_v2_request_and_response_roundtrip(proposal):
    """Version two carries an explicit nullable memory proposal."""
    request = _request(_context())
    assert request.to_dict()['schema_version'] == 2
    assert decode_request(encode_request(request)) == request
    response = ProposalResponse(
        request_id=request.request_id,
        model='fake-model',
        output=TextReply('message', '', '', memory_proposal=proposal),
        schema_version=2,
    )
    assert 'memory_proposal' in response.to_dict()['output']
    assert decode_response(encode_response(response)) == response


def test_memory_proposal_cannot_be_silently_sent_as_v1():
    """A legacy envelope must not discard a memory operation."""
    with pytest.raises(RaiSidecarProtocolError):
        ProposalResponse(
            request_id='memory-rai-1',
            model='fake-model',
            output=TextReply('message', '', '', memory_proposal=_proposal()),
        )


@pytest.mark.parametrize('context', [None, [], 'enabled'])
def test_v2_requires_an_object_memory_context(context):
    """A version marker without valid memory context is not support."""
    value = _request(_context()).to_dict()
    value['memory_context'] = context
    with pytest.raises(RaiSidecarProtocolError):
        decode_request(json.dumps(value).encode())


@pytest.mark.parametrize('output_kind', ['text_reply', 'action_proposal'])
def test_client_rejects_v1_success_for_a_memory_aware_request(output_kind):
    """An old sidecar cannot turn absent memory support into success."""
    calls = []
    response = ProposalResponse(
        request_id='memory-rai-1', model='old-model',
        output=TextReply('message', '저장했어요.', ''),
    ).to_dict()
    if output_kind == 'action_proposal':
        response['output'] = {
            'kind': 'action_proposal', 'tool_name': 'navigate',
            'arguments': {'location': '거실'}, 'message': '', 'reason': '',
            'confidence': None, 'expires_in_ms': 5000,
        }

    def transport(payload, _timeout):
        calls.append(decode_request(payload))
        return json.dumps(response).encode()

    with pytest.raises(RaiSidecarMalformedResponseError):
        RaiSidecarClient(transport).propose(_request(_context()))
    assert len(calls) == 1


def test_v2_truncated_response_is_not_retried_or_accepted():
    """A partial proposal never becomes a partially successful operation."""
    calls = []

    def transport(_payload, _timeout):
        calls.append(True)
        return b'{"schema_version":2,"kind":"proposal_response"'

    with pytest.raises(RaiSidecarMalformedResponseError):
        RaiSidecarClient(transport).propose(_request(_context()))
    assert calls == [True]


def test_provider_runtime_v2_roundtrip_uses_actual_pydantic(monkeypatch):
    """The production adapter model validates a fake graph response."""
    calls = []
    runtime = _runtime(_payload(_proposal()), calls)
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.create_runtime',
        lambda _factory: runtime,
    )
    requests = []

    def transport(payload, _timeout):
        requests.append(decode_request(payload))
        return run_once(payload)

    provider = RaiSidecarProvider(RaiSidecarClient(transport))
    result = provider.complete(
        _agent_request(), [], [], [], memory_context=_context(),
    )
    assert result.memory_supported is True
    assert result.memory_proposal == _proposal()
    assert result.decision.type == 'message'
    assert result.decision.tool_name is None
    assert requests[0].memory_context == _context()
    model_input = json.loads(requests[0].model_input.split('\n', 1)[1])
    assert model_input['memory_management_context'] == _context()
    assert model_input['current_user_utterance'] == '우리 강아지 이름은 초코야'
    assert len(requests) == 1
    assert len(calls) == 2


def test_actual_pydantic_memory_model_is_closed_and_nullable():
    """Actual schema requires each field and cannot accept authority data."""
    runtime = _runtime(_payload(), [])
    model = runtime._output_model(_request(_context()))
    schema = model.model_json_schema()
    assert 'memory_proposal' in schema['required']
    assert schema['additionalProperties'] is False
    assert {'type': 'null'} in schema['properties']['memory_proposal']['anyOf']
    definitions = schema['$defs']
    for name in ('MalbutRaiMemoryFactV2', 'MalbutRaiMemoryProposalV2'):
        assert definitions[name]['additionalProperties'] is False
        assert set(definitions[name]['required']) == set(
            definitions[name]['properties']
        )
    assert model.model_validate(_payload()).memory_proposal is None


@pytest.mark.parametrize('mutation', [
    lambda payload: payload.pop('memory_proposal'),
    lambda payload: payload['memory_proposal'].update({'user_id': 'other'}),
    lambda payload: payload['memory_proposal'].update({'saved': True}),
    lambda payload: payload['memory_proposal'].update({'consent': True}),
    lambda payload: payload['memory_proposal']['facts'][0].update(
        {'value': 4},
    ),
    lambda payload: payload['memory_proposal'].update({'target_ids': [3]}),
    lambda payload: payload['memory_proposal'].update(
        {'operation': 'execute'},
    ),
    lambda payload: payload['memory_proposal'].update({
        'facts': payload['memory_proposal']['facts'] * 9,
    }),
    lambda payload: payload['memory_proposal'].update({
        'target_ids': [str(index) for index in range(21)],
    }),
])
def test_actual_pydantic_model_rejects_invalid_memory_payloads(mutation):
    """Strict nested fields reject coercion, truncation and extra authority."""
    pydantic = pytest.importorskip('pydantic', minversion='2')
    model = _runtime(_payload(), [])._output_model(_request(_context()))
    payload = copy.deepcopy(_payload(_proposal()))
    mutation(payload)
    with pytest.raises(pydantic.ValidationError):
        model.model_validate(payload)


def test_runtime_rejects_robot_and_memory_in_the_same_proposal(monkeypatch):
    """A syntactically valid mixed graph output must fail as a whole."""
    payload = _payload(_proposal())
    payload.update({
        'kind': 'action_proposal', 'response_type': None,
        'tool_name': 'navigate', 'arguments': {'location': '거실'},
        'expires_in_ms': 5000,
    })
    runtime = _runtime(payload, [])
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.create_runtime',
        lambda _factory: runtime,
    )
    response = decode_response(run_once(encode_request(_request(_context()))))
    assert response == RuntimeErrorResponse('runtime_failed', schema_version=2)
    with pytest.raises(RaiSidecarRuntimeError):
        RaiSidecarClient(lambda payload, _timeout: run_once(payload)).propose(
            _request(_context()),
        )


def test_protocol_rejects_memory_fields_on_robot_output():
    """The wire action shape has no place to carry memory changes."""
    value = {
        'schema_version': 2, 'kind': 'proposal_response',
        'request_id': 'memory-rai-1', 'model': 'fake-model',
        'response_id': None,
        'usage': {
            'input_tokens': None, 'output_tokens': None, 'total_tokens': None,
        },
        'output': {
            'kind': 'action_proposal', 'tool_name': 'navigate',
            'arguments': {'location': '거실'}, 'message': '', 'reason': '',
            'confidence': None, 'expires_in_ms': 5000,
            'memory_proposal': _proposal(),
        },
    }
    with pytest.raises(RaiSidecarProtocolError):
        decode_response(json.dumps(value).encode())
