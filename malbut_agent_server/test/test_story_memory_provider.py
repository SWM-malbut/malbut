"""Foreground story recall does not bypass policy or authority boundaries."""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_agent_server.providers.base import AgentProvider, ProviderError, accepts_memory_context
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.schemas import (
    AgentDecision, ProviderResult, ProviderUsage, RobotState, SpeechAgentRequest, ValidationError,
)
from malbut_agent_server.story_memory_provider import StoryMemoryProvider
from malbut_agent_server.tools import STORY_RECALL_TOOL, TOOL_SPECS


class Service:
    def __init__(self):
        self.revision = 1
        self.data_revision = 1
        self.recorded = []
        self.history_recorded = []
        self.context_value = {
            'revision': 1,
            'stories': [{'story_id': 'exhibition', 'title': '전시 방문',
                         'current': [{'text': '첫 방문에는 피곤했다고 말했다.',
                                      'kind': 'experience', 'actor': 'user', 'status': 'stated'}]}],
        }

    def context(self, user, utterance, recent):
        return deepcopy(self.context_value)

    def validate(self, user, revision, data_revision=None):
        return (revision == self.revision
                and (data_revision is None or data_revision == self.data_revision))

    def record_reply(self, user, request_id, stories, revision):
        self.recorded.append((user, request_id, stories, revision))
        return self.validate(user, revision)

    def record_context_reply(self, user, request_id, turns, summary):
        self.history_recorded.append((user, request_id, turns, summary))

    def validate_readset(self, user, revision, readset, request_id=None):
        current = {story['story_id']: {hashlib.sha256(json.dumps(
            {key: entry[key] for key in ('text', 'kind', 'actor', 'status')},
            ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        ).encode()).hexdigest() for entry in story['current']}
            for story in self.context_value['stories']}
        return self.validate(user, revision) and all(
            set(item['entry_hashes']) <= current.get(item['story_id'], set())
            for item in readset)


class Provider:
    supports_memory = True

    def __init__(self, callback=None):
        self.callback = callback
        self.calls = []

    def complete(self, request, memories, turns, tools, conversation_summary=None,
                 *, memory_context=None, weather_context=None):
        self.calls.append((memory_context, weather_context, tools))
        if self.callback:
            self.callback()
        return 'answer'


def request():
    return SimpleNamespace(user_id='user-a', request_id='req-a', utterance='그 전시 말이야')


def test_wrapper_conforms_to_common_provider_contract():
    wrapper = StoryMemoryProvider(Provider(), Service())

    assert isinstance(wrapper, AgentProvider)
    assert accepts_memory_context(wrapper)
    assert wrapper.complete(request(), [], [], []) == 'answer'


def test_injects_related_data_preserving_settings_and_tracks_reply():
    service, backend = Service(), Provider()
    wrapper = StoryMemoryProvider(backend, service)
    original = {'mode': 'answer_only', 'response_settings': {'length': '짧게'}}
    weather = {'status': 'fresh', 'temperature_c': 20}
    assert wrapper.complete(request(), [], [], [], memory_context=original,
                            weather_context=weather) == 'answer'
    injected = backend.calls[0][0]
    assert injected['response_settings'] == original['response_settings']
    assert 'story_memory_untrusted' not in original
    assert injected['story_memory_untrusted']['execution_authorized'] is False
    assert backend.calls[0][1] == weather
    assert backend.calls[0][2] == []
    assert service.recorded == [('user-a', 'req-a', ['exhibition'], 1)]
    assert wrapper.reply_revision('user-a', 'req-a') == 1
    wrapper.validate_reply('user-a', 'req-a')
    service.revision += 1
    with pytest.raises(ValidationError, match='memory_changed'):
        wrapper.validate_reply('user-a', 'req-a')


def test_policy_change_before_submission_blocks_network():
    service, backend = Service(), Provider()
    service.revision = 2
    wrapper = StoryMemoryProvider(backend, service)
    with pytest.raises(ValidationError, match='memory_changed'):
        wrapper.complete(request(), [], [], [])
    assert backend.calls == []


def test_policy_change_during_inference_blocks_response():
    service = Service()
    backend = Provider(lambda: setattr(service, 'revision', 2))
    wrapper = StoryMemoryProvider(backend, service)
    with pytest.raises(ValidationError, match='memory_changed'):
        wrapper.complete(request(), [], [], [])
    assert wrapper.reply_revision('user-a', 'req-a') is None


def test_source_review_does_not_read_previous_stories():
    service, backend = Service(), Provider()
    wrapper = StoryMemoryProvider(backend, service)
    wrapper.complete(request(), [], [], [], memory_context={'mode': 'source_review'})
    assert 'story_memory_untrusted' not in backend.calls[0][0]
    assert not service.recorded


