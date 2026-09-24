#!/usr/bin/env python3
"""Opt-in sender-perspective regression with independent viewpoint controls.

These three synthetic cases are bounded evidence, not a general perspective
guarantee. The transport permits at most eight actual requests including retries.
"""

from conversation_ambiguity_smoke import smoke


_original = next(case for case in smoke.CASES
                 if case[0] == 'proposal_and_confirmed_plan')
smoke.CASES = [
    (*_original[:4], _original[4] +
     ' 추가 관점 기준: 최종 공지는 사용자 자신이 민수에게 보내는 글로 작성한다. '
     '사용자와 민수를 함께 가리킬 때 우리 둘·함께 만나요 등 발신자 관점을 쓰고, '
     '말벗이 두 사람을 밖에서 설명하는 시점으로 바꾸지 않는다.'),
    ('explicit_third_person_report', {}, [
        '연습용 행사 기록이에요. 민수와 지연은 금요일 오후 4시에 도서관에서 '
        '만나고 참가비는 없어요. 저는 기록 담당자이고 이 모임에는 참석하지 않아요.',
        '이 내용을 제삼자 시점의 두 문장 행사 기록으로 작성해 주세요. '
        '누군가에게 보내는 초대장이 아니라 민수와 지연의 일정을 기록하는 글이에요.',
    ], None,
     '최종 기록은 민수와 지연을 제삼자로 서술하고 사용자나 말벗을 참석자에 '
     '포함하지 않는다. 금요일 오후 4시·도서관·참가비 없음을 유지하며, 우리·나·저 '
     '같은 참여자 일인칭이나 직접 초대하는 발신자 시점으로 바꾸지 않는다.'),
    ('explicit_other_sender', {}, [
        '초대장 작성 연습이에요. 주최자 미나가 태호를 금요일 오후 6시에 '
        '마을회관으로 초대하려고 해요. 간식은 미나가 준비해요. 저는 미나가 '
        '아니고 초안 작성만 돕는 사람이에요.',
        '발신자는 미나로 하고, 미나가 태호에게 직접 말하듯 두 문장 초대장을 '
        '작성해 주세요. 간식 준비도 미나 자신의 일인칭으로 표현해 주세요.',
    ], None,
     '최종 초대장은 지정된 발신자 미나의 일인칭으로 태호에게 말한다. 금요일 '
     '오후 6시·마을회관·미나의 간식 준비를 유지하고, 태호에게 준비를 넘기거나 '
     '초안 작성자인 사용자를 발신자 또는 주최자로 바꾸지 않는다.'),
]

_build = smoke.build_orchestrator
_calls = 0


def bounded_runtime(*args, **kwargs):
    runtime = _build(*args, **kwargs)
    adapter = runtime.provider._providers[0]
    transport = adapter.transport

    def bounded_transport(*values):
        global _calls
        if _calls >= 8:
            raise RuntimeError('synthetic perspective evaluation API budget reached')
        _calls += 1
        return transport(*values)

    adapter.transport = bounded_transport
    return runtime


if __name__ == '__main__':
    smoke.build_orchestrator = bounded_runtime
    smoke.main()
    print('actual_api_requests', _calls, flush=True)
