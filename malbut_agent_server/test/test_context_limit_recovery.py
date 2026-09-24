"""Rejected raw input is retained without poisoning the next conversation."""

import json
import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.conversation import is_context_limit_response
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.providers.reliable import ContextBudgetExceeded, ReliableProvider
from malbut_agent_server.schemas import SpeechAgentRequest
from malbut_agent_server.summarization import SummaryResult
from test_semantic_prompting import _data
from test_personal_memory_flow import Flow, pet_fact, proposed


@pytest.mark.parametrize('prefix,suffix', [
    ('앞으로 존댓말로 말해줘. ', ''), ('', ' 기억해 줘'),
])
def test_oversized_turn_retains_raw_and_valid_context_across_restart_and_compaction(tmp_path, prefix, suffix):
    sent = []
    settings = Settings(provider='openai', openai_api_key='test-key',
                        database_path=str(tmp_path / 'conversation.sqlite3'))

    def counter(payload):
        data = _data(payload)
        text = data['current_user_utterance'] + ''.join(
            item['user'] + item['assistant']
            for item in data['conversation_history_untrusted']
        )
        return 1025 if len(text) > 2000 else 100

    def transport(_url, _headers, payload, _timeout):
        sent.append(_data(payload))
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [{
            'type': 'output_text', 'text': json.dumps({
                'type': 'message', 'message': '목요일 오후 3시 도서관에서 민수와 만나기로 했어.',
                'reason': 'answer', 'confidence': 1, 'memory_proposal': None,
            }),
        }]}]}

    def build():
        runtime = build_orchestrator(settings, http_server=False)
        runtime.context_compactor.close()
        runtime.context_compactor = None
        runtime.provider = ReliableProvider([OpenAIResponsesProvider(
            'test-key', 'offline-model', semantic_context=True,
            max_input_tokens=2048, token_counter=counter, transport=transport,
        )])
        return runtime

    def request(number, text):
        return SpeechAgentRequest.from_dict({
            'request_id': f'request-{number}', 'turn_id': f'turn-{number}',
            'user_id': 'user', 'conversation_id': 'conversation',
            'utterance': text, 'robot_state': {}, 'available_tools': [],
        })

    def reserve(store):
        return store.begin_turn('user', 'conversation', 'compaction', 'compaction',
                                'a' * 64, '압축 작업 검증')

    runtime = build()
    huge = prefix + '처리하지못한새정보' * 400 + suffix
    try:
        store = runtime.conversation_store
        store.create('user', 'conversation')
        runtime.handle(request(1, '반말로 말해줘. 민수와 목요일 오후 3시에 도서관에서 만나기로 했어.'))
        runtime.handle(request(2, '그 약속은 확정이야.'))
        begin = reserve(store)
        assert store.apply_compaction(begin.token, None, begin.history[:1], SummaryResult(
            '반말로 대화한다. 민수와 목요일 오후 3시에 도서관에서 만날 약속이 있다.',
            '{}', 'openai-semantic-v1',
        ))
        store.fail_turn(begin.token)
        saved = store.get_summary('user', 'conversation')

        result = runtime.handle(request(3, huge))
        assert result.decision.reason == 'conversation_context_limit'
        assert len(sent) == 2  # Rejected locally before transport, without retry.
        assert store.list_turns('user', 'conversation')[-1].user_content == huge
        assert store.get_summary('user', 'conversation') == saved
        assert is_context_limit_response(store.list_turns('user', 'conversation')[-1].response)
        runtime.close()
        runtime = build()
        store = runtime.conversation_store
        result = runtime.handle(request(4, '아까 원문에 있던 약속은 언제 어디서였지?'))
        assert result.decision.type == 'message'
        assert len(sent) == 3
        history = sent[-1]['conversation_history_untrusted']
        assert [item['ordinal'] for item in history] == [1, 2, 3]
        assert history[-1]['user'] == ''
        assert history[-1]['assistant']
        assert huge not in json.dumps(sent[-1], ensure_ascii=False)
        assert sent[-1]['memory_management_context']['response_settings']['tone'] == '친근한 반말'
        assert store.get_summary('user', 'conversation') == saved
        assert store.list_turns('user', 'conversation')[2].user_content == huge

        begin = reserve(store)
        snapshot = runtime.personal_memory.snapshot(
            request(5, '이어가기'), begin.token, begin.history, begin.summary,
        )
        assert [turn.ordinal for turn in snapshot.history] == [2, 3, 4]
        sources = snapshot.history[:2]
        assert sources[-1].user_content == ''
        assert store.apply_compaction(begin.token, saved, sources, SummaryResult(
            '민수와 목요일 오후 3시에 도서관에서 만날 약속은 확정됐다. '
            '너무 긴 새 입력은 처리하지 못했으며 기존 반말 설정을 유지한다.',
            '{}', 'openai-semantic-v1',
        ))
        compacted = store.get_summary('user', 'conversation')
        assert compacted.source_end_ordinal == 3
        assert compacted.source_digest == store._extend_source_digest(saved.source_digest, sources)
        store.fail_turn(begin.token)
        assert runtime.handle(request(5, '그 약속을 계속 이야기하자.')).decision.type == 'message'
        assert store.list_turns('user', 'conversation')[2].user_content == huge
    finally:
        runtime.close()


