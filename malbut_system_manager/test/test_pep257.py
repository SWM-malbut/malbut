"""Docstring style test for the system manager package."""

from ament_pep257.main import main
import pytest


@pytest.mark.linter
@pytest.mark.pep257
def test_pep257():
    """Run pydocstyle through the standard ament wrapper."""
    return_code = main(argv=['.', 'test'])
    assert return_code == 0
