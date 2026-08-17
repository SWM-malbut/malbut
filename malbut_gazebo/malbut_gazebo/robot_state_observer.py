"""
Narrow read-only ROS 2 adapter for trusted RobotState evidence.

The node observes Nav2 lifecycle state, action/service readiness, and the
``map`` to ``base_footprint`` transform.  It never sends an action goal,
calls a command service, publishes velocity, or fills safety fields for which
this repository has no authoritative source.
"""

import math
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Sequence

import rclpy
import tf2_ros
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from malbut_interfaces.msg import HomecamMediaEvidence
from nav2_msgs.action import ComputePathToPose, NavigateToPose
from nav2_msgs.srv import GetCostmap
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from rclpy.time import Time
from rcl_interfaces.msg import SetParametersResult

from malbut_agent_server.robot_state import (
    MAX_ROBOT_STATE_LIFETIME_NS,
    trusted_boottime_ns,
)
from malbut_agent_server.robot_state_collector import (
    RobotStateCollectorError,
    RobotStateCollectorServer,
    RobotStateSnapshotStore,
)
from malbut_gazebo.robot_state_observation import (
    HOMECAM_MEDIA_EVIDENCE_TOPIC,
    HomecamMediaEvidenceTracker,
    HomecamMediaObservationPublisher,
    Nav2ObservationBatch,
    RobotStateObservationPublisher,
    TimedBoolObservation,
)


_LIFECYCLE_SERVICES = {
    'amcl': '/amcl/get_state',
    'bt_navigator': '/bt_navigator/get_state',
    'planner_server': '/planner_server/get_state',
    'controller_server': '/controller_server/get_state',
    'global_costmap': '/global_costmap/global_costmap/get_state',
}
_MAP_FRAME = 'map'
_BASE_FRAME = 'base_footprint'
_HOMECAM_MEDIA_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)
_FIXED_PARAMETERS = frozenset({
    'device_id',
    'map_id',
    'map_revision',
    'socket_path',
    'expected_agent_uid',
    'physical_authority',
    'evidence_ttl_seconds',
    'observation_timeout_seconds',
    'lifecycle_poll_seconds',
    'publish_period_seconds',
    'tf_max_age_seconds',
    'socket_timeout_seconds',
    'use_sim_time',
})


def _bounded_seconds(
    value: object,
    field_name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError(f'{field_name} is invalid')
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f'{field_name} is invalid') from None
    if (
        not math.isfinite(numeric)
        or numeric < minimum
        or numeric > maximum
    ):
        raise ValueError(f'{field_name} is invalid')
    return numeric


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f'{field_name} is invalid')
    return value


