"""Check Python style in the isolated re-identification package."""

from ament_flake8.main import main_with_errors
from pathlib import Path


def test_flake8():
    """Run the project style rules."""
    package_root = Path(__file__).resolve().parents[1]
    return_code, errors = main_with_errors(argv=[str(package_root)])
    assert return_code == 0, '\n'.join(errors)
