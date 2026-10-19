"""The unified fall check (SWM25-261) on the 18 cases walked through on 2026-10-17
(~/fall-refactor-cases-20261017.md) and on each design decision. Pure: a fake
clock, no ROS, no Cloud; these are unit tests, not robot checks."""

import math

import pytest

from malbut_agent_server.fall_rules.fall_flow import (
    FORGET_S, POSTURE_AGAIN_S, RECHECK_S, WAIT_ALERT_S, WAIT_LIMIT_S, FallFlow, Incident, Sighting,
)
from malbut_agent_server.fall_rules.person_tracker import SHORT_GONE_S, Frame, Observation

SPOT = (3.0, 0.0)
FROM = (0.0, 0.0)


class Robot:
    def __init__(self):
        self.flow = FallFlow()
        self.now = 0.0
        self._n = 0
        self.log = []

    def wait(self, seconds):
        self.now += seconds
        self.flow.tick(self.now)
        return self.take()

    def take(self):
        out = self.flow.drain()
        self.log.extend(out)
        return out

    def see(self, kind='low', **fields):
        self._n += 1
        fields.setdefault('point', SPOT)
        fields.setdefault('robot', FROM)
        fields.setdefault('photo', f'p{self._n}')
        s = Sighting(f's{self._n}', self.now, kind, **fields)
        self.flow.sighted(s, self.now)
        return s, self.take()

    def frame(self, *people, view=0):
        """A pose frame from the VLM (``fall_people``): who is in view, numbers only."""
        self.flow.people(Frame(self.now, view, tuple(people)), self.now)
        return self.take()

    def filter(self, s, *verdicts):
        out = []
        for i, verdict in enumerate(verdicts):
            if i:
                out += self.wait(3.0)
            self.flow.filter_answered(s.sighting_id, verdict, self.now)
            out += self.take()
        return out

    def open(self, kind='low', **fields):
        """A suspicion that passes the first filter; returns its incident id."""
        s, _ = self.see(kind, **fields)
        if kind != 'cloud':
            self.filter(s, 'suspect')
        return self.active()

    def active(self):
        return self.flow.status()['incident_id']

    def drive(self, purpose, outcome='arrived'):
        self.flow.drive_done(self.active(), purpose, outcome, self.now)
        return self.take()

    def arrival(self, verdict):
        self.flow.arrival_answered(self.active(), verdict, self.now)
        return self.take()

    def posture(self, *answers):
        out = []
        for i, (verdict, earlier) in enumerate(answers):
            if i:
                out += self.wait(POSTURE_AGAIN_S)
            self.flow.posture_answered(self.active(), verdict, earlier, self.now)
            out += self.take()
        return out

    def ask(self, answer):
        incident = self.active()
        self.flow.asked(incident, answer, self.now)
        return self.take()

    def only(self):
        incident, = self.flow.incidents.values()
        return incident

    def to_posture(self, compare='same_person'):
        """Approach, compare on arrival, move for the whole body."""
        self.drive('approach')
        self.arrival(compare)
        return self.drive('view')


def person(track='t1', point=SPOT, box=None, state='unknown'):
    return Observation(track, box, point, state)


def kinds(commands, kind):
    return [c for c in commands if c['kind'] == kind]


def looks(commands, what=None):
    return [c for c in kinds(commands, 'look') if what is None or c['what'] == what]


def drives(commands, purpose=None):
    return [c for c in kinds(commands, 'drive') if purpose is None or c['purpose'] == purpose]


def notices(commands):
    return [(c['reason'], c['level']) for c in kinds(commands, 'notify')]


def records(commands, event):
    return [c['reason'] for c in kinds(commands, 'record') if c['event'] == event]


# ------------------------------------------------------------ the 18 cases


def test_case01_a_bag_is_dropped_twice_and_then_remembered_by_its_track():
    robot = Robot()
    s, out = robot.see(track='bag-1')
    assert looks(out, 'filter') and looks(out)[0]['ordinary_drops'] is True
    out = robot.filter(s, 'not_person', 'not_person')
    assert looks(out, 'filter') and records(out, 'candidate_dropped') == ['not_person']
    assert robot.active() is None
    _, out = robot.see(track='bag-1')
    assert out == []  # The same track is not filtered again.


def test_case01_a_bag_that_passes_the_filter_is_closed_on_arrival_as_the_same_object():
    robot = Robot()
    robot.open(track='bag-1')
    robot.drive('approach')
    out = robot.arrival('same_object')
    assert records(out, 'incident_resolved') == ['not_a_person']
    assert drives(out, 'return')  # Closed without a question: go back.


def test_case02_a_fall_seen_on_the_floor_is_asked_even_when_cloud_says_self():
    robot = Robot()
    s, out = robot.see('motion', track='t1', fall_moment='fall-1')
    assert looks(out, 'filter')[0]['ordinary_drops'] is False  # Never dropped as ordinary.
    robot.filter(s, 'suspect')
    out = robot.to_posture()
    assert looks(out, 'posture')[0]['fall_moments'] == ('fall-1',)
    out = robot.posture(('floor_active', 'self'))
    assert drives(out, 'closer')  # Decision A: on the floor after a fall, always ask.
    out = robot.drive('closer')
    assert kinds(out, 'ask')