@dataclass(frozen=True)
class RobotStateObserverConfig:
    """Fixed deployment configuration for the read-only ROS adapter."""

    device_id: str
    map_id: str
    map_revision: str
    socket_path: str
    expected_agent_uid: int
    physical_authority: bool = False
    evidence_ttl_seconds: float = 1.0
    observation_timeout_seconds: float = 0.75
    lifecycle_poll_seconds: float = 0.25
    publish_period_seconds: float = 0.1
    tf_max_age_seconds: float = 0.5
    socket_timeout_seconds: float = 1.0
    use_sim_time: bool = False

    def __post_init__(self) -> None:
        """Reject partial, simulated-physical, and stale configurations."""
        for name in ('device_id', 'map_id', 'map_revision', 'socket_path'):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name),
            )
        if (
            isinstance(self.expected_agent_uid, bool)
            or not isinstance(self.expected_agent_uid, int)
            or self.expected_agent_uid < 0
            or self.expected_agent_uid > (1 << 31) - 1
        ):
            raise ValueError('expected_agent_uid is invalid')
        if type(self.physical_authority) is not bool:
            raise ValueError('physical_authority is invalid')
        if type(self.use_sim_time) is not bool:
            raise ValueError('use_sim_time is invalid')
        maximum = MAX_ROBOT_STATE_LIFETIME_NS / 1_000_000_000
        ttl = _bounded_seconds(
            self.evidence_ttl_seconds,
            'evidence_ttl_seconds',
            minimum=0.001,
            maximum=maximum,
        )
        observation = _bounded_seconds(
            self.observation_timeout_seconds,
            'observation_timeout_seconds',
            minimum=0.001,
            maximum=maximum,
        )
        lifecycle_poll = _bounded_seconds(
            self.lifecycle_poll_seconds,
            'lifecycle_poll_seconds',
            minimum=0.01,
            maximum=maximum,
        )
        publish_period = _bounded_seconds(
            self.publish_period_seconds,
            'publish_period_seconds',
            minimum=0.01,
            maximum=maximum,
        )
        tf_max_age = _bounded_seconds(
            self.tf_max_age_seconds,
            'tf_max_age_seconds',
            minimum=0.001,
            maximum=maximum,
        )
        socket_timeout = _bounded_seconds(
            self.socket_timeout_seconds,
            'socket_timeout_seconds',
            minimum=0.001,
            maximum=5.0,
        )
        if observation > ttl:
            raise ValueError(
                'observation_timeout_seconds cannot exceed evidence TTL'
            )
        if lifecycle_poll > observation:
            raise ValueError(
                'lifecycle_poll_seconds cannot exceed observation timeout'
            )
        if tf_max_age > observation:
            raise ValueError(
                'tf_max_age_seconds cannot exceed observation timeout'
            )
        if self.physical_authority and self.use_sim_time:
            raise ValueError(
                'simulated time cannot provide physical authority'
            )
        object.__setattr__(self, 'evidence_ttl_seconds', ttl)
        object.__setattr__(
            self,
            'observation_timeout_seconds',
            observation,
        )
        object.__setattr__(
            self,
            'lifecycle_poll_seconds',
            lifecycle_poll,
        )
        object.__setattr__(
            self,
            'publish_period_seconds',
            publish_period,
        )
        object.__setattr__(self, 'tf_max_age_seconds', tf_max_age)
        object.__setattr__(
            self,
            'socket_timeout_seconds',
            socket_timeout,
        )


def _lifecycle_observation(
    state_id: object,
    received_boottime_ns: int,
) -> TimedBoolObservation:
    """Map one current lifecycle primary state to a strict tri-state."""
    if isinstance(state_id, bool) or not isinstance(state_id, int):
        return TimedBoolObservation.unknown()
    if state_id == State.PRIMARY_STATE_ACTIVE:
        return TimedBoolObservation(True, received_boottime_ns)
    if state_id in {
        State.PRIMARY_STATE_UNCONFIGURED,
        State.PRIMARY_STATE_INACTIVE,
        State.PRIMARY_STATE_FINALIZED,
    }:
        return TimedBoolObservation(False, received_boottime_ns)
    return TimedBoolObservation.unknown()


