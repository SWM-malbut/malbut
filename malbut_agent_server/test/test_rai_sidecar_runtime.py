"""Tests for the lazy, one-request RAI sidecar runtime."""

import ast
from io import BytesIO
from pathlib import Path
import sys

import pytest

from malbut_agent_server.rai_sidecar_protocol import (
    ActionProposal,
    ProposalRequest,
    ProposalResponse,
    RuntimeErrorResponse,
    TextReply,
    decode_response,
    encode_request,
    project_tool_specs,
)
from malbut_agent_server.rai_sidecar_runtime import (
    RAI_CORE_DISTRIBUTION,
    REQUIRED_RAI_CORE_VERSION,
    RaiRuntimeConfigurationError,
    create_runtime,
    main,
    run_once,
)
from malbut_agent_server.tools import TOOL_SPECS


SENSITIVE = 'secret-runtime-canary-SWM25-131'


@pytest.fixture(autouse=True)
def _pinned_rai_core_distribution(monkeypatch) -> None:
    """Present the reviewed distribution to isolated runtime unit tests."""
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.sys.prefix',
        '/isolated/rai-venv',
    )
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.sys.base_prefix',
        '/usr',
    )
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.'
        'importlib_metadata.version',
        lambda name: (
            REQUIRED_RAI_CORE_VERSION
            if name == RAI_CORE_DISTRIBUTION
            else None
        ),
    )


def test_runtime_rejects_non_isolated_interpreter(monkeypatch) -> None:
    """Do not import RAI when the process is not running in a venv."""
    imports = []
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.sys.prefix',
        '/usr',
    )
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.sys.base_prefix',
        '/usr',
    )
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.importlib.import_module',
        lambda name: imports.append(name),
    )

    with pytest.raises(RaiRuntimeConfigurationError) as raised:
        create_runtime(lambda _module: _Runtime(TextReply(
            'message',
            'unused',
            '',
            None,
        )))

    assert imports == []
    assert SENSITIVE not in str(raised.value)


def _request() -> ProposalRequest:
    return ProposalRequest(
        request_id='runtime-request-1',
        instructions='Return one proposal.',
        model_input='거실로 이동해 줘',
        tools=project_tool_specs([TOOL_SPECS['navigate']]),
    )


class _Runtime:
    model = 'fake-rai-model'

    def __init__(self, output) -> None:
        self.output = output
        self.calls = 0

    def propose(self, _request: ProposalRequest):
        self.calls += 1
        return self.output


def _factory_for(runtime: _Runtime):
    return lambda _rai_module: runtime


def test_client_and_protocol_have_no_rai_import_statements() -> None:
    """Keep the Agent-side protocol and transport independent from RAI."""
    package = Path(__file__).parents[1] / 'malbut_agent_server'
    for name in ('rai_sidecar_protocol.py', 'rai_sidecar_client.py'):
        tree = ast.parse((package / name).read_text(encoding='utf-8'))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or '')
        assert not any(
            name == 'rai' or name.startswith('rai.')
            for name in imported
        )


def test_create_runtime_is_the_only_lazy_rai_import_hook(monkeypatch) -> None:
    """Load RAI only through the isolated runtime construction boundary."""
    imported = []
    sentinel_module = object()
    runtime = _Runtime(TextReply('message', '안녕하세요', '', None))

    def fake_import(name: str):
        imported.append(name)
        return sentinel_module

    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.importlib.import_module',
        fake_import,
    )
    built = create_runtime(
        lambda rai_module: runtime
        if rai_module is sentinel_module
        else None
    )

    assert built is runtime
    assert imported == ['rai.agents.langchain.core']
    assert not any(name.startswith('rai.tools') for name in imported)


def test_default_runtime_is_off_without_isolated_rai_configuration(
    monkeypatch,
) -> None:
    """Fail closed when no explicit model configuration is present."""
    monkeypatch.delenv('MALBUT_RAI_MODEL', raising=False)

    with pytest.raises(RaiRuntimeConfigurationError) as raised:
        create_runtime()

    assert SENSITIVE not in str(raised.value)


