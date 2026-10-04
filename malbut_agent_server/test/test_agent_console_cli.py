"""Exercise the real command loop with mock inference and isolated databases."""

import json
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from test_agent_console_support import CONSOLE_SCRIPT, restore_console_umask  # noqa: F401


def test_weather_key_from_loaded_env_reaches_console(tmp_path, monkeypatch, capsys):
    import console

    received = []
    original_core = console.ConsoleCore

    def core(settings, **kwargs):
        received.append(kwargs['weather_service_key'])
        return original_core(settings, **kwargs)

    monkeypatch.setattr(console, 'load_env_file',
                        lambda _path, target: target.update(KMA_SERVICE_KEY='fixture-key'))
    monkeypatch.setattr(console, 'ConsoleCore', core)
    monkeypatch.setattr('builtins.input', lambda _prompt: '/quit')
    monkeypatch.setattr(sys, 'argv', [
        'console.py', '--provider', 'mock', '--no-tts',
        '--database', str(tmp_path / 'agent.sqlite3'),
    ])
    assert console.main() == 0
    assert received == ['fixture-key']
    captured = capsys.readouterr()
    assert 'fixture-key' not in captured.out + captured.err


@pytest.mark.parametrize('error', [RuntimeError, KeyboardInterrupt])
def test_startup_failure_closes_core(tmp_path, monkeypatch, error):
    import console

    closed = []
    original_close = console.ConsoleCore.close

    def close(core):
        original_close(core)
        closed.append(core)

    def fail(*args, **kwargs):
        raise error()

    monkeypatch.setattr(console.ConsoleCore, 'close', close)
    monkeypatch.setattr(console, 'build_situation_factory', fail)
    monkeypatch.setattr(sys, 'argv', [
        'console.py', '--provider', 'mock', '--no-tts',
        '--database', str(tmp_path / 'agent.sqlite3'),
    ])
    with pytest.raises(error):
        console.main()
    assert len(closed) == 1


@pytest.mark.parametrize('arguments,commands,enabled', [
    (['--no-tts'], ['/fall', '/quit'], False),
    ([], ['/tts off', '/fall', '/quit'], False),
    (['--no-tts'], ['/tts on', '/fall', '/quit'], True),
])
def test_fall_respects_voice_output_setting(tmp_path, monkeypatch, capsys,
                                           arguments, commands, enabled):
    import console

    called = []
    closed = []
    answers = iter(commands)
    monkeypatch.setattr(console, 'OpenAISynthesizer',
                        lambda **_kwargs: SimpleNamespace(load=lambda: None))
    monkeypatch.setattr(console, 'ConsoleAudio', lambda *_args, **_kwargs: SimpleNamespace(
        fall=lambda *_: (called.append(True), ('succeeded', None))[1],
        close=lambda: closed.append(True),
    ))
    monkeypatch.setattr('builtins.input', lambda _prompt: next(answers))
    monkeypatch.setattr(sys, 'argv', [
        'console.py', '--provider', 'mock', '--database', str(tmp_path / 'agent.sqlite3'),
        *arguments,
    ])
    assert console.main() == 0
    assert called == ([True] if enabled else [])
    assert closed == ([True] if enabled else [])
    if not enabled:
        assert '음성 출력이 꺼져' in capsys.readouterr().out


def test_no_tts_does_not_construct_audio_or_synthesizer(tmp_path, monkeypatch):
    import console

    def unexpected(*_args, **_kwargs):
        pytest.fail('text-only startup constructed an optional voice component')

    monkeypatch.setattr(console, 'ConsoleAudio', unexpected)
    monkeypatch.setattr(console, 'OpenAISynthesizer', unexpected)
    commands = iter(['안녕', '/status', '/quit'])
    monkeypatch.setattr('builtins.input', lambda _prompt: next(commands))
    monkeypatch.setattr(sys, 'argv', [
        'console.py', '--provider', 'mock', '--no-tts',
        '--database', str(tmp_path / 'agent.sqlite3'),
    ])
    assert console.main() == 0


def test_terminal_modes_and_persistence(tmp_path):
    command = [sys.executable, str(CONSOLE_SCRIPT), '--provider', 'mock', '--no-tts',
               '--database', str(tmp_path / 'agent.sqlite3')]
    first = subprocess.run(command, input='\n'.join([
        '우리 강아지 이름은 초코야. 기억해줘', '네', '/memory',
        '초기 설정 말투=친근한 반말, 호칭=현재', '/settings', '/new',
        '우리 강아지 이름이 뭐야?', '/tool navigate {"location":"거실"}',
        '/tool get_weather', '/tools', '/fall text', '그냥 누워 있는 거야',
        '/robot', '거실로 가줘', '네', '/back', '/status', '/quit', '',
    ]), capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stderr
    assert '처리 실패' not in first.stdout, first.stdout
    for expected in ('초코', '현재', 'nav2_goal_published', 'resolved',
                     'confirmation_approved', 'physical_authorized', 'unavailable'):
        assert expected in first.stdout, expected
    second = subprocess.run(command, input='/memory\n/quit\n', capture_output=True,
                            text=True, timeout=30)
    assert second.returncode == 0 and '초코' in second.stdout
    assert '"enabled": true' in second.stdout


def test_mock_no_tts_in_clean_process_without_voice_dependencies_or_assets(tmp_path):
    """An installed local audio stack cannot mask eager optional imports in CI."""
    missing_model = tmp_path / 'missing-model.bin'
    missing_library = tmp_path / 'missing-library.so'
    database = tmp_path / 'database' / 'agent.sqlite3'
    bootstrap = textwrap.dedent('''
        import importlib.abc
        from pathlib import Path
        import runpy
        import sys

        blocked = {'numpy', 'openai', 'sounddevice', 'webrtcvad',
                   'console_audio', 'fall_voice', 'malbut_stt', 'malbut_tts'}
        attempted = []

        class NoVoiceImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.', 1)[0] in blocked:
                    attempted.append(fullname)
                    raise ModuleNotFoundError('optional voice dependency blocked: ' + fullname)

        assert not blocked.intersection(sys.modules)
        sys.meta_path.insert(0, NoVoiceImports())
        script, database, model, library = sys.argv[1:]
        assert not Path(model).exists() and not Path(library).exists()
        sys.path.insert(0, str(Path(script).parent))
        sys.argv = [script, '--provider', 'mock', '--no-tts', '--database', database,
                    '--stt-model', model, '--stt-library', library]
        try:
            runpy.run_path(script, run_name='__main__')
        except SystemExit as result:
            assert result.code in (None, 0), result.code
        assert attempted == [], attempted
        assert not blocked.intersection(sys.modules)
        print('portable-console-import-check: passed')
    ''')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('OPENAI_', 'MALBUT_', 'PYTHON'))}
    # The console loads the repository .env with setdefault. Explicit empties
    # keep this test credential-free even in a developer checkout with a key.
    env['OPENAI_API_KEY'] = ''
    result = subprocess.run(
        [sys.executable, '-I', '-B', '-c', bootstrap, str(CONSOLE_SCRIPT), str(database),
         str(missing_model), str(missing_library)],
        input='안녕\n/status\n/fall text\n그냥 누워 있는 거야\n/quit\n',
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '처리 실패' not in result.stdout, result.stdout
    assert 'portable-console-import-check: passed' in result.stdout
    assert 'resolved' in result.stdout
    assert database.is_file()
    assert json.loads(database.with_suffix('.status.json').read_text())['state'] == 'closed'
