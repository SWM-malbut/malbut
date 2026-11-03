"""One silent preparation slot, owned by the existing runtime worker.

All state methods are called under SpeechRuntime's condition. Native work runs
outside that lock, on the same thread as ordinary synthesis. No player or
playback status is created here.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Event


MAX_PREPARED_BYTES = 2 * 1024 * 1024
PREPARATION_TTL_S = 8.0


def preparation_key(request_id, generation_id, prepared_id):
    values = (request_id, generation_id, prepared_id)
    return values if all(isinstance(v, str) and v.strip() and len(v) <= 256
                         for v in values) else None


@dataclass
class Preparation:
    key: tuple
    text: str
    deadline: float
    cancel: Event = field(default_factory=Event)
    state: str = 'queued'
    audio: object = None


class PrefetchSlot:
    def __init__(self, clock):
        self.clock = clock
        self.job = None
        self.retired = OrderedDict()

    def _retire(self, key):
        self.retired[key] = None
        self.retired.move_to_end(key)
        while len(self.retired) > 256:
            self.retired.popitem(last=False)

    def cancel(self, key=None):
        if key is not None:
            self._retire(key)  # CANCEL may arrive before its PREPARE.
        job = self.job
        if job is not None and (key is None or job.key == key):
            job.cancel.set()
            job.audio = None
            self._retire(job.key)
            if job.state != 'running':
                self.job = None

    def expire(self):
        if self.job is not None and self.clock() >= self.job.deadline:
            self.cancel(self.job.key)

    def request(self, key, text):
        self.expire()
        if key in self.retired:
            return False
        if self.job is not None:
            if self.job.key == key:
                return self.job.text == text and not self.job.cancel.is_set()
            self.cancel()
            if self.job is not None:  # Never queue a second native job.
                return False
        self.job = Preparation(key, text, self.clock() + PREPARATION_TTL_S)
        return True

    def start(self):
        self.expire()
        if self.job is not None and self.job.state == 'queued':
            self.job.state = 'running'
            return self.job
        return None

    def finish(self, job, audio):
        if self.job is not job:
            return
        if audio is None or job.cancel.is_set() or self.clock() >= job.deadline:
            self._retire(job.key)
            self.job = None
        else:
            job.audio = audio
            job.state = 'ready'

    def take(self, key):
        self.expire()
        job = self.job
        if job is None or job.state != 'ready' or job.key != key:
            return None
        self.job = None
        self._retire(job.key)
        return job.audio

    def wait_timeout(self):
        if self.job is not None and self.job.state == 'ready':
            return max(0, self.job.deadline - self.clock())
        return None
