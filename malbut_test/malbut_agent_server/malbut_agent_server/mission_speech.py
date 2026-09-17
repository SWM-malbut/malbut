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
    """Suppress repeated progress while preserving real state changes."""

    def __init__(self, speak: Callable[[str], bool]) -> None:
        """Use the Agent's normal text publication boundary."""
        self._speak = speak
        self._last: Dict[str, tuple] = {}
        self._finished = set()

    def handle(self, event: Dict) -> Optional[str]:
        """Announce a confirmed event and return its published text."""
        request_id = event['request_id']
        kind = event.get('kind')
        if request_id in self._finished:
            return None
        text = event_speech(event)
        if text is None:
            return None
        signature = (kind, event.get('state') if kind == 'progress' else None)
        if self._last.get(request_id) == signature:
            return None
        if not self._speak(text):
            return None
        self._last[request_id] = signature
        if kind in _TERMINAL:
            self._finished.add(request_id)
        return text
