"""Offline preparation preserves voice timing, provenance and the PCM contract."""

import json
import sys
from threading import Event

import numpy as np
import pytest

from malbut_tts.piper_notices import PiperNotices
from malbut_tts.prepare_notices import main, prepare


def test_resampling_preserves_duration_pitch_and_closes_the_voice(monkeypatch):
    source = np.sin(2 * np.pi * 1000 * np.arange(22050) / 22050).astype(np.float32) * .5
    closed = []

    class Voice:
        def __init__(self, path, *, threads):
            assert path == '/local/kss.onnx' and threads == 6

        def load(self):
            pass

        def generate(self, text, cancel):
            try:
                assert text == '원문 그대로' and not cancel.is_set()
                yield source, 22050
            finally:
                closed.append(True)

    monkeypatch.setattr('malbut_tts.piper_notices.PiperSynthesizer', Voice)
    [(pcm, rate)] = list(PiperNotices('/local/kss.onnx').generate('원문 그대로', Event()))
    assert rate == 24000 and len(pcm) == 24000 and closed == [True]
    expected = .5 * np.sin(2 * np.pi * 1000 * np.arange(24000) / 24000)
    np.testing.assert_allclose(pcm[40:-40], expected[40:-40], atol=.001)


def test_preparation_rejects_recordings_from_different_normalization(tmp_path):
    class Voice:
        def generate(self, text, cancel):
            yield np.zeros(2400, dtype=np.float32), 24000

    catalog = {'notice': '작업을 시작할게요.'}
    original = dict(model='piper', voice='kss', instructions=None,
                    generation_profile={'backend': 'piper', 'source_sha256': 'old'})
    assert prepare(catalog, tmp_path, Voice(), **original) == 1
    changed = {**original, 'generation_profile': {'backend': 'piper', 'source_sha256': 'new'}}
    with pytest.raises(ValueError, match='does not match'):
        prepare(catalog, tmp_path, check_only=True, **changed)


@pytest.mark.parametrize('check', [False, True])
def test_piper_cli_never_creates_an_api_client_or_loads_a_model_to_check(
        tmp_path, monkeypatch, check):
    import malbut_tts.piper_notices as notices

    catalog = {'notice': '작업을 시작할게요.'}
    catalog_path = tmp_path / 'catalog.json'
    catalog_path.write_text(json.dumps(catalog))
    settings = dict(model='piper', voice='kss', instructions=None,
                    generation_profile={'backend': 'piper'})
    calls = []

    class Voice:
        def __init__(self, *args, **kwargs):
            calls.append('load')

        def generate(self, text, cancel):
            yield np.zeros(2400, dtype=np.float32), 24000

    if check:
        prepare(catalog, tmp_path / 'out', Voice(), **settings)
        calls.clear()
    monkeypatch.setitem(sys.modules, 'malbut_tts.api_synthesis', None)
    monkeypatch.setattr(notices, 'notice_settings', lambda *a, **kw: settings)
    monkeypatch.setattr(notices, 'PiperNotices', Voice)
    args = ['--model-path', '/local/kss.onnx',
            '--catalog', str(catalog_path), '--output-dir', str(tmp_path / 'out')]
    assert main(args + (['--check'] if check else [])) == 0
    assert calls == ([] if check else ['load'])
