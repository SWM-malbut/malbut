"""Shared offline fixtures for the TTS tests."""

import pytest


@pytest.fixture(scope='session')
def _empty_key_root(tmp_path_factory):
    return tmp_path_factory.mktemp('malbut-keys')


@pytest.fixture(autouse=True)
def _no_web_keys(monkeypatch, _empty_key_root):
    """Keys the web set on this machine never reach a test (SWM25-235)."""
    monkeypatch.setenv('MALBUT_KEY_DIR', str(_empty_key_root / 'none'))
