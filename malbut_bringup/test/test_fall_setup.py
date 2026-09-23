"""Check VLM startup choices without hardware, credentials or Cloud requests."""

from pathlib import Path

import pytest

from malbut_bringup.fall_setup import prepare_fall_monitor


def test_auto_skips_missing_configuration(tmp_path):
    """Keep unconfigured development machines usable without enabling analysis."""
    assert prepare_fall_monitor('auto', str(tmp_path / 'missing.json')) is None


def test_false_never_opens_configuration(monkeypatch):
    """Disabling VLM must work even if its files cannot be read."""
    monkeypatch.setattr(Path, 'stat', lambda _: pytest.fail('unexpected file access'))
    assert prepare_fall_monitor('false', '/private/fall.json') is None


@pytest.mark.parametrize('mode', ['auto', 'true'])
def test_empty_path_is_rejected(mode):
    """Do not interpret an empty filename as the working directory."""
    with pytest.raises(RuntimeError, match='must name'):
        prepare_fall_monitor(mode, '')


@pytest.mark.parametrize('mode', ['auto', 'true'])
@pytest.mark.parametrize('kind', ['directory', 'oversized', 'invalid_json'])
def test_existing_invalid_file_is_never_silently_skipped(tmp_path, mode, kind):
    """Auto mode only skips missing files, not broken configured deployments."""
    config = tmp_path / 'fall.json'
    if kind == 'directory':
        config.mkdir()
    else:
        config.write_text(' ' * 16385 if kind == 'oversized' else '{not json}')
    with pytest.raises(RuntimeError):
        prepare_fall_monitor(mode, str(config))


def test_unknown_startup_mode_is_rejected(tmp_path):
    """An unrecognized flag cannot be treated as an implicit enable."""
    with pytest.raises(RuntimeError, match='must be auto, true or false'):
        prepare_fall_monitor('yes', str(tmp_path / 'fall.json'))
