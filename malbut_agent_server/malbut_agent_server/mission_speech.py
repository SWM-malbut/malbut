"""Describe transport events without inferring physical state."""

from typing import Callable, Dict, Optional


_LABELS = {
    'follow_person': '사람 따라가기',
    'navigate_to_pose': '목적지 이동',
    'patrol': '순찰',
}
_PROGRESS = {
    'PENDING': 'Manager에서 실행을 기다리고 있어요.',
    'RUNNING': 'Manager가 실행 중인 상태로 알려왔어요.',
    'CANCELING': 'Manager가 취소를 처리하고 있어요.',
    'SUSPENDED': 'Manager에서 일시 중단한 상태예요.',
}
_MESSAGES = {
    'accepted': 'Manager가 요청을 접수했어요.',
    'rejected': 'Manager가 요청 접수를 거절했어요.',
    'succeeded': 'Manager가 실행 요청을 성공 상태로 종료했다고 알려왔어요.',
    'failed': 'Manager가 실행 요청을 실패 상태로 종료했다고 알려왔어요.',
    'canceled': '실행 요청이 취소 상태로 종료됐어요.',
    'unknown': '실행 상태를 확인할 수 없어요. 시작 요청을 다시 보내지는 않았어요.',
    'unavailable': 'Manager에 연결할 수 없어 실행 요청을 보내지 못했어요.',
    'cancel_requested': '취소를 요청했어요. 종료 여부를 확인할게요.',
    'cancel_accepted': 'Manager가 취소 요청을 접수했어요. 아직 종료 확인 전이에요.',
    'cancel_rejected': '취소가 접수되지 않았어요. 실행이 종료된 것으로 판단하지 않을게요.',
    'cancel_unknown': '취소 접수 여부를 확인할 수 없어요. 종료된 것으로 판단하지 않을게요.',
}
_TERMINAL = {'rejected', 'succeeded', 'failed', 'canceled', 'unavailable'}
_ANNOUNCED = _TERMINAL | {'unknown', 'cancel_unknown', 'cancel_rejected'}


def event_speech(event: Dict) -> Optional[str]:
    """Describe only the Manager's reported status and supplied reason."""
    kind = event.get('kind')
    if kind == 'progress':
        message = _PROGRESS.get(event.get('state'))
    else:
        message = _MESSAGES.get(kind)
    if message is None:
        return None
    label = _LABELS.get(event.get('capability_id'), '기능 실행')
    text = f'{label} 요청: {message}'
    reason = event.get('reason')
    if (kind == 'failed'
            and isinstance(reason, str) and reason.strip()):
        text += ' 전달받은 사유는 다음과 같아요. ' + reason
    return text


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
