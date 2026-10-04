"""Common runtime story lifecycle and durable reply guards, entirely offline."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
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
    AgentDecision, AgentRequest, ProviderResult, ValidationError,
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
