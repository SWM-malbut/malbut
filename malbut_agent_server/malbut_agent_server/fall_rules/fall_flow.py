"""One fall check for every suspicion (SWM25-261, 2026-10-17): the Agent decides.

The VLM only reports what it saw (sightings) and what Cloud answered; the link
runs the robot's moves and questions and reports how they ended. This module
decides everything in between, the same way whether the robot goes the first
time, late after waiting, or again after a close:

  1. a fall suspicion is seen; Cloud's first filter drops what is clearly not a
     person, only an image, or ordinary (twice), and one incident is one
     person's one fall (who is who: person_tracker.py, SWM25-267);
  2. the robot stops 1 m from the spot on the side it was first seen from and
     compares the first sight with what it sees on arrival (gone: look again
     from where it was first seen, then ask Cloud what left);
  3. it looks at the whole body and Cloud judges the posture, with the fall
     moment when a fall was seen;
  4. it goes back to 1 m and asks.
Afterwards the posture at the close is remembered; standing up, a fall motion,
lying on the floor after a close off the floor, or 30 minutes after a close on
the floor start the check again (a new round of the same incident).

Pure: no ROS, transport or clock. Callers pass the time and drain ``commands``:
look (a Cloud look by the VLM), drive, ask, stop (the link), and record, notify,
link and clip (written by the VLM in its journal, in the web's words:
malbut_interfaces/contracts/fall_events.json). Each record carries the web state,
whether a fall was seen, the latest answer and the check round. Clip marks the
robot's check for the incident's video (SWM25-268, 목업 53번): start when the
robot gets to work on a round, pause while it waits, end at the judgment.
Decisions: ~/fall-refactor-plan-20261017.md, ~/fall-refactor-design-20261017.md.
"""

from dataclasses import dataclass, field, replace
import math
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from .person_tracker import (
    FORGET_S, REAPPEAR_M, SHORT_GONE_S, Observation, PersonTracker,
)

STANDOFF_M = 1.0
VIEW_MAX_M = 3.0
# A drop needs a second filter look a few seconds later (design 4).
FILTER_AGAIN_S = 3.0
# Closing without a question needs two posture looks that agree (2026-10-11).
POSTURE_AGAIN_S = 2.5
# Waiting on 직접 움직이기 or another job (2026-10-12): an urgent alert after a
# minute of waiting in all, then the robot goes back to the spot and checks from
# the start; it stops trying after ten minutes of waiting in all.
WAIT_ALERT_S = 60.0
WAIT_LIMIT_S = 600.0
# After a close (design 8): forget someone unseen for 30 minutes (FORGET_S, the same
# as the person tracking); recheck someone closed on the floor every 30 minutes (G).
RECHECK_S = 1800.0
# One alert per place and level (decision 6, mockup 55 ③): the same level from the same
# place (1 m) within 10 minutes is noted on the incident that sent it instead.
GROUP_S, GROUP_M = 600.0, 1.0

FLOOR, OFF_FLOOR = 'floor', 'off_floor'
POSTURE_PLACE = {'floor': FLOOR, 'floor_active': FLOOR, 'unclear': FLOOR,
                 'furniture': OFF_FLOOR, 'standing': OFF_FLOOR}
DROPS = ('not_person', 'reflection', 'ordinary')
WAITING_FOR = ('manual_drive', 'busy')
FAILURES = ('speech_unavailable', 'question_error', 'time_limit')

# The web's words for what happened (fall_events.json reasonsByKind).
OPEN_REASON = {'low': 'pose_sustained_low_posture', 'lying': 'pose_sustained_horizontal_posture',
               'motion': 'pose_rapid_posture_change', 'cloud': 'cloud_crosscheck'}
POSTURE_REASON = {'furniture': 'sitting_off_floor', 'standing': 'standing',
                  'floor_active': 'active_on_floor', 'floor': 'on_floor'}
RECHECK_REASON = {'stood_up': 'stood_up', 'recheck': 'floor_recheck',
                  'again': 'person_seen_again', 'restart': 'agent_restart'}
EARLIER = ('fall', 'self', 'unknown')

# Why the guardian is told (levels per malbut_interfaces/contracts/fall_events.json).
URGENT, CHECK, INFO = 'urgent', 'check', 'info'
NOTICES = {
    'robot_could_not_confirm': URGENT,     # a minute of waiting, or no question possible
    'help_requested': URGENT,              # "도와줘" during the check
    'confirmation_help_required': URGENT,  # the answer needs help (or no answer)
    'scene_answer_unconfirmed': CHECK,     # someone answered, maybe not this person
    'check_stopped_by_user': CHECK,        # "그만" during the check
    'person_may_differ': CHECK,            # a different person at the spot (design 6)
    'left_unseen': CHECK,                  # gone, and the leaving was not seen
    'could_not_compare': CHECK,            # the spot could not be seen, even from the first place
    'person_crawled_away': CHECK,          # a person moved away low on the floor
    'fall_observed_person_okay': INFO,     # a fall was seen and the person said they are fine
    'closed_after_alert': INFO,            # closed after an urgent alert went (decision B)
}


