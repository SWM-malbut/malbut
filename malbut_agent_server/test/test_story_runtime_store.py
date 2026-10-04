"""Offline runtime storage contracts using real conversation and memory tables."""

from copy import deepcopy
import json

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.story_memory import StoryMemoryError
from malbut_agent_server.story_runtime_store import StoryRuntimeStore
from malbut_agent_server.summarization import SummaryResult


class RuntimeHarness:
    """Complete synthetic turns without a provider, robot, or running worker."""

    def __init__(self, path):
        self.path = str(path)
        self.now = 1000.0
        self.counter = 0
        self.open()

    def open(self):
        self.conversations = SQLiteConversationStore(
            self.path, semantic_context=True, clock=lambda: self.now,
        )
        self.memories = SQLiteMemoryStore(self.path)
        self.memories.bind_connection(
            self.conversations._connection, self.conversations._lock,
        )
        self.store = StoryRuntimeStore(
            self.conversations, self.memories, clock=lambda: self.now,
        )

    def close(self):
        self.memories.close()
        self.conversations.close()

    def reopen(self):
        self.close()
        self.open()

    def allow(self, user='alice', **kwargs):
        self.now += 1
        return self.store.set_enabled(
            user, True, external_consent=True, **kwargs,
        )

    def begin(self, text='전시 안내를 한 장으로 만들고 싶어.', *,
              user='alice', room='room-a'):
        self.counter += 1
        self.now += 1
        self.conversations.create(user, room)
        return self.conversations.begin_turn(
            user, room, f'turn-{self.counter}', f'request-{self.counter}',
            f'fingerprint-{self.counter}', text,
        )

    def complete(self, text='전시 안내를 한 장으로 만들고 싶어.', *,
                 answer='그림 중심의 안내도 제안할게.', user='alice',
                 room='room-a'):
        begun = self.begin(text, user=user, room=room)
        self.now += 1
        self.conversations.complete_turn(begun.token, answer, {
            'schema_version': 1,
            'decision': {'type': 'message', 'message': answer,
                         'tool_name': None, 'arguments': {}},
        })
        return begun.token

    def claim_turn(self, text='전시 안내를 한 장으로 만들고 싶어.', **kwargs):
        token = self.complete(text, **kwargs)
        assert self.store.enqueue_completed(token.user_id, token.request_id)
        job = self.store.claim(token.user_id)
        assert job is not None
        return token, job

    def row(self, token):
        row = self.conversations._connection.execute(
            'SELECT * FROM conversation_turns WHERE user_id=? '
            'AND request_id=?', (token.user_id, token.request_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def source(self, job, *, role='user', containing=None):
        sources = self.store.job_source(job)
        assert sources is not None
        return next(source for source in sources if (
            source['role'] == role
            and (containing is None or containing in source['text'])
        ))

    def update(self, job, *, title='전시 안내', text=None, quote=None,
               source=None, story_id=None, actor='user', status='stated',
               kind='goal'):
        source = source or self.source(job)
        entry = {
            'text': text or '한 장짜리 전시 안내를 만들고 싶다고 말했다.',
            'kind': kind, 'actor': actor, 'status': status,
            'evidence': [{'source_id': source['id'],
                          'quote': quote or source['text']}],
        }
        return {
            'story_id': story_id, 'title': title, 'aliases': ['전시장'],
            'current': [entry], 'episode': [deepcopy(entry)],
        }

    def saved(self, text='전시 안내를 한 장으로 만들고 싶어.', **kwargs):
        token, job = self.claim_turn(text, **kwargs)
        assert self.store.commit(job, [self.update(job)])
        stories = self.store.list_stories(token.user_id)
        assert len(stories) == 1
        return token, job, stories[0]


@pytest.fixture
def runtime(tmp_path):
    harness = RuntimeHarness(tmp_path / 'stories.sqlite3')
    yield harness
    harness.close()


def _claim_three_turns(runtime):
    tokens = []
    for text in (
        '전시 안내를 만들고 싶어.',
        '한 장 안내와 작은 책자를 비교하고 있어.',
        '수정하기 쉬워서 한 장 안내를 선택했어.',
    ):
        token = runtime.complete(text)
        assert runtime.store.enqueue_completed('alice', token.request_id)
        tokens.append(token)
    job = runtime.store.claim('alice', batch_size=3)
    assert job is not None
    assert len(job['batch_job_ids']) == 3
    return tokens, job


def test_default_off_does_not_queue_or_disclose_completed_sources(runtime):
    token = runtime.complete()
    policy = runtime.store.policy('alice')
    assert policy['enabled'] is False
    assert policy['external_consent'] is False
    assert not runtime.store.enqueue_completed('alice', token.request_id)
    assert runtime.store.claim('alice') is None
    assert runtime.store.candidates('alice', '전시') == []
    assert runtime.store.list_stories('alice') == []
    assert not runtime.store.validate('alice', policy['revision'])


def test_external_processing_needs_its_own_consent(runtime):
    policy = runtime.store.set_enabled('alice', True)
    assert policy['enabled'] and not policy['external_consent']
    token = runtime.complete()
    assert not runtime.store.enqueue_completed('alice', token.request_id)
    runtime.store.recover('alice')
    assert runtime.store.claim('alice') is None


def test_new_consent_does_not_silently_include_existing_turns(runtime):
    old = runtime.complete('예전에는 작은 책자를 만들까 생각했어.')
    policy = runtime.allow()
    assert policy['enabled'] and policy['external_consent']
    assert runtime.store.validate('alice', policy['revision'])
    assert not runtime.store.enqueue_completed('alice', old.request_id)
    new, job = runtime.claim_turn('지금은 전시 안내를 한 장으로 만들고 싶어.')
    sources = runtime.store.job_source(job)
    assert {source['ref']['turn_id'] for source in sources} == {new.turn_id}
    assert '예전에는' not in json.dumps(sources, ensure_ascii=False)


def test_pending_and_other_users_turns_cannot_be_enqueued(runtime):
    runtime.allow()
    pending = runtime.begin()
    assert not runtime.store.enqueue_completed('alice', pending.token.request_id)
    runtime.conversations.fail_turn(pending.token)
    other = runtime.complete(user='bob', room='bob-room')
    assert not runtime.store.enqueue_completed('alice', other.request_id)
    assert runtime.store.claim('alice') is None


def test_explicit_history_opt_in_covers_completed_history(runtime):
    historical = runtime.complete('전시 안내는 수정하기 쉬워야 해.')
    runtime.allow(include_history=True)
    runtime.store.recover('alice')
    job = runtime.store.claim('alice')
    assert job is not None
    assert {source['ref']['turn_id'] for source in runtime.store.job_source(job)} == {
        historical.turn_id,
    }
    assert runtime.store.commit(job, [runtime.update(job)])
    assert runtime.store.candidates('alice', '전시 안내')


def test_history_preview_does_not_grant_turn_completed_while_user_decides(runtime):
    historical = runtime.complete('전시 안내는 수정하기 쉬워야 해.')
    pending = runtime.begin('동의 화면을 읽는 동안 새 이야기가 완료되었어.')
    preview = runtime.store.history_preview('alice')
    assert preview['turn_count'] == 1
    runtime.now += 1
    runtime.conversations.complete_turn(pending.token, '새 이야기를 들었어.', {})
    runtime.allow(include_history=True, history_scope=preview)
    assert not runtime.store.enqueue_completed('alice', pending.token.request_id)
    runtime.store.recover('alice')
    job = runtime.store.claim('alice')
    assert job is not None
    assert {s['ref']['turn_id'] for s in runtime.store.job_source(job)} == {
        historical.turn_id,
    }
    assert runtime.store.commit(job, [runtime.update(job)])
    assert runtime.store.claim('alice') is None
    _new, fresh = runtime.claim_turn('동의한 뒤에는 안내를 한 장으로 만들자.')
    assert pending.token.turn_id not in {
        s['ref']['turn_id'] for s in runtime.store.job_source(fresh)
    }


def test_history_preview_excludes_source_modified_after_preview(runtime):
    historical = runtime.complete()
    preview = runtime.store.history_preview('alice')
    connection = runtime.conversations._connection
    connection.execute(
        'UPDATE conversation_turns SET user_content=? '
        'WHERE user_id=? AND request_id=?',
        ('미리보기 뒤에 수정된 이야기야.', 'alice', historical.request_id),
    )
    connection.commit()
    runtime.allow(include_history=True, history_scope=preview)
    assert not runtime.store.enqueue_completed('alice', historical.request_id)
    assert runtime.store.recover('alice') == 0
    assert runtime.store.claim('alice') is None


def test_rowid_reuse_after_session_deletion_does_not_exclude_new_allowed_turn(runtime):
    old = runtime.complete('동의 전에 나눈 대화야.')
    connection = runtime.conversations._connection
    old_rowid = connection.execute(
        'SELECT rowid FROM conversation_turns WHERE user_id=? AND request_id=?',
        ('alice', old.request_id),
    ).fetchone()[0]
    runtime.allow()
    runtime.conversations.delete('alice', old.conversation_id)
    new = runtime.complete('동의 후에는 전시 안내를 한 장으로 만들자.')
    new_rowid = connection.execute(
        'SELECT rowid FROM conversation_turns WHERE user_id=? AND request_id=?',
        ('alice', new.request_id),
    ).fetchone()[0]
    assert new_rowid == old_rowid
    assert runtime.store.enqueue_completed('alice', new.request_id)
    job = runtime.store.claim('alice')
    assert job is not None
    assert {s['ref']['turn_id'] for s in runtime.store.job_source(job)} == {
        new.turn_id,
    }


def test_disabled_interval_is_not_retroactively_granted_on_reenable(runtime):
    runtime.allow()
    runtime.saved()
    runtime.store.set_enabled('alice', False)
    gap = runtime.complete('기억을 끈 동안 비공개 계획을 논의했어.')
    runtime.allow()
    assert not runtime.store.enqueue_completed('alice', gap.request_id)
    runtime.store.recover('alice')
    assert runtime.store.claim('alice') is None
    new, job = runtime.claim_turn('전시 안내에 글자 크기도 살펴보자.')
    source_turns = {s['ref']['turn_id'] for s in runtime.store.job_source(job)}
    assert new.turn_id in source_turns
    assert gap.turn_id not in source_turns


def test_recovery_finds_unqueued_completion_after_reopening_database(runtime):
    runtime.allow()
    token = runtime.complete()
    runtime.reopen()
    assert runtime.store.policy('alice')['enabled']
    assert runtime.store.recover('alice') >= 1
    job = runtime.store.claim('alice')
    assert job is not None
    assert {s['ref']['turn_id'] for s in runtime.store.job_source(job)} == {
        token.turn_id,
    }
    assert runtime.store.commit(job, [runtime.update(job)])
    runtime.reopen()
    assert runtime.store.recover('alice') == 0
    assert runtime.store.claim('alice') is None
    assert len(runtime.store.candidates('alice', '전시')) == 1


def test_duplicate_enqueue_and_commit_do_not_duplicate_story_or_episode(runtime):
    runtime.allow()
    token, job = runtime.claim_turn()
    assert not runtime.store.enqueue_completed('alice', token.request_id)
    updates = [runtime.update(job)]
    assert runtime.store.commit(job, updates)
    before = deepcopy(runtime.store.list_stories('alice'))
    runtime.store.commit(job, updates)
    assert runtime.store.list_stories('alice') == before
    assert not runtime.store.enqueue_completed('alice', token.request_id)
    assert runtime.store.claim('alice') is None


def test_later_checkpoint_can_cite_prior_evidence_with_unified_source_ids(runtime):
    runtime.allow()
    first, _first_job, story = runtime.saved()
    later, job = runtime.claim_turn('수정하기 쉬워서 한 장 안내를 선택했어.')
    sources = runtime.store.job_source(job)
    prior_source = next(item for item in sources if (
        item['ref']['turn_id'] == first.turn_id and item['role'] == 'user'
    ))
    new_source = next(item for item in sources if (
        item['ref']['turn_id'] == later.turn_id and item['role'] == 'user'
    ))
    update = runtime.update(
        job, source=new_source, story_id=story['story_id'], kind='decision',
        status='confirmed', text='수정하기 쉬운 한 장 안내를 선택했다.',
    )
    prior = runtime.update(job, source=prior_source)['current'][0]
    update['current'].insert(0, prior)
    assert runtime.store.commit(job, [update])
    restored = runtime.store.list_stories('alice')
    assert len(restored) == 1
    assert restored[0]['story_id'] == story['story_id']
    assert restored[0]['version'] == story['version'] + 1
    assert [item['text'] for item in restored[0]['current']] == [
        prior['text'], '수정하기 쉬운 한 장 안내를 선택했다.',
    ]
    assert len(restored[0]['episodes']) == len(story['episodes']) + 1


def test_three_turn_batch_commits_one_episode_and_completes_every_member(runtime):
    runtime.allow()
    tokens, job = _claim_three_turns(runtime)
    sources = runtime.store.job_source(job)
    assert {s['ref']['turn_id'] for s in sources} == {
        token.turn_id for token in tokens
    }
    entries = [runtime.update(
        job, source=source, text=source['text'], kind='context',
    )['current'][0] for source in sources if source['role'] == 'user']
    update = runtime.update(job)
    update['current'] = entries
    update['episode'] = deepcopy(entries)
    assert runtime.store.commit(job, [update])
    stats = runtime.store.stats('alice')
    assert stats['done'] == 3 and stats['running'] == 0 and stats['queued'] == 0
    stories = runtime.store.list_stories('alice')
    assert len(stories) == 1 and len(stories[0]['episodes']) == 1
    assert len(stories[0]['current']) == 3
    assert {s['ref']['turn_id'] for s in runtime.store.evidence(
        'alice', stories[0]['story_id'],
    )} == {token.turn_id for token in tokens}
    runtime.reopen()
    assert runtime.store.commit(job, [update])
    assert runtime.store.list_stories('alice') == stories
    assert runtime.store.recover('alice') == 0
    assert runtime.store.claim('alice', batch_size=3) is None


def test_changed_middle_source_rejects_entire_batch_without_partial_results(runtime):
    runtime.allow()
    tokens, job = _claim_three_turns(runtime)
    updates = [runtime.update(job)]
    connection = runtime.conversations._connection
    connection.execute(
        'UPDATE conversation_turns SET user_content=? '
        'WHERE user_id=? AND request_id=?',
        ('중간 발화가 정정되었어.', 'alice', tokens[1].request_id),
    )
    connection.commit()
    assert runtime.store.job_source(job) is None
    assert not runtime.store.commit(job, updates)
    assert runtime.store.list_stories('alice') == []
    assert runtime.store.stats('alice')['done'] == 0


def test_batch_failures_back_off_and_exhaust_all_three_members_together(runtime):
    runtime.allow()
    _tokens, job = _claim_three_turns(runtime)
    identifiers = job['batch_job_ids']
    for attempt in range(1, 4):
        runtime.store.fail(job, RuntimeError('synthetic batch failure'))
        stats = runtime.store.stats('alice')
        assert stats['running'] == 0 and stats['done'] == 0
        assert stats['queued' if attempt < 3 else 'failed'] == 3
        assert runtime.store.claim('alice', batch_size=3) is None
        runtime.now += 2 ** attempt
        if attempt < 3:
            job = runtime.store.claim('alice', batch_size=3)
            assert job is not None
            assert job['batch_job_ids'] == identifiers
    assert runtime.store.recover('alice') == 0
    assert runtime.store.claim('alice', batch_size=3) is None
    assert runtime.store.list_stories('alice') == []


def test_own_reply_matches_only_last_batch_member_even_with_identical_timestamps(runtime):
    runtime.allow()
    tokens = []
    for index in range(3):
        runtime.now = 2000.0
        token = runtime.complete(f'전시 안내의 {index + 1}번째 내용을 논의했어.')
        tokens.append(token)
        assert runtime.store.enqueue_completed('alice', token.request_id)
    assert len({runtime.row(token)['completed_at'] for token in tokens}) == 1
    job = runtime.store.claim('alice', batch_size=3)
    assert job is not None
    update = runtime.update(job)
    entries = [runtime.update(job, source=source)['current'][0]
               for source in runtime.store.job_source(job)
               if source['role'] == 'user']
    update['current'], update['episode'] = entries, deepcopy(entries)
    assert runtime.store.commit(job, [update])
    story = runtime.store.list_stories('alice')[0]
    for index, token in enumerate(tokens):
        assert runtime.store.own_reply_updated_story(
            'alice', story['story_id'], token.request_id,
        ) is (index == 2)
    assert not runtime.store.own_reply_updated_story(
        'bob', story['story_id'], tokens[-1].request_id,
    )
    connection = runtime.conversations._connection
    connection.execute(
        'UPDATE conversation_turns SET user_content=? '
        'WHERE user_id=? AND request_id=?',
        ('마지막 원문이 정정되었어.', 'alice', tokens[-1].request_id),
    )
    connection.commit()
    assert not runtime.store.own_reply_updated_story(
        'alice', story['story_id'], tokens[-1].request_id,
    )


def test_later_story_update_revokes_old_own_reply_match_at_same_clock_time(runtime):
    runtime.allow()
    first, _job, story = runtime.saved()
    assert runtime.store.own_reply_updated_story(
        'alice', story['story_id'], first.request_id,
    )
    runtime.now = runtime.row(first)['completed_at'] - 2
    later, job = runtime.claim_turn('전시 안내에 표지를 추가하자.')
    assert runtime.row(later)['completed_at'] == runtime.row(first)['completed_at']
    update = runtime.update(
        job, story_id=story['story_id'], text='전시 안내에 표지를 추가한다.',
    )
    assert runtime.store.commit(job, [update])
    assert not runtime.store.own_reply_updated_story(
        'alice', story['story_id'], first.request_id,
    )
    assert runtime.store.own_reply_updated_story(
        'alice', story['story_id'], later.request_id,
    )


@pytest.mark.parametrize('location', ['current', 'source_spans'])
def test_prior_story_evidence_cannot_be_copied_into_a_new_story(runtime, location):
    runtime.allow()
    runtime.saved()
    _token, job = runtime.claim_turn('전시 안내를 다음에 어떻게 수정할까?')
    prior = next(source for source in runtime.store.job_source(job)
                 if source['id'].startswith('p') and source['role'] == 'user')
    update = runtime.update(job, title='새로운 이야기')
    if location == 'current':
        update = runtime.update(job, source=prior, title='새로운 이야기')
    else:
        update['source_spans'] = [{
            'source_id': prior['id'], 'quote': prior['text'],
        }]
    before = deepcopy(runtime.store.list_stories('alice'))
    try:
        accepted = runtime.store.commit(job, [update])
    except ValueError:
        accepted = False
    assert accepted is False
    assert runtime.store.list_stories('alice') == before


def test_completed_story_is_recalled_in_new_session_and_is_user_isolated(runtime):
    runtime.allow()
    _token, _job, story = runtime.saved()
    runtime.conversations.create('alice', 'room-b')
    matches = runtime.store.candidates('alice', '전시 안내')
    assert [s['story_id'] for s in matches] == [story['story_id']]
    assert matches[0]['untrusted'] is True
    assert matches[0]['execution_authorized'] is False
    assert runtime.store.candidates('alice', '화성 우주선 엔진') == []
    assert runtime.store.candidates('bob', '전시 안내') == []
    assert runtime.store.list_stories('bob') == []
    assert runtime.store.evidence('bob', story['story_id']) == []
    runtime.allow('bob')
    assert runtime.store.candidates('bob', '전시 안내') == []


def test_policy_change_fences_claimed_job_even_after_reenable(runtime):
    original = runtime.allow()
    _token, job = runtime.claim_turn()
    updates = [runtime.update(job)]
    runtime.store.set_enabled('alice', False)
    runtime.allow()
    assert not runtime.store.validate('alice', original['revision'])
    assert runtime.store.job_source(job) is None
    assert not runtime.store.commit(job, updates)
    assert runtime.store.list_stories('alice') == []


def test_source_correction_fences_job_before_model_input_and_commit(runtime):
    runtime.allow()
    token, job = runtime.claim_turn()
    updates = [runtime.update(job)]
    connection = runtime.conversations._connection
    connection.execute(
        'UPDATE conversation_turns SET user_content=? '
        'WHERE user_id=? AND request_id=?',
        ('전시 대신 다른 행사를 논의했어.', 'alice', token.request_id),
    )
    connection.commit()
    assert runtime.store.job_source(job) is None
    assert not runtime.store.commit(job, updates)
    assert runtime.store.list_stories('alice') == []


def test_missing_original_hides_derived_story_and_its_evidence(runtime):
    runtime.allow()
    token, _job, story = runtime.saved()
    runtime.conversations.delete('alice', token.conversation_id)
    assert runtime.store.candidates('alice', '전시 안내') == []
    assert runtime.store.evidence('alice', story['story_id']) == []


def test_failures_retry_with_backoff_but_stop_after_three_attempts(runtime):
    runtime.allow()
    _token, job = runtime.claim_turn()
    for attempt in range(1, 4):
        runtime.store.fail(job, 'synthetic provider failure')
        assert runtime.store.claim('alice') is None
        runtime.now += 2 ** attempt
        if attempt < 3:
            job = runtime.store.claim('alice')
            assert job is not None
    runtime.store.recover('alice')
    assert runtime.store.claim('alice') is None
    assert runtime.store.list_stories('alice') == []


def test_explicit_retry_restarts_technical_failures_but_not_cancelled_jobs(runtime):
    runtime.allow()
    _token, job = runtime.claim_turn()
    for attempt in range(1, 4):
        runtime.store.fail(job, RuntimeError('synthetic temporary failure'))
        runtime.now += 2 ** attempt
        if attempt < 3:
            job = runtime.store.claim('alice')
            assert job is not None
    assert runtime.store.retry_failed('alice') == 1
    retried = runtime.store.claim('alice')
    assert retried is not None
    assert runtime.store.commit(retried, [runtime.update(retried)])
    assert runtime.store.stats('alice')['last_error'] is None
    _later, pending = runtime.claim_turn('전시 안내에 여백도 넣어 보자.')
    runtime.store.set_enabled('alice', False)
    runtime.allow()
    assert runtime.store.retry_failed('alice') == 0
    assert runtime.store.job_source(pending) is None
    assert runtime.store.claim('alice') is None


def test_successful_automatic_retry_clears_error_and_cancelled_errors_are_inactive(runtime):
    runtime.allow()
    token, job = runtime.claim_turn()
    runtime.store.fail(job, RuntimeError('synthetic transient error'))
    assert runtime.store.stats('alice')['last_error'] == 'RuntimeError'
    runtime.now += 2
    retried = runtime.store.claim('alice')
    assert retried is not None
    assert runtime.store.commit(retried, [runtime.update(retried)])
    assert runtime.store.stats('alice')['last_error'] is None
    row = runtime.conversations._connection.execute(
        'SELECT error_code FROM story_runtime_jobs WHERE user_id=? AND request_id=?',
        ('alice', token.request_id),
    ).fetchone()
    assert row['error_code'] is None
    _later, pending = runtime.claim_turn('전시 안내의 색상도 논의하자.')
    runtime.store.fail(pending, RuntimeError('synthetic cancelled error'))
    assert runtime.store.stats('alice')['last_error'] == 'RuntimeError'
    runtime.store.set_enabled('alice', False)
    assert runtime.store.stats('alice')['last_error'] is None


def test_processed_migration_marks_legacy_done_only_and_survives_later_cancellation(runtime):
    runtime.allow()
    completed, _job, _story = runtime.saved()
    cancelled, _pending = runtime.claim_turn('전시 안내의 크기는 아직 미정이야.')
    runtime.store.set_enabled('alice', False)
    connection = runtime.conversations._connection
    # Rebuild the fixture as the previous schema, retaining its real indexes,
    # row identities, constraints, and successful/cancelled checkpoint records.
    schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='story_runtime_jobs'",
    ).fetchone()[0]
    assert 'processed INTEGER NOT NULL DEFAULT 0,' in schema
    legacy_schema = schema.replace(
        'CREATE TABLE story_runtime_jobs',
        'CREATE TABLE story_runtime_jobs_legacy', 1,
    ).replace('processed INTEGER NOT NULL DEFAULT 0,', '', 1)
    columns = ','.join('"' + row['name'] + '"' for row in connection.execute(
        'PRAGMA table_info(story_runtime_jobs)',
    ) if row['name'] != 'processed')
    connection.execute(legacy_schema)
    connection.execute(
        'INSERT INTO story_runtime_jobs_legacy (' + columns + ') '
        'SELECT ' + columns + ' FROM story_runtime_jobs',
    )
    connection.execute('DROP TABLE story_runtime_jobs')
    connection.execute('ALTER TABLE story_runtime_jobs_legacy RENAME TO story_runtime_jobs')
    connection.commit()
    runtime.reopen()
    connection = runtime.conversations._connection
    migrated = {row['request_id']: row['processed'] for row in connection.execute(
        'SELECT request_id,processed FROM story_runtime_jobs WHERE user_id=?',
        ('alice',),
    )}
    assert migrated == {completed.request_id: 1, cancelled.request_id: 0}
    connection.execute(
        "UPDATE story_runtime_jobs SET state='cancelled' WHERE user_id=? AND request_id=?",
        ('alice', completed.request_id),
    )
    connection.commit()
    runtime.reopen()
    persisted = runtime.conversations._connection.execute(
        'SELECT processed FROM story_runtime_jobs WHERE user_id=? AND request_id=?',
        ('alice', completed.request_id),
    ).fetchone()
    assert persisted['processed'] == 1


def test_policy_change_cancels_exhausted_jobs_instead_of_leaving_failed_blockers(runtime):
    runtime.allow()
    _token, job = runtime.claim_turn()
    for attempt in range(1, 4):
        runtime.store.fail(job, RuntimeError('synthetic exhausted failure'))
        runtime.now += 2 ** attempt
        if attempt < 3:
            job = runtime.store.claim('alice')
            assert job is not None
    assert runtime.store.stats('alice')['failed'] == 1
    runtime.store.set_enabled('alice', False)
    runtime.allow()
    stats = runtime.store.stats('alice')
    assert stats['failed'] == 0 and stats['cancelled'] == 1
    assert runtime.store.retry_failed('alice') == 0
    assert runtime.store.recover('alice') == 0
    assert runtime.store.claim('alice') is None
    _new, fresh = runtime.claim_turn('전시 안내를 새로 논의하자.')
    assert runtime.store.commit(fresh, [runtime.update(fresh)])


def test_running_lease_is_recovered_after_restart_but_not_before_expiry(runtime):
    runtime.allow()
    _token, original_job = runtime.claim_turn()
    stale_updates = [runtime.update(original_job)]
    runtime.reopen()
    runtime.store.recover('alice')
    assert runtime.store.claim('alice') is None
    runtime.now += 301
    runtime.store.recover('alice')
    resumed = runtime.store.claim('alice')
    assert resumed is not None
    assert not runtime.store.commit(original_job, stale_updates)
    assert runtime.store.commit(resumed, [runtime.update(resumed)])
    assert len(runtime.store.list_stories('alice')) == 1


def test_exact_quote_retains_actor_and_proposal_separately_from_user_decision(runtime):
    runtime.allow()
    text = '수정하기 쉬워서 한 장 안내를 먼저 시험해 보자.'
    answer = '그림 중심의 구성도 대안으로 제안할게.'
    _token, job = runtime.claim_turn(text, answer=answer)
    decision = runtime.update(
        job, text='한 장 안내를 먼저 시험한다.', kind='decision',
        status='confirmed', quote=text,
    )
    proposal = runtime.update(
        job, source=runtime.source(job, role='assistant'),
        text='그림 중심의 구성을 제안했다.', quote=answer,
        kind='decision', actor='assistant', status='proposed',
    )['current'][0]
    decision['current'].append(proposal)
    decision['episode'].append(deepcopy(proposal))
    assert runtime.store.commit(job, [decision])
    story = runtime.store.list_stories('alice')[0]
    assert [(item['actor'], item['status']) for item in story['current']] == [
        ('user', 'confirmed'), ('assistant', 'proposed'),
    ]
    evidence = runtime.store.evidence('alice', story['story_id'])
    assert {(item['role'], item['text']) for item in evidence} == {
        ('user', text), ('assistant', answer),
    }


@pytest.mark.parametrize(
    'invalid', ['missing_quote', 'missing_source', 'assistant_decision'],
)
def test_invalid_evidence_or_actor_never_commits_partial_updates(runtime, invalid):
    runtime.allow()
    _token, job = runtime.claim_turn()
    valid = runtime.update(job)
    bad = runtime.update(job, title='다른 이야기')
    if invalid == 'missing_quote':
        bad['current'][0]['evidence'][0]['quote'] = '원문에는 없는 새 결정'
    elif invalid == 'missing_source':
        bad['current'][0]['evidence'][0]['source_id'] = 'unavailable-source'
    else:
        bad = runtime.update(
            job, source=runtime.source(job, role='assistant'),
            actor='assistant', kind='decision', status='confirmed',
        )
    try:
        accepted = runtime.store.commit(job, [valid, bad])
    except ValueError:
        accepted = False
    assert accepted is False
    assert runtime.store.list_stories('alice') == []


def test_story_deletion_removes_only_cited_spans_and_preserves_ordinals(runtime):
    runtime.allow()
    selected = '전시 안내는 한 장으로 만들자.'
    unrelated = '정원에서는 해바라기를 관찰했어.'
    echoed = '전시 안내는 한 장으로 만들기로 했어.'
    retained_reply = '해바라기 관찰도 재미있었겠어.'
    token, job = runtime.claim_turn(
        selected + ' ' + unrelated,
        answer=echoed + ' ' + retained_reply,
    )
    update = runtime.update(job, quote=selected)
    update['source_spans'] = [{
        'source_id': runtime.source(job, role='assistant')['id'],
        'quote': echoed,
    }]
    assert runtime.store.commit(job, [update])
    story = runtime.store.list_stories('alice')[0]
    before = runtime.row(token)
    other = runtime.complete('동네 도서관에 들렀어.', answer='책을 골랐구나.')
    unrelated_row = runtime.row(other)
    result = runtime.store.delete_story('alice', story['story_id'])
    assert isinstance(result, dict)
    after = runtime.row(token)
    assert after['ordinal'] == before['ordinal']
    assert selected not in after['user_content']
    assert unrelated in after['user_content']
    assert echoed not in after['assistant_content']
    assert retained_reply in after['assistant_content']
    assert selected not in after['response_json']
    assert echoed not in after['response_json']
    assert runtime.row(other) == unrelated_row
    assert runtime.store.candidates('alice', '전시 안내') == []
    assert runtime.store.evidence('alice', story['story_id']) == []
    assert not runtime.store.commit(job, [update])
    # Retained text may be re-extracted under its new digest, but the old
    # checkpoint and the deleted excerpts must not come back with it.
    assert runtime.store.enqueue_completed('alice', token.request_id)
    sanitized_job = runtime.store.claim('alice')
    assert sanitized_job is not None
    sanitized = json.dumps(runtime.store.job_source(sanitized_job),
                           ensure_ascii=False)
    assert selected not in sanitized and echoed not in sanitized
    assert unrelated in sanitized and retained_reply in sanitized
    runtime.store.delete_story('alice', story['story_id'])
    assert runtime.row(token) == after


@pytest.mark.parametrize('pending_state', ['unqueued', 'queued', 'running', 'failed'])
def test_settled_deletion_rejects_unprocessed_sources_without_any_mutation(
    runtime, pending_state,
):
    runtime.allow()
    token, _job, story = runtime.saved()
    later = runtime.complete('전시 안내에서 글자 크기는 아직 미정이야.')
    if pending_state != 'unqueued':
        assert runtime.store.enqueue_completed('alice', later.request_id)
    if pending_state in ('running', 'failed'):
        job = runtime.store.claim('alice')
        assert job is not None
        if pending_state == 'failed':
            for attempt in range(1, 4):
                runtime.store.fail(job, RuntimeError('synthetic pending failure'))
                runtime.now += 2 ** attempt
                if attempt < 3:
                    job = runtime.store.claim('alice')
                    assert job is not None
    original_rows = [runtime.row(token), runtime.row(later)]
    original_stories = deepcopy(runtime.store.list_stories('alice'))
    original_stats = deepcopy(runtime.store.stats('alice'))
    with pytest.raises(StoryMemoryError, match='requires_settled_sources'):
        runtime.store.delete_story('alice', story['story_id'], require_settled=True)
    assert [runtime.row(token), runtime.row(later)] == original_rows
    assert runtime.store.list_stories('alice') == original_stories
    assert runtime.store.stats('alice') == original_stats
    assert runtime.store.delete_story('alice', story['story_id'])['deleted']


def test_settled_deletion_succeeds_when_all_allowed_completed_sources_are_done(runtime):
    runtime.allow()
    _token, _job, story = runtime.saved()
    assert runtime.store.delete_story(
        'alice', story['story_id'], require_settled=True,
    )['deleted']
    assert runtime.store.list_stories('alice') == []


@pytest.mark.parametrize('unprocessed_state', ['unqueued', 'queued', 'running', 'failed'])
def test_disabled_settled_deletion_requires_review_of_unprocessed_approved_sources(
    runtime, unprocessed_state,
):
    runtime.allow()
    token, _job, story = runtime.saved()
    later = runtime.complete('전시 안내의 예산은 아직 결정하지 못했어.')
    if unprocessed_state != 'unqueued':
        assert runtime.store.enqueue_completed('alice', later.request_id)
    if unprocessed_state in ('running', 'failed'):
        job = runtime.store.claim('alice')
        assert job is not None
        if unprocessed_state == 'failed':
            for attempt in range(1, 4):
                runtime.store.fail(job, RuntimeError('synthetic review failure'))
                runtime.now += 2 ** attempt
                if attempt < 3:
                    job = runtime.store.claim('alice')
                    assert job is not None
    runtime.store.set_enabled('alice', False)
    if unprocessed_state != 'unqueued':
        assert runtime.store.stats('alice')['cancelled'] == 1
    original_rows = [runtime.row(token), runtime.row(later)]
    original_stories = deepcopy(runtime.store.list_stories('alice'))
    original_stats = deepcopy(runtime.store.stats('alice'))
    with pytest.raises(StoryMemoryError, match='requires_history_review'):
        runtime.store.delete_story('alice', story['story_id'], require_settled=True)
    assert [runtime.row(token), runtime.row(later)] == original_rows
    assert runtime.store.list_stories('alice') == original_stories
    assert runtime.store.stats('alice') == original_stats


def test_disabled_settled_deletion_accepts_all_successfully_processed_batch_sources(runtime):
    runtime.allow()
    _tokens, job = _claim_three_turns(runtime)
    update = runtime.update(job)
    entries = [runtime.update(job, source=source)['current'][0]
               for source in runtime.store.job_source(job)
               if source['role'] == 'user']
    update['current'], update['episode'] = entries, deepcopy(entries)
    assert runtime.store.commit(job, [update])
    story = runtime.store.list_stories('alice')[0]
    runtime.store.set_enabled('alice', False)
    runtime.reopen()
    assert runtime.store.delete_story(
        'alice', story['story_id'], require_settled=True,
    )['deleted']
    assert runtime.store.list_stories('alice') == []


@pytest.mark.parametrize('off_period', ['before_first_consent', 'after_disable'])
def test_settled_deletion_does_not_require_processing_unapproved_off_period_text(
    runtime, off_period,
):
    if off_period == 'before_first_consent':
        off_turn = runtime.complete('동의하지 않은 동안 나눈 별도 이야기야.')
    runtime.allow()
    _token, _job, story = runtime.saved()
    runtime.store.set_enabled('alice', False)
    if off_period == 'after_disable':
        off_turn = runtime.complete('기억을 끈 동안 나눈 별도 이야기야.')
    original = runtime.row(off_turn)
    assert runtime.store.delete_story(
        'alice', story['story_id'], require_settled=True,
    )['deleted']
    assert runtime.row(off_turn) == original


@pytest.mark.parametrize('shared_auxiliary', [False, True])
def test_partial_deletion_rebases_surviving_story_evidence_in_same_message(
    runtime, shared_auxiliary,
):
    runtime.allow()
    removed = '전시 안내는 한 장으로 만들자.'
    retained = '정원에서는 해바라기를 관찰했어.'
    token, job = runtime.claim_turn(removed + ' ' + retained)
    exhibition = runtime.update(job, quote=removed)
    garden = runtime.update(
        job, title='정원 관찰', quote=retained,
        text='정원에서 해바라기를 관찰했다.', kind='experience',
    )
    garden['aliases'] = ['해바라기']
    if shared_auxiliary:
        assistant = runtime.source(job, role='assistant')
        span = {'source_id': assistant['id'], 'quote': assistant['text']}
        exhibition['source_spans'] = [span]
        garden['source_spans'] = [deepcopy(span)]
    assert runtime.store.commit(job, [exhibition, garden])
    stories = {item['title']: item for item in runtime.store.list_stories('alice')}
    runtime.store.delete_story('alice', stories['전시 안내']['story_id'])
    results = runtime.store.candidates('alice', '해바라기')
    assert [item['story_id'] for item in results] == [stories['정원 관찰']['story_id']]
    evidence = runtime.store.evidence('alice', stories['정원 관찰']['story_id'])
    assert [item['text'] for item in evidence] == [retained]
    assert removed not in runtime.row(token)['user_content']
    assert retained in runtime.row(token)['user_content']


def test_invalidate_summary_excludes_it_from_recall_without_erasing_original(runtime):
    runtime.allow()
    token, _job, story = runtime.saved()
    before = runtime.row(token)
    assert runtime.store.invalidate_summary('alice', story['story_id'])
    assert runtime.store.candidates('alice', '전시 안내') == []
    assert runtime.row(token) == before
    assert not runtime.store.invalidate_summary('bob', story['story_id'])


def test_disabled_memory_remains_manageable_but_cannot_be_recalled(runtime):
    runtime.allow()
    token, _job, story = runtime.saved()
    original = runtime.row(token)
    runtime.store.set_enabled('alice', False)
    assert runtime.store.candidates('alice', '전시 안내') == []
    assert runtime.store.claim('alice') is None
    assert [item['story_id'] for item in runtime.store.list_stories('alice')] == [
        story['story_id'],
    ]
    assert runtime.store.evidence('alice', story['story_id'])
    assert runtime.row(token) == original
    runtime.store.delete_story('alice', story['story_id'])
    assert runtime.store.list_stories('alice') == []


def test_deletion_drops_derived_session_summary_and_tombstones_linked_facts(runtime):
    runtime.allow()
    text = '나는 전시를 좋아해. 정원에서는 해바라기를 봤어.'
    token = runtime.complete(text)
    source = {
        'conversation_id': token.conversation_id,
        'session_instance_id': token.session_instance_id,
        'generation': token.generation, 'turn_id': token.turn_id,
        'request_id': token.request_id, 'text': text,
    }
    runtime.memories.set_personalization('alice', True, source)
    fact = runtime.memories.upsert_fact('alice', {
        'kind': 'preference', 'subject': 'user', 'attribute': 'likes',
        'value': '전시', 'evidence': '나는 전시를 좋아해.',
    }, source)['records'][0]
    assert runtime.store.enqueue_completed('alice', token.request_id)
    job = runtime.store.claim('alice')
    assert job is not None
    assert runtime.store.commit(job, [runtime.update(
        job, quote='나는 전시를 좋아해.', text='전시를 좋아한다고 말했다.',
        kind='experience',
    )])
    story = runtime.store.list_stories('alice')[0]
    begun = runtime.begin('다음 대화의 원문도 남겨줘.')
    assert runtime.conversations.apply_compaction(
        begun.token, begun.summary, begun.history,
        SummaryResult('전시를 좋아한다고 말했다.', '{}', 'openai-semantic-v1'),
    )
    runtime.conversations.fail_turn(begun.token)
    assert runtime.conversations.get_summary('alice', 'room-a') is not None
    runtime.store.delete_story('alice', story['story_id'])
    assert runtime.conversations.get_summary('alice', 'room-a') is None
    assert fact.id in runtime.memories.invalidated_ids('alice')
    assert all(item.id != fact.id for item in runtime.memories.list_for_user('alice'))
    assert '정원에서는 해바라기를 봤어.' in runtime.row(token)['user_content']
    result = runtime.memories.upsert_fact('alice', {
        'kind': 'preference', 'subject': 'user', 'attribute': 'likes',
        'value': '전시', 'evidence': '나는 전시를 좋아해.',
    }, source)
    assert result['status'] == 'conflict'


def test_other_user_cannot_delete_story_or_change_originals(runtime):
    runtime.allow()
    token, _job, story = runtime.saved()
    before = runtime.row(token)
    runtime.allow('bob')
    runtime.store.delete_story('bob', story['story_id'])
    assert runtime.row(token) == before
    assert runtime.store.candidates('alice', '전시 안내')
    assert runtime.store.evidence('bob', story['story_id']) == []


def test_sequential_deletions_keep_other_story_reply_dependencies(runtime):
    runtime.allow()
    _token, _job, exhibition = runtime.saved()
    _garden_token, garden_job = runtime.claim_turn('정원에서 해바라기를 관찰했어.')
    garden_update = runtime.update(
        garden_job, title='정원 관찰', kind='experience',
        text='정원에서 해바라기를 관찰했다.',
    )
    garden_update['aliases'] = ['해바라기']
    assert runtime.store.commit(garden_job, [garden_update])
    garden = next(story for story in runtime.store.list_stories('alice')
                  if story['title'] == '정원 관찰')
    paraphrase = '노란 꽃을 살펴본 경험에 관해 이어서 이야기해 보자.'
    reply = runtime.complete('그 경험을 이어서 이야기하자.', answer=paraphrase)
    assert runtime.store.record_reply(
        'alice', reply.request_id, [garden['story_id']],
        runtime.store.policy('alice')['revision'],
    )
    runtime.store.delete_story('alice', exhibition['story_id'])
    assert runtime.row(reply)['assistant_content'] == paraphrase
    runtime.store.delete_story('alice', garden['story_id'])
    assert paraphrase not in runtime.row(reply)['assistant_content']
    assert paraphrase not in runtime.row(reply)['response_json']


@pytest.mark.parametrize('running', [False, True])
def test_deletion_preserves_unrelated_pending_work_with_a_new_claim(runtime, running):
    runtime.allow()
    _token, _job, exhibition = runtime.saved()
    unrelated = runtime.complete('정원에서 해바라기를 관찰했어.')
    assert runtime.store.enqueue_completed('alice', unrelated.request_id)
    old_job = runtime.store.claim('alice') if running else None
    old_updates = [runtime.update(old_job)] if old_job else None
    runtime.store.delete_story('alice', exhibition['story_id'])
    if old_job:
        assert runtime.store.job_source(old_job) is None
        assert not runtime.store.commit(old_job, old_updates)
    new_job = runtime.store.claim('alice')
    assert new_job is not None
    assert {s['ref']['turn_id'] for s in runtime.store.job_source(new_job)} == {
        unrelated.turn_id,
    }
    update = runtime.update(
        new_job, title='정원 관찰', kind='experience',
        text='정원에서 해바라기를 관찰했다.',
    )
    assert runtime.store.commit(new_job, [update])


def test_deletion_does_not_redact_identical_generic_reply_without_dependency(runtime):
    runtime.allow()
    generic_reply = '이야기를 잘 들었어.'
    _token, job = runtime.claim_turn(answer=generic_reply)
    update = runtime.update(job)
    update['source_spans'] = [{
        'source_id': runtime.source(job, role='assistant')['id'],
        'quote': generic_reply,
    }]
    assert runtime.store.commit(job, [update])
    story = runtime.store.list_stories('alice')[0]
    other = runtime.complete('동네 도서관에 들렀어.', answer=generic_reply)
    before = runtime.row(other)
    runtime.store.delete_story('alice', story['story_id'])
    assert runtime.row(other) == before


def test_partial_deletion_preserves_unrelated_fact_and_scrubs_its_source_copy(runtime):
    runtime.allow()
    removed = '나는 전시를 좋아해.'
    retained = '나는 정원을 좋아해.'
    text = removed + ' ' + retained
    token = runtime.complete(text)
    source = {
        'conversation_id': token.conversation_id,
        'session_instance_id': token.session_instance_id,
        'generation': token.generation, 'turn_id': token.turn_id,
        'request_id': token.request_id, 'text': text,
    }
    runtime.memories.set_personalization('alice', True, source)
    facts = {}
    for value, evidence in [('전시', removed), ('정원', retained)]:
        facts[value] = runtime.memories.upsert_fact('alice', {
            'kind': 'preference', 'subject': 'user', 'attribute': 'likes',
            'value': value, 'evidence': evidence,
        }, source)['records'][0]
    assert runtime.store.enqueue_completed('alice', token.request_id)
    job = runtime.store.claim('alice')
    assert job is not None
    assert runtime.store.commit(job, [runtime.update(
        job, quote=removed, text='전시를 좋아한다고 말했다.', kind='experience',
    )])
    story = runtime.store.list_stories('alice')[0]
    runtime.store.delete_story('alice', story['story_id'])
    remaining = runtime.memories.list_for_user('alice')
    assert [fact.id for fact in remaining] == [facts['정원'].id]
    assert removed not in json.dumps(remaining[0].metadata, ensure_ascii=False)
    assert retained in remaining[0].metadata['source']['text']
    assert facts['전시'].id in runtime.memories.invalidated_ids('alice')
    assert facts['정원'].id not in runtime.memories.invalidated_ids('alice')
