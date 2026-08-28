"""Tests for the fail-closed RAI sidecar client and Provider adapter."""

import os
import subprocess
import sys

import pytest

from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.rai_sidecar_client import (
    RaiSidecarClient,
    RaiSidecarCrashError,
    RaiSidecarMalformedResponseError,
    RaiSidecarOutputLimitError,
    RaiSidecarProvider,
    RaiSidecarRuntimeError,
    RaiSidecarTimeoutError,
    SubprocessRaiSidecarTransport,
)
from malbut_agent_server.rai_sidecar_protocol import (
    ActionProposal,
    MAX_RESPONSE_BYTES,
    ProposalRequest,
    ProposalResponse,
    RuntimeErrorResponse,
    SidecarUsage,
    TextReply,
    decode_request,
    decode_response,
    encode_response,
)
from malbut_agent_server.schemas import AgentRequest
from malbut_agent_server.tools import TOOL_SPECS


SENSITIVE = 'credential-user-argument-canary-SWM25-131'


def _agent_request() -> AgentRequest:
    return AgentRequest.from_dict({
        'request_id': 'request-1',
        'user_id': 'user-1',
        'conversation_id': 'conversation-1',
        'turn_id': 'turn-1',
        'utterance': '거실로 이동해 줘',
        'robot_state': {},
        'available_tools': ['navigate'],
    })


def _proposal_request() -> ProposalRequest:
    return ProposalRequest(
        request_id='request-1',
        instructions='Return one proposal.',
        model_input='거실로 이동해 줘',
        tools=({
            'name': 'navigate',
            'description': 'Navigate.',
            'parameters': TOOL_SPECS['navigate'].parameters,
        },),
    )


def _response(output) -> bytes:
    return encode_response(ProposalResponse(
        request_id='request-1',
        model='fake-rai',
        response_id='response-1',
        usage=SidecarUsage(10, 4, 14),
        output=output,
    ))


def test_provider_implements_existing_contract_and_maps_one_action() -> None:
    calls = []

    def transport(payload: bytes, timeout: float) -> bytes:
        calls.append((decode_request(payload), timeout))
        return _response(ActionProposal(
            tool_name='navigate',
            arguments={'location': '거실'},
            message='거실로 이동할까요?',
            reason='named_destination',
            confidence=0.8,
            expires_in_ms=4000,
        ))

    provider = RaiSidecarProvider(
        RaiSidecarClient(transport, timeout_seconds=2.5)
    )
    result = provider.complete(
        _agent_request(),
        memories=[],
        conversation_turns=[],
        tools=[TOOL_SPECS['navigate']],
    )

    assert isinstance(provider, AgentProvider)
    assert result.provider == 'rai-sidecar'
    assert result.model == 'fake-rai'
    assert result.response_id == 'response-1'
    assert result.usage.total_tokens == 14
    assert result.decision.type == 'tool_call'
    assert result.decision.tool_name == 'navigate'
    assert result.decision.arguments == {'location': '거실'}
    assert result.decision.expires_in_ms == 4000
    assert len(calls) == 1
    assert calls[0][1] == 2.5
    assert calls[0][0].tools[0]['name'] == 'navigate'


def test_provider_maps_one_text_reply_without_a_tool() -> None:
    client = RaiSidecarClient(
        lambda _payload, _timeout: _response(
            TextReply('clarification', '어느 방인가요?', '', 0.7)
        )
    )

    result = RaiSidecarProvider(client).complete(
        _agent_request(),
        memories=[],
        conversation_turns=[],
        tools=[TOOL_SPECS['navigate']],
    )

    assert result.decision.type == 'clarification'
    assert result.decision.tool_name is None
    assert result.decision.arguments == {}


@pytest.mark.parametrize(
    'output',
    [
        ActionProposal(
            'capture_photo',
            {},
            '',
            '',
            None,
            5000,
        ),
        ActionProposal(
            'navigate',
            {'location': None},
            '',
            '',
            None,
            5000,
        ),
        ActionProposal(
            'navigate',
            {'location': '거실', 'approved': 'true'},
            '',
            '',
            None,
            5000,
        ),
    ],
)
def test_provider_revalidates_tool_name_and_arguments_locally(output) -> None:
    provider = RaiSidecarProvider(RaiSidecarClient(
        lambda _payload, _timeout: _response(output)
    ))

    with pytest.raises(RaiSidecarMalformedResponseError):
        provider.complete(
            _agent_request(),
            memories=[],
            conversation_turns=[],
            tools=[TOOL_SPECS['navigate']],
        )