def _fresh_tf_observation(
    transform: object,
    *,
    ros_now_ns: int,
    received_boottime_ns: int,
    max_age_seconds: float,
) -> TimedBoolObservation:
    """Return true only for a finite, non-future, fresh map transform."""
    try:
        stamp = transform.header.stamp
        seconds = stamp.sec
        nanoseconds = stamp.nanosec
        translation = transform.transform.translation
        rotation = transform.transform.rotation
        numbers = (
            float(translation.x),
            float(translation.y),
            float(translation.z),
            float(rotation.x),
            float(rotation.y),
            float(rotation.z),
            float(rotation.w),
        )
    except (AttributeError, OverflowError, TypeError, ValueError):
        return TimedBoolObservation.unknown()
    if (
        type(seconds) is not int
        or seconds < 0
        or seconds > (1 << 31) - 1
        or type(nanoseconds) is not int
        or nanoseconds < 0
        or nanoseconds >= 1_000_000_000
        or isinstance(ros_now_ns, bool)
        or not isinstance(ros_now_ns, int)
        or ros_now_ns < 0
        or not all(math.isfinite(number) for number in numbers)
    ):
        return TimedBoolObservation.unknown()
    stamp_ns = seconds * 1_000_000_000 + nanoseconds
    quaternion_norm = sum(number * number for number in numbers[3:])
    if not math.isclose(
        quaternion_norm,
        1.0,
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        return TimedBoolObservation.unknown()
    age_ns = ros_now_ns - stamp_ns
    max_age_ns = int(max_age_seconds * 1_000_000_000)
    if age_ns < 0 or age_ns >= max_age_ns:
        return TimedBoolObservation.unknown()
    return TimedBoolObservation(True, received_boottime_ns)


class TrustedRobotStateObserver(Node):
    """Observe ROS health and serve a local, non-command state envelope."""

    def __init__(
        self,
        *,
        parameter_overrides: Optional[Sequence[Parameter]] = None,
    ) -> None:
        """Create fixed read-only clients and the local UDS server."""
        super().__init__(
            'trusted_robot_state_observer',
            parameter_overrides=parameter_overrides,
        )
        self._closed = False
        self._state_lock = threading.RLock()
        self._server: Optional[RobotStateCollectorServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._declare_parameters()
        config = self._configuration()
        self._config = config
        self._parameter_guard = self.add_on_set_parameters_callback(
            self._guard_parameter_updates
        )
        self._store = RobotStateSnapshotStore(
            config.device_id,
            config.map_id,
            config.map_revision,
            ttl_seconds=config.evidence_ttl_seconds,
            physical_authority=config.physical_authority,
        )
        self._publisher = RobotStateObservationPublisher(
            self._store,
            observation_timeout_seconds=(
                config.observation_timeout_seconds
            ),
        )
        self._media_tracker = HomecamMediaEvidenceTracker(
            config.device_id,
            require_physical_authority=config.physical_authority,
            maximum_lifetime_seconds=config.evidence_ttl_seconds,
        )
        self._media_publisher = HomecamMediaObservationPublisher(
            self._store,
        )
        unknown = TimedBoolObservation.unknown
        self._lifecycle: Dict[str, TimedBoolObservation] = {
            name: unknown() for name in _LIFECYCLE_SERVICES
        }
        self._lifecycle_futures: Dict[str, object] = {}
        self._lifecycle_clients = {
            name: self.create_client(GetState, service)
            for name, service in _LIFECYCLE_SERVICES.items()
        }
        self._tf_buffer = tf2_ros.Buffer(node=self)
        self._tf_listener = tf2_ros.TransformListener(
            self._tf_buffer,
            self,
            spin_thread=False,
        )
        self._compute_path = ActionClient(
            self,
            ComputePathToPose,
            '/compute_path_to_pose',
        )
        self._navigate = ActionClient(
            self,
            NavigateToPose,
            '/navigate_to_pose',
        )
        self._global_costmap = self.create_client(
            GetCostmap,
            '/global_costmap/get_costmap',
        )
        self._media_subscription = self.create_subscription(
            HomecamMediaEvidence,
            HOMECAM_MEDIA_EVIDENCE_TOPIC,
            self._on_media_evidence,
            _HOMECAM_MEDIA_QOS,
        )
        if (
            self._media_subscription.topic_name
            != HOMECAM_MEDIA_EVIDENCE_TOPIC
        ):
            self.destroy_subscription(self._media_subscription)
            raise RuntimeError(
                'trusted Homecam media topic must not be remapped'
            )
        self._wall_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self._lifecycle_timer = self.create_timer(
            config.lifecycle_poll_seconds,
            self._poll_lifecycle,
            clock=self._wall_clock,
        )
        self._publish_timer = self.create_timer(
            config.publish_period_seconds,
            self._publish_observation,
            clock=self._wall_clock,
        )
        server = RobotStateCollectorServer(
            self._store,
            config.socket_path,
            config.expected_agent_uid,
            timeout_seconds=config.socket_timeout_seconds,
        )
        try:
            server.start()
            server_thread = threading.Thread(
                target=self._serve,
                args=(server,),
                name='trusted-robot-state-uds',
                daemon=False,
            )
            self._server = server
            self._server_thread = server_thread
            server_thread.start()
        except Exception:
            server.close()
            raise

    def _declare_parameters(self) -> None:
        self.declare_parameter('device_id', '')
        self.declare_parameter('map_id', '')
        self.declare_parameter('map_revision', '')
        self.declare_parameter('socket_path', '')
        self.declare_parameter('expected_agent_uid', -1)
        self.declare_parameter('physical_authority', False)
        self.declare_parameter('evidence_ttl_seconds', 1.0)
        self.declare_parameter('observation_timeout_seconds', 0.75)
        self.declare_parameter('lifecycle_poll_seconds', 0.25)
        self.declare_parameter('publish_period_seconds', 0.1)
        self.declare_parameter('tf_max_age_seconds', 0.5)
        self.declare_parameter('socket_timeout_seconds', 1.0)

    def _configuration(self) -> RobotStateObserverConfig:
        use_sim_time = self.get_parameter('use_sim_time').value
        return RobotStateObserverConfig(
            device_id=self.get_parameter('device_id').value,
            map_id=self.get_parameter('map_id').value,
            map_revision=self.get_parameter('map_revision').value,
            socket_path=self.get_parameter('socket_path').value,
            expected_agent_uid=(
                self.get_parameter('expected_agent_uid').value
            ),
            physical_authority=(
                self.get_parameter('physical_authority').value
            ),
            evidence_ttl_seconds=(
                self.get_parameter('evidence_ttl_seconds').value
            ),
            observation_timeout_seconds=(
                self.get_parameter('observation_timeout_seconds').value
            ),
            lifecycle_poll_seconds=(
                self.get_parameter('lifecycle_poll_seconds').value
            ),
            publish_period_seconds=(
                self.get_parameter('publish_period_seconds').value
            ),
            tf_max_age_seconds=(
                self.get_parameter('tf_max_age_seconds').value
            ),
            socket_timeout_seconds=(
                self.get_parameter('socket_timeout_seconds').value
            ),
            use_sim_time=use_sim_time,
        )

    def _guard_parameter_updates(
        self,
        parameters: Sequence[Parameter],
    ) -> SetParametersResult:
        """Keep every identity, clock, and freshness parameter immutable."""
        for parameter in parameters:
            if (
                parameter.name in _FIXED_PARAMETERS
                and parameter.value
                != self.get_parameter(parameter.name).value
            ):
                return SetParametersResult(
                    successful=False,
                    reason='trusted robot-state parameters are immutable',
                )
        return SetParametersResult(successful=True)

    def _poll_lifecycle(self) -> None:
        for name, client in self._lifecycle_clients.items():
            with self._state_lock:
                if self._closed or name in self._lifecycle_futures:
                    continue
                self._lifecycle_futures[name] = None
            if not client.service_is_ready():
                with self._state_lock:
                    self._lifecycle_futures.pop(name, None)
                continue
            try:
                future = client.call_async(GetState.Request())
            except Exception:
                with self._state_lock:
                    self._lifecycle[name] = (
                        TimedBoolObservation.unknown()
                    )
                    self._lifecycle_futures.pop(name, None)
                continue
            with self._state_lock:
                if self._closed:
                    self._lifecycle_futures.pop(name, None)
                    continue
                self._lifecycle_futures[name] = future
            future.add_done_callback(
                lambda completed, node_name=name: (
                    self._lifecycle_result(node_name, completed)
                )
            )

    def _lifecycle_result(self, name: str, future: object) -> None:
        observation = TimedBoolObservation.unknown()
        try:
            response = future.result()
            receipt = trusted_boottime_ns()
            observation = _lifecycle_observation(
                response.current_state.id,
                receipt,
            )
        except Exception:
            pass
        with self._state_lock:
            if self._lifecycle_futures.get(name) is future:
                self._lifecycle[name] = observation
                self._lifecycle_futures.pop(name, None)

    def _tf_observation(
        self,
        receipt: int,
    ) -> TimedBoolObservation:
        try:
            transform = self._tf_buffer.lookup_transform(
                _MAP_FRAME,
                _BASE_FRAME,
                Time(),
            )
            ros_now_ns = self.get_clock().now().nanoseconds
        except Exception:
            return TimedBoolObservation.unknown()
        return _fresh_tf_observation(
            transform,
            ros_now_ns=ros_now_ns,
            received_boottime_ns=receipt,
            max_age_seconds=self._config.tf_max_age_seconds,
        )

    def _on_media_evidence(self, message: HomecamMediaEvidence) -> None:
        """Validate and atomically commit one local Homecam observation."""
        try:
            receipt = trusted_boottime_ns()
            binding_token = self._store.binding_token()
            batch = self._media_tracker.observe(
                message,
                binding_token=binding_token,
                received_boottime_ns=receipt,
            )
            if batch is not None:
                self._media_publisher.publish_fail_closed(batch)
        except Exception:
            self.get_logger().warning(
                'trusted Homecam media evidence was rejected'
            )

    def _publish_observation(self) -> None:
        try:
            receipt = trusted_boottime_ns()
            compute_path = TimedBoolObservation(
                bool(self._compute_path.server_is_ready()),
                receipt,
            )
            navigate = TimedBoolObservation(
                bool(self._navigate.server_is_ready()),
                receipt,
            )
            costmap = TimedBoolObservation(
                bool(self._global_costmap.service_is_ready()),
                receipt,
            )
            map_tf = self._tf_observation(receipt)
            with self._state_lock:
                lifecycle = dict(self._lifecycle)
            batch = Nav2ObservationBatch(
                binding_token=self._store.binding_token(),
                amcl_active=lifecycle['amcl'],
                bt_navigator_active=lifecycle['bt_navigator'],
                planner_server_active=lifecycle['planner_server'],
                controller_server_active=lifecycle['controller_server'],
                global_costmap_active=lifecycle['global_costmap'],
                compute_path_ready=compute_path,
                navigate_ready=navigate,
                global_costmap_ready=costmap,
                map_tf_fresh=map_tf,
            )
            self._publisher.publish(batch)
            expired_media = self._media_tracker.expire(
                binding_token=self._store.binding_token(),
                now_boottime_ns=receipt,
            )
            if expired_media is not None:
                self._media_publisher.publish_fail_closed(expired_media)
        except Exception:
            self.get_logger().warning(
                'trusted robot-state observation was rejected'
            )

    def _serve(self, server: RobotStateCollectorServer) -> None:
        try:
            server.serve_forever()
        except RobotStateCollectorError:
            with self._state_lock:
                closed = self._closed
            if not closed:
                self.get_logger().error(
                    'trusted robot-state socket server stopped'
                )

    def destroy_node(self) -> bool:
        """Stop UDS work before releasing ROS entities."""
        with self._state_lock:
            if self._closed:
                return True
            self._closed = True
            server = self._server
            thread = self._server_thread
            self._server = None
            self._server_thread = None
        self._lifecycle_timer.cancel()
        self._publish_timer.cancel()
        if server is not None:
            server.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._config.socket_timeout_seconds + 1.0)
            if thread.is_alive():
                self.get_logger().error(
                    'trusted robot-state socket server did not stop'
                )
        return super().destroy_node()


def main(args: Optional[Sequence[str]] = None) -> None:
    """Run the read-only observation node until shutdown."""
    rclpy.init(args=args)
    node: Optional[TrustedRobotStateObserver] = None
    try:
        node = TrustedRobotStateObserver()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


__all__ = [
    'RobotStateObserverConfig',
    'TrustedRobotStateObserver',
    'main',
]