def test_case02_after_a_fall_up_again_is_closed_only_when_cloud_saw_a_controlled_move():
    asked = Robot()
    asked.open('motion', track='t1', fall_moment='fall-1')
    asked.to_posture()
    assert drives(asked.posture(('standing', 'fall')), 'closer')
    closed = Robot()
    closed.open('motion', track='t1', fall_moment='fall-1')
    closed.to_posture()
    out = closed.posture(('furniture', 'self'), ('furniture', 'self'))
    assert records(out, 'incident_resolved') == ['off_floor_verified']


def test_case03_an_object_is_remembered_while_in_view_and_filtered_again_once_out_of_it():
    # SWM25-267 (design decision 6): an object by its person number while it stays in view;
    # a new YOLO track for the same pile in view is still the pile.
    robot = Robot()
    s, _ = robot.see(track='pile-1')
    robot.filter(s, 'not_person', 'not_person')
    _, out = robot.see(track='pile-2')
    assert not looks(out)
    robot.wait(SHORT_GONE_S + 1)  # Out of view (the robot moved): forgotten.
    _, out = robot.see(track='pile-3')
    assert looks(out, 'filter')


def test_case04_someone_resting_on_a_bed_is_filtered_and_not_checked_every_30_minutes():
    robot = Robot()
    s, _ = robot.see(track='t1', on_floor=False)
    robot.filter(s, 'ordinary', 'ordinary')
    assert not looks(robot.frame(person('t1')))
    for _ in range(4):  # Stays in view for an hour: no recheck off the floor (decision G).
        robot.frame(person('t1'))
        assert not looks(robot.wait(RECHECK_S / 2))


def test_case05_one_person_seen_as_two_tracks_is_one_incident():
    robot = Robot()
    first = robot.open(track='t1', point=(3.0, 0.0))
    _, out = robot.see(track='t2', point=(3.4, 0.2))
    assert robot.active() == first and records(out, 'incident_opened') == []
    assert len(robot.flow.incidents) == 1


def test_case06_a_certain_person_on_the_bed_edge_still_gets_the_whole_body_look():
    robot = Robot()
    robot.open(track='t1')
    out = robot.to_posture()
    assert looks(out, 'posture')
    out = robot.posture(('furniture', None), ('furniture', None))
    assert records(out, 'incident_resolved') == ['off_floor_verified'] and drives(out, 'return')
    assert not kinds(out, 'ask')


def test_case07_not_reaching_the_circle_still_compares_from_where_it_got_to():
    robot = Robot()
    robot.open(track='t1')
    out = robot.drive('approach', 'failed')
    assert looks(out, 'arrival')
    robot.arrival('person_unsure')
    out = robot.drive('view', 'failed')
    assert looks(out, 'arrival')  # Compared again with the whole body in view.
    out = robot.arrival('person_unsure')
    assert notices(out) == [] and looks(out, 'posture')  # Unsure is not "different" (B).
    out = robot.posture(('unclear', None))
    assert drives(out, 'closer')


def test_case08_closed_on_a_chair_then_lying_on_the_floor_starts_again():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    robot.posture(('furniture', None), ('furniture', None))
    robot.wait(120)
    _, out = robot.see('lying', track='t1')
    assert looks(out, 'filter')  # Decision F: a new suspicion, filtered afresh.


def test_case09_a_cloud_finding_on_the_same_fall_joins_its_incident():
    robot = Robot()
    first = robot.open(track='t1')
    _, out = robot.see('cloud', point=(3.3, 0.1))
    assert records(out, 'incident_opened') == [] and robot.active() == first


def test_case10_an_unclear_posture_is_asked():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    assert drives(robot.posture(('unclear', None)), 'closer')


def test_case11_a_wrong_view_on_arrival_looks_again_from_where_it_was_first_seen():
    robot = Robot()
    robot.open(track='t1', robot=(0.0, 4.0))
    robot.drive('approach')
    out = robot.arrival('gone')  # 10-12 22:33: Cloud took another place for the spot.
    reverify, = drives(out, 'reverify')
    assert reverify['from_point'] == (0.0, 4.0)
    assert reverify['standoff'] == pytest.approx(math.dist((0.0, 4.0), SPOT))
    out = robot.drive('reverify')
    assert looks(out, 'arrival')
    out = robot.arrival('same_person')
    assert drives(out, 'view')


def test_case12_no_question_possible_is_an_urgent_alert():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    robot.posture(('floor', None))
    robot.drive('closer')
    out = robot.ask('failed')
    assert notices(out) == [('robot_could_not_confirm', 'urgent')]
    assert robot.only().idle == 'person'


def test_case13_a_reflection_is_dropped_and_never_approached():
    robot = Robot()
    s, _ = robot.see(track='mirror-1')
    out = robot.filter(s, 'reflection', 'reflection')
    assert not drives(robot.log)
    assert records(out, 'candidate_dropped') == ['reflection']