def test_client_does_not_retry_timeout_or_runtime_failure() -> None:
    timeout_calls = []

    def timeout_transport(_payload, _timeout):
        timeout_calls.append(True)
        raise TimeoutError(SENSITIVE)

    with pytest.raises(RaiSidecarTimeoutError) as timeout_error:
        RaiSidecarClient(timeout_transport).propose(_proposal_request())

    runtime_calls = []

    def runtime_transport(_payload, _timeout):
        runtime_calls.append(True)
        return encode_response(RuntimeErrorResponse('runtime_failed'))

    with pytest.raises(RaiSidecarRuntimeError) as runtime_error:
        RaiSidecarClient(runtime_transport).propose(_proposal_request())

    assert len(timeout_calls) == 1
    assert len(runtime_calls) == 1
    assert SENSITIVE not in str(timeout_error.value)
    assert SENSITIVE not in repr(timeout_error.value)
    assert SENSITIVE not in str(runtime_error.value)


@pytest.mark.parametrize(
    'raw',
    [
        b'{',
        b'',
        _response(TextReply('message', '', '', None)).replace(
            b'"request-1"',
            b'"different-request"',
        ),
    ],
)
def test_client_rejects_partial_empty_or_mismatched_response(raw) -> None:
    client = RaiSidecarClient(lambda _payload, _timeout: raw)

    with pytest.raises(RaiSidecarMalformedResponseError):
        client.propose(_proposal_request())


def _transport_for_script(script: str) -> SubprocessRaiSidecarTransport:
    return SubprocessRaiSidecarTransport((
        os.path.realpath(sys.executable),
        '-c',
        script,
    ))


def test_subprocess_transport_uses_fixed_argv_no_shell_and_clean_env(
    monkeypatch,
) -> None:
    valid = _response(TextReply('message', 'ok', '', None))
    script = (
        'import os,sys;sys.stdin.buffer.read();'
        f'sys.stdout.buffer.write({valid!r} if '
        f'os.getenv({SENSITIVE!r}) is None else b"leaked")'
    )
    real_popen = subprocess.Popen
    calls = []

    def recording_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return real_popen(*args, **kwargs)

    monkeypatch.setenv(SENSITIVE, SENSITIVE)
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_client.subprocess.Popen',
        recording_popen,
    )
    transport = _transport_for_script(script)

    response = transport(b'{}', 2.0)

    assert type(decode_response(response)) is ProposalResponse
    assert len(calls) == 1
    assert calls[0][0][0] == transport.argv
    assert calls[0][1]['shell'] is False
    assert calls[0][1]['stderr'] is subprocess.DEVNULL
    assert SENSITIVE not in calls[0][1]['env']
    assert repr(transport) == 'SubprocessRaiSidecarTransport(<redacted>)'


def test_subprocess_failures_are_typed_and_fail_closed() -> None:
    timeout = _transport_for_script(
        'import sys,time;sys.stdin.buffer.read();time.sleep(2)'
    )
    crash = _transport_for_script(
        'import sys;sys.stdin.buffer.read();sys.exit(7)'
    )
    partial = _transport_for_script(
        'import sys;sys.stdin.buffer.read();sys.stdout.write("{")'
    )
    oversized = _transport_for_script(
        'import sys;sys.stdin.buffer.read();'
        f'sys.stdout.buffer.write(b"x"*{MAX_RESPONSE_BYTES + 1})'
    )

    with pytest.raises(RaiSidecarTimeoutError):
        timeout(b'{}', 0.05)
    with pytest.raises(RaiSidecarCrashError):
        crash(b'{}', 1.0)
    with pytest.raises(RaiSidecarMalformedResponseError):
        RaiSidecarClient(partial).propose(_proposal_request())
    with pytest.raises(RaiSidecarOutputLimitError):
        oversized(b'{}', 1.0)


def test_subprocess_rejects_relative_argv_and_unknown_env() -> None:
    with pytest.raises(ValueError):
        SubprocessRaiSidecarTransport(('python3', '-c', 'pass'))
    with pytest.raises(ValueError):
        SubprocessRaiSidecarTransport(
            (os.path.realpath(sys.executable), '-c', 'pass'),
            environment={'UNSAFE_SECRET': SENSITIVE},
        )
