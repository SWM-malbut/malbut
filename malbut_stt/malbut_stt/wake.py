"""Recognize a Korean call and preserve any request spoken in the same utterance."""

from pathlib import Path
import unicodedata


_WAKE_PHRASES = tuple(sorted(
    (prefix + name for prefix in ('', '야', '저기') for name in ('제이크', '제이크야')),
    key=len, reverse=True,
))
_CALL_DELIMITERS = ',，、.!！?？:：;；…'


def parse_wake_phrase(text: str) -> str | None:
    """Return the remaining request, '' for a call alone, or None for no call.

    Match only at the beginning. Ignore spacing/punctuation within the call,
    but require a boundary after the name so 제이크가/제이크는 are not calls.
    Keep the request's spacing, quotes, numbers and punctuation intact.
    """
    if not isinstance(text, str):
        return None
    normalized = unicodedata.normalize('NFC', text)
    positions = [
        index for index, char in enumerate(normalized)
        if not char.isspace() and not unicodedata.category(char).startswith('P')
    ]
    compact = ''.join(normalized[index] for index in positions)
    if compact in _WAKE_PHRASES:
        return ''
    for phrase in _WAKE_PHRASES:
        if not compact.startswith(phrase):
            continue
        end = positions[len(phrase) - 1] + 1
        if end == len(normalized) or not (
                normalized[end].isspace() or normalized[end] in _CALL_DELIMITERS):
            continue
        while end < len(normalized) and (
                normalized[end].isspace() or normalized[end] in _CALL_DELIMITERS):
            end += 1
        # Repeating just the name is not a request to publish to the Agent.
        if compact[len(phrase):] in _WAKE_PHRASES:
            return None
        return normalized[end:].strip()
    return None


def is_wake_phrase(text: str) -> bool:
    """Accept a supported call, optionally followed by a request."""
    return parse_wake_phrase(text) is not None


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
