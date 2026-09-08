"""Check explicit commands without ROS or natural-language execution."""

from types import SimpleNamespace

import pytest

from malbut_agent_server.ros_communication import (
    CommandLines, MAX_COMMAND_BYTES, apply_command,
)


@pytest.fixture
def node():
    """Record calls at the transport boundary without touching a robot."""
    calls = []

    def submit(capability, arguments, request_id=None):
        calls.append(('submit', capability, arguments, request_id))
        return request_id or 'generated'

    return SimpleNamespace(
        calls=calls,
        say=lambda text: calls.append(('say', text)) or bool(
            isinstance(text, str) and text.strip()
        ),
        missions=SimpleNamespace(
            submit=submit,
            cancel=lambda request_id: calls.append(('cancel', request_id)),
            snapshot=lambda request_id: {'request_id': request_id},
        ),
    )


def test_submit_status_and_cancel_use_the_selected_request(node):
    """Control commands target their stored request."""
    arguments = {
        'target_mode': 1, 'target_person_id': 'test',
        'desired_distance_m': 1.0,
    }
    assert apply_command(node, {
        'op': 'submit', 'capability_id': 'follow_person',
        'arguments': arguments, 'request_id': 'test-1',
    }) == {'request_id': 'test-1'}
    assert apply_command(node, {'op': 'status', 'request_id': 'test-1'}) == {
        'request_id': 'test-1',
    }
    apply_command(node, {'op': 'cancel', 'request_id': 'test-1'})
    assert node.calls == [
        ('submit', 'follow_person', arguments, 'test-1'), ('cancel', 'test-1'),
    ]


def test_say_preserves_text_without_creating_a_mission(node):
    """Response text publication is independent of Manager execution."""
    assert apply_command(node, {'op': 'say', 'text': '  안내\n'}) == {
        'published': True,
    }
    assert node.calls == [('say', '  안내\n')]


@pytest.mark.parametrize('command', [
    '따라와', [], {'op': 'unknown'},
    {'op': 'submit', 'user_id': 'injected'},
    {'op': 'cancel'}, {'op': 'status', 'request_id': ' '},
])
def test_bad_commands_do_not_reach_transport(node, command):
    """Raw STT speech and extra control fields cannot cause execution."""
    with pytest.raises(ValueError):
        apply_command(node, command)
    assert node.calls == []


def test_partial_stdin_waits_for_newline_without_blocking_ros():
    """A partial pipe write must not be parsed or block the ROS event loop."""
    lines = CommandLines()
    assert list(lines.feed(b'{"op":"say",')) == []
    assert list(lines.feed(b'"text":"hello"}\n\n')) == [
        b'{"op":"say","text":"hello"}\n', b'\n',
    ]


def test_large_input_is_discarded_as_one_command_then_recovers():
    """Do not interpret the tail of an oversized command as a new operation."""
    lines = CommandLines()
    assert list(lines.feed(b'x' * (MAX_COMMAND_BYTES + 1))) == [None]
    assert list(lines.feed(b'more discarded')) == []
    assert list(lines.feed(b'\n{"op":"status"}\n')) == [b'{"op":"status"}\n']
