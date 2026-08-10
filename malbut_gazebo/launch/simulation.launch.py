import math
import os
import re
import shlex
from pathlib import Path
from xml.etree import ElementTree

import xacro
from ament_index_python.packages import (
    get_package_prefix,
    get_package_share_directory,
)
from launch import LaunchDescription
from launch.actions import (
    AppendEnvironmentVariable,
    DeclareLaunchArgument,
    EmitEvent,
    ExecuteProcess,
    IncludeLaunchDescription,
    OpaqueFunction,
    RegisterEventHandler,
)
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from malbut_description.variant_config import (
    load_variant_arguments,
    resolve_variant_config,
    xacro_value,
)


DESCRIPTION_PACKAGE = "malbut_description"
GAZEBO_PACKAGE = "malbut_gazebo"


def _as_bool(value, name):
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false, got: {value}")


def _resolve_file(value, directory, suffix):
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        if candidate.suffix == "":
            candidate = candidate.with_suffix(suffix)
        candidate = directory / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise RuntimeError(f"Required file does not exist: {candidate}")
    return candidate


def _world_name(world_file):
    try:
        root = ElementTree.parse(world_file).getroot()
        world = root.find("world")
        if world is None or not world.get("name"):
            raise ValueError("missing <world name=...>")
        return _validate_transport_name(world.get("name"), "world name")
    except (ElementTree.ParseError, ValueError) as error:
        raise RuntimeError(f"Invalid SDF world {world_file}: {error}") from error


