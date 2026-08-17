"""Run ROS docstring checks when the test dependency is present."""

import pytest


ament_pep257 = pytest.importorskip('ament_pep257.main')


@pytest.mark.linter
@pytest.mark.pep257
def test_pep257():
    """Check public Python docstrings with ament_pep257."""
    return_code = ament_pep257.main(argv=['.', 'test'])
    assert return_code == 0, 'Found code style errors / warnings'
