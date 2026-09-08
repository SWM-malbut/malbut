"""Use the OpenAI Transcriptions API for one completed in-memory recording."""

import io
import wave
from typing import Any


class OpenAITranscriber:
    """Convert PCM to WAV and preserve the returned transcript verbatim."""

    def __init__(self, client: Any, model: str = 'gpt-transcribe') -> None:
        """Accept an SDK client so tests do not need credentials or network."""
        self.client = client
        self.model = model

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """Submit exactly one recording with a Korean language hint."""
        with io.BytesIO() as buffer:
            with wave.open(buffer, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm)
            response = self.client.audio.transcriptions.create(
                model=self.model,
                file=('utterance.wav', buffer.getvalue(), 'audio/wav'),
                extra_body={'languages': ['ko']},
            )
        if not isinstance(response.text, str):
            raise ValueError('transcription response must contain text')
        return response.text
