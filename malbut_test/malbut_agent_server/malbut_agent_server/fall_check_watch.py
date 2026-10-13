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
# From the suspicion until the question ends the fall check comes before the
# conversation; the drive back does not (the user, 2026-10-13). The person at
# home heard nothing when the robot left a conversation to check a fall.
WINDOW_PHASES = frozenset({'approach', 'look', 'ask', 'wait'})
FALL_CHECK_START = '낙상이 의심돼서 먼저 확인할게요.'
FALL_CHECK_BUSY = '지금 확인 중이에요. 잠시만요.'
FALL_CHECK_HELP = '보호자에게 바로 알릴게요.'
FALL_CHECK_STOPPED = '알겠어요. 확인을 멈추고 보호자에게 알릴게요.'
# Said during the check before the question, for the fall rules: {"kind": "help"}
# alerts the guardian at once; {"kind": "stop"} stops the check like 직접 움직이기,
# asks nothing and tells the guardian to check (the user, 2026-10-13).
VOICE_TOPIC = '/malbut/falls/check/voice'
VOICE_KINDS = ('help', 'stop')
HELP_WORDS = ('도와줘', '도와주세요', '도와달', '살려', '구급차', '119', '도움이필요',
              '못일어나', '일으켜', '다쳤', '아파', '아프')
# "그만/멈춰" ranks above the fall check, like 직접 움직이기 (the user, 2026-10-13).
STOP_WORDS = ('그만', '멈춰', '멈춰줘', '정지', '스톱', '취소', '하지마')


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
            phase = value.get('phase') if isinstance(value.get('phase'), str) else None
            with self._lock:
                self._status = (value['active'], self._clock(), phase)

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

    def in_window(self):
        """Return True from the suspicion until the question ends, by fresh status."""
        with self._lock:
            status = self._status
        return bool(status is not None and status[0] and status[2] in WINDOW_PHASES
                    and self._clock() - status[1] <= FALL_CHECK_STATUS_TIMEOUT_S)

    def move_refusal(self):
        """Return the spoken refusal while a fall check is active, else None."""
        return FALL_CHECK_RESPONSE if self.active() else None


def asks_for_help(text):
    """Return True for a spoken request for help ("도와줘", "살려줘", "아파요", …)."""
    if not isinstance(text, str):
        return False
    normalized = ''.join(text.split()).replace('아파트', '')
    return any(word in normalized for word in HELP_WORDS)


def asks_to_stop(text):
    """Return True for a spoken stop or cancel ("그만", "멈춰", "취소해", …)."""
    if not isinstance(text, str):
        return False
    normalized = ''.join(text.split()).lower()
    return normalized in ('서', 'stop') or any(word in normalized for word in STOP_WORDS)
