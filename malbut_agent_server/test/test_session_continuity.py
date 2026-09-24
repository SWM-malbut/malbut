"""Persisted dialogue time, wake/sleep independence and explicit new starts."""

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.speech_dialogue import DialogueWorker, starts_new_conversation
from test_personal_memory_flow import ScriptProvider
from test_speech_dialogue import RuntimeFactory, collect, wait_until


def _factory(database, now):
    factory = RuntimeFactory(database_path=str(database))

    def timed_runtime():
        runtime = factory()
        runtime.conversation_store._clock = lambda: now[0]
        runtime.conversation_store.semantic_context = True
        return runtime

    return factory, timed_runtime


@pytest.mark.parametrize('gap,resumed', [(3599, True), (3600, False), (3601, False)])
def test_restart_uses_elapsed_wall_time_and_restores_history(tmp_path, gap, resumed):
    now = [1000.0]
    database = tmp_path / 'dialogue.sqlite3'
    first_factory, first_runtime = _factory(database, now)
    first_worker = DialogueWorker(first_runtime, 'speaker')
    try:
        first_worker.submit('first', '친구의 고양이 이름은 나비야')
        first = collect(first_worker, 1)[0]
        before = first_factory.runtime.conversation_store.get(
            'speaker', first['conversation_id'],
        )
    finally:
        first_worker.close()

    now[0] += gap
    second_factory, second_runtime = _factory(database, now)
    second_worker = DialogueWorker(second_runtime, 'speaker')
    try:
        wait_until(lambda: second_worker.ready)
        if resumed:
            assert second_factory.runtime.conversation_store.get(
                'speaker', first['conversation_id'],
            ) == before
        second_worker.submit('second', '이름이 뭐였지?')
        second = collect(second_worker, 1)[0]
        assert second['kind'] == 'answer'
        assert (second['conversation_id'] == first['conversation_id']) is resumed
        history = second_factory.provider.calls[0][1]
        assert [turn.user_content for turn in history] == (
            ['친구의 고양이 이름은 나비야'] if resumed else []
        )
        # Expiry never deletes the original archived turn.
        assert second_factory.runtime.conversation_store._connection.execute(
            'SELECT user_content FROM conversation_turns WHERE conversation_id = ?',
            (first['conversation_id'],),
        ).fetchone()[0] == '친구의 고양이 이름은 나비야'
    finally:
        second_worker.close()


def test_wake_sleep_and_restart_without_dialogue_do_not_extend_continuity(tmp_path):
    now = [1000.0]
    database = tmp_path / 'dialogue.sqlite3'
    factory, runtime_factory = _factory(database, now)
    worker = DialogueWorker(runtime_factory, 'speaker')
    try:
        worker.submit('first', '처음 이야기')
        first = collect(worker, 1)[0]
        now[0] += 1800
        # STT returns to wake listening without closing the Agent worker.
        worker.submit('wake-again', '아까 이야기 이어가자')
        second = collect(worker, 1)[0]
        assert second['conversation_id'] == first['conversation_id']
    finally:
        worker.close()

    now[0] += 3599
    factory, runtime_factory = _factory(database, now)
    worker = DialogueWorker(runtime_factory, 'speaker')
    try:
        wait_until(lambda: worker.ready)
        now[0] += 1
        worker.submit('after-hour', '새 이야기')
        third = collect(worker, 1)[0]
        assert third['kind'] == 'answer'
        assert third['conversation_id'] != second['conversation_id']
        assert factory.provider.calls[-1][1] == []
    finally:
        worker.close()


def test_explicit_new_start_archives_old_context_and_does_not_match_quotation():
    factory = RuntimeFactory()
    worker = DialogueWorker(factory, 'speaker')
    try:
        worker.submit('first', '오늘은 짧게 답해줘')
        first = collect(worker, 1)[0]
        worker.submit('quote', '친구가 새로 시작하자고 했어')
        quoted = collect(worker, 1)[0]
        assert quoted['conversation_id'] == first['conversation_id']
        worker.submit('reset', '새로 시작하자!')
        reset = collect(worker, 1)[0]
        assert reset['kind'] == 'answer'
        assert reset['conversation_id'] != first['conversation_id']
        assert factory.provider.calls[-1][1] == []
        old = factory.runtime.conversation_store.snapshot(
            'speaker', first['conversation_id'],
        )
        assert old.session.status == 'closed'
        assert [turn.user_content for turn in old.turns] == [
            '오늘은 짧게 답해줘', '친구가 새로 시작하자고 했어',
        ]
    finally:
        worker.close()


