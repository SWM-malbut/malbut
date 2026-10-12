"""Generate an observed copy of the pinned source; never edit its checkout."""

from pathlib import Path
import sys


ANCHOR = '            // update audio window\n            seek += seek_delta;'
OBSERVATION = '''            // Malbut: copy the actual #2625 decision and #2629 seek, including
            // windows that produced no segments. No threshold or token changes.
            const mb_whisper_window mb_window = {
                state->no_speech_prob, best_decoder.sequence.avg_logprobs,
                params.no_speech_thold, params.logprob_thold,
                seek / 100.0, std::min(seek_end, seek + WHISPER_CHUNK_SIZE * 100) / 100.0,
                (seek + seek_delta) / 100.0,
                is_no_speech, single_timestamp_ending,
                (int) n_segments_before, (int) (state->result_all.size() - n_segments_before),
                (int) best_decoder.sequence.result_len,
            };
            mb_whisper_trace_emit(&mb_window);

'''


def instrument(source):
    if source.count(ANCHOR) != 1:
        raise ValueError('pinned Whisper observation point is missing or ambiguous')
    return '#include "whisper_trace.h"\n' + source.replace(ANCHOR, OBSERVATION + ANCHOR)


if __name__ == '__main__':
    source, target = map(Path, sys.argv[1:])
    content = instrument(source.read_text())
    if not target.exists() or target.read_text() != content:
        target.write_text(content)
