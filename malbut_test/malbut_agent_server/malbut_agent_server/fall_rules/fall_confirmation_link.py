"""Route fall confirmations through the generic mission manager."""

import json
import math
from threading import RLock
import time

import yaml

from .fall_confirmation import FallConfirmationCoordinator


EXECUTE_MISSION_ACTION = '/malbut/mission/execute'
FALL_MISSIONS = ('fall_confirmation', 'fall_approach')
# Work kinds that are a fall_approach drive, not a conversation.
DRIVES = ('approach', 'view', 'revisit')
MANUAL_DRIVE = 'manual_drive'
# "낙상 확인 중" for the map screen and the robot's own move guard (malbut_bringup
# web_panel): every second and on change; silent for 3 s means not checking.
CHECK_STATUS_TOPIC = '/malbut/falls/check/status'
STATUS_PERIOD_S = 1.0
# The Manager's own words (mission_scheduler, mission_executor) for a refusal or
# an interruption that passes by itself: the same step goes again a second later.
WAIT_MESSAGES = frozenset({
    'manual recovery is in progress', 'system manager is still booting',
    'emergency stop is active', 'foreground missions are blocked while recharging',
    'localization is switching; retry after it settles',
    'localization is ERROR; retry after it settles',
    'mission preempted by a replacement request', 'system manager is shutting down',
})
WAIT_PREFIXES = ('conflicting mission ', 'superseded before execution by ')
# A question's Agent is busy with another conversation. A drive's server
# refusing its goal is not waited for: that refusal does not pass by itself.
DOWNSTREAM_REJECTED = 'Downstream Action server rejected the goal'
NO_MAP = 'select a saved map first; only mapping is available'
# No conversation server: speech is off (or failed at start). Waiting cannot help.
SPEECH_UNAVAILABLE = 'Action server /malbut/agent/confirm_situation is unavailable'
# Only part of the body was seen from 1 m (2026-10-11): ask the drive to move
# back to where the 12 cm camera sees a seated person's head (about 2.5 m).
VIEW_STANDOFF_M = 2.5


