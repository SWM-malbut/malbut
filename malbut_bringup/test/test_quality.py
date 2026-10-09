"""Run the same style checks used by the other ROS packages."""

from pathlib import Path
import subprocess
import sys

from ament_pep257.main import main as pep257_main


ROOT = Path(__file__).parents[1]


def test_flake8():
    """Keep Bringup Python code lint-clean."""
    # Flake8 forks checker workers. Start a fresh interpreter so they cannot
    # inherit native ROS state left by earlier tests in this pytest process.
    result = subprocess.run(
        [sys.executable, '-m', 'ament_flake8.main', str(ROOT)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout


def test_pep257():
    """Keep module and public entry-point documentation present."""
    assert pep257_main(argv=[str(ROOT)]) == 0
