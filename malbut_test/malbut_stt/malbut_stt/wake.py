"""Recognize a complete wake phrase with an already downloaded local model."""

from pathlib import Path
import unicodedata


def is_wake_phrase(text: str) -> bool:
    """Accept 제이크 and its vocative 제이크야, ignoring spaces and punctuation."""
    normalized = unicodedata.normalize('NFC', text)
    return ''.join(
        char for char in normalized
        if not char.isspace() and not unicodedata.category(char).startswith('P')
    ) in ('제이크', '제이크야')


class LocalWakeRecognizer:
    """Run Korean Whisper locally; construction must never download a model."""

    def __init__(self, model_path, *, compute_type='int8'):
        path = Path(model_path).expanduser()
        if not path.is_dir():
            raise ValueError('wake model must be an existing local directory')
        # faster-whisper otherwise falls back to a hosted tokenizer even for a local model.
        if not (path / 'tokenizer.json').is_file():
            raise ValueError('local wake model is missing tokenizer.json')
        from faster_whisper import WhisperModel

        self.model = WhisperModel(
            str(path), device='cpu', compute_type=compute_type, cpu_threads=6,
            local_files_only=True,
        )

    @classmethod
    def from_transcriber(cls, transcriber):
        """Share a loaded local model when wake and sentence inference are serialized."""
        recognizer = cls.__new__(cls)
        recognizer.model = transcriber.model
        return recognizer

    def _transcribe(self, audio):
        segments, _ = self.model.transcribe(
            audio, language='ko', beam_size=1, condition_on_previous_text=False,
            initial_prompt='로봇 이름은 제이크입니다.',
        )
        return ''.join(segment.text for segment in segments).strip()

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """Transcribe mono PCM16 from a completed, closed microphone capture."""
        if sample_rate != 16000:
            raise ValueError('local wake recognition requires 16kHz PCM')
        if len(pcm) % 2:
            raise ValueError('PCM16 input must contain whole samples')
        if not any(pcm):
            return ''
        import numpy as np

        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        return self._transcribe(audio)

    def transcribe_file(self, path) -> str:
        """Exercise the same recognizer with a local test audio file."""
        return self._transcribe(str(path))
