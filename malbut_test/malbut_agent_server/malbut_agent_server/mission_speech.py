"""Announce observed results briefly, leaving diagnostics in event logs."""

from typing import Callable, Dict, Optional

from malbut_agent_server.mission_audio_cases import EVENT_AUDIO_IDS, NOTICE_TEXTS


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
    if kind == 'succeeded' and event.get('capability_id') == 'set_weather_location':
        return NOTICE_TEXTS['set_weather_location.succeeded']
    return NOTICE_TEXTS.get(EVENT_AUDIO_IDS.get(kind))


class MissionAnnouncer:
    """Announce outcomes and uncertainty without narrating routine progress."""

    def __init__(self, speak: Callable[[str], bool], *, speak_audio=None) -> None:
        """Use the Agent's normal text publication boundary."""
        self._speak = speak
        self._speak_audio = speak_audio
        self._last: Dict[str, str] = {}
        self._finished = set()

    def handle(self, event: Dict) -> Optional[str]:
        """Publish final results and actionable problems once observed."""
        request_id = event['request_id']
        kind = event.get('kind')
        if request_id in self._finished or kind not in _ANNOUNCED:
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
        self._last[request_id] = kind
        if kind in _TERMINAL:
            self._finished.add(request_id)
        return text
