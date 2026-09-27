"""Verify conversational lab configuration without any real model API requests."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from malbut_agent_server import factory
from malbut_agent_server import voice_lab
from malbut_agent_server.config import Settings
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.providers.routed import RoutedAgentProvider
from malbut_agent_server.speech_mission_policy import configure_speech_missions


def arguments(**overrides):
    values = {'chat': False, 'provider': None, 'env_file': None, 'model': None}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_no_options_stays_offline_even_when_process_has_live_provider_settings(monkeypatch):
    monkeypatch.setattr(os, 'environ', {
        'MALBUT_AGENT_PROVIDER': 'openai', 'OPENAI_API_KEY': 'test-only-not-a-real-key',
    })
    assert voice_lab._dialogue_settings(arguments()).provider == 'mock'


def test_env_precedence_cli_override_and_isolated_database(monkeypatch, tmp_path):
    source = {'OPENAI_MODEL': 'environment-model', 'UNRELATED': 'unchanged'}
    monkeypatch.setattr(os, 'environ', source)
    env_file = tmp_path / 'agent.env'
    env_file.write_text(
        'MALBUT_AGENT_PROVIDER=openai\nOPENAI_API_KEY=test-only-not-a-real-key\n'
        'OPENAI_MODEL=file-model\nOPENAI_SUMMARY_MODEL=file-summary\n'
        'OPENAI_GENERAL_MODEL=file-general\nOPENAI_ROBOT_PLANNER_MODEL=file-planner\n'
        'MALBUT_AGENT_DB=/must-not-open-production.sqlite3\n'
        'MALBUT_AGENT_USER_ID=production-user\nMALBUT_AGENT_TOOL_MODE=simulation\n',
    )
    settings = voice_lab._dialogue_settings(arguments(env_file=str(env_file)))
    assert settings.provider == 'openai'
    assert settings.openai_model == 'environment-model'
    assert settings.database_path == ':memory:'
    assert settings.user_id == 'voice-lab-user'
    assert settings.tool_mode == 'proposal'
    overridden = voice_lab._dialogue_settings(arguments(
        provider='openai', env_file=str(env_file), model='cli-model',
    ))
    assert overridden.openai_model == 'cli-model'
    assert overridden.openai_summary_model == 'file-summary'
    assert overridden.openai_general_model == 'file-general'
    assert overridden.openai_robot_planner_model == 'file-planner'
    assert source == {'OPENAI_MODEL': 'environment-model', 'UNRELATED': 'unchanged'}


def test_chat_reads_only_the_expected_conventional_file(monkeypatch, tmp_path):
    monkeypatch.setattr(os, 'environ', {})
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    config = tmp_path / '.config/malbut/agent.env'
    config.parent.mkdir(parents=True)
    config.write_text('OPENAI_API_KEY=test-only-not-a-real-key\nOPENAI_MODEL=local-chat\n')
    result = voice_lab._dialogue_settings(arguments(chat=True))
    assert result.provider == 'openai'
    assert result.openai_model == 'local-chat'
    assert os.environ == {}


@pytest.mark.parametrize('values,options,match', [
    ({}, {'provider': 'openai'}, 'OPENAI_API_KEY'),
    ({'OPENAI_API_KEY': 'test-only-not-a-real-key'},
     {'provider': 'openai', 'model': 'invalid model'}, 'OPENAI_MODEL'),
    ({}, {'chat': True, 'provider': 'mock'}, '--chat'),
    ({}, {'provider': 'mock', 'model': 'unused-model'}, '--model'),
])
def test_invalid_live_configuration_fails_locally(monkeypatch, values, options, match):
    monkeypatch.setattr(os, 'environ', values)
    with pytest.raises(ValueError, match=match):
        voice_lab._dialogue_settings(arguments(**options))


def test_explicit_missing_environment_file_is_not_silently_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(os, 'environ', {})
    with pytest.raises(ValueError, match='파일'):
        voice_lab._dialogue_settings(arguments(env_file=str(tmp_path / 'missing.env')))


class RecordingGraph:
    """Observe CLI factory selection without importing or spinning ROS."""

    instances = []

    def __init__(self, output, domain_id=197, dialogue_settings=None):
        self.output = output
        self.domain_id = domain_id
        self.settings = dialogue_settings
        self.closed = False
        self.sent = []
        self.__class__.instances.append(self)

    def start(self):
        pass

    def close(self):
        self.closed = True

    def send(self, text):
        self.sent.append(text)
        return 'utterance-' + str(len(self.sent))

    def wait_reply(self, uid):
        return {'utterance_id': uid, 'kind': 'answer', 'text': 'offline response'}


def _offline_scenario(identifier, graph_factory, output):
    graph = graph_factory(output=output)
    try:
        graph.start()
        assert graph.settings is None
        result = {'id': identifier, 'passed': True, 'evidence': {'provider': 'mock'}}
        output({'event': 'scenario_result', **result})
        return result
    finally:
        graph.close()


@pytest.mark.parametrize('batch', ['--scenario', '--all'])
def test_batch_scenarios_never_load_credentials_or_construct_live_settings(
    monkeypatch, tmp_path, batch,
):
    from malbut_agent_server import voice_lab_graph, voice_lab_scenarios

    RecordingGraph.instances = []
    monkeypatch.setattr(voice_lab_graph, 'VoiceLabGraph', RecordingGraph)
    monkeypatch.setattr(voice_lab, '_dialogue_settings',
                        lambda args: pytest.fail('batch must not inspect live configuration'))
    monkeypatch.setattr(voice_lab_scenarios, 'run_scenario', _offline_scenario)
    monkeypatch.setattr(voice_lab_scenarios, 'run_all',
                        lambda factory, output: [_offline_scenario('navigation', factory, output)])
    options = [batch] + (['navigation'] if batch == '--scenario' else [])
    code = voice_lab.main([
        *options, '--provider', 'openai', '--env-file', '/not-to-be-opened.env',
        '--model', 'not-to-be-called', '--report', str(tmp_path / 'results.json'),
    ])
    assert code == 0
    assert len(RecordingGraph.instances) == 1
    assert RecordingGraph.instances[0].settings is None
    assert RecordingGraph.instances[0].closed


def test_interactive_chat_uses_live_settings_but_scenarios_remain_mock(
    monkeypatch, tmp_path, capsys,
):
    from malbut_agent_server import voice_lab_graph, voice_lab_scenarios

    monkeypatch.setattr(os, 'environ', {})
    RecordingGraph.instances = []
    monkeypatch.setattr(voice_lab_graph, 'VoiceLabGraph', RecordingGraph)
    monkeypatch.setattr(voice_lab_scenarios, 'run_scenario', _offline_scenario)
    env_file = tmp_path / 'agent.env'
    fake_key = 'test-only-not-a-real-key'
    env_file.write_text('OPENAI_API_KEY=' + fake_key + '\n')

    def interact(terminal, stream):
        assert terminal.graph.settings.provider == 'openai'
        assert terminal.command('안녕, 오늘은 어떤 이야기를 해볼까?')
        assert terminal.command('/new')
        assert terminal.graph.sent[-1] == '새 대화 시작해줘'
        terminal.scenario('navigation')
        assert terminal.graph.settings.provider == 'openai'

    monkeypatch.setattr(voice_lab.Terminal, 'interact', interact)
    report_path = tmp_path / 'results.json'
    code = voice_lab.main([
        '--chat', '--env-file', str(env_file), '--model', 'chat-model',
        '--report', str(report_path),
    ])
    assert code == 0
    assert [graph.settings.provider if graph.settings else 'mock'
            for graph in RecordingGraph.instances] == ['openai', 'mock', 'openai']
    assert all(graph.closed for graph in RecordingGraph.instances)
    report = json.loads(report_path.read_text())
    assert report['passed'] == 1
    assert fake_key not in report_path.read_text()
    assert fake_key not in report_path.with_name(report_path.name + '.events.jsonl').read_text()
    assert fake_key not in capsys.readouterr().out


def test_live_runtime_startup_is_lazy_and_role_models_do_not_replace_foreground(
    monkeypatch, tmp_path,
):
    calls = []

    def forbidden_transport(*args, **kwargs):
        calls.append(True)
        raise AssertionError('a model request must wait for a user turn')

    monkeypatch.setattr(OpenAIResponsesProvider, '_urllib_transport',
                        staticmethod(forbidden_transport))
    # Tokenizer warmup is local infrastructure, not an API call; avoid even its
    # optional first-run download so this test is entirely offline.
    monkeypatch.setattr(factory, 'count_tokens', lambda value: 1)
    settings = Settings(
        provider='openai', openai_api_key='test-only-not-a-real-key',
        openai_model='test-primary', openai_general_model='test-addressee',
        openai_robot_planner_model='test-unused-planner',
        openai_summary_model='test-summary',
        database_path=str(tmp_path / 'conversation.sqlite3'),
    )
    runtime = factory.build_orchestrator(settings, http_server=False)
    try:
        configure_speech_missions(runtime)
        runtime.start_background_memory()
        assert isinstance(runtime.provider, ReliableProvider)
        assert not isinstance(runtime.provider, RoutedAgentProvider)
        assert runtime.provider._providers[0].model == 'test-primary'
        assert runtime.speech_addressee.provider.model == 'test-addressee'
        assert runtime.context_compactor.summarizer.model == 'test-summary'
        assert 'request_follow_person' in runtime.speech_mission_tools
    finally:
        runtime.close()
    assert calls == []
