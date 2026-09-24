"""ROS Action bridge: fall runtime -> Manager -> conversation Agent -> Manager."""

import json
import math
from threading import RLock
import time

from .fall_confirmation import FallConfirmationCoordinator


CONFIRM_SITUATION_ACTION = '/malbut/agent/confirm_situation'


class FallConfirmationLink:
    """Serialize incident conversations and cancel superseded requests."""

    def __init__(self, node, *, runtime_id='', goal_response_timeout_s=5.0,
                 result_timeout_s=610.0, server_loss_timeout_s=5.0,
                 clock=time.monotonic):
        from rclpy.action import ActionClient
        from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
        from rclpy.clock import Clock, ClockType
        from malbut_interfaces.action import ConfirmSituation
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
        self.action_type, self.message_type = ConfirmSituation, String
        self.group = MutuallyExclusiveCallbackGroup()
        self.client = ActionClient(node, ConfirmSituation, CONFIRM_SITUATION_ACTION,
                                   callback_group=self.group)
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

    def _cancel_current(self):
        handle = self.handle
        self.request = self.handle = self.goal_future = None
        self.accepted_at = self.server_missing_since = None
        if handle is not None:
            try:
                handle.cancel_goal_async()
            except Exception:
                self.node.get_logger().warning('confirmation_cancel_transport_failed')

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
                if self.client.server_is_ready():
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
        request = next(iter(self.coordinator.requests.values()), None)
        if request is None:
            return
        self.request = request
        self.sent_at = self.clock()
        try:
            future = self.client.send_goal_async(self.action_type.Goal(
                request_id=request.request_id, situation_type='fall', summary=request.summary))
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
                # Agent may still be cancelling the previous incident. Keep
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
                if response.status != GoalStatus.STATUS_SUCCEEDED:
                    self.coordinator.fail(request)
                else:
                    self.coordinator.complete(
                        request, situation_assessment=response.result.situation_assessment,
                        help_needed=response.result.help_needed)
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
            self._cancel_current()

    def destroy(self):
        self.close()
        self.client.destroy()
