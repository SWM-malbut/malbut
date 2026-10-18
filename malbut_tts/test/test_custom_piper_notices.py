"""Custom notices require an explicit model identity and retain provenance."""

import hashlib
import json
import sys

import numpy as np
import pytest

from malbut_tts.custom_piper_notices import custom_notice_settings
from malbut_tts.piper_notices import notice_settings
from malbut_tts.prepare_notices import main


@pytest.fixture
def custom_voice(tmp_path, monkeypatch):
    monkeypatch.setattr('malbut_tts.custom_piper_notices.version', lambda _: 'test')
    model = tmp_path / 'private.onnx'
    model.write_bytes(b'private test model')
    config = tmp_path / 'private.onnx.json'
    config.write_text(json.dumps({'audio': {'sample_rate': 22050}}))
    return model, config, hashlib.sha256(model.read_bytes()).hexdigest()


def test_custom_profile_is_content_identified_and_default_kss_stays_locked(custom_voice):
    model, config, digest = custom_voice
    with pytest.raises(ValueError, match='selected Piper KSS'):
        notice_settings(model)
    settings = custom_notice_settings(model, digest.upper())
    assert settings['model'] == 'local-piper/sha256/' + digest
    assert settings['voice'] == 'custom'
    profile = settings['generation_profile']
    assert profile['model_sha256'] == digest
    assert profile['model_config_sha256'] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert 'custom_piper_notices.py' in profile['source_sha256']


@pytest.mark.parametrize('digest', ['no-hash', '0' * 64])
def test_cli_rejects_wrong_identity_before_model_load(custom_voice, tmp_path, monkeypatch, digest):
    import malbut_tts.piper_notices as notices
    model, _, _ = custom_voice
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'notice': '작업을 완료했어요.'}))
    monkeypatch.setattr(notices, 'PiperNotices', lambda *a, **kw: pytest.fail('model loaded'))
    assert main(['--model-path', str(model), '--piper-model-sha256', digest,
                 '--catalog', str(catalog), '--output-dir', str(tmp_path / 'out')]) == 1
    assert not (tmp_path / 'out').exists()


def test_custom_config_must_exist_and_use_supported_rate(custom_voice):
    model, config, digest = custom_voice
    config.write_text(json.dumps({'audio': {'sample_rate': 24000}}))
    with pytest.raises(ValueError, match='22050'):
        custom_notice_settings(model, digest)
    config.unlink()
    with pytest.raises(FileNotFoundError):
        custom_notice_settings(model, digest)


def test_custom_cli_generation_check_and_stale_config(custom_voice, tmp_path, monkeypatch):
    import malbut_tts.piper_notices as notices
    model, config, digest = custom_voice
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'notice': '작업을 완료했어요.'}))
    loads = []

    class Voice:
        def __init__(self, path, *, threads):
            assert path == model and threads == 6
            loads.append(True)

        def generate(self, text, cancel):
            yield np.zeros(2400, dtype=np.float32), 24000

    monkeypatch.setitem(sys.modules, 'malbut_tts.api_synthesis', None)
    monkeypatch.setattr(notices, 'PiperNotices', Voice)
    args = ['--model-path', str(model), '--piper-model-sha256', digest,
            '--catalog', str(catalog), '--output-dir', str(tmp_path / 'out')]
    assert main(args) == 0
    assert loads == [True]
    assert main(args + ['--check']) == 0
    assert loads == [True]
    config.write_text(json.dumps({'audio': {'sample_rate': 22050}, 'phoneme_id_map': {}}))
    assert main(args + ['--check']) == 1
    assert loads == [True]
