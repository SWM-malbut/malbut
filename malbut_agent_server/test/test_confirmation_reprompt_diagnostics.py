"""Explain a normal clarification without retaining speech or model input."""

from collections import deque
import json
from types import SimpleNamespace

import pytest

from malbut_agent_server.ros_situation import SituationActionServer
from malbut_agent_server.situation_dialogue import (
    SituationDialogue, SituationInterpretation,
)


def dialogue(*interpretations):
    responses = deque(interpretations)
    return SituationDialogue(SimpleNamespace(evaluate=lambda _context: responses.popleft()))


def speaker(engine):
    records, messages = [], []
    server = SituationActionServer.__new__(SituationActionServer)
    server._closed = False
    server._prerecorded_audio = False
    server._active_goal = SimpleNamespace(
        goal_id=SimpleNamespace(uuid=bytes(range(16))),
        request=SimpleNamespace(request_id='request-1'),
    )
    server._session = SimpleNamespace(engine=engine, phase='speaking')
    server.node = SimpleNamespace(context=SimpleNamespace(ok=lambda: True),
                                get_logger=lambda: SimpleNamespace(info=records.append))
    class Speech(SimpleNamespace):
        CONFIRMATION = 2
    server._speech_type = Speech
    server._speech = SimpleNamespace(publish=messages.append)
    return server, records, messages


@pytest.mark.parametrize('assessment,next_question,stage,repeated', [
    ('unknown', '첫 질문 원문', 'situation', True),
    ('confirmed_incident', '도움 확인 질문 원문', 'help', False),
])
def test_reprompt_explains_unresolved_field_without_user_text(
        assessment, next_question, stage, repeated):
    engine = dialogue(SituationInterpretation('unknown', None, '첫 질문 원문'),
                      SituationInterpretation(assessment, None, next_question))
    first = engine.start('request-1', 'fall', '비공개 상황 요약')
    server, records, messages = speaker(engine)
    assert server.speak(first.text, 'first-playback')
    assert records == []

    turn = engine.answer('비공개 사용자의 발화')
    assert turn.result is None
    assert server.speak(turn.text, 'followup-playback')
    assert len(records) == 1
    prefix, payload = records[0].split(' ', 1)
    assert prefix == 'confirmation_reprompt'
    assert json.loads(payload) == dict(
        request_id='request-1', goal_id=bytes(range(16)).hex(),
        playback_id='followup-playback', situation_assessment=assessment,
        help_needed=None, stage=stage, answer_count=1, repeated_question=repeated,
    )
    for private in ('비공개', '첫 질문 원문', '도움 확인 질문 원문'):
        assert private not in records[0]
    assert messages[-1].text == next_question


@pytest.mark.parametrize('answer', ['도와줘', '도움 필요 없어요'])
def test_completed_answer_is_not_reported_as_another_question(answer):
    engine = dialogue(SituationInterpretation('unknown', None, '첫 질문 원문'))
    engine.start('request-1', 'fall', '비공개 상황 요약')
    turn = engine.answer(answer)
    server, records, _ = speaker(engine)
    server._session.phase = 'closing'
    assert turn.result is not None
    assert server.speak(turn.text, 'closing-playback')
    assert records == []


def test_reprompt_count_advances_without_recording_previous_words():
    engine = dialogue(*[SituationInterpretation('unknown', None, '동일 질문')] * 3)
    engine.start('request-1', 'fall', '비공개 상황 요약')
    server, records, _ = speaker(engine)
    for number in (1, 2):
        turn = engine.answer('비공개 발화 ' + str(number))
        assert turn.result is None
        server.speak(turn.text, 'playback-' + str(number))
    assert [json.loads(line.split(' ', 1)[1])['answer_count'] for line in records] == [1, 2]
    assert all(json.loads(line.split(' ', 1)[1])['repeated_question'] for line in records)
    assert all('비공개' not in line and '동일 질문' not in line for line in records)


def test_provider_failure_keeps_failure_path_without_a_normal_reprompt_record():
    from test_situation_session import Harness

    h = Harness()
    h.engine = dialogue(SituationInterpretation('unknown', None, '첫 질문 원문'), object())
    server, records, _ = speaker(h.engine)
    server._session = h.session
    h.speak = server.speak
    h.begin_question()
    h.played()
    assert h.session.transcript(h.session.session_id, 'utterance-1', '비공개 발화')
    h.complete_work()
    assert h.finishes == [('aborted', None)]
    assert h.session.failure['reason'] == 'model_answer'
    assert h.session.failure['exception_type'] == 'ProviderError'
    assert records == []
