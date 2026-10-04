"""Real SQLite end-to-end checks with an explicitly injected offline extractor."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.story_memory_service import (
    StoryMemoryService, StoryServiceError, StoryUnavailableError,
)
from malbut_agent_server.story_runtime_store import StoryRuntimeStore


class FakeExtractor:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = None
        self.failing = False

    def extract(self, stories, sources):
        self.calls.append((stories, sources))
        self.entered.set()
        if self.release is not None and not self.release.wait(5):
            raise ProviderError('offline fixture was not released')
        if self.failing:
            raise ProviderError('synthetic extraction failure')
        grouped = {}
        for source in sources:
            if not source['id'].startswith('s') or source['role'] != 'user':
                continue
            for quote in source['text'].split(' || '):
                topic = ('전시' if '전시' in quote else '설명서' if '설명서' in quote else None)
                if topic is None:
                    continue
                prior = next((item for item in stories if item['title'] == topic), None)
                evidence = [{'source_id': source['id'], 'quote': quote}]
                item = {'text': quote, 'kind': 'experience', 'actor': 'user',
                        'status': 'stated', 'evidence': evidence}
                record = grouped.setdefault(topic, {
                    'story_id': prior['story_id'] if prior else None,
                    'title': topic, 'aliases': [topic], 'current': [],
                    'episode': [], 'source_spans': [],
                })
                record['current'] = [item]
                record['episode'].append(item)
                record['source_spans'].extend(evidence)
        return list(grouped.values())


class StoryMemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'runtime.sqlite3')
        self.conversations = SQLiteConversationStore(self.path, semantic_context=True)
        self.memory = SQLiteMemoryStore(self.path)
        self.extractor = FakeExtractor()
        self.service = self._service()
        self.sequence = 0

    def _service(self, extractor='fixture', **kwargs):
        return StoryMemoryService(
            self.conversations, self.memory,
            self.extractor if extractor == 'fixture' else extractor,
            debounce_seconds=0, max_delay_seconds=0, **kwargs,
        )

    def tearDown(self):
        if self.extractor.release is not None:
            self.extractor.release.set()
        self.service.close()
        self.memory.close()
        self.conversations.close()
        self.temp.cleanup()

    def _turn(self, text, *, user='alice', room='room', enqueue=True):
        self.sequence += 1
        request = f'request-{self.sequence}'
        self.conversations.create(user, room)
        begun = self.conversations.begin_turn(user, room, f'turn-{self.sequence}', request,
                                               f'fingerprint-{self.sequence}', text)
        answer = '알겠어요.'
        self.conversations.complete_turn(begun.token, answer, {
            'schema_version': 1, 'decision': {'type': 'message', 'message': answer,
                                            'tool_name': None, 'arguments': {}},
        })
        if enqueue:
            self.service.after_turn(user, request)
        return request

    def _save(self, text='전시의 작품이 좋아서 즐거웠어.'):
        self.service.enable('alice')
        self._turn(text)
        self.assertTrue(self.service.flush('alice', timeout=5))
        return self.service.list_stories('alice')

    def test_live_path_requires_extractor_and_default_off_does_not_call_it(self):
        self._turn('전시 이야기야.')
        self.assertFalse(self.service.policy('alice')['enabled'])
        self.assertFalse(self.service.context('alice', '전시')['enabled'])
        self.assertEqual(self.extractor.calls, [])
        self.service.close()
        self.service = self._service(extractor=None)
        with self.assertRaises(StoryUnavailableError):
            self.service.enable('alice')
        self.assertFalse(self.service.policy('alice')['enabled'])

    def test_complete_new_session_reopen_and_raw_evidence(self):
        original = self._save()[0]
        self.conversations.reset('alice', 'room')
        context = self.service.context('alice', '전시')
        self.assertEqual(context['stories'][0]['story_id'], original['story_id'])
        self.assertEqual(context['evidence'], [])
        self.assertNotIn('source_keys', context['stories'][0]['current'][0])
        exact = self.service.context('alice', '전시 원문은?')
        self.assertEqual(exact['evidence'][0]['story_id'], original['story_id'])
        self.assertIn('즐거웠어', exact['evidence'][0]['text'])
        self.service.close()
        self.service = self._service()
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertEqual(self.service.list_stories('alice')[0]['story_id'], original['story_id'])
        self.assertEqual(self.service.context('bob', '전시')['stories'], [])

    def test_foreground_does_not_wait_and_pending_hides_old_current(self):
        self._save()
        self.extractor.entered.clear()
        self.extractor.release = threading.Event()
        started = time.monotonic()
        self._turn('전시에서 즐거웠다는 건 정정할게. 사실 조금 지쳤어.')
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(self.extractor.entered.wait(2))
        context = self.service.context('alice', '전시')
        self.assertEqual(context['stories'], [])
        self.assertGreater(context['pending'], 0)
        self.assertFalse(self.service.flush('alice', timeout=0.01))
        self.extractor.release.set()
        self.assertTrue(self.service.flush('alice', timeout=5))
        current = self.service.context('alice', '전시')['stories'][0]['current']
        self.assertIn('지쳤어', current[0]['text'])

    def test_readset_allows_unrelated_updates_but_blocks_changed_claims(self):
        self._save()
        context = self.service.context('alice', '전시')
        self.assertEqual(len(context['readset']), 1)
        self.assertTrue(self.service.validate_readset('alice', context['revision'],
                                                     context['readset']))
        self._turn('설명서는 한 장으로 만들자.')
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertTrue(self.service.validate_readset('alice', context['revision'],
                                                     context['readset']))
        self._turn('전시에 대한 감정을 정정할게. 사실 지쳤어.')
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertFalse(self.service.validate_readset('alice', context['revision'],
                                                      context['readset']))

    def test_readset_allows_new_current_items_when_existing_claim_is_retained(self):
        self._save()
        context = self.service.context('alice', '전시')
        original_extract = self.extractor.extract
        def retain_existing(stories, sources):
            updates = original_extract(stories, sources)
            for update in updates:
                prior = next((story for story in stories
                              if story['story_id'] == update['story_id']), None)
                if prior:
                    update['current'] += prior['current']
            return updates
        self.extractor.extract = retain_existing
        self._turn('전시에는 다음 주에도 갈 생각이야.')
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertTrue(self.service.validate_readset('alice', context['revision'],
                                                     context['readset']))
        self.assertFalse(self.service.validate_readset('bob', context['revision'],
                                                      context['readset']))
        self.service.disable('alice')
        self.assertFalse(self.service.validate_readset('alice', context['revision'],
                                                      context['readset']))

    def test_readset_allows_only_own_completed_reply_to_refresh_existing_claims(self):
        self._save()
        context = self.service.context('alice', '전시')
        own_request = self._turn('전시를 생각하니 다시 즐거워졌어.')
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertTrue(self.service.validate_readset(
            'alice', context['revision'], context['readset'], request_id=own_request))
        self.assertFalse(self.service.validate_readset(
            'alice', context['revision'], context['readset'], request_id='request-1'))
        self._turn('전시가 즐거웠다는 말은 정정할게. 사실 지쳤어.')
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertFalse(self.service.validate_readset(
            'alice', context['revision'], context['readset'], request_id=own_request))

    def test_disable_during_extraction_discards_late_result_and_retains_old_story(self):
        original = self._save()[0]
        self.extractor.release = threading.Event()
        self.extractor.entered.clear()
        self._turn('전시를 다시 보니 한산했어.')
        self.assertTrue(self.extractor.entered.wait(2))
        revision = self.service.policy('alice')['revision']
        self.service.disable('alice')
        self.assertFalse(self.service.validate('alice', revision))
        self.extractor.release.set()
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertEqual(self.service.context('alice', '전시')['stories'], [])
        self.assertEqual(self.service.list_stories('alice')[0]['story_id'], original['story_id'])
        self.service.enable('alice')
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertEqual(self.service.list_stories('alice')[0]['version'], original['version'])

    def test_disabled_period_is_not_implicitly_backfilled_on_reenable(self):
        self._save()
        self.service.disable('alice')
        self._turn('설명서는 한 장으로 먼저 시험해 보자.')
        self.service.enable('alice')
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertFalse(any(item['title'] == '설명서' for item in self.service.list_stories('alice')))
        scope = self.service.history_preview('alice')
        self.service.enable('alice', include_history=True, history_scope=scope)
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertTrue(any(item['title'] == '설명서' for item in self.service.list_stories('alice')))

    def test_history_preview_does_not_grant_later_completed_turn(self):
        self._turn('전시에 처음 가봤어.', enqueue=False)
        preview = self.service.history_preview('alice')
        self._turn('설명서는 아직 정하지 않았어.', enqueue=False)
        self.service.enable('alice', include_history=True, history_scope=preview)
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertEqual([item['title'] for item in self.service.list_stories('alice')], ['전시'])

    def test_one_story_delete_preserves_other_part_of_same_turn(self):
        stories = self._save('전시의 작품이 좋았어. || 설명서는 한 장으로 먼저 시험하자.')
        exhibit = next(item for item in stories if item['title'] == '전시')
        guide = next(item for item in stories if item['title'] == '설명서')
        revision = self.service.policy('alice')['revision']
        self.service.forget('alice', exhibit['story_id'])
        self.assertFalse(self.service.validate('alice', revision))
        remaining = self.service.list_stories('alice')
        self.assertEqual([item['story_id'] for item in remaining], [guide['story_id']])
        raw = self.conversations.list_turns('alice', 'room')[0].user_content
        self.assertNotIn('전시의 작품', raw)
        self.assertIn('설명서는 한 장', raw)
        self.service.close()
        self.service = self._service()
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertEqual([item['story_id'] for item in self.service.list_stories('alice')],
                         [guide['story_id']])

    def test_delete_waits_for_pending_paraphrase_scope_and_never_claims_false_success(self):
        original = self._save()[0]
        self.extractor.entered.clear()
        self.extractor.release = threading.Event()
        self._turn('전시가 즐거웠다는 이야기를 다시 해볼게.')
        self.assertTrue(self.extractor.entered.wait(2))
        with self.assertRaisesRegex(StoryServiceError, '삭제 범위를 확정하지 못했어요'):
            self.service.forget('alice', original['story_id'], timeout=0.01)
        self.assertEqual(len(self.service.list_stories('alice')), 1)
        self.assertEqual(self.service.context('alice', '전시')['stories'], [])
        self.extractor.release.set()
        self.service.forget('alice', original['story_id'], timeout=5)
        self.assertEqual(self.service.list_stories('alice'), [])
        raw = ' '.join(turn.user_content for turn in
                       self.conversations.list_turns('alice', 'room'))
        self.assertNotIn('전시', raw)

    def test_disabled_unprocessed_sources_require_review_before_delete(self):
        original = self._save()[0]
        self.extractor.entered.clear()
        self.extractor.release = threading.Event()
        self._turn('전시가 즐거웠다는 말을 다시 하고 싶어.')
        self.assertTrue(self.extractor.entered.wait(2))
        self.service.disable('alice')
        self.extractor.release.set()
        calls = len(self.extractor.calls)
        with self.assertRaisesRegex(StoryServiceError, '다시 동의'):
            self.service.forget('alice', original['story_id'], timeout=1)
        self.assertEqual(len(self.extractor.calls), calls)
        preview = self.service.history_preview('alice')
        self.service.enable('alice', include_history=True, history_scope=preview)
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.service.forget('alice', original['story_id'], timeout=5)
        self.assertEqual(self.service.list_stories('alice'), [])

    def test_recall_only_retains_current_and_links_new_raw_for_deletion(self):
        original = self._save()[0]
        def coverage_only(stories, sources):
            prior = next(story for story in stories if story['story_id'] == original['story_id'])
            return [{'story_id': prior['story_id'], 'title': prior['title'],
                     'aliases': prior['aliases'], 'current': prior['current'], 'episode': [],
                     'source_spans': [{'source_id': source['id'], 'quote': source['text']}
                                      for source in sources if source['id'].startswith('s')]}]
        self.extractor.extract = coverage_only
        self._turn('전시에 대한 기억을 다시 말해줘.')
        self.assertTrue(self.service.flush('alice', timeout=5))
        updated = self.service.list_stories('alice')[0]
        self.assertEqual(updated['current'], original['current'])
        self.assertEqual(updated['source_time'], original['source_time'])
        self.service.forget('alice', original['story_id'], timeout=5)
        self.assertFalse(any('전시' in turn.user_content for turn in
                             self.conversations.list_turns('alice', 'room')))

    def test_completion_enqueue_gap_recovers_after_restart(self):
        self.service.enable('alice')
        self._turn('전시에서 새로운 작품을 봤어.', enqueue=False)
        self.service.close()
        self.service = self._service()
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertEqual(len(self.service.list_stories('alice')), 1)
        self.assertFalse(self.service.after_turn('alice', 'request-1'))

    def test_restart_recovers_expired_claim_without_new_user_input(self):
        self.service.enable('alice')
        self.service.close()
        request = self._turn('전시의 원문이 보존돼 있어.', enqueue=False)
        store = StoryRuntimeStore(self.conversations, self.memory)
        self.assertTrue(store.enqueue_completed('alice', request))
        self.assertIsNotNone(store.claim('alice'))
        conn = self.conversations._connection
        conn.execute('UPDATE story_runtime_jobs SET lease_until=? WHERE user_id=?',
                     (time.time() + 0.1, 'alice'))
        conn.commit()
        self.extractor.entered.clear()
        self.service = self._service()
        self.assertTrue(self.extractor.entered.wait(3))
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertEqual(len(self.service.list_stories('alice')), 1)

    def test_history_larger_than_pending_limit_refills_until_complete(self):
        for number in range(5):
            self._turn(f'전시에서 {number}번 작품을 봤어.', enqueue=False)
        scope = self.service.history_preview('alice')
        with patch('malbut_agent_server.story_runtime_store.MAX_PENDING', 2):
            self.service.enable('alice', include_history=True, history_scope=scope)
            self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertEqual(self.service.store.stats('alice')['done'], 5)
        self.assertIn('4번', self.service.list_stories('alice')[0]['current'][0]['text'])

    def test_completed_turns_are_batched_into_one_extraction(self):
        for number in range(3):
            self._turn(f'전시에서 {number}번 작품을 봤어.', enqueue=False)
        scope = self.service.history_preview('alice')
        self.service.enable('alice', include_history=True, history_scope=scope)
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertEqual(len(self.extractor.calls), 1)
        sources = self.extractor.calls[0][1]
        self.assertEqual(len([source for source in sources if source['role'] == 'user']), 3)
        self.assertEqual(self.service.store.stats('alice')['done'], 3)
        story = self.service.list_stories('alice')[0]
        self.assertIn('2번', story['current'][0]['text'])

    def test_invalid_batch_size_rejected_before_worker_creation(self):
        for size in (False, 0, 4, 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self._service(batch_size=size)

    def test_bounded_failure_is_visible_and_only_explicit_retry_recovers(self):
        self.service.enable('alice')
        self.extractor.failing = True
        self._turn('전시가 기대돼.')
        self.assertFalse(self.service.flush('alice', timeout=12))
        policy = self.service.policy('alice')
        self.assertEqual(policy['failed'], 1)
        self.assertTrue(policy['error'])
        attempts = len(self.extractor.calls)
        self.assertFalse(self.service.flush('alice', timeout=0.2))
        self.assertEqual(len(self.extractor.calls), attempts)
        self.assertEqual(self.service.context('alice', '전시')['stories'], [])
        self.extractor.failing = False
        self.assertTrue(self.service.flush('alice', timeout=5, retry_failed=True))
        self.assertEqual(len(self.service.list_stories('alice')), 1)

    def test_successful_automatic_retry_clears_old_error(self):
        original_extract = self.extractor.extract
        attempts = []
        def transient_failure(stories, sources):
            attempts.append(1)
            if len(attempts) == 1:
                raise ProviderError('synthetic transient failure')
            return original_extract(stories, sources)
        self.extractor.extract = transient_failure
        self.service.enable('alice')
        self._turn('전시가 즐거웠어.')
        self.assertTrue(self.service.flush('alice', timeout=5))
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(self.service.policy('alice')['error'])


if __name__ == '__main__':
    unittest.main()
