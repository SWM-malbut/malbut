"""Expose explicit, idempotent controls for the small-house demo person."""

import json
from pathlib import Path
from threading import Lock

from ament_index_python.packages import (
    get_package_prefix,
    get_package_share_directory,
)
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger

from malbut_gazebo.gazebo_actor import GazeboActorController


class DemoActorManager(Node):
    """Show or hide one scripted actor and publish verified scene state."""

    def __init__(self) -> None:
        super().__init__('demo_actor_manager')
        share = Path(get_package_share_directory('malbut_gazebo'))
        prefix = Path(get_package_prefix('malbut_gazebo'))
        defaults = {
            'world': 'small_house',
            'entity_name': 'scenario_humanoid',
            'actor_file': str(
                share
                / 'models'
                / 'humanoid_actor'
                / 'scenarios'
                / 'front_door_entry.sdf'
            ),
            'spawn_helper': str(
                prefix / 'lib' / 'malbut_gazebo' / 'spawn_when_ready'
            ),
            'service_prefix': '/world/small_house/scenario_actor',
            'x': 6.0,
            'y': -6.2,
            'z': 0.0,
            'yaw': 0.0,
            'operation_timeout_s': 10.0,
        }
        for name, value in defaults.items():
            self.declare_parameter(name, value)
        self._operation_lock = Lock()
        self._actor = GazeboActorController(
            world=str(self.get_parameter('world').value),
            entity_name=str(self.get_parameter('entity_name').value),
            actor_file=Path(str(self.get_parameter('actor_file').value)),
            spawn_helper=Path(
                str(self.get_parameter('spawn_helper').value)
            ),
            service_prefix=str(
                self.get_parameter('service_prefix').value
            ),
            x=float(self.get_parameter('x').value),
            y=float(self.get_parameter('y').value),
            z=float(self.get_parameter('z').value),
            yaw=float(self.get_parameter('yaw').value),
            timeout_s=float(
                self.get_parameter('operation_timeout_s').value
            ),
        )
        qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self._status = self.create_publisher(
            String, '/demo/person/status', qos
        )
        self.create_service(Trigger, '/demo/person/show', self._show)
        self.create_service(Trigger, '/demo/person/hide', self._hide)
        self._publish_verified_status()

    def _operate(self, response, *, visible: bool):
        if not self._operation_lock.acquire(blocking=False):
            response.success = False
            response.message = 'person transition is already running'
            return response
        try:
            if visible:
                if self._actor.exists():
                    response.message = 'person is already visible'
                else:
                    self._actor.spawn()
                    response.message = 'person entered from the front door'
            else:
                removed = self._actor.remove()
                response.message = (
                    'person removed from the demo house'
                    if removed else 'person is already hidden'
                )
            response.success = True
            self._publish_verified_status()
        except (OSError, RuntimeError, ValueError) as error:
            response.success = False
            response.message = str(error)
            self.get_logger().error(f'demo person command failed: {error}')
            self._publish_verified_status(error=str(error))
        finally:
            self._operation_lock.release()
        return response

    def _show(self, _request, response):
        return self._operate(response, visible=True)

    def _hide(self, _request, response):
        return self._operate(response, visible=False)

    def _publish_verified_status(self, error: str | None = None) -> None:
        try:
            visible = self._actor.exists()
        except (OSError, RuntimeError, ValueError) as state_error:
            visible = False
            error = error or str(state_error)
        message = String()
        message.data = json.dumps({
            'visible': visible,
            'entity_name': str(self.get_parameter('entity_name').value),
            'route': Path(
                str(self.get_parameter('actor_file').value)
            ).stem,
            'error': error,
        })
        self._status.publish(message)


def main(args=None) -> None:
    rclpy.init(args=args)
    node = DemoActorManager()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