@pytest.mark.parametrize(
    'installed_version',
    [None, '2.12.0', '2.12.1+local', '2.13.0'],
)
def test_runtime_rejects_missing_or_non_exact_rai_core_distribution(
    monkeypatch,
    installed_version,
) -> None:
    """Missing, older, newer, and locally modified RAI all fail closed."""
    imports = []

    def distribution_version(name):
        assert name == RAI_CORE_DISTRIBUTION
        if installed_version is None:
            raise RuntimeError(SENSITIVE)
        return installed_version

    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.'
        'importlib_metadata.version',
        distribution_version,
    )
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.importlib.import_module',
        lambda name: imports.append(name),
    )

    with pytest.raises(RaiRuntimeConfigurationError) as raised:
        create_runtime(lambda _module: _Runtime(TextReply(
            'message',
            'unused',
            '',
            None,
        )))

    assert not imports
    assert SENSITIVE not in str(raised.value)


def test_default_runtime_uses_rai_structured_graph_and_injected_llm(
    monkeypatch,
) -> None:
    """Build one structured RAI graph with the explicitly selected LLM."""
    calls = {'imports': [], 'graph_builds': [], 'invocations': []}
    payload = {
        'kind': 'action_proposal',
        'response_type': None,
        'message': '이동할까요?',
        'reason': 'named_destination',
        'confidence': 0.8,
        'tool_name': 'navigate',
        'arguments': {'location': '거실'},
        'expires_in_ms': 5000,
    }

    class Parsed:
        @staticmethod
        def model_dump(*, mode):
            assert mode == 'python'
            return dict(payload)

    class Graph:
        @staticmethod
        def invoke(state):
            calls['invocations'].append(state)
            return {
                'messages': [
                    {
                        'parsed': Parsed(),
                        'parsing_error': None,
                    }
                ]
            }

    class RaiCore:
        @staticmethod
        def create_structured_output_runnable(**kwargs):
            calls['graph_builds'].append(kwargs)
            return Graph()

    class ChatOpenAI:
        def __init__(self, **kwargs):
            self.configuration = kwargs

    class OpenAIModule:
        pass

    OpenAIModule.ChatOpenAI = ChatOpenAI

    class HumanMessage:
        def __init__(self, *, content):
            self.content = content

    class MessagesModule:
        pass

    MessagesModule.HumanMessage = HumanMessage

    class PydanticModule:
        model_calls = []

        @staticmethod
        def ConfigDict(**kwargs):
            return kwargs

        @staticmethod
        def create_model(name, **kwargs):
            assert kwargs['__config__'] == {
                'extra': 'forbid',
                'strict': True,
            }
            PydanticModule.model_calls.append((name, kwargs))
            return type(name, (), {})

    modules = {
        'rai.agents.langchain.core': RaiCore,
        'langchain_openai': OpenAIModule,
        'langchain_core.messages': MessagesModule,
        'pydantic': PydanticModule,
    }

    class InvocationHelpers:
        get_tracing_callbacks = object()

    modules['rai.agents.langchain.invocation_helpers'] = (
        InvocationHelpers
    )

    def fake_import(name):
        calls['imports'].append(name)
        return modules[name]

    monkeypatch.setenv('MALBUT_RAI_MODEL', 'gpt-test')
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.importlib.import_module',
        fake_import,
    )

    runtime = create_runtime()
    output = runtime.propose(_request())

    assert output == ActionProposal(
        'navigate',
        {'location': '거실'},
        '이동할까요?',
        'named_destination',
        0.8,
        5000,
    )
    assert calls['imports'] == [
        'rai.agents.langchain.core',
        'langchain_openai',
        'langchain_core.messages',
        'pydantic',
        'rai.agents.langchain.invocation_helpers',
    ]
    assert InvocationHelpers.get_tracing_callbacks() == []
    assert len(calls['graph_builds']) == 1
    assert [
        name for name, _fields in PydanticModule.model_calls
    ] == ['MalbutRaiArgumentsV1', 'MalbutRaiProposalV1']
    argument_fields = PydanticModule.model_calls[0][1]
    assert set(argument_fields) == {'__config__', 'location'}
    proposal_fields = PydanticModule.model_calls[1][1]
    assert all(
        field[1] is ...
        for name, field in proposal_fields.items()
        if name != '__config__'
    )
    llm = calls['graph_builds'][0]['llm']
    assert llm.configuration == {
        'model': 'gpt-test',
        'temperature': 0,
        'max_retries': 0,
    }
    assert len(calls['invocations']) == 1
    prompt = calls['graph_builds'][0]['system_prompt']
    assert '"name":"navigate"' in prompt
    assert 'approved' not in prompt


