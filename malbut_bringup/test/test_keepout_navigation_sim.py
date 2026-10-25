"""Opt-in physical simulation regression for the hardware costmap configuration."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get('MALBUT_KEEPOUT_SIM_TEST') != '1',
                    reason='requires a sourced Fortress simulation build')
def test_keepout_margin_and_round_trip_in_gazebo(tmp_path):
    """Keep lidar hazards lethal while a simulated robot navigates around a Zone."""
    subprocess.run([
        sys.executable, str(Path(__file__).with_name('keepout_navigation_sim.py')),
        '--output', str(tmp_path / 'fixed'),
    ], check=True)
