"""Opt-in physical simulation regression for the hardware costmap configuration."""

import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


def run_simulation(arguments):
    """Let the verifier cancel motion and clean its process groups on interruption."""
    command = [sys.executable, str(Path(__file__).with_name('keepout_navigation_sim.py')),
               *arguments]
    process = subprocess.Popen(command, start_new_session=True)
    try:
        returncode = process.wait()
    except BaseException:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    if returncode:
        raise subprocess.CalledProcessError(returncode, command)


@pytest.mark.skipif(os.environ.get('MALBUT_KEEPOUT_SIM_TEST') != '1',
                    reason='requires a sourced Fortress simulation build')
def test_keepout_margin_and_round_trip_in_gazebo(tmp_path):
    """Keep lidar hazards lethal while a simulated robot navigates around a Zone."""
    run_simulation([
        '--output', str(tmp_path / 'fixed'),
    ])


@pytest.mark.skipif(os.environ.get('MALBUT_KEEPOUT_SIM_TEST') != '1',
                    reason='requires a sourced Fortress simulation build')
def test_amcl_with_unmapped_lidar_obstacle(tmp_path):
    """Keep AMCL accurate when a real laser obstacle is absent from the map."""
    run_simulation([
        '--amcl', '--output', str(tmp_path / 'amcl'),
    ])


@pytest.mark.skipif(os.environ.get('MALBUT_CORRIDOR_SIM_TEST') != '1',
                    reason='requires a sourced Fortress simulation build')
@pytest.mark.parametrize('barriers', ['physical', 'keepout'])
def test_too_narrow_corridor_is_rejected(tmp_path, barriers):
    """Reject a 20 cm passage without crossing physical or virtual boundaries."""
    run_simulation([
        '--scenario', 'corridor', '--width', '.2', '--barriers', barriers,
        '--expect-blocked', '--output', str(tmp_path / barriers),
    ])


@pytest.mark.skipif(os.environ.get('MALBUT_CORRIDOR_SIM_TEST') != '1',
                    reason='requires a sourced Fortress simulation build')
@pytest.mark.parametrize('width', [.4, .5])
@pytest.mark.parametrize('barriers', ['physical', 'mixed'])
def test_narrow_corridor_round_trip_without_recovery(tmp_path, width, barriers):
    """Cross in both directions without progress aborts or boundary intersections."""
    run_simulation([
        '--scenario', 'corridor', '--width', str(width), '--barriers', barriers,
        '--output', str(tmp_path / 'corridor'),
    ])


@pytest.mark.skipif(os.environ.get('MALBUT_PATROL_SIM_TEST') != '1',
                    reason='requires Fortress, RGB-D bridge and built malbut_patrol')
def test_two_room_patrol_and_return(tmp_path):
    """Run the real Patrol action with AMCL, camera evidence and a 60 cm corridor."""
    run_simulation([
        '--scenario', 'patrol', '--width', '.6', '--barriers', 'mixed',
        '--timeout', '900', '--output', str(tmp_path / 'patrol'),
    ])