@dataclass(frozen=True)
class Sighting:
    """A suspicion the VLM saw. ``kind``: low, lying, motion (a fall motion) or cloud
    (a Cloud crosscheck finding, already filtered).

    ``robot`` is where the robot saw it from; ``view`` changes whenever the
    robot moves, so boxes compare only within one view. ``person`` and
    ``uncertain`` come from the person tracking when the rules take it in.
    """

    sighting_id: str
    at: float
    kind: str
    track: Optional[str] = None
    point: Optional[Tuple[float, float]] = None
    box: Optional[Tuple[float, float, float, float]] = None
    view: Optional[int] = None
    robot: Optional[Tuple[float, float]] = None
    on_floor: Optional[bool] = None
    photo: Optional[str] = None
    fall_moment: Optional[str] = None
    person: Optional[str] = None
    uncertain: bool = False

    @property
    def fall_seen(self):
        return self.kind == 'motion' or self.fall_moment is not None


@dataclass
class Incident:
    incident_id: str
    first: Sighting
    person: Optional[str]        # None after a restart until someone is seen at its place
    opened_at: float
    place: Optional[Tuple[float, float]] = None   # where to go: the person's last certain place
    uncertain: bool = False      # opened while who is who was uncertain (decision 4)
    fall_moments: List[str] = field(default_factory=list)
    state: str = 'open'          # open, closed
    step: str = 'queued'         # see FallFlow.status()
    purpose: str = 'approach'    # approach, revisit, recheck, stood_up, again, restart
    # Open but not being checked: nobody (no person verified there) or a person.
    idle: Optional[str] = None
    reverified: bool = False     # looked again from where it was first seen
    compare_at_view: bool = False
    keep_open: bool = False      # the first person was never verified (design 6)
    asked: bool = False
    urgent_sent: bool = False
    posture: Optional[str] = None
    looks: List[str] = field(default_factory=list)
    next_look_at: Optional[float] = None
    look_id: Optional[str] = None  # the Cloud look this step waits for
    # The check round: 1, then one more each time the robot checks it again.
    check_round: int = 1
    answer: Optional[str] = None   # the web answer: okay, help_request, unclear, failed
    # Waiting on the Manager this round: since when, why, how long before.
    waiting_since: Optional[float] = None
    waiting_for: Optional[str] = None
    waited: float = 0.0
    wait_alerted: bool = False
    waits_noted: set = field(default_factory=set)
    revisit_noted: bool = False
    clip_open: bool = False      # this round's clip range is growing (SWM25-268)

    @property
    def fall_seen(self):
        return bool(self.fall_moments) or self.first.fall_seen


@dataclass
class Candidate:
    sighting: Sighting
    person: str
    drops: List[str] = field(default_factory=list)
    next_look_at: Optional[float] = None
    look_id: Optional[str] = None


@dataclass
class Memory:
    """Someone checked or filtered as not a fall, or a filtered object (while seen)."""

    person: str
    kind: str                     # person or object
    place: Optional[str] = None   # floor or off_floor, for a person
    since: float = 0.0
    incident_id: Optional[str] = None
    # Filtered 30 minutes ago and never opened: filtered again on the next suspicion.
    due: bool = False


