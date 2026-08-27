#!/usr/bin/env python3
"""Spawn an SDF file or ROS description after Gazebo is ready."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree


Probe = Callable[[], tuple[bool, str]]


def _simulation_time(world: str, deadline: float) -> float:
    """Read the current time of one Gazebo world in seconds."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("spawn deadline expired before reading sim time")
    try:
        result = subprocess.run(
            [
                "ign",
                "topic",
                "-e",
                "-t",
                f"/world/{world}/stats",
                "-n",
                "1",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=min(remaining, 10.0),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(
            f"cannot read Gazebo simulation time: {error}"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"cannot read Gazebo simulation time: {detail}")
    match = re.search(r"sim_time\s*\{([^}]*)\}", result.stdout, re.DOTALL)
    if match is None:
        raise RuntimeError("Gazebo stats did not contain sim_time")
    fields = match.group(1)
    seconds = re.search(r"\bsec:\s*(-?\d+)", fields)
    nanoseconds = re.search(r"\bnsec:\s*(\d+)", fields)
    # Protobuf text omits scalar fields whose value is zero. This is common
    # while a fresh world is still in its first simulated second.
    if seconds is None and nanoseconds is None:
        return 0.0
    return (
        int(seconds.group(1)) if seconds is not None else 0
    ) + (
        int(nanoseconds.group(1)) if nanoseconds is not None else 0
    ) / 1_000_000_000


def _actor_sdf_starting_now(
    path: str,
    world: str,
    deadline: float,
    script_start_delay_s: float,
) -> str:
    """Return actor SDF whose script begins just after entity creation."""
    tree = ElementTree.parse(path)
    root = tree.getroot()
    actor = root.find("actor") if root.tag != "actor" else root
    if actor is None:
        raise ValueError("script alignment requires an SDF actor")
    delay_start = actor.find("script/delay_start")
    if delay_start is None:
        raise ValueError("actor script is missing delay_start")

    # Fortress evaluates runtime actor scripts against absolute world time.
    # Keep the lead configurable so benchmarks can begin after a repeatable
    # sensor warm-up while ordinary actor spawns retain the one-second lead.
    delay_start.text = (
        f"{_simulation_time(world, deadline) + script_start_delay_s:.9f}"
    )
    return ElementTree.tostring(root, encoding="unicode")


def _wait_for(probe: Probe, description: str, deadline: float) -> None:
    last_detail = ""
    while time.monotonic() < deadline:
        ready, detail = probe()
        if ready:
            return
        last_detail = detail
        time.sleep(0.2)
    suffix = f" ({last_detail})" if last_detail else ""
    raise TimeoutError(f"timed out waiting for {description}{suffix}")


def _listed(command: list[str], expected: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return False, detail
    entries = {line.strip() for line in result.stdout.splitlines()}
    return expected in entries, f"{expected} not listed"


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", required=True)
    parser.add_argument("--entity-name", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--topic")
    source.add_argument("--file")
    parser.add_argument(
        "--align-actor-script",
        action="store_true",
        help=(
            "start an SDF actor script at insertion instead of absolute "
            "world time"
        ),
    )
    parser.add_argument(
        "--actor-script-start-delay",
        type=float,
        default=1.0,
        help="seconds after creation before an aligned actor script starts",
    )
    parser.add_argument("--x", required=True, type=float)
    parser.add_argument("--y", required=True, type=float)
    parser.add_argument("--z", required=True, type=float)
    parser.add_argument("--yaw", required=True, type=float)
    parser.add_argument("--timeout", type=float, default=60.0)
    arguments = parser.parse_args()
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
    if arguments.file and not Path(arguments.file).is_file():
        parser.error(f"--file does not exist: {arguments.file}")
    if arguments.align_actor_script and not arguments.file:
        parser.error("--align-actor-script requires --file")
    if arguments.actor_script_start_delay <= 0.0:
        parser.error("--actor-script-start-delay must be positive")
    return arguments


def main() -> int:
    arguments = _parse_arguments()
    deadline = time.monotonic() + arguments.timeout
    create_service = f"/world/{arguments.world}/create"

    try:
        _wait_for(
            lambda: _listed(["ign", "service", "-l"], create_service),
            f"Gazebo service {create_service}",
            deadline,
        )
    except TimeoutError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    source_kind = "topic" if arguments.topic else "file"
    source_value = arguments.topic or arguments.file
    print(
        f"Gazebo is ready; spawning {arguments.entity_name!r} from "
        f"{source_kind} {source_value}."
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        print("ERROR: spawn deadline expired before create.", file=sys.stderr)
        return 1
    if arguments.file:
        half_yaw = arguments.yaw / 2.0
        try:
            actor_sdf = (
                _actor_sdf_starting_now(
                    arguments.file,
                    arguments.world,
                    deadline,
                    arguments.actor_script_start_delay,
                )
                if arguments.align_actor_script
                else None
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        source_field = (
            f"sdf: {json.dumps(actor_sdf)}, "
            if actor_sdf is not None
            else f"sdf_filename: {json.dumps(arguments.file)}, "
        )
        request = (
            source_field + f"name: {json.dumps(arguments.entity_name)}, "
            "allow_renaming: false, "
            "pose: {position: "
            f"{{x: {arguments.x}, y: {arguments.y}, z: {arguments.z}}}, "
            "orientation: "
            f"{{z: {math.sin(half_yaw)}, w: {math.cos(half_yaw)}}}}}"
        )
        command = [
            "ign",
            "service",
            "-s",
            create_service,
            "--reqtype",
            "ignition.msgs.EntityFactory",
            "--reptype",
            "ignition.msgs.Boolean",
            "--timeout",
            str(math.ceil(remaining * 1000.0)),
            "--req",
            request,
        ]
    else:
        command = [
            "ros2",
            "run",
            "ros_gz_sim",
            "create",
            "-world",
            arguments.world,
            "-name",
            arguments.entity_name,
            "-topic",
            arguments.topic,
            "-allow_renaming",
            "false",
            "-x",
            str(arguments.x),
            "-y",
            str(arguments.y),
            "-z",
            str(arguments.z),
            "-Y",
            str(arguments.yaw),
        ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as error:
        print(
            f"ERROR: entity creation timed out after {error.timeout:.1f}s.",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        print(f"ERROR: cannot start entity creation: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    confirmation = (
        "data: true"
        if arguments.file
        else "Requested creation of entity."
    )
    if result.returncode != 0 or confirmation not in (
        result.stdout + result.stderr
    ):
        print(
            "ERROR: Gazebo did not confirm entity creation.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
