#!/usr/bin/env python3
"""Spawn the robot only after Gazebo and robot_description are ready."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable


Probe = Callable[[], tuple[bool, str]]


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
    parser.add_argument("--topic", default="/robot_description")
    parser.add_argument("--x", required=True)
    parser.add_argument("--y", required=True)
    parser.add_argument("--z", required=True)
    parser.add_argument("--yaw", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    arguments = parser.parse_args()
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
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
        _wait_for(
            lambda: _listed(
                [
                    "ros2",
                    "topic",
                    "list",
                    "--no-daemon",
                    "--spin-time",
                    "1",
                ],
                arguments.topic,
            ),
            f"ROS topic {arguments.topic}",
            deadline,
        )
    except TimeoutError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        f"Gazebo and {arguments.topic} are ready; spawning "
        f"{arguments.entity_name!r}."
    )
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
        arguments.x,
        "-y",
        arguments.y,
        "-z",
        arguments.z,
        "-Y",
        arguments.yaw,
    ]
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        print("ERROR: spawn deadline expired before create.", file=sys.stderr)
        return 1
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
            f"ERROR: ros_gz_sim create timed out after {error.timeout:.1f}s.",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        print(f"ERROR: cannot start ros_gz_sim create: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if (
        result.returncode != 0
        or "Requested creation of entity."
        not in result.stdout + result.stderr
    ):
        print(
            "ERROR: ros_gz_sim create did not confirm entity creation.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