def test_resume_is_user_isolated_and_does_not_count_reads_as_dialogue():
    now = [1000.0]
    store = SQLiteConversationStore(':memory:', clock=lambda: now[0])
    try:
        first = store.resume_or_create('first-user')
        other = store.resume_or_create('other-user')
        now[0] += 3599
        assert store.resume_or_create('first-user') == first
        assert store.resume_or_create('other-user') == other
        now[0] += 1
        assert store.resume_or_create('first-user').conversation_id != first.conversation_id
        assert store.get('other-user', other.conversation_id).status == 'expired'
    finally:
        store.close()


@pytest.mark.parametrize('text', [
    '새로 시작하자. 내일 날씨 어때?',
    ' 새로 시작해 주세요! 내일 날씨 어때?',
    '대화를 새로 시작하자。내일 날씨 어때?',
    '새로 시작하자',
])
def test_direct_opening_restart_command_accepts_a_followup_request(text):
    assert starts_new_conversation(text)


@pytest.mark.parametrize('text', [
    '"새로 시작하자." 내일 날씨 어때?',
    '“새로 시작하자.”라는 말은 무슨 뜻이야?',
    '친구가 새로 시작하자고 했어. 내일 날씨 어때?',
    '새로 시작하자고 했다는 말을 들었어.',
    '새로 시작하자. 라고 말하면 돼?',
    '새로 시작하지 마. 내일 날씨 어때?',
    '새로 시작하자는 뜻은 아니야. 내일 날씨 어때?',
    '내일 날씨 어때? 새로 시작하자.',
    '그 친구가 말했어. 새로 시작하자.',
])
def test_quoted_reported_negated_or_middle_restart_does_not_change_session(text):
    assert not starts_new_conversation(text)


def test_opening_restart_forwards_weather_without_old_context_or_temporary_settings():
    factory = RuntimeFactory(ScriptProvider())
    defaults = {'tone': '편안한 존댓말', 'address': '기본호칭',
                'length': '자세하게', 'initiative': '자연스럽게 주고받자'}

    def configured_runtime():
        runtime = factory()
        runtime.personal_memory.set_initial_settings('speaker', defaults)
        runtime.weather_executor = lambda request_id: {'status': 'unknown_location'}
        return runtime

    worker = DialogueWorker(configured_runtime, 'speaker')
    try:
        worker.submit('consent-question', '개인화 켜줘')
        collect(worker, 1)
        worker.submit('consent-answer', '네')
        collect(worker, 1)
        runtime = factory.runtime
        assert runtime.memory_store.policy_state('speaker')['enabled']
        remembered = runtime.memory_store.add('speaker', '날씨는 서울 기준을 선호한다.')
        worker.submit('temporary', '오늘은 반말로 말해줘. 짧게 답해줘')
        previous = collect(worker, 1)[0]
        assert factory.provider.calls[-1]['context']['response_settings']['tone'] == '친근한 반말'
        assert factory.provider.calls[-1]['context']['response_settings']['length'] == '짧고 간단하게'

        combined = '새로 시작하자. 내일 날씨 어때?'
        worker.submit('restart-and-weather', combined)
        reply = collect(worker, 1)[0]
        assert reply['kind'] == 'answer'
        assert reply['conversation_id'] != previous['conversation_id']
        call = factory.provider.calls[-1]
        assert call['request'].utterance == combined
        assert 'get_weather' in call['request'].available_tools
        assert call['history'] == [] and call['summary'] is None
        assert call['context']['response_settings'] == dict(defaults, response_mode='natural')
        assert runtime.personal_memory.initial_settings('speaker') == defaults
        assert runtime.memory_store.policy_state('speaker')['enabled']
        assert remembered.id in {item.id for item in runtime.memory_store.list_for_user('speaker')}
        assert runtime.conversation_store.get(
            'speaker', previous['conversation_id'],
        ).status == 'closed'
        assert runtime.conversation_store.list_turns(
            'speaker', reply['conversation_id'],
        )[0].user_content == combined
    finally:
        worker.close()


