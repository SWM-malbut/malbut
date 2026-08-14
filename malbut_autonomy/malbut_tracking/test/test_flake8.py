"""Run flake8 through the standard ament integration."""

from ament_flake8.main import main_with_errors
import pytest


@pytest.mark.flake8
@pytest.mark.linter
def test_flake8():
    """Require Python code to pass the ROS 2 style checker."""
    return_code, errors = main_with_errors(argv=[])
    assert return_code == 0, (
        f'Found {len(errors)} code style errors / warnings:\n'
        + '\n'.join(errors)
    )
