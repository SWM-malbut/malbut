"""Check package docstrings."""

from ament_pep257.main import main
from pathlib import Path


def test_pep257():
    """Run the project docstring rules."""
    package_root = Path(__file__).resolve().parents[1]
    assert main(argv=[str(package_root / path) for path in (
        'malbut_reid', 'launch', 'setup.py', 'test',
    )]) == 0
