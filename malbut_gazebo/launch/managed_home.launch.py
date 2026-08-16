"""Select first-run mapping or saved-map navigation at launch time."""

import math
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
)
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_gazebo.map_lifecycle import load_active_revision
from malbut_gazebo.world_catalog import resolve_world


SIMULATION_ARGUMENTS = (
    "world_name", "variant_config", "entity_name", "spawn_timeout",
    "x", "y", "z", "yaw", "gui", "headless", "paused",
    "use_sim_time", "depth_camera_enabled", "imu_enabled", "verbosity",
    "iterations",
)


def _is_true(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _saved_initial_pose(active: dict) -> dict[str, str] | None:
    """Return a validated pose stored with a map revision."""
    pose = active.get("initial_pose")
    if not isinstance(pose, dict):
        return None
    try:
        values = {name: float(pose[name]) for name in ("x", "y", "yaw")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values.values()):
        return None
    return {name: str(value) for name, value in values.items()}


def _simulation_initial_pose(
    context,
    share: Path,
    saved_pose: dict[str, str] | None = None,
) -> dict[str, str]:
    """
    Resolve one pose shared by Gazebo spawning and AMCL.

    An explicit launch override wins.  Otherwise a saved map pose is used
    before falling back to the world catalog.  Sharing this result prevents
    Gazebo from spawning at the catalog pose while AMCL is initialized at a
    different saved pose.
    """
    world_name = LaunchConfiguration("world_name").perform(context)
    _, config = resolve_world(
        share / "config" / "worlds.yaml", share / "worlds", world_name
    )
    pose = {}
    for name in ("x", "y", "yaw"):
        override = LaunchConfiguration(name).perform(context).strip()
        fallback = (
            saved_pose[name]
            if saved_pose is not None
            else str(config["spawn"][name])
        )
        pose[name] = override or fallback
    return pose


def _localization_arguments(
    trusted_pose: dict[str, str] | None,
    trusted_handoff: bool = False,
) -> dict[str, str]:
    """
    Return fail-closed localization arguments for this runtime.

    A simulator can be deterministically spawned at ``trusted_pose`` and
    AMCL may therefore use that same pose.  Real hardware must not trust a map
    revision's last pose after a restart: it may have been carried while off.
    A SLAM-to-navigation handoff is allowed only when the supervisor proves
    that the odometry source stayed alive.  A hardware boot defaults to no
    initial pose and therefore leaves navigation unlocalized.
    """
    if trusted_pose is None and trusted_handoff:
        return {
            "restore_localization": "true",
            "set_initial_pose": "false",
        }
    if trusted_pose is None:
        return {
            "restore_localization": "false",
            "set_initial_pose": "false",
        }
    return {
        "restore_localization": "false",
        "set_initial_pose": "true",
        "initial_pose_x": trusted_pose["x"],
        "initial_pose_y": trusted_pose["y"],
        "initial_pose_yaw": trusted_pose["yaw"],
    }


def _explicit_initial_pose(context) -> dict[str, str]:
    """Return a finite pose supplied by a trusted runtime supervisor."""
    pose = {}
    for name in ("x", "y", "yaw"):
        raw = LaunchConfiguration(name).perform(context).strip()
        try:
            value = float(raw)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"trusted_initial_pose requires a finite {name}"
            ) from error
        if not math.isfinite(value):
            raise RuntimeError(
                f"trusted_initial_pose requires a finite {name}"
            )
        pose[name] = str(value)
    return pose


def _select_mode(context):
    share = Path(get_package_share_directory("malbut_gazebo"))
    store = Path(
        LaunchConfiguration("map_store").perform(context)
    ).expanduser()
    forced = _is_true(LaunchConfiguration("force_mapping").perform(context))
    simulation_enabled = _is_true(
        LaunchConfiguration("simulation").perform(context)
    )
    trusted_initial_pose = _is_true(
        LaunchConfiguration("trusted_initial_pose").perform(context)
    )
    trusted_localization_handoff = _is_true(
        LaunchConfiguration(
            "trusted_localization_handoff"
        ).perform(context)
    )
    active = load_active_revision(store)
    common = {
        name: LaunchConfiguration(name) for name in SIMULATION_ARGUMENTS
    }
    if active is None or forced:
        launch_name = (
            "first_run_mapping.launch.py"
            if simulation_enabled else "map_onboarding.launch.py"
        )
        mapping_arguments = {
            "use_sim_time": LaunchConfiguration("use_sim_time"),
            "rviz": LaunchConfiguration("rviz"),
            "web_host": LaunchConfiguration("web_host"),
            "web_port": LaunchConfiguration("web_port"),
            "map_store": LaunchConfiguration("map_store"),
            "replace_existing": "true" if active is not None else "false",
            "save_posegraph": LaunchConfiguration("save_posegraph"),
            "auto_start": LaunchConfiguration("auto_start"),
            "runtime_request_file": LaunchConfiguration(
                "runtime_request_file"
            ),
        }
        if simulation_enabled:
            mapping_arguments.update(common)
        mapping = IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                str(share / "launch" / launch_name)
            ),
            launch_arguments=mapping_arguments.items(),
        )
        return [mapping, _cloud_sync()]
    map_yaml = str((store / active["map_yaml"]).resolve())
    user_map = str((store / active["user_map"]).resolve())
    revision = (store / active["map_yaml"]).resolve().parent
    zone_mask = revision / "zone-filter.yaml"
    saved_pose = _saved_initial_pose(active)
    simulation_pose = None
    actions = []
    if simulation_enabled:
        simulation_pose = _simulation_initial_pose(
            context, share, saved_pose
        )
        actions.append(IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                str(share / "launch" / "worlds.launch.py")
            ),
            launch_arguments={
                **common,
                "x": simulation_pose["x"],
                "y": simulation_pose["y"],
                "yaw": simulation_pose["yaw"],
                "rviz": "false",
                "lidar_enabled": "true",
                "spawn_robot": "true",
                "bridge": "true",
            }.items(),
        ))
    elif trusted_initial_pose:
        simulation_pose = _explicit_initial_pose(context)
    localization_arguments = _localization_arguments(
        simulation_pose, trusted_localization_handoff
    )
    navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(share / "launch" / "navigation.launch.py")
        ),
        launch_arguments={
            "map": map_yaml,
            "user_map": user_map,
            "zone_mask": str(zone_mask) if zone_mask.is_file() else "",
            "localization_source": "static",
            "use_sim_time": LaunchConfiguration("use_sim_time"),
            "rviz": LaunchConfiguration("rviz"),
            "robot_web": "true",
            "robot_web_port": LaunchConfiguration("web_port"),
            **localization_arguments,
        }.items(),
    )
    return [*actions, navigation, _cloud_sync()]


