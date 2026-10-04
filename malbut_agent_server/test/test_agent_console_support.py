"""Make tracked console and sibling voice sources importable without ROS installs."""

import os
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_DIR = REPO_ROOT / 'malbut_agent_server' / 'tools' / 'agent_console'
CONSOLE_SCRIPT = CONSOLE_DIR / 'console.py'

# Agent CI runs from malbut_agent_server with PYTHONPATH=.; these tests must
# also work when pytest is invoked by the console from the repository root.
for source in reversed((CONSOLE_DIR, REPO_ROOT / 'malbut_agent_server',
                        REPO_ROOT / 'malbut_stt', REPO_ROOT / 'malbut_tts')):
    path = str(source)
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture(autouse=True)
def restore_console_umask():
    """Console startup changes the process mask; keep that change inside a test."""
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)