def _as_finite_number(value, name):
    try:
        number = float(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be numeric, got: {value}") from error
    if not math.isfinite(number):
        raise RuntimeError(f"{name} must be finite, got: {value}")
    return f"{number:.17g}"


def _validate_transport_name(value, label):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError(
            f"{label} must start with a letter or underscore and contain "
            f"only letters, digits, and underscores: {value!r}"
        )
    return value


def _as_verbosity(value):
    try:
        verbosity = int(value)
    except ValueError as error:
        raise RuntimeError(
            f"verbosity must be an integer from 0 to 4, got: {value}"
        ) from error
    if str(verbosity) != value.strip() or not 0 <= verbosity <= 4:
        raise RuntimeError(
            f"verbosity must be an integer from 0 to 4, got: {value}"
        )
    return str(verbosity)


def _as_positive_number(value, name):
    number = _as_finite_number(value, name)
    if float(number) <= 0:
        raise RuntimeError(f"{name} must be positive, got: {value}")
    return number


def _as_positive_integer(value, name):
    try:
        number = int(value)
    except ValueError as error:
        raise RuntimeError(
            f"{name} must be a positive integer, got: {value}"
        ) from error
    if str(number) != value.strip() or number <= 0:
        raise RuntimeError(
            f"{name} must be a positive integer, got: {value}"
        )
    return str(number)


def _shutdown_on_spawn_failure(event, _context):
    if event.returncode == 0:
        return []
    reason = f"robot spawn helper exited with code {event.returncode}"
    return [EmitEvent(event=Shutdown(reason=reason))]


def _launch_setup(context):
    description_share = Path(get_package_share_directory(DESCRIPTION_PACKAGE))
    gazebo_share = Path(get_package_share_directory(GAZEBO_PACKAGE))
    ros_gz_sim_share = Path(get_package_share_directory("ros_gz_sim"))
    spawn_helper = (
        Path(get_package_prefix(GAZEBO_PACKAGE))
        / "lib"
        / GAZEBO_PACKAGE
        / "spawn_when_ready"
    )
    variant_file = resolve_variant_config(
        LaunchConfiguration("variant_config").perform(context),
        description_share,
    )
    world_file = _resolve_file(
        LaunchConfiguration("world").perform(context),
        gazebo_share / "worlds",
        ".sdf",
    )

    xacro_arguments = load_variant_arguments(variant_file)

    lidar_enabled = _as_bool(
        LaunchConfiguration("lidar_enabled").perform(context),
        "lidar_enabled",
    )
    depth_camera_enabled = _as_bool(
        LaunchConfiguration("depth_camera_enabled").perform(context),
        "depth_camera_enabled",
    )
    imu_enabled = _as_bool(
        LaunchConfiguration("imu_enabled").perform(context),
        "imu_enabled",
    )
    gui = _as_bool(LaunchConfiguration("gui").perform(context), "gui")
    headless = _as_bool(
        LaunchConfiguration("headless").perform(context), "headless"
    )
    paused = _as_bool(
        LaunchConfiguration("paused").perform(context), "paused"
    )
    use_sim_time = _as_bool(
        LaunchConfiguration("use_sim_time").perform(context), "use_sim_time"
    )
    spawn_robot = _as_bool(
        LaunchConfiguration("spawn_robot").perform(context), "spawn_robot"
    )
    bridge_enabled = _as_bool(
        LaunchConfiguration("bridge").perform(context), "bridge"
    )
    if spawn_robot and not spawn_helper.is_file():
        raise RuntimeError(f"Spawn helper is not installed: {spawn_helper}")
    entity_name = _validate_transport_name(
        LaunchConfiguration("entity_name").perform(context),
        "entity_name",
    )
    world_name = _world_name(world_file)
    spawn_pose = [
        _as_finite_number(
            LaunchConfiguration(argument).perform(context),
            argument,
        )
        for argument in ("x", "y", "z", "yaw")
    ]
    spawn_timeout = _as_positive_number(
        LaunchConfiguration("spawn_timeout").perform(context),
        "spawn_timeout",
    )
    verbosity = _as_verbosity(
        LaunchConfiguration("verbosity").perform(context)
    )
    iterations = LaunchConfiguration("iterations").perform(context).strip()

    sim_xacro = gazebo_share / "urdf" / "robot.gazebo.xacro"
    mappings = {
        key: xacro_value(value) for key, value in xacro_arguments.items()
    }
    mappings.update(
        {
            "lidar_enabled": xacro_value(lidar_enabled),
            "depth_camera_enabled": xacro_value(depth_camera_enabled),
            "imu_enabled": xacro_value(imu_enabled),
        }
    )
    robot_description = xacro.process_file(
        str(sim_xacro), mappings=mappings
    ).toxml()

    gz_options = []
    if not paused:
        gz_options.append("-r")
    if headless:
        gz_options.extend(["-s", "--headless-rendering"])
    elif not gui:
        gz_options.append("-s")
    if iterations:
        gz_options.extend(
            ["--iterations", _as_positive_integer(iterations, "iterations")]
        )
    gz_options.extend(
        [
            "-v",
            verbosity,
            shlex.quote(str(world_file)),
        ]
    )

    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(ros_gz_sim_share / "launch" / "gz_sim.launch.py")
        ),
        launch_arguments={
            "gz_args": " ".join(gz_options),
            "gz_version": "6",
            "on_exit_shutdown": "true",
        }.items(),
    )

    state_publisher = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        parameters=[
            {
                "robot_description": robot_description,
                "use_sim_time": use_sim_time,
            }
        ],
        output="screen",
    )

    spawn = ExecuteProcess(
        cmd=[
            str(spawn_helper),
            "--world",
            world_name,
            "--entity-name",
            entity_name,
            "--topic",
            "/robot_description",
            "--x",
            spawn_pose[0],
            "--y",
            spawn_pose[1],
            "--z",
            spawn_pose[2],
            "--yaw",
            spawn_pose[3],
            "--timeout",
            spawn_timeout,
        ],
        output="screen",
    )

    bridge = Node(
        package="ros_gz_bridge",
        executable="parameter_bridge",
        name="malbut_bridge",
        parameters=[
            {"config_file": str(gazebo_share / "config" / "bridge.yaml")}
        ],
        output="screen",
    )

    rviz = Node(
        package="rviz2",
        executable="rviz2",
        arguments=[
            "-d",
            str(description_share / "rviz" / "view.rviz"),
        ],
        parameters=[{"use_sim_time": use_sim_time}],
        condition=IfCondition(LaunchConfiguration("rviz")),
        output="screen",
    )

    resource_candidates = [world_file.parent]
    sibling_models = world_file.parent / "models"
    if sibling_models.is_dir():
        resource_candidates.append(sibling_models)
    if world_file.parent.name in {"world", "worlds"}:
        package_models = world_file.parent.parent / "models"
        if package_models.is_dir():
            resource_candidates.append(package_models)
            nested_models = package_models / "aws_small_house"
            if nested_models.is_dir():
                resource_candidates.append(nested_models)
    resource_candidates.extend([gazebo_share, description_share])
    resource_paths = os.pathsep.join(
        str(path)
        for path in dict.fromkeys(resource_candidates)
    )
    actions = [
        AppendEnvironmentVariable(
            "IGN_GAZEBO_RESOURCE_PATH", resource_paths
        ),
        AppendEnvironmentVariable("GZ_SIM_RESOURCE_PATH", resource_paths),
        AppendEnvironmentVariable("SDF_PATH", resource_paths),
    ]
    if spawn_robot:
        actions.append(
            RegisterEventHandler(
                OnProcessExit(
                    target_action=spawn,
                    on_exit=_shutdown_on_spawn_failure,
                )
            )
        )
    actions.append(gazebo)
    if spawn_robot:
        actions.extend([state_publisher, spawn])
    if bridge_enabled:
        actions.append(bridge)
    actions.append(rviz)
    return actions


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "variant_config",
                default_value="rosorin_ultimate_mecanum.yaml",
                description="Variant YAML basename or absolute path.",
            ),
            DeclareLaunchArgument(
                "world",
                default_value="empty",
                description=(
                    "A world basename from malbut_gazebo/worlds or an "
                    "absolute SDF path."
                ),
            ),
            DeclareLaunchArgument("entity_name", default_value="malbut"),
            DeclareLaunchArgument(
                "spawn_timeout",
                default_value="60",
                description="Seconds to wait for Gazebo and robot_description.",
            ),
            DeclareLaunchArgument("x", default_value="0.0"),
            DeclareLaunchArgument("y", default_value="0.0"),
            DeclareLaunchArgument("z", default_value="0.002"),
            DeclareLaunchArgument("yaw", default_value="0.0"),
            DeclareLaunchArgument("gui", default_value="true"),
            DeclareLaunchArgument("headless", default_value="false"),
            DeclareLaunchArgument("paused", default_value="false"),
            DeclareLaunchArgument("use_sim_time", default_value="true"),
            DeclareLaunchArgument("rviz", default_value="false"),
            DeclareLaunchArgument("lidar_enabled", default_value="true"),
            DeclareLaunchArgument(
                "depth_camera_enabled", default_value="true"
            ),
            DeclareLaunchArgument("imu_enabled", default_value="true"),
            DeclareLaunchArgument("verbosity", default_value="2"),
            DeclareLaunchArgument("spawn_robot", default_value="true"),
            DeclareLaunchArgument("bridge", default_value="true"),
            DeclareLaunchArgument(
                "iterations",
                default_value="",
                description=(
                    "Stop after this many Gazebo iterations; empty runs "
                    "until shutdown."
                ),
            ),
            OpaqueFunction(function=_launch_setup),
        ]
    )
