"""Do not admit summary-only companion speech while raw diagnostics are enabled."""


def diagnostic_environment(environ):
    """Require explicit diagnostic opt-in for the normal speech entry point.

    Even the incident recorder's logs-only mode retains transcript text. Its
    implicit default would otherwise refuse every companion listening session.
    Standalone incident tools and explicitly requested diagnostics stay intact.
    """
    settings = dict(environ)
    if not any(settings.get(key, '').strip() for key in (
            'MALBUT_STT_INCIDENT_CONFIG', 'MALBUT_STT_DIAGNOSTIC_DIR')):
        settings['MALBUT_STT_INCIDENT_CONFIG'] = '{"enabled":false}'
    return settings


def can_start_session(session_id, diagnostics):
    return not (session_id.startswith('patrol-chat-') and diagnostics is not None)
