"""Route fall confirmations through the generic mission manager."""

import json
import math
from threading import RLock
import time

import yaml

from .fall_confirmation import FallConfirmationCoordinator


EXECUTE_MISSION_ACTION = '/malbut/mission/execute'


class FallConfirmationLink:
    """Serialize incident conversations and cancel superseded requests."""

    def __init__(self, node, *, runtime_id='', goal_response_timeout_s=5.0,
                 result_timeout_s=610.0, server_loss_timeout_s=5.0,
                 clock=time.monotonic):
        from rclpy.action import ActionClient
        from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
        from rclpy.clock import Clock, ClockType
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from malbut_interfaces.action import ConfirmSituation, ExecuteMission
        from malbut_interfaces.msg import SystemState
        from std_msgs.msg import String

        self.node = node
        self.lock = RLock()
        self.clock = clock
        self.goal_response_timeout_s = goal_response_timeout_s
        for value in (goal_response_timeout_s, result_timeout_s, server_loss_timeout_s):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not (
                    math.isfinite(value) and value > 0):
                raise ValueError('confirmation transport timeouts must be positive and finite')
        # Agent's own operational watchdog is 600 seconds. This is transport
        # recovery only, never the ten-second user-response policy.
        self.result_timeout_s = result_timeout_s
        self.server_loss_timeout_s = server_loss_timeout_s
        self.coordinator = FallConfirmationCoordinator(runtime_id=runtime_id)
        self.action_type, self.message_type = ExecuteMission, String
        self.group = MutuallyExclusiveCallbackGroup()
        self.client = ActionClient(node, ExecuteMission, EXECUTE_MISSION_ACTION,
                                   callback_group=self.group)
        # Discovery only: Goals and cancellation always go through the manager.
        # Preserve the existing Agent-loss watchdog after adding the manager hop.
        self.agent_presence = ActionClient(
            node, ConfirmSituation, '/malbut/agent/confirm_situation', callback_group=self.group)
        self.decisions = node.create_publisher(String, '/malbut/falls/runtime/decision', 10)
        self.events = node.create_subscription(
            String, '/malbut/falls/runtime/events', self.on_event, 50,
            callback_group=self.group)
        self.request = None
        self.goal_future = None
        self.handle = None
        self.sent_at = 0.0
        self.accepted_at = None
        self.server_missing_since = None
        self.next_attempt = 0.0
        self.manager_state = None
        self.states = node.create_subscription(
            SystemState, '/malbut/state', self.on_state,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                       reliability=ReliabilityPolicy.RELIABLE), callback_group=self.group)
        self.timer = node.create_timer(
            0.5, self.tick, callback_group=self.group,
            clock=Clock(clock_type=ClockType.STEADY_TIME))

    def _publish(self):
        for command in self.coordinator.drain_commands():
            self.decisions.publish(self.message_type(data=json.dumps(command, allow_nan=False)))

    def on_event(self, message):
        with self.lock:
            self.coordinator.receive(message.data)
            self._drive()

    def tick(self):
        with self.lock:
            self._drive()

    def on_state(self, message):
        with self.lock:
            self.manager_state = message
            self._drive()

    def _cancel_current(self):
        handle = self.handle
        pending = self.goal_future
        self.request = self.handle = self.goal_future = None
        self.accepted_at = self.server_missing_since = None
        if handle is not None:
            try:
                return handle.cancel_goal_async()
            except Exception:
                self.node.get_logger().warning('confirmation_cancel_transport_failed')
        return pending

    def _drive(self):
        if self.coordinator.closed:
            return
        self._publish()
        if (self.request is not None
                and self.coordinator.requests.get(self.request.request_id) != self.request):
            self._cancel_current()
        if self.request is not None:
            transport_failed = False
            if (self.goal_future is not None
                    and self.clock() - self.sent_at >= self.goal_response_timeout_s):
                transport_failed = True
            elif self.handle is not None:
                now = self.clock()
                if self.client.server_is_ready() and self.agent_presence.server_is_ready():
                    self.server_missing_since = None
                elif self.server_missing_since is None:
                    self.server_missing_since = now
                transport_failed = (
                    now - self.accepted_at >= self.result_timeout_s
                    or (self.server_missing_since is not None
                        and now - self.server_missing_since >= self.server_loss_timeout_s))
            if transport_failed:
                self.coordinator.fail(self.request)
                self._cancel_current()
                self._publish()
            return
        if self.clock() < self.next_attempt or not self.client.server_is_ready():
            return
        state = self.manager_state
        if state is None or any(
                mission.capability_id == 'fall_confirmation'
                for mission in (*state.active_foreground_missions,
                                *state.active_background_missions,
                                *state.pending_missions, *state.suspended_missions)):
            # After a coordinator restart, an earlier confirmation may still
            # run in the manager. Wait, then recover the Agent's cached result;
            # resubmitting immediately would preempt that same conversation.
            return
        request = next(iter(self.coordinator.requests.values()), None)
        if request is None:
            return
        self.request = request
        self.sent_at = self.clock()
        try:
            future = self.client.send_goal_async(self.action_type.Goal(
                capability_id='fall_confirmation',
                arguments_yaml=json.dumps(dict(
                    request_id=request.request_id, situation_type='fall',
                    summary=request.summary), ensure_ascii=False)))
            self.goal_future = future
            future.add_done_callback(lambda done: self._accepted(request, done))
        except Exception:
            self.coordinator.fail(request)
            self._cancel_current()
            self._publish()

    def _accepted(self, request, future):
        with self.lock:
            try:
                handle = future.result()
            except Exception:
                if self.request == request:
                    self.coordinator.fail(request)
                    self._cancel_current()
                    self._publish()
                return
            if (self.request != request or self.coordinator.closed
                    or self.coordinator.requests.get(request.request_id) != request):
                if handle.accepted:
                    handle.cancel_goal_async()
                return
            self.goal_future = None
            if not handle.accepted:
                # Manager may still be preparing the robot. Keep
                # this request queued; rejection is not the user's silence.
                self.request = None
                self.next_attempt = self.clock() + 1.0
                return
            self.handle = handle
            self.accepted_at = self.clock()
            self.server_missing_since = None
            try:
                handle.get_result_async().add_done_callback(
                    lambda done: self._done(request, done))
            except Exception:
                self.coordinator.fail(request)
                self._cancel_current()
                self._publish()

    def _done(self, request, future):
        from action_msgs.msg import GoalStatus

        with self.lock:
            if self.request != request or self.coordinator.closed:
                return
            try:
                response = future.result()
                if (response.status == GoalStatus.STATUS_ABORTED
                        and response.result.message == 'Downstream Action server rejected the goal'
                        and not response.result.result_yaml):
                    # Preserve the former direct-Action retry while Agent finishes
                    # the preceding conversation. Do not treat an aborted or
                    # preempted accepted conversation as a retry or user silence.
                    self.next_attempt = self.clock() + 1.0
                elif response.status != GoalStatus.STATUS_SUCCEEDED:
                    self.coordinator.fail(request)
                else:
                    result = yaml.safe_load(response.result.result_yaml)
                    self.coordinator.complete(
                        request, situation_assessment=result['situation_assessment'],
                        help_needed=result['help_needed'])
            except Exception:
                self.coordinator.fail(request)
            self.request = self.handle = self.goal_future = None
            self.accepted_at = self.server_missing_since = None
            self._drive()

    def close(self):
        with self.lock:
            if self.coordinator.closed:
                return
            self.coordinator.close()
            self.timer.cancel()
            return self._cancel_current()

    def destroy(self):
        self.close()
        self.client.destroy()
        self.agent_presence.destroy()
