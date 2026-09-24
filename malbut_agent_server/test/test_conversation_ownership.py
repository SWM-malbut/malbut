"""Real process death recovery cannot remove an inference still running."""

import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import threading

import pytest

from malbut_agent_server.conversation import ConversationConflictError
from malbut_agent_server.schemas import AgentRequest
from test_speech_dialogue import FixedProvider, RuntimeFactory


def _request(conversation_id, number, text):
    return AgentRequest.from_dict({
        'request_id': f'request-{number}', 'user_id': 'speaker',
        'conversation_id': conversation_id, 'turn_id': f'turn-{number}',
        'utterance': text, 'robot_state': {}, 'available_tools': [],
    })


@pytest.mark.parametrize('restart_before_pending', [False, True])
def test_killed_inference_recovers_prior_session_clock_history_and_style(
    tmp_path, restart_before_pending,
):
    database = str(tmp_path / 'crashed.sqlite3')
    script = '''
import sys, time
sys.path.insert(0, sys.argv[2])
from test_conversation_ownership import _request
from test_speech_dialogue import RuntimeFactory
factory = RuntimeFactory(database_path=sys.argv[1])
runtime = factory()
session = runtime.conversation_store.resume_or_create('speaker')
runtime.handle(_request(session.conversation_id, 1, '반말로 말해줘'))
print('ready', flush=True)
sys.stdin.readline()
def blocked(request, history):
    print('pending', flush=True)
    time.sleep(60)
factory.provider.respond = blocked
runtime.handle(_request(session.conversation_id, 2, '진행 중인 질문'))
'''
    process = subprocess.Popen(
        [sys.executable, '-c', script, database, str(Path(__file__).parent)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    runtime = None
    try:
        assert select.select([process.stdout], [], [], 5)[0], 'child did not start'
        assert process.stdout.readline().strip() == 'ready'
        if restart_before_pending:
            factory = RuntimeFactory(database_path=database)
            runtime = factory()
            assert not runtime._deferred_conversation_recovery
        process.stdin.write('start\n')
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0], 'child did not reserve turn'
        assert process.stdout.readline().strip() == 'pending'
        with sqlite3.connect(database) as connection:
            before = connection.execute(
                'SELECT conversation_id, updated_at, expires_at FROM conversation_sessions',
            ).fetchone()
            settings = connection.execute(
                'SELECT settings_json FROM session_response_settings',
            ).fetchone()[0]
            assert json.loads(settings)['tone'] == '친근한 반말'
            assert connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE status='pending'",
            ).fetchone()[0] == 1
        process.kill()
        process.wait(timeout=5)
        if runtime is None:
            factory = RuntimeFactory(database_path=database)
            runtime = factory()
        try:
            session = runtime.conversation_store.resume_or_create('speaker')
            assert (session.conversation_id, session.updated_at, session.expires_at) == before
            assert runtime.conversation_store._connection.execute(
                'SELECT settings_json FROM session_response_settings',
            ).fetchone()[0] == settings
            runtime.handle(_request(session.conversation_id, 3, '계속 이야기하자'))
            assert [turn.user_content for turn in factory.provider.calls[0][1]] == [
                '반말로 말해줘',
            ]
            assert [(turn.turn_id, turn.request_id, turn.ordinal)
                    for turn in runtime.conversation_store.list_turns(
                        'speaker', session.conversation_id,
                    )] == [('turn-1', 'request-1', 1), ('turn-3', 'request-3', 2)]
            assert runtime.conversation_store._connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE status='pending'",
            ).fetchone()[0] == 0
        finally:
            runtime.close()
            runtime = None
    finally:
        if runtime is not None:
            runtime.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


def test_startup_and_deferred_recovery_preserve_another_live_inference(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def blocked(request, history):
        entered.set()
        assert release.wait(5)
        return FixedProvider.answer(request, history)

    database = str(tmp_path / 'live.sqlite3')
    first_factory = RuntimeFactory(FixedProvider(blocked), database)
    first = first_factory()
    second = None
    session = first.conversation_store.resume_or_create('speaker')
    results, errors = [], []

    def infer():
        try:
            results.append(first.handle(_request(session.conversation_id, 1, '첫 질문')))
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=infer)
    try:
        worker.start()
        assert entered.wait(5)
        second_factory = RuntimeFactory(database_path=database)
        second = second_factory()
        assert second._deferred_conversation_recovery == {('speaker', session.conversation_id)}
        assert second.conversation_store._connection.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE status='pending'",
        ).fetchone()[0] == 1
        with pytest.raises(ConversationConflictError, match='already in progress'):
            second.handle(_request(session.conversation_id, 2, '후속 질문'))
        assert second_factory.provider.calls == []
        assert second._deferred_conversation_recovery == {('speaker', session.conversation_id)}
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert len(results) == 1 and errors == []
        second.handle(_request(session.conversation_id, 2, '후속 질문'))
        assert not second._deferred_conversation_recovery
        assert [turn.user_content for turn in second_factory.provider.calls[0][1]] == ['첫 질문']
    finally:
        release.set()
        worker.join(5)
        first.close()
        if second is not None:
            second.close()


@pytest.mark.parametrize('file_backed', [False, True])
def test_nested_request_preserves_own_active_pending_turn(tmp_path, file_backed):
    database = str(tmp_path / 'nested.sqlite3') if file_backed else ':memory:'
    factory = RuntimeFactory(database_path=database)
    runtime = factory()
    session = runtime.conversation_store.resume_or_create('speaker')

    def nested(request, history):
        with pytest.raises(ConversationConflictError, match='already in progress'):
            runtime.handle(_request(session.conversation_id, 2, '중첩 요청'))
        return FixedProvider.answer(request, history)

    factory.provider.respond = nested
    try:
        runtime.handle(_request(session.conversation_id, 1, '원래 요청'))
        turns = runtime.conversation_store.list_turns('speaker', session.conversation_id)
        assert [(turn.turn_id, turn.user_content) for turn in turns] == [
            ('turn-1', '원래 요청'),
        ]
        assert len(factory.provider.calls) == 1
    finally:
        runtime.close()
