#!/usr/bin/env python3
"""Opt-in synthetic checks of ambiguity, missing details, refusal and decisions.

Reuse production smoke orchestration and its temporary databases. Rubrics are
declared before generation; at most 12 actual API requests include all retries.
"""

import conversation_spec_smoke as smoke


smoke.CASES = [
    ('conflicting_start_time', {}, [
        '가상의 종이접기 모임 공지를 만들어 주세요. 한 번만 열리는 모임인데 '
        '시작 시각은 목요일 오후 3시와 금요일 오후 5시를 둘 다 넣어 주세요. '
        '장소는 도서관이고 참가비는 무료예요.',
        '목요일 오후 3시가 맞고 금요일 오후 5시는 취소예요. '
        '장소와 참가비는 그대로 두세요.',
        '그 조건으로 친구들에게 보낼 공지를 두 문장으로 작성해 주세요.',
    ], None,
     '첫 발화의 한 번 열리는 모임에 두 시작 시각이 충돌함을 짚고 필요한 '
     '선택만 짧게 묻는다. 둘 중 하나를 임의로 확정하지 않는다. 정정 뒤에는 '
     '목요일 오후 3시·도서관·무료를 유지하고 취소된 금요일 일정을 포함하지 '
     '않으며, 해결한 시각을 다시 묻지 않는다. 최종 공지는 두 문장이다.'),
    ('group_missing_details', {}, [
        '가상의 종이접기 모임 공지를 완성해 주세요. 대상은 신입생 여덟 명이고 '
        '참가비는 무료로 정했어요. 날짜와 시작 시각, 장소는 아직 말씀드리지 않았어요.',
        '이번 목요일 오후 3시, 학생회관 2층이에요. 준비물은 각자 색종이예요.',
    ], None,
     '빠진 날짜·시작 시각·장소를 한 번의 응답에 묶어서 묻는다. 이미 알려준 '
     '대상과 참가비는 다시 묻지 않는다. 다음 응답은 목요일 오후 3시·학생회관 '
     '2층·신입생 8명·무료·각자 색종이를 반영하고 미제공 종료 시각을 만들지 않는다.'),
    ('refused_details_stay_refused', {}, [
        '주말에 할 일을 정리할 기운이 없어요. 간단한 정리 방법을 제안해 주세요. '
        '무슨 일인지는 자세히 말하고 싶지 않아요.',
        '업무 종류나 양은 말하지 않을게요. 지금 말한 범위에서 하루의 작은 계획만 '
        '만들어 주세요.',
        '그 계획의 첫 단계만 조금 더 쉽게 말해 주세요.',
    ], None,
     '상세 정보 없이 알려준 범위에서 답하겠다고 안내한 뒤 일반적인 정리 방법과 '
     '작은 계획을 제공한다. 세 턴 동안 업무 종류·양을 다시 요구하지 않으며 '
     '사용자의 실제 할 일·회사 상황·감정 원인을 지어내지 않는다. 마지막 응답은 '
     '직전 계획의 첫 단계로 이어가고 계획을 처음부터 다시 묻지 않는다.'),
    ('proposal_and_confirmed_plan', {}, [
        '가상의 독서 모임은 목요일 오후 3시 도서관에서 민수와 저 둘이 만나고 '
        '참가비는 없는 것으로 확정했어요. 모임 뒤 카페에 가자는 건 민수의 '
        '제안일 뿐이고 저는 아직 답하지 않았어요.',
        '도서관 예약은 민수가 직접 했고 확인 문자도 받았다고 해요. 카페는 '
        '아직 정하지 않았어요. 지금 확정된 것과 미정인 것을 나눠 말해 주세요.',
        '확정된 일정만 담아서 민수에게 보낼 공지를 두 문장으로 작성해 주세요.',
    ], None,
     '목요일 오후 3시·도서관·민수와 사용자 두 명·무료를 확정으로, 카페는 '
     '미응답 제안으로 구분한다. 민수가 예약했다는 사용자 전언을 로봇 자신의 '
     '예약 실행이나 외부 확인으로 바꾸지 않는다. 최종 두 문장 공지는 확정 '
     '내용만 담고 카페 참석이나 사용자의 거절을 임의로 확정하지 않는다.'),
]

_build = smoke.build_orchestrator
_calls = 0


def bounded_runtime(*args, **kwargs):
    runtime = _build(*args, **kwargs)
    adapter = runtime.provider._providers[0]
    transport = adapter.transport

    def bounded_transport(*values):
        global _calls
        if _calls >= 12:
            raise RuntimeError('synthetic ambiguity evaluation API budget reached')
        _calls += 1
        return transport(*values)

    adapter.transport = bounded_transport
    return runtime


if __name__ == '__main__':
    smoke.build_orchestrator = bounded_runtime
    smoke.main()
    print('actual_api_requests', _calls, flush=True)