def _cloud_sync() -> Node:
    """Create the optional outbound-only cloud map synchronization node."""
    return Node(
        package="malbut_gazebo",
        executable="cloud_robot_sync",
        name="cloud_robot_sync",
        output="screen",
        condition=IfCondition(LaunchConfiguration("cloud_sync")),
        parameters=[{
            "use_sim_time": LaunchConfiguration("use_sim_time"),
            "backend_url": LaunchConfiguration("cloud_backend_url"),
            "device_id": LaunchConfiguration("cloud_device_id"),
            "token_file": LaunchConfiguration("cloud_token_file"),
            "map_store": LaunchConfiguration("map_store"),
            "local_url": LaunchConfiguration("cloud_local_url"),
            "runtime_request_file": LaunchConfiguration(
                "runtime_request_file"
            ),
        }],
    )


def generate_launch_description():
    """Create one stable product entry point for mapping and navigation."""
    declarations = [
        DeclareLaunchArgument("world_name", default_value="small_house"),
        DeclareLaunchArgument(
            "variant_config", default_value="rosorin_ultimate_mecanum.yaml"
        ),
        DeclareLaunchArgument("entity_name", default_value="malbut"),
        DeclareLaunchArgument("spawn_timeout", default_value="60"),
        DeclareLaunchArgument("x", default_value=""),
        DeclareLaunchArgument("y", default_value=""),
        DeclareLaunchArgument("z", default_value=""),
        DeclareLaunchArgument("yaw", default_value=""),
        DeclareLaunchArgument("gui", default_value="true"),
        DeclareLaunchArgument("headless", default_value="false"),
        DeclareLaunchArgument("paused", default_value="false"),
        DeclareLaunchArgument("use_sim_time", default_value="true"),
        DeclareLaunchArgument("depth_camera_enabled", default_value="true"),
        DeclareLaunchArgument("imu_enabled", default_value="true"),
        DeclareLaunchArgument("verbosity", default_value="2"),
        DeclareLaunchArgument("iterations", default_value=""),
        DeclareLaunchArgument("rviz", default_value="true"),
        DeclareLaunchArgument(
            "simulation",
            default_value="true",
            description=(
                "Start Gazebo; set false when real robot drivers are running."
            ),
        ),
        DeclareLaunchArgument(
            "trusted_initial_pose",
            default_value="false",
            description=(
                "Allow explicit x/y/yaw initialization only when an external "
                "supervisor also owns the simulator spawn or a verified "
                "hardware localization anchor. Real robots must leave this "
                "false unless that invariant is proven."
            ),
        ),
        DeclareLaunchArgument(
            "trusted_localization_handoff",
            default_value="false",
            description=(
                "Restore a saved map-to-odom handoff only when an external "
                "supervisor proves that the same odometry session remained "
                "alive. Never enable this merely because a pose was saved."
            ),
        ),
        DeclareLaunchArgument("web_host", default_value="127.0.0.1"),
        DeclareLaunchArgument("web_port", default_value="8765"),
        DeclareLaunchArgument("cloud_sync", default_value="false"),
        DeclareLaunchArgument("cloud_backend_url", default_value=""),
        DeclareLaunchArgument("cloud_device_id", default_value=""),
        DeclareLaunchArgument("cloud_token_file", default_value=""),
        DeclareLaunchArgument(
            "cloud_local_url", default_value="http://127.0.0.1:8765"
        ),
        DeclareLaunchArgument(
            "map_store",
            default_value=str(
                Path.home() / ".local" / "share" / "malbut" / "maps"
            ),
        ),
        DeclareLaunchArgument(
            "force_mapping",
            default_value="false",
            description=(
                "Keep the active revision while creating a replacement."
            ),
        ),
        DeclareLaunchArgument("auto_start", default_value="false"),
        DeclareLaunchArgument("runtime_request_file", default_value=""),
        DeclareLaunchArgument(
            "save_posegraph",
            default_value="false",
            description=(
                "Store the large SLAM pose graph for later mapping "
                "continuation."
            ),
        ),
    ]
    return LaunchDescription([
        *declarations, OpaqueFunction(function=_select_mode)
    ])
