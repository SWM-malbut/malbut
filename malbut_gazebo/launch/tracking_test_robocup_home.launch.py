"""Measure tracking retention in the Hiwonder robocup_home baseline."""

from malbut_gazebo.tracking_benchmark_launch import (
    TrackingBenchmarkProfile,
    create_tracking_benchmark_launch,
)


PROFILE = TrackingBenchmarkProfile(
    world_name='robocup_home',
    map_filename='robocup_home.yaml',
    actor_filename='robocup_home.sdf',
    lap_duration_s=86.535,
    robot_x=0.0,
    robot_y=0.0,
    robot_yaw=0.0,
    actor_x=0.0,
    actor_y=0.0,
    actor_yaw=0.0,
)


def generate_launch_description():
    """Run the 86.535-second baseline-home circuit once."""
    return create_tracking_benchmark_launch(PROFILE)
