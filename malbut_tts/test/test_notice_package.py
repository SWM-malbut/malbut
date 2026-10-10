"""Verify the actual notice survives both production package builds."""

from pathlib import Path
import json
import subprocess
import sys
import wave

import pytest

from malbut_tts.prepare_notices import prepare


@pytest.mark.parametrize('relative', ['malbut_tts', 'malbut_test/malbut_tts'])
def test_built_package_contains_the_real_notice(tmp_path, relative):
    root = Path(__file__).resolve().parents[2]
    package = root / relative
    result = subprocess.run(
        [sys.executable, 'setup.py', 'build_py', '--build-lib', str(tmp_path)],
        cwd=package, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    installed = tmp_path / 'malbut_tts/assets/notice_no_dialogue.wav'
    original = root / 'malbut_tts/malbut_tts/assets/notice_no_dialogue.wav'
    assert installed.read_bytes() == original.read_bytes()
    with wave.open(str(installed), 'rb') as audio:
        assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
        assert audio.getframerate() == 24000
        assert 0 < audio.getnframes() <= 24000 * 30
    directory = tmp_path / 'malbut_tts/audio'
    catalog = json.loads((directory / 'catalog.json').read_text())
    assert catalog['function.get_weather.starting'] == '네, 날씨 조회를 시작하겠습니다.'
    assert prepare(catalog, directory, check_only=True) == 0
    assert installed.read_bytes() == (directory / 'conversation.unavailable.wav').read_bytes()
