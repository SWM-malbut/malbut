"""
Which costmaps keep the no-entry Zones on (SWM25-237, 2026-10-08).

Manual driving ignores the Zones so a person can always drive the 말벗 out of
one: only the local costmap's keepout filter is turned off, and the LiDAR
Collision Monitor still guards real obstacles. Autonomous escape keeps both
filters on: KeepoutEscapeFilter checks fresh localization and opens only the
body's bounded exit corridor. Tracking a web escape never disables a filter.
Nav2 serves each filter's SetBool at ``<costmap>/<filter>/toggle_filter``.
"""

import threading
import time


LOCAL_TOGGLE = '/local_costmap/keepout_filter/toggle_filter'
GLOBAL_TOGGLE = '/global_costmap/keepout_filter/toggle_filter'
# Expire web escape bookkeeping if the mission never reports its end.
ESCAPE_LIMIT_S = 60.0
# A restarted costmap starts with its filter on: repeat "off" while it should be off.
RESEND_S = 5.0


class ZoneOverride:
    """Decide which keepout filters should be on; the ROS bridge sends the changes."""

    def __init__(self, clock=time.monotonic):
        """Start with every filter on (Nav2's own default) and nothing sent."""
        self.clock = clock
        self.lock = threading.Lock()
        self.manual = False
        self.escape_until = None
        self.sent = {}

    def set_manual(self, active):
        """Record whether a manual drive mission runs."""
        with self.lock:
            self.manual = bool(active)

    def start_escape(self, limit_s=ESCAPE_LIMIT_S):
        """Track the web escape phase without disabling either filter."""
        with self.lock:
            self.escape_until = self.clock() + limit_s

    def end_escape(self):
        """Finish the web escape phase."""
        with self.lock:
            self.escape_until = None

    def escaping(self):
        """Return whether a web escape is still pending."""
        with self.lock:
            return self._escaping()

    def _escaping(self):
        return self.escape_until is not None and self.clock() < self.escape_until

    def wanted(self):
        """Return each toggle service and whether its filter should be on."""
        with self.lock:
            return {LOCAL_TOGGLE: not self.manual, GLOBAL_TOGGLE: True}

    def due(self):
        """Return (service, enabled) to send now: a change, or a repeat of "off"."""
        wanted = self.wanted()
        now = self.clock()
        with self.lock:
            due = []
            for service, enabled in wanted.items():
                last = self.sent.get(service)
                if last is None:
                    if not enabled:
                        due.append((service, enabled))
                elif last[0] != enabled or (not enabled and now - last[1] >= RESEND_S):
                    due.append((service, enabled))
            return due

    def mark_sent(self, service, enabled):
        """Remember a request handed to Nav2."""
        with self.lock:
            self.sent[service] = (bool(enabled), self.clock())
