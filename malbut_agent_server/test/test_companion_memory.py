"""Calendar retention, category withdrawal and summary-only persistence boundaries."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import json

import pytest

from malbut_agent_server.companion_memory import CompanionMemory, expires_at, validate_summary
from malbut_agent_server.companion_service import CompanionService
from malbut_agent_server.companion_dialogue import CompanionDialogue
from malbut_agent_server.companion_extractor import CompanionExtractor


def stamp(date):
    return datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('Asia/Seoul')).timestamp()


NOW = stamp('2026-11-01T12:00:00')
SESSION = 'patrol-chat-' + 'a' * 32
SUMMARY = dict(summary='영희와 내일 산책하기로 했다고 말함.', categories=['daily'],
               event_state='planned', event_date='2026-11-02')


def wait_memory(service):
    with service._memory_condition:
        assert service._memory_condition.wait_for(
            lambda: service._memory_active is None and not service._memory_jobs, timeout=3)


class Client:
    def __init__(self):
        self.data = dict(ownerId='owner', revision=1, eraseRevision=0, categories=['daily'],
                         externalProcessing=True, confirmedSessionId=SESSION, remainingSeconds=300)
        self.reports = []

    def sync(self, report):
        self.reports.append(report)
        return dict(self.data)


@pytest.fixture
def setup(tmp_path):
    wall, mono = [NOW], [10.]
    store = CompanionMemory(tmp_path / 'memory.sqlite3', clock=lambda: wall[0])
    client = Client()
    extractor = SimpleNamespace(extract=lambda *args: [SUMMARY])
    service = CompanionService(store, client, extractor, clock=lambda: mono[0], wall=lambda: wall[0])
    service.begin(SESSION)
    service.sync()
    yield service, client, wall, mono
    service.close()


@pytest.mark.parametrize('source,expected', [
    ('2026-11-30T23:59:59', '2027-02-28T23:59:59'),
    ('2027-11-30T12:00:00', '2028-02-29T12:00:00'),
    ('2026-01-31T12:00:00', '2026-04-30T12:00:00'),
    ('2026-11-01T12:00:00', '2027-02-01T12:00:00'),
])
def test_retention_is_calendar_months_not_ninety_days(source, expected):
    assert expires_at(stamp(source)) == stamp(expected)


def test_summary_survives_restart_but_originals_and_expired_rows_do_not(setup, tmp_path):
    service, _, wall, _ = setup
    source = '이 원문 자체는 저장되면 안 됩니다.'
    assert service.remember(service.binding(SESSION), 't1', source, NOW) == 1
    row, = service.memory.recall('owner')
    assert row['event_state'] == 'planned'
    assert row['expires_at'] == stamp('2027-02-01T12:00:00')
    dbpath = tmp_path / 'memory.sqlite3'
    assert source.encode() not in dbpath.read_bytes()
    assert not list(tmp_path.glob('*-wal'))
    assert dbpath.stat().st_mode & 0o777 == 0o600
    reopened = CompanionMemory(dbpath, clock=lambda: wall[0])
    assert len(reopened.recall('owner')) == 1
    assert reopened.recall('someone-else') == []
    wall[0] = row['expires_at'] - 1
    assert len(reopened.recall('owner')) == 1
    wall[0] += 1
    assert reopened.recall('owner') == []
    assert reopened.db.execute('SELECT count(*) FROM companion_episodes').fetchone()[0] == 0
    reopened.close()
    assert SUMMARY['summary'].encode() not in dbpath.read_bytes()


def test_retries_do_not_extend_expiry_or_duplicate_and_late_arrivals_remain_old(setup):
    service, _, wall, _ = setup
    store = service.memory
    assert store.save('owner', 1, 't1', NOW, [SUMMARY]) == 1
    wall[0] += 60
    assert store.save('owner', 1, 't1', NOW, [SUMMARY]) == 0
    assert store.recall('owner')[0]['observed_at'] == NOW
    wall[0] = expires_at(NOW)
    assert store.save('owner', 1, 'late', NOW, [SUMMARY]) == 0


def test_mixed_category_requires_every_consent_and_revocation_erases_it(setup):
    service, client, _, _ = setup
    mixed = dict(SUMMARY, categories=['daily', 'health', 'family'], summary='딸과 진료를 다녀왔다고 말함.')
    assert service.memory.save('owner', 1, 't', NOW, [mixed]) == 0
    client.data.update(revision=2, categories=['daily', 'health', 'family'])
    service.sync()
    assert service.memory.save('owner', 2, 't', NOW, [mixed]) == 1
    assert service.memory.save('owner', 2, 'safe', NOW, [SUMMARY]) == 1
    client.data.update(revision=3, categories=['daily', 'family'])
    service.sync()
    assert [r['summary'] for r in service.memory.recall('owner')] == [SUMMARY['summary']]


def test_external_consent_and_app_erasure_fence_late_extractions(setup):
    service, client, _, _ = setup
    binding = service.binding(SESSION)
    def withdraw(*args):
        client.data.update(revision=2, externalProcessing=False)
        return [SUMMARY]
    service.extractor.extract = withdraw
    assert service.remember(binding, 't', '내일 산책할게', NOW) == 0
    assert not service.valid(binding)
    assert service.memory.recall('owner') == []
    client.data.update(revision=3, externalProcessing=True)
    service.sync()
    service.memory.save('owner', 3, 't', NOW, [SUMMARY])
    client.data.update(revision=4, eraseRevision=1)
    service.sync()
    assert service.memory.recall('owner') == []
    # Repeated acknowledgement must not delete a subsequently observed event.
    service.memory.save('owner', 4, 'new', NOW, [SUMMARY])
    service.sync()
    assert len(service.memory.recall('owner')) == 1


def test_unknown_stale_wrong_session_restart_and_new_encounter_cannot_recall(setup):
    service, client, _, mono = setup
    binding = service.binding(SESSION)
    assert binding
    assert service.binding('other') is None
    mono[0] += 10
    assert not service.valid(binding)
    service.sync()
    assert service.binding(SESSION)
    service.end(SESSION)
    assert not service.valid(binding)
    service.begin('patrol-chat-' + 'b' * 32)
    service.sync()
    assert service.binding(service.session) is None
    fresh = CompanionService(service.memory, client, clock=lambda: mono[0])
    assert fresh.binding(SESSION) is None


@pytest.mark.parametrize('change', [dict(remainingSeconds=float('nan')), dict(categories=['unknown']),
                                    dict(revision=-1), dict(externalProcessing='yes'), dict(ownerId='')])
def test_invalid_cloud_policy_is_rejected(setup, change):
    service, client, _, _ = setup
    client.data.update(change)
    with pytest.raises(ValueError):
        service.sync()
    assert service.binding(SESSION) is None


def test_offline_purge_runs_without_cloud_and_retry_does_not_retain_source(setup):
    import time
    service, client, wall, _ = setup
    service.memory.save('owner', 1, 't', NOW, [SUMMARY])
    wall[0] = expires_at(NOW)
    def offline(report):
        raise OSError('offline')
    client.sync = offline
    service.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with service.memory.transaction() as db:
            if db.execute('SELECT count(*) FROM companion_episodes').fetchone()[0] == 0:
                break
        time.sleep(.01)
    with service.memory.transaction() as db:
        assert db.execute('SELECT count(*) FROM companion_episodes').fetchone()[0] == 0
    assert service.binding(SESSION) is None


def test_explicit_forget_clears_memory_and_recalled_dialogue_context(setup):
    service, _, _, _ = setup
    dialogue, provider = CompanionDialogue(service), Provider()
    dialogue.begin(SESSION)
    dialogue.answer(provider, SESSION, 'event', '내일 산책할 거야', None)
    wait_memory(service)
    assert service.memory.recall('owner')
    assert '삭제했어요' in dialogue.answer(provider, SESSION, 'delete', '일상 기억 모두 지워줘', None)
    assert service.memory.recall('owner') == []
    assert dialogue._turns == []


def test_recall_finds_named_older_event_and_preserves_latest_correction(setup):
    service, _, _, _ = setup
    store = service.memory
    store.save('owner', 1, 'old', NOW - 60, [SUMMARY])
    cancelled = dict(SUMMARY, summary='영희와 산책 약속을 취소했다고 말함.', event_state='cancelled')
    store.save('owner', 1, 'cancelled', NOW - 50, [cancelled])
    for i in range(20):
        store.save('owner', 1, 'other-' + str(i), NOW - i, [dict(SUMMARY, summary=f'다른 일상 사건 {i}')])
    result = store.recall('owner', limit=2, query='영희는?')
    assert [r['event_state'] for r in result] == ['cancelled', 'planned']


class Provider:
    def __init__(self, during=lambda: None):
        self.calls, self.during = [], during

    def complete(self, request, memories, turns, tools):
        self.calls.append((request, memories, turns, tools))
        self.during()
        return SimpleNamespace(decision=SimpleNamespace(type='message', message='친구분과 산책하기로 하셨군요.'))


def test_verified_conversation_recall_and_save_use_no_owner_history_tools_or_raw_store(setup):
    service, client, _, _ = setup
    dialogue, provider = CompanionDialogue(service), Provider()
    dialogue.begin(SESSION)
    assert dialogue.answer(provider, SESSION, 'first', '내일 영희와 산책할 거야', None)
    wait_memory(service)
    assert service.memory.recall('owner')[0]['event_state'] == 'planned'
    assert dialogue.answer(provider, SESSION, 'second', '내일 뭐 하기로 했지?', None)
    request, memories, turns, tools = provider.calls[-1]
    assert request.user_id == 'app-verified-companion'
    assert request.scene_context['daily_memories'][0]['event_state'] == 'planned'
    assert not memories and not tools and request.turn_image is None
    assert len(turns) == 1
    client.data.update(revision=2, confirmedSessionId=None, remainingSeconds=0)
    assert dialogue.answer(provider, SESSION, 'guest', '안녕하세요', None)
    request, _, turns, _ = provider.calls[-1]
    assert request.scene_context == {'companion_guest': True}
    assert turns == []


def test_outgoing_reply_is_blocked_after_departure_and_consent_change(setup):
    service, client, _, _ = setup
    dialogue, provider = CompanionDialogue(service), Provider()
    dialogue.begin(SESSION)
    assert dialogue.answer(provider, SESSION, 'first', '내일 산책할 거야', None)
    assert dialogue.reply_allowed(SESSION)
    receipt = dialogue.reply_receipt()
    client.data.update(revision=2, confirmedSessionId=None, remainingSeconds=0)
    service.sync()
    assert not dialogue.reply_allowed(SESSION)
    client.data.update(revision=3, confirmedSessionId=SESSION, remainingSeconds=300)
    dialogue.answer(provider, SESSION, 'new', '다른 이야기 해요', None)
    assert dialogue.reply_allowed(SESSION)
    assert not dialogue.reply_allowed(SESSION, receipt)
    dialogue.end(SESSION)
    assert not dialogue.reply_allowed(SESSION)


def test_expired_recalled_text_is_removed_from_short_term_context_too(setup):
    service, _, wall, _ = setup
    # Choose a summary with one second left, including a last-day calendar boundary.
    old = stamp('2026-08-01T12:00:01')
    service.memory.save('owner', 1, 'old', old, [SUMMARY])
    dialogue, provider = CompanionDialogue(service), Provider()
    dialogue.begin(SESSION)
    service.extractor = None
    dialogue.answer(provider, SESSION, 'one', '전에 한 얘기 기억나?', None)
    assert provider.calls[-1][0].scene_context['daily_memories']
    wall[0] += 1
    assert not dialogue.reply_allowed(SESSION)
    dialogue.answer(provider, SESSION, 'two', '다시 알려줘', None)
    assert provider.calls[-1][2] == []
    assert provider.calls[-1][0].scene_context['daily_memories'] == []


def test_source_quotes_are_validated_and_never_returned_for_persistence():
    text = '내일 영희와 산책할 거야'
    output = dict(SUMMARY, quote=text)
    calls = []
    backend = SimpleNamespace(model='configured-model', reasoning_effort='none', base_url='https://api.openai.com/v1',
                              _api_key='test-only', timeout_seconds=30, _response_text=lambda response: response,
                              transport=lambda *args: calls.append(args) or json.dumps({'episodes': [output]}, ensure_ascii=False))
    extractor = CompanionExtractor(backend)
    assert extractor.extract(text, NOW, ['daily']) == [SUMMARY]
    assert calls[0][2]['store'] is False
    output['quote'] = '다른 사람이 한 말'
    with pytest.raises(ValueError, match='evidence'):
        extractor.extract(text, NOW, ['daily'])
    output.update(quote='병원 다녀왔어', summary='병원에 다녀왔다고 말함.')
    before = len(calls)
    assert extractor.extract('병원 다녀왔어', NOW, ['daily']) == []
    assert len(calls) == before


@pytest.mark.parametrize('patch', [dict(summary='x' * 281), dict(event_state='done'),
                                  dict(event_date='2026-02-30'), dict(categories=['private'])])
def test_malformed_summary_never_reaches_storage(patch):
    with pytest.raises(ValueError):
        validate_summary(dict(SUMMARY, **patch))
