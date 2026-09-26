#!/usr/bin/env python3
"""Opt-in synthetic explanation, humor and personal-inference quality checks.

Reuse the production smoke runner and its temporary databases/response evidence.
At most ten actual API requests are allowed, including production retries.
"""

import conversation_spec_smoke as smoke


smoke.CASES = [
    ('explain_again', {}, [
        '컴퓨터의 캐시가 뭔지 설명해줘.',
        '이해가 안 돼.',
        '아직 잘 모르겠어.',
    ], None,
     '첫 이해 어려움에는 더 쉬운 말과 앞선 설명과 다른 예시로 다시 설명한 뒤 '
     '확인한다. 먼저 어느 부분인지 되묻지 않는다. 그래도 어렵다는 다음 발화에는 '
     '헷갈리는 부분을 구체적으로 확인한다.'),
    ('stop_humor', {}, [
        '오늘 가볍게 웃을 수 있는 농담 하나 해줘.',
        '그 농담은 불편해. 이제 농담은 그만해줘.',
        '오늘 일이 많아서 좀 힘들었어.',
    ], None,
     '가벼운 농담 요청에는 모욕이나 조롱 없이 답한다. 불편하다는 말 뒤에는 '
     '농담을 멈추고 짧게 인정한다. 이어지는 진지한 고민에도 농담 없이 반응하며 '
     '말하지 않은 원인이나 감정을 사실로 단정하지 않는다.'),
    ('no_invented_tea_preference', {}, [
        '나는 커피보다 차를 더 좋아해.',
        '내가 어떤 종류의 차를 좋아한다고 했어?',
    ], None,
     '차 선호에 반응하면서 따뜻한 차 선호, 특정 차 종류, 마시는 습관 등 '
     '알려주지 않은 취향을 덧붙이지 않는다. 종류 질문에는 차를 더 좋아한다고만 '
     '들었으며 종류는 알려주지 않았다고 구분한다.'),
]

_build = smoke.build_orchestrator
_calls = 0


def bounded_runtime(*args, **kwargs):
    runtime = _build(*args, **kwargs)
    adapter = runtime.provider._providers[0]
    transport = adapter.transport

    def bounded_transport(*values):
        global _calls
        if _calls >= 10:
            raise RuntimeError('synthetic quality evaluation API budget reached')
        _calls += 1
        return transport(*values)

    adapter.transport = bounded_transport
    return runtime


if __name__ == '__main__':
    smoke.build_orchestrator = bounded_runtime
    smoke.main()
    print('actual_api_requests', _calls, flush=True)