def test_context_budget_drops_whole_stories_and_does_not_mutate_source():
    service, backend = Service(), Provider()
    service.context_value['stories'][0]['current'][0]['text'] = '긴 이야기' * 2000
    original = deepcopy(service.context_value)
    wrapper = StoryMemoryProvider(backend, service)
    wrapper.complete(request(), [], [], [])
    assert backend.calls[0][0]['story_memory_untrusted']['stories'] == []
    assert service.context_value == original
    assert service.recorded == []


def test_budget_never_keeps_orphan_evidence_for_a_dropped_story():
    service, backend = Service(), Provider()
    service.context_value['stories'][0]['current'][0]['text'] = '긴 이야기' * 2000
    service.context_value['evidence'] = [{'story_id': 'exhibition', 'text': '원문 근거'}]
    wrapper = StoryMemoryProvider(backend, service)
    wrapper.complete(request(), [], [], [])
    attached = backend.calls[0][0]['story_memory_untrusted']
    assert attached['stories'] == []
    assert attached['evidence'] == []
    assert not service.recorded


def test_missing_or_evicted_receipt_cannot_authorize_delayed_delivery():
    wrapper = StoryMemoryProvider(Provider(), Service())
    with pytest.raises(ValidationError, match='memory_changed'):
        wrapper.validate_reply('user-a', 'unknown')
    wrapper.complete(request(), [], [], [])
    wrapper._revisions.clear()
    with pytest.raises(ValidationError, match='memory_changed'):
        wrapper.validate_reply('user-a', 'req-a')


def test_disabled_story_service_does_not_block_normal_dialogue():
    service, backend = Service(), Provider()
    service.context_value = {'revision': 0, 'enabled': False, 'stories': []}
    wrapper = StoryMemoryProvider(backend, service)
    assert wrapper.complete(request(), [], [], []) == 'answer'
    assert 'story_memory_untrusted' not in backend.calls[0][0]
    assert wrapper.reply_revision('user-a', 'req-a') is None


def test_history_dependencies_are_registered_even_without_search_hits():
    service, backend = Service(), Provider()
    service.context_value = {'revision': 1, 'enabled': True, 'stories': []}
    turns, summary = [object()], object()
    wrapper = StoryMemoryProvider(backend, service)
    wrapper.complete(request(), [], turns, [], conversation_summary=summary)
    assert service.history_recorded == [('user-a', 'req-a', turns, summary)]


def test_correction_commit_during_inference_rejects_old_story_answer():
    service = Service()
    def correct():
        service.context_value['stories'][0]['current'][0]['text'] = '피곤하지 않았다고 정정했다.'
    backend = Provider(correct)
    wrapper = StoryMemoryProvider(backend, service)
    with pytest.raises(ValidationError, match='memory_changed'):
        wrapper.complete(request(), [], [], [])


def test_unrelated_background_update_does_not_cancel_normal_voice_delivery():
    service = Service()
    backend = Provider(lambda: setattr(service, 'data_revision', 2))
    wrapper = StoryMemoryProvider(backend, service)
    assert wrapper.complete(request(), [], [], []) == 'answer'
    wrapper.validate_reply('user-a', 'req-a')


def test_internal_readset_is_not_sent_to_model_or_charged_to_story_budget():
    service, backend = Service(), Provider()
    service.context_value['readset'] = [{'entry_hashes': ['a' * 64] * 100}]
    wrapper = StoryMemoryProvider(backend, service)
    wrapper.complete(request(), [], [], [])
    attachment = backend.calls[0][0]['story_memory_untrusted']
    assert 'readset' not in attachment
    assert attachment['stories'][0]['story_id'] == 'exhibition'


def test_missing_current_request_blocks_untracked_inference():
    service, backend = Service(), Provider()
    service.record_context_reply = lambda *args: False
    wrapper = StoryMemoryProvider(backend, service)
    with pytest.raises(ValidationError, match='memory_changed'):
        wrapper.complete(request(), [], [], [])
    assert backend.calls == []


def speech_fixture(*decisions):
    service = Service()
    service.recall_revision = Mock(return_value=service.revision)
    service.context = Mock(wraps=service.context)
    service.trace = Mock()
    backend = Provider()
    backend.complete = Mock(side_effect=[
        ProviderResult(decision, 'openai', 'test', 10, ProviderUsage(5, 3, 8), input_chars=20)
        for decision in decisions
    ])
    wrapper = StoryMemoryProvider(backend, service, on_demand_speech=True)
    speech = SpeechAgentRequest('req-a', 'user-a', 'conv-a', 'turn-a', '그 전시 말이야',
                                RobotState(), ('request_patrol',))
    return wrapper, speech, service, backend


def recall(**arguments):
    return AgentDecision('tool_call', '', tool_name=STORY_RECALL_TOOL.name, arguments=arguments)


