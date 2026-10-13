"""Whether a fall check holds the speaker now, so the guardian's talk waits (2026-10-13).

The fall check comes before the guardian's homecam talk from the suspicion until
its question ends (the user): the same phases and three-second freshness as the
conversation follows (malbut_agent_server fall_check_watch).
"""

import json
from time import monotonic

WINDOW_PHASES = frozenset({'approach', 'look', 'ask', 'wait'})
STATUS_TIMEOUT_S = 3.0
MAX_STATUS_BYTES = 4096


class FallCheckWindow:
    """Read ``/malbut/falls/check/status`` and answer ``active()``."""

    def __init__(self, clock=monotonic):
        self._clock = clock
        self._status = None

    def observe(self, data):
        """Keep the latest status; a malformed one is ignored."""
        if not isinstance(data, str) or len(data) > MAX_STATUS_BYTES:
            return
        try:
            value = json.loads(data)
        except ValueError:
            return
        if isinstance(value, dict) and type(value.get('active')) is bool:
            self._status = (value['active'], value.get('phase'), self._clock())

    def active(self):
        """Return True from the suspicion until the question ends, by fresh status."""
        status = self._status
        return bool(status is not None and status[0] and status[1] in WINDOW_PHASES
                    and self._clock() - status[2] <= STATUS_TIMEOUT_S)
