"""Apply the standard ROS Python source-quality checks."""

from pathlib import Path

from ament_flake8.main import main_with_errors
from ament_pep257.main import main


ROOT = Path(__file__).parents[1]


def test_flake8():
    """Keep the automatic mapping package lint-clean."""
    result, errors = main_with_errors(argv=[str(ROOT)])
    assert result == 0, '\n'.join(errors)


def test_pep257():
    """Document public entry points consistently with other ROS packages."""
    assert main(argv=[str(ROOT)]) == 0
