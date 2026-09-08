"""Keep memory replies distinct from robot confirmation and replay."""

import sqlite3
from types import SimpleNamespace

import pytest

from malbut_agent_server.adapters.outbound import SQLiteActionRepository
from malbut_agent_server.conversation import ConversationConflictError
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import MemoryChangedError
from test_text_turn import _request, _runtime


@pytest.fixture
def text_runtime(tmp_path):
    """Exercise text routing with action creation enabled and no worker."""
    service, provider, resolver, store, memory, database = _runtime(tmp_path)
    actions = SQLiteActionRepository(database)
    service.create_robot_actions = True
    store.create('user-1', 'conversation-1')
    value = SimpleNamespace(
        service=service, provider=provider, resolver=resolver, store=store,
        memory=memory, database=database, actions=actions,
    )
    yield value
    actions.close()
    store.close()
    memory.close()


def send(runtime, key, text, conversation='conversation-1'):
    """Send a content-bound request to the authenticated text API service."""
    return runtime.service.handle(
        user_id='user-1',
        value=_request(key, 'turn-' + key, text, conversation),
    )


def action_count(runtime):
    """Read the durable action ledger without executing a robot command."""
    with sqlite3.connect(runtime.database) as connection:
        return connection.execute(
            'SELECT COUNT(*) FROM robot_actions',
        ).fetchone()[0]


def consent_source(text):
    """Represent a trusted current control outside the text request thread."""
    return {
        'conversation_id': 'control-room', 'session_instance_id': 'control',
        'generation': 1, 'turn_id': 'control', 'request_id': 'control',
        'text': text,
    }


def enable(runtime):
    """Approve personalization only after the full server consent question."""
    first = send(runtime, 'enable', '개인화 켜줘')
    assert first['decision']['type'] == 'clarification'
    second = send(runtime, 'enable-yes', '네')
    assert runtime.memory.policy_state('user-1')['enabled'] is True
    return second


def question_template(runtime):
    """Capture a valid memory question before creating the robot candidate."""
    send(runtime, 'memory-question', '개인화 켜줘')
    question = runtime.service.orchestrator.personal_memory.pending_question(
        'user-1', 'conversation-1',
    )
    assert question is not None
    proposal = send(runtime, 'robot-proposal', '거실로 가줘')
    assert proposal['status'] == 'awaiting_confirmation'
    return question, proposal


