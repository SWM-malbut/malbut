"""Expose sibling sources for ROS-independent handoff and speech contract tests."""

from pathlib import Path
import sys

import pytest


# CI runs this package without installing the ROS workspace. Append, rather
# than prepend, so an explicitly selected deployment mirror keeps precedence.
sys.path.append(str(Path(__file__).resolve().parents[2] / 'malbut_fall_coordinator'))
sys.path.append(str(Path(__file__).resolve().parents[2] / 'malbut_stt'))


@pytest.fixture(scope='session')
def _empty_key_root(tmp_path_factory):
    return tmp_path_factory.mktemp('malbut-keys')


@pytest.fixture(autouse=True)
def _no_web_keys(monkeypatch, _empty_key_root):
    """Keys the web set on this machine never reach a test (SWM25-235)."""
    monkeypatch.setenv('MALBUT_KEY_DIR', str(_empty_key_root / 'none'))
