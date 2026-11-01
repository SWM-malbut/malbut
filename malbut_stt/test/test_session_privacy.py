"""A privacy-preserving encounter cannot run alongside raw diagnostic capture."""

from malbut_stt.session_privacy import can_start_session, diagnostic_environment


def test_raw_diagnostics_refuse_companion_but_preserve_fall_and_normal_sessions():
    recorder = object()
    assert not can_start_session('patrol-chat-123', recorder)
    assert can_start_session('patrol-chat-123', None)
    assert can_start_session('fall-check-123', recorder)
    assert can_start_session('', recorder)


def test_normal_entrypoint_disables_implicit_logs_without_changing_explicit_diagnostics():
    environment = {'unrelated': 'value', 'MALBUT_STT_INCIDENT_CONFIG': ' '}
    settings = diagnostic_environment(environment)
    assert settings['MALBUT_STT_INCIDENT_CONFIG'] == '{"enabled":false}'
    assert environment['MALBUT_STT_INCIDENT_CONFIG'] == ' '
    assert settings['unrelated'] == 'value'
    for environment in ({'MALBUT_STT_INCIDENT_CONFIG': '{}'},
                        {'MALBUT_STT_INCIDENT_CONFIG': '{"enabled":false}'},
                        {'MALBUT_STT_DIAGNOSTIC_DIR': '/explicit/diagnostics'}):
        assert diagnostic_environment(environment) == environment