def install_question(runtime, question, *, memory_revision=None):
    """Reproduce simultaneous valid questions from delayed/imported state."""
    session = runtime.store.get('user-1', 'conversation-1')
    value = dict(question)
    value['revision'] = session.revision
    if memory_revision is not None:
        value['memory_revision'] = memory_revision
    with sqlite3.connect(runtime.database) as connection:
        connection.execute(
            '''INSERT OR REPLACE INTO memory_questions (
                user_id, conversation_id, session_instance_id, generation,
                revision, kind, proposal_json, source_json, memory_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            tuple(value[key] for key in (
                'user_id', 'conversation_id', 'session_instance_id',
                'generation', 'revision', 'kind', 'proposal_json',
                'source_json', 'memory_revision',
            )),
        )


def test_memory_yes_reaches_memory_policy_not_robot_no_pending(text_runtime):
    """A memory-only confirmation must not become no_pending_confirmation."""
    result = enable(text_runtime)
    assert result['status'] == 'completed'
    assert '개인화를 시작' in result['decision']['message']
    assert action_count(text_runtime) == 0
    replay = send(text_runtime, 'enable-yes', '네')
    assert replay == result


def test_explicit_save_and_no_declines_memory_without_robot_claim(
    text_runtime,
):
    """Rejecting memory never consumes a robot-response namespace."""
    first = send(text_runtime, 'remember', '우리 강아지 이름은 두부야. 기억해줘')
    assert first['decision']['type'] == 'clarification'
    result = send(text_runtime, 'no', '아니요')
    assert result['status'] == 'completed'
    assert not text_runtime.memory.policy_state('user-1')['enabled']
    assert text_runtime.memory.list_for_user('user-1') == []
    assert text_runtime.store.has_agent_request('user-1', 'no') is True


def test_both_valid_questions_never_approve_a_robot_action(text_runtime):
    """Ambiguous yes discards approval authority instead of choosing a node."""
    question, proposal = question_template(text_runtime)
    install_question(text_runtime, question)
    result = send(text_runtime, 'ambiguous-yes', '네')
    assert result['decision']['type'] == 'clarification'
    assert '둘 다 승인하지' in result['decision']['message']
    assert not text_runtime.memory.policy_state('user-1')['enabled']
    assert action_count(text_runtime) == 0
    assert text_runtime.actions.find_by_confirmation(
        proposal['confirmation_request_id'],
    ) is None
    assert text_runtime.store.pending_confirmation(
        'user-1', 'conversation-1',
    ) is None
    assert send(text_runtime, 'ambiguous-yes', '네') == result


def test_explicit_memory_stop_bypasses_pending_robot_question(text_runtime):
    """A direct memory control is never mistaken for an unclear robot reply."""
    enable(text_runtime)
    send(text_runtime, 'robot-proposal', '거실로 가줘')
    stopped = send(text_runtime, 'stop-personalization', '개인화 중단해줘')
    assert stopped['status'] == 'completed'
    assert '개인화를 중단' in stopped['decision']['message']
    assert not text_runtime.memory.policy_state('user-1')['enabled']
    assert action_count(text_runtime) == 0
    assert text_runtime.store.pending_confirmation(
        'user-1', 'conversation-1',
    ) is None


def test_stale_memory_question_does_not_steal_valid_robot_reply(text_runtime):
    """Only an exact-current memory question participates in disambiguation."""
    question, proposal = question_template(text_runtime)
    install_question(text_runtime, question, memory_revision=999)
    result = send(text_runtime, 'robot-yes', '네')
    assert result['status'] == 'approved'
    assert action_count(text_runtime) == 1
    assert text_runtime.actions.find_by_confirmation(
        proposal['confirmation_request_id'],
    ) is not None
    assert not text_runtime.memory.policy_state('user-1')['enabled']


@pytest.mark.parametrize('state', ['pending', 'unrecognized', 'approved'])
def test_robot_response_cache_checks_original_memory_revision(
    text_runtime, state,
):
    """Every confirmation representation remains bound to its source turn."""
    send(text_runtime, 'robot-proposal', '거실로 가줘')
    key, text = 'robot-proposal', '거실로 가줘'
    if state == 'unrecognized':
        key, text = 'unsure', '글쎄'
        send(text_runtime, key, text)
    elif state == 'approved':
        key, text = 'robot-yes', '네'
        send(text_runtime, key, text)
    previous_actions = action_count(text_runtime)
    text_runtime.memory.add('user-1', '새로운 기억')
    with pytest.raises(MemoryChangedError):
        send(text_runtime, key, text)
    assert action_count(text_runtime) == previous_actions


def test_policy_change_immediately_before_confirmation_commit_blocks_action(
    text_runtime, monkeypatch,
):
    """Approval checks freshness under the same SQLite write lock."""
    send(text_runtime, 'robot-proposal', '거실로 가줘')
    original = text_runtime.store.resolve_confirmation
    other = SQLiteMemoryStore(text_runtime.database)

    def race(*args, **kwargs):
        other.set_personalization(
            'user-1', False, consent_source('개인화 중단'),
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(text_runtime.store, 'resolve_confirmation', race)
    try:
        with pytest.raises(MemoryChangedError):
            send(text_runtime, 'robot-yes', '네')
        assert action_count(text_runtime) == 0
        assert text_runtime.store.pending_confirmation(
            'user-1', 'conversation-1',
        ) is not None
    finally:
        other.close()


def test_new_memory_question_before_approval_commit_cannot_create_action(
    text_runtime, monkeypatch,
):
    """Recheck late valid questions inside confirmation resolution."""
    question, _proposal = question_template(text_runtime)
    original = text_runtime.store.resolve_confirmation

    def race(*args, **kwargs):
        install_question(text_runtime, question)
        return original(*args, **kwargs)

    monkeypatch.setattr(text_runtime.store, 'resolve_confirmation', race)
    with pytest.raises(ConversationConflictError, match='memory question'):
        send(text_runtime, 'robot-yes', '네')
    assert action_count(text_runtime) == 0
    monkeypatch.setattr(text_runtime.store, 'resolve_confirmation', original)
    retried = send(text_runtime, 'robot-yes', '네')
    assert retried['decision']['type'] == 'clarification'
    assert action_count(text_runtime) == 0


def test_independent_user_change_does_not_block_robot_confirmation(
    text_runtime,
):
    """Persistent revision checks use the source user's namespace."""
    send(text_runtime, 'robot-proposal', '거실로 가줘')
    text_runtime.memory.add('different-user', '다른 사람의 정보')
    result = send(text_runtime, 'robot-yes', '네')
    assert result['status'] == 'approved'
    assert action_count(text_runtime) == 1


def test_unrelated_conversation_memory_question_is_not_robot_consent(
    text_runtime,
):
    """Question matching includes the authenticated user and conversation."""
    text_runtime.store.create('user-1', 'memory-room')
    send(text_runtime, 'memory-question', '개인화 켜줘', 'memory-room')
    send(text_runtime, 'robot-proposal', '거실로 가줘')
    result = send(text_runtime, 'robot-yes', '네')
    assert result['status'] == 'approved'
    assert not text_runtime.memory.policy_state('user-1')['enabled']
    assert action_count(text_runtime) == 1
