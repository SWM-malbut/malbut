"""Correlate metadata-only speech logs on one host; no audio or text is recorded.

Run ``python -m malbut_agent_server.speech_metrics LOG`` on a combined ROS log.
The playback boundary is the device stream's PLAYING acknowledgement, not an
acoustic microphone measurement. Never add overlapping stage durations.
"""

from collections import OrderedDict
import json
import math
import time


def emit_timing(logger, stage, request_id, **fields):
    try:
        logger.info(json.dumps(dict(event='speech_timing', version=1,
            stage=stage, request_id=request_id, at_s=time.monotonic(), **fields)))
    except Exception:
        pass


class SpeechMetrics:
    """Bounded, duplicate-tolerant collection; missing observations stay null."""

    def __init__(self, capacity=256):
        self.capacity = capacity
        self.turns = OrderedDict()

    def observe(self, event):
        if not isinstance(event, dict) or event.get('event') != 'speech_timing':
            return
        uid, stage, stamp = (event.get(key) for key in ('request_id', 'stage', 'at_s'))
        if (not isinstance(uid, str) or not 0 < len(uid) <= 256
                or stage not in {'speech_started', 'endpoint', 'transcript_final',
                    'agent_queued', 'agent_started', 'agent_finished', 'tts_started',
                    'tts_first_audio', 'playback_started', 'playback_terminal'}
                or type(stamp) not in (int, float) or not math.isfinite(stamp)):
            return
        turn = self.turns.setdefault(uid, {'stages': {}, 'playbacks': {}})
        self.turns.move_to_end(uid)
        while len(self.turns) > self.capacity:
            self.turns.popitem(last=False)
        if stage.startswith(('tts_', 'playback_')):
            # Progress notices must not masquerade as the final answer.
            pid = event.get('playback_id')
            if (event.get('interim') is not False or not isinstance(pid, str)
                    or not 0 < len(pid) <= 200):
                return
            if pid not in turn['playbacks'] and len(turn['playbacks']) >= 8:
                return
            values = turn['playbacks'].setdefault(pid, {})
        else:
            values = turn['stages']
        values.setdefault(stage, stamp)
        if stage == 'endpoint':
            end = event.get('estimated_speech_end_s')
            if type(end) in (int, float) and math.isfinite(end) and end <= stamp:
                values.setdefault('estimated_speech_end', end)
        if stage == 'playback_terminal':
            values['terminal_state'] = event.get('state')

    def summaries(self):
        def duration(values, start, end):
            a, b = values.get(start), values.get(end)
            return round((b-a)*1000, 3) if a is not None and b is not None and b >= a else None
        rows = []
        for uid, turn in self.turns.items():
            stages = turn['stages']
            for pid, playback in (turn['playbacks'] or {'': {}}).items():
                all_stages = dict(stages, **playback)
                rows.append(dict(request_id=uid, playback_id=pid,
                    endpoint_ms=duration(stages, 'estimated_speech_end', 'endpoint'),
                    final_asr_wait_ms=duration(stages, 'endpoint', 'transcript_final'),
                    agent_queue_ms=duration(stages, 'agent_queued', 'agent_started'),
                    agent_ms=duration(stages, 'agent_started', 'agent_finished'),
                    tts_first_audio_ms=duration(playback, 'tts_started', 'tts_first_audio'),
                    audio_to_playback_ms=duration(playback, 'tts_first_audio', 'playback_started'),
                    speech_to_playback_ms=duration(all_stages, 'estimated_speech_end', 'playback_started'),
                    terminal_state=playback.get('terminal_state'),
                    playback_boundary='device_stream_started'))
        return rows


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log')
    args = parser.parse_args()
    metrics = SpeechMetrics()
    with open(args.log, encoding='utf-8') as stream:
        for line in stream:
            try:
                metrics.observe(json.loads(line[line.index('{'):]))
            except (ValueError, TypeError):
                continue
    print(json.dumps(metrics.summaries(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
