"""Answer-only local-time formatting without rewriting or extending provider replies."""

import copy

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.patrol_memory import PatrolMemoryStore
from malbut_agent_server.patrol_query import _answer_evidence, configure_patrol_queries
from malbut_agent_server.schemas import AgentDecision, ProviderResult
from malbut_agent_server.speech_dialogue import DialogueWorker
from test_patrol_memory import ANALYSIS, ARGS, QueryProvider, capture
from test_speech_dialogue import collect


CAPTURE = '2026-11-11T04:11:51.469281+09:00'
DISPLAY = '2026년 11월 11일 04시 11분 51초 (한국 시각)'
# Retain the actual historical failure as an explicit limit: input normalization
# cannot repair a provider that ignores it and returns an independently wrong clock.
WRONG_REPLY = ('마지막 관찰에서 리모컨의 실제 구역은 확인되지 않았어요'
               '(2026년 11월 11일 13시 11분 51초, 한국 시간). '
               '사진에서는 소파 위에 보였고, 가상 환경의 순찰 기록입니다.')


@pytest.mark.parametrize('raw,expected', [
    (CAPTURE, DISPLAY),
    ('2026-11-10T19:11:51.469281Z', DISPLAY),
    ('2026-11-10T19:11:51+00:00', DISPLAY),
    ('2026-11-10T15:11:51-04:00', DISPLAY),
    ('2026-11-11T00:56:51+05:45', DISPLAY),
    ('2026-12-31T15:00:00Z', '2027년 01월 01일 00시 00분 00초 (한국 시각)'),
    ('2026-11-11T03:00:00+00:00', '2026년 11월 11일 12시 00분 00초 (한국 시각)'),
])
def test_answer_copy_normalizes_each_instant_and_keeps_original_metadata(raw, expected):
    original = {'observations': [{'observation_id': 'new', 'captured_at': raw,
                                  'objects': [], 'simulated': True}],
                'last_confirmed_observations': [{'observation_id': 'old',
                                                 'captured_at': '2026-11-10T09:00:00+09:00',
                                                 'region': {'name': '부엌'}}],
                'queried_at': '2026-11-11T19:00:00Z'}
    saved = copy.deepcopy(original)
    result = _answer_evidence(original)
    assert result['observations'][0]['captured_at'] == expected
    assert result['observations'][0]['observation_id'] == 'new'
    assert result['observations'][0]['simulated']
    assert result['last_confirmed_observations'] == [dict(
        saved['last_confirmed_observations'][0],
        captured_at='2026년 11월 10일 09시 00분 00초 (한국 시각)')]
    assert result['queried_at'] == '2026년 11월 12일 04시 00분 00초 (한국 시각)'
    assert original == saved


@pytest.mark.parametrize('invalid', [None, '', '2026-11-11T04:11:51', 'broken', {}])
def test_unknown_time_does_not_borrow_another_observations_clock(invalid):
    result = _answer_evidence({
        'observations': [{'captured_at': invalid, 'objects': []}],
        'last_confirmed_observations': [{'captured_at': CAPTURE}],
        'queried_at': invalid})
    assert result['observations'][0]['captured_at'] is None
    assert result['queried_at'] is None
    assert result['last_confirmed_observations'][0]['captured_at'] == DISPLAY


@pytest.mark.parametrize('reply', [
    '마지막 순찰 사진에서는 소파 위에 보였어요. 실제 구역은 미확정이고 가상 기록이에요.',
    '그 사진은 한국 시각 2026년 11월 11일 오전 4시 11분 51초에 찍었어요.',
    WRONG_REPLY,
])
def test_normal_voice_reply_is_exactly_preserved_after_localized_input(tmp_path, reply):
    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    path, _ = capture(tmp_path, captured_at=CAPTURE, simulated=True)
    store.ingest()
    store.complete(path.stem, ANALYSIS, 'fixture')

    class ReplyProvider(QueryProvider):
        def complete(self, request, memories, conversation_turns, tools,
                     conversation_summary=None, *, memory_context=None):
            result = super().complete(request, memories, conversation_turns, tools,
                                      conversation_summary, memory_context=memory_context)
            if 'patrol_observations_untrusted' not in memory_context:
                return result
            observation, = memory_context['patrol_observations_untrusted']['observations']
            assert observation['observation_id'] == path.stem
            assert observation['captured_at'] == DISPLAY
            assert observation['objects'][0]['image_location'] == '소파 위'
            assert observation['objects'][0]['spatial']['status'] == 'unknown'
            assert observation['simulated']
            return ProviderResult(AgentDecision('message', reply), 'fixture', 'fixed', 1)

    provider = ReplyProvider()

    def factory():
        runtime = build_orchestrator(Settings(database_path=':memory:'), http_server=False)
        runtime.provider = provider
        configure_patrol_queries(runtime, store)
        return runtime

    worker = DialogueWorker(factory, 'owner')
    try:
        assert worker.submit('patrol-time', '지난 순찰 사진에서 리모컨을 어디서 봤어?')
        response, = collect(worker, 1)
        assert response['text'].encode() == reply.encode()
        assert len(provider.calls) == 2 and not provider.calls[1][1]
        assert provider.calls[0][2]['patrol_query_clock']['timezone'] == 'Asia/Seoul'
    finally:
        worker.close()
    # Raw APIs and half-open timezone-aware filters continue to use original instants.
    assert not store.search('owner', dict(ARGS, since='2026-11-10T19:11:52Z'))['observations']
    item, = store.search('owner', dict(ARGS, until='2026-11-11T04:11:52+09:00'))['observations']
    assert item['captured_at'] == CAPTURE
    with pytest.raises(PermissionError):
        store.search('another-owner', ARGS)
