"""Everyday topics, durable repetition limits, and real dialogue context handoff."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json

import pytest

from malbut_agent_server.patrol_conversation import (
    CHAT_COOLDOWN_S, TOPIC_REPEAT_S, EncounterGate, PatrolConversationTopics,
    conversation_preference, ends_conversation,
)
from malbut_agent_server.patrol_memory import PatrolMemoryStore
from malbut_agent_server.speech_dialogue import DialogueWorker
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from test_patrol_memory import capture
from test_speech_dialogue import RuntimeFactory, collect


NOW = datetime(2026, 10, 29, 3, tzinfo=timezone.utc).timestamp()


def remember(store, name='화분', *, at=NOW - 60, confidence='high', kind='object'):
    path, _ = capture(store.directory, captured_at=datetime.fromtimestamp(
        at, timezone.utc).isoformat())
    store.ingest()
    store.complete(path.stem, {
        'summary': '시뮬레이션 관찰', 'objects': [{'name': name, 'kind': kind,
            'location': '중앙', 'confidence': confidence,
            'spatial': {'status': 'unknown', 'region': None,
                        'method': 'registered_rgbd', 'reason': 'insufficient_depth'}}],
        'uncertainties': [],
    }, 'fixture')
    return path.stem


@pytest.fixture
def memory(tmp_path):
    store = PatrolMemoryStore(tmp_path, 'owner')
    PatrolConversationTopics(store).set_enabled(True)
    return store


def test_unknown_region_can_start_smalltalk_without_photos_or_inferred_ownership(memory):
    identity = remember(memory)
    opener = PatrolConversationTopics(memory, clock=lambda: NOW).reserve()
    assert opener.topic == 'plants' and opener.observation_id == identity
    assert opener.text == '식물 키우는 거 좋아하세요?'
    assert opener.context()['person_identity'] == 'unknown'
    assert not list(memory.directory.rglob('*.jpg'))
    assert not list(memory.directory.rglob('*.npy'))


@pytest.mark.parametrize('name,kind,confidence', [
    ('침대', 'object', 'high'), ('사람', 'person', 'high'), ('약통', 'object', 'high'),
    ('화분', 'object', 'uncertain'), ('화분을 든 사람', 'person', 'high'),
    ('책상', 'object', 'high'), ('책장', 'object', 'high'), ('책임자', 'object', 'high'),
    ('기타 물체', 'object', 'high'),
])
def test_private_uncertain_and_unrelated_objects_do_not_become_topics(
        memory, name, kind, confidence):
    remember(memory, name, kind=kind, confidence=confidence)
    assert PatrolConversationTopics(memory, clock=lambda: NOW).reserve() is None


@pytest.mark.parametrize('name,topic', [('펼쳐진 책', 'books'), ('기타', 'music'),
                                      ('요가 매트', 'exercise')])
def test_supported_everyday_topics(memory, name, topic):
    remember(memory, name)
    assert PatrolConversationTopics(memory, clock=lambda: NOW).reserve().topic == topic


@pytest.mark.parametrize('at', [NOW - 86401, NOW + 1])
def test_old_and_future_observations_do_not_sound_recent(memory, at):
    remember(memory, at=at)
    assert PatrolConversationTopics(memory, clock=lambda: NOW).reserve() is None


@pytest.mark.parametrize('fault', ['analysis', 'metadata', 'receipt', 'status'])
def test_unverified_records_are_never_topics(memory, fault):
    identity = remember(memory)
    with memory.connect() as connection:
        if fault in ('analysis', 'metadata'):
            connection.execute(f'UPDATE observations SET {fault}=?', ('{}',))
        elif fault == 'receipt':
            connection.execute('DELETE FROM patrol_observation_evidence')
        else:
            connection.execute("UPDATE observations SET status='failed' WHERE id=?", (identity,))
    assert PatrolConversationTopics(memory, clock=lambda: NOW).reserve() is None


def test_cooldown_and_topic_history_survive_reopen_and_clock_rollback(memory):
    remember(memory)
    now = [NOW]
    topics = PatrolConversationTopics(memory, clock=lambda: now[0])
    assert topics.reserve().topic == 'plants'
    remember(memory, '책', at=NOW - 30)
    reopened = PatrolMemoryStore(memory.directory, 'owner')
    topics = PatrolConversationTopics(reopened, clock=lambda: now[0])
    assert topics.reserve() is None
    now[0] -= 60
    assert topics.reserve() is None
    now[0] = NOW + CHAT_COOLDOWN_S + 1
    assert topics.reserve().topic == 'books'
    now[0] += CHAT_COOLDOWN_S + 1
    assert topics.reserve() is None  # Different photo, same topic is still suppressed.
    now[0] = NOW + TOPIC_REPEAT_S + 1
    remember(memory, at=now[0] - 60)
    assert topics.reserve().topic == 'plants'


def test_two_controllers_cannot_reserve_two_topics_at_once(memory):
    remember(memory)
    remember(memory, '책')
    with ThreadPoolExecutor(2) as pool:
        choices = list(pool.map(lambda _: PatrolConversationTopics(
            memory, clock=lambda: NOW).reserve(), range(2)))
    assert sum(item is not None for item in choices) == 1


def test_explicit_opt_out_survives_reopen_until_user_allows_it(memory):
    remember(memory)
    PatrolConversationTopics(memory).set_enabled(False)
    reopened = PatrolConversationTopics(PatrolMemoryStore(memory.directory, 'owner'),
                                       clock=lambda: NOW)
    assert reopened.reserve() is None
    reopened.set_enabled(True)
    assert reopened.reserve().topic == 'plants'
    assert conversation_preference('먼저 말 걸지 마') is False
    assert conversation_preference('먼저 말 걸어도 돼') is True
    assert conversation_preference('친구가 먼저 말 걸지 마라고 했어') is None


def test_gate_requires_stability_idle_time_and_explicit_departure():
    now = [0.]
    gate = EncounterGate(clock=lambda: now[0])
    for second in range(31):
        now[0] = float(second)
        gate.observe(True)
        assert gate.ready() == (second >= 30)
    gate.used = True
    for second in range(31, 45):
        now[0] = second
        gate.observe(None)
    assert gate.used  # More people joining is not an empty scene/new encounter.
    now[0] = 100
    gate.observe(True)
    assert not gate.ready()  # A sensor outage is not a new encounter.
    for second in range(101, 112):
        now[0] = second
        gate.observe(False)
    assert not gate.used
    for second in range(112, 116):
        now[0] = second
        gate.observe(True)
    assert gate.ready()
    gate.activity()
    assert not gate.ready()
    now[0] += 40
    assert not gate.ready()  # Last detection has expired.


def test_spontaneous_topics_require_explicit_opt_in(tmp_path):
    store = PatrolMemoryStore(tmp_path, 'owner')
    remember(store)
    topics = PatrolConversationTopics(store, clock=lambda: NOW)
    assert topics.reserve() is None
    topics.set_enabled(True)
    assert topics.reserve().topic == 'plants'


@pytest.mark.parametrize('text,ended', [('나중에', True), ('아니요.', False), ('괜찮아요', False), ('아니', False),
                                       ('말 걸지 마세요', True), ('요즘 바빠서 운동을 못 했어', False),
                                       ('응', False)])
def test_respect_declines_without_misreading_an_ordinary_answer(text, ended):
    assert ends_conversation(text) is ended


def test_first_reply_knows_what_robot_asked_without_a_fake_user_turn(memory):
    remember(memory)
    context = PatrolConversationTopics(memory, clock=lambda: NOW).reserve().context()
    factory = RuntimeFactory()
    worker = DialogueWorker(factory, 'owner')
    try:
        assert worker.submit('first', '응 좋아해', opener_context=context)
        collect(worker, 1)
        request, history, _ = factory.provider.calls[0]
        assert history == []
        assert request.utterance == '응 좋아해'
        assert request.scene_context['conversation_opener'] == context
        payload = OpenAIResponsesProvider('fixture', 'fixture').build_payload(request, [], [], [])
        prompt = json.loads(payload['input'].split('\n', 1)[1])
        assert prompt['scene_context_untrusted']['conversation_opener'] == context
        assert '위치 확인을 요구하지 않는다' in payload['instructions']
        assert worker.submit('next', '요즘은 산책도 해')
        collect(worker, 1)
        request, history, _ = factory.provider.calls[1]
        assert request.scene_context is None
        assert [turn.user_content for turn in history] == ['응 좋아해']
    finally:
        worker.close()


def test_explicit_new_conversation_drops_old_opener_context():
    factory = RuntimeFactory()
    worker = DialogueWorker(factory, 'owner')
    try:
        assert worker.submit('new', '새 대화 시작해줘', opener_context={'topic': 'books'})
        collect(worker, 1)
        assert factory.provider.calls[0][0].scene_context is None
    finally:
        worker.close()
