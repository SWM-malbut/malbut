"""Run the one fall check (FallFlow) through the generic mission manager (SWM25-261).

With Bringup ``fall_flow`` on, this replaces FallConfirmationLink. The VLM's
sightings and Cloud answers go into FallFlow; its commands come out as decisions
for the VLM (a Cloud look, a record, a notification, a clip link) or as Manager
missions (``fall_approach`` drives and holds, ``fall_confirmation`` questions).
How a mission is sent, waited for and held is unchanged from that link: HIGH
missions, 직접 움직이기 (URGENT) and other jobs make a step wait and go again a
second later, a drive's outcome may come as feedback while it holds the wheels,
and a busy Agent gets a hold until it can ask. FallFlow decides everything else.
"""

import json
import math
from threading import RLock
import time
from uuid import uuid4

import yaml

from ..fall_check_watch import VOICE_KINDS, VOICE_TOPIC
from .fall_confirmation import VIDEO_SUMMARIES
from .fall_confirmation_link import (
    CHECK_STATUS_TOPIC, DOWNSTREAM_REJECTED, EXECUTE_MISSION_ACTION, FALL_MISSIONS,
    MANUAL_DRIVE, NO_MAP, SPEECH_UNAVAILABLE, STATUS_PERIOD_S, WAIT_MESSAGES, WAIT_PREFIXES,
)
from .fall_flow import Body, FallFlow, Incident, Sighting

# A Cloud look not answered by then (a VLM restart lost it): treat it as failed.
LOOK_TIMEOUT_S = 90.0
# Until the VLM answers flow_hello (it may start later), ask again this often.
HELLO_EVERY_S = 5.0
# Looks (moving to see) and asks, for the map screen (목업 27·32번).
STEP_PHASE = {'queued': 'approach', 'approach': 'approach', 'reverify': 'approach',
              'view': 'approach', 'closer': 'approach', 'compare': 'look',
              'departure': 'look', 'posture': 'look', 'ask': 'ask', 'wait': 'wait'}
SIGHTING_KINDS = ('low', 'lying', 'motion', 'cloud', 'up', 'seen')
VERDICTS = {
    'filter': ('suspect', 'not_person', 'reflection', 'ordinary', 'failed', 'guard'),
    'arrival': ('same_person', 'person_unsure', 'different_person', 'same_object', 'gone',
                'not_visible', 'failed', 'guard'),
    'departure': ('animal_left', 'person_walked', 'person_crawled', 'not_seen', 'failed',
                  'guard'),
    'posture': ('furniture', 'standing', 'floor_active', 'floor', 'unclear', 'failed', 'guard'),
}


def _text(value, limit=200):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError('invalid text')
    return value


def _pair(value):
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in value)):
        raise ValueError('invalid point')
    return (float(value[0]), float(value[1]))


def _box(value):
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1
                   for v in value)):
        raise ValueError('invalid box')
    return tuple(float(v) for v in value)


def parse_sighting(data):
    """A VLM sighting message (fall_flow_sensor) as FallFlow's Sighting, or ValueError."""
    if not isinstance(data, dict):
        raise ValueError('invalid sighting')
    at, kind, view = data.get('at'), data.get('kind'), data.get('view')
    if (isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at)
            or kind not in SIGHTING_KINDS
            or (view is not None and (type(view) is not int or view < 0))
            or data.get('on_floor') not in (None, True, False)):
        raise ValueError('invalid sighting')
    sighting_id = _text(data.get('sighting_id'))
    if sighting_id is None:
        raise ValueError('invalid sighting')
    return Sighting(
        sighting_id, float(at), kind, track=_text(data.get('track')),
        point=_pair(data.get('point')), box=_box(data.get('box')), view=view,
        robot=_pair(data.get('robot')), on_floor=data.get('on_floor'),
        photo=_text(data.get('photo')), fall_moment=_text(data.get('fall_moment')))


def restored_incident(data, now):
    """An open incident from flow_ready, checked again from the start after a restart."""
    first = parse_sighting(data.get('first'))
    round_ = data.get('check_round', 1)
    if type(round_) is not int or not 1 <= round_ <= 10000:
        raise ValueError('invalid check round')
    moments = [_text(m) for m in data.get('fall_moments') or [] if m][:2]
    return Incident(_text(data.get('incident_id')), first, Body.of(first), now,
                    fall_moments=moments, check_round=round_)


