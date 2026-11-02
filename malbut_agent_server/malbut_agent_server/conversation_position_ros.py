"""One bounded follow mission through Manager, with confirmed cleanup before speech."""

import json
import math
import time
from uuid import uuid4

import yaml


class ConversationPosition:
    def __init__(self, node, *, clock=time.monotonic):
        from malbut_interfaces.action import ExecuteMission
        from rclpy.action import ActionClient
        from rclpy.qos import DurabilityPolicy, QoSProfile
        from std_msgs.msg import String

        self.node, self.clock = node, clock
        self.action_type = ExecuteMission
        self.client = ActionClient(node, ExecuteMission, '/malbut/mission/execute')
        self.subscription = node.create_subscription(
            String, '/malbut/runtime/state', self._runtime,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.phase = 'idle'
        self.handle = None
        self.mission_id = ''
        self.success = False
        self.cancel_requested = False
        self.runtime_revision = 0
        self.start_revision = 0
        self.runtime_idle = False

    @property
    def busy(self):
        return self.phase not in ('idle', 'done')

    @property
    def released(self):
        return (self.phase == 'done' and self.runtime_idle
                and (self.runtime_revision > self.start_revision
                     or getattr(self, 'unstarted', False))
                and self.node.count_publishers('/malbut/runtime/state') > 0)

    def _runtime(self, message):
        try:
            payload = json.loads(message.data)
            groups = payload['active_groups']
            self.runtime_idle = (payload.get('ready') is True and not payload.get('error')
                                 and isinstance(groups, list) and not groups
                                 and payload.get('capabilities') == [])
        except (ValueError, TypeError, KeyError, AttributeError):
            self.runtime_idle = False
        self.runtime_revision += 1

    def start(self, navigation=None, *, rotation=None):
        from unique_identifier_msgs.msg import UUID

        if self.busy or not self.client.server_is_ready():
            return False
        if rotation is not None and (navigation is not None or not math.isfinite(rotation)
                                     or not 0 < abs(rotation) <= math.pi):
            return False
        identity = uuid4()
        self.mission_id = identity.hex
        self.phase, self.handle = 'requesting', None
        self.success = self.cancel_requested = False
        self.unstarted = False
        self.start_revision = self.runtime_revision
        self.navigation = navigation is not None
        self.rotation = rotation is not None
        self.deadline = self.clock() + (120 if self.navigation else 55)
        self.accept_deadline = self.clock() + 5
        capability = 'navigate_to_pose' if self.navigation else 'follow_person'
        arguments = navigation if self.navigation else dict(
            target_mode=0, target_person_id='', desired_distance_m=1.5, approach_only=True)
        if self.rotation:
            capability = 'spin'
            arguments = dict(target_yaw=rotation, time_allowance=dict(sec=20, nanosec=0))
        try:
            future = self.client.send_goal_async(self.action_type.Goal(
                capability_id=capability, arguments_yaml=json.dumps(arguments), idle_only=True),
                goal_uuid=UUID(uuid=list(identity.bytes)))
            future.add_done_callback(self._accepted)
        except Exception:
            # Dispatch uncertainty is not proof that the base is free.
            self.cancel_requested = True
            self.phase = 'canceling'
            self.node.get_logger().warning('conversation_position_dispatch_unconfirmed')
        return True

    def _accepted(self, future):
        try:
            handle = future.result()
            if not handle.accepted:
                self.unstarted = True
                self.phase = 'done'
                return
            self.handle = handle
            self.phase = 'running'
            handle.get_result_async().add_done_callback(self._finished)
            if self.cancel_requested or self.clock() >= self.accept_deadline:
                self.cancel()
        except Exception:
            self.cancel_requested = True
            self.phase = 'canceling'
            self.node.get_logger().warning('conversation_position_acceptance_unconfirmed')

    def _finished(self, future):
        from action_msgs.msg import GoalStatus

        try:
            reply = future.result()
        except Exception:
            self.success = False
            self.cancel()
            return  # A lost result does not confirm that motion ended.
        if (getattr(reply, 'status', None) == GoalStatus.STATUS_ABORTED
                and getattr(getattr(reply, 'result', None), 'message', None)
                == 'conversation positioning skipped: manager is busy'):
            # Manager rejected before submitting any downstream goal. No
            # runtime transition is expected, but idle state/odometry still gate speech.
            self.unstarted = True
        try:
            result = yaml.safe_load(reply.result.result_yaml)
            stopped = (isinstance(result, dict) and result.get('success') is True
                       and result.get('final_state') == 'STOPPED')
            # Nav2 NavigateToPose has an empty result; terminal SUCCEEDED is
            # followed by independent runtime release and odometry checks.
            completed = (isinstance(result, dict) and not result
                         if getattr(self, 'navigation', False) else stopped)
            if getattr(self, 'rotation', False):
                completed = (isinstance(result, dict)
                             and isinstance(result.get('total_elapsed_time'), dict)
                             and result.get('error_code', 0) == 0)
            self.success = (not self.cancel_requested and completed
                            and reply.status == GoalStatus.STATUS_SUCCEEDED)
        except Exception:
            self.success = False
        self.phase = 'done'
        self.node.get_logger().info('conversation_position_finished success=' + str(self.success))

    def allowed_state(self, message):
        missions = (*message.active_foreground_missions, *message.active_background_missions,
                    *message.pending_missions, *message.suspended_missions)
        return (message.control_mode == message.AUTONOMOUS
                and message.system_state in (message.IDLE, message.EXECUTING_MISSION)
                and not message.pending_missions and not message.suspended_missions
                and all(m.mission_id == self.mission_id for m in missions))

    def tick(self, allowed):
        if self.busy and (not allowed or self.clock() >= self.deadline
                          or self.phase == 'requesting' and self.clock() >= self.accept_deadline):
            self.cancel()

    def cancel(self):
        if not self.busy:
            return
        self.cancel_requested = True
        if self.handle is not None and self.phase != 'canceling':
            try:
                self.handle.cancel_goal_async()
            except Exception:
                self.node.get_logger().warning('conversation_position_cancel_unconfirmed')
        elif self.handle is not None and self.phase == 'canceling':
            return
        self.phase = 'canceling' if self.handle is not None else 'requesting'

    def close(self):
        self.cancel()
        self.node.destroy_subscription(self.subscription)
        self.client.destroy()
