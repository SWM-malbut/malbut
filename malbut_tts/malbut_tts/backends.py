"""Select one TTS backend; never silently fall back."""

import os
from pathlib import Path


def default_piper_model():
    """Locate the installed robot voice without downloading anything."""
    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return cache / 'malbut_speech/models/piper-kss/ko_KR-kss-medium.onnx'


def create_synthesizer(model_path=None, *, backend='piper', speaker='Sohee',
                       language='Korean', cuda_dtype='float32',
                       cuda_sentence_mode=True, sentence_max_chars=80,
                       api_model='gpt-4o-mini-tts', api_voice='nova',
                       api_timeout_seconds=10.0, piper_threads=6):
    """Default to local CPU speech; remote/experimental engines require opt-in."""
    if backend == 'piper':
        from malbut_tts.piper_synthesis import PiperSynthesizer
        return PiperSynthesizer(model_path or default_piper_model(), threads=piper_threads)
    if backend == 'openai':
        from malbut_tts.api_synthesis import OpenAISynthesizer
        return OpenAISynthesizer(
            model=api_model, voice=api_voice, timeout_seconds=api_timeout_seconds,
        )
    if backend != 'qwen-cuda':
        raise ValueError('TTS backend must be piper, qwen-cuda or openai')
    if type(cuda_sentence_mode) is not bool:
        raise ValueError('cuda_sentence_mode must be a boolean')
    if type(sentence_max_chars) is not int or not 16 <= sentence_max_chars <= 512:
        raise ValueError('sentence_max_chars must be an integer from 16 through 512')
    from malbut_tts.cuda_synthesis import CudaSynthesizer
    synthesizer = CudaSynthesizer(
        model_path, speaker=speaker, language=language, dtype=cuda_dtype,
    )
    if cuda_sentence_mode:
        from malbut_tts.sentence_synthesis import SentenceSynthesizer
        return SentenceSynthesizer(synthesizer, max_chars=sentence_max_chars)
    return synthesizer
