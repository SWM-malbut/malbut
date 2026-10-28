"""Recovery stays cheap at idle without losing cross-connection completions."""

from test_story_runtime_store import RuntimeHarness


def test_idle_recovery_does_not_revisit_historical_sources(tmp_path):
    runtime = RuntimeHarness(tmp_path / 'memory.sqlite3')
    try:
        runtime.allow()
        for _ in range(10):
            runtime.complete()
        assert runtime.store.recover('alice') == 10
        statements = []
        runtime.conversations._connection.set_trace_callback(statements.append)
        for _ in range(20):
            assert runtime.store.recover('alice') == 0
        assert not any('FROM conversation_turns' in sql for sql in statements)
        assert runtime.store.stats('alice')['queued'] == 10
    finally:
        runtime.close()


def test_recovery_finds_completion_from_another_connection_after_idle(tmp_path):
    path = tmp_path / 'memory.sqlite3'
    runtime = RuntimeHarness(path)
    other = None
    try:
        runtime.allow()
        assert runtime.store.recover('alice') == 0
        assert runtime.store.recover('alice') == 0
        other = RuntimeHarness(path)
        other.now = runtime.now + 10
        token = other.complete('다른 연결에서 새 대화가 끝났어.')
        assert runtime.store.recover('alice') == 1
        job = runtime.store.claim('alice')
        assert job['request_id'] == token.request_id
        assert any(s['text'] == '다른 연결에서 새 대화가 끝났어.'
                   for s in runtime.store.job_source(job))
    finally:
        if other is not None:
            other.close()
        runtime.close()


def test_recovery_finds_same_connection_completion_after_idle(tmp_path):
    runtime = RuntimeHarness(tmp_path / 'memory.sqlite3')
    try:
        runtime.allow()
        assert runtime.store.recover('alice') == 0
        token = runtime.complete()
        assert runtime.store.recover('alice') == 1
        job = runtime.store.claim('alice')
        assert job['request_id'] == token.request_id
        # Expiry is a clock change, without any intervening database write.
        runtime.store.recover('alice')
        runtime.now += 301
        runtime.store.recover('alice')
        reclaimed = runtime.store.claim('alice')
        assert reclaimed is not None and reclaimed['claim'] != job['claim']
    finally:
        runtime.close()
