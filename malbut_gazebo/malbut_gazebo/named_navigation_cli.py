"""Operator-explicit CLI for the SWM25-130 named Gazebo goal test."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Callable

from malbut_gazebo.named_navigation import NamedNavigationError
from malbut_gazebo.named_navigation_facade import (
    ActiveMapCatalogSource,
    NamedNavigationExecution,
    NamedNavigationFacade,
    NamedNavigationFacadeError,
    SimulationNavigationAuthority,
    terminal_status_dict,
)
from malbut_gazebo.robot_web_navigation_client import (
    NavigationStatus,
    RobotWebNavigationClient,
    RobotWebNavigationClientError,
    RobotWebOutcomeUnknown,
)


DEFAULT_DEVICE_ID = "malbut-sim-01"
TERMINAL_STATES = frozenset({"succeeded", "canceled", "failed"})


def wait_for_terminal(
    facade: NamedNavigationFacade,
    execution: NamedNavigationExecution,
    *,
    timeout_s: float,
    poll_interval_s: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> NavigationStatus:
    """Poll bounded status without sending or retrying a navigation command."""
    if not 0.0 < timeout_s <= 600.0:
        raise ValueError("timeout_s must be within (0, 600]")
    if not 0.05 <= poll_interval_s <= 5.0:
        raise ValueError("poll_interval_s must be within [0.05, 5]")
    deadline = monotonic() + timeout_s
    while True:
        status = facade.status(execution)
        if status.state in TERMINAL_STATES:
            return status
        if monotonic() >= deadline:
            raise TimeoutError("named navigation did not reach a known result")
        sleep(poll_interval_s)


def _print_result(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve one semantic room name through the local Robot Web "
            "connector. The default only previews; --execute-simulation is "
            "required to start Gazebo motion."
        )
    )
    parser.add_argument("--map-store", type=Path, required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument(
        "--robot-web-url",
        default="http://127.0.0.1:8765",
    )
    parser.add_argument(
        "--execute-simulation",
        action="store_true",
        help="Explicitly consume the preview and start one simulation goal.",
    )
    parser.add_argument("--wait-timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    return parser


def main(arguments: list[str] | None = None) -> int:
    """Preview or explicitly execute one name-only Gazebo navigation goal."""
    parsed = _parser().parse_args(arguments)
    try:
        source = ActiveMapCatalogSource(
            parsed.map_store,
            DEFAULT_DEVICE_ID,
        )
        client = RobotWebNavigationClient(parsed.robot_web_url)
        authority = (
            SimulationNavigationAuthority.explicit_test_authority()
            if parsed.execute_simulation
            else None
        )
        facade = NamedNavigationFacade(
            source.load,
            client,
            authority=authority,
        )
        if not parsed.execute_simulation:
            prepared = facade.preview(parsed.location)
            _print_result(prepared.to_public_dict())
            return 0

        execution = facade.navigate(parsed.location)
        try:
            status = wait_for_terminal(
                facade,
                execution,
                timeout_s=parsed.wait_timeout,
                poll_interval_s=parsed.poll_interval,
            )
        except KeyboardInterrupt:
            try:
                cancel = facade.cancel(execution)
            except RobotWebOutcomeUnknown as error:
                _print_result({
                    "state": "unknown",
                    "operation": error.operation,
                    "simulation": True,
                    "physical_authorized": False,
                })
                return 3
            _print_result({
                "state": cancel.state,
                "interrupted": True,
                "simulation": True,
                "physical_authorized": False,
            })
            return 130
        except TimeoutError:
            try:
                cancel = facade.cancel(execution)
            except RobotWebOutcomeUnknown as error:
                _print_result({
                    "state": "unknown",
                    "operation": error.operation,
                    "timed_out": True,
                    "simulation": True,
                    "physical_authorized": False,
                })
                return 3
            _print_result({
                "state": cancel.state,
                "timed_out": True,
                "simulation": True,
                "physical_authorized": False,
            })
            return 3
        public = terminal_status_dict(execution, status)
        _print_result(public)
        return 0 if status.state == "succeeded" else 4
    except RobotWebOutcomeUnknown as error:
        _print_result({
            "state": "unknown",
            "operation": error.operation,
            "simulation": True,
            "physical_authorized": False,
        })
        return 3
    except (
        NamedNavigationError,
        NamedNavigationFacadeError,
        RobotWebNavigationClientError,
        ValueError,
    ) as error:
        code = getattr(error, "code", type(error).__name__)
        _print_result({
            "state": "rejected",
            "error_code": code,
            "simulation": True,
            "physical_authorized": False,
        })
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