def test_case14_floor_activity_closes_and_is_checked_again_every_30_minutes():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    out = robot.posture(('floor_active', None), ('floor_active', None))
    assert records(out, 'incident_resolved') == ['floor_activity_verified']
    robot.wait(RECHECK_S - 60)
    robot.frame(person('t1'))  # Still in view (the VLM keeps reporting it).
    out = robot.wait(60)
    assert records(out, 'recheck_started') == ['floor_recheck'] and drives(out, 'approach')


def test_case15_a_minute_of_waiting_alerts_then_goes_late_and_reports_the_close():
    robot = Robot()
    incident = robot.open(track='t1')
    robot.flow.blocked(incident, 'manual_drive', robot.now)
    assert records(robot.take(), 'check_waiting') == ['manual_drive']
    assert robot.flow.status()['step'] == 'wait'
    out = robot.wait(WAIT_ALERT_S)
    assert notices(out) == [('robot_could_not_confirm', 'urgent')]
    assert robot.flow.status()['purpose'] == 'revisit'
    robot.to_posture()
    out = robot.posture(('standing', None), ('standing', None))
    assert ('closed_after_alert', 'info') in notices(out)  # Decision B.


def test_case15_a_different_person_found_late_is_checked_and_the_incident_stays_open():
    robot = Robot()
    robot.open(track='t1')
    robot.drive('approach')
    out = robot.arrival('different_person')
    assert notices(out) == [('person_may_differ', 'check')] and drives(out, 'view')
    robot.drive('view')
    robot.posture(('floor', None))
    robot.drive('closer')
    robot.ask('okay')
    assert robot.only().state == 'open'  # The first person is still unknown.


def test_case16_stop_and_help_during_a_check():
    stop = Robot()
    stop.open(track='t1')
    stop.flow.voice('stop', stop.now)
    out = stop.take()
    assert kinds(out, 'stop') and notices(out) == [('check_stopped_by_user', 'check')]
    assert records(out, 'check_stopped') == ['user_stop'] and stop.active() is None
    helped = Robot()
    helped.open(track='t1')
    helped.flow.voice('help', helped.now)
    assert notices(helped.take()) == [('help_requested', 'urgent')]


def test_case17_okay_on_the_floor_is_checked_again_after_30_minutes():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    robot.posture(('floor', None))
    robot.drive('closer')
    out = robot.ask('okay')
    assert records(out, 'incident_resolved') == ['response_completed']
    assert not drives(out, 'return')
    robot.wait(RECHECK_S - 60)
    robot.frame(person('t1'))
    out = robot.wait(60)
    assert records(out, 'recheck_started') == ['floor_recheck']


def test_case18_a_pet_that_left_is_closed_after_looking_again_and_reviewing_the_way():
    robot = Robot()
    robot.open(track='dog')
    robot.drive('approach')
    robot.arrival('gone')
    robot.drive('reverify')
    out = robot.arrival('gone')
    assert looks(out, 'departure')
    robot.flow.departure_answered(robot.active(), 'animal_left', robot.now)
    out = robot.take()
    assert records(out, 'incident_resolved') == ['not_a_person'] and drives(out, 'return')


def test_case18_leaving_unseen_stays_open_and_tells_the_guardian():
    robot = Robot()
    robot.open(track='t1')
    robot.drive('approach')
    robot.arrival('gone')
    robot.drive('reverify')
    robot.arrival('gone')
    robot.flow.departure_answered(robot.active(), 'not_seen', robot.now)
    assert notices(robot.take()) == [('left_unseen', 'check')]
    assert robot.only().idle == 'nobody'


# ------------------------------------------------------------ design decisions


def test_a_suspicion_that_cannot_be_told_apart_is_checked_separately():
    # SWM25-267 (decision 4): it no longer joins "the latest incident" (10-17 decision A).
    robot = Robot()
    robot.open(track='t1', point=None, box=(0.1, 0.1, 0.4, 0.8), view=1)
    _, out = robot.see(point=None, box=(0.6, 0.1, 0.9, 0.8), view=2)  # Robot moved, no place.
    assert looks(out, 'filter') and not kinds(out, 'link') and len(robot.flow.incidents) == 1


def test_another_place_on_the_map_is_another_person():
    robot = Robot()
    robot.open(track='t1', point=(3.0, 0.0))
    s, out = robot.see(track='t2', point=(6.0, 0.0))
    assert looks(out, 'filter')


def test_a_fall_while_being_checked_joins_that_check():
    robot = Robot()
    robot.open(track='t1', fall_moment='fall-1')
    robot.drive('approach')
    _, out = robot.see('motion', track='t1', fall_moment='fall-2')
    assert [c['kind'] for c in out] == ['link']
    assert robot.only().fall_moments == ['fall-1', 'fall-2']
    _, out = robot.see('motion', track='t1', fall_moment='fall-3')
    assert robot.only().fall_moments == ['fall-1', 'fall-3']  # First and last.


def test_a_fall_after_a_close_opens_a_new_incident():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    robot.posture(('furniture', None), ('furniture', None))
    _, out = robot.see('motion', track='t1', fall_moment='fall-9')
    assert looks(out, 'filter') and not robot.flow.memory


