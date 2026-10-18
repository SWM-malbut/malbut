"""One fall check for every suspicion (SWM25-261, 2026-10-17): the Agent decides.

The VLM only reports what it saw (sightings) and what Cloud answered; the link
runs the robot's moves and questions and reports how they ended. This module
decides everything in between, the same way whether the robot goes the first
time, late after waiting, or again after a close:

  1. a fall suspicion is seen; Cloud's first filter drops what is clearly not a
     person, only an image, or ordinary (twice), and one incident is one
     person's one fall;
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
look (a Cloud look by the VLM), drive, ask, stop (the link), and record, notify
and link (written by the VLM in its journal, in the web's words:
malbut_interfaces/contracts/fall_events.json). Each record carries the web state,
whether a fall was seen, the latest answer and the check round.
Decisions: ~/fall-refactor-plan-20261017.md, ~/fall-refactor-design-20261017.md.
"""

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

# One person's one fall (design 3): the same person by a track seen again within
# seconds, then by map place, then by screen box while the camera stayed still;
# unknown attaches.
SAME_PLACE_M = 1.0
SAME_BOX_IOU = 0.3
# A YOLO track is handed to someone else after an occlusion (10-17 21:50, 95bd kept
# 사람2 in 사람1's incident for half an hour): it names the person only this soon after.
TRACK_KEEP_S = 10.0
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
# After a close (design 8): forget someone unseen for 30 minutes; recheck
# someone closed on the floor every 30 minutes (decision G).
FORGET_S = 1800.0
RECHECK_S = 1800.0

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
    """What the VLM saw. ``kind``: low, lying, motion (a fall motion), cloud (a
    Cloud crosscheck finding, already filtered), up (stood up) or seen (still there).

    ``robot`` is where the robot saw it from; ``view`` changes whenever the
    robot moves, so boxes compare only within one view.
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

    @property
    def fall_seen(self):
        return self.kind == 'motion' or self.fall_moment is not None

    @property
    def suspicion(self):
        return self.kind in ('low', 'lying', 'motion', 'cloud')


@dataclass
class Body:
    """Who a check, a filter or a memory is about, kept up to date by sightings."""

    tracks: dict = field(default_factory=dict)   # track -> when last seen as this person
    point: Optional[Tuple[float, float]] = None
    box: Optional[Tuple[float, float, float, float]] = None
    view: Optional[int] = None
    last_seen: float = 0.0

    def __post_init__(self):
        if not isinstance(self.tracks, dict):
            self.tracks = {track: self.last_seen for track in self.tracks}

    @classmethod
    def of(cls, s):
        return cls({s.track: s.at} if s.track else {}, s.point, s.box, s.view, s.at)

    def follows(self, s):
        """The same track seen again without a break: still this person."""
        return bool(s.track) and s.at - self.tracks.get(s.track, -math.inf) <= TRACK_KEEP_S

    def update(self, s, *, move=True):
        """``move``: a suspicion may move the place and box; a plain sighting only keeps
        the track alive (4-4 replay, bed 10-10 20:43)."""
        self.last_seen = max(self.last_seen, s.at)
        if not self.follows(s):
            # Matched by place or box only: who this is never moves to another person
            # (10-17: one wrong join moved the box to 사람2, and the rest followed).
            return
        self.tracks = {t: at for t, at in self.tracks.items() if s.at - at <= TRACK_KEEP_S}
        self.tracks[s.track] = s.at
        if move:
            self.point = s.point or self.point
            if s.box:
                self.box, self.view = s.box, s.view


def _iou(a, b):
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def same_person(body, s):
    """True, False, or None when it cannot be told (design 3)."""
    if body.follows(s):
        return True
    if s.point and body.point:
        return math.dist(s.point, body.point) <= SAME_PLACE_M
    if s.box and body.box and s.view is not None and s.view == body.view:
        return _iou(s.box, body.box) >= SAME_BOX_IOU
    return None


@dataclass
class Incident:
    incident_id: str
    first: Sighting
    body: Body
    opened_at: float
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

    @property
    def fall_seen(self):
        return bool(self.fall_moments) or self.first.fall_seen


@dataclass
class Candidate:
    sighting: Sighting
    body: Body
    drops: List[str] = field(default_factory=list)
    next_look_at: Optional[float] = None
    look_id: Optional[str] = None


@dataclass
class Memory:
    """Someone checked or filtered as not a fall, or a filtered object (track only)."""

    body: Body
    kind: str                     # person or object
    place: Optional[str] = None   # floor or off_floor, for a person
    since: float = 0.0
    incident_id: Optional[str] = None
    # Filtered 30 minutes ago and never opened: filtered again on the next suspicion.
    due: bool = False


class FallFlow:
    def __init__(self):
        self.commands = []
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
                    alerted=incident.wait_alerted, target=incident.body.point,
                    check_round=incident.check_round)

    # ------------------------------------------------------------ inputs: VLM

    def sighted(self, s, now):
        """A suspicion or a sighting of someone watched after a close."""
        checking = self.incidents.get(self.active)
        if s.fall_seen and checking is not None and same_person(checking.body, s) is not False:
            self._attach(checking, s)  # A fall while being checked joins that check.
            return
        open_incident = None if s.fall_seen else self._open_match(s, strict=not s.suspicion)
        if open_incident is not None:
            self._attach(open_incident, s)
            if open_incident.idle == 'nobody' and s.suspicion:
                self._restart(open_incident, 'again', now)  # Someone there now (design 3).
            return
        if s.suspicion and self._candidate_match(s):
            return  # Already waiting for Cloud's first filter.
        memory = self._memory_match(s)
        if memory is not None and s.fall_seen:
            self.memory.remove(memory)  # A new fall: start afresh (design 3).
        elif memory is not None:
            memory.body.update(s)
            if memory.due and s.suspicion:
                # 30 minutes on, filtered again with what is seen now (2026-10-18: a copy
                # of the old sighting kept 30-minute-old data and an id the VLM never saw).
                self.memory.remove(memory)
                self._candidate(s, now)
            elif s.kind == 'up' and memory.kind == 'person':
                self._recheck(memory, 'stood_up', now)
            elif (memory.kind == 'person' and memory.place == OFF_FLOOR
                  and s.kind in ('lying', 'cloud')):
                self.memory.remove(memory)  # Closed off the floor, now down (decision F).
                self._candidate(s, now)
            return
        if s.suspicion:
            self._candidate(s, now)

    def filter_answered(self, sighting_id, verdict, now, *, look_id=None, reason=None):
        """Cloud's first filter: suspect, not_person, reflection, ordinary, failed or guard."""
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
                self.memory.append(Memory(candidate.body, 'person', place, now))
            elif candidate.sighting.track:
                self.memory.append(Memory(Body({candidate.sighting.track}, last_seen=now),
                                          'object', since=now))
            return
        del self.candidates[sighting_id]
        self._open(candidate.sighting, candidate.body, now, guard=verdict == 'guard',
                   look_id=look_id if verdict == 'suspect' else None, guard_reason=reason)

    def arrival_answered(self, incident_id, verdict, now, *, look_id=None, reason=None):
        """First sight vs arrival (design 6)."""
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
        incident = self.incidents.get(incident_id)
        if incident is None or incident_id != self.active:
            return
        reason = reason if reason in WAITING_FOR else 'busy'
        if incident.waiting_since is None:
            incident.waiting_since = now
        incident.waiting_for = reason
        if reason not in incident.waits_noted:
            incident.waits_noted.add(reason)
            self._record('check_waiting', incident, reason)

    def resumed(self, incident_id, now):
        """The Manager took the step: the waiting (if any) is over."""
        incident = self.incidents.get(incident_id)
        if incident is not None and incident_id == self.active:
            self._resume(incident, now)

    def voice(self, kind, now):
        """"도와줘" or "그만" heard during a check (2026-10-13)."""
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
            if now - memory.body.last_seen >= FORGET_S and now - memory.since >= FORGET_S:
                self.memory.remove(memory)  # Unseen for 30 minutes: start afresh next time.
            elif (memory.kind == 'person' and memory.place == FLOOR and not memory.due
                  and now - memory.since >= RECHECK_S):
                self._recheck(memory, 'recheck', now)

    def restore(self, incidents, now):
        """After a restart: every open incident is checked again from the start."""
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
            self._open(s, Body.of(s), now)
            return
        candidate = Candidate(s, Body.of(s))
        self.candidates[s.sighting_id] = candidate
        self._filter_look(candidate)

    def _filter_look(self, candidate):
        s = candidate.sighting
        candidate.look_id = str(uuid4())
        # A fall motion is never dropped as ordinary: sitting after a fall looks it (design 4).
        self.commands.append(dict(kind='look', what='filter', look_id=candidate.look_id,
                                  sighting_id=s.sighting_id, photo=s.photo, box=s.box,
                                  track=s.track, ordinary_drops=not s.fall_seen))

    def _open(self, s, body, now, *, guard=False, look_id=None, guard_reason=None):
        incident = Incident(str(uuid4()), s, body, now)
        if s.fall_moment:
            incident.fall_moments.append(s.fall_moment)
        self.incidents[incident.incident_id] = incident
        self._record('incident_opened', incident, OPEN_REASON.get(s.kind),
                     sighting_id=s.sighting_id)
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

    def _approach(self, incident):
        if incident.body.point is None:
            self._posture(incident, None)  # No map place: look and ask from here.
            return
        if incident.purpose != 'revisit':  # Going back after waiting is revisit_started.
            self._record('approach_started', incident, None)
        incident.step = 'approach'
        self._command('drive', incident, purpose='approach', target=incident.body.point,
                      from_point=incident.first.robot, standoff=STANDOFF_M)

    def _look(self, incident, what, **fields):
        incident.look_id = str(uuid4())
        self._command('look', incident, what=what, look_id=incident.look_id, **fields)

    def _compare(self, incident):
        incident.step = 'compare'
        self._look(incident, 'arrival', before=incident.first.photo, box=incident.first.box)

    def _reverify(self, incident):
        incident.reverified = True
        origin = incident.first.robot
        if origin is None or incident.body.point is None:
            self._idle(incident, 'nobody', 'could_not_compare', None)
            return
        incident.step = 'reverify'
        self._command('drive', incident, purpose='reverify', target=incident.body.point,
                      from_point=origin,
                      standoff=max(STANDOFF_M, math.dist(origin, incident.body.point)))

    def _view(self, incident):
        incident.step = 'view'
        if incident.body.point is None:
            self._posture(incident, None)
            return
        self._command('drive', incident, purpose='view', target=incident.body.point,
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
        if incident.body.point is None:
            self._ask(incident)
            return
        self._command('drive', incident, purpose='closer', target=incident.body.point,
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
            self.memory.append(Memory(incident.body, 'person', place, now, incident.incident_id))
        elif memory == 'object' and incident.body.tracks:
            self.memory.append(Memory(Body(set(incident.body.tracks), last_seen=now), 'object',
                                      since=now))
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
        incident.body = memory.body
        incident.asked = incident.keep_open = False
        self._restart(incident, purpose, now)

    def _forget(self, incident_id):
        self.memory = [m for m in self.memory if m.incident_id != incident_id]

    def _attach(self, incident, s):
        # Only the same track seen again moves who this is (Body.update), and only a
        # suspicion moves the place and box (4-4 replay bed 10-10 20:43, 10-17 21:50).
        incident.body.update(s, move=s.suspicion)
        if not s.suspicion:
            return
        if s.fall_moment and s.fall_moment not in incident.fall_moments:
            incident.fall_moments.append(s.fall_moment)
            del incident.fall_moments[1:-1]  # The first and the last fall (design 5).
        # Its moment joins the incident's clip.
        self.commands.append(dict(kind='link', sighting_id=s.sighting_id,
                                  incident_id=incident.incident_id))

    def _open_match(self, s, *, strict):
        unknown = None
        for incident in self.incidents.values():
            if incident.state != 'open':
                continue
            same = same_person(incident.body, s)
            if same:
                return incident
            if same is None and (unknown is None
                                 or incident.body.last_seen > unknown.body.last_seen):
                unknown = incident
        # A suspicion that cannot be told apart joins the open incident (decision A);
        # a plain sighting never does, so it cannot lend its track to someone else.
        return None if strict else unknown

    def _candidate_match(self, s):
        for candidate in self.candidates.values():
            if same_person(candidate.body, s):
                candidate.body.update(s)
                return candidate
        return None

    def _memory_match(self, s):
        for memory in self.memory:
            if memory.kind == 'object':
                if s.track and s.track in memory.body.tracks:
                    return memory  # Objects only by their track (design 8).
            elif same_person(memory.body, s) is not False:
                return memory
        return None

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
        if level == URGENT:
            incident.urgent_sent = True
        self._command('notify', incident, reason=reason, level=level,
                      **self._web(incident, state))
