"""Foreground story recall does not bypass policy or authority boundaries."""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from malbut_agent_server.providers.base import AgentProvider, accepts_memory_context
from malbut_agent_server.schemas import ValidationError
from malbut_agent_server.story_memory_provider import StoryMemoryProvider


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
