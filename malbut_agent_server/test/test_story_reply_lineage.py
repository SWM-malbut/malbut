"""Delete paraphrases produced from short context, including without recall hits."""

from types import SimpleNamespace

import pytest

from test_story_runtime_store import RuntimeHarness


@pytest.fixture
def runtime(tmp_path):
    harness = RuntimeHarness(tmp_path / 'lineage.sqlite3')
    yield harness
    harness.close()


def finish(runtime, begun, answer):
    runtime.conversations.complete_turn(
        begun.token, answer,
        {'schema_version': 1, 'decision': {'type': 'message', 'message': answer,
                                          'tool_name': None, 'arguments': {}}},
    )


def test_short_history_paraphrase_and_its_descendant_are_erased(runtime):
    runtime.allow()
    _source, _job, story = runtime.saved()
    first = runtime.begin('그 작업은 어떻게 할까?')
    assert runtime.store.record_context_reply('alice', first.token.request_id, first.history, None)
    finish(runtime, first, '얇은 한 쪽짜리 소개물을 먼저 만드는 계획이었어요.')
    second = runtime.begin('그 이야기를 계속해줘.')
    # Only the first paraphrase remains in this simulated provider input.
    history = list(second.history)[-1:]
    assert runtime.store.record_context_reply('alice', second.token.request_id, history, None)
    finish(runtime, second, '수정할 부분이 생기면 그 소개물부터 고치면 돼요.')
    runtime.store.delete_story('alice', story['story_id'])
    assert runtime.row(first.token)['assistant_content'] == ''
    assert runtime.row(second.token)['assistant_content'] == ''
    assert runtime.row(second.token)['user_content'] == '그 이야기를 계속해줘.'


def test_summary_and_disabled_short_context_still_track_erasure(runtime):
    runtime.allow()
    token, _job, story = runtime.saved()
    runtime.store.set_enabled('alice', False)
    begun = runtime.begin('그 일은 어떻게 할까?')
    summary = SimpleNamespace(
        user_id='alice', conversation_id=token.conversation_id,
        session_instance_id=token.session_instance_id, generation=token.generation,
        source_start_ordinal=1, source_end_ordinal=1,
    )
    assert runtime.store.record_context_reply('alice', begun.token.request_id, [], summary)
    finish(runtime, begun, '작은 종이 소개물을 만드는 작업이었어요.')
    runtime.store.delete_story('alice', story['story_id'])
    assert runtime.row(begun.token)['assistant_content'] == ''


def test_recall_and_short_history_dependencies_are_unioned(runtime):
    runtime.allow()
    _first, _job, exhibition = runtime.saved()
    token, garden_job = runtime.claim_turn('정원에서 해바라기를 봤어.', room='garden')
    update = runtime.update(garden_job, title='정원', text='해바라기를 관찰했다.')
    assert runtime.store.commit(garden_job, [update])
    garden = next(s for s in runtime.store.list_stories('alice') if s['title'] == '정원')
    begun = runtime.begin('아까 이야기 두 개를 이어가자.')
    assert runtime.store.record_reply('alice', begun.token.request_id, [garden['story_id']],
                                      runtime.store.policy('alice')['revision'])
    assert runtime.store.record_context_reply('alice', begun.token.request_id, begun.history, None)
    finish(runtime, begun, '소개물 작업과 꽃을 살펴본 경험이 있었어요.')
    runtime.store.delete_story('alice', exhibition['story_id'])
    assert runtime.row(begun.token)['assistant_content'] == ''
