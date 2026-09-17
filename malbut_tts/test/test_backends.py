"""Selecting an engine neither imports another engine nor falls back."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_tts.backends import create_synthesizer


def test_cuda_is_explicit_and_receives_precision(monkeypatch):
    factory = Mock()
    monkeypatch.setitem(sys.modules, 'malbut_tts.cuda_synthesis',
                        SimpleNamespace(CudaSynthesizer=factory))
    monkeypatch.setitem(sys.modules, 'malbut_tts.api_synthesis', None)
    monkeypatch.setitem(sys.modules, 'openai', None)
    assert create_synthesizer('/model', backend='qwen-cuda',
                              cuda_dtype='float16',
                              cuda_sentence_mode=False) is factory.return_value
    factory.assert_called_once_with(
        '/model', speaker='Sohee', language='Korean', dtype='float16',
    )


def test_cuda_default_wraps_one_local_engine_and_configured_segment_limit(monkeypatch):
    factory = Mock()
    monkeypatch.setitem(sys.modules, 'malbut_tts.cuda_synthesis',
                        SimpleNamespace(CudaSynthesizer=factory))
    from malbut_tts.sentence_synthesis import SentenceSynthesizer
    engine = create_synthesizer('/model', backend='qwen-cuda', sentence_max_chars=48)
    assert isinstance(engine, SentenceSynthesizer)
    assert engine._synthesizer is factory.return_value
    assert engine._max_chars == 48
    assert engine.sentence_streaming is True
    engine.load()
    factory.return_value.load.assert_called_once_with()


@pytest.mark.parametrize('options', [
    {'cuda_sentence_mode': 'false'}, {'sentence_max_chars': True},
    {'sentence_max_chars': 15}, {'sentence_max_chars': 513},
])
def test_sentence_config_rejected_before_model_import(monkeypatch, options):
    monkeypatch.setitem(sys.modules, 'malbut_tts.cuda_synthesis', None)
    with pytest.raises(ValueError):
        create_synthesizer('/model', backend='qwen-cuda', **options)


def test_unknown_backend_rejected_without_imports(monkeypatch):
    monkeypatch.setitem(sys.modules, 'malbut_tts.cuda_synthesis', None)
    monkeypatch.setitem(sys.modules, 'malbut_tts.api_synthesis', None)
    with pytest.raises(ValueError, match='backend'):
        create_synthesizer('/model', backend='legacy')


def test_cuda_failure_never_falls_back(monkeypatch):
    factory = Mock(side_effect=RuntimeError('CUDA unavailable'))
    api = Mock()
    monkeypatch.setitem(sys.modules, 'malbut_tts.cuda_synthesis',
                        SimpleNamespace(CudaSynthesizer=factory))
    monkeypatch.setitem(sys.modules, 'malbut_tts.api_synthesis',
                        SimpleNamespace(OpenAISynthesizer=api))
    with pytest.raises(RuntimeError, match='CUDA unavailable'):
        create_synthesizer('/model', backend='qwen-cuda')
    api.assert_not_called()


def test_openai_is_default_without_local_model_or_sentence_wrapper(monkeypatch):
    factory = Mock()
    monkeypatch.setitem(sys.modules, 'malbut_tts.api_synthesis',
                        SimpleNamespace(OpenAISynthesizer=factory))
    for module in ('cuda_synthesis', 'sentence_synthesis'):
        monkeypatch.setitem(sys.modules, f'malbut_tts.{module}', None)
    assert create_synthesizer() is factory.return_value
    factory.assert_called_once_with(
        model='gpt-4o-mini-tts', voice='marin', timeout_seconds=8.0,
    )
    factory.return_value.load.assert_not_called()


def test_openai_options_are_separate_from_local_voice_and_precision(monkeypatch):
    factory = Mock()
    monkeypatch.setitem(sys.modules, 'malbut_tts.api_synthesis',
                        SimpleNamespace(OpenAISynthesizer=factory))
    assert create_synthesizer(
        '/unused-local-model', backend='openai', speaker='Sohee',
        api_model='tts-1', api_voice='alloy', api_timeout_seconds=4.5,
    ) is factory.return_value
    factory.assert_called_once_with(model='tts-1', voice='alloy', timeout_seconds=4.5)


def test_openai_failure_never_falls_back(monkeypatch):
    factory = Mock(side_effect=RuntimeError('API unavailable'))
    monkeypatch.setitem(sys.modules, 'malbut_tts.api_synthesis',
                        SimpleNamespace(OpenAISynthesizer=factory))
    for module in ('cuda_synthesis', 'sentence_synthesis'):
        monkeypatch.setitem(sys.modules, f'malbut_tts.{module}', None)
    with pytest.raises(RuntimeError, match='API unavailable'):
        create_synthesizer()
