"""Short notices; detailed transport reasons remain in their event logs."""


NOTICE_TEXTS = {
    'operation.succeeded': '요청하신 작업이 완료됐어요.',
    'operation.failed': '작업을 완료하지 못했어요.',
    'operation.canceled': '작업이 취소됐어요.',
    'operation.unsupported': '현재 지원하지 않아요.',
    'operation.unavailable': '지금은 실행할 수 없어요.',
    'operation.unknown': '실행 상태를 확인하지 못했어요.',
    'operation.submitted': '요청을 접수했어요.',
    'cancel.requested': '취소를 요청했어요.',
    'cancel.rejected': '취소 요청을 처리하지 못했어요.',
    'cancel.unknown': '취소 여부를 확인하지 못했어요.',
    'cancel.none': '진행 중인 작업이 없어요.',
    'navigation.map_required': '저장된 지도를 먼저 선택해 주세요.',
    'navigation.target_missing': '이동할 목적지를 다시 알려주세요.',
    'navigation.changed': '지도가 바뀌었어요. 다시 요청해 주세요.',
    'weather.location_not_found': '지역 이름을 다시 알려주세요.',
    'weather.location_required': '날씨를 확인할 지역을 알려주세요.',
    'set_weather_location.succeeded': '날씨 조회 지역을 저장했어요.',
}

EVENT_AUDIO_IDS = {
    'succeeded': 'operation.succeeded',
    'failed': 'operation.failed',
    'rejected': 'operation.failed',
    'canceled': 'operation.canceled',
    'unsupported': 'operation.unsupported',
    'unavailable': 'operation.unavailable',
    'unknown': 'operation.unknown',
    'accepted': 'operation.submitted',
    'cancel_requested': 'cancel.requested',
    'cancel_accepted': 'cancel.requested',
    'cancel_rejected': 'cancel.rejected',
    'cancel_unknown': 'cancel.unknown',
}