def test_someone_seen_where_nobody_was_verified_is_checked_again():
    robot = Robot()
    robot.open(track='t1')
    robot.drive('approach')
    robot.arrival('not_visible')
    robot.drive('reverify', 'failed')
    assert robot.only().idle == 'nobody'
    _, out = robot.see(track='t5')
    assert drives(out, 'approach') and robot.flow.status()['purpose'] == 'again'


def test_a_person_left_open_for_help_is_not_visited_again():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    robot.posture(('floor', None))
    robot.drive('closer')
    out = robot.ask('help')
    assert [c['who'] for c in kinds(out, 'left_open')] == ['person']  # Kept for a restart.
    _, out = robot.see(track='t1')
    assert not drives(out) and robot.active() is None


def test_incidents_are_checked_one_at_a_time_in_order():
    robot = Robot()
    first = robot.open(track='t1', point=(3.0, 0.0))
    s, _ = robot.see(track='t2', point=(8.0, 0.0))
    out = robot.filter(s, 'suspect')
    assert not drives(out) and robot.active() == first
    robot.to_posture()
    out = robot.posture(('standing', None), ('standing', None))
    approach, = drives(out, 'approach')
    assert approach['target'] == (8.0, 0.0)


def test_after_a_restart_open_incidents_are_checked_from_the_start():
    flow = FallFlow()
    s = Sighting('s1', 0.0, 'low', track='t1', point=SPOT, robot=FROM)
    incident = Incident('i7', s, None, 0.0, place=SPOT, step='posture')
    flow.restore([incident], 5.0)
    out = flow.drain()
    assert flow.status()['incident_id'] == 'i7' and drives(out, 'approach')
    assert flow.status()['purpose'] == 'restart'
    assert records(out, 'recheck_started') == ['agent_restart'] and incident.check_round == 2


def test_after_a_restart_an_incident_left_for_the_guardian_stays_open_unchecked():
    """10-17 22:19: left for the guardian after "help", then checked at every start."""
    flow = FallFlow()
    left = Incident('i7', Sighting('s1', 0.0, 'low', track='t1', point=SPOT, robot=FROM),
                    None, 0.0, place=SPOT, idle='person')
    cut = Incident('i8', Sighting('s2', 0.0, 'low', track='t2', point=(8.0, 0.0), robot=FROM),
                   None, 0.0, place=(8.0, 0.0), step='posture')
    flow.restore([left, cut], 5.0)
    out = flow.drain()
    assert records(out, 'recheck_started') == ['agent_restart']  # Only the one cut off.
    assert flow.status()['incident_id'] == 'i8' and not notices(out)
    assert (left.state, left.step, left.idle, left.check_round) == ('open', 'open', 'person', 1)


def test_someone_unseen_for_30_minutes_is_forgotten():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    robot.posture(('furniture', None), ('furniture', None))
    robot.wait(FORGET_S)
    assert robot.flow.memory == []


def test_the_two_posture_looks_must_agree_to_close():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    out = robot.posture(('furniture', None), ('floor_active', None))
    assert drives(out, 'closer')


def test_a_failed_filter_opens_the_incident():
    robot = Robot()
    s, _ = robot.see(track='t1')
    out = robot.filter(s, 'failed')
    assert records(out, 'incident_opened') and drives(out, 'approach')


def test_a_consent_or_connection_guard_opens_without_a_move_or_question():
    robot = Robot()
    s, _ = robot.see(track='t1')
    out = robot.filter(s, 'guard')
    assert records(out, 'incident_opened') and not drives(out) and robot.active() is None


def test_without_a_map_place_it_looks_and_asks_from_where_it_is():
    robot = Robot()
    robot.open(track='t1', point=None)
    assert looks(robot.log, 'posture') and not drives(robot.log)
    out = robot.posture(('floor', None))
    assert kinds(out, 'ask')


def test_ten_minutes_of_waiting_stops_trying():
    robot = Robot()
    incident = robot.open(track='t1')
    robot.flow.blocked(incident, 'busy', robot.now)
    robot.wait(WAIT_ALERT_S)
    out = robot.wait(WAIT_LIMIT_S)
    assert kinds(out, 'stop') and robot.active() is None


def test_a_guardian_close_stops_the_check_quietly():
    robot = Robot()
    incident = robot.open(track='t1')
    robot.flow.guardian_closed(incident, robot.now)
    out = robot.take()
    assert kinds(out, 'stop') and records(out, 'incident_resolved') == ['guardian_closed']
    assert not notices(out) and not drives(out, 'return')


# ------------------------------------------------------------ the web record (4-3)


def record(commands, event):
    found = [c for c in kinds(commands, 'record') if c['event'] == event]
    assert len(found) == 1, (event, found)
    return found[0]


