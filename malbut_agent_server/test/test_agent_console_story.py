"""Offline console controls and actual SQLite story lifecycle integration."""

import subprocess
import sys

import pytest

from test_agent_console_support import CONSOLE_SCRIPT, restore_console_umask  # noqa: F401

from console_core import ConsoleCore
from malbut_agent_server.config import Settings
from malbut_agent_server.schemas import ValidationError
from malbut_agent_server.story_memory_service import StoryServiceError


class FixtureExtractor:
    """Explicit test fixture; no mock success path exists in the user console."""

    def extract(self, stories, sources):
        updates = []
        for source in sources:
            if source['role'] != 'user' or not source['id'].startswith('s'):
                continue
            text = source['text']
            title = next((title for title in ('바다 전시', '산 전시') if title in text), None)
            if title is None:
                continue
            previous = next((item for item in stories if item['title'] == title), None)
            evidence = {'source_id': source['id'], 'quote': text,
                        'start': 0, 'end': len(text)}
            entry = {'text': text, 'kind': 'experience', 'actor': 'user',
                     'status': 'stated', 'evidence': [evidence]}
            spans = [evidence]
            for other in sources:
                if other['role'] == 'assistant' and other['id'].startswith('s') and other['text']:
                    spans.append({'source_id': other['id'], 'quote': other['text'],
                                  'start': 0, 'end': len(other['text'])})
            updates.append({'story_id': previous['story_id'] if previous else None,
                            'title': title, 'aliases': [title, '전시'],
                            'current': [entry], 'episode': [entry], 'source_spans': spans})
        return updates


def make_core(tmp_path):
    return ConsoleCore(Settings(database_path=str(tmp_path / 'stories.sqlite3'),
                                user_id='console-story-user', tool_mode='simulation'),
                       story_extractor=FixtureExtractor())


def enable(core):
    assert '동의' in core.chat('장기기억 켜줘')['text']
    assert not core.stories()['policy']['enabled']
    assert '켰어요' in core.chat('네')['text']
    assert core.stories()['policy']['enabled']


def test_distinct_consent_management_not_recorded_and_mock_unavailable(tmp_path):
    core = make_core(tmp_path)
    try:
        core.chat('개인화에 동의해')
        assert not core.stories()['policy']['enabled']
        count = core.status()['turn_count']
        core.story_command('/stories on')
        core.chat('아니요')
        assert not core.stories()['policy']['enabled']
        enable(core)
        assert core.status()['turn_count'] == count
    finally:
        core.close()
    mock = ConsoleCore(Settings(database_path=str(tmp_path / 'mock.sqlite3'),
                                tool_mode='simulation'))
    try:
        response = mock.story_command('/stories on')
        assert '켤 수 없어요' in response['text']
        assert not mock.stories()['policy']['enabled']
        assert mock.status()['turn_count'] == 0
    finally:
        mock.close()


def test_story_survives_new_conversation_restart_and_has_original_evidence(tmp_path):
    core = make_core(tmp_path)
    try:
        enable(core)
        core.chat('바다 전시에서 푸른 그림을 보니 마음이 편해졌어.')
        assert core.story_memory.flush(core.settings.user_id, timeout=10)
        stories = core.stories()['items']
        assert len(stories) == 1
        story_id = stories[0]['story_id']
        original_session = core.conversation_id
        core.new_conversation()
        assert original_session != core.conversation_id
        assert core.stories()['items'][0]['story_id'] == story_id
        evidence = core.story_command('/stories sources ' + story_id)
        assert '마음이 편해졌어' in str(evidence['data'])
        core.validate_reply(evidence)
    finally:
        core.close()
    core = make_core(tmp_path)
    try:
        assert core.stories()['policy']['enabled']
        assert core.stories()['items'][0]['story_id'] == story_id
        assert core.story_memory.context(core.settings.user_id, '바다 전시 이야기를 이어가자')['stories']
    finally:
        core.close()


def test_correction_updates_same_story_and_local_delete_avoids_new_source(tmp_path):
    core = make_core(tmp_path)
    try:
        enable(core)
        core.chat('바다 전시에는 토요일에 다시 가려고 해.')
        assert core.story_memory.flush(core.settings.user_id, timeout=10)
        first = core.stories()['items'][0]['story_id']
        core.chat('바다 전시 날짜는 토요일이 아니라 일요일이야.')
        assert core.story_memory.flush(core.settings.user_id, timeout=10)
        story = core.stories()['items'][0]
        assert story['story_id'] == first
        assert '일요일' in str(story['current'])
        old_evidence = core.story_command('/stories sources ' + first)
        response = core.chat('바다 전시 이야기 잊어줘')
        assert '삭제했어요' in response['text']
        assert core.stories()['items'] == []
        with pytest.raises(ValidationError, match='memory_changed'):
            core.validate_reply(old_evidence)
        store = core.runtime.conversation_store
        with store._lock:
            bodies = str([tuple(row) for row in store._connection.execute(
                'SELECT user_content, assistant_content FROM conversation_turns WHERE user_id=?',
                (core.settings.user_id,),
            ).fetchall()])
        assert '토요일' not in bodies
        assert '일요일' not in bodies
        assert '잊어줘' not in bodies
    finally:
        core.close()


