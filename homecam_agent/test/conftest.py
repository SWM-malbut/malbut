"""Keep offline CLI tests from leaking process-wide settings into other tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def restore_process_umask():
    # Evaluation entrypoints deliberately choose 077 for private output.
    # Direct in-process calls in tests must not affect subsequent test cases.
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)
