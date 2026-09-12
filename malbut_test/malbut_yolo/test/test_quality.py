"""Scope standard ROS Python checks to this package."""

from pathlib import Path

from ament_flake8.main import main_with_errors
from ament_pep257.main import main


SOURCES = [str(Path(__file__).parents[1] / item)
           for item in ('malbut_yolo', 'launch', 'test', 'setup.py')]


def test_flake8():
    """Check only this package regardless of pytest's working directory."""
    result, errors = main_with_errors(argv=SOURCES)
    assert result == 0, '\n'.join(errors)


def test_pep257():
    """Keep public Python entry points documented."""
    assert main(argv=SOURCES) == 0
