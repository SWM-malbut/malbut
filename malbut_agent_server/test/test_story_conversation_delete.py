"""Raw-session deletion must revoke derived stories without widening raw scope."""

from test_story_runtime_store import RuntimeHarness


def test_delete_conversation_revokes_cross_session_reply_and_preserves_other_users(tmp_path):
    runtime = RuntimeHarness(tmp_path / 'delete.sqlite3')
    try:
        runtime.allow()
        source, _, story = runtime.saved()
        reply = runtime.complete('전시 이야기 이어가자.', room='room-b', answer='한 장 안내를 선택했었지.')
        assert runtime.store.record_reply('alice', reply.request_id, [story['story_id']],
                                          runtime.store.policy('alice')['revision'])
        unrelated = runtime.complete('오늘 산책을 했어.', room='room-c')
        other = runtime.complete('다른 사용자의 전시 경험.', user='bob', room='room-a')
        before = runtime.store.policy('alice')
        assert runtime.store.delete_conversation('alice', 'room-a')
        assert runtime.row(source) is None
        assert runtime.row(reply)['user_content'] == '전시 이야기 이어가자.'
        assert runtime.row(reply)['assistant_content'] == ''
        assert runtime.row(reply)['response_json'] == '{}'
        assert runtime.row(unrelated)['user_content'] == '오늘 산책을 했어.'
        assert runtime.row(other)['user_content'] == '다른 사용자의 전시 경험.'
        assert runtime.store.list_stories('alice') == []
        assert runtime.store.policy('alice')['revision'] == before['revision'] + 1
        runtime.reopen()
        runtime.store.recover('alice')
        assert runtime.store.list_stories('alice') == []
        assert not runtime.store.delete_conversation('alice', 'room-a')
    finally:
        runtime.close()


def test_delete_blocks_late_commit_and_requeues_only_unrelated_work(tmp_path):
    runtime = RuntimeHarness(tmp_path / 'late.sqlite3')
    try:
        runtime.allow()
        removed, job = runtime.claim_turn(room='room-a')
        update = runtime.update(job)
        retained = runtime.complete('다른 날의 산책 이야기.', room='room-b')
        assert runtime.store.enqueue_completed('alice', retained.request_id)
        assert runtime.store.delete_conversation('alice', 'room-a')
        assert not runtime.store.commit(job, [update])
        assert runtime.row(removed) is None
        next_job = runtime.store.claim('alice')
        assert next_job['request_id'] == retained.request_id
        assert runtime.store.commit(next_job, [runtime.update(next_job, title='산책')])
        assert len(runtime.store.list_stories('alice')) == 1
    finally:
        runtime.close()


def test_legacy_raw_delete_creates_epoch_without_enabling_memory(tmp_path):
    runtime = RuntimeHarness(tmp_path / 'legacy.sqlite3')
    try:
        runtime.complete()
        assert runtime.store.policy('alice')['revision'] == 0
        assert runtime.store.delete_conversation('alice', 'room-a')
        assert runtime.store.policy('alice') == {
            'enabled': False, 'external_consent': False, 'revision': 1, 'data_revision': 1,
        }
    finally:
        runtime.close()
