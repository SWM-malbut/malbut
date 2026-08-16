"""Tests for protected fixed voice configuration loading."""

import os
from dataclasses import replace

import pytest

from conftest import voice_config_dict, write_protected_json
from malbut_voice.config import (
    FIXED_SOURCE,
    VoiceConfig,
    load_protected_config,
)
from malbut_voice.errors import (
    ConfigSecurityError,
    VoiceBoundaryError,
    chain_free_boundary,
)


def test_protected_config_loads_exact_hardware_and_binding(config_file):
    """Accept a non-writable regular file with the strict schema."""
    config = load_protected_config(config_file)

    assert config.audio.alsa_device == 'plughw:CARD=PCH,DEV=0'
    assert config.audio.rdev_major == 116
    assert config.audio.rdev_minor == 9
    assert config.audio.device_mode == 0o660
    assert config.capture.expected_bytes(1) == 32000
    assert config.binding.speaker_id == 'operator-unverified'
    assert config.is_protected is True
    assert FIXED_SOURCE == 'local-hardware-faster-whisper-v1'


def test_config_rejects_symlink(tmp_path):
    """Never follow an operator-selected configuration symlink."""
    target = write_protected_json(
        tmp_path / 'target.json',
        voice_config_dict(tmp_path),
    )
    alias = tmp_path / 'alias.json'
    alias.symlink_to(target)

    with pytest.raises(ConfigSecurityError):
        load_protected_config(alias)


def test_config_rejects_group_or_world_write(tmp_path):
    """Reject mutable configuration even when its JSON is valid."""
    path = write_protected_json(
        tmp_path / 'voice.json',
        voice_config_dict(tmp_path),
    )
    path.chmod(0o666)

    with pytest.raises(ConfigSecurityError, match='protected_file_writable'):
        load_protected_config(path)


def test_config_rejects_duplicate_and_unknown_fields(tmp_path):
    """Reject ambiguous JSON and schema extensions by default."""
    duplicate = tmp_path / 'duplicate.json'
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1}',
        encoding='utf-8',
    )
    duplicate.chmod(0o600)
    with pytest.raises(ConfigSecurityError, match='duplicate'):
        load_protected_config(duplicate)

    value = voice_config_dict(tmp_path)
    value['audio']['command'] = 'malicious'
    path = write_protected_json(tmp_path / 'unknown.json', value)
    with pytest.raises(ConfigSecurityError, match='unknown'):
        load_protected_config(path)


@pytest.mark.parametrize(
    'selector',
    ['default', 'pulse', 'dsnoop:CARD=PCH', 'hw:Loopback,0', 'file:/tmp/x'],
)
def test_config_rejects_virtual_or_command_like_alsa_selectors(
    tmp_path,
    selector,
):
    """Permit only the fixed hw/plughw selector grammar."""
    value = voice_config_dict(tmp_path)
    value['audio']['alsa_device'] = selector
    path = write_protected_json(tmp_path / 'voice.json', value)

    with pytest.raises(ConfigSecurityError):
        load_protected_config(path)


def test_config_reader_does_not_accept_hard_links(tmp_path):
    """Reject path aliases that can mutate after administrative review."""
    target = write_protected_json(
        tmp_path / 'target.json',
        voice_config_dict(tmp_path),
    )
    alias = tmp_path / 'hard-link.json'
    os.link(target, alias)

    with pytest.raises(ConfigSecurityError, match='link_count'):
        load_protected_config(alias)


def test_schema_version_rejects_boolean(tmp_path):
    """Do not let Python boolean equality satisfy an integer schema field."""
    value = voice_config_dict(tmp_path)
    value['schema_version'] = True
    path = write_protected_json(tmp_path / 'voice.json', value)

    with pytest.raises(ConfigSecurityError, match='schema_version'):
        load_protected_config(path)


def test_public_config_failure_has_no_underlying_exception_chain(tmp_path):
    """Expose only a stable code when an OS-selected path fails."""
    private_path = tmp_path / 'private-secret-name.json'

    with pytest.raises(ConfigSecurityError) as raised:
        load_protected_config(private_path)

    assert str(raised.value) == 'protected_file_open_failed'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__traceback__ is None


def test_arbitrary_public_error_code_is_not_projected():
    """Replace caller-provided error strings with the stable class code."""
    error = ConfigSecurityError('/private/path/secret')

    assert error.code == 'config_security_failed'
    assert str(error) == 'config_security_failed'


def test_public_boundary_sanitizes_unexpected_exception():
    """Do not leak an unexpected exception message or chain."""
    @chain_free_boundary
    def fail_with_private_value():
        raise RuntimeError('/private/secret')

    with pytest.raises(VoiceBoundaryError) as raised:
        fail_with_private_value()

    assert str(raised.value) == 'voice_internal_error'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__traceback__ is None


@pytest.mark.parametrize(
    ('field', 'value'),
    [('sample_rate_hz', 48000), ('channels', 2)],
)
def test_config_rejects_pcm_shape_faster_whisper_would_misinterpret(
    tmp_path,
    field,
    value,
):
    """Require raw ndarray input to be exact 16 kHz mono S16_LE."""
    body = voice_config_dict(tmp_path)
    body['capture'][field] = value
    path = write_protected_json(tmp_path / 'voice.json', body)

    with pytest.raises(ConfigSecurityError, match='pcm_shape'):
        load_protected_config(path)


def test_direct_dictionary_config_is_not_a_protected_capability(tmp_path):
    """Distinguish parsed examples from one descriptor-attested config."""
    config = VoiceConfig.from_dict(voice_config_dict(tmp_path))

    assert config.is_protected is False


def test_dataclass_replace_cannot_carry_protected_config_proof(config_file):
    """Drop fd-loader authority from ordinary dataclass copies and changes."""
    config = load_protected_config(config_file)

    copied = replace(config)
    changed = replace(
        config,
        capture=replace(config.capture, sample_rate_hz=8000),
    )

    assert copied.is_protected is False
    assert changed.is_protected is False


@pytest.mark.parametrize(
    ('field', 'value'),
    [('device_uid', 1000), ('device_mode', 0o666), ('rdev_major', 1)],
)
def test_config_rejects_unsafe_device_security_shape(tmp_path, field, value):
    """Keep the active host binding on root-owned ALSA major 116 mode 0660."""
    body = voice_config_dict(tmp_path)
    body['audio'][field] = value
    path = write_protected_json(tmp_path / 'voice.json', body)

    with pytest.raises(ConfigSecurityError, match='device_security'):
        load_protected_config(path)
