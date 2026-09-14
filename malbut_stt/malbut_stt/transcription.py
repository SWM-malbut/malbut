"""Transcribe completed in-memory recordings with local Whisper or OpenAI."""

import io
from pathlib import Path
import wave
from typing import Any


class LocalWhisperTranscriber:
    """Reuse a downloaded Whisper model without runtime downloads or API calls."""

    def __init__(self, model_path: str, *, device: str = 'cpu',
                 compute_type: str = 'int8') -> None:
        """Require local model assets before loading the inference runtime."""
        path = Path(model_path).expanduser()
        if not path.is_dir():
            raise ValueError('Whisper model must be an existing local directory')
        # faster-whisper otherwise falls back to a hosted tokenizer for local models.
        if not (path / 'tokenizer.json').is_file():
            raise ValueError('local Whisper model is missing tokenizer.json')
        from faster_whisper import WhisperModel

        self.model = WhisperModel(
            str(path), device=device, compute_type=compute_type, cpu_threads=6,
            local_files_only=True,
        )

    def transcribe(self, pcm: bytes, sample_rate: int, *,
                   initial_prompt: str | None = None) -> str:
        """Decode mono PCM16, preserving text except for surrounding whitespace."""
        if sample_rate != 16000:
            raise ValueError('local Whisper transcription requires 16kHz PCM')
        if len(pcm) % 2:
            raise ValueError('PCM16 input must contain whole samples')
        if not any(pcm):
            return ''
        import numpy as np

        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            audio, language='ko', beam_size=1, condition_on_previous_text=False,
            initial_prompt=initial_prompt,
        )
        return ''.join(segment.text for segment in segments).strip()


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
