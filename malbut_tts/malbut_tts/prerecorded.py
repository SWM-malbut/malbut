"""Read bounded mono PCM WAV notices through the normal playback worker."""

from importlib.resources import files
from pathlib import Path
import re
import wave

import numpy as np


def valid_audio_id(value):
    """Accept catalog IDs, never filesystem paths."""
    return isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_.-]{0,127}', value) is not None


class PrerecordedAudio:
    """Yield file PCM with cancellation; never create an API/model client."""

    sentence_streaming = False
    max_seconds = 30

    def __init__(self, directory=None):
        self.directory = (Path(directory).expanduser().resolve() if directory
                          else Path(str(files('malbut_tts').joinpath('audio'))).resolve())

    def generate(self, audio_id, cancel_event):
        """Reject missing, invalid, escaped or truncated files as failed speech."""
        if cancel_event.is_set():
            return
        if not valid_audio_id(audio_id):
            raise ValueError('invalid prerecorded audio ID')
        path = (self.directory / (audio_id + '.wav')).resolve()
        if path.parent != self.directory:
            raise ValueError('prerecorded audio must be inside audio_directory')
        with wave.open(str(path), 'rb') as stream:
            rate = stream.getframerate()
            frames = stream.getnframes()
            if (stream.getnchannels() != 1 or stream.getsampwidth() != 2
                    or stream.getcomptype() != 'NONE' or rate != 24000
                    or not 0 < frames <= rate * self.max_seconds):
                raise ValueError('notice WAV must be mono 24 kHz PCM16, at most 30 seconds')
            # Validate the entire short clip before emitting any of it.
            pcm = stream.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError('truncated prerecorded audio')
        for offset in range(0, len(pcm), 4800):
            if cancel_event.is_set():
                return
            audio = np.frombuffer(pcm[offset:offset + 4800], dtype='<i2')
            audio = audio.astype(np.float32) / 32768
            yield audio, rate
