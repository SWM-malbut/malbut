"""Run the same style checks used by the other ROS packages."""

from pathlib import Path

from ament_flake8.main import main_with_errors
from ament_pep257.main import main as pep257_main


ROOT = Path(__file__).parents[1]


def test_flake8():
    """Keep Bringup Python code lint-clean."""
    result, errors = main_with_errors(argv=[str(ROOT)])
    assert result == 0, '\n'.join(errors)


def test_pep257():
    """Keep module and public entry-point documentation present."""
    assert pep257_main(argv=[str(ROOT)]) == 0
