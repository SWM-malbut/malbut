"""Bind speech classification to configured Agent models without calling them."""

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider


@pytest.mark.parametrize('general_model, expected', [
    ('', 'configured-main-model'),
    ('configured-chat-model', 'configured-chat-model'),
])
def test_classifier_reuses_agent_configuration_and_bypasses_robot_routing(
    monkeypatch, general_model, expected,
):
    def no_network(*args, **kwargs):
        pytest.fail('constructing the Agent must not make a model request')

    monkeypatch.setattr(OpenAIResponsesProvider, '_urllib_transport', no_network)
    runtime = build_orchestrator(Settings(
        database_path=':memory:', provider='openai',
        openai_api_key='test-key-not-for-network',
        openai_model='configured-main-model',
        openai_general_model=general_model,
        openai_robot_planner_model='configured-robot-model',
        request_timeout_seconds=17,
        provider_total_timeout_seconds=20,
    ), http_server=False)
    try:
        provider = runtime.speech_addressee.provider
        assert isinstance(provider, OpenAIResponsesProvider)
        assert provider.model == expected
        assert provider.timeout_seconds == 17
        assert provider.include_reasoning is False
    finally:
        runtime.close()


def test_mock_agent_does_not_guess_addressee_from_keywords():
    runtime = build_orchestrator(Settings(database_path=':memory:'))
    try:
        session = runtime.conversation_store.create('speech-test')
        snapshot = runtime.conversation_store.snapshot(
            'speech-test', session.conversation_id,
        )
        assert runtime.speech_addressee.classify('제이크, 멈춰', snapshot) == 'unknown'
        assert runtime.conversation_store.snapshot(
            'speech-test', session.conversation_id,
        ).turns == ()
    finally:
        runtime.close()
