"""Track whether the previous spoken answer was heard to its end."""
from collections import OrderedDict


class SpeechDelivery:
    def __init__(self):
        self.owned = OrderedDict()
        self.current = None
        self.interrupted = False

    def requested(self, playback_id, *, interim):
        if interim:
            return
        self.owned[playback_id] = None
        while len(self.owned) > 128:
            self.owned.popitem(last=False)

    def observe(self, playback_id, state):
        if playback_id not in self.owned:
            return
        previous = self.owned[playback_id]
        if previous in ('finished', 'stopped', 'failed'):
            return
        self.owned[playback_id] = state
        if state == 'playing':
            self.current = playback_id
            self.interrupted = False
        if self.current == playback_id:
            if state in ('stopped', 'failed'):
                self.interrupted = True
            elif state == 'finished':
                self.interrupted = False

    def input_started(self):
        return self.interrupted or self.owned.get(self.current) in ('playing', 'paused')