class FallFlow:
    def __init__(self, tracker=None):
        self.commands = []
        self.tracker = tracker or PersonTracker()
        self.now = 0.0
        # Alerts sent lately: (when, level, place, incident) for grouping (decision 6).
        self.sent: List[tuple] = []
        self.incidents: Dict[str, Incident] = {}
        self.candidates: Dict[str, Candidate] = {}
        self.memory: List[Memory] = []
        self.queue: List[str] = []
        self.active: Optional[str] = None

    def drain(self):
        out, self.commands = self.commands, []
        return out

    # ------------------------------------------------------------ status

    def status(self):
        """For the fall check status: which incident, which step, why it went, waiting."""
        incident = self.incidents.get(self.active)
        if incident is None:
            return dict(active=False, incident_id=None, step=None, purpose=None,
                        waiting_for=None, alerted=False, target=None, check_round=None)
        step = 'wait' if incident.waiting_since is not None else incident.step
        return dict(active=True, incident_id=incident.incident_id, step=step,
                    purpose=incident.purpose, waiting_for=incident.waiting_for,
                    alerted=incident.wait_alerted, target=self._target(incident),
                    check_round=incident.check_round)

    # ------------------------------------------------------------ inputs: VLM

    def people(self, frame, now):
        """A pose frame (VLM ``fall_people``): who is who, then what follows from it."""
        self.now = now  # For the alert grouping (10 minutes).
        result = self.tracker.update(frame)
        if result.changed:  # For the people boxes on the incident's video.
            self.commands.append(dict(kind='people', at=frame.at, tracks=dict(result.changed)))
        for person in result.stood_up:
            memory = next((m for m in self.memory if m.person == person
                           and m.kind == 'person'), None)
            if memory is not None and not self.tracker.uncertain(person):
                self._recheck(memory, 'stood_up', now)
        for incident in list(self.incidents.values()):
            if incident.state == 'open' and incident.person is None and incident.place:
                # After a restart: the first one seen for sure at its place (decision 5).
                here = [p for p in self.tracker.people.values() if p.last_seen == frame.at
                        and p.point and math.dist(p.point, incident.place) <= REAPPEAR_M
                        and not self.tracker.uncertain(p.person_id)]
                if here:
                    self._bind(incident, here[0].person_id)

    def sighted(self, s, now):
        """A suspicion: whose it is, then that person's incident, filter or memory."""
        self.now = now  # For the alert grouping (10 minutes).
        person, uncertain = self.tracker.assign(Observation(s.track, s.box, s.point), s.at, s.view)
        s = replace(s, person=person, uncertain=uncertain)
        checking = self.incidents.get(self.active)
        if (s.fall_seen and checking is not None and checking.person == person
                and not uncertain):
            self._attach(checking, s)  # A fall while being checked joins that check.
            return
        open_incident = None if s.fall_seen else self._open_match(s)
        if open_incident is not None:
            self._attach(open_incident, s)
            if open_incident.idle == 'nobody':
                self._restart(open_incident, 'again', now)  # Someone there now (design 3).
            return
        if self._candidate_match(s):
            return  # Already waiting for Cloud's first filter.
        memory = self._memory_match(s)
        if memory is not None and s.fall_seen:
            self.memory.remove(memory)  # A new fall: start afresh (design 3).
        elif memory is not None:
            if memory.due:
                # 30 minutes on, filtered again with what is seen now (2026-10-18: a copy
                # of the old sighting kept 30-minute-old data and an id the VLM never saw).
                self.memory.remove(memory)
                self._candidate(s, now)
            elif (memory.kind == 'person' and memory.place == OFF_FLOOR
                  and s.kind in ('lying', 'cloud')):
                self.memory.remove(memory)  # Closed off the floor, now down (decision F).
                self._candidate(s, now)
            return
        self._candidate(s, now)

    def filter_answered(self, sighting_id, verdict, now, *, look_id=None, reason=None):
        """Cloud's first filter: suspect, not_person, reflection, ordinary, failed or guard."""
        self.now = now  # For the alert grouping (10 minutes).
        candidate = self.candidates.get(sighting_id)
        if candidate is None or (look_id is not None and look_id != candidate.look_id):
            return
        if verdict in DROPS and not (verdict == 'ordinary' and candidate.sighting.fall_seen):
            candidate.drops.append(verdict)
            if len(candidate.drops) < 2:
                candidate.next_look_at = now + FILTER_AGAIN_S
                return
            del self.candidates[sighting_id]
            self._record('candidate_dropped', None, verdict, sighting_id=sighting_id)
            if verdict == 'ordinary':
                place = OFF_FLOOR if candidate.sighting.on_floor is False else FLOOR
                self.memory.append(Memory(candidate.person, 'person', place, now))
            else:
                self.memory.append(Memory(candidate.person, 'object', since=now))
            return
        del self.candidates[sighting_id]
        self._open(candidate.sighting, now, guard=verdict == 'guard',
                   look_id=look_id if verdict == 'suspect' else None, guard_reason=reason)

    def arrival_answered(self, incident_id, verdict, now, *, look_id=None, reason=None):
        """First sight vs arrival (design 6)."""
        self.now = now  # For the alert grouping (10 minutes).
        incident = self._current(incident_id, 'compare', look_id)
        if incident is None:
            return
        self._resume(incident, now)
        if verdict == 'guard':
            self._guarded(incident, reason, now)
            return
        self._record('arrival_compared', incident, verdict, look_id=look_id)
        at_view = incident.compare_at_view
        if verdict == 'failed':
            verdict = 'person_unsure'  # Cannot compare by Cloud: keep checking the person.
        if verdict == 'same_person':
            self._to_view_or_posture(incident, at_view, now)
        elif verdict == 'person_unsure':
            if at_view:
                # Still unsure with the whole body in view: go on as the same person. The
                # 12 cm camera often saw only legs first, and an alert each time was noise
                # (4-4 replay, the user 2026-10-17: only a clearly different person tells).
                self._posture(incident, now)
            else:
                incident.compare_at_view = True
                self._view(incident)
        elif verdict == 'different_person':
            self._differ(incident)
            self._to_view_or_posture(incident, at_view, now)
        elif verdict == 'same_object':
            self._close(incident, 'not_a_person', now, memory='object')
        elif verdict in ('gone', 'not_visible'):
            if not incident.reverified:
                self._reverify(incident)  # Look from where it was first seen first.
            elif verdict == 'gone':
                incident.step = 'departure'
                self._look(incident, 'departure', before=incident.first.photo,
                           box=incident.first.box)
            else:
                self._idle(incident, 'nobody', 'could_not_compare', now)

    def departure_answered(self, incident_id, verdict, now, *, look_id=None, reason=None):
        """What left the spot, from the frames between first sight and arrival."""
        self.now = now  # For the alert grouping (10 minutes).
        incident = self._current(incident_id, 'departure', look_id)
        if incident is None:
            return
        if verdict == 'guard':
            self._guarded(incident, reason, now)
            return
        self._record('departure_reviewed', incident, verdict, look_id=look_id)
        if verdict == 'animal_left':
            self._close(incident, 'not_a_person', now, memory=None)  # Not a person (목업 30번).
        elif verdict == 'person_walked':
            self._close(incident, 'left_the_spot', now, memory=None)
        elif verdict == 'person_crawled':
            self._idle(incident, 'nobody', 'person_crawled_away', now)
        else:
            self._idle(incident, 'nobody', 'left_unseen', now)

    def posture_answered(self, incident_id, verdict, earlier, now, *, look_id=None, reason=None):
        """Where the body rests now, and (fall seen) whether the earlier moment was a fall."""
        self.now = now  # For the alert grouping (10 minutes).
        incident = self._current(incident_id, 'posture', look_id)
        if incident is None:
            return
        self._resume(incident, now)
        if verdict == 'guard':
            self._guarded(incident, reason, now)
            return
        self._record('posture_check_completed', incident,
                     POSTURE_REASON.get(verdict, 'posture_unconfirmed'), look_id=look_id)
        if incident.fall_seen and verdict != 'failed':
            self._record('fall_moment_reviewed', incident,
                         earlier if earlier in EARLIER else 'unknown')
        incident.posture = verdict
        incident.looks.append(verdict)
        if verdict == 'failed' or self._ask_after(incident, verdict, earlier):
            self._closer(incident)
            return
        if len(incident.looks) < 2:
            incident.next_look_at = now + POSTURE_AGAIN_S
            return
        if POSTURE_PLACE.get(incident.looks[-2]) != POSTURE_PLACE[verdict]:
            self._closer(incident)  # The two looks disagree: ask.
            return
        if incident.keep_open:
            # Someone else, not on the floor (a helper standing): no question, but the
            # first person was never verified, so it stays open (design 6).
            self._idle(incident, 'nobody', None, now)
            return
        reason = 'floor_activity_verified' if verdict == 'floor_active' else 'off_floor_verified'
        self._close(incident, reason, now, memory='person')

    # ------------------------------------------------------------ inputs: the link

    def drive_done(self, incident_id, purpose, outcome, now):
        self.now = now  # For the alert grouping (10 minutes).
        incident = self.incidents.get(incident_id)
        if purpose == 'return':
            # The way back after a close without a question; the check is already over.
            if incident is not None:
                self._record('approach_returned', incident,
                             'returned' if outcome == 'returned' else 'return_failed')
            return
        if incident is None or incident_id != self.active:
            return
        self._resume(incident, now)
        if purpose == 'approach':
            self._record('approach_completed', incident, outcome)
            if outcome == 'no_map':
                self._ask(incident)  # No place on the map: ask where it stands.
                return
            # Arrived on the circle, or facing the spot from the best place it reached.
            self._compare(incident)
        elif purpose == 'reverify':
            self._record('reverify_completed', incident, outcome)
            if outcome == 'arrived':
                self._compare(incident)
            else:
                self._idle(incident, 'nobody', 'could_not_compare', now)
        elif purpose == 'view':
            self._record('view_completed', incident, outcome)
            if incident.compare_at_view:
                self._compare(incident)
            else:
                self._posture(incident, now)
        elif purpose == 'closer':
            self._ask(incident)  # From the nearest place it reached (design 7).

    def asked(self, incident_id, answer, now, *, assessment=None, reason=None):
        """okay, help (help or no answer), unclear (who answered) or failed (no question).

        ``assessment``: the Agent's situation (confirmed_incident, resolved, unknown);
        ``reason``: why it failed (speech_unavailable, question_error, time_limit).
        """
        self.now = now  # For the alert grouping (10 minutes).
        incident = self.incidents.get(incident_id)
        if incident is None or incident_id != self.active or incident.step != 'ask':
            return
        self._resume(incident, now)
        if answer == 'okay':
            incident.answer = 'okay'
            if incident.fall_seen:
                self._notify(incident, 'fall_observed_person_okay')
            if incident.keep_open:
                incident.idle = 'nobody'  # The first person is still unknown.
                self._record('voice_result', incident, None)
                self._idle(incident, 'nobody', None, now)
                return
            self._record('confirmation_completed', incident, assessment or 'resolved',
                         state='resolved')
            self._close(incident, 'response_completed', now, memory='person')
        elif answer == 'help':
            incident.answer, incident.idle = 'help_request', 'person'
            self._record('confirmation_completed', incident, assessment or 'unknown')
            self._idle(incident, 'person', 'confirmation_help_required', now)
        elif answer == 'unclear':
            incident.answer, incident.idle = 'unclear', 'person'
            self._record('voice_result', incident, 'scene_answer_unassociated')
            self._idle(incident, 'person', 'scene_answer_unconfirmed', now)
        else:
            incident.answer, incident.idle = 'failed', 'person'
            self._record('agent_check_failed', incident,
                         reason if reason in FAILURES else 'question_error')
            self._idle(incident, 'person', 'robot_could_not_confirm', now)

    def blocked(self, incident_id, reason, now):
        """The Manager refused or interrupted the step; the link sends it again."""
        self.now = now  # For the alert grouping (10 minutes).
        incident = self.incidents.get(incident_id)
        if incident is None or incident_id != self.active:
            return
        reason = reason if reason in WAITING_FOR else 'busy'
        if incident.waiting_since is None:
            self._clip(incident, 'pause')  # The wait is left out of the video (목업 53번).
            incident.waiting_since = now
        incident.waiting_for = reason
        if reason not in incident.waits_noted:
            incident.waits_noted.add(reason)
            self._record('check_waiting', incident, reason)

    def resumed(self, incident_id, now):
        """The Manager took the step: the waiting (if any) is over."""
        self.now = now  # For the alert grouping (10 minutes).
        incident = self.incidents.get(incident_id)
        if incident is not None and incident_id == self.active:
            self._resume(incident, now)

    def voice(self, kind, now):
        """"도와줘" or "그만" heard during a check (2026-10-13)."""
        self.now = now  # For the alert grouping (10 minutes).
        incident = self.incidents.get(self.active)
        if incident is None:
            return
        if kind == 'help':
            self._command('stop', incident)
            incident.answer, incident.idle = 'help_request', 'person'
            self._idle(incident, 'person', 'help_requested', now)
        elif kind == 'stop':
            self._command('stop', incident)
            incident.idle = 'person'
            self._record('check_stopped', incident, 'user_stop')
            self._idle(incident, 'person', 'check_stopped_by_user', now)

    def guardian_closed(self, incident_id, now):
        """처리 완료: stop, and never check this incident again (목업 31번)."""
        self.now = now  # For the alert grouping (10 minutes).
        incident = self.incidents.get(incident_id)
        if incident is None:
            return
        if incident.state == 'closed':
            if any(m.incident_id == incident_id for m in self.memory):
                self._forget(incident_id)
                self._record('incident_resolved', incident, 'guardian_closed')
            return
        if incident_id in self.queue:
            self.queue.remove(incident_id)
        if incident_id == self.active:
            self._command('stop', incident)
        self._close(incident, 'guardian_closed', now, memory=None, quiet=True)

    def tick(self, now):
        self.now = now  # For the alert grouping (10 minutes).
        for candidate in list(self.candidates.values()):
            if candidate.next_look_at is not None and now >= candidate.next_look_at:
                candidate.next_look_at = None
                self._filter_look(candidate)
        incident = self.incidents.get(self.active)
        if incident is not None:
            if incident.next_look_at is not None and now >= incident.next_look_at:
                incident.next_look_at = None
                self._posture_look(incident)
            if incident.waiting_since is not None:
                waited = incident.waited + now - incident.waiting_since
                if waited >= WAIT_LIMIT_S:
                    self._command('stop', incident)
                    incident.answer, incident.idle = 'failed', 'person'
                    self._record('agent_check_failed', incident, 'time_limit')
                    self._idle(incident, 'person', None, now)
                elif waited >= WAIT_ALERT_S and not incident.wait_alerted:
                    incident.wait_alerted = True
                    self._record('check_not_confirmed', incident,
                                 'waited_' + (incident.waiting_for or 'busy'),
                                 state='recheck_required')
                    self._notify(incident, 'robot_could_not_confirm', state='recheck_required')
                    # The person may have moved meanwhile: go back and check from the start.
                    self.active = None
                    self._enqueue(incident, 'revisit')
                    self._next(now)
        for memory in list(self.memory):
            person = self.tracker.people.get(memory.person)
            if person is None or now - person.last_seen >= FORGET_S:
                self.memory.remove(memory)  # Unseen for 30 minutes: start afresh next time.
            elif memory.kind == 'object' and (now - person.last_seen > SHORT_GONE_S
                                              or self.tracker.uncertain(memory.person)):
                # An object only while it stays in view and sure (10-17 design 8): someone
                # who later lies down there must not inherit it.
                self.memory.remove(memory)
            elif (memory.kind == 'person' and memory.place == FLOOR and not memory.due
                  and now - memory.since >= RECHECK_S):
                self._recheck(memory, 'recheck', now)

    def restore(self, incidents, now):
        """After a restart: every open incident is checked again from the start."""
        self.now = now  # For the alert grouping (10 minutes).
        for incident in incidents:
            if incident.state != 'open' or incident.incident_id in self.incidents:
                continue
            self.incidents[incident.incident_id] = incident
            incident.idle = None
            self._new_round(incident, 'restart')
            self._enqueue(incident, 'restart')
        self._next(now)

    # ------------------------------------------------------------ steps

    def _candidate(self, s, now):
        if s.kind == 'cloud':  # Cloud's crosscheck already asked the same questions.
            self._open(s, now)
            return
        candidate = Candidate(s, s.person)
        self.candidates[s.sighting_id] = candidate
        self._filter_look(candidate)

    def _filter_look(self, candidate):
        s = candidate.sighting
        candidate.look_id = str(uuid4())
        # A fall motion is never dropped as ordinary: sitting after a fall looks it (design 4).
        self.commands.append(dict(kind='look', what='filter', look_id=candidate.look_id,
                                  sighting_id=s.sighting_id, photo=s.photo, box=s.box,
                                  track=s.track, ordinary_drops=not s.fall_seen))

    def _open(self, s, now, *, guard=False, look_id=None, guard_reason=None):
        incident = Incident(str(uuid4()), s, s.person, now, place=s.point,
                            uncertain=s.uncertain)
        if s.fall_moment:
            incident.fall_moments.append(s.fall_moment)
        self.incidents[incident.incident_id] = incident
        self._record('incident_opened', incident, OPEN_REASON.get(s.kind),
                     sighting_id=s.sighting_id)
        self._bind(incident, s.person)
        if look_id is not None or s.kind == 'cloud':
            # The filter's explanation, or for a Cloud find its periodic look's.
            self._record('analysis_completed', incident, None, look_id=look_id or s.sighting_id)
        if guard:  # Consent or connection off: no Cloud look, no question (unchanged).
            incident.idle = 'person'
            incident.step = 'guard'
            self._record('analysis_unavailable', incident, guard_reason or 'cloud_blocked')
            return
        self._enqueue(incident, 'approach')
        self._next(now)

    def _enqueue(self, incident, purpose):
        incident.purpose = purpose
        incident.step = 'queued'
        if incident.incident_id not in self.queue and incident.incident_id != self.active:
            self.queue.append(incident.incident_id)

    def _next(self, now):
        while self.active is None and self.queue:
            incident = self.incidents[self.queue.pop(0)]
            if incident.state != 'open':
                continue
            self.active = incident.incident_id
            incident.idle = None
            incident.reverified = incident.compare_at_view = False
            incident.keep_open = incident.asked = False
            incident.looks = []
            self._approach(incident)

    def _target(self, incident):
        """Where the incident's person is now, or their last certain place."""
        person = self.tracker.people.get(incident.person) if incident.person else None
        if (person is not None and person.point is not None
                and not self.tracker.uncertain(incident.person)):
            incident.place = person.point
        return incident.place

    def _bind(self, incident, person):
        """Whose incident it is, also for the yellow box on its video (decision 2)."""
        incident.person = person
        self.commands.append(dict(kind='people', incidents={incident.incident_id: person}))

    def _approach(self, incident):
        if self._target(incident) is None:
            self._posture(incident, None)  # No map place: look and ask from here.
            return
        if incident.purpose != 'revisit':  # Going back after waiting is revisit_started.
            self._record('approach_started', incident, None)
        incident.step = 'approach'
        self._command('drive', incident, purpose='approach', target=self._target(incident),
                      from_point=incident.first.robot, standoff=STANDOFF_M)

    def _look(self, incident, what, **fields):
        self._clip(incident, 'start')  # Looking without a drive first (no map place).
        incident.look_id = str(uuid4())
        self._command('look', incident, what=what, look_id=incident.look_id, **fields)

    def _compare(self, incident):
        incident.step = 'compare'
        self._look(incident, 'arrival', before=incident.first.photo, box=incident.first.box)

    def _reverify(self, incident):
        incident.reverified = True
        origin, target = incident.first.robot, self._target(incident)
        if origin is None or target is None:
            self._idle(incident, 'nobody', 'could_not_compare', None)
            return
        incident.step = 'reverify'
        self._command('drive', incident, purpose='reverify', target=target,
                      from_point=origin, standoff=max(STANDOFF_M, math.dist(origin, target)))

    def _view(self, incident):
        incident.step = 'view'
        if self._target(incident) is None:
            self._posture(incident, None)
            return
        self._command('drive', incident, purpose='view', target=self._target(incident),
                      standoff=VIEW_MAX_M)

    def _to_view_or_posture(self, incident, at_view, now):
        incident.compare_at_view = False
        if at_view:
            self._posture(incident, now)
        else:
            self._view(incident)

    def _posture(self, incident, now):
        incident.step = 'posture'
        incident.looks = []
        self._posture_look(incident)

    def _posture_look(self, incident):
        moments = incident.fall_moments
        self._look(incident, 'posture', fall_moments=tuple(moments[:1] + moments[1:][-1:]))

    @staticmethod
    def _ask_after(incident, verdict, earlier):
        """Whether this posture means asking (design 5, decision A)."""
        if verdict in ('floor', 'unclear'):
            return True
        if incident.fall_seen:
            if POSTURE_PLACE.get(verdict) == FLOOR:
                return True  # A fall was seen and the person is on the floor: always ask.
            return earlier != 'self'  # Up again: ask unless Cloud saw a controlled move.
        return False

    def _closer(self, incident):
        incident.step = 'closer'
        if self._target(incident) is None:
            self._ask(incident)
            return
        self._command('drive', incident, purpose='closer', target=self._target(incident),
                      standoff=STANDOFF_M)

    def _ask(self, incident):
        incident.step = 'ask'
        incident.asked = True
        self._record('question_requested', incident, None)
        self._command('ask', incident, fall_seen=incident.fall_seen)

    def _differ(self, incident):
        if not incident.keep_open:
            incident.keep_open = True
            self._notify(incident, 'person_may_differ')

    def _guarded(self, incident, reason, now):
        """Consent or connection went off during the check: stop, ask nothing (unchanged)."""
        incident.idle = 'person'
        self._record('analysis_unavailable', incident, reason or 'cloud_blocked')
        self._idle(incident, 'person', None, now)

    def _close(self, incident, reason, now, *, memory, quiet=False):
        if incident.urgent_sent and not quiet:
            self._notify(incident, 'closed_after_alert')  # Still open while it is told.
        incident.state = 'closed'
        incident.step = 'closed'
        incident.idle = None
        self._record('incident_resolved', incident, reason)
        if memory == 'person':
            place = POSTURE_PLACE.get(incident.posture, FLOOR)
            self._forget(incident.incident_id)
            if incident.person:
                self.memory.append(Memory(incident.person, 'person', place, now,
                                          incident.incident_id))
        elif memory == 'object' and incident.person:
            self.memory.append(Memory(incident.person, 'object', since=now))
        self._finish(incident, now, go_back=not incident.asked and not quiet,
                     why='not_a_person' if reason == 'not_a_person' else 'off_floor')

    def _idle(self, incident, who, notice, now):
        """Left open for the guardian: nobody verified there, or a person who needs help."""
        incident.idle = who
        incident.step = 'open'
        if notice:
            self._notify(incident, notice)
        self._finish(incident, now, go_back=False)

    def _finish(self, incident, now, *, go_back, why=None):
        self._clip(incident, 'end')  # The judgment: the video ends 5 s later.
        if go_back:  # why: the approach node's return reason (not_a_person, off_floor).
            self._command('drive', incident, purpose='return', reason=why)
        incident.waiting_since = None
        incident.next_look_at = None
        incident.look_id = None
        if self.active == incident.incident_id:
            self.active = None
        self._next(now)

    def _resume(self, incident, now):
        if incident.waiting_since is not None:
            incident.waited += max(0.0, now - incident.waiting_since)
            incident.waiting_since = None
        if (incident.purpose == 'revisit' and incident.wait_alerted
                and not incident.revisit_noted):
            incident.revisit_noted = True
            self._record('revisit_started', incident, incident.waiting_for or 'busy')
        self._clip(incident, 'start')  # The Manager took a step, or a step came back.

    def _new_round(self, incident, why):
        """Check it again: a new round with its own waiting and alerts (decision B)."""
        incident.check_round += 1
        incident.answer = None
        incident.urgent_sent = incident.wait_alerted = incident.revisit_noted = False
        incident.waited = 0.0
        incident.waiting_since = incident.waiting_for = None
        incident.waits_noted = set()
        self._record('recheck_started', incident, RECHECK_REASON[why], state='verifying')

    def _restart(self, incident, purpose, now):
        incident.idle = None
        self._new_round(incident, purpose)
        self._enqueue(incident, purpose)
        self._next(now)

    def _recheck(self, memory, purpose, now):
        """Check again someone remembered after a close (decision G, standing up)."""
        incident = self.incidents.get(memory.incident_id)
        if incident is None:
            memory.due = True  # Filtered, never opened: filtered again when next suspected.
            return
        self.memory.remove(memory)
        incident.state = 'open'
        incident.asked = incident.keep_open = False
        self._restart(incident, purpose, now)

    def _forget(self, incident_id):
        self.memory = [m for m in self.memory if m.incident_id != incident_id]

    def _attach(self, incident, s):
        if incident.person is None:
            self._bind(incident, s.person)  # After a restart: seen at its place.
        if s.point and not s.uncertain and s.person == incident.person:
            incident.place = s.point
        if s.fall_moment and s.fall_moment not in incident.fall_moments:
            incident.fall_moments.append(s.fall_moment)
            del incident.fall_moments[1:-1]  # The first and the last fall (design 5).
        # Its moment joins the incident's clip.
        self.commands.append(dict(kind='link', sighting_id=s.sighting_id,
                                  incident_id=incident.incident_id))

    def _open_match(self, s):
        """That person's open incident (design decision 6). Uncertain who it is: only an
        incident opened while uncertain, so another person is never missed (decision 4).
        Nothing joins "the latest incident" any more (10-17 decision A, 사람2 at 21:50)."""
        mine = [i for i in self.incidents.values()
                if i.state == 'open' and i.person is not None and i.person == s.person]
        if not s.uncertain:
            sure = next((i for i in mine if not i.uncertain), None)
            if sure is not None:
                return sure
        separate = next((i for i in mine if i.uncertain), None)
        if separate is not None or s.uncertain or s.point is None:
            return separate
        # After a restart nobody is bound yet: a suspicion at its place is its person.
        return next((i for i in self.incidents.values()
                     if i.state == 'open' and i.person is None and i.place
                     and math.dist(s.point, i.place) <= REAPPEAR_M), None)

    def _candidate_match(self, s):
        if s.uncertain:
            return None  # Maybe someone else: filtered separately.
        return next((c for c in self.candidates.values() if c.person == s.person), None)

    def _memory_match(self, s):
        if s.uncertain:
            return None  # Maybe someone else: from the first filter.
        return next((m for m in self.memory if m.person == s.person), None)

    def _current(self, incident_id, step, look_id=None):
        incident = self.incidents.get(incident_id)
        if (incident is None or incident_id != self.active or incident.step != step
                or (look_id is not None and look_id != incident.look_id)):
            return None
        return incident

    # ------------------------------------------------------------ outputs

    @staticmethod
    def _state(incident):
        """The web state (fall_events.json states) of an incident now."""
        if incident.state == 'closed':
            return 'resolved'
        if incident.answer == 'help_request':
            return 'help_required'
        return 'recheck_required' if incident.idle is not None else 'verifying'

    def _command(self, kind, incident, **fields):
        self.commands.append(dict(kind=kind, incident_id=incident.incident_id, **fields))

    def _clip(self, incident, mark):
        """The video of this round (SWM25-268): it starts when the robot gets to work, so
        a wait before that (직접 움직이기, another job, another incident) leaves the first
        suspicion's own range as 발견; it pauses for a wait and ends at the judgment."""
        if mark == 'start':
            if incident.clip_open or incident.incident_id != self.active:
                return
            incident.clip_open = True
        elif incident.clip_open:
            incident.clip_open = False
        else:
            return
        self._command('clip', incident, mark=mark, check_round=incident.check_round)

    def _web(self, incident, state):
        return dict(state=state or self._state(incident), fall_seen=incident.fall_seen,
                    answer=incident.answer, check_round=incident.check_round)

    def _record(self, event, incident, reason, *, state=None, **fields):
        if incident is None:  # Kept in the VLM's local journal only (never uploaded).
            self.commands.append(dict(kind='record', event=event, incident_id=None,
                                      reason=reason, **fields))
            return
        self.commands.append(dict(kind='record', event=event, incident_id=incident.incident_id,
                                  reason=reason, **self._web(incident, state), **fields))

    def _notify(self, incident, reason, *, state=None):
        level = NOTICES[reason]
        place = self._target(incident)
        self.sent = [n for n in self.sent if self.now - n[0] <= GROUP_S]
        earlier = next((n for n in reversed(self.sent) if n[1] == level and place and n[2]
                        and math.dist(place, n[2]) <= GROUP_M), None)
        if earlier is not None:
            # Not sent: "다시 확인함" on the incident that sent it, and on this one.
            for target in dict.fromkeys((earlier[3], incident.incident_id)):
                if target in self.incidents:
                    self._record('notification_grouped', self.incidents[target], reason)
            return
        if level == URGENT:
            incident.urgent_sent = True
        self.sent.append((self.now, level, place, incident.incident_id))
        self._command('notify', incident, reason=reason, level=level,
                      **self._web(incident, state))