def test_records_carry_the_web_state_round_and_answer():
    robot = Robot()
    s, _ = robot.see('motion', track='t1', fall_moment='fall-1')
    look = looks(robot.log, 'filter')[0]['look_id']
    robot.flow.filter_answered(s.sighting_id, 'suspect', robot.now, look_id=look)
    out = robot.take()
    opened = record(out, 'incident_opened')
    assert opened['reason'] == 'pose_rapid_posture_change'
    assert opened['sighting_id'] == s.sighting_id
    assert (opened['state'], opened['fall_seen'], opened['check_round']) == ('verifying', True, 1)
    assert record(out, 'analysis_completed')['look_id'] == look  # The filter's explanation.
    assert record(out, 'approach_started')['reason'] is None
    robot.to_posture()
    robot.posture(('floor', 'fall'))
    assert record(robot.log, 'fall_moment_reviewed')['reason'] == 'fall'
    robot.drive('closer')
    out = robot.ask('help')
    done = record(out, 'confirmation_completed')
    assert (done['state'], done['answer']) == ('help_required', 'help_request')
    alert, = kinds(out, 'notify')
    assert (alert['reason'], alert['state'], alert['answer']) == (
        'confirmation_help_required', 'help_required', 'help_request')


def test_a_stale_look_answer_is_ignored():
    robot = Robot()
    robot.open(track='t1')
    robot.drive('approach')
    robot.flow.arrival_answered(robot.active(), 'gone', robot.now, look_id='old-look')
    assert robot.take() == [] and robot.only().step == 'compare'


def test_okay_after_a_seen_fall_tells_the_guardian_and_closes():
    robot = Robot()
    robot.open('motion', track='t1', fall_moment='fall-1')
    robot.to_posture()
    robot.posture(('standing', 'fall'))
    robot.drive('closer')
    out = robot.ask('okay')
    assert notices(out) == [('fall_observed_person_okay', 'info')]
    assert record(out, 'confirmation_completed')['state'] == 'resolved'
    order = [c['kind'] for c in out if c['kind'] in ('notify', 'record')]
    assert order[:2] == ['notify', 'record']


def test_closed_after_an_alert_is_told_while_still_open():
    robot = Robot()
    incident = robot.open(track='t1')
    robot.flow.blocked(incident, 'busy', robot.now)
    robot.wait(WAIT_ALERT_S)
    robot.flow.resumed(incident, robot.now)
    robot.to_posture()
    out = robot.posture(('furniture', None), ('furniture', None))
    alert = [c for c in kinds(out, 'notify') if c['reason'] == 'closed_after_alert'][0]
    assert alert['state'] != 'resolved'  # The web refuses a notice on a resolved state.
    assert out.index(alert) < out.index(record(out, 'incident_resolved'))


def test_a_minute_of_waiting_goes_back_and_starts_again_then_ten_minutes_stop():
    robot = Robot()
    incident = robot.open(track='t1')
    robot.drive('approach')
    robot.arrival('same_person')
    robot.flow.blocked(incident, 'manual_drive', robot.now)  # The view drive is refused.
    out = robot.wait(WAIT_ALERT_S)
    assert record(out, 'check_not_confirmed')['reason'] == 'waited_manual_drive'
    assert drives(out, 'approach') and not records(out, 'approach_started')
    robot.wait(30)
    robot.flow.resumed(incident, robot.now)  # The drive back starts.
    assert record(robot.take(), 'revisit_started')['reason'] == 'manual_drive'
    robot.flow.blocked(incident, 'manual_drive', robot.now)  # Refused again later.
    out = robot.wait(WAIT_LIMIT_S - WAIT_ALERT_S - 30)
    assert kinds(out, 'stop') and record(out, 'agent_check_failed')['reason'] == 'time_limit'
    assert robot.active() is None  # Ten minutes of waiting in all, not per wait.


def test_a_helper_standing_where_someone_else_was_keeps_it_open():
    robot = Robot()
    robot.open(track='t1')
    robot.drive('approach')
    robot.arrival('different_person')
    robot.drive('view')
    out = robot.posture(('standing', None), ('standing', None))
    assert not records(out, 'incident_resolved') and not kinds(out, 'ask')
    assert robot.only().idle == 'nobody' and robot.only().state == 'open'


def test_consent_off_during_the_check_stops_without_a_question():
    robot = Robot()
    robot.open(track='t1')
    robot.drive('approach')
    robot.flow.arrival_answered(robot.active(), 'guard', robot.now, reason='cloud_consent_missing')
    out = robot.take()
    assert records(out, 'analysis_unavailable') == ['cloud_consent_missing']
    assert not kinds(out, 'ask') and robot.only().idle == 'person' and robot.active() is None


def test_a_person_seen_walking_away_closes_as_left_the_spot():
    robot = Robot()
    robot.open(track='t1')
    robot.drive('approach')
    robot.arrival('gone')
    robot.drive('reverify')
    robot.arrival('gone')
    robot.flow.departure_answered(robot.active(), 'person_walked', robot.now)
    assert records(robot.take(), 'incident_resolved') == ['left_the_spot']


def test_a_guardian_close_after_the_robot_closed_it_stops_rechecking():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    robot.posture(('floor', None))
    robot.drive('closer')
    robot.ask('okay')
    incident = robot.only().incident_id
    robot.flow.guardian_closed(incident, robot.now)
    assert records(robot.take(), 'incident_resolved') == ['guardian_closed']
    robot.flow.guardian_closed(incident, robot.now)  # The web repeats the list.
    assert robot.take() == []
    robot.frame(person('t1'))
    assert not records(robot.wait(RECHECK_S), 'recheck_started')  # 목업 31번.


