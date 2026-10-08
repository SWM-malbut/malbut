"""Route fall confirmations through the generic mission manager."""

import json
import math
from threading import RLock
import time

import yaml

from .fall_confirmation import FallConfirmationCoordinator


EXECUTE_MISSION_ACTION = '/malbut/mission/execute'
FALL_MISSIONS = ('fall_confirmation', 'fall_approach')


class FallConfirmationLink:
    """Serialize incident conversations and cancel superseded requests.

    An uncertain suspicion first runs a `fall_approach` mission (drive 1 m in
    front, facing it); a check that found no person drives back instead of
    asking. These share the one-at-a-time queue with the conversations.
    """

    # Defaults for links built without __init__ (tests) and for the approach.
    mode = 'confirm'
    approach_timeout_s = 90.0  # 60 s drive limit plus Manager and planner slack.
    return_job = return_future = return_handle = None
    return_sent_at = 0.0

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
        self.canceling_requests = set()
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

    def _cancel_current(self, *, wait_for_result=False):
        handle = self.handle
        pending = self.goal_future
        request = self.request
        if wait_for_result and request is not None:
            if not hasattr(self, 'canceling_requests'):
                self.canceling_requests = set()
            self.canceling_requests.add(request.request_id)
        self.request = self.handle = self.goal_future = None
        self.accepted_at = self.server_missing_since = None
        if handle is not None:
            if wait_for_result:
                self._wait_for_canceled_result(request, handle)
            return self._cancel_handle(handle)
        return pending

    def _wait_for_canceled_result(self, request, handle):
        # A cancellation ACK is only acceptance, not proof that speech stopped.
        # Keep the next conversation queued until this mission actually ends.
        try:
            handle.get_result_async().add_done_callback(
                lambda done: self._canceled_done(request, done))
        except Exception:
            self.node.get_logger().warning('confirmation_merge_cancel_unconfirmed')

    def _canceled_done(self, request, future):
        with self.lock:
            try:
                if future.result().status not in (4, 5, 6):
                    return
            except Exception:
                self.node.get_logger().warning('confirmation_merge_cancel_unconfirmed')
                return
            self.canceling_requests.discard(request.request_id)
            self._drive()

    def _cancel_handle(self, handle):
        if handle is not None:
            try:
                return handle.cancel_goal_async()
            except Exception:
                self.node.get_logger().warning('confirmation_cancel_transport_failed')

    def _expire_current(self):
        """Enforce the same deadline before timer and future callbacks advance state."""
        now = self.clock()
        expired = False
        if self.goal_future is not None:
            expired = now - self.sent_at >= self.goal_response_timeout_s
        elif self.handle is not None:
            if self.client.server_is_ready() and (
                    self.mode == 'approach' or self.agent_presence.server_is_ready()):
                self.server_missing_since = None
            elif self.server_missing_since is None:
                self.server_missing_since = now
            limit = self.approach_timeout_s if self.mode == 'approach' else self.result_timeout_s
            expired = (
                now - self.accepted_at >= limit
                or (self.server_missing_since is not None
                    and now - self.server_missing_since >= self.server_loss_timeout_s))
        if expired:
            if self.mode == 'approach':
                # Not there in time: ask from wherever the robot is.
                self.coordinator.approach_done(self.request, 'timeout', now)
            else:
                self.coordinator.fail(self.request)
            self._cancel_current()
            self._publish()
        return expired

    def _drive(self):
        if self.coordinator.closed:
            return
        self._publish()
        if self.return_job is not None:
            self._expire_return()
            return
        if (self.request is not None
                and self.coordinator.requests.get(self.request.request_id) != self.request):
            terminal_scene = (self.request.subject_key is None and
                self.coordinator.terminal_revisions.get(self.request.incident_id, 0)
                >= self.request.revision)
            self._cancel_current(wait_for_result=terminal_scene)
        if self.request is not None:
            self._expire_current()
            return
        if getattr(self, 'canceling_requests', None):
            return
        if self.clock() < self.next_attempt or not self.client.server_is_ready():
            return
        state = self.manager_state
        if state is None or any(
                mission.capability_id in FALL_MISSIONS
                for mission in (*state.active_foreground_missions,
                                *state.active_background_missions,
                                *state.pending_missions, *state.suspended_missions)):
            # After a coordinator restart, an earlier confirmation may still
            # run in the manager. Wait, then recover the Agent's cached result;
            # resubmitting immediately would preempt that same conversation.
            return
        work = self.coordinator.next_work(self.clock())
        if work is None:
            return
        kind, request = work
        if kind == 'return':
            self._send_return(request)
            return
        self.request, self.mode = request, kind
        self.sent_at = self.clock()
        if kind == 'approach':
            x, y = request.approach_target
            capability, arguments = 'fall_approach', dict(
                request_id=request.request_id, phase='approach', x=x, y=y, standoff_m=1.0)
        else:
            capability, arguments = 'fall_confirmation', dict(
                request_id=request.request_id, situation_type='fall', summary=request.summary)
        try:
            future = self.client.send_goal_async(self.action_type.Goal(
                capability_id=capability,
                arguments_yaml=json.dumps(arguments, ensure_ascii=False)))
            self.goal_future = future
            future.add_done_callback(lambda done: self._accepted(request, done))
        except Exception:
            self._give_up(request)

    def _give_up(self, request):
        """A transport failure: a confirmation fails; an approach asks in place."""
        if self.mode == 'approach':
            self.coordinator.approach_done(request, 'failed', self.clock())
        else:
            self.coordinator.fail(request)
        self._cancel_current()
        self._publish()

    def _accepted(self, request, future):
        with self.lock:
            if self.request == request and self.goal_future is future:
                self._expire_current()
            try:
                handle = future.result()
            except Exception:
                if self.request == request and self.goal_future is future:
                    self._give_up(request)
                return
            if (self.request != request or self.coordinator.closed
                    or self.goal_future is not future
                    or self.coordinator.requests.get(request.request_id) != request):
                if handle.accepted:
                    if request.request_id in getattr(self, 'canceling_requests', ()):
                        self._wait_for_canceled_result(request, handle)
                    self._cancel_handle(handle)
                elif request.request_id in getattr(self, 'canceling_requests', ()):
                    self.canceling_requests.discard(request.request_id)
                    self._drive()
                return
            self.goal_future = None
            if not handle.accepted:
                if self.mode == 'approach':
                    # No map, localization switching or a busy robot: ask here.
                    self.coordinator.approach_done(request, 'rejected', self.clock())
                    self.request = None
                    self._publish()
                    return
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
                self._give_up(request)

    def _done(self, request, future):
        from action_msgs.msg import GoalStatus

        with self.lock:
            if self.request != request or self.coordinator.closed:
                return
            if self._expire_current():
                return
            try:
                response = future.result()
                if self.mode == 'approach':
                    outcome = 'failed'
                    if response.status == GoalStatus.STATUS_SUCCEEDED:
                        outcome = (yaml.safe_load(response.result.result_yaml) or {}).get(
                            'outcome', 'failed')
                    self.coordinator.approach_done(request, outcome, self.clock())
                elif (response.status == GoalStatus.STATUS_ABORTED
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
                if self.mode == 'approach':
                    self.coordinator.approach_done(request, 'failed', self.clock())
                else:
                    self.coordinator.fail(request)
            self.request = self.handle = self.goal_future = None
            self.accepted_at = self.server_missing_since = None
            self._drive()

    # ------------------------------------------------------------ return trip

    def _send_return(self, job):
        self.return_job, self.return_sent_at = job, self.clock()
        try:
            future = self.client.send_goal_async(self.action_type.Goal(
                capability_id='fall_approach', arguments_yaml=json.dumps(dict(
                    request_id=job.question_id, phase='return', x=0.0, y=0.0, standoff_m=1.0))))
            self.return_future = future
            future.add_done_callback(lambda done: self._return_accepted(job, done))
        except Exception:
            self._finish_return(job, 'failed')

    def _return_accepted(self, job, future):
        with self.lock:
            if self.return_job != job:
                return
            try:
                handle = future.result()
            except Exception:
                return self._finish_return(job, 'failed')
            if not handle.accepted:
                return self._finish_return(job, 'failed')
            self.return_handle, self.return_future = handle, None
            try:
                handle.get_result_async().add_done_callback(
                    lambda done: self._return_done(job, done))
            except Exception:
                self._finish_return(job, 'failed')

    def _return_done(self, job, future):
        from action_msgs.msg import GoalStatus

        with self.lock:
            if self.return_job != job:
                return
            outcome = 'failed'
            try:
                response = future.result()
                if response.status == GoalStatus.STATUS_SUCCEEDED:
                    outcome = (yaml.safe_load(response.result.result_yaml) or {}).get(
                        'outcome', 'failed')
            except Exception:
                pass
            self._finish_return(job, outcome)

    def _expire_return(self):
        limit = (self.goal_response_timeout_s if self.return_handle is None
                 else self.approach_timeout_s)
        if self.clock() - self.return_sent_at >= limit:
            self._cancel_handle(self.return_handle)
            self._finish_return(self.return_job, 'failed')

    def _finish_return(self, job, outcome):
        self.coordinator.return_done(job, outcome)
        self.return_job = self.return_future = self.return_handle = None
        self._publish()
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
