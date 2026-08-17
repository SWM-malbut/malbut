"""Run ROS Python formatting checks when the test dependency is present."""

import pytest


ament_flake8 = pytest.importorskip('ament_flake8.main')


@pytest.mark.flake8
@pytest.mark.linter
def test_flake8():
    """Check Python source formatting with ament_flake8."""
    return_code, errors = ament_flake8.main_with_errors(argv=[])
    assert return_code == 0, (
        'Found %d code style errors / warnings:\n' % len(errors)
        + '\n'.join(errors)
    )
