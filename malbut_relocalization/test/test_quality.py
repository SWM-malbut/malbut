"""Apply the standard ROS Python source-quality checks."""

from configparser import ConfigParser
from pathlib import Path

import ament_flake8
from ament_flake8.main import main_with_errors
from ament_pep257.main import main


ROOT = Path(__file__).parents[1]


def test_flake8(tmp_path):
    """Keep the relocalization package lint-clean."""
    # Forking lint workers after ROS tests can crash and leave the pool waiting.
    config = ConfigParser()
    config.read(Path(ament_flake8.__file__).parent / 'configuration' / 'ament_flake8.ini')
    config['flake8']['jobs'] = '1'
    config_path = tmp_path / 'flake8.ini'
    with config_path.open('w') as stream:
        config.write(stream)
    result, errors = main_with_errors(argv=['--config', str(config_path), str(ROOT)])
    assert result == 0, '\n'.join(errors)


def test_pep257():
    """Document public entry points consistently with other ROS packages."""
    assert main(argv=[str(ROOT)]) == 0
