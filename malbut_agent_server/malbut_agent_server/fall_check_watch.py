"""Whether a fall check runs now, so that a voice move is refused (2026-10-12).

Same rule as the web panel's move buttons (``malbut_bringup`` drive_mode
``fall_check_active``): the fall rules' fresh status, which also covers the gaps
between missions, or a fall mission held by Manager. A status older than three
seconds counts as ended, as on the web panel.
"""

import json
from threading import Lock
import time

FALL_CHECK_STATUS_TOPIC = '/malbut/falls/check/status'
FALL_MISSIONS = ('fall_confirmation', 'fall_approach')
FALL_CHECK_STATUS_TIMEOUT_S = 3.0
# The app's notice for its locked move buttons, said instead of "네, …시작하겠습니다".
FALL_CHECK_RESPONSE = '낙상 확인 중이라 잠시 이동할 수 없어요.'
MAX_STATUS_BYTES = 4096


class FallCheckWatch:
    """Keep the fall rules' status and Manager's fall missions; answer ``active()``."""

    def __init__(self, clock=time.monotonic):
        """Start with no fall check known."""
        self._clock = clock
        self._lock = Lock()
        self._status = None
        self._manager_holds_fall = False

    def observe_status(self, data):
        """Read ``/malbut/falls/check/status``; a malformed one is ignored."""
        if not isinstance(data, str) or len(data) > MAX_STATUS_BYTES:
            return
        try:
            value = json.loads(data)
        except ValueError:
            return
        if isinstance(value, dict) and type(value.get('active')) is bool:
            with self._lock:
                self._status = (value['active'], self._clock())

    def observe_missions(self, capability_ids):
        """Note whether Manager runs, waits or keeps a fall mission."""
        held = any(capability in FALL_MISSIONS for capability in capability_ids)
        with self._lock:
            self._manager_holds_fall = held

    def active(self):
        """Return True while a fall check runs or waits."""
        with self._lock:
            status, held = self._status, self._manager_holds_fall
        fresh = (status is not None and status[0]
                 and self._clock() - status[1] <= FALL_CHECK_STATUS_TIMEOUT_S)
        return bool(fresh or held)

    def move_refusal(self):
        """Return the spoken refusal while a fall check is active, else None."""
        return FALL_CHECK_RESPONSE if self.active() else None
