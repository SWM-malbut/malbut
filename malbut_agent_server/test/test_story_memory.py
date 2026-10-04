"""Offline contract tests for consent-scoped story memory and original sources."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.story_memory import (
    SQLiteStoryMemoryStore,
    StoryConsentError,
    StoryConflictError,
    StoryEntry,
    StoryMemoryError,
    StorySourceError,
)
from malbut_agent_server.story_memory_sources import make_source_ref, read_source


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class StoryMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'story.sqlite3')
        self.clock = FakeClock()
        self.conversations = SQLiteConversationStore(
            self.path, semantic_context=True, clock=self.clock,
        )
        before = self._table_names()
        self.store = SQLiteStoryMemoryStore(self.path, clock=self.clock)
        self.story_tables = self._table_names() - before
        self.counter = 0
        self.original = '전시의 푸른 작품이 좋았지만 사람이 많아서 조금 지쳤어.'
        self.ref, self.assistant_ref = self._turn(self.original)

    def tearDown(self):
        self.store.close()
        self.conversations.close()
        self.temp.cleanup()

    def _table_names(self):
        return {row[0] for row in self.conversations._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}

    def _turn(self, text, *, user='alice', room='room-a', answer='다음엔 어땠어?'):
        self.counter += 1
        self.clock.now += 1
        session = self.conversations.create(user, room)
        begun = self.conversations.begin_turn(
            user, room, f'turn-{self.counter}', f'request-{self.counter}',
            f'fingerprint-{self.counter}', text,
        )
        self.conversations.complete_turn(begun.token, answer, {
            'schema_version': 1,
            'decision': {'type': 'message', 'message': answer,
                         'tool_name': None, 'arguments': {}},
        })
        args = (self.conversations._connection, user, room,
                session.session_instance_id, session.generation,
                begun.token.turn_id)
        return (make_source_ref(*args, 'user'),
                make_source_ref(*args, 'assistant'))

    def _allow(self, *refs, user='alice'):
        return self.store.set_consent(
            user, True, allowed_sources=refs or (self.ref,),
        )

    def _entry(self, text='전시의 첫 방문에서 피로를 느꼈다고 말했다.', *,
               ref=None, kind='experience', actor='user', status='stated'):
        return StoryEntry(text, kind, actor, status, ((ref or self.ref).key,))

    def _begin(self, job='job-1', *, story='exhibition', refs=None, user='alice'):
        return self.store.begin_checkpoint(
            user, story, job, tuple(refs or (self.ref,)),
        )

    def _commit(self, job='job-1', *, entries=None, user='alice', title='전시 방문'):
        entries = tuple(entries or (self._entry(),))
        return self.store.commit_checkpoint(
            user, job, title=title, aliases=('전시장',),
            current=entries, episode=entries,
        )

    def _saved(self):
        self._allow()
        self._begin()
        return self._commit()

    def _restart(self):
        self.store.close()
        self.store = SQLiteStoryMemoryStore(self.path, clock=self.clock)

    def _edit_original(self):
        conn = self.conversations._connection
        conn.execute(
            "UPDATE conversation_turns SET user_content=? "
            "WHERE user_id='alice' AND request_id='request-1'",
            ('다른 내용으로 바뀐 원문',),
        )
        conn.commit()

    def test_default_off_and_exact_source_scope(self):
        self.assertIsNone(self.store.get_story('alice', 'exhibition'))
        self.assertEqual(self.store.search('alice', '전시'), [])
        with self.assertRaises(StoryConsentError):
            self._begin()
        self._allow()
        other, _ = self._turn('설명서를 쉽게 고쳐 쓰고 싶어.')
        with self.assertRaises(StoryConsentError):
            self._begin(refs=(other,))

    def test_persisted_story_search_is_private_relevant_and_untrusted(self):
        saved = self._saved()
        self._restart()
        restored = self.store.get_story('alice', 'exhibition')
        self.assertEqual(restored, saved)
        self.assertEqual([s.story_id for s in self.store.search('alice', '전시')],
                         ['exhibition'])
        self.assertEqual(self.store.search('alice', '우주선 엔진 수리'), [])
        self.assertEqual(self.store.search('bob', '전시'), [])
        self.assertIsNone(self.store.get_story('bob', 'exhibition'))
        self.assertEqual(self.store.get_evidence('bob', 'exhibition'), [])
        self.assertTrue(restored.untrusted)
        self.assertFalse(restored.execution_authorized)

    def test_sources_are_references_and_evidence_keeps_exact_role_and_span(self):
        start, end = self.original.index('사람이'), len(self.original)
        session = self.conversations.get('alice', 'room-a')
        span = make_source_ref(self.conversations._connection, 'alice', 'room-a',
                               session.session_instance_id, session.generation,
                               'turn-1', 'user', start, end)
        self._allow(span)
        self._begin(refs=(span,))
        self._commit(entries=(self._entry(ref=span),))
        evidence = self.store.get_evidence('alice', 'exhibition')
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].ref, span)
        self.assertEqual(evidence[0].text, self.original[start:end])
        with self.assertRaises(StoryConsentError):
            self._begin('wider-source', refs=(self.ref,))
        self.assertTrue(self.story_tables)
        for table in self.story_tables:
            quoted = '"' + table.replace('"', '""') + '"'
            for row in self.conversations._connection.execute('SELECT * FROM ' + quoted):
                self.assertNotIn(self.original, json.dumps(tuple(row), ensure_ascii=False))

    def test_proposal_is_preserved_separately_from_user_decision(self):
        chosen, proposal = self._turn(
            '수정하기 쉬운 한 장 요약을 먼저 시험해 보자.',
            answer='그림 중심 구성도 제안할게.',
        )
        self._allow(chosen, proposal)
        self._begin(refs=(chosen, proposal))
        decision = self._entry('한 장 요약을 먼저 시험한다.', ref=chosen,
                               kind='decision', status='confirmed')
        suggested = self._entry('그림 중심 구성.', ref=proposal,
                                kind='decision', actor='assistant', status='proposed')
        saved = self._commit(entries=(decision, suggested))
        self.assertEqual(saved.current, (decision, suggested))
        self.assertEqual(saved.episodes, ((decision, suggested),))
        self.assertFalse(saved.execution_authorized)

    def test_assistant_cannot_be_confirmed_and_user_needs_user_source(self):
        self._allow(self.assistant_ref)
        for number, changes in enumerate((
            {'kind': 'decision', 'actor': 'assistant', 'status': 'confirmed'},
            {'actor': 'user'},
        )):
            with self.subTest(changes=changes):
                job = f'invalid-{number}'
                self._begin(job, refs=(self.assistant_ref,))
                with self.assertRaises(StoryMemoryError):
                    entry = self._entry(ref=self.assistant_ref, **changes)
                    self._commit(job, entries=(entry,))
        self.assertIsNone(self.store.get_story('alice', 'exhibition'))

    def test_prior_generation_evidence_remains_available_after_reset(self):
        saved = self._saved()
        self.conversations.reset('alice', 'room-a')
        self.assertEqual(self.conversations.list_turns('alice', 'room-a'), [])
        self.assertEqual(read_source(self.conversations._connection, 'alice', self.ref).text,
                         self.original)
        self.assertEqual(self.store.get_story('alice', 'exhibition'), saved)
        self._begin('after-reset')
        self.assertEqual(self._commit('after-reset').version, saved.version + 1)

    def test_disabled_memory_is_retained_but_not_read_or_written(self):
        saved = self._saved()
        self.store.set_consent('alice', False)
        self.assertIsNone(self.store.get_story('alice', 'exhibition'))
        self.assertEqual(self.store.search('alice', '전시'), [])
        self.assertEqual(self.store.get_evidence('alice', 'exhibition'), [])
        with self.assertRaises(StoryConsentError):
            self._begin('disabled')
        self._restart()
        self.store.set_consent('alice', True)
        self.assertEqual(self.store.get_story('alice', 'exhibition'), saved)
        self._begin('reenabled')
        self._commit('reenabled')

    def test_disable_enable_does_not_revive_pending_work(self):
        self._allow()
        self._begin()
        self.store.set_consent('alice', False)
        self.store.set_consent('alice', True)
        with self.assertRaises(StoryMemoryError):
            self._commit()
        self.assertIsNone(self.store.get_story('alice', 'exhibition'))

    def test_scope_change_invalidates_pending_work(self):
        self._allow()
        self._begin()
        other, _ = self._turn('다음 방문에는 한산해서 편안했어.')
        self._allow(self.ref, other)
        with self.assertRaises(StoryMemoryError):
            self._commit()
        self._begin('fresh', refs=(other,))
        self._commit('fresh', entries=(self._entry(ref=other),))

    def test_removing_source_scope_hides_previous_story(self):
        self._saved()
        other, _ = self._turn('새로 시작한 설명서 이야기야.')
        self._allow(other)
        self.assertIsNone(self.store.get_story('alice', 'exhibition'))
        self.assertEqual(self.store.search('alice', '전시'), [])
        self.assertEqual(self.store.get_evidence('alice', 'exhibition'), [])

    def test_entry_cannot_claim_evidence_outside_checkpoint_read_set(self):
        other, _ = self._turn('다시 가니 한산해서 편안했어.')
        self._allow(self.ref, other)
        self._begin()
        with self.assertRaises(StoryMemoryError):
            self._commit(entries=(self._entry(ref=other),))

    def test_pending_checkpoint_can_complete_after_restart(self):
        self._allow()
        self.assertEqual(self._begin(), 'job-1')
        self._restart()
        saved = self._commit()
        self.assertEqual(self.store.get_story('alice', 'exhibition'), saved)

    def test_duplicate_job_is_idempotent_and_changed_payload_conflicts(self):
        first = self._saved()
        self._restart()
        self.assertEqual(self._begin(), 'job-1')
        self.assertEqual(self._commit(), first)
        self.assertEqual(len(first.episodes), 1)
        with self.assertRaises(StoryConflictError):
            self._commit(title='다른 제목')
        self.assertEqual(self.store.get_story('alice', 'exhibition'), first)

    def test_concurrent_checkpoints_cannot_overwrite_newer_story_version(self):
        first = self._saved()
        self._begin('job-a')
        self._begin('job-b')
        second = self._commit('job-b')
        with self.assertRaises(StoryConflictError):
            self._commit('job-a')
        self.assertEqual(second.version, first.version + 1)
        self.assertEqual(self.store.get_story('alice', 'exhibition'), second)

    def test_modified_source_hides_story_and_rejects_pending_commit(self):
        self._saved()
        self._begin('pending')
        self._edit_original()  # Simulates stale evidence, not a supported deletion API.
        self.assertIsNone(read_source(self.conversations._connection, 'alice', self.ref))
        self.assertIsNone(self.store.get_story('alice', 'exhibition'))
        self.assertEqual(self.store.search('alice', '전시'), [])
        self.assertEqual(self.store.get_evidence('alice', 'exhibition'), [])
        with self.assertRaises(StorySourceError):
            self._commit('pending')

    def test_deleted_session_and_reused_ids_do_not_resurrect_old_evidence(self):
        self._saved()
        self._begin('pending')
        self.conversations.delete('alice', 'room-a')
        self.assertIsNone(self.store.get_story('alice', 'exhibition'))
        with self.assertRaises(StoryMemoryError):
            self._commit('pending')
        self.counter = 0
        new_ref, _ = self._turn(self.original)
        self.assertNotEqual(new_ref.key, self.ref.key)
        self.assertIsNone(read_source(self.conversations._connection, 'alice', self.ref))
        self.assertIsNone(self.store.get_story('alice', 'exhibition'))

    def test_invalid_owner_hash_and_range_are_never_returned(self):
        conn = self.conversations._connection
        self.assertIsNone(read_source(conn, 'bob', self.ref))
        with self.assertRaises(StoryMemoryError):
            self.store.set_consent('bob', True, allowed_sources=(self.ref,))
            self._begin(user='bob')
        for ref in (replace(self.ref, sha256='0' * 64),
                    replace(self.ref, end=len(self.original) + 100)):
            with self.subTest(ref=ref):
                self.assertIsNone(read_source(conn, 'alice', ref))
                with self.assertRaises(StorySourceError):
                    self._allow(ref)
                    self._begin(refs=(ref,))
        with self.assertRaises(ValueError):
            replace(self.ref, role='system')
        with self.assertRaises(ValueError):
            replace(self.ref, start=-1)

    def test_pending_turn_is_not_a_source(self):
        session = self.conversations.get('alice', 'room-a')
        pending = self.conversations.begin_turn(
            'alice', 'room-a', 'pending-turn', 'pending-request', 'fingerprint',
            '아직 완료하지 않은 요청',
        )
        try:
            with self.assertRaises(ValueError):
                make_source_ref(self.conversations._connection, 'alice', 'room-a',
                                session.session_instance_id, session.generation,
                                pending.token.turn_id, 'user')
        finally:
            self.conversations.fail_turn(pending.token)


if __name__ == '__main__':
    unittest.main()
