"""Select finite prerecorded notices from actual operation outcomes."""

import json
import re

import yaml

from malbut_agent_server.conversation_progress import DELAY_NOTICE
from malbut_agent_server.mission_audio_cases import (
    COMMON_FAILURES, ENDPOINTS, FAILURES, FIXED_REPLIES, LABELS, PREFIX_FAILURES,
)


CATALOG = {
    'operation.succeeded': '요청하신 작업이 완료됐어요.',
    'operation.failed': '작업을 완료하지 못했어요.',
    'operation.canceled': '작업이 취소됐어요.',
    'operation.unsupported': '현재 지원하지 않아요.',
    'conversation.delay': DELAY_NOTICE,
    **FIXED_REPLIES,
}
OUTCOMES = {
    'succeeded': '요청이 성공 상태로 종료됐어요.',
    'failed': '작업을 완료하지 못했어요. 구체적인 사유는 확인되지 않았어요.',
    'canceled': '요청이 취소 상태로 종료됐어요.',
    'unsupported': '기능은 현재 지원하지 않아요.',
    'rejected': '요청이 거절됐어요. 구체적인 사유는 전달받지 못했어요.',
    'unavailable': '요청을 보내지 못했어요. 실행 관리자에 연결할 수 없어요.',
    'unknown': '실행 상태를 확인하지 못했어요. 요청을 다시 보내지는 않았어요.',
    'cancel_unknown': '취소 접수 여부를 확인하지 못했어요. 종료 여부도 확인되지 않았어요.',
    'cancel_rejected': '취소 요청이 거절됐어요. 종료 여부는 확인되지 않았어요.',
    'accepted': '요청을 접수했어요.',
    'cancel_requested': '취소를 요청했어요. 종료 여부를 확인할게요.',
    'cancel_accepted': '취소 요청을 접수했어요. 종료 여부를 확인할게요.',
}
for _capability, _label in LABELS.items():
    for _kind, _text in OUTCOMES.items():
        CATALOG[f'{_capability}.{_kind}'] = f'{_label} {_text}'
    for _suffix, _, _text in (*COMMON_FAILURES, *FAILURES.get(_capability, ()),
                              *PREFIX_FAILURES.get(_capability, ())):
        CATALOG[f'{_capability}.failed.{_suffix}'] = f'{_label}: {_text}'
    CATALOG[f'{_capability}.failed.manager_action_unavailable'] = (
        f'{_label}: 기능 실행 서버에 연결하지 못했어요.')
CATALOG['relocalize.failed.match_insufficient'] = '위치 보정: 라이다 측정과 지도가 충분히 일치하지 않아 위치를 찾지 못했어요.'
CATALOG['set_weather_location.succeeded'] = '날씨 조회 지역을 저장했어요.'
TEXT_IDS = {text: audio_id for audio_id, text in CATALOG.items()}


def notice_for_event(event):
    """Prefer Manager reasons, then that capability's known result fields."""
    capability, kind = event.get('capability_id'), event.get('kind')
    if capability not in LABELS:
        kind = 'failed' if kind in {'rejected', 'unavailable'} else kind
        audio_id = 'operation.' + str(kind)
        return _notice(audio_id) if audio_id in CATALOG else None
    if kind not in OUTCOMES:
        return None
    if kind == 'failed':
        reason = event.get('reason')
        reason = reason if isinstance(reason, str) else ''
        payload = {}
        raw = event.get('result_yaml', '')
        if isinstance(raw, str) and 0 < len(raw) <= 16384:
            try:
                parsed = yaml.safe_load(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except (yaml.YAMLError, ValueError, RecursionError):
                pass
        if reason == f'Action server {ENDPOINTS[capability]} is unavailable':
            return _notice(f'{capability}.failed.manager_action_unavailable')
        # Never borrow a child cause when Manager supplied a different reason.
        candidates = [reason] if reason else [payload.get('error_code'), payload.get('message')]
        for value in candidates:
            if not isinstance(value, str) or not value:
                continue
            for suffix, expected, _ in (*COMMON_FAILURES, *FAILURES.get(capability, ())):
                if value == expected:
                    return _notice(f'{capability}.failed.{suffix}')
            for suffix, prefix, _ in PREFIX_FAILURES.get(capability, ()):
                if value.startswith(prefix):
                    return _notice(f'{capability}.failed.{suffix}')
            if capability == 'relocalize' and re.fullmatch(
                    r'(global search requested|no saved pose for this map|'
                    r'saved pose matched only [0-9]+% of the scan); '
                    r'global search matched only [0-9]+% of the scan', value):
                return _notice('relocalize.failed.match_insufficient')
    return _notice(f'{capability}.{kind}')


def _notice(audio_id):
    return audio_id, CATALOG[audio_id]


# Direct command responses still use the established transport vocabulary.
# Map those exact phrases to the same recordings as asynchronous notices.
from malbut_agent_server.mission_speech import event_speech  # noqa: E402
for _capability in ('follow_person', 'navigate_to_pose', 'patrol'):
    for _kind in OUTCOMES:
        _legacy = event_speech({'capability_id': _capability, 'kind': _kind})
        if _legacy:
            TEXT_IDS.setdefault(_legacy, f'{_capability}.{_kind}')


if __name__ == '__main__':
    print(json.dumps(CATALOG, ensure_ascii=False, indent=2, sort_keys=True))
