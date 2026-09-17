"""Exercise CLI selection without downloading models or opening audio."""

from unittest.mock import Mock

import pytest

from malbut_tts import smoke


@pytest.mark.parametrize('options,backend,dtype', [
    (['--backend', 'qwen-cuda'], 'qwen-cuda', 'float32'),
    (['--backend', 'qwen-cuda', '--cuda-dtype', 'float16'],
     'qwen-cuda', 'float16'),
])
def test_single_request_uses_selected_backend_and_same_runtime(
    tmp_path, monkeypatch, options, backend, dtype,
):
    factory = Mock()
    monkeypatch.setattr('malbut_tts.backends.create_synthesizer', factory)
    received = {}

    class Runtime:
        def __init__(self, synth, player_factory, on_status):
            received['synth'] = synth
            self.on_status = on_status

        def submit(self, text):
            received['text'] = text
            self.on_status('test-id', 'finished')
            return 'test-id'

        def close(self):
            received['closed'] = True

    monkeypatch.setattr(smoke, 'SpeechRuntime', Runtime)
    assert smoke.main(['--model-path', str(tmp_path), '--text', ' 원문 ',
                       *options]) == 0
    factory.assert_called_once_with(tmp_path, backend=backend, cuda_dtype=dtype,
                                    cuda_sentence_mode=True, sentence_max_chars=80,
                                    api_model='gpt-4o-mini-tts', api_voice='marin',
                                    api_timeout_seconds=8.0)
    assert received == {'synth': factory.return_value,
                        'text': ' 원문 ', 'closed': True}


def test_unknown_backend_fails_before_runtime_creation(tmp_path, monkeypatch):
    runtime = Mock()
    monkeypatch.setattr(smoke, 'SpeechRuntime', runtime)
    with pytest.raises(SystemExit) as error:
        smoke.main(['--model-path', str(tmp_path), '--backend', 'legacy'])
    assert error.value.code == 2
    runtime.assert_not_called()


@pytest.mark.parametrize('options,model,voice,timeout', [
    ([], 'gpt-4o-mini-tts', 'marin', 8.0),
    (['--api-model', 'tts-1', '--api-voice', 'alloy',
      '--api-timeout-seconds', '3.5'], 'tts-1', 'alloy', 3.5),
])
def test_default_api_request_needs_no_local_model_and_discloses_external_audio(
    monkeypatch, capsys, options, model, voice, timeout,
):
    factory = Mock()
    monkeypatch.setattr('malbut_tts.backends.create_synthesizer', factory)
    received = {}

    class Runtime:
        def __init__(self, synth, player_factory, on_status):
            received['synth'] = synth
            self.on_status = on_status

        def submit(self, text):
            received['text'] = text
            self.on_status('api-id', 'finished')
            return 'api-id'

        def close(self):
            received['closed'] = True

    monkeypatch.setattr(smoke, 'SpeechRuntime', Runtime)
    assert smoke.main(['--text', ' 원문 ', *options]) == 0
    factory.assert_called_once_with(
        None, backend='openai', cuda_dtype='float32', cuda_sentence_mode=True,
        sentence_max_chars=80, api_model=model, api_voice=voice,
        api_timeout_seconds=timeout,
    )
    assert received == {'synth': factory.return_value,
                        'text': ' 원문 ', 'closed': True}
    output = capsys.readouterr().out
    assert '유료 외부 API' in output
    assert 'AI 합성 음성' in output


def test_local_backend_still_requires_model_path(monkeypatch):
    runtime = Mock()
    monkeypatch.setattr(smoke, 'SpeechRuntime', runtime)
    with pytest.raises(SystemExit) as error:
        smoke.main(['--backend', 'qwen-cuda', '--text', '안녕하세요.'])
    assert error.value.code == 2
    runtime.assert_not_called()