def test_a_recheck_is_a_new_round_that_alerts_again():
    robot = Robot()
    incident = robot.open(track='t1')
    robot.flow.blocked(incident, 'busy', robot.now)
    robot.wait(WAIT_ALERT_S)
    robot.flow.resumed(incident, robot.now)
    robot.to_posture()
    robot.posture(('floor', None))
    robot.drive('closer')
    robot.ask('okay')
    robot.frame(person('t1'))  # In view while being asked.
    robot.wait(RECHECK_S - 60)
    robot.frame(person('t1'))
    out = robot.wait(60)
    started = record(out, 'recheck_started')
    assert (started['reason'], started['check_round'], started['state']) == (
        'floor_recheck', 2, 'verifying')
    robot.flow.blocked(incident, 'busy', robot.now)
    out = robot.wait(WAIT_ALERT_S)
    alert, = kinds(out, 'notify')
    assert (alert['reason'], alert['check_round']) == ('robot_could_not_confirm', 2)  # Decision B.


def test_a_suspicion_joining_an_incident_extends_its_clip():
    robot = Robot()
    incident = robot.open(track='t1')
    robot.drive('approach')
    s, out = robot.see('lying', track='t1')
    assert kinds(out, 'link') == [
        dict(kind='link', sighting_id=s.sighting_id, incident_id=incident)]
    assert not kinds(robot.frame(person('t1')), 'link')  # Being in view is not clip evidence.


def test_someone_merely_in_view_does_not_move_the_incidents_place():
    # 4-4 replay, bed 10-10 20:43: a box 1 m off moved the place; the next Cloud find at
    # the first spot then opened a second incident for the same person.
    robot = Robot()
    robot.open('cloud', point=(2.0, 1.2))
    robot.drive('approach')
    robot.frame(person('t7', (1.3, 0.6)))
    assert robot.only().place == (2.0, 1.2)
    _, out = robot.see('cloud', point=(2.0, 1.2))
    assert records(out, 'incident_opened') == [] and len(robot.flow.incidents) == 1


def test_still_unsure_with_the_whole_body_goes_on_as_the_same_person():
    # The user, 2026-10-17 (B): only legs at first sight is common; no alert, not kept open.
    robot = Robot()
    robot.open(track='t1')
    robot.drive('approach')
    robot.arrival('person_unsure')
    robot.drive('view')
    out = robot.arrival('person_unsure')
    assert not notices(out) and looks(out, 'posture') and not robot.only().keep_open
    out = robot.posture(('furniture', None), ('furniture', None))
    assert records(out, 'incident_resolved') == ['off_floor_verified']


# ------------------------------------------------------------ the same person (2026-10-18)
# 10-17 21:31~22:22 on the robot: 사람1 under a table and 사람2 beside the robot ended up in one
# incident for half an hour (~/.local/share/malbut-evaluations/ids-20261017).

LEFT, RIGHT = (0.16, 0.24, 0.42, 0.49), (0.40, 0.0, 1.0, 0.56)  # 사람1 and 사람2 on screen


def test_a_track_seen_again_long_after_where_someone_else_is_is_not_trusted():
    robot = Robot()
    robot.open(track='t1', point=None, box=LEFT, view=1)
    robot.wait(11.0)
    # The same number long after, where 사람2 is: maybe someone else, checked separately.
    _, out = robot.see(track='t1', point=None, box=RIGHT, view=1)
    assert looks(out, 'filter') and len(robot.flow.incidents) == 1


def test_the_10_17_layout_is_two_incidents():
    """사람1 under the table, then 사람2 on the floor beside the robot (ids-20261017)."""
    robot = Robot()
    first = robot.open(track='a', point=None, box=LEFT, view=1)
    robot.frame(person('a', None, LEFT), view=1)
    robot.frame(person('a', None, LEFT), person('b', None, RIGHT), view=1)
    s, out = robot.see(track='b', point=None, box=RIGHT, view=1)
    assert looks(out, 'filter') and not kinds(out, 'link')
    robot.filter(s, 'suspect')
    assert len(robot.flow.incidents) == 2 and first in robot.flow.incidents
    second = next(i for i in robot.flow.incidents.values() if i.incident_id != first)
    assert second.person != robot.flow.incidents[first].person and not second.uncertain


def test_depth_back_moves_what_the_new_number_had_to_that_person():
    """Design decision 2: a suspicion while uncertain was checked separately under a new
    number; depth back at the person's last place, it is theirs again."""
    robot = Robot()
    robot.flow.tracker.reid = False
    for _ in range(5):
        robot.frame(person('a', (3.0, 1.0), LEFT))
        robot.now += 0.2
    robot.now += 1.0
    nudged = (0.36, 0.24, 0.62, 0.49)  # Overlaps LEFT a little, no depth.
    for _ in range(3):  # Seen in three frames: it counts.
        robot.frame(person('b', None, nudged))
        robot.now += 0.2
    s, out = robot.see(track='b', point=None, box=nudged)
    assert looks(out, 'filter')
    robot.filter(s, 'suspect')
    incident = robot.only()
    retired = incident.person
    assert incident.uncertain and retired != robot.flow.tracker.person_of('a')
    out = robot.frame(person('b', (3.2, 1.0), nudged))
    assert incident.person != retired and incident.person == robot.flow.tracker.person_of('b')
    assert {'kind': 'people', 'incidents': {incident.incident_id: incident.person}} in out


