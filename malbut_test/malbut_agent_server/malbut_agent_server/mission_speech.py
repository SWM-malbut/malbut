"""Announce observed results briefly, leaving diagnostics in event logs."""

from contextlib import contextmanager
from typing import Callable, Dict, Optional

from malbut_agent_server.mission_audio_cases import event_audio_id, NOTICE_TEXTS


_PROGRESS = {
    'PENDING': '실행을 기다리고 있어요.',
    'RUNNING': '작업을 진행하고 있어요.',
    'CANCELING': '취소를 처리하고 있어요.',
    'SUSPENDED': '작업이 잠시 중단됐어요.',
}
_TERMINAL = {'rejected', 'succeeded', 'failed', 'canceled', 'unavailable', 'unsupported'}
_ANNOUNCED = _TERMINAL | {'unknown', 'cancel_unknown', 'cancel_rejected'}


def event_speech(event: Dict) -> Optional[str]:
    """Use the observed status; do not read internal errors or infer results."""
    kind = event.get('kind')
    if kind == 'progress':
        return _PROGRESS.get(event.get('state'))
    return NOTICE_TEXTS.get(event_audio_id(event))


class MissionAnnouncer:
    """Announce outcomes and uncertainty without narrating routine progress."""

    def __init__(self, speak: Callable[[str], bool], *, speak_audio=None) -> None:
        """Use the Agent's normal text publication boundary."""
        self._speak = speak
        self._speak_audio = speak_audio
        self._last: Dict[str, str] = {}
        self._finished = set()
        self._direct_request = None
        self._deferred = []

    @contextmanager
    def direct_reply(self, request_id):
        """Coalesce synchronous Manager notices with a published voice reply.

        ManagerClient may report unavailable inside submit(), before the direct
        reply is published. Defer only that request's events until publication
        is known. A failed/suppressed direct reply leaves the notice available.
        Construction and calls belong to the same ROS owner as handle().
        """
        if request_id is None:
            yield lambda _text: None
            return
        if self._direct_request is not None:
            raise RuntimeError('direct mission replies cannot overlap')
        self._direct_request = request_id

        def acknowledge(text):
            # Called only after the direct response really reached publication.
            for event in self._deferred:
                if event.get('kind') in _ANNOUNCED and event_speech(event) == text:
                    self._record(event)

        try:
            yield acknowledge
        finally:
            events, self._deferred = self._deferred, []
            self._direct_request = None
            for event in events:
                self.handle(event)

    def _record(self, event):
        self._last[event['request_id']] = event.get('kind')
        if event.get('kind') in _TERMINAL:
            self._finished.add(event['request_id'])

    def handle(self, event: Dict) -> Optional[str]:
        """Publish final results and actionable problems once observed."""
        request_id = event['request_id']
        kind = event.get('kind')
        if request_id in self._finished or kind not in _ANNOUNCED:
            return None
        if request_id == self._direct_request:
            self._deferred.append(dict(event))
            return None
        notice = None
        if self._speak_audio is not None:
            from malbut_agent_server.mission_audio import notice_for_event
            notice = notice_for_event(event)
        if notice is not None:
            audio_id, text = notice
        else:
            text = event_speech(event)
        if text is None:
            return None
        if self._last.get(request_id) == kind:
            return None
        published = (self._speak_audio(text, audio_id)
                     if notice is not None else self._speak(text))
        if not published:
            return None
        self._record(event)
        return text
