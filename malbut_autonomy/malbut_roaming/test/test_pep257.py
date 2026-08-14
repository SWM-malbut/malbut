"""Run pydocstyle through the standard ament integration."""

from ament_pep257.main import main
from pathlib import Path
import pytest


@pytest.mark.linter
@pytest.mark.pep257
def test_pep257():
    """Require public Python code to carry useful docstrings."""
    package_root = Path(__file__).parents[1]
    assert main(argv=[
        str(package_root / 'malbut_roaming'),
        str(package_root / 'launch'),
        str(package_root / 'setup.py'),
        str(package_root / 'test'),
    ]) == 0