def test_ambiguous_delete_selects_one_story_and_off_invalidates_old_reply(tmp_path):
    core = make_core(tmp_path)
    try:
        enable(core)
        core.chat('바다 전시를 봤어.')
        assert core.story_memory.flush(core.settings.user_id, timeout=10)
        core.chat('산 전시에도 다녀왔어.')
        assert core.story_memory.flush(core.settings.user_id, timeout=10)
        assert len(core.stories()['items']) == 2
        reply = core.chat('전시 이야기 잊어줘')
        assert '어느 이야기' in reply['text']
        assert len(core.stories()['items']) == 2
        item = next(item for item in core.stories()['items'] if item['title'] == '바다 전시')
        assert '삭제했어요' in core.chat(item['story_id'])['text']
        assert [item['title'] for item in core.stories()['items']] == ['산 전시']
        local_list = core.story_command('/stories')
        core.chat('장기기억을 꺼줘')
        assert not core.stories()['policy']['enabled']
        assert len(core.stories()['items']) == 1
        with pytest.raises(ValidationError, match='memory_changed'):
            core.validate_reply(local_list)
    finally:
        core.close()


def test_history_requires_second_explicit_consent(tmp_path):
    core = make_core(tmp_path)
    try:
        core.chat('바다 전시를 보고 마음이 편해졌어.')
        enable(core)
        assert core.story_memory.flush(core.settings.user_id, timeout=10)
        assert core.stories()['items'] == []
        prompt = core.story_command('/stories history')
        assert prompt['data']['turn_count'] == 1
        assert 'OpenAI' in prompt['text']
        core.chat('아니요')
        assert core.stories()['items'] == []
        core.story_command('/stories history')
        core.chat('네')
        assert core.story_memory.flush(core.settings.user_id, timeout=10)
        assert len(core.stories()['items']) == 1
    finally:
        core.close()


def test_piped_mock_does_not_claim_enabled_or_consume_unrelated_input(tmp_path):
    command = [sys.executable, str(CONSOLE_SCRIPT),
               '--provider', 'mock', '--no-tts', '--database', str(tmp_path / 'pipe.sqlite3')]
    result = subprocess.run(command, input='/stories\n/stories on\n/status\n/quit\n',
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '처리 실패' not in result.stdout
    assert '켤 수 없어요' in result.stdout
    assert '"enabled": false' in result.stdout


def test_history_consent_does_not_expand_past_displayed_scope(tmp_path):
    core = make_core(tmp_path)
    other = None
    try:
        core.chat('바다 전시를 봤어.')
        preview = core.story_command('/stories history')
        assert preview['data']['turn_count'] == 1
        other = make_core(tmp_path)
        other.chat('산 전시를 봤어.')
        other.close()
        other = None
        core.chat('네')
        assert core.story_memory.flush(core.settings.user_id, timeout=10)
        assert [item['title'] for item in core.stories()['items']] == ['바다 전시']
    finally:
        if other is not None:
            other.close()
        core.close()


def test_evidence_read_cannot_pick_up_revision_after_concurrent_disable(tmp_path, monkeypatch):
    core = make_core(tmp_path)
    try:
        enable(core)
        core.chat('바다 전시에서 편안했어.')
        assert core.story_memory.flush(core.settings.user_id, timeout=10)
        story_id = core.stories()['items'][0]['story_id']
        original = core.story_memory.evidence
        def interrupted(user, identifier):
            data = original(user, identifier)
            core.story_memory.disable(user)
            return data
        monkeypatch.setattr(core.story_memory, 'evidence', interrupted)
        with pytest.raises(ValidationError, match='memory_changed'):
            core.story_command('/stories sources ' + story_id)
    finally:
        core.close()


def test_actual_command_loop_shows_story_sources_and_cross_session_state(tmp_path, monkeypatch, capsys):
    import console

    original_core = console.ConsoleCore
    monkeypatch.setattr(console, 'ConsoleCore', lambda settings: original_core(
        settings, story_extractor=FixtureExtractor(),
    ))
    commands = iter([
        '/stories on', '네', '바다 전시를 보니 기분이 편안했어.',
        '/stories sync', '/new', '/stories', '/stories sources 바다 전시',
        '장기기억 꺼줘', '/stories', '/quit',
    ])
    monkeypatch.setattr('builtins.input', lambda _prompt: next(commands))
    monkeypatch.setattr(sys, 'argv', [
        'console.py', '--provider', 'mock', '--no-tts',
        '--database', str(tmp_path / 'command.sqlite3'),
    ])
    assert console.main() == 0
    output = capsys.readouterr().out
    assert '처리 실패' not in output
    assert '장기 이야기 기억을 켰어요' in output
    assert '이야기 정리가 끝났어요' in output
    assert '새 대화:' in output
    assert '나: 바다 전시를 보니 기분이 편안했어.' in output
    assert '장기 이야기 기억 꺼짐 · 1개' in output


@pytest.mark.parametrize('recovery', [
    '정리되지 않은 대화가 있어 삭제 범위를 확정하지 못했어요. /stories sync 후 다시 삭제해 주세요.',
    '기억을 끈 동안의 미처리 원문이 있어 삭제 범위를 확정하지 못했어요. '
    '/stories history에서 범위를 확인하고 정리에 다시 동의한 뒤 삭제해 주세요.',
])
def test_delete_preserves_specific_recovery_guidance(tmp_path, monkeypatch, recovery):
    core = make_core(tmp_path)
    try:
        enable(core)
        core.chat('바다 전시를 보고 편안했어.')
        assert core.story_memory.flush(core.settings.user_id, timeout=10)
        story_id = core.stories()['items'][0]['story_id']
        def unsettled(*_args, **_kwargs):
            raise StoryServiceError(recovery)
        monkeypatch.setattr(core.story_memory, 'forget', unsettled)
        result = core.story_command('/stories delete ' + story_id)
        assert '아직 삭제하지 않았어요' in result['text']
        assert recovery in result['text']
        assert len(core.stories()['items']) == 1
    finally:
        core.close()
