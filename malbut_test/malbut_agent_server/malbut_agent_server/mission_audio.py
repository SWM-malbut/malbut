"""Reuse brief outcomes and a single start announcement for each function."""

import json

from malbut_agent_server.conversation_progress import (
    DELAY_NOTICE, MODEL_RETRY_NOTICE,
    SERVICE_UNAVAILABLE_NOTICE, WEATHER_RETRY_NOTICE,
)
from malbut_agent_server.function_speech import FUNCTION_STARTS
from malbut_agent_server.mission_audio_cases import EVENT_AUDIO_IDS, NOTICE_TEXTS


CATALOG = {
    **NOTICE_TEXTS,
    'conversation.delay': DELAY_NOTICE,
    'conversation.model_retry': MODEL_RETRY_NOTICE,
    'conversation.weather_retry': WEATHER_RETRY_NOTICE,
    'conversation.unavailable': SERVICE_UNAVAILABLE_NOTICE,
    **{f'function.{tool}.starting': text for tool, text in FUNCTION_STARTS.items()},
}
TEXT_IDS = {text: audio_id for audio_id, text in CATALOG.items()}
# Existing detailed command replies use the same concise recording at publication.
_REPLY_AUDIO_IDS = {
    '목적지 이동 설정을 확인할 수 없어 이동하지 않았어요.': 'operation.unavailable',
    '사용 중인 저장 지도를 확인할 수 없어 이동하지 않았어요.': 'navigation.map_required',
    '지도나 목적지 설정이 변경되어 이동하지 않았어요. 다시 말씀해 주세요.': 'navigation.changed',
    '저장된 지도에서 그 목적지를 확인하지 못했어요. 등록된 목적지 이름을 포함해 이동 요청을 다시 말씀해 주세요.': 'navigation.target_missing',
    '실행 요청의 상태를 확인할 수 없어요. 시작 요청을 다시 보내지는 않았어요.': 'operation.unknown',
    '요청 조건을 확인할 수 없어 요청을 보내지 않았어요.': 'operation.unavailable',
    '실행 요청을 보냈어요. 접수 결과를 확인할게요.': 'operation.submitted',
    '음성으로 요청한 실행 중인 동작이 없어요.': 'cancel.none',
    '음성으로 요청한 동작의 취소를 요청했어요. 종료 여부를 확인할게요.': 'cancel.requested',
    '음성으로 요청한 동작의 취소를 요청했지만 접수 여부를 확인하지 못했어요. 종료된 것으로 판단하지 않을게요.': 'cancel.unknown',
    '전면 작업의 취소를 요청했어요. 종료 여부를 확인할게요.': 'cancel.requested',
    '현재 실행 중이거나 대기 중인 전면 작업이 없어요.': 'cancel.none',
    '전면 작업의 취소를 요청했지만 접수 여부를 확인하지 못했어요. 종료된 것으로 판단하지 않을게요.': 'cancel.unknown',
    '지금 날씨 조회 기능을 사용할 수 없어요.': 'operation.unavailable',
    '홈캠 정보를 조회하지 못했어요. 잠시 후 다시 요청해 주세요.': 'operation.failed',
    '지역을 찾지 못했어요. 시·구·동을 더 자세히 알려주세요.': 'weather.location_not_found',
    '날씨 조회 위치를 저장하지 못했어요. 잠시 후 다시 알려주세요.': 'operation.failed',
    '날씨를 확인할 지역을 먼저 알려주세요.': 'weather.location_required',
}

TEXT_IDS.update(_REPLY_AUDIO_IDS)


def notice_for_event(event):
    """Report observed outcomes without narrating opaque implementation errors."""
    audio_id = EVENT_AUDIO_IDS.get(event.get('kind'))
    if audio_id is None:
        return None
    if (event.get('capability_id') == 'set_weather_location'
            and event.get('kind') == 'succeeded'):
        audio_id = 'set_weather_location.succeeded'
    return audio_id, CATALOG[audio_id]


if __name__ == '__main__':
    print(json.dumps(CATALOG, ensure_ascii=False, indent=2, sort_keys=True))
