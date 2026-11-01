"""Do not admit summary-only companion speech while raw diagnostics are enabled."""


def can_start_session(session_id, diagnostics):
    return not (session_id.startswith('patrol-chat-') and diagnostics is not None)
