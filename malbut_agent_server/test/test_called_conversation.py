"""Location answers are explicit map bindings, not model-produced coordinates."""

import pytest

from malbut_agent_server.called_conversation import caller_location, cancel_approach
from malbut_agent_server.speech_missions import SpeechMissions
from navigation_user_map import room, write_user_map, rooms, rewrite


@pytest.mark.parametrize('text,expected', [
    ('거실', '거실'), ('거실이야', '거실'), ('지금 거실에 있어요', '거실'),
    ('거실이요', '거실'), ('거실요', '거실'), ('안방입니다', '안방'),
    ('거실 소파 옆이야', '거실'), ('저는 안방이에요.', '안방'),
    ('여기!', None), ('큰거실', None), ('거실 말고 안방', None),
    ('거실에 없어', None), ('거실에 있었어', None), ('거실이랑 안방 사이', None),
    ('거실로 오지 마', None), ('x=4 y=2', None), ('등록 안 된 곳', None),
    ('거실에 안 있어', None), ('거실에 있지 않아요', None), ('아마 거실', None),
])
def test_caller_location(text, expected):
    assert caller_location(text, ('거실', '안방')) == expected


@pytest.mark.parametrize('text', ['멈춰', '오지 마', '그만 와', '접근 취소', '거기 있어'])
def test_motion_cancellation(text):
    assert cancel_approach(text)


def test_named_point_requires_free_map_space_and_current_binding(tmp_path):
    from unittest.mock import Mock
    path, selected, _, document = write_user_map(tmp_path, rooms=[room('거실', 0, 0)])
    missions = SpeechMissions(Mock())
    missions.observe_localization({'mode': 'LOCALIZATION', 'map': str(selected)})
    proposal = missions.prepare_conversation_navigation('called', '거실')
    assert proposal is not None and missions.navigation_matches(proposal)
    rooms(document)[0]['properties']['representative_point'] = [999, 999]
    rewrite(path, document)
    assert not missions.navigation_matches(proposal)
    assert missions.prepare_conversation_navigation('outside', '거실') is None