def test_a_track_handed_to_someone_far_away_does_not_join_the_first_incident():
    robot = Robot()
    robot.open(track='95bd', point=(3.0, 1.0))
    robot.frame(person('95bd', (3.0, 1.0)))
    robot.now += 0.2
    _, out = robot.see(track='95bd', point=(0.6, 0.0))  # 10-17 21:50: YOLO gave it to 사람2.
    assert looks(out, 'filter') and not kinds(out, 'link')


def test_a_fall_while_two_are_too_close_is_checked_separately_once():
    robot = Robot()
    first = robot.open(track='a', point=(3.0, 1.0))
    for _ in range(3):  # Too close, seen in three frames: uncertain.
        robot.frame(person('a', (3.0, 1.0)), person('b', (3.3, 1.0)))
        robot.now += 0.2
    s, out = robot.see(track='b', point=(3.3, 1.0))
    assert looks(out, 'filter') and not kinds(out, 'link')
    robot.filter(s, 'suspect')
    separate = next(i for i in robot.flow.incidents.values() if i.incident_id != first)
    assert separate.uncertain
    _, out = robot.see(track='b', point=(3.3, 1.0))
    assert kinds(out, 'link') == [dict(kind='link', sighting_id=out[0]['sighting_id'],
                                       incident_id=separate.incident_id)]
    assert len(robot.flow.incidents) == 2  # One more, not one per suspicion.


def test_someone_who_lies_down_next_to_a_filtered_object_is_not_taken_for_it():
    robot = Robot()
    s, _ = robot.see(track='bag', point=(3.0, 1.0))
    robot.filter(s, 'not_person', 'not_person')
    for step in range(10):  # Someone walks up to the bag.
        robot.now += 0.2
        robot.frame(person('bag', (3.0, 1.0)), person('walk', (3.0, 3.0 - step * 0.2)))
    robot.wait(0.2)
    _, out = robot.see(track='walk', point=(3.0, 1.2))
    assert looks(out, 'filter')


def test_standing_up_after_a_close_on_the_floor_checks_again():
    robot = Robot()
    robot.open(track='t1')
    robot.to_posture()
    robot.posture(('floor', None))
    robot.drive('closer')
    robot.ask('okay')
    robot.frame(person('t1', state='suspected'))
    robot.now += 0.2
    out = robot.frame(person('t1', state='clear'))
    assert records(out, 'recheck_started') == ['stood_up']


def test_the_vlm_is_told_who_each_track_is_and_whose_each_incident_is():
    robot = Robot()
    incident = robot.open(track='t1')
    bound, = [c for c in robot.log if c['kind'] == 'people' and 'incidents' in c]
    assert bound['incidents'] == {incident: robot.only().person}
    both = (person('t1'), person('t2', (6.0, 0.0)))
    tracks, = [c['tracks'] for c in robot.frame(*both) if c['kind'] == 'people']
    assert tracks['t1'] == robot.only().person and tracks['t2'] != tracks['t1']
    assert not kinds(robot.frame(*both), 'people')  # Only changes are told.


def test_after_a_restart_an_incident_is_bound_to_who_is_at_its_place():
    flow = FallFlow()
    s = Sighting('s1', 0.0, 'low', track='old', point=SPOT, robot=FROM)
    incident = Incident('i7', s, None, 0.0, place=SPOT)
    flow.restore([incident], 5.0)
    flow.drain()
    flow.people(Frame(6.0, 0, (person('new', (3.2, 0.0)),)), 6.0)
    out = flow.drain()
    assert incident.person is not None
    assert {'kind': 'people', 'incidents': {'i7': incident.person}} in out


def test_a_person_filtered_on_the_floor_is_filtered_again_with_a_new_sighting():
    # 10-17 21:31: the 30-minute refilter copied the 21:01 sighting under a new id the VLM
    # never saw (no first photo, no place, not restored after a restart).
    robot = Robot()
    s, _ = robot.see(track='t1', point=None, box=LEFT, view=1)
    robot.filter(s, 'ordinary', 'ordinary')
    assert not looks(robot.wait(RECHECK_S))  # Due, but nothing is copied.
    fresh, out = robot.see(track='t9', point=(2.0, 0.5), box=LEFT, view=1)
    look, = looks(out, 'filter')
    assert look['sighting_id'] == fresh.sighting_id and '+' not in look['sighting_id']
    robot.filter(fresh, 'suspect')
    assert robot.only().first.sighting_id == fresh.sighting_id and drives(robot.log, 'approach')


# ------------------------------------------------------------ the check's video (SWM25-268)


def clips(commands):
    return [(c['mark'], c['check_round']) for c in kinds(commands, 'clip')]


