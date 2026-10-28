"""Experimental candidate ducking; strict AEC alone still authorizes STOP."""
from malbut_stt.silero_stream import SileroStreamVad


class ProbeDuck:
    def __init__(self):
        self.vad = SileroStreamVad()
        self.pid = None
        self.until = self.cooldown = self.last = self.last_refresh = 0.
        self.speech_s = 0.

    def reset(self):
        self.pid = None
        self.until = self.cooldown = self.last_refresh = self.speech_s = 0.
        self.vad.reset()

    def feed(self, pid, pcm, now):
        if pid != self.pid or now-self.last > .2:
            self.reset()
            self.pid = pid
        self.last = now
        speech = self.vad.is_speech(bytes(pcm), 16000)
        self.speech_s = self.speech_s + len(pcm)/32000 if speech else 0.
        started = False
        if now >= self.until and now >= self.cooldown and self.speech_s >= .16:
            self.until = now + 1.2
            self.cooldown = self.until + 2.
            self.speech_s = 0.
            started = True
        return now < self.until, started


def install(cls):
    original_feed = cls._feed
    original_update = cls._update_ducking

    def feed(self, pcm, busy_at_capture):
        eligible = (self._can_barge_into_reply() and not busy_at_capture
                    and not self._busy and self._pending is None)
        probe = getattr(self, '_experimental_probe', None)
        self._experimental_duck = False
        if eligible:
            if probe is None:
                probe = self._experimental_probe = ProbeDuck()
            active, started = probe.feed(self.session.playback_id, pcm, self.clock())
            self._experimental_duck = active
            if started:
                self.report('barge_probe_duck:1.2s')
        elif probe is not None and probe.pid is not None:
            probe.reset()
        return original_feed(self, pcm, busy_at_capture)

    def update(self, candidate=False):
        probe = getattr(self, '_experimental_probe', None)
        active = (self._can_barge_into_reply()
                  and getattr(self, '_experimental_duck', False)
                  and probe is not None and probe.pid == self.session.playback_id)
        previous = self._ducked_playback
        original_update(self, candidate or active)
        if self._ducked_playback is not None and probe is not None:
            now = self.clock()
            if previous is None:
                probe.last_refresh = now
            elif now - probe.last_refresh >= .1:
                # Player leases ducking for only 300 ms; refresh until released.
                self.publish_control(self._ducked_playback, 'duck')
                probe.last_refresh = now

    cls._feed = feed
    cls._update_ducking = update
