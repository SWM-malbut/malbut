"""Preserve speech unless the backend supplies joint negative ASR evidence.

Use Whisper's existing no-speech/log-probability thresholds. Missing scores,
invalid values, and approximate token averages are not evidence of silence.
Filtering is per segment; unrelated speech and its timestamps are unchanged.
"""

import math
from numbers import Real


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def confidence_scores(segment):
    """Return only valid backend probabilities and actual average log probability."""
    no_speech = _finite_number(getattr(segment, 'no_speech_prob', None))
    logprob = _finite_number(getattr(segment, 'avg_logprob', None))
    if no_speech is not None and not 0 <= no_speech <= 1:
        no_speech = None
    if logprob is not None and logprob > 0:
        logprob = None
    return no_speech, logprob


def has_no_speech_evidence(segment):
    no_speech, logprob = confidence_scores(segment)
    return (no_speech is not None and logprob is not None
            and no_speech > .6 and logprob < -1.0)


def filter_segments(segments, model=None, *, on_rejected=None):
    """Drop only jointly suspect spans; optionally report numeric evidence, not text."""
    report = getattr(model, 'report_confidence', None)

    def emit(event, **metadata):
        if callable(report):
            try:
                report(event, **metadata)
            except Exception:
                # Diagnostic failures must not change the transcript or capture lifecycle.
                pass

    inspected = scored = rejected = no_speech_available = logprob_available = 0
    for index, segment in enumerate(segments):
        inspected += 1
        no_speech, logprob = confidence_scores(segment)
        no_speech_available += no_speech is not None
        logprob_available += logprob is not None
        if no_speech is not None and logprob is not None:
            scored += 1
        if has_no_speech_evidence(segment):
            rejected += 1
            if on_rejected is not None:
                on_rejected(segment)
            emit('confidence_segment_rejected', segment_index=index,
                 reason='no_speech_and_low_logprob',
                 no_speech_prob=no_speech, avg_logprob=logprob)
            continue
        yield segment
    emit('confidence_filter_summary', inspected_segments=inspected,
         scored_segments=scored, unavailable_segments=inspected - scored,
         no_speech_available_segments=no_speech_available,
         avg_logprob_available_segments=logprob_available,
         rejected_segments=rejected)
