"""Common runtime story lifecycle and durable reply guards, entirely offline."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from malbut_agent_server.config import Settings
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import MemoryChangedError, OrchestrationResult
from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.safety import SafetyResult
from malbut_agent_server.schemas import (
    AgentDecision, AgentRequest, ProviderResult, SpeechAgentRequest, ValidationError,
)
from malbut_agent_server.story_memory_provider import StoryMemoryProvider
from malbut_agent_server.story_runtime_store import StoryRuntimeStore

from test_story_memory_service import FakeExtractor


class MemoryAwareProvider:
    """Keep foreground answers predictable while recording actual story input."""

    supports_memory = True

    def __init__(self):
        self.calls = []
        self.failure = None

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None, *, memory_context=None,
                 weather_context=None):
        self.calls.append(deepcopy(memory_context or {}))
        if self.failure is not None:
            raise self.failure
        stories = (memory_context or {}).get('story_memory_untrusted', {}).get('stories', [])
        answer = (stories[0]['current'][0]['text'] if stories else '알겠어요.')
        return ProviderResult(
            decision=AgentDecision(type='message', message=answer),
            provider='offline-story-fixture', model='fixture-v1',
            latency_ms=0.0, memory_supported=True,
        )


class StoryOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'runtime.sqlite3')
        self.sequence = 0

    def runtime(self, *, extractor='fixture', backend=None):
        extractor = FakeExtractor() if extractor == 'fixture' else extractor
        backend = backend or MemoryAwareProvider()
        with patch('malbut_agent_server.factory.build_provider', return_value=backend):
            runtime = build_orchestrator(
                Settings(provider='mock', database_path=self.path),
                story_extractor=extractor,
            )
        self.addCleanup(runtime.close)
        return runtime, backend, extractor

    def request(self, runtime, text, *, room='room', user='alice'):
        self.sequence += 1
        runtime.conversation_store.create(user, room)
        return AgentRequest.from_dict({
            'request_id': f'request-{self.sequence}',
            'turn_id': f'turn-{self.sequence}',
            'conversation_id': room, 'user_id': user,
            'utterance': text, 'robot_state': {}, 'available_tools': [],
        })

    def turn(self, runtime, text, **kwargs):
        request = self.request(runtime, text, **kwargs)
        return request, runtime.handle(request)

    def checkpoint(self, runtime):
        self.assertTrue(runtime.checkpoint_story_memory('alice', timeout=5))

    def recalled(self, runtime):
        runtime.story_memory.enable('alice')
        self.turn(runtime, '전시의 작품이 좋아서 즐거웠어.', room='source')
        self.checkpoint(runtime)
        story = runtime.story_memory.list_stories('alice')[0]
        request, result = self.turn(runtime, '전시 이야기를 다시 들려줘.', room='recall')
        self.checkpoint(runtime)
        binding = result.to_persisted_dict()['story_binding']
        self.assertEqual(binding['readset'][0]['story_id'], story['story_id'])
        self.assertTrue(binding['readset'][0]['entry_hashes'])
        return request, result, story

    def test_on_demand_speech_reply_replay_respects_story_deletion(self):
        runtime, backend, _ = self.runtime()
        runtime.story_provider.on_demand_speech = True
        runtime.story_memory.enable('alice')
        self.turn(runtime, '전시의 작품이 좋아서 즐거웠어.', room='source')
        self.checkpoint(runtime)
        story = runtime.story_memory.list_stories('alice')[0]
        request = SpeechAgentRequest.from_dict(
            self.request(runtime, '전시 이야기를 다시 들려줘.', room='recall').to_dict()
        )
        answer = backend.complete

        def decide(request, memories, conversation_turns, tools, **kwargs):
            if any(tool.name == 'recall_story_memory' for tool in tools):
                self.assertNotIn('story_memory_untrusted', kwargs['memory_context'])
                return ProviderResult(
                    decision=AgentDecision(type='tool_call', tool_name='recall_story_memory',
                                           arguments={}, message='이야기를 조회할게.'),
                    provider='offline-story-fixture', model='fixture', latency_ms=0,
                )
            return answer(request, memories, conversation_turns, tools, **kwargs)

        with patch.object(backend, 'complete', side_effect=decide) as model:
            reply = runtime.handle(request)
            self.assertEqual(reply.to_dict()['decision']['message'], story['current'][0]['text'])
            self.assertEqual(self.persisted(runtime, request)['story_binding']['readset'][0]
                             ['story_id'], story['story_id'])
            self.assertEqual(runtime.handle(request).decision_id, reply.decision_id)
            self.assertEqual(model.call_count, 2)
        runtime.story_memory.forget('alice', story['story_id'], timeout=5)
        with self.assertRaises(MemoryChangedError):
            runtime.assert_reply_fresh('alice', request.request_id)
        with self.assertRaises(ValidationError):
            runtime.handle(request)

    def fact_voice_runtime(self, *, stories=False):
        runtime, backend, _ = self.runtime()
        runtime.story_provider.on_demand_speech = True
        if stories:
            runtime.story_memory.enable('alice')
        origin = {'request_id': 'enable', 'turn_id': 'enable', 'generation': 1,
                  'conversation_id': 'controls', 'session_instance_id': 'controls-1',
                  'text': '개인화를 켜 줘.'}
        runtime.memory_store.set_personalization('alice', True, origin)
        return runtime, backend

    @staticmethod
    def fact_voice_decision(name=None, **args):
        decision = (AgentDecision('tool_call', '', tool_name=name, arguments=args) if name
                    else AgentDecision('message', '조회한 내용으로 답할게요.'))
        return ProviderResult(decision, 'offline', 'test', 0, memory_supported=True)

    def test_direct_speech_skips_all_fact_contents_on_enabled_profile(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        saved = runtime.memory_store.add('alice', '순찰과 무관한 고유 개인 사실', kind='preference')
        request = SpeechAgentRequest.from_dict(self.request(runtime, '집 한 바퀴 순찰해 줘.').to_dict())
        statements = []
        with runtime.conversation_store._lock:
            runtime.conversation_store._connection.set_trace_callback(statements.append)
        with patch.object(backend, 'complete', return_value=self.fact_voice_decision()) as model:
            reply = runtime.handle(request)
        with runtime.conversation_store._lock:
            runtime.conversation_store._connection.set_trace_callback(None)
        call = model.call_args
        self.assertEqual(call.args[1], [])
        self.assertEqual(call.kwargs['memory_context']['memories'], [])
        self.assertIn('recall_personal_context', [tool.name for tool in call.args[3]])
        self.assertNotIn('recall_story_memory', [tool.name for tool in call.args[3]])
        self.assertEqual(reply.memory_ids, [])
        self.assertFalse(any('FROM memories ' in ' '.join(sql.split()) for sql in statements))
        conn = runtime.conversation_store._connection
        deps = conn.execute('SELECT dependencies_json FROM memory_turn_state WHERE request_id=?',
                            (request.request_id,)).fetchone()
        self.assertNotIn(saved.id, json.loads(deps[0]))

    def test_preference_hit_keeps_fact_ids_expiry_and_does_not_read_stories(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        expiry = time.time() + 20
        pref = runtime.memory_store.add('alice', '매운 음식을 싫어해요.', kind='preference', expires_at=expiry)
        runtime.memory_store.add('alice', '비공개 이름은 다래', kind='name')
        runtime.memory_store.add('bob', '다른 사용자의 정보', kind='preference')
        request = SpeechAgentRequest.from_dict(self.request(runtime, '내 입맛에 맞는 저녁 추천해 줘.').to_dict())
        responses = [self.fact_voice_decision()]
        with patch.object(backend, 'complete', side_effect=responses) as model, patch.object(
                runtime.story_memory, 'context', wraps=runtime.story_memory.context) as story:
            reply = runtime.handle(request)
            self.assertEqual(runtime.handle(request).decision_id, reply.decision_id)
        self.assertEqual(model.call_count, 1)
        self.assertFalse(story.called)
        answer = model.call_args
        self.assertEqual([item.id for item in answer.args[1]], [pref.id])
        self.assertEqual(answer.kwargs['memory_context']['memories'][0]['id'], pref.id)
        self.assertEqual(answer.args[3], [])
        self.assertEqual(reply.memory_ids, [pref.id])
        self.assertLessEqual(reply.expires_at, expiry)
        conn = runtime.conversation_store._connection
        deps = conn.execute('SELECT dependencies_json FROM memory_turn_state WHERE request_id=?',
                            (request.request_id,)).fetchone()
        self.assertIn(pref.id, json.loads(deps[0]))
        followup = SpeechAgentRequest.from_dict(self.request(runtime, '알겠어.').to_dict())
        with patch.object(backend, 'complete', return_value=self.fact_voice_decision()) as model:
            later = runtime.handle(followup)
        self.assertEqual(later.memory_ids, [])
        self.assertEqual(model.call_args.args[1], [])
        inherited = conn.execute('SELECT dependencies_json FROM memory_turn_state WHERE request_id=?',
                                 (followup.request_id,)).fetchone()
        self.assertIn(pref.id, json.loads(inherited[0]))
        runtime.memory_store.remove_facts('alice', [pref.id])
        for turn in (request, followup):
            with self.assertRaises(ValidationError):
                runtime.handle(turn)

    def test_followup_drink_question_sends_owned_evidence_in_one_call(self):
        runtime, backend = self.fact_voice_runtime()
        saved = runtime.memory_store.add('alice', '음료 취향은 식혜', kind='preference')
        runtime.memory_store.add('bob', '음료 취향은 홍차', kind='preference')
        request = SpeechAgentRequest.from_dict(
            self.request(runtime, '그리고 제가 좋아하는 음료는 무엇인가요?').to_dict())
        with patch.object(backend, 'complete', return_value=self.fact_voice_decision()) as model:
            reply = runtime.handle(request)
        model.assert_called_once()
        self.assertEqual(reply.memory_ids, [saved.id])
        self.assertEqual([item.id for item in model.call_args.args[1]], [saved.id])
        self.assertEqual(model.call_args.args[3], [])

    def test_typed_fact_miss_reads_stories_once_if_consented(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        source = '전시에서 사람들에게 내 이름을 겨울이라고 소개했어.'
        self.turn(runtime, source, room='source')
        self.checkpoint(runtime)
        request = SpeechAgentRequest.from_dict(self.request(runtime, '전시에서 밝힌 내 이름은?').to_dict())
        answer = backend.complete

        def choose(req, memories, turns, tools, **kwargs):
            if any(tool.name == 'recall_personal_context' for tool in tools):
                return self.fact_voice_decision('recall_personal_context', focus='name')
            return answer(req, memories, turns, tools, **kwargs)

        with patch.object(backend, 'complete', side_effect=choose) as model, patch.object(
                runtime.story_memory, 'context', wraps=runtime.story_memory.context) as story:
            reply = runtime.handle(request)
        self.assertEqual(story.call_count, 1)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[3], [])
        self.assertEqual(reply.memory_ids, [])
        self.assertEqual(reply.decision.message, source)
        self.assertTrue(self.persisted(runtime, request)['story_binding']['readset'])

    def test_personal_recall_stops_before_second_model_if_deleted_after_first(self):
        runtime, backend = self.fact_voice_runtime()
        pref = runtime.memory_store.add('alice', '매운 음식을 싫어해요.', kind='preference')
        request = SpeechAgentRequest.from_dict(self.request(runtime, '어떤 저녁이 나한테 어울릴까?').to_dict())

        def erase(*args, **kwargs):
            runtime.memory_store.remove_facts('alice', [pref.id])
            return self.fact_voice_decision('recall_personal_context', focus='preference')

        with patch.object(backend, 'complete', side_effect=erase) as model:
            with self.assertRaises(MemoryChangedError):
                runtime.handle(request)
        self.assertEqual(model.call_count, 1)

    def test_personal_recall_refuses_unknown_focus_and_second_pass_actions(self):
        runtime, backend = self.fact_voice_runtime()
        recall = 'recall_personal_context'
        for decisions in ([self.fact_voice_decision(recall, focus='all_users')],
                          [self.fact_voice_decision(recall, focus='related'),
                           self.fact_voice_decision('request_patrol')]):
            with self.subTest(steps=len(decisions)):
                req = SpeechAgentRequest.from_dict(self.request(runtime, '내 취향에 맞춰 줘.').to_dict())
                with patch.object(backend, 'complete', side_effect=decisions) as model:
                    with self.assertRaises(ProviderError):
                        runtime.handle(req)
                self.assertEqual(model.call_count, len(decisions))
                if len(decisions) == 2:
                    self.assertEqual(model.call_args.args[3], [])

    def test_fact_deleted_during_read_only_model_call_cannot_be_delivered(self):
        runtime, backend = self.fact_voice_runtime()
        for i, (question, calls) in enumerate((('내 별명은 뭐야?', 1),
                                                ('혹시 나를 뭐라고 부르더라?', 2))):
            with self.subTest(question=question):
                fact = runtime.memory_store.add('alice', '별명은 구름', kind='nickname')
                req = SpeechAgentRequest.from_dict(
                    self.request(runtime, question, room=f'alias-{i}').to_dict())

                def choose(req, memories, turns, tools, **kwargs):
                    if tools:
                        return self.fact_voice_decision('recall_personal_context', focus='nickname')
                    self.assertEqual([item.id for item in memories], [fact.id])
                    runtime.memory_store.remove_facts('alice', [fact.id])
                    return self.fact_voice_decision()

                with patch.object(backend, 'complete', side_effect=choose) as model:
                    with self.assertRaises(MemoryChangedError):
                        runtime.handle(req)
                self.assertEqual(model.call_count, calls)

    def save_identity_fact(self, runtime, kind, subject, attribute, value, text, *, correct_ids=()):
        request, _ = self.turn(runtime, text, room='identity-source')
        session = runtime.conversation_store.get('alice', request.conversation_id)
        source = {'request_id': request.request_id, 'turn_id': request.turn_id, 'text': text,
                  'conversation_id': session.conversation_id, 'generation': session.generation,
                  'session_instance_id': session.session_instance_id}
        saved = runtime.memory_store.upsert_fact('alice', {
            'kind': kind, 'subject': subject, 'attribute': attribute, 'value': value, 'evidence': text,
        }, source, correct_ids=correct_ids)
        return saved['records'][0]

    def ask_identity(self, runtime, backend, text, *, decide=None):
        request = SpeechAgentRequest.from_dict(self.request(runtime, text, room='identity-voice').to_dict())

        def read_name(req, memories, history, tools, **kwargs):
            if tools:
                return self.fact_voice_decision('recall_personal_context', focus='name')
            values = [item.metadata['fact']['value'] for item in memories]
            return ProviderResult(AgentDecision('message', ', '.join(values) or '모르겠어요.'),
                                  'offline', 'test', 0, memory_supported=True)

        with patch.object(backend, 'complete', side_effect=decide or read_name) as model:
            reply = runtime.handle(request)
        return request, reply, model.call_args_list

    def test_self_name_edit_receives_only_its_live_slot_and_keeps_final_i(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        original = self.save_identity_fact(runtime, 'name', 'user', 'name', '나예솔', '내 본명은 나예솔이야.')
        self.save_identity_fact(runtime, 'nickname', 'user', 'nickname', '모래별', '내 호칭은 모래별이야.')
        self.save_identity_fact(runtime, 'name', '친구', 'name', '하은별', '내 친구 이름은 하은별이야.')
        text = '내 본명을 해담이로 정정해 줘.'

        def correct(req, memories, history, tools, **kwargs):
            self.assertEqual([record.id for record in memories], [original.id])
            self.assertEqual(tools, [])
            self.assertEqual(req.available_tools, ())
            ctx = kwargs['memory_context']
            self.assertEqual([record['id'] for record in ctx['memories']], [original.id])
            self.assertEqual(ctx['self_name_correction']['new_name_verbatim'], '해담이')
            return ProviderResult(
                AgentDecision('clarification', '해담으로 바꿀까요?'), 'offline', 'test', 0,
                memory_supported=True, memory_proposal={
                    'operation': 'correct', 'target_ids': [original.id], 'query': '', 'evidence': text,
                    'facts': [{'kind': 'name', 'subject': 'user', 'attribute': 'name', 'value': '해담',
                               'evidence': text}],
                },
            )

        _, result, calls = self.ask_identity(runtime, backend, text, decide=correct)
        self.assertEqual(len(calls), 1)
        self.assertIn('정정했어요', result.decision.message)
        _, result, calls = self.ask_identity(runtime, backend, '지금 저장된 내 본명이 뭐야?')
        self.assertEqual(calls, [])
        self.assertIn('해담이', result.decision.message)
        self.assertNotIn(original.id, result.memory_ids)
        runtime.memory_store.remove_facts('alice', result.memory_ids)
        for query in ('지금 저장된 내 본명이 뭐야?', '새 대화야. 지금 저장된 내 본명이 뭐야?'):
            _, result, calls = self.ask_identity(runtime, backend, query)
            self.assertEqual(calls, [])
            self.assertEqual(result.memory_ids, [])
            self.assertEqual(result.decision.message, '현재 저장된 본명을 확인할 수 없어요.')
        _, result, calls = self.ask_identity(runtime, backend, '새 대화야. 장기 기억에 저장된 내 호칭이 뭐야?')
        self.assertEqual(calls, [])
        self.assertIn('모래별', result.decision.message)

    def test_saved_name_repeat_reads_current_name_once_with_distinct_nickname_and_pet(self):
        runtime, backend = self.fact_voice_runtime()
        real = self.save_identity_fact(runtime, 'name', 'user', 'name', '나예솔', '내 본명은 나예솔이야.')
        self.save_identity_fact(runtime, 'nickname', 'user', 'nickname', '모래별', '내 호칭은 모래별이야.')
        self.save_identity_fact(runtime, 'pet', '강아지', 'name', '콩눈이', '우리 강아지 이름은 콩눈이야.')
        _, first, _ = self.ask_identity(runtime, backend, '내 이름이 뭐였지?')
        self.assertEqual(first.memory_ids, [real.id])
        alias = '오늘 이 방에서만 나를 단비콩이라고 불러 줘.'
        self.turn(runtime, alias, room='identity-voice')
        req, result, calls = self.ask_identity(runtime, backend, '전에 알려 준 내 이름이 뭐였더라?')
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.decision.message, '나예솔')
        self.assertEqual(result.memory_ids, [real.id])
        self.assertEqual(calls[0].args[3], [])
        self.assertEqual(calls[0].kwargs['memory_context']['mode'], 'answer_only')
        self.assertEqual([c['id'] for c in calls[0].kwargs['memory_context']['memories']], [real.id])
        with patch.object(backend, 'complete', side_effect=AssertionError('replay called the model')):
            self.assertEqual(runtime.handle(req).decision_id, result.decision_id)
        # Asking how to address the speaker now still has access to the temporary instruction.
        _, _, recent = self.ask_identity(runtime, backend, '조금 전 이 방에서만 뭐라고 불러 달랬지?',
                                         decide=lambda *a, **k: self.fact_voice_decision())
        self.assertIn(alias, [turn.user_content for turn in recent[0].args[2]])
        self.assertEqual(recent[0].args[1], [])

    def test_stored_identity_pair_reads_only_both_current_self_slots_without_model(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        name = self.save_identity_fact(runtime, 'name', 'user', 'name', '나예솔', '내 본명은 나예솔이야.')
        alias = self.save_identity_fact(runtime, 'nickname', 'self', 'nickname', '별42', '내 호칭은 별42야.')
        self.save_identity_fact(runtime, 'name', '친구', 'name', '다른솔', '친구 이름은 다른솔이야.')
        self.turn(runtime, '오늘 이 방에서만 나를 단비콩이라고 불러 줘.', room='identity-voice')
        queries = ('내가 알려 준 내 본명과 저장 호칭을 목록으로 말해 줘.',
                   '제 이름이랑 저장해 둔 제 별명을 둘 다 알려 주세요.',
                   '저장된 내 호칭과 본명이 뭐야?', '내가 저장한 본명과 호칭을 각각 말해 줘.',
                   '저장 호칭과 내 본명을 같이 알려 줘.')
        for text in queries:
            with self.subTest(text=text):
                request, result, calls = self.ask_identity(runtime, backend, text)
                self.assertEqual(calls, [])
                self.assertEqual(set(result.memory_ids), {name.id, alias.id})
                self.assertEqual(result.decision.message, '저장된 본명은 나예솔입니다. 저장된 호칭은 별42입니다.')
        corrected = self.save_identity_fact(runtime, 'name', 'user', 'name', '한여울',
                                            '내 본명은 한여울이야.', correct_ids=[name.id])
        with self.assertRaises(MemoryChangedError):
            runtime.handle(request)
        _, current, calls = self.ask_identity(runtime, backend, queries[0])
        self.assertEqual(calls, [])
        self.assertEqual(current.decision.message, '저장된 본명은 한여울입니다. 저장된 호칭은 별42입니다.')
        self.assertEqual(set(current.memory_ids), {corrected.id, alias.id})
        runtime.memory_store.remove_facts('alice', [corrected.id])
        _, missing, calls = self.ask_identity(runtime, backend, queries[0])
        self.assertEqual(calls, [])
        self.assertEqual(missing.decision.message, '현재 저장된 본명을 확인할 수 없어요. 저장된 호칭은 별42입니다.')
        runtime.memory_store.remove_facts('alice', [alias.id])
        _, empty, calls = self.ask_identity(runtime, backend, queries[0])
        self.assertEqual(calls, [])
        self.assertEqual(empty.decision.message, '현재 저장된 본명을 확인할 수 없어요. 현재 저장된 호칭을 확인할 수 없어요.')
        self.assertEqual(empty.memory_ids, [])

    def test_identity_pair_uses_guarded_legacy_names_and_does_not_guess_ambiguous_slots(self):
        runtime, backend = self.fact_voice_runtime()
        runtime.memory_store.add('alice', '제 본명은 비단', kind='name')
        runtime.memory_store.add('alice', '동료 이름은 은별', kind='name')
        self.save_identity_fact(runtime, 'nickname', 'user', 'nickname', '첫별', '내 호칭은 첫별이야.')
        self.save_identity_fact(runtime, 'nickname', 'self', 'nickname', '둘별', '내 별명은 둘별이야.')
        _, result, calls = self.ask_identity(runtime, backend, '저장된 내 본명과 호칭을 알려 줘.')
        self.assertEqual(calls, [])
        self.assertEqual(result.decision.type, 'clarification')
        self.assertIn('본명은 비단', result.decision.message)
        self.assertIn('호칭 후보가 여러 개', result.decision.message)
        self.assertFalse(any(value in result.decision.message for value in ('첫별', '둘별', '은별')))

    def test_identity_pair_leaves_other_meanings_and_disabled_profiles_on_original_route(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        name = self.save_identity_fact(runtime, 'name', 'user', 'name', '나예솔', '내 이름은 나예솔이야.')
        queries = ('내 본명과 저장 호칭을 말하지 마.', '“내 본명과 저장 호칭을 알려 줘”라고 읽어 줘.',
                   '내 친구 본명과 저장 호칭을 말해 줘.', '내 본명과 이 방에서만 쓰는 호칭을 알려 줘.',
                   '내 본명과 저장 호칭의 글자 수를 알려 줘.', '내 본명과 저장 호칭을 삭제해 줘.')
        for text in queries:
            with self.subTest(text=text):
                _, _, calls = self.ask_identity(runtime, backend, text,
                                                 decide=lambda *a, **k: self.fact_voice_decision())
                self.assertTrue(calls)
        self.turn(runtime, '개인화를 꺼 줘.', room='identity-voice')
        self.assertFalse(runtime.memory_store.policy_state('alice')['enabled'])
        _, result, calls = self.ask_identity(runtime, backend, '내 본명과 저장 호칭을 말해 줘.',
                                             decide=lambda *a, **k: self.fact_voice_decision())
        self.assertTrue(calls)
        self.assertNotIn(name.id, result.memory_ids)
        self.assertNotIn('나예솔', result.decision.message)

    def test_saved_name_repeat_keeps_ambiguous_identity_requests_on_existing_route(self):
        runtime, backend = self.fact_voice_runtime()
        self.save_identity_fact(runtime, 'name', 'user', 'name', '나예솔', '내 본명은 나예솔이야.')
        self.ask_identity(runtime, backend, '내 이름이 뭐였지?')
        temporary = '오늘 이 방에서만 단비콩으로 불러 줘.'
        self.turn(runtime, temporary, room='identity-voice')
        for text in ('전에 알려 준 내 본명하고 아까 불러 달라고 한 호칭을 각각 말해 줘.',
                     '전에 전시회에서 사용했던 내 이름이 뭐였지?',
                     '전에 내 친구가 알려 준 본명이 뭐였더라?'):
            with self.subTest(text=text):
                _, _, calls = self.ask_identity(runtime, backend, text,
                                                decide=lambda *a, **k: self.fact_voice_decision())
                self.assertIn('recall_personal_context', [tool.name for tool in calls[0].args[3]])
                self.assertIn(temporary, [t.user_content for t in calls[0].args[2]])

    def test_plain_name_question_never_uses_a_friends_name_after_deletion(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        old = self.save_identity_fact(runtime, 'name', 'user', 'name', '윤가람', '내 본명은 윤가람이야.')
        self.ask_identity(runtime, backend, '내 이름이 뭐였지?')
        # This independent turn has no fact receipt to revoke at deletion.
        self.turn(runtime, '내 본명은 윤가람이야.', room='identity-voice')
        runtime.memory_store.remove_facts('alice', [old.id])
        self.save_identity_fact(runtime, 'name', '친구', 'name', '윤가람', '내 친구 이름은 윤가람이야.')
        self.turn(runtime, '윤가람은 친구 이름이야. 내 이름으로 저장하지 마.', room='identity-voice')
        for text in ('내 이름이 뭐야?', '전에 알려 준 내 이름이 뭐였더라?', '내 이름은 뭐지?',
                     '지금 내 이름이 뭐야?', '지금 저장된 내 본명을 알려줘.', '내 이름을 말해 줘.',
                     '제 이름이 뭔지 가르쳐 주세요.', '현재 제 이름은 어떻게 저장돼 있어?'):
            _, reply, calls = self.ask_identity(runtime, backend, text)
            self.assertEqual(calls, [])
            self.assertEqual(reply.memory_ids, [])
            self.assertEqual(reply.decision.message, '현재 저장된 본명을 확인할 수 없어요.')

    def test_explicit_name_owner_constrains_model_lookup_as_well_as_direct_reads(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        own = self.save_identity_fact(runtime, 'name', 'user', 'name', '나예솔', '내 본명은 나예솔이야.')
        other = self.save_identity_fact(runtime, 'name', '친구', 'name', '윤가람', '친구 이름은 윤가람이야.')
        self.turn(runtime, '내 친구가 윤가람이야.', room='identity-voice')
        for text in ('현재 내 이름은 뭐죠?', '현재 내 이름의 첫 글자를 알려 줘.'):
            _, result, calls = self.ask_identity(runtime, backend, text)
            self.assertEqual(result.memory_ids, [own.id])
            supplied = calls[-1]
            self.assertEqual([m.metadata['fact']['subject'] for m in supplied.args[1]], ['user'])
            self.assertEqual(supplied.args[2:4], ([], []))
            self.assertNotIn('윤가람', repr(supplied))
        runtime.memory_store.remove_facts('alice', [own.id])
        _, result, calls = self.ask_identity(runtime, backend, '현재 내 이름의 첫 글자를 알려 줘.')
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.memory_ids, [])
        self.assertEqual(calls[-1].args[1:4], ([], [], []))
        self.assertNotIn('story_memory_untrusted', calls[-1].kwargs['memory_context'])
        # When the named subject is actually a friend, their record remains available.
        _, result, _ = self.ask_identity(runtime, backend, '내 친구 이름을 알려 줘.')
        self.assertEqual(result.memory_ids, [other.id])

    def test_plain_name_question_preserves_legacy_and_recent_first_person_evidence(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        gone = self.save_identity_fact(runtime, 'name', 'user', 'name', '나예솔', '내 이름은 나예솔이야.')
        runtime.memory_store.remove_facts('alice', [gone.id])
        self.save_identity_fact(runtime, 'name', '친구', 'name', '윤가람', '내 친구 이름은 윤가람이야.')
        legacy = runtime.memory_store.add('alice', '제 이름은 비단', kind='name')
        self.turn(runtime, '친구 윤가람이 그러던데.', room='identity-voice')
        self.turn(runtime, '저는 대학생입니다.', room='identity-voice')
        self.turn(runtime, '제 본명은 해담이입니다.', room='identity-voice')
        pet = self.save_identity_fact(runtime, 'pet', '고양이', 'name', '두부', '고양이 이름은 두부야.')
        runtime.memory_store.remove_facts('alice', [pet.id])
        _, reply, calls = self.ask_identity(runtime, backend, '내 이름이 뭐야?',
                                             decide=lambda *a, **k: self.fact_voice_decision())
        self.assertEqual(len(calls), 1)
        self.assertEqual(reply.memory_ids, [legacy.id])
        self.assertEqual([t.user_content for t in calls[0].args[2]], ['제 본명은 해담이입니다.'])
        self.assertEqual(calls[0].args[3], [])
        self.assertNotIn('윤가람', repr(calls))
        self.assertNotIn('story_memory_untrusted', calls[0].kwargs['memory_context'])

    def test_saved_name_repeat_after_other_session_correction_and_deletion(self):
        runtime, backend = self.fact_voice_runtime()
        old = self.save_identity_fact(runtime, 'name', 'user', 'name', '나예솔', '내 이름은 나예솔이야.')
        old_request, old_reply, _ = self.ask_identity(runtime, backend, '내 이름이 뭐였지?')
        self.assertEqual(old_reply.decision.message, '나예솔')
        paraphrase = '예쁜 솔이라는 두 음절이에요.'
        with patch.object(backend, 'complete', return_value=ProviderResult(
                AgentDecision('message', paraphrase), 'offline', 'test', 0, memory_supported=True)):
            self.turn(runtime, '방금 답을 다르게 말해 줘.', room='identity-voice')
        current = self.save_identity_fact(runtime, 'name', 'user', 'name', '한여울',
                                          '내 본명을 한여울로 정정해 줘.', correct_ids=[old.id])
        text = '전에 알려 준 내 이름이 뭐였더라?'
        req, answer, calls = self.ask_identity(runtime, backend, text)
        self.assertEqual(answer.decision.message, '한여울')
        self.assertEqual(answer.memory_ids, [current.id])
        self.assertNotIn('나예솔', repr(calls))
        self.assertNotIn(paraphrase, repr(calls))
        with self.assertRaises(ValidationError):
            runtime.handle(old_request)
        runtime.memory_store.remove_facts('alice', [current.id])
        with self.assertRaises(MemoryChangedError):
            answer.to_dict()
        with self.assertRaises(ValidationError):
            runtime.handle(req)
        _, missing, calls = self.ask_identity(runtime, backend, text)
        self.assertEqual(missing.memory_ids, [])
        self.assertEqual(missing.decision.message, '현재 저장된 본명을 확인할 수 없어요.')
        self.assertEqual(calls, [])
        for revoked in ('한여울', '나예솔', paraphrase):
            self.assertNotIn(revoked, repr(calls))

    def test_saved_name_repeat_revocation_during_the_only_model_call_blocks_delivery(self):
        runtime, backend = self.fact_voice_runtime()
        fact = self.save_identity_fact(runtime, 'name', 'user', 'name', '나예솔', '내 본명은 나예솔이야.')
        self.ask_identity(runtime, backend, '내 이름이 뭐였지?')
        repeat = SpeechAgentRequest.from_dict(self.request(
            runtime, '전에 알려 준 내 이름이 뭐였더라?', room='identity-voice').to_dict())

        def revoke(req, memories, history, tools, **kwargs):
            if tools:
                return self.fact_voice_decision('recall_personal_context', focus='name')
            self.assertEqual([item.id for item in memories], [fact.id])
            runtime.memory_store.set_personalization('alice', False, {
                'request_id': 'disable', 'turn_id': 'disable', 'generation': 1,
                'conversation_id': 'controls', 'session_instance_id': 'controls-1', 'text': '개인화를 꺼 줘.',
            })
            return ProviderResult(AgentDecision('message', '나예솔'), 'offline', 'test', 0, memory_supported=True)

        with patch.object(backend, 'complete', side_effect=revoke) as model:
            with self.assertRaises(MemoryChangedError):
                runtime.handle(repeat)
        self.assertEqual(model.call_count, 1)

    def first_grounded_speech_answer(self):
        runtime, backend, extractor = self.runtime()
        runtime.story_provider.on_demand_speech = True
        runtime.story_memory.enable('alice')
        source = '우리 전시 약속은 수요일이야.'
        self.turn(runtime, source, room='source')
        self.checkpoint(runtime)
        story = runtime.story_memory.list_stories('alice')[0]
        question = '전시 약속을 우리가 전에 언제로 정했지?'
        request = SpeechAgentRequest.from_dict(self.request(runtime, question, room='recall').to_dict())
        with patch.object(backend, 'complete', wraps=backend.complete) as model:
            result = runtime.handle(request)
            self.assertEqual(result.decision.message, source)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(model.call_args.args[3], [])
            self.assertEqual(model.call_args.kwargs['memory_context']['mode'], 'answer_only')
            self.assertEqual(runtime.handle(request).decision_id, result.decision_id)
            self.assertEqual(model.call_count, 1)
        binding = self.persisted(runtime, request)['story_binding']
        self.assertEqual(binding['readset'][0]['story_id'], story['story_id'])
        # A question about the date does not change it.
        with patch.object(extractor, 'extract', return_value=[]):
            self.checkpoint(runtime)
        return runtime, backend, extractor, request, story

    def test_single_call_story_answer_preserves_source_and_deletion_fence(self):
        runtime, _, _, request, story = self.first_grounded_speech_answer()
        runtime.story_memory.forget('alice', story['story_id'], timeout=5)
        with self.assertRaises(MemoryChangedError):
            runtime.assert_reply_fresh('alice', request.request_id)
        with self.assertRaises(ValidationError):
            runtime.handle(request)

    def test_grounded_repeat_reads_again_once_and_fences_its_own_reply(self):
        runtime, backend, _, first, story = self.first_grounded_speech_answer()
        repeat = SpeechAgentRequest.from_dict(self.request(
            runtime, '전에 우리가 잡았던 전시 약속은 무슨 요일이었지?', room='recall').to_dict())
        with patch.object(backend, 'complete', wraps=backend.complete) as model:
            answer = runtime.handle(repeat)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[3], [])
        self.assertEqual(model.call_args.kwargs['memory_context']['mode'], 'answer_only')
        self.assertEqual(answer.decision.message, story['current'][0]['text'])
        self.assertEqual(self.persisted(runtime, repeat)['story_binding']['readset'],
                         self.persisted(runtime, first)['story_binding']['readset'])
        self.assertTrue(model.call_args.args[2])
        self.assertTrue(all(not t.assistant_content and not t.response for t in model.call_args.args[2]))
        runtime.story_memory.forget('alice', story['story_id'], timeout=5)
        with self.assertRaises(MemoryChangedError):
            answer.to_dict()
        with self.assertRaises(ValidationError):
            runtime.handle(repeat)

    def test_grounded_repeat_cannot_deliver_after_consent_changes_during_model_call(self):
        runtime, backend, _, _, _ = self.first_grounded_speech_answer()
        repeat = SpeechAgentRequest.from_dict(self.request(
            runtime, '전에 우리가 잡았던 전시 약속은 무슨 요일이었지?', room='recall').to_dict())
        original = backend.complete

        def revoke_during_answer(*args, **kwargs):
            answer = original(*args, **kwargs)
            self.assertIn('수요일', answer.decision.message)
            runtime.story_memory.disable('alice')
            return answer

        with patch.object(backend, 'complete', side_effect=revoke_during_answer) as model:
            with self.assertRaises(MemoryChangedError):
                runtime.handle(repeat)
        self.assertEqual(model.call_count, 1)

    def test_grounded_repeat_during_correction_keeps_user_words_without_old_answers(self):
        runtime, backend, extractor, _, _ = self.first_grounded_speech_answer()
        paraphrase = '일주일의 세 번째 날이라고 말씀드렸어요.'
        with patch.object(backend, 'complete', return_value=ProviderResult(
                AgentDecision('message', paraphrase), 'offline', 'test', 0, memory_supported=True)):
            self.turn(runtime, '다르게 말해 줘.', room='recall')
        with patch.object(extractor, 'extract', return_value=[]):
            self.checkpoint(runtime)

        # Hold the other session's correction queued or running for both reads.
        extractor.release = threading.Event()
        self.addCleanup(extractor.release.set)
        self.turn(runtime, '전시 약속은 목요일로 바꿨어.', room='other')
        local_change = '전시 약속은 금요일이야.'
        for corrected_here in (False, True):
            with self.subTest(corrected_here=corrected_here):
                if corrected_here:
                    self.turn(runtime, local_change, room='recall')
                repeat = SpeechAgentRequest.from_dict(self.request(
                    runtime, '전에 우리가 잡았던 전시 약속은 무슨 요일이었지?', room='recall').to_dict())
                with patch.object(backend, 'complete', wraps=backend.complete) as model:
                    runtime.handle(repeat)
                self.assertEqual(model.call_count, 1)
                memory = model.call_args.kwargs['memory_context']['story_memory_untrusted']
                self.assertEqual(memory['status'], 'updating')
                self.assertEqual(memory['stories'], [])
                history = model.call_args.args[2]
                self.assertTrue(all(not t.assistant_content and not t.response for t in history))
                self.assertNotIn('수요일', repr(history))
                self.assertNotIn(paraphrase, repr(history))
                self.assertEqual(local_change in [t.user_content for t in history], corrected_here)
                self.assertNotIn('목요일', repr(history))

    def test_unconsented_fresh_owner_never_loads_another_owners_pet(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        runtime.memory_store.add('alice', '강아지 이름은 다래', kind='pet')
        request = SpeechAgentRequest.from_dict(
            self.request(runtime, '우리 강아지 이름 기억나?', user='bob').to_dict())
        with patch.object(backend, 'complete', wraps=backend.complete) as model, patch.object(
                runtime.story_memory, 'context', wraps=runtime.story_memory.context) as story:
            result = runtime.handle(request)
        self.assertEqual(story.call_count, 0)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[1], [])
        self.assertEqual(result.memory_ids, [])

    def test_read_prohibition_skips_both_stores_and_private_tools_including_on_retry(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        runtime.memory_store.add('alice', '제 이름은 비단', kind='name')
        request = SpeechAgentRequest.from_dict(
            self.request(runtime, '제 이름을 꺼내지 말아 주세요.').to_dict())
        with patch.object(backend, 'complete', wraps=backend.complete) as model, patch.object(
                runtime.memory_store, 'search_with_revision', wraps=runtime.memory_store.search_with_revision) as facts, patch.object(
                runtime.story_memory, 'context', wraps=runtime.story_memory.context) as stories:
            answer = runtime.handle(request)
            self.assertEqual(runtime.handle(request).decision_id, answer.decision_id)
        self.assertEqual(model.call_count, 1)
        self.assertFalse(facts.called or stories.called)
        call = model.call_args
        self.assertEqual(call.args[1], [])
        self.assertEqual(call.kwargs['memory_context']['memories'], [])
        self.assertEqual(call.kwargs['memory_context']['mode'], 'answer_only')
        self.assertFalse({'recall_story_memory', 'recall_personal_context'}.intersection(
            tool.name for tool in call.args[3]))

    def test_unoffered_recall_on_prohibited_turn_cannot_trigger_a_read(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        req = SpeechAgentRequest.from_dict(self.request(runtime, '기억은 조회하지 마.').to_dict())
        for name in ('recall_story_memory', 'recall_personal_context'):
            with self.subTest(name=name), patch.object(
                    backend, 'complete', return_value=self.fact_voice_decision(name)) as model, patch.object(
                    runtime.story_memory, 'context', side_effect=AssertionError('unrequested story')):
                with self.assertRaises(ProviderError):
                    runtime.handle(req)
                self.assertEqual(model.call_count, 1)

    def test_no_disclosure_plus_deletion_resolves_target_locally_without_current_fact_in_input(self):
        from test_personal_memory_flow import proposed

        runtime, backend = self.fact_voice_runtime(stories=True)
        fact = runtime.memory_store.add('alice', '저장 이름은 비단', kind='name')
        keep = runtime.memory_store.add('alice', '저장 호칭은 연두', kind='nickname')
        text = '이름은 말하지 말고 저장한 이름은 지워줘.'
        req = SpeechAgentRequest.from_dict(self.request(runtime, text).to_dict())
        proposal = self.fact_voice_decision()
        proposal.memory_proposal = proposed('forget', text, query='이름')
        with patch.object(backend, 'complete', return_value=proposal) as model:
            result = runtime.handle(req)
            self.assertEqual(runtime.handle(req).decision_id, result.decision_id)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[1], [])
        self.assertNotIn('비단', str(model.call_args.kwargs['memory_context']))
        self.assertNotIn('비단', result.decision.message)
        identifiers = {record.id for record in runtime.memory_store.list_for_user('alice')}
        self.assertNotIn(fact.id, identifiers)
        self.assertIn(keep.id, identifiers)

    def test_event_specific_attribute_reads_both_current_fact_and_source_story(self):
        runtime, backend = self.fact_voice_runtime(stories=True)
        current = runtime.memory_store.add('alice', '내 현재 별명은 구름', kind='nickname')
        source = '시낭송 전시에서 처음 소개한 별명은 솔잎이야.'
        self.turn(runtime, source, room='source')
        self.checkpoint(runtime)
        q = '시낭송 전시에서 썼던 내 별명은 뭐였지?'
        request = SpeechAgentRequest.from_dict(self.request(runtime, q, room='question').to_dict())
        with patch.object(backend, 'complete', wraps=backend.complete) as model:
            result = runtime.handle(request)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[3], [])
        self.assertEqual([row.id for row in model.call_args.args[1]], [current.id])
        self.assertIn('event_attribute_evidence', model.call_args.kwargs['memory_context'])
        self.assertEqual(result.decision.message, source)
        self.assertTrue(self.persisted(runtime, request)['story_binding']['readset'])

    def test_personal_management_confirmation_keeps_its_original_target(self):
        from test_personal_memory_flow import pet_fact, proposed

        runtime, backend = self.fact_voice_runtime()
        for value in ('두부', '보리'):
            text = f'우리 강아지 이름은 {value}야. 기억해줘'
            req = SpeechAgentRequest.from_dict(self.request(runtime, text).to_dict())
            proposal = self.fact_voice_decision()
            proposal.memory_proposal = proposed('remember', text, facts=[pet_fact(text, value)])
            with patch.object(backend, 'complete', return_value=proposal) as model:
                result = runtime.handle(req)
            self.assertNotIn('recall_personal_context', [tool.name for tool in model.call_args.args[3]])
        self.assertEqual(result.decision.type, 'clarification')
        self.assertEqual(runtime.memory_store.list_for_user('alice')[0].metadata['fact']['value'], '두부')
        confirm = SpeechAgentRequest.from_dict(self.request(runtime, '네').to_dict())
        with patch.object(backend, 'complete', side_effect=AssertionError('confirmation is local')):
            confirmed = runtime.handle(confirm)
        self.assertIn('정정했어요', confirmed.decision.message)
        self.assertEqual(runtime.memory_store.list_for_user('alice')[0].metadata['fact']['value'], '보리')

    def persisted(self, runtime, request):
        with runtime.conversation_store._lock:
            row = runtime.conversation_store._connection.execute(
                'SELECT response_json FROM conversation_turns '
                'WHERE user_id=? AND request_id=?',
                (request.user_id, request.request_id),
            ).fetchone()
        return json.loads(row[0])

    def replace_persisted(self, runtime, request, value):
        with runtime.conversation_store._lock:
            conn = runtime.conversation_store._connection
            conn.execute(
                'UPDATE conversation_turns SET response_json=? '
                'WHERE user_id=? AND request_id=?',
                (json.dumps(value, ensure_ascii=False), request.user_id, request.request_id),
            )
            conn.commit()

    def test_default_off_and_explicit_enable_enqueue_only_after_completion(self):
        runtime, backend, extractor = self.runtime()
        self.assertFalse(runtime.story_memory.policy('alice')['enabled'])
        self.turn(runtime, '전시에 다녀왔어.')
        self.checkpoint(runtime)
        self.assertEqual(extractor.calls, [])
        self.assertEqual(runtime.story_memory.list_stories('alice'), [])
        self.assertNotIn('story_memory_untrusted', backend.calls[-1])

        runtime.story_memory.enable('alice')
        observed = []
        after_turn = runtime.story_memory.after_turn

        def after_completed(user, request_id):
            with runtime.conversation_store._lock:
                row = runtime.conversation_store._connection.execute(
                    'SELECT status, response_json FROM conversation_turns '
                    'WHERE user_id=? AND request_id=?', (user, request_id),
                ).fetchone()
            observed.append((row['status'], json.loads(row['response_json'])))
            return after_turn(user, request_id)

        with patch.object(runtime.story_memory, 'after_turn', side_effect=after_completed) as enqueue:
            request, result = self.turn(runtime, '설명서는 한 장으로 만들자.')
        enqueue.assert_called_once_with('alice', request.request_id)
        self.assertEqual(observed[0][0], 'completed')
        self.assertEqual(observed[0][1]['public']['request_id'], result.request_id)
        self.checkpoint(runtime)
        self.assertEqual([story['title'] for story in runtime.story_memory.list_stories('alice')],
                         ['설명서'])

    def test_provider_or_commit_failure_never_enqueues_a_failed_turn(self):
        for failure in ('provider', 'commit'):
            with self.subTest(failure=failure):
                runtime, backend, extractor = self.runtime()
                runtime.story_memory.enable('alice')
                request = self.request(runtime, '전시에서 작품을 봤어.')
                if failure == 'provider':
                    backend.failure = ProviderError('offline failure')
                with patch.object(runtime.story_memory, 'after_turn',
                                  wraps=runtime.story_memory.after_turn) as enqueue:
                    if failure == 'commit':
                        with patch.object(runtime.conversation_store, 'complete_turn',
                                          side_effect=RuntimeError('commit failure')):
                            with self.assertRaisesRegex(RuntimeError, 'commit failure'):
                                runtime.handle(request)
                    else:
                        with self.assertRaises(ProviderError):
                            runtime.handle(request)
                enqueue.assert_not_called()
                self.checkpoint(runtime)
                self.assertEqual(extractor.calls, [])
                self.assertEqual(runtime.story_memory.list_stories('alice'), [])
                runtime.close()

    def test_reopened_cached_reply_revalidates_story_binding(self):
        for invalidation in ('disable', 'forget', 'correction'):
            with self.subTest(invalidation=invalidation):
                # Each mutation starts from an independent durable database.
                self.path = str(Path(self.temp.name) / f'{invalidation}.sqlite3')
                runtime, _backend, _extractor = self.runtime()
                request, original, story = self.recalled(runtime)
                original_binding = self.persisted(runtime, request)['story_binding']
                runtime.close()
                reopened, backend, _extractor = self.runtime()
                cached = reopened.handle(request)
                self.assertEqual(cached.decision_id, original.decision_id)
                self.assertEqual(cached.to_persisted_dict()['story_binding'], original_binding)
                self.assertEqual(backend.calls, [])

                if invalidation == 'disable':
                    reopened.story_memory.disable('alice')
                elif invalidation == 'forget':
                    reopened.story_memory.forget('alice', story['story_id'], timeout=5)
                else:
                    self.turn(reopened, '전시에서는 사실 피곤했어.', room='correction')
                    self.checkpoint(reopened)
                with self.assertRaises(MemoryChangedError):
                    reopened.assert_reply_fresh('alice', request.request_id)
                with self.assertRaises(MemoryChangedError):
                    cached.to_dict()
                calls = len(backend.calls)
                # Deletion also revokes the request fingerprint, so its cache
                # lookup may reject with ConversationConflictError first.
                with self.assertRaises(ValidationError):
                    reopened.handle(request)
                self.assertEqual(len(backend.calls), calls)
                reopened.close()

    def test_delayed_serialization_checks_story_and_fact_policy(self):
        for invalidation in ('story', 'fact'):
            with self.subTest(invalidation=invalidation):
                self.path = str(Path(self.temp.name) / f'delayed-{invalidation}.sqlite3')
                runtime, _backend, _extractor = self.runtime()
                request, result, _story = self.recalled(runtime)
                self.assertEqual(result.to_dict()['request_id'], request.request_id)
                if invalidation == 'story':
                    runtime.story_memory.disable('alice')
                else:
                    runtime.memory_store.invalidate_answers('alice')
                with self.assertRaises(MemoryChangedError):
                    result.to_dict()
                with self.assertRaises(MemoryChangedError):
                    runtime.assert_reply_fresh('alice', request.request_id)
                runtime.close()

    def test_durable_guard_survives_multiple_wrappers_and_receipt_eviction(self):
        runtime, backend, _extractor = self.runtime()
        request, result, _story = self.recalled(runtime)
        first_wrapper = runtime.story_provider
        second_wrapper = StoryMemoryProvider(backend, runtime.story_memory)
        runtime.provider = second_wrapper
        first_wrapper._revisions.clear()
        second_wrapper._revisions.clear()
        calls = len(backend.calls)
        runtime.assert_reply_fresh('alice', request.request_id)
        self.assertEqual(runtime.handle(request).decision_id, result.decision_id)
        self.assertEqual(len(backend.calls), calls)
        runtime.story_memory.disable('alice')
        with self.assertRaises(MemoryChangedError):
            runtime.assert_reply_fresh('alice', request.request_id)
        with self.assertRaises(MemoryChangedError):
            result.to_dict()

    def test_legacy_reply_with_story_ledger_but_no_binding_fails_closed(self):
        runtime, _backend, _extractor = self.runtime()
        request, _result, _story = self.recalled(runtime)
        value = self.persisted(runtime, request)
        value.pop('story_binding')
        with runtime.conversation_store._lock:
            ledger = runtime.conversation_store._connection.execute(
                'SELECT stories_json FROM story_runtime_replies '
                'WHERE user_id=? AND request_id=?', ('alice', request.request_id),
            ).fetchone()
        self.assertTrue(json.loads(ledger[0]))
        self.replace_persisted(runtime, request, value)
        runtime.close()
        reopened, backend, _extractor = self.runtime()
        with self.assertRaises(MemoryChangedError):
            reopened.assert_reply_fresh('alice', request.request_id)
        with self.assertRaises(MemoryChangedError):
            reopened.handle(request)
        self.assertEqual(backend.calls, [])

    def test_ordinary_legacy_reply_without_story_dependencies_remains_usable(self):
        runtime, backend, _extractor = self.runtime(extractor=None)
        self.assertFalse(runtime.story_memory.policy('alice')['enabled'])
        request, result = self.turn(runtime, '안녕.')
        value = self.persisted(runtime, request)
        value.pop('story_binding', None)
        self.replace_persisted(runtime, request, value)
        runtime.assert_reply_fresh('alice', request.request_id)
        self.assertEqual(runtime.handle(request).decision_id, result.decision_id)
        self.assertEqual(len(backend.calls), 1)

    def test_disabled_story_with_short_history_keeps_a_private_lineage_binding(self):
        runtime, backend, _extractor = self.runtime()
        runtime.story_memory.enable('alice')
        self.turn(runtime, '전시에서 작품을 보니 즐거웠어.')
        self.checkpoint(runtime)
        runtime.story_memory.disable('alice')
        request, result = self.turn(runtime, '그 이야기를 계속해줘.')
        binding = self.persisted(runtime, request)['story_binding']
        self.assertEqual(binding, {
            'policy_revision': runtime.story_memory.policy('alice')['revision'],
            'readset': [],
        })
        self.assertNotIn('story_memory_untrusted', backend.calls[-1])
        self.assertEqual(result.to_dict()['request_id'], request.request_id)
        runtime.story_provider._revisions.clear()
        runtime.assert_reply_fresh('alice', request.request_id)

    def test_checkpoint_timeout_leaves_extraction_running_for_later_completion(self):
        extractor = FakeExtractor()
        extractor.release = threading.Event()
        runtime, _backend, _extractor = self.runtime(extractor=extractor)
        # Release before the runtime's cleanup attempts to join its worker.
        self.addCleanup(extractor.release.set)
        runtime.story_memory.enable('alice')
        self.turn(runtime, '전시에 새로운 작품이 있었어.')
        self.assertFalse(runtime.checkpoint_story_memory('alice', timeout=0.01))
        self.assertTrue(extractor.entered.wait(3))
        extractor.release.set()
        self.checkpoint(runtime)
        self.assertEqual(runtime.story_memory.list_stories('alice')[0]['title'], '전시')

    def test_factory_constructor_defers_previously_authorized_recovery(self):
        conversations = SQLiteConversationStore(self.path)
        memory = SQLiteMemoryStore(self.path)
        try:
            store = StoryRuntimeStore(conversations, memory)
            store.set_enabled('alice', True, external_consent=True)
            conversations.create('alice', 'prior')
            begun = conversations.begin_turn(
                'alice', 'prior', 'prior-turn', 'prior-request', 'prior-fingerprint',
                '전시에서 멋진 작품을 봤어.',
            )
            conversations.complete_turn(begun.token, '알겠어요.', {
                'schema_version': 1, 'decision': {'type': 'message', 'message': '알겠어요.',
                                                'tool_name': None, 'arguments': {}},
            })
            self.assertTrue(store.enqueue_completed('alice', 'prior-request'))
        finally:
            conversations.close()
            memory.close()
        runtime, backend, extractor = self.runtime()
        self.assertFalse(extractor.entered.wait(0.05))
        self.assertEqual(backend.calls, [])
        runtime.start_background_memory()
        self.assertTrue(extractor.entered.wait(3))
        self.checkpoint(runtime)
        self.assertEqual(runtime.story_memory.list_stories('alice')[0]['title'], '전시')

    def test_close_is_idempotent_and_stops_story_worker_before_sqlite(self):
        runtime, _backend, _extractor = self.runtime()
        runtime.story_memory.enable('alice')
        self.turn(runtime, '설명서는 한 장으로 만들어보자.')
        self.checkpoint(runtime)
        closed = []
        story_close = runtime.story_memory.close
        conversation_close = runtime.conversation_store.close
        memory_close = runtime.memory_store.close

        def stop_story():
            closed.append('story')
            story_close()

        def close_conversations():
            self.assertIn('story', closed)
            closed.append('conversations')
            conversation_close()

        def close_memory():
            self.assertIn('story', closed)
            closed.append('memory')
            memory_close()

        with patch.object(runtime.story_memory, 'close', side_effect=stop_story), \
                patch.object(runtime.conversation_store, 'close', side_effect=close_conversations), \
                patch.object(runtime.memory_store, 'close', side_effect=close_memory):
            runtime.close()
            runtime.close()
        self.assertEqual(closed.count('story'), 1)
        self.assertEqual(closed.count('conversations'), 1)
        self.assertEqual(closed.count('memory'), 1)


class StoryBindingPersistenceTests(unittest.TestCase):
    @staticmethod
    def persisted(schema_version=2):
        decision = AgentDecision(type='message', message='이전 이야기를 들었어요.')
        result = OrchestrationResult(
            request_id='reply-1', conversation_id='room', turn_id='turn-1',
            conversation_generation=1, conversation_revision=1, conversation_ordinal=1,
            raw_decision=decision, decision=decision,
            safety=SafetyResult(allowed=True, code='message', reason='message'),
            provider_result=ProviderResult(decision=decision, provider='fixture',
                                           model='fixture-v1', latency_ms=0.0),
            memory_ids=[], decision_id='decision-1', issued_at=100.0,
            expires_at=105.0, state_trusted=False, memory_revision=0,
        )
        value = result.to_persisted_dict()
        value['story_binding'] = {
            'policy_revision': 1,
            'readset': [{'story_id': 'story-1', 'entry_hashes': ['a' * 64]}],
        }
        if schema_version == 3:
            value['schema_version'] = 3
            value['safety_binding'] = {
                'state_evidence_id': 'evidence-1', 'state_observed_at': 99.0,
                'safety_policy_revision': 'policy-v1',
            }
        return value

    def test_binding_round_trips_privately_in_both_persisted_schemas(self):
        for schema in (2, 3):
            with self.subTest(schema=schema):
                value = self.persisted(schema)
                result = OrchestrationResult.from_persisted_dict(value)
                self.assertEqual(result.to_persisted_dict(), value)
                self.assertNotIn('story_binding', result.to_dict())
                self.assertNotIn('story_binding', result.to_dict(include_raw_decision=True))
                legacy = deepcopy(value)
                legacy.pop('story_binding')
                restored = OrchestrationResult.from_persisted_dict(legacy)
                self.assertNotIn('story_binding', restored.to_persisted_dict())

    def test_binding_does_not_share_mutable_input_or_serialized_output(self):
        value = self.persisted()
        expected = deepcopy(value['story_binding'])
        result = OrchestrationResult.from_persisted_dict(value)
        value['story_binding']['policy_revision'] = 999
        value['story_binding']['readset'][0]['entry_hashes'].clear()
        value['story_binding']['readset'].append({'story_id': 'injected', 'entry_hashes': []})
        self.assertEqual(result.to_persisted_dict()['story_binding'], expected)
        output = result.to_persisted_dict()
        output['story_binding']['readset'][0]['story_id'] = 'mutated'
        output['story_binding']['readset'][0]['entry_hashes'][0] = 'b' * 64
        self.assertEqual(result.to_persisted_dict()['story_binding'], expected)

    def test_malformed_story_bindings_are_rejected(self):
        valid_item = {'story_id': 'story-1', 'entry_hashes': ['a' * 64]}
        malformed = [
            None, [], {},
            {'policy_revision': True, 'readset': []},
            {'policy_revision': -1, 'readset': []},
            {'policy_revision': 1.0, 'readset': []},
            {'policy_revision': '1', 'readset': []},
            {'policy_revision': 1, 'readset': {}},
            {'policy_revision': 1, 'readset': [], 'extra': True},
            {'policy_revision': 1, 'readset': [{'story_id': '', 'entry_hashes': ['a' * 64]}]},
            {'policy_revision': 1, 'readset': [{'story_id': 1, 'entry_hashes': ['a' * 64]}]},
            {'policy_revision': 1, 'readset': [{'story_id': 'story-1', 'entry_hashes': []}]},
            {'policy_revision': 1, 'readset': [{'story_id': 'story-1', 'entry_hashes': 'a' * 64}]},
            {'policy_revision': 1, 'readset': [{'story_id': 'story-1', 'entry_hashes': ['invalid']}]},
            {'policy_revision': 1, 'readset': [{'story_id': 'story-1', 'entry_hashes': [True]}]},
            {'policy_revision': 1, 'readset': [dict(valid_item, extra=True)]},
            {'policy_revision': 1, 'readset': [valid_item, deepcopy(valid_item)]},
            {'policy_revision': 1, 'readset': [
                {'story_id': f'story-{number}', 'entry_hashes': ['a' * 64]}
                for number in range(21)
            ]},
            {'policy_revision': 1, 'readset': [
                {'story_id': 'story-1', 'entry_hashes': ['a' * 64] * 65},
            ]},
        ]
        for schema in (2, 3):
            for index, binding in enumerate(malformed):
                with self.subTest(schema=schema, binding=index):
                    value = self.persisted(schema)
                    value['story_binding'] = binding
                    with self.assertRaisesRegex(RuntimeError, 'stored orchestration response is invalid'):
                        OrchestrationResult.from_persisted_dict(value)


if __name__ == '__main__':
    unittest.main()