def test_current_speech_action_skips_recall_and_keeps_recent_lineage():
    decision = AgentDecision('tool_call', '', tool_name='request_patrol',
                             arguments={'thoroughness': 'normal'})
    wrapper, speech, service, backend = speech_fixture(decision)
    offered = [TOOL_SPECS['request_patrol']]
    result = wrapper.complete(speech, [], ['recent'], offered)
    assert result.decision == decision
    service.context.assert_not_called()
    assert not service.recorded
    assert service.history_recorded == [('user-a', 'req-a', ['recent'], None)]
    args, kwargs = backend.complete.call_args
    assert args[0].available_tools == ('request_patrol', 'recall_story_memory')
    assert args[3] == [*offered, STORY_RECALL_TOOL]
    assert 'story_memory_untrusted' not in kwargs['memory_context']
    assert speech.available_tools == ('request_patrol',) and len(offered) == 1
    wrapper.validate_reply('user-a', 'req-a')


def test_requested_story_uses_original_utterance_once_and_answers_without_tools():
    wrapper, speech, service, backend = speech_fixture(recall(), AgentDecision('message', '전시였어요.'))
    result = wrapper.complete(speech, [], ['recent'], [TOOL_SPECS['request_patrol']])
    service.context.assert_called_once_with('user-a', speech.utterance, ['recent'])
    assert result.usage == ProviderUsage(10, 6, 16)
    assert result.latency_ms == 20 and result.input_chars == 40
    assert [args[1] for args, _ in service.trace.call_args_list] == [
        'memory_retrieved', 'story_recall_requested', 'memory_retrieved', 'provider_completed',
    ]
    args, kwargs = backend.complete.call_args_list[1]
    assert args[0].available_tools == () and args[3] == []
    assert kwargs['memory_context']['story_memory_untrusted']['stories'][0]['story_id'] == 'exhibition'
    assert service.recorded == [('user-a', 'req-a', ['exhibition'], 1)]


@pytest.mark.parametrize('second', [
    recall(), AgentDecision('tool_call', '', tool_name='request_patrol', arguments={'thoroughness': 'light'}),
])
def test_story_answer_cannot_call_a_second_tool(second):
    wrapper, speech, service, backend = speech_fixture(recall(), second)
    with pytest.raises(ProviderError, match='after story lookup'):
        wrapper.complete(speech, [], [], [TOOL_SPECS['request_patrol']])
    assert backend.complete.call_count == 2 and service.context.call_count == 1


@pytest.mark.parametrize('revision,weather,arguments', [
    (None, None, {}), (1, {'status': 'fresh'}, {}), (1, None, {'user_id': 'bob'}),
])
def test_unoffered_or_malformed_lookup_does_not_read_stories(revision, weather, arguments):
    wrapper, speech, service, backend = speech_fixture(recall(**arguments))
    service.recall_revision.return_value = revision
    with pytest.raises(ProviderError, match='invalid story lookup'):
        wrapper.complete(speech, [], [], [], weather_context=weather)
    service.context.assert_not_called()
    assert backend.complete.call_count == 1


def test_changed_story_policy_between_calls_does_not_send_recalled_text():
    wrapper, speech, service, backend = speech_fixture(recall(), AgentDecision('message', 'unused'))
    service.context_value['revision'] = service.revision + 1
    with pytest.raises(ValidationError, match='memory_changed'):
        wrapper.complete(speech, [], [], [])
    assert backend.complete.call_count == 1


def test_openai_recall_response_schema_allows_only_an_answer():
    _, speech, service, _ = speech_fixture()
    payloads = []

    def transport(url, headers, payload, timeout):
        payloads.append(payload)
        if len(payloads) == 1:
            output = {'type': 'function_call', 'name': STORY_RECALL_TOOL.name, 'arguments': '{}'}
        else:
            output = {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                'type': 'message', 'message': '전시를 보러 갔어요.', 'reason': 'story_recalled',
                'confidence': 1.0,
            })}]}
        return {'status': 'completed', 'output': [output]}

    backend = OpenAIResponsesProvider(api_key='test-only-key', model='test-model', transport=transport)
    wrapper = StoryMemoryProvider(backend, service, on_demand_speech=True)
    answer = wrapper.complete(speech, [], [], [TOOL_SPECS['request_patrol']],
                              memory_context={'enabled': False})
    assert answer.decision.message == '전시를 보러 갔어요.'
    assert answer.memory_proposal is None
    first, second = payloads
    assert 'memory_proposal' in first['text']['format']['schema']['properties']
    assert 'memory_proposal' not in second['text']['format']['schema']['properties']
    assert 'tools' not in second and 'tool_choice' not in second
    assert 'exhibition' in second['input']
