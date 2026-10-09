"""Foreground cancellation uses explicit UUIDs, never a global stop service."""

from concurrent.futures import Future
from types import SimpleNamespace
from uuid import uuid4

import pytest

from malbut_agent_server.foreground_cancel import ForegroundCancellation


def mission(mode=0):
    return SimpleNamespace(mode=mode, mission_id=uuid4().hex)


def harness():
    value = ForegroundCancellation.__new__(ForegroundCancellation)
    value._closed, value._ids = False, None
    value._type = SimpleNamespace(Request=lambda: SimpleNamespace(
        goal_info=SimpleNamespace(goal_id=SimpleNamespace(uuid=[]))))
    sent, warnings = [], []

    def send(request):
        sent.append(bytes(request.goal_info.goal_id.uuid).hex())
        future = Future()
        future.set_result(SimpleNamespace(return_code=0, ERROR_REJECTED=1))
        return future

    value._client = SimpleNamespace(service_is_ready=lambda: True, call_async=send)
    value._node = SimpleNamespace(get_logger=lambda: SimpleNamespace(warning=warnings.append))
    return value, sent


def test_only_foreground_uuids_are_canceled_and_waiting_work_is_included():
    client, sent = harness()
    pending, suspended, active, background = mission(), mission(), mission(), mission(1)
    client.observe(SimpleNamespace(
        pending_missions=[pending, background], suspended_missions=[suspended, background],
        active_foreground_missions=[active], active_background_missions=[background],
    ))
    client()
    assert sent == [pending.mission_id, suspended.mission_id, active.mission_id]
    assert background.mission_id not in sent
    assert '0' * 32 not in sent


def test_no_state_or_unavailable_server_does_not_issue_global_cancellation():
    client, sent = harness()
    with pytest.raises(RuntimeError):
        client()
    client._ids = [mission().mission_id]
    client._client.service_is_ready = lambda: False
    with pytest.raises(RuntimeError):
        client()
    assert sent == []
