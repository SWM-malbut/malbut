"""A privacy-preserving encounter cannot run alongside raw diagnostic capture."""

from malbut_stt.session_privacy import can_start_session


def test_raw_diagnostics_refuse_companion_but_preserve_fall_and_normal_sessions():
    recorder = object()
    assert not can_start_session('patrol-chat-123', recorder)
    assert can_start_session('patrol-chat-123', None)
    assert can_start_session('fall-check-123', recorder)
    assert can_start_session('', recorder)
