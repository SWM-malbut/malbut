"""The Cloud looks of the unified fall check (2026-10-17): the shared criteria, the
posture with the fall moments, and the arrival and departure comparisons. Wire
format only; no credentials or remote inference."""

import json

import pytest

from malbut_agent_server.adapters.outbound.ollama_cloud_fall import (
    arrival_prompt, build_arrival_check_payload, build_departure_check_payload,
    build_posture_check_payload, departure_prompt, parse_arrival_check, parse_departure_check,
    parse_posture_check, posture_prompt,
)
from malbut_agent_server.domain.fall_monitoring import (
    ARRIVAL_VERDICTS, DEPARTURE_VERDICTS, EARLIER_VERDICTS, POSTURE_VERDICTS,
    ArrivalCheckRequest, DepartureCheckRequest, FrameWindow, PostureCheckRequest, RgbFrame,
)
from malbut_agent_server.fall_contract import fall_contract
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError
from test_ollama_cloud_fall import jpeg, response


def frames(start, count):
    return FrameWindow(tuple(RgbFrame(start + i * 0.4, jpeg()) for i in range(count)),
                       start, start + count * 0.4, False)


def sent(body):
    message = json.loads(body)['messages']
    return message[0]['content'], message[1]


def test_the_answer_values_are_the_contract_values():
    answers = fall_contract()['cloudAnswers']
    assert list(POSTURE_VERDICTS) == answers['posture']
    assert list(EARLIER_VERDICTS) == answers['earlier']
    assert list(ARRIVAL_VERDICTS) == answers['arrival']
    assert list(DEPARTURE_VERDICTS) == answers['departure']


def test_every_look_carries_the_shared_criteria():
    criteria = fall_contract()['cloudCriteria']
    for prompt in (posture_prompt(), posture_prompt(earlier=True), arrival_prompt(),
                   departure_prompt()):
        assert criteria in prompt


def test_fall_moments_go_first_and_the_reply_must_say_how_the_person_got_down():
    request = PostureCheckRequest('p1', frames(30.0, 3), (frames(10.0, 12),))
    system, user = sent(build_posture_check_payload(request, model='gemma'))
    assert system == posture_prompt(earlier=True)
    assert 'earlier (fall|self|unknown)' in system
    assert len(user['images']) == 15
    assert json.loads(user['content'].split('Input metadata: ')[1]) == dict(
        earlier_frames=12, current_frames=3)
    for earlier in EARLIER_VERDICTS:
        reply = parse_posture_check(response(json.dumps(
            dict(verdict='floor_active', earlier=earlier, explanation='앉음'))), request)
        assert (reply.verdict, reply.earlier) == ('floor_active', earlier)
    with pytest.raises(CloudFallProviderError):  # Asked about the fall: it must answer.
        parse_posture_check(response(json.dumps(dict(verdict='floor', explanation='누움'))),
                            request)
    plain = PostureCheckRequest('p2', frames(30.0, 3))
    with pytest.raises(CloudFallProviderError):  # Not asked: no such field.
        parse_posture_check(response(json.dumps(
            dict(verdict='floor', earlier='fall', explanation='누움'))), plain)


def test_two_fall_moments_at_most():
    two = PostureCheckRequest('p3', frames(30.0, 3), (frames(1.0, 12), frames(10.0, 12)))
    assert len(sent(build_posture_check_payload(two, model='gemma'))[1]['images']) == 27
    for bad in (PostureCheckRequest('p4', frames(30.0, 3), (frames(1.0, 1),) * 3),
                PostureCheckRequest('p5', frames(30.0, 5)),
                PostureCheckRequest('p6', frames(30.0, 3), (frames(1.0, 13), frames(9.0, 12)))):
        with pytest.raises(CloudFallProviderError):
            build_posture_check_payload(bad, model='gemma')


def test_arrival_compares_the_first_sight_with_the_arrival():
    request = ArrivalCheckRequest('a1', frames(10.0, 1), (0.2, 0.4, 0.6, 0.9), frames(40.0, 1))
    system, user = sent(build_arrival_check_payload(request, model='gemma'))
    assert system == arrival_prompt()
    assert len(user['images']) == 2
    assert json.loads(user['content'].split('Input metadata: ')[1]) == dict(
        earlier_frames=1, later_frames=1, earlier_target_box=[0.2, 0.4, 0.6, 0.9])
    for verdict in ARRIVAL_VERDICTS:
        assert parse_arrival_check(response(json.dumps(
            dict(verdict=verdict, explanation='설명')))).verdict == verdict
    for bad in (dict(verdict='same', explanation='x'), dict(verdict='gone')):
        with pytest.raises(CloudFallProviderError):
            parse_arrival_check(response(json.dumps(bad)))
    with pytest.raises(CloudFallProviderError):
        build_arrival_check_payload(ArrivalCheckRequest(
            'a2', frames(10.0, 1), (0.6, 0.4, 0.2, 0.9), frames(40.0, 1)), model='gemma')


def test_departure_sees_the_frames_in_between():
    request = DepartureCheckRequest('d1', frames(10.0, 1), None, frames(12.0, 12), frames(40.0, 1))
    system, user = sent(build_departure_check_payload(request, model='gemma'))
    assert system == departure_prompt()
    assert len(user['images']) == 14
    for verdict in DEPARTURE_VERDICTS:
        assert parse_departure_check(response(json.dumps(
            dict(verdict=verdict, explanation='설명')))).verdict == verdict
    with pytest.raises(CloudFallProviderError):
        build_departure_check_payload(DepartureCheckRequest(
            'd2', frames(10.0, 1), None, frames(12.0, 13), frames(40.0, 1)), model='gemma')
