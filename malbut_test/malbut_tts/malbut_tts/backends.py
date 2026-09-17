"""Select one TTS backend; never silently fall back."""


def create_synthesizer(model_path=None, *, backend='openai', speaker='Sohee',
                       language='Korean', cuda_dtype='float32',
                       cuda_sentence_mode=True, sentence_max_chars=80,
                       api_model='gpt-4o-mini-tts', api_voice='marin',
                       api_timeout_seconds=8.0):
    """Create the selected backend, defaulting to OpenAI audio streaming."""
    if backend == 'openai':
        from malbut_tts.api_synthesis import OpenAISynthesizer
        return OpenAISynthesizer(
            model=api_model, voice=api_voice, timeout_seconds=api_timeout_seconds,
        )
    if backend != 'qwen-cuda':
        raise ValueError('TTS backend must be qwen-cuda or openai')
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
