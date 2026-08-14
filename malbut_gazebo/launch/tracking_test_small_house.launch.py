"""Measure tracking retention over one AWS Small House humanoid lap."""

from malbut_gazebo.tracking_benchmark_launch import (
    TrackingBenchmarkProfile,
    create_tracking_benchmark_launch,
)


PROFILE = TrackingBenchmarkProfile(
    world_name='small_house',
    map_filename='small_house.yaml',
    actor_filename='model.sdf',
    lap_duration_s=141.134,
    robot_x=-3.665503,
    robot_y=-0.4874,
    robot_yaw=0.0,
    actor_x=-2.19,
    actor_y=-1.17,
    actor_yaw=0.0,
)


def generate_launch_description():
    """Run the existing 141.134-second Small House circuit once."""
    return create_tracking_benchmark_launch(PROFILE)