def test_duplicate_restart_reuses_stored_response_before_any_session_change():
    factory = RuntimeFactory()
    worker = DialogueWorker(factory, 'speaker')
    try:
        worker.submit('initial', '이전 이야기')
        collect(worker, 1)
        worker.submit('same-restart-id', '새로 시작하자. 내일 날씨 어때?')
        first = collect(worker, 1)[0]
        assert first['kind'] == 'answer'
        store = factory.runtime.conversation_store
        before = store.get('speaker', first['conversation_id'])
        session_count = store._connection.execute(
            'SELECT COUNT(*) FROM conversation_sessions',
        ).fetchone()[0]
        provider_calls = len(factory.provider.calls)

        worker.submit('same-restart-id', '새로 시작하자. 내일 날씨 어때?')
        duplicate = collect(worker, 1)[0]
        assert duplicate == first
        assert store.get('speaker', first['conversation_id']) == before
        assert len(factory.provider.calls) == provider_calls
        assert len(store.list_turns('speaker', first['conversation_id'])) == 1

        # Reusing the ID with changed input must fail without resetting context.
        worker.submit('same-restart-id', '새로 시작하자. 다른 질문이야.')
        conflict = collect(worker, 1)[0]
        assert conflict['kind'] == 'error'
        assert conflict['conversation_id'] == first['conversation_id']
        assert store.get('speaker', first['conversation_id']) == before
        assert len(factory.provider.calls) == provider_calls
        assert store._connection.execute(
            'SELECT COUNT(*) FROM conversation_sessions',
        ).fetchone()[0] == session_count
    finally:
        worker.close()


def test_worker_user_switch_restores_only_own_history_and_settings(tmp_path):
    database = str(tmp_path / 'shared-users.sqlite3')
    sessions, settings = {}, {}
    for phase, user in enumerate(('alice', 'bob', 'alice')):
        factory = RuntimeFactory(ScriptProvider(), database)
        worker = DialogueWorker(factory, user)

        def say(utterance_id, text):
            assert worker.submit(utterance_id, text)
            response = collect(worker, 1)[0]
            assert response['kind'] == 'answer'
            return response

        try:
            if phase == 0:
                say('setup', '초기 설정 말투=친근한 반말, 호칭=앨리스, '
                    '답변 길이=자세하게, 대화 중 적극성=주로 들어줘')
                sessions[user] = say('temporary', '오늘은 존댓말로 말해줘. 짧게 답해줘')['conversation_id']
                settings[user] = factory.provider.calls[-1]['context']['response_settings']
                assert settings[user]['tone'] == '편안한 존댓말'
                assert settings[user]['length'] == '짧고 간단하게'
            elif phase == 1:
                # The same utterance IDs belong to a different user namespace.
                fresh = say('temporary', '안녕')
                assert fresh['conversation_id'] != sessions['alice']
                call = factory.provider.calls[-1]
                assert call['history'] == []
                assert call['context']['response_settings']['address'] == ''
                say('setup', '초기 설정 말투=편안한 존댓말, 호칭=밥, '
                    '답변 길이=짧고 간단하게, 대화 중 적극성=적극적으로 이어줘')
                sessions[user] = say('new', '새로 시작하자. 안녕')['conversation_id']
                assert sessions[user] != fresh['conversation_id']
                assert factory.provider.calls[-1]['history'] == []
                settings[user] = factory.provider.calls[-1]['context']['response_settings']
                assert settings[user]['address'] == '밥'
                assert settings[user]['initiative'] == '적극적으로 이어줘'
            else:
                resumed = say('back', '아까 이야기 계속하자')
                assert resumed['conversation_id'] == sessions[user]
                call = factory.provider.calls[-1]
                assert call['context']['response_settings'] == settings[user]
                assert call['history'] and all(turn.user_id == user for turn in call['history'])
                restarted = say('new', '새로 시작하자. 안녕')
                assert restarted['conversation_id'] != sessions[user]
                call = factory.provider.calls[-1]
                assert call['history'] == []
                assert call['context']['response_settings'] == {
                    'tone': '친근한 반말', 'address': '앨리스', 'length': '자세하게',
                    'initiative': '주로 들어줘', 'response_mode': 'natural',
                }
                assert factory.runtime.conversation_store.get('bob', sessions['bob']).status == 'active'
                assert factory.runtime.personal_memory.initial_settings('bob')['address'] == '밥'
        finally:
            worker.close()
