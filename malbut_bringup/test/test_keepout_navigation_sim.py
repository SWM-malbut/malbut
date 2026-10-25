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


@pytest.mark.skipif(os.environ.get('MALBUT_CORRIDOR_SIM_TEST') != '1',
                    reason='requires a sourced Fortress simulation build')
@pytest.mark.parametrize('barriers', ['physical', 'keepout'])
def test_too_narrow_corridor_is_rejected(tmp_path, barriers):
    """Reject a 20 cm passage without crossing physical or virtual boundaries."""
    subprocess.run([
        sys.executable, str(Path(__file__).with_name('keepout_navigation_sim.py')),
        '--scenario', 'corridor', '--width', '.2', '--barriers', barriers,
        '--expect-blocked', '--output', str(tmp_path / barriers),
    ], check=True)


@pytest.mark.skipif(os.environ.get('MALBUT_PATROL_SIM_TEST') != '1',
                    reason='requires Fortress, RGB-D bridge and built malbut_patrol')
def test_two_room_patrol_and_return(tmp_path):
    """Run the real Patrol action with AMCL, camera evidence and a 60 cm corridor."""
    subprocess.run([
        sys.executable, str(Path(__file__).with_name('keepout_navigation_sim.py')),
        '--scenario', 'patrol', '--width', '.6', '--barriers', 'mixed',
        '--timeout', '900', '--output', str(tmp_path / 'patrol'),
    ], check=True)