class FallFlowLink:
    """FallFlow's commands as VLM decisions and Manager missions, one step at a time."""

    approach_timeout_s = 90.0  # 60 s drive limit plus Manager and planner slack.

    def __init__(self, node, *, runtime_id='', goal_response_timeout_s=5.0,
                 result_timeout_s=610.0, server_loss_timeout_s=5.0, clock=time.monotonic):
        from rclpy.action import ActionClient
        from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
        from rclpy.clock import Clock, ClockType
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from malbut_interfaces.action import ConfirmSituation, ExecuteMission
        from malbut_interfaces.msg import SystemState
        from std_msgs.msg import String

        for value in (goal_response_timeout_s, result_timeout_s, server_loss_timeout_s):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not (
                    math.isfinite(value) and value > 0):
                raise ValueError('confirmation transport timeouts must be positive and finite')
        self.node, self.runtime_id = node, runtime_id
        self.goal_response_timeout_s = goal_response_timeout_s
        self.result_timeout_s = result_timeout_s
        self.server_loss_timeout_s = server_loss_timeout_s
        self.action_type, self.message_type = ExecuteMission, String
        self.group = MutuallyExclusiveCallbackGroup()
        self.client = ActionClient(node, ExecuteMission, EXECUTE_MISSION_ACTION,
                                   callback_group=self.group)
        # Discovery only: goals and cancellation always go through the Manager.
        self.agent_presence = ActionClient(
            node, ConfirmSituation, '/malbut/agent/confirm_situation', callback_group=self.group)
        self.decisions = node.create_publisher(String, '/malbut/falls/runtime/decision', 10)
        self.status_publisher = node.create_publisher(String, CHECK_STATUS_TOPIC, 10)
        self.init_state(clock)
        self.events = node.create_subscription(
            String, '/malbut/falls/runtime/events', self.on_event, 50, callback_group=self.group)
        self.voice = node.create_subscription(
            String, VOICE_TOPIC, self.on_voice, 10, callback_group=self.group)
        self.states = node.create_subscription(
            SystemState, '/malbut/state', self.on_state,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                       reliability=ReliabilityPolicy.RELIABLE), callback_group=self.group)
        self.timer = node.create_timer(
            0.5, self.tick, callback_group=self.group,
            clock=Clock(clock_type=ClockType.STEADY_TIME))

    def init_state(self, clock):
        self.lock = RLock()
        self.clock = clock
        self.flow = FallFlow()
        self.closed = False
        self.step = None          # the Manager step to run: drive or ask
        self.goal_future = self.handle = None
        self.sent_at, self.accepted_at, self.server_missing_since = 0.0, None, None
        self.running = False
        self.held = None          # (step, goal handle) keeping the wheels between steps
        self.next_attempt = 0.0
        self.manager_state = None
        self.looks = {}           # look_id -> (what, sighting or incident id, sent at)
        self.ready = False
        self.hello_at = -math.inf
        self.last_status, self.status_at = None, -math.inf

    # ------------------------------------------------------------ inputs

    def on_event(self, message):
        with self.lock:
            try:
                data = json.loads(message.data) if len(message.data) <= 131072 else None
            except (TypeError, ValueError):
                data = None
            if not isinstance(data, dict) or (
                    self.runtime_id and data.get('runtime_id') != self.runtime_id):
                return
            try:
                self._receive(data, self.clock())
            except (ValueError, TypeError, KeyError):
                self.node.get_logger().warning('fall_flow_event_rejected')
            self._run()

    def _receive(self, data, now):
        kind = data.get('kind')
        if kind == 'fall_sighting':
            self.flow.sighted(parse_sighting(data.get('sighting')), now)
        elif kind == 'fall_look_result':
            look_id = _text(data.get('look_id'))
            sent = self.looks.pop(look_id, None)
            what = data.get('what')
            if what not in VERDICTS or data.get('verdict') not in VERDICTS[what]:
                raise ValueError('invalid look result')
            if sent is not None and sent[0] != what:
                raise ValueError('look result for another look')
            self._answer(what, data, now)
        elif kind == 'flow_ready':
            if data.get('flow') is not True:
                self.node.get_logger().error('fall_flow_switch_mismatch')
                return
            first_ready = not self.ready
            self.ready = True
            if first_ready:
                incidents = []
                for item in data.get('open_incidents') or []:
                    try:
                        incidents.append(restored_incident(item, now))
                    except (ValueError, TypeError, AttributeError):
                        continue
                self.flow.restore(incidents, now)
                for item in sorted(data.get('recent') or [],
                                   key=lambda s: s.get('at', 0) if isinstance(s, dict) else 0):
                    try:
                        self.flow.sighted(parse_sighting(item), now)
                    except (ValueError, TypeError):
                        continue
        elif kind == 'guardian_closed':
            self.flow.guardian_closed(_text(data.get('incident_id')), now)

    def _answer(self, what, data, now, *, verdict=None):
        verdict = verdict or data['verdict']
        look_id, reason = _text(data.get('look_id')), _text(data.get('reason'), 80)
        if what == 'filter':
            self.flow.filter_answered(_text(data.get('sighting_id')), verdict, now,
                                      look_id=look_id, reason=reason)
        elif what == 'arrival':
            self.flow.arrival_answered(_text(data.get('incident_id')), verdict, now,
                                       look_id=look_id, reason=reason)
        elif what == 'departure':
            self.flow.departure_answered(_text(data.get('incident_id')), verdict, now,
                                         look_id=look_id, reason=reason)
        else:
            earlier = data.get('earlier')
            self.flow.posture_answered(
                _text(data.get('incident_id')), verdict,
                earlier if earlier in ('fall', 'self', 'unknown') else None, now,
                look_id=look_id, reason=reason)

    def on_voice(self, message):
        """"도와줘" or "그만" heard during the check (2026-10-13)."""
        try:
            kind = json.loads(message.data).get('kind')
        except (AttributeError, TypeError, ValueError):
            return
        if kind not in VOICE_KINDS:
            return
        with self.lock:
            self.flow.voice(kind, self.clock())
            self._run()

    def on_state(self, message):
        with self.lock:
            if self.manager_state is None:
                self.next_attempt = 0.0  # The Manager is up: send now, not a second later.
            self.manager_state = message
            waiting = self.flow.status().get('waiting_for')
            if waiting == MANUAL_DRIVE and not self._manual_driving():
                self.next_attempt = 0.0  # 직접 움직이기 ended: take the wheels back now.
            self._run()

    def tick(self):
        with self.lock:
            now = self.clock()
            if not self.ready and now - self.hello_at >= HELLO_EVERY_S:
                self.hello_at = now
                self._decide(dict(action='flow_hello', flow=True))
            for look_id, (what, key, sent_at) in tuple(self.looks.items()):
                if now - sent_at >= LOOK_TIMEOUT_S:
                    del self.looks[look_id]
                    field = 'sighting_id' if what == 'filter' else 'incident_id'
                    self._answer(what, {'look_id': look_id, field: key, 'reason': 'look_timeout'},
                                 now, verdict='failed')
            self.flow.tick(now)
            self._run()

    # ------------------------------------------------------------ FallFlow's commands

    def _decide(self, data):
        self.decisions.publish(self.message_type(data=json.dumps(data, allow_nan=False)))

    def _run(self):
        if self.closed:
            return
        for command in self.flow.drain():
            self._execute(command)
        self._advance()
        if not self.closed:
            self._publish_status()

    def _execute(self, command):
        kind = command['kind']
        fields = {k: v for k, v in command.items() if k != 'kind'}
        if kind == 'look':
            key = fields.get('sighting_id') or fields.get('incident_id')
            self.looks[fields['look_id']] = (fields['what'], key, self.clock())
            self._decide(dict(action='look', **{
                k: (list(v) if isinstance(v, tuple) else v) for k, v in fields.items()}))
        elif kind in ('record', 'notify', 'link'):
            self._decide(dict(action=kind, **fields))
        elif kind in ('drive', 'ask'):
            self._replace(dict(command))
        elif kind == 'stop':
            self._replace(None)

    def _replace(self, step):
        """The next step replaces the one in flight; a stop just ends it."""
        if self.step is not None:
            self._cancel_current()
        self.step, self.running = step, False
        self.next_attempt = 0.0
        if step is None:
            self._release_held()

    # ------------------------------------------------------------ Manager missions

    def _manual_driving(self):
        state = self.manager_state
        return state is not None and any(
            mission.capability_id == MANUAL_DRIVE
            for mission in (*state.active_foreground_missions, *state.pending_missions))

    def _wait_reason(self, message, result_yaml):
        """'manual_drive' | 'busy' for a step to send again, None for a final result."""
        message = message or ''
        asking = self.step is not None and self.step['kind'] == 'ask'
        downstream = message == DOWNSTREAM_REJECTED and not result_yaml and asking
        if not (downstream or message in WAIT_MESSAGES or message.startswith(WAIT_PREFIXES)):
            return None
        if message.endswith('higher priority URGENT') and self._manual_driving():
            return MANUAL_DRIVE
        if message.startswith(('mission preempted', 'superseded')) and self._manual_driving():
            return MANUAL_DRIVE
        return 'busy'

    def _wait(self, why):
        """Send the same step again a second later; FallFlow counts the wait."""
        now = self.clock()
        if self.step is not None and self.step.get('purpose') != 'return':
            self.flow.blocked(self.step['incident_id'], why, now)
        self.next_attempt = now + 1.0
        self._run_commands()

    def _run_commands(self):
        for command in self.flow.drain():
            self._execute(command)

    def _advance(self):
        if self.step is None:
            if self.held is not None and not self.flow.status()['active']:
                self._release_held()  # The check is over: let the wheels go.
            return
        if self.goal_future is not None or self.handle is not None:
            self._expire_current()
            return
        now = self.clock()
        if now < self.next_attempt:
            return
        state = self.manager_state
        if not self.client.server_is_ready() or state is None:
            self._wait('busy')  # No Manager yet (starting) or gone.
            return
        held = self._held_mission_id()
        if any(mission.capability_id in FALL_MISSIONS
               and (held is None or getattr(mission, 'mission_id', None) != held)
               for mission in (*state.active_foreground_missions,
                               *state.active_background_missions,
                               *state.pending_missions, *state.suspended_missions)):
            # After an Agent restart an earlier fall mission may still run: wait for it.
            return
        step = self.step
        returning = step['kind'] == 'drive' and step['purpose'] == 'return'
        if self._manual_driving() and not returning:
            # 직접 움직이기 refuses it anyway (URGENT): wait without asking every second.
            self._wait(MANUAL_DRIVE)
            return
        self.held = None  # This step replaces the hold in the Manager (same priority).
        self.sent_at, self.running = now, False
        if step['kind'] == 'drive':
            target = step.get('target') or (0.0, 0.0)
            arguments = dict(request_id=step['incident_id'],
                             phase={'view': 'view', 'return': 'return'}.get(
                                 step['purpose'], 'approach'),
                             x=float(target[0]), y=float(target[1]),
                             standoff_m=float(step.get('standoff') or 1.0))
            if step.get('from_point') is not None:
                arguments.update(from_x=float(step['from_point'][0]),
                                 from_y=float(step['from_point'][1]))
            if returning:
                arguments['reason'] = step.get('reason') or 'off_floor'
            capability = 'fall_approach'
        else:
            # A fresh ID for every question: a later round is never an earlier one's answer.
            step['request_id'] = str(uuid4())
            capability, arguments = 'fall_confirmation', dict(
                request_id=step['request_id'], situation_type='fall',
                summary=VIDEO_SUMMARIES['observed_fall' if step.get('fall_seen')
                                        else 'suspected_fall'])
        try:
            future = self.client.send_goal_async(self.action_type.Goal(
                capability_id=capability,
                arguments_yaml=json.dumps(arguments, ensure_ascii=False)),
                feedback_callback=lambda message: self._feedback(step, message))
            self.goal_future = future
            future.add_done_callback(lambda done: self._accepted(step, done))
        except Exception:
            self._give_up(step)

    def _drive_done(self, step, outcome):
        self.flow.drive_done(step['incident_id'], step['purpose'], outcome, self.clock())

    def _asked(self, step, answer, **fields):
        self.flow.asked(step['incident_id'], answer, self.clock(), **fields)

    def _give_up(self, step):
        """A transport failure: a question fails; a drive goes on from where it is."""
        if step['kind'] == 'drive':
            self._drive_done(step, 'failed')
        else:
            self._asked(step, 'failed', reason='question_error')
        self._finish_step(step)

    def _finish_step(self, step):
        if self.step is step:
            self.step = None
        self.goal_future = self.handle = None
        self.accepted_at = self.server_missing_since = None
        self.running = False
        self._run_commands()

    def _expire_current(self):
        now = self.clock()
        step, expired = self.step, False
        if self.goal_future is not None:
            expired = now - self.sent_at >= self.goal_response_timeout_s
        elif self.handle is not None:
            drive = step['kind'] == 'drive'
            if self.client.server_is_ready() and (drive or self.agent_presence.server_is_ready()):
                self.server_missing_since = None
            elif self.server_missing_since is None:
                self.server_missing_since = now
            limit = self.approach_timeout_s if drive else self.result_timeout_s
            expired = (now - self.accepted_at >= limit
                       or (self.server_missing_since is not None
                           and now - self.server_missing_since >= self.server_loss_timeout_s))
        if not expired:
            return False
        if self.goal_future is not None:
            self._cancel_current()  # The Manager did not answer: busy, send it again.
            self._wait('busy')
            return True
        self._cancel_current()
        if step['kind'] == 'drive':
            self._drive_done(step, 'timeout')  # Not there in time: go on from here.
        else:
            self._asked(step, 'failed', reason='question_error')
        self._finish_step(step)
        return True

    def _feedback(self, step, message):
        with self.lock:
            if self.step is not step or getattr(message.feedback, 'state', '') != 'RUNNING':
                return
            if not self.running:
                self.running = True  # The Manager started this step: no longer waiting.
                if step.get('purpose') != 'return':
                    self.flow.resumed(step['incident_id'], self.clock())
                self._run_commands()
                self._publish_status(force=True)
            if step['kind'] != 'drive' or self.handle is None:
                return
            try:
                feedback = yaml.safe_load(message.feedback.feedback_yaml or '') or {}
            except (AttributeError, yaml.YAMLError):
                return
            outcome = feedback.get('outcome') if isinstance(feedback, dict) else None
            if not isinstance(outcome, str) or not outcome:
                return
            # The drive ended and holds the wheels: take the outcome now and keep the
            # mission until the next step replaces it.
            self.held = (step, self.handle)
            self.handle = None
            self._drive_done(step, outcome)
            self._finish_step(step)
            self._run()

    def _accepted(self, step, future):
        with self.lock:
            if self.step is step and self.goal_future is future:
                if self._expire_current():
                    return
            try:
                handle = future.result()
            except Exception:
                if self.step is step and self.goal_future is future:
                    self._give_up(step)
                    self._run()
                return
            if self.step is not step or self.goal_future is not future or self.closed:
                if handle.accepted:
                    self._cancel_handle(handle)  # Replaced meanwhile.
                return
            self.goal_future = None
            if not handle.accepted:
                self._wait('busy')  # The Manager may still be preparing the robot.
                self._run()
                return
            self.handle, self.accepted_at = handle, self.clock()
            self.server_missing_since = None
            try:
                handle.get_result_async().add_done_callback(
                    lambda done: self._done(step, done, handle))
            except Exception:
                self._give_up(step)
                self._run()

    def _done(self, step, future, handle):
        from action_msgs.msg import GoalStatus

        with self.lock:
            if self.held is not None and self.held[1] is handle:
                self.held = None  # The held mission ended by itself (직접 움직이기).
                self._run()
                return
            if self.step is not step or self.handle is not handle or self.closed:
                return
            if self._expire_current():
                self._run()
                return
            try:
                response = future.result()
                message = response.result.message
                why = (None if response.status == GoalStatus.STATUS_SUCCEEDED
                       else self._wait_reason(message, response.result.result_yaml))
                if why is not None and step.get('purpose') != 'return':
                    # Refused or interrupted by 직접 움직이기 or another job: the same
                    # step goes again. Not the user's silence, not a failure.
                    self.handle = None
                    self.accepted_at = self.server_missing_since = None
                    self._wait(why)
                    if message == DOWNSTREAM_REJECTED and step['kind'] == 'ask':
                        self._send_hold(step)  # The Agent cannot ask yet: hold the wheels.
                    self._run()
                    return
                if step['kind'] == 'drive':
                    outcome = 'no_map' if message == NO_MAP else 'failed'
                    if response.status == GoalStatus.STATUS_SUCCEEDED:
                        outcome = (yaml.safe_load(response.result.result_yaml) or {}).get(
                            'outcome', 'failed')
                    if step['purpose'] == 'return' and outcome != 'returned':
                        outcome = 'return_failed'
                    self._drive_done(step, outcome)
                elif response.status != GoalStatus.STATUS_SUCCEEDED:
                    self._asked(step, 'failed', reason=(
                        'speech_unavailable' if message == SPEECH_UNAVAILABLE
                        else 'question_error'))
                else:
                    result = yaml.safe_load(response.result.result_yaml)
                    self._asked(step, 'help' if result['help_needed'] else 'okay',
                                assessment=result.get('situation_assessment'))
            except Exception:
                if step['kind'] == 'drive':
                    self._drive_done(step, 'failed')
                else:
                    self._asked(step, 'failed', reason='question_error')
            self._finish_step(step)
            self._run()

    def _cancel_current(self):
        handle, pending = self.handle, self.goal_future
        self.goal_future = self.handle = None
        self.accepted_at = self.server_missing_since = None
        if handle is not None:
            return self._cancel_handle(handle)
        return pending

    def _cancel_handle(self, handle):
        if handle is not None:
            try:
                return handle.cancel_goal_async()
            except Exception:
                self.node.get_logger().warning('confirmation_cancel_transport_failed')

    def _held_mission_id(self):
        if self.held is None:
            return None
        try:
            return bytes(self.held[1].goal_id.uuid).hex()  # The Manager's mission ID.
        except Exception:
            return None

    def _release_held(self):
        held, self.held = self.held, None
        if held is not None:
            self._cancel_handle(held[1])

    def _send_hold(self, step):
        """Keep a HIGH mission while waiting to ask (the Agent refused the question)."""
        try:
            future = self.client.send_goal_async(self.action_type.Goal(
                capability_id='fall_approach', arguments_yaml=json.dumps(dict(
                    request_id=step['incident_id'], phase='hold', x=0.0, y=0.0,
                    standoff_m=1.0))))
        except Exception:
            return
        future.add_done_callback(lambda done: self._hold_accepted(step, done))

    def _hold_accepted(self, step, future):
        with self.lock:
            try:
                handle = future.result()
            except Exception:
                return
            if not handle.accepted:
                return  # Something above the fall check holds the wheels anyway.
            if (self.closed or self.step is not step or self.goal_future is not None
                    or self.handle is not None):
                self._cancel_handle(handle)  # The next step went out meanwhile.
                return
            self.held = (step, handle)
            try:
                handle.get_result_async().add_done_callback(
                    lambda done: self._done(step, done, handle))
            except Exception:
                pass

    # ------------------------------------------------------------ status

    def status(self):
        """What the robot does for falls now, for the map screen (목업 27·32번)."""
        flow = self.flow.status()
        returning = (self.step is not None and self.step['kind'] == 'drive'
                     and self.step['purpose'] == 'return')
        if returning:
            return dict(active=True, phase='return', waiting_for=None, alerted=False,
                        target=None, step='return', purpose=None, incident_id=None,
                        check_round=None)
        if not flow['active']:
            return dict(active=False, phase=None, waiting_for=None, alerted=False, target=None,
                        step=None, purpose=None, incident_id=None, check_round=None)
        target = flow['target']
        return dict(active=True, phase=STEP_PHASE.get(flow['step'], 'approach'),
                    waiting_for=flow['waiting_for'] if flow['step'] == 'wait' else None,
                    alerted=flow['alerted'],
                    target=dict(x=target[0], y=target[1]) if target is not None else None,
                    step=flow['step'], purpose=flow['purpose'],
                    incident_id=flow['incident_id'], check_round=flow['check_round'])

    def _publish_status(self, *, force=False):
        status = self.status()
        now = self.clock()
        if (not force and status == self.last_status
                and now - self.status_at < STATUS_PERIOD_S - 0.1):
            return
        self.last_status, self.status_at = status, now
        self.status_publisher.publish(self.message_type(data=json.dumps(status, allow_nan=False)))

    def close(self):
        with self.lock:
            if self.closed:
                return None
            self.closed = True
            self.timer.cancel()
            self.step = None
            try:
                self.last_status = None
                self.status_publisher.publish(self.message_type(data=json.dumps(dict(
                    active=False, phase=None, waiting_for=None, alerted=False, target=None,
                    step=None, purpose=None, incident_id=None, check_round=None))))
            except Exception:
                pass
            self._release_held()
            return self._cancel_current()

    def destroy(self):
        self.close()
        self.client.destroy()
        self.agent_presence.destroy()
