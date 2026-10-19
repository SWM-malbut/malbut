"""Local log of what the one fall check saw and decided (SWM25-261, 2026-10-18).

Every message the VLM sends to the Agent's fall rules (sightings, Cloud answers, and
since SWM25-267 every pose frame) and every decision it gets back, one JSON line each:
``{"logged_at": wall time, "dir": to_rules or from_rules, "data": the message}``. The
message is nested (10-19): spread beside the wall time, a pose frame's own ``at`` clashed
with it and every frame was lost (lines before that are flat, with ``at`` = wall time).
Numbers only, no pixels: a sighting's or frame's screen box, track, map place and torso
height above the floor (both measured with depth, which is not kept anywhere
else) and where the robot was. Pose frames come five a second, about 100-250 bytes each.
With it, a change of the rules can be replayed on a real day; the recording has no
depth (10-17 21:31~22:22: 사람2 hidden in 사람1's incident could not be replayed). Kept
seven days like the recording; a day file stops growing at its size cap. Never raises:
a full disk loses log lines, never a fall check.
"""

import json
import os
from pathlib import Path
import time

KEEP_DAYS = 7
MAX_DAY_BYTES = 200 * 1024 * 1024


class FallFlowLog:
    def __init__(self, directory, *, keep_days=KEEP_DAYS, max_day_bytes=MAX_DAY_BYTES,
                 wall_clock=time.time):
        self.directory = Path(directory)
        self.keep_days, self.max_day_bytes = keep_days, max_day_bytes
        self._wall_clock = wall_clock
        self._day = None
        self.failures = self.dropped = 0

    def write(self, direction, data):
        """``direction``: to_rules (VLM to the Agent) or from_rules (the Agent to the VLM)."""
        try:
            now = self._wall_clock()
            day = time.strftime('%Y-%m-%d', time.localtime(now))
            if day != self._day:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._day = day
                self._expire(now)
            path = self.directory / f'{day}.jsonl'
            if path.exists() and path.stat().st_size >= self.max_day_bytes:
                self.dropped += 1
                return
            line = json.dumps(dict(logged_at=round(now, 3), dir=direction, data=data),
                              ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(line + '\n')
        except (OSError, TypeError, ValueError):
            self.failures += 1

    def _expire(self, now):
        oldest = time.strftime('%Y-%m-%d', time.localtime(now - self.keep_days * 86400))
        for path in self.directory.glob('*.jsonl'):
            if path.stem < oldest:
                try:
                    os.remove(path)
                except OSError:
                    self.failures += 1