def test_the_video_starts_when_the_robot_gets_to_work_and_ends_at_the_judgment():
    robot = Robot()
    incident = robot.open(track='t1')
    assert clips(robot.log) == []  # Opened and sent, not started yet.
    robot.flow.resumed(incident, robot.now)  # The Manager took the drive.
    assert clips(robot.take()) == [('start', 1)]
    robot.to_posture()
    robot.posture(('standing', None), ('standing', None))
    assert clips(robot.log) == [('start', 1), ('end', 1)]


def test_waits_are_left_out_of_the_video():
    robot = Robot()
    incident = robot.open(track='t1')
    robot.flow.blocked(incident, 'manual_drive', robot.now)  # Before any work: nothing yet.
    robot.wait(WAIT_ALERT_S)
    robot.flow.resumed(incident, robot.now)  # 직접 움직이기 ended: 발견 stays its own range.
    robot.drive('approach')
    robot.flow.blocked(incident, 'busy', robot.now)
    robot.wait(20)
    robot.flow.resumed(incident, robot.now)
    robot.arrival('same_person')
    robot.drive('view')
    robot.posture(('floor', None))
    robot.drive('closer')
    robot.ask('help')
    assert clips(robot.log) == [('start', 1), ('pause', 1), ('start', 1), ('end', 1)]


def test_without_a_map_place_the_video_starts_with_the_first_look():
    robot = Robot()
    robot.open(track='t1', point=None)
    assert clips(robot.log) == [('start', 1)]


def test_a_later_round_has_its_own_video():
    robot = Robot()
    incident = robot.open(track='t1')
    robot.to_posture()
    robot.posture(('floor', None))
    robot.drive('closer')
    robot.ask('okay')
    robot.wait(RECHECK_S - 60)
    robot.frame(person('t1'))
    robot.wait(60)
    assert robot.active() == incident
    robot.flow.resumed(incident, robot.now)
    robot.take()
    assert clips(robot.log) == [('start', 1), ('end', 1), ('start', 2)]


# ------------------------------------------------------------ alert grouping (SWM25-267)


def grouping(minutes, reason='confirmation_help_required', place=(3.5, 0.0), same=False):
    """Two alerts: an urgent one at SPOT, then another `minutes` later from `place`."""
    flow = FallFlow()
    s = Sighting('s1', 0.0, 'low', point=SPOT)
    first = Incident('first', s, 'P1', 0.0, place=SPOT)
    second = first if same else Incident('second', s, 'P2', 0.0, place=place)
    flow.incidents = {i.incident_id: i for i in (first, second)}
    flow._notify(first, 'confirmation_help_required')
    flow.now = minutes * 60.0
    flow._notify(second, reason)
    out = flow.drain()
    return ([c['reason'] for c in kinds(out, 'notify')],
            [(c['incident_id'], c['reason']) for c in kinds(out, 'record')
             if c['event'] == 'notification_grouped'])


def test_the_same_level_from_the_same_place_within_10_minutes_is_noted_not_sent():
    sent, noted = grouping(6)
    assert sent == ['confirmation_help_required']  # Only the first one.
    assert noted == [('first', 'confirmation_help_required'),
                     ('second', 'confirmation_help_required')]
    assert grouping(6, same=True)[1] == [('first', 'confirmation_help_required')]


def test_later_higher_further_or_placeless_alerts_are_sent():
    assert grouping(11) == (['confirmation_help_required'] * 2, [])
    assert grouping(6, reason='person_may_differ') == (
        ['confirmation_help_required', 'person_may_differ'], [])  # Another level.
    assert grouping(6, place=(5.0, 0.0)) == (['confirmation_help_required'] * 2, [])  # 2 m.
    assert grouping(6, place=None) == (['confirmation_help_required'] * 2, [])


def test_a_grouped_alert_does_not_count_as_urgent_sent():
    flow = FallFlow()
    s = Sighting('s1', 0.0, 'low', point=SPOT)
    first, second = (Incident(n, s, p, 0.0, place=SPOT) for n, p in (('a', 'P1'), ('b', 'P2')))
    flow.incidents = {'a': first, 'b': second}
    flow._notify(first, 'help_requested')
    flow._notify(second, 'help_requested')
    assert first.urgent_sent and not second.urgent_sent  # No closed_after_alert for b later.


# ------------------------------------------------------------ ReID splits (SWM25-267)


def test_a_reappearance_asks_the_vlm_and_an_unlike_answer_splits_off_someone_else():
    robot = Robot()
    robot.frame(person('a', (3.0, 1.0)))
    robot.wait(600)
    out = robot.frame(person('z', (3.3, 1.0)))
    compare, = kinds(out, 'compare')
    assert compare['a']['track'] == 'a' and compare['b']['track'] == 'z'
    s, out = robot.see(track='z', point=(3.3, 1.0))  # Uncertain meanwhile: filtered.
    robot.filter(s, 'suspect')
    incident = robot.only()
    assert incident.uncertain and incident.person == robot.flow.tracker.person_of('z')
    robot.flow.appearance(compare['request_id'], 0.4, robot.now)
    out = robot.take()
    new = robot.flow.tracker.person_of('z')
    assert new != compare['a'] and incident.person == new
    assert {'kind': 'people', 'at': compare['b']['at'], 'tracks': {'z': new}} in out
