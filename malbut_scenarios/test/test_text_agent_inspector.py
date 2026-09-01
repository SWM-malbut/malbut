"""Contracts for the local, non-actuating Text Agent inspector."""

import ast
from dataclasses import fields
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.schemas import AgentDecision, ProviderResult
from malbut_scenarios.text_agent_inspector import (
    InspectionReport,
    ISOLATED,
    TextAgentInspector,
    _load_settings,
    _parser,
    format_history,
    format_report,
    run_console,
)
from malbut_scenarios.simulation_text_runtime import (
    build_simulation_text_runtime,
)


class Catalog:
    """Resolve the three private fixture names without execution methods."""

    def resolve(self, location: str):
        if location not in {'거실', '주방', '침실'}:
            raise ValueError('unknown target')
        return SimpleNamespace(
            room_name=location,
            room_category='room',
            binding_digest=(location.encode('utf-8').hex() + 'a' * 64)[:64],
        )


def _inspector(*, mode='stateful'):
    settings = Settings(
        provider='mock',
        auth_token='inspector-test-only',
        database_path=':memory:',
        tool_mode='proposal',
    )
    orchestrator, service = build_simulation_text_runtime(
        settings,
        Catalog,
    )
    inspector = TextAgentInspector(
        orchestrator,
        service,
        user_id='local-user',
        mode=mode,
    )
    return inspector, orchestrator, service


def _close(orchestrator) -> None:
    orchestrator.conversation_store.close()
    orchestrator.memory_store.close()


def test_parser_defaults_to_offline_stateful_without_execution_flag() -> None:
    parser = _parser()
    parsed = parser.parse_args([])

    assert parsed.provider == 'mock'
    assert parsed.allow_live_provider is False
    assert parsed.mode == 'stateful'
    assert 'execute' not in parser.format_help()


@pytest.mark.parametrize('provider', ('openai', 'rai-sidecar'))
def test_live_provider_requires_explicit_opt_in(
    monkeypatch,
    provider,
) -> None:
    monkeypatch.setenv('MALBUT_AGENT_PROVIDER', 'mock')
    parsed = _parser().parse_args([
        '--env-file', '/dev/null',
        '--provider', provider,
        '--map-store', '/tmp/map-store',
        '--device-id', 'malbut-sim-01',
    ])

    with pytest.raises(ValueError, match='allow-live-provider'):
        _load_settings(parsed)


def test_hostile_environment_cannot_select_live_or_simulation(
    monkeypatch,
) -> None:
    monkeypatch.setenv('MALBUT_AGENT_PROVIDER', 'openai')
    monkeypatch.setenv('MALBUT_AGENT_TOOL_MODE', 'simulation')
    monkeypatch.setenv('MALBUT_AGENT_DB', '/tmp/do-not-use.sqlite3')
    parsed = _parser().parse_args([
        '--env-file', '/dev/null',
        '--map-store', '/tmp/map-store',
        '--device-id', 'malbut-sim-01',
    ])

    settings, _, _ = _load_settings(parsed)

    assert settings.provider == 'mock'
    assert settings.tool_mode == 'proposal'


def test_stateful_clarification_exposes_projection_without_effects(
) -> None:
    inspector, orchestrator, service = _inspector()
    try:
        question = inspector.submit('여기로 가줘')
        answer = inspector.submit('거실')
        approval = inspector.submit('네')

        assert question.route == 'clarification_required'
        assert question.route_code == 'navigation_destination_missing'
        assert question.provider_called is False
        assert question.effective_changed is None
        assert question.provider == 'malbut-server-policy'
        assert question.model == 'navigation-clarification-v1'
        assert answer.route == 'confirmable_action_proposal'
        assert answer.provider_called is True
        assert answer.effective_changed is True
        assert answer.model_tool_name == 'navigate'
        assert answer.model_argument_keys == ('location',)
        assert answer.status == 'awaiting_confirmation'
        assert approval.route == 'confirmation_response'
        assert approval.provider_called is False
        assert approval.status == 'approved'
        assert service.create_robot_actions is False
        for report in (question, answer, approval):
            assert report.execution['execution_authorized'] is False
            assert report.execution['physical_authorized'] is False
            assert report.execution['nav2_start_count'] == 0
            assert report.execution['nav2_cancel_count'] == 0
    finally:
        _close(orchestrator)


def test_isolated_mode_does_not_reuse_previous_clarification() -> None:
    inspector, orchestrator, _ = _inspector(mode=ISOLATED)
    try:
        question = inspector.submit('여기로 가줘')
        answer = inspector.submit('거실')

        assert question.route == 'clarification_required'
        assert question.provider_called is False
        assert answer.effective_changed is False
        assert answer.route == 'clarification_required'
        assert answer.status == 'completed'
        assert answer.proposal_present is False
    finally:
        _close(orchestrator)


def test_console_commands_do_not_call_provider() -> None:
    inspector, orchestrator, _ = _inspector()
    values = iter([
        '',
        '/history',
        '/new',
        '/isolated',
        '/stateful',
        '/unknown',
        '/quit',
    ])
    output = StringIO()
    try:
        run_console(
            inspector,
            read=lambda _prompt: next(values),
            output=output,
        )

        assert inspector.provider.last_observation is None
        assert inspector.history() == ()
        assert '알 수 없는 명령입니다.' in output.getvalue()
    finally:
        _close(orchestrator)


def test_report_schema_has_no_model_or_user_content_fields() -> None:
    names = {field.name for field in fields(InspectionReport)}

    assert names.isdisjoint({
        'raw_utterance',
        'effective_utterance',
        'model_decision',
        'final_decision',
        'message',
        'proposal',
        'conversation',
    })


def test_unknown_credential_shape_never_reaches_report_or_history() -> None:
    credential = 'ghp_0123456789abcdefghijklmnopqrstuvwxyz'
    inspector, orchestrator, _ = _inspector()

    def credential_provider(request, *args, **kwargs):
        del request, args, kwargs
        return ProviderResult(
            decision=AgentDecision(
                type='tool_call',
                message=credential,
                tool_name='navigate',
                arguments={
                    'location': credential,
                    credential: credential,
                },
            ),
            provider=credential,
            model=credential,
            latency_ms=0.0,
        )

    inspector.provider.delegate.complete = credential_provider
    try:
        report = inspector.submit(credential)
        rendered = '\n'.join(
            format_report(report)
            + format_history(inspector.history())
        )

        assert credential not in repr(report)
        assert credential not in rendered
        assert credential not in repr(inspector.history())
        assert report.provider == '<configured>'
        assert report.model == '<configured>'
        assert report.model_argument_keys == (
            'location',
            '<unexpected>',
        )
        assert inspector.provider.last_observation is None
        assert 'chars=' in rendered
        assert 'binding_digest' not in rendered
    finally:
        _close(orchestrator)


def test_inspector_source_has_no_execution_or_ros_imports() -> None:
    path = Path(__file__).parents[1] / (
        'malbut_scenarios/text_agent_inspector.py'
    )
    tree = ast.parse(path.read_text(encoding='utf-8'))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = {
        'rclpy',
        'malbut_agent_server.adapters.outbound',
        'malbut_agent_server.application.approved_action_worker',
        'malbut_gazebo.robot_web_navigation_client',
        'malbut_scenarios.text_agent_server',
    }

    assert imports.isdisjoint(forbidden)


def test_console_script_is_registered() -> None:
    setup = (Path(__file__).parents[1] / 'setup.py').read_text(
        encoding='utf-8',
    )

    assert 'malbut_text_agent_inspector = ' in setup
    assert 'malbut_scenarios.text_agent_inspector:main' in setup