@pytest.mark.parametrize(
    'output',
    [
        TextReply('message', '안녕하세요', 'direct', 0.9),
        ActionProposal(
            'navigate',
            {'location': '거실'},
            '이동할까요?',
            'named_destination',
            0.8,
            5000,
        ),
    ],
)
def test_run_once_returns_one_output_and_calls_runtime_once(
    monkeypatch,
    output,
) -> None:
    """Return exactly one typed result from exactly one runtime call."""
    runtime = _Runtime(output)
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.importlib.import_module',
        lambda _name: object(),
    )

    response = decode_response(
        run_once(
            encode_request(_request()),
            runtime_factory=_factory_for(runtime),
        )
    )

    assert type(response) is ProposalResponse
    assert response.request_id == 'runtime-request-1'
    assert response.model == runtime.model
    assert response.output == output
    assert runtime.calls == 1


@pytest.mark.parametrize(
    ('runtime_output', 'expected_code'),
    [
        (object(), 'invalid_runtime_output'),
        (
            ActionProposal(
                'capture_photo',
                {},
                '',
                '',
                None,
                5000,
            ),
            'invalid_runtime_output',
        ),
        (
            ActionProposal(
                'navigate',
                {'location': None},
                '',
                '',
                None,
                5000,
            ),
            'invalid_runtime_output',
        ),
    ],
)
def test_run_once_rejects_invalid_or_unbound_runtime_output(
    monkeypatch,
    runtime_output,
    expected_code,
) -> None:
    """Reject outputs outside the request's neutral Tool projection."""
    runtime = _Runtime(runtime_output)
    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.importlib.import_module',
        lambda _name: object(),
    )

    response = decode_response(
        run_once(
            encode_request(_request()),
            runtime_factory=_factory_for(runtime),
        )
    )

    assert response == RuntimeErrorResponse(expected_code)
    assert runtime.calls == 1


def test_runtime_exception_returns_content_free_failure(monkeypatch) -> None:
    """Do not return dependency exception text across the process edge."""
    class FailingRuntime:
        model = 'fake-model'

        @staticmethod
        def propose(_request):
            raise RuntimeError(SENSITIVE)

    monkeypatch.setattr(
        'malbut_agent_server.rai_sidecar_runtime.importlib.import_module',
        lambda _name: object(),
    )

    raw = run_once(
        encode_request(_request()),
        runtime_factory=_factory_for(FailingRuntime()),
    )

    assert decode_response(raw) == RuntimeErrorResponse('runtime_failed')
    assert SENSITIVE.encode() not in raw


def test_invalid_request_does_not_initialize_runtime() -> None:
    """Reject malformed envelopes before importing or constructing RAI."""
    calls = []

    response = decode_response(
        run_once(
            b'{"schema_version":1,"unknown":"' +
            SENSITIVE.encode() + b'"}',
            runtime_factory=lambda module: calls.append(module),
        )
    )

    assert response == RuntimeErrorResponse('invalid_request')
    assert not calls
    assert SENSITIVE.encode() not in encode_request(_request())


def test_main_reads_and_writes_exactly_one_bounded_envelope() -> None:
    """Keep the command entry point to one bounded request and response."""
    stdout = BytesIO()

    exit_code = main(
        argv=(),
        runtime_factory=None,
        stdin=BytesIO(encode_request(_request())),
        stdout=stdout,
    )

    assert exit_code == 0
    assert decode_response(stdout.getvalue()) == RuntimeErrorResponse(
        'runtime_unavailable'
    )


def test_importing_main_process_modules_does_not_load_rai() -> None:
    """Prove normal Agent imports do not contaminate the ROS process."""
    before = {
        name for name in sys.modules
        if name == 'rai' or name.startswith('rai.')
    }

    __import__('malbut_agent_server.rai_sidecar_client')
    __import__('malbut_agent_server.rai_sidecar_protocol')

    after = {
        name for name in sys.modules
        if name == 'rai' or name.startswith('rai.')
    }
    assert after == before