class FallConfirmationLink:
    """Serialize incident conversations and cancel superseded requests.

    An uncertain suspicion first runs a `fall_approach` mission (drive 1 m in
    front, facing it); a check that found no person drives back instead of
    asking. These share the one-at-a-time queue with the conversations.

    The missions are NORMAL (2026-10-12): 직접 움직이기 (HIGH) refuses or ends
    them, and other jobs may too. Such a step waits and goes again; the
    coordinator counts only that waiting.
    """

    # Defaults for links built without __init__ (tests) and for the approach.
    mode = 'confirm'
    approach_timeout_s = 90.0  # 60 s drive limit plus Manager and planner slack.
    return_job = return_future = return_handle = None
    return_sent_at = 0.0
    running = False  # The Manager reported the current step RUNNING.
    status_publisher = None
    last_status = None
    status_at = -math.inf

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
        self.status_publisher = node.create_publisher(String, CHECK_STATUS_TOPIC, 10)
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

    def _publish_status(self, *, force=False):
        if self.status_publisher is None:
            return
        status = self.coordinator.status(
            current=self.mode if self.request is not None else None,
            returning=self.return_job is not None)
        now = self.clock()
        # Ticks come every 0.5 s: a little slack keeps the cadence at one second.
        if (not force and status == self.last_status
                and now - self.status_at < STATUS_PERIOD_S - 0.1):
            return
        self.last_status, self.status_at = status, now
        self.status_publisher.publish(self.message_type(data=json.dumps(status, allow_nan=False)))

    def _manual_driving(self):
        state = self.manager_state
        return state is not None and any(
            mission.capability_id == MANUAL_DRIVE
            for mission in (*state.active_foreground_missions, *state.pending_missions))

    def _wait_reason(self, message, result_yaml):
        """'manual_drive' | 'busy' for a step to send again, None for a final result."""
        message = message or ''
        downstream = message == DOWNSTREAM_REJECTED and not result_yaml and self.mode not in DRIVES
        if not (downstream or message in WAIT_MESSAGES or message.startswith(WAIT_PREFIXES)):
            return None
        if message.endswith('higher priority HIGH'):
            return MANUAL_DRIVE  # 직접 움직이기 is the only HIGH mission.
        if message.startswith(('mission preempted', 'superseded')) and self._manual_driving():
            return MANUAL_DRIVE
        return 'busy'

    def _wait(self, why):
        """Send the same step again a second later; the coordinator counts the wait."""
        now = self.clock()
        self.coordinator.wait(now, why)
        self.next_attempt = now + 1.0
        self._publish()

    def _feedback(self, request, message):
        with self.lock:
            if (self.request != request or self.running or self.coordinator.closed
                    or getattr(message.feedback, 'state', '') != 'RUNNING'):
                return
            # The Manager started this step: it is no longer waiting.
            self.running = True
            self.coordinator.resume(self.clock())
            if self.mode == 'revisit':
                self.coordinator.revisit_started(request)
            self._publish()
            self._publish_status(force=True)

    def on_event(self, message):
        with self.lock:
            self.coordinator.receive(message.data)
            self._drive()

    def tick(self):
        with self.lock:
            self._drive()

    def on_state(self, message):
        with self.lock:
            if self.manager_state is None:
                self.next_attempt = 0.0  # The Manager is up: send now, not a second later.
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
                    self.mode in DRIVES or self.agent_presence.server_is_ready()):
                self.server_missing_since = None
            elif self.server_missing_since is None:
                self.server_missing_since = now
            limit = self.approach_timeout_s if self.mode in DRIVES else self.result_timeout_s
            expired = (
                now - self.accepted_at >= limit
                or (self.server_missing_since is not None
                    and now - self.server_missing_since >= self.server_loss_timeout_s))
        if expired:
            if self.goal_future is not None:
                # The Manager did not answer: busy, send it again.
                self._cancel_current()
                self._wait('busy')
                return expired
            if self.mode in DRIVES:
                # Not there in time: ask from wherever the robot is.
                self._drive_done(self.request, 'timeout')
            else:
                # The Agent went away or never finished: alert, do not wait.
                self.coordinator.fail(self.request, 'question_error')
            self._cancel_current()
            self._publish()
        return expired

    def _drive(self):
        self._advance()
        if not self.coordinator.closed:
            self._publish_status()

    def _advance(self):
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
        now = self.clock()
        self.coordinator.check_waits(now)
        self._publish()
        if now < self.next_attempt:
            return
        state = self.manager_state
        if not self.client.server_is_ready() or state is None:
            if self.coordinator.requests:
                self._wait('busy')  # No Manager yet (starting) or gone.
            return
        if any(mission.capability_id in FALL_MISSIONS
               for mission in (*state.active_foreground_missions,
                               *state.active_background_missions,
                               *state.pending_missions, *state.suspended_missions)):
            # After an Agent restart, an earlier confirmation may still
            # run in the manager. Wait, then recover the Agent's cached result;
            # resubmitting immediately would preempt that same conversation.
            return
        work = self.coordinator.next_work(now)
        if work is None:
            return
        kind, request = work
        if kind == 'return':
            self._send_return(request)
            return
        if self._manual_driving():
            # 직접 움직이기 refuses it anyway (HIGH): wait without asking every second.
            self._wait(MANUAL_DRIVE)
            return
        self.request, self.mode, self.running = request, kind, False
        self.sent_at = now
        if kind in DRIVES:
            x, y = request.revisit_target if kind == 'revisit' else request.approach_target
            # Going back after waiting is the same 1 m look as the first approach.
            capability, arguments = 'fall_approach', dict(
                request_id=request.request_id, phase='view' if kind == 'view' else 'approach',
                x=x, y=y, standoff_m=VIEW_STANDOFF_M if kind == 'view' else 1.0)
        else:
            capability, arguments = 'fall_confirmation', dict(
                request_id=request.request_id, situation_type='fall', summary=request.summary)
        try:
            future = self.client.send_goal_async(self.action_type.Goal(
                capability_id=capability,
                arguments_yaml=json.dumps(arguments, ensure_ascii=False)),
                feedback_callback=lambda message: self._feedback(request, message))
            self.goal_future = future
            future.add_done_callback(lambda done: self._accepted(request, done))
        except Exception:
            self._give_up(request)

    def _drive_done(self, request, outcome):
        if self.mode == 'view':
            self.coordinator.view_done(request, outcome, self.clock())
        elif self.mode == 'revisit':
            self.coordinator.revisit_done(request, outcome, self.clock())
        else:
            self.coordinator.approach_done(request, outcome, self.clock())

    def _give_up(self, request):
        """A transport failure: a confirmation fails; a drive asks in place."""
        self.coordinator.resume(self.clock())
        if self.mode in DRIVES:
            self._drive_done(request, 'failed')
        else:
            self.coordinator.fail(request, 'question_error')
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
                # Manager may still be preparing the robot. Keep
                # this request queued; rejection is not the user's silence.
                self.request = None
                self._wait('busy')
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
                message = response.result.message
                why = (None if response.status == GoalStatus.STATUS_SUCCEEDED
                       else self._wait_reason(message, response.result.result_yaml))
                if why is not None:
                    # Refused or interrupted by 직접 움직이기 or another job: the
                    # same step goes again. Not the user's silence, not a failure.
                    self._wait(why)
                else:
                    self.coordinator.resume(self.clock())
                    if self.mode in DRIVES:
                        outcome = 'no_map' if message == NO_MAP else 'failed'
                        if response.status == GoalStatus.STATUS_SUCCEEDED:
                            outcome = (yaml.safe_load(response.result.result_yaml) or {}).get(
                                'outcome', 'failed')
                        self._drive_done(request, outcome)
                    elif response.status != GoalStatus.STATUS_SUCCEEDED:
                        speech_off = message == SPEECH_UNAVAILABLE
                        self.coordinator.fail(
                            request, 'speech_unavailable' if speech_off else 'question_error')
                    else:
                        result = yaml.safe_load(response.result.result_yaml)
                        self.coordinator.complete(
                            request, situation_assessment=result['situation_assessment'],
                            help_needed=result['help_needed'])
            except Exception:
                self.coordinator.resume(self.clock())
                if self.mode in DRIVES:
                    self._drive_done(request, 'failed')
                else:
                    self.coordinator.fail(request, 'question_error')
            self.request = self.handle = self.goal_future = None
            self.accepted_at = self.server_missing_since = None
            self._drive()

    # ------------------------------------------------------------ return trip

    def _send_return(self, job):
        self.return_job, self.return_sent_at = job, self.clock()
        try:
            future = self.client.send_goal_async(self.action_type.Goal(
                capability_id='fall_approach', arguments_yaml=json.dumps(dict(
                    request_id=job.question_id, phase='return', x=0.0, y=0.0, standoff_m=1.0,
                    reason=job.reason))))
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
            try:
                self._publish_status(force=True)  # Not checking any more: unlock moves now.
            except Exception:
                pass
            return self._cancel_current()

    def destroy(self):
        self.close()
        self.client.destroy()
        self.agent_presence.destroy()
