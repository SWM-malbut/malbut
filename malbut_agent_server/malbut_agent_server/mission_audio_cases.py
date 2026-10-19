"""Short notices; detailed transport reasons remain in their event logs."""

import yaml


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
    'relocalize.low_match': '지도와 주변 환경이 충분히 일치하지 않아 현재 위치를 확인하지 못했어요.',
    'relocalize.timeout': '제한 시간 안에 현재 위치를 찾지 못했어요. 지도와 주변 환경을 확인해 주세요.',
    'relocalize.failed': '현재 위치를 확인하지 못했어요. 지도와 센서 상태를 확인해 주세요.',
    'conversation.input_unrecognized': '잘 알아듣지 못했어요. 다시 제이크라고 불러 주세요.',
    'conversation.input_too_long': '한 번에 처리할 수 있는 16000자를 넘었어요. 나누어 말씀해 주세요.',
    'conversation.receipt_failed': '대화를 접수하지 못했어요. 다시 말씀해 주세요.',
    'conversation.startup_failed': '대화 처리를 준비하지 못했어요. 실행 설정을 확인해 주세요.',
    'conversation.busy': '앞선 대화를 처리하고 있어요. 잠시 뒤 다시 말씀해 주세요.',
    'conversation.processing_failed': '대화를 처리하지 못했어요. 다시 말씀해 주세요.',
    'conversation.answer_failed': '답변을 만들지 못했어요. 다시 말씀해 주세요.',
    'conversation.session_unavailable': '대화 세션을 사용할 수 없어요. Agent 대화 모드를 다시 시작해 주세요.',
    'conversation.memory_changed': '기억 정보가 변경되어 이전 답변을 전달하지 않았어요. 다시 말씀해 주세요.',
    'conversation.context_limit': (
        '대화 내용이 한 번에 읽을 수 있는 분량을 넘었어요. '
        '내용을 임의로 빼지는 않았어요. 잠시 후 다시 시도하거나 '
        '이번 요청을 나눠서 말해 주세요.'
    ),
    'operation.conversation_changed': '대화가 변경되어 이전 실행 요청을 보내지 않았어요.',
    'operation.expired': '요청의 유효 시간이 지나 실행하지 않았어요. 다시 말씀해 주세요.',
    'operation.invalid_arguments': '실행 요청의 인자가 올바르지 않아요.',
    'operation.invalid_expiry': '실행 요청의 유효 시간이 올바르지 않아요.',
    'operation.request_unknown': '실행 요청의 처리 여부를 확인하지 못했어요. 자동으로 다시 보내지 않을게요.',
    'operation.voice_unavailable': '이 음성 실행 기능이 연결되지 않았어요.',
    'weather.unavailable': '날씨 조회 기능을 사용할 수 없습니다.',
    'weather.invalid_arguments': '날씨 조회 인자가 올바르지 않습니다.',
    'weather.answer_failed': '날씨 조회 결과로 답변을 만들지 못했어요.',
    'homecam.unavailable': '홈캠 조회가 연결되지 않았어요.',
    'homecam.invalid_arguments': '홈캠 조회 인자가 올바르지 않아요.',
    'homecam.answer_failed': '홈캠 조회 결과로 답변을 만들지 못했어요.',
    'navigation.confirmation_expired': '목적지 확인 시간이 지났어요. 다시 요청해 주세요.',
    'navigation.confirmation_changed': '확인 중 지도나 목적지가 변경되었거나 시간이 지났어요. 다시 요청해 주세요.',
    'homecam.permission_required': '웹에서 소유자의 음성 홈캠 조회 허용이 필요해요.',
    'situation.no_response': '답변을 확인하지 못했어요. 도움이 필요한 상황으로 판단할게요.',
    'situation.unclear': '상황을 충분히 확인하지 못했어요. 도움이 필요한 상황으로 판단할게요.',
    'situation.help_needed': '도움이 필요하신 것으로 확인했어요.',
    'situation.help_not_needed': '알겠어요. 도움이 필요하지 않으신 것으로 확인했어요.',
    'fall_check.start': '낙상이 의심돼서 먼저 확인할게요.',
    'fall_check.busy': '지금 확인 중이에요. 잠시만요.',
    'fall_check.help': '보호자에게 바로 알릴게요.',
    'fall_check.stopped': '알겠어요. 확인을 멈추고 보호자에게 알릴게요.',
    'fall_check.movement_blocked': '낙상 확인 중이라 잠시 이동할 수 없어요.',
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


def event_audio_id(event):
    """Select only documented result codes, never speak arbitrary diagnostics."""
    kind = event.get('kind')
    capability = event.get('capability_id')
    if capability == 'set_weather_location' and kind == 'succeeded':
        return 'set_weather_location.succeeded'
    if capability == 'relocalize' and kind == 'failed':
        payload = event.get('result_yaml')
        if isinstance(payload, str) and len(payload) <= 16384:
            try:
                result = yaml.safe_load(payload)
            except (yaml.YAMLError, ValueError, RecursionError):
                result = None
            if isinstance(result, dict) and result.get('success') is False:
                reason = result.get('failure_reason')
                if reason in ('low_match', 'timeout'):
                    return 'relocalize.' + reason
        return 'relocalize.failed'
    return EVENT_AUDIO_IDS.get(kind)