def test_model_reason_cannot_hide_a_normal_user_turn():
    decision = {'type': 'refusal', 'reason': 'conversation_context_limit'}
    for provider in ({}, {'provider': 'openai', 'model': 'safe-non-action'},
                     {'provider': 'reliable-fallback', 'model': 'user-model'}):
        assert not is_context_limit_response({'public': {'provider': provider, 'decision': decision}})


def test_context_rejection_preserves_pending_consent_without_approving_it(tmp_path):
    flow = Flow(tmp_path / 'pending.sqlite3')
    flow.conversations.semantic_context = True
    flow.runtime.provider = ReliableProvider([flow.provider])
    try:
        text = '우리 강아지 이름은 두부야. 기억해줘'
        assert flow.say(text, proposed('remember', text, facts=[pet_fact(text)])).decision.type == 'clarification'
        personal = flow.runtime.personal_memory
        before = personal.pending_question('alice', 'room')
        state = flow.memory.policy_state('alice')
        assert before and before['kind'] == 'consent' and not state['enabled']

        def too_large():
            raise ContextBudgetExceeded()

        flow.provider.callback = too_large
        huge = SpeechAgentRequest.from_dict({
            'request_id': 'oversized', 'turn_id': 'oversized',
            'user_id': 'alice', 'conversation_id': 'room',
            'utterance': '처리할수없는긴발화' * 400,
            'robot_state': {}, 'available_tools': [],
        })
        rejected = flow.runtime.handle(huge)
        assert rejected.decision.reason == 'conversation_context_limit'
        after = personal.pending_question('alice', 'room')
        assert after == dict(before, revision=before['revision'] + 1)
        assert flow.memory.policy_state('alice') == state
        assert not flow.memory.list_for_user('alice')
        assert flow.runtime.handle(huge).decision.reason == 'conversation_context_limit'
        assert personal.pending_question('alice', 'room') == after

        result = flow.say('네')
        assert result.decision.type == 'message'
        assert flow.memory.policy_state('alice')['enabled']
        saved = flow.memory.list_for_user('alice')
        assert len(saved) == 1 and '두부' in saved[0].content
        assert personal.pending_question('alice', 'room') is None
        assert len(flow.provider.calls) == 2  # Source proposal, then rejected input.
        state = flow.memory.policy_state('alice')
        flow.runtime.handle(flow.requests[-1])
        assert flow.memory.policy_state('alice') == state
        assert flow.memory.list_for_user('alice') == saved
    finally:
        flow.close()
