"""Conservative local agreement for serial snapshots of one PCM utterance."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class _Segment:
    start: float
    end: float
    text: str
    timing_valid: bool = True


def _text(segments):
    return ''.join(segment.text for segment in segments).strip()


def _same(left, right):
    return left.text.strip() == right.text.strip()


class IncrementalWhisperStream:
    """Keep agreed text and decode an overlapping suffix of the original audio.

    The caller owns recording and submits full PCM snapshots serially. Two
    consecutive hypotheses must agree before complete segments become stable. Audio is
    trimmed only at a decoded segment boundary, retaining a committed segment
    for acoustic context. A changed overlap falls back to the full recording;
    it is never blindly concatenated to a possibly incompatible prefix.
    """

    def __init__(self, model):
        self.model = model
        self._committed = []
        self._previous = []
        self._offset_samples = 0
        self.last_metrics = {}

    def _decode(self, pcm, offset_samples):
        import numpy as np

        offset = offset_samples / 16000
        duration = len(pcm) / 32000
        audio = np.frombuffer(pcm, dtype='<i2', offset=offset_samples * 2)
        audio = audio.astype(np.float32) / 32768.0
        prompt = _text([segment for segment in self._committed if segment.end <= offset])
        segments, _ = self.model.transcribe(
            audio, language='ko', beam_size=1, condition_on_previous_text=False,
            initial_prompt=prompt[-200:] or None,
        )
        decoded = []
        for segment in segments:
            if not segment.text.strip():
                continue
            start, end = offset + segment.start, offset + segment.end
            # Native decoder timestamps avoid expensive CPU word alignment.
            valid = (math.isfinite(start + end) and offset <= start <= end
                     and end <= duration + 0.04)
            # Native timestamps may overshoot a truncated live snapshot. Keep
            # its text revisable, but never use such a boundary to trim audio.
            decoded.append(_Segment(
                min(start, duration) if valid else offset,
                min(end, duration) if valid else duration, segment.text, valid,
            ))
        return decoded

    def transcribe(self, pcm: bytes, sample_rate: int, *, final: bool = False,
                   speech_end_s: float | None = None) -> str:
        """Return a revisable full transcript, including this snapshot's last audio.

        Empty output and decode exceptions stay empty/errors even if a prefix
        was committed earlier, so callers cannot publish a stale prefix as a
        successful final result. Existing state survives inference failures.
        Metrics describe this call's actual input window; trimmed_s is the
        retained window's starting offset for the next call. speech_end_s is
        the last voiced time from the caller's VAD, relative to the full input.
        """
        if sample_rate != 16000:
            raise ValueError('local Whisper transcription requires 16kHz PCM')
        if len(pcm) % 2:
            raise ValueError('PCM16 input must contain whole samples')
        duration = len(pcm) / 32000
        if speech_end_s is not None and (
            isinstance(speech_end_s, bool) or not math.isfinite(speech_end_s)
            or not 0 <= speech_end_s <= duration
        ):
            raise ValueError('speech_end_s must be within the PCM snapshot')
        offset = self._offset_samples / 16000
        self.last_metrics = {
            'input_s': duration, 'input_window_s': max(0.0, duration - offset),
            'committed_s': self._committed[-1].end if self._committed else 0.0,
            'trimmed_s': offset, 'fallback_full': False, 'final': final,
            'tail_gap_s': None, 'fallback_reason': None,
        }
        if not any(pcm):
            return ''
        if len(pcm) // 2 <= self._offset_samples:
            raise ValueError('snapshot does not include the retained audio window')
        decoded = self._decode(pcm, self._offset_samples)
        if not decoded:
            self._previous = []
            return ''

        def missing_tail(result):
            # Native segment times have some slack. VAD identifies actual
            # unaccounted speech; direct callers without that hint tolerate
            # the normal three-second endpoint silence before recovering.
            end = max(segment.end for segment in result)
            gap = max(0.0, (duration if speech_end_s is None else speech_end_s) - end)
            self.last_metrics['tail_gap_s'] = gap
            return gap > (3.0 if speech_end_s is None else 0.8)

        uncovered_tail = missing_tail(decoded)
        invalid_timing = any(not segment.timing_valid for segment in decoded)

        retained = [segment for segment in self._committed if segment.end > offset + 0.01]
        overlap_matches = len(decoded) >= len(retained) and all(
            old.timing_valid and new.timing_valid and _same(old, new)
            and abs(old.start - new.start) <= 0.8
            and abs(old.end - new.end) <= 0.8
            for old, new in zip(retained, decoded)
        )
        reset = not overlap_matches or bool(
            self._offset_samples and (uncovered_tail or invalid_timing))
        if reset and self._offset_samples:
            # Re-decode before mutating state: a failed recovery must not
            # erase the last valid prefix or pretend it is a final answer.
            decoded = self._decode(pcm, 0)
            self.last_metrics.update(
                input_window_s=duration, fallback_full=True,
                fallback_reason=('missing_tail' if uncovered_tail else
                                 'invalid_timing' if invalid_timing else 'overlap_changed'),
            )
            if not decoded:
                self._previous = []
                return ''
            uncovered_tail = missing_tail(decoded)

        if speech_end_s is not None and uncovered_tail:
            # Never let a provisional endpoint result containing only old
            # speech become a successful final transcript in the caller.
            raise ValueError('incremental transcription omitted recent speech')

        if reset:
            self._committed, self._previous = [], []
            self._offset_samples = 0
            self.last_metrics['committed_s'] = 0.0
            retained = []

        pending = decoded[len(retained):]
        stable_count = 0
        for old, new in zip(self._previous, pending):
            if (not old.timing_valid or not new.timing_valid
                    or not _same(old, new) or abs(old.start - new.start) > 0.8
                    or abs(old.end - new.end) > 0.8
                    or new.end > duration - 1.0):
                break
            stable_count += 1
        self._committed.extend(pending[:stable_count])
        self._previous = pending[stable_count:]
        result = _text(self._committed + self._previous)

        if self._committed:
            stable_end = self._committed[-1].end
            # Keep the newest completed segment, including its acoustic context.
            if (len(self._committed) >= 2
                    and self._committed[-2].end >= self._offset_samples / 16000 + 8):
                self._offset_samples = int(self._committed[-2].end * 16000)
            self.last_metrics['committed_s'] = stable_end
        self.last_metrics['trimmed_s'] = self._offset_samples / 16000
        return result
