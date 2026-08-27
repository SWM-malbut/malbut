"""Activate the fixed Nav2 lifecycle stack once and fail closed."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import os
import sys
import time
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class LifecycleManagerTarget:
    """One lifecycle manager and the nodes it must make ACTIVE."""

    label: str
    service_name: str
    managed_nodes: tuple[str, ...]


LIFECYCLE_MANAGERS = (
    LifecycleManagerTarget(
        label='localization',
        service_name='/lifecycle_manager_localization/manage_nodes',
        managed_nodes=('map_server', 'amcl'),
    ),
    LifecycleManagerTarget(
        label='collision',
        service_name='/collision_lifecycle_manager/manage_nodes',
        managed_nodes=('collision_monitor',),
    ),
    LifecycleManagerTarget(
        label='navigation',
        service_name='/lifecycle_manager_navigation/manage_nodes',
        managed_nodes=(
            'controller_server',
            'smoother_server',
            'planner_server',
            'behavior_server',
            'bt_navigator',
            'waypoint_follower',
            'velocity_smoother',
        ),
    ),
)


class StartupGateError(RuntimeError):
    """Report one stable, path-free startup failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require_isolated_ros_context(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Require a local, non-default DDS domain before creating ROS clients."""
    values = os.environ if environment is None else environment
    raw_domain = values.get('ROS_DOMAIN_ID', '').strip()
    try:
        domain_id = int(raw_domain)
    except ValueError as error:
        raise StartupGateError('isolated_ros_domain_required') from error
    if not 1 <= domain_id <= 100:
        raise StartupGateError('isolated_ros_domain_required')
    if values.get('ROS_LOCALHOST_ONLY', '').strip() != '1':
        raise StartupGateError('localhost_only_required')
    return domain_id


class LifecycleStartupPort(Protocol):
    """Provide the bounded lifecycle operations used by the pure sequence."""

    def await_stable_services(
        self,
        *,
        timeout_seconds: float,
        stability_seconds: float,
        quiet_period_seconds: float,
    ) -> None:
        """Wait until every required service remains continuously ready."""

    def startup(
        self,
        target: LifecycleManagerTarget,
        *,
        response_timeout_seconds: float,
    ) -> bool:
        """Issue exactly one STARTUP request for one manager."""

    def confirm_active(
        self,
        target: LifecycleManagerTarget,
        *,
        response_timeout_seconds: float,
    ) -> None:
        """Require every managed node to report ACTIVE."""


def start_nav2_managers(
    port: LifecycleStartupPort,
    *,
    service_timeout_seconds: float,
    discovery_stability_seconds: float,
    quiet_period_seconds: float,
    response_timeout_seconds: float,
) -> None:
    """Run the one-shot safety-first startup sequence without retries."""
    try:
        port.await_stable_services(
            timeout_seconds=service_timeout_seconds,
            stability_seconds=discovery_stability_seconds,
            quiet_period_seconds=quiet_period_seconds,
        )
    except StartupGateError:
        raise
    except Exception as error:
        raise StartupGateError('service_discovery_failed') from error

    for target in LIFECYCLE_MANAGERS:
        try:
            acknowledged = port.startup(
                target,
                response_timeout_seconds=response_timeout_seconds,
            )
        except StartupGateError:
            raise
        except Exception as error:
            raise StartupGateError(
                f'{target.label}_startup_request_failed'
            ) from error
        if not acknowledged:
            raise StartupGateError(f'{target.label}_startup_rejected')
        try:
            port.confirm_active(
                target,
                response_timeout_seconds=response_timeout_seconds,
            )
        except StartupGateError:
            raise
        except Exception as error:
            raise StartupGateError(
                f'{target.label}_active_confirmation_failed'
            ) from error


class RclpyLifecycleStartupPort:
    """ROS implementation of the one-shot lifecycle startup boundary."""

    def __init__(self, node) -> None:
        """Create fixed clients without issuing an external request."""
        from lifecycle_msgs.srv import GetState
        from nav2_msgs.srv import ManageLifecycleNodes

        self._node = node
        self._manage_type = ManageLifecycleNodes
        self._state_type = GetState
        self._manager_clients = {
            target.label: node.create_client(
                ManageLifecycleNodes,
                target.service_name,
            )
            for target in LIFECYCLE_MANAGERS
        }
        self._state_clients = {
            node_name: node.create_client(
                GetState,
                f'/{node_name}/get_state',
            )
            for target in LIFECYCLE_MANAGERS
            for node_name in target.managed_nodes
        }

    def _all_clients(self):
        return (
            *self._manager_clients.values(),
            *self._state_clients.values(),
        )

    def await_stable_services(
        self,
        *,
        timeout_seconds: float,
        stability_seconds: float,
        quiet_period_seconds: float,
    ) -> None:
        """Use monotonic time rather than simulation time for discovery."""
        import rclpy

        deadline = time.monotonic() + timeout_seconds
        required_ready_seconds = stability_seconds + quiet_period_seconds
        stable_since = None
        while True:
            now = time.monotonic()
            ready = all(
                client.service_is_ready()
                for client in self._all_clients()
            )
            if ready:
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= required_ready_seconds:
                    return
            else:
                stable_since = None
            remaining = deadline - now
            if remaining <= 0.0:
                raise StartupGateError('service_discovery_not_stable')
            rclpy.spin_once(
                self._node,
                timeout_sec=min(0.05, remaining),
            )

    def _call_once(
        self,
        client,
        request,
        *,
        response_timeout_seconds: float,
        error_prefix: str,
    ):
        import rclpy

        if not client.service_is_ready():
            raise StartupGateError(f'{error_prefix}_service_unavailable')
        future = client.call_async(request)
        rclpy.spin_until_future_complete(
            self._node,
            future,
            timeout_sec=response_timeout_seconds,
        )
        if not future.done():
            # This request may already have taken effect. Forget the local
            # future and fail closed; never send an ambiguous mutation again.
            client.remove_pending_request(future)
            future.cancel()
            raise StartupGateError(f'{error_prefix}_response_unknown')
        error = future.exception()
        if error is not None:
            raise StartupGateError(
                f'{error_prefix}_response_failed'
            ) from error
        response = future.result()
        if response is None:
            raise StartupGateError(f'{error_prefix}_response_missing')
        return response

    def startup(
        self,
        target: LifecycleManagerTarget,
        *,
        response_timeout_seconds: float,
    ) -> bool:
        """Send STARTUP once and never retry an unknown response."""
        request = self._manage_type.Request()
        request.command = self._manage_type.Request.STARTUP
        response = self._call_once(
            self._manager_clients[target.label],
            request,
            response_timeout_seconds=response_timeout_seconds,
            error_prefix=f'{target.label}_startup',
        )
        return bool(response.success)

    def confirm_active(
        self,
        target: LifecycleManagerTarget,
        *,
        response_timeout_seconds: float,
    ) -> None:
        """Read every managed lifecycle state and require ACTIVE."""
        from lifecycle_msgs.msg import State

        for node_name in target.managed_nodes:
            response = self._call_once(
                self._state_clients[node_name],
                self._state_type.Request(),
                response_timeout_seconds=response_timeout_seconds,
                error_prefix=f'{node_name}_state',
            )
            if response.current_state.id != State.PRIMARY_STATE_ACTIVE:
                raise StartupGateError(f'{node_name}_state_not_active')


def _bounded_float(parser, arguments, name, *, allow_zero=False):
    value = getattr(arguments, name)
    lower_ok = value >= 0.0 if allow_zero else value > 0.0
    if not math.isfinite(value) or not lower_ok or value > 300.0:
        interval = '[0, 300]' if allow_zero else '(0, 300]'
        parser.error(
            f'--{name.replace("_", "-")} must be in {interval}'
        )


def _parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--service-timeout-seconds',
        type=float,
        default=30.0,
    )
    parser.add_argument(
        '--discovery-stability-seconds',
        type=float,
        default=1.0,
    )
    parser.add_argument(
        '--quiet-period-seconds',
        type=float,
        default=2.0,
    )
    parser.add_argument(
        '--response-timeout-seconds',
        type=float,
        default=60.0,
    )
    arguments = parser.parse_args(argv)
    _bounded_float(parser, arguments, 'service_timeout_seconds')
    _bounded_float(parser, arguments, 'discovery_stability_seconds')
    _bounded_float(
        parser,
        arguments,
        'quiet_period_seconds',
        allow_zero=True,
    )
    _bounded_float(parser, arguments, 'response_timeout_seconds')
    return arguments


def main() -> int:
    """Run the bounded gate and return nonzero when readiness is uncertain."""
    import rclpy
    from rclpy.utilities import remove_ros_args

    arguments = _parse_arguments(remove_ros_args(sys.argv)[1:])
    try:
        _require_isolated_ros_context()
    except StartupGateError as error:
        print(
            f'Nav2 startup gate failed: {error.code}',
            file=sys.stderr,
        )
        return 1
    rclpy.init(args=sys.argv)
    node = rclpy.create_node('nav2_startup_gate')
    try:
        start_nav2_managers(
            RclpyLifecycleStartupPort(node),
            service_timeout_seconds=arguments.service_timeout_seconds,
            discovery_stability_seconds=(
                arguments.discovery_stability_seconds
            ),
            quiet_period_seconds=arguments.quiet_period_seconds,
            response_timeout_seconds=arguments.response_timeout_seconds,
        )
        node.get_logger().info(
            'Nav2 lifecycle stack is ACTIVE; startup gate is complete'
        )
    except StartupGateError as error:
        node.get_logger().error(
            f'Nav2 startup gate failed: {error.code}'
        )
        return 1
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
