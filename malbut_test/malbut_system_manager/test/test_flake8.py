"""Flake8 test for the system manager package."""

from ament_flake8.main import main_with_errors
import pytest


@pytest.mark.flake8
@pytest.mark.linter
def test_flake8():
    """Run flake8 through the standard ament wrapper."""
    return_code, errors = main_with_errors(argv=[])
    assert return_code == 0, '\n'.join(errors)
