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
the floor start the check again.

Pure: no ROS, transport or clock. Callers pass the time and drain ``commands``.
Decisions: ~/fall-refactor-plan-20261017.md, ~/fall-refactor-design-20261017.md.
"""

from dataclasses import dataclass, field, replace
import itertools
import math
from typing import Dict, List, Optional, Tuple

# One person's one fall (design 3): the same person by track, then by map
# place, then by screen box while the camera stayed still; unknown attaches.
SAME_PLACE_M = 1.0
SAME_BOX_IOU = 0.3
STANDOFF_M = 1.0
VIEW_MAX_M = 3.0
# A drop needs a second filter look a few seconds later (design 4).
FILTER_AGAIN_S = 3.0
# Closing without a question needs two posture looks that agree (2026-10-11).
POSTURE_AGAIN_S = 2.5
# Waiting on 직접 움직이기 or another job (2026-10-12): an urgent alert after a
# minute, and the robot still goes later; it stops trying after ten minutes.
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

    tracks: set = field(default_factory=set)
    point: Optional[Tuple[float, float]] = None
    box: Optional[Tuple[float, float, float, float]] = None
    view: Optional[int] = None
    last_seen: float = 0.0

    @classmethod
    def of(cls, s):
        return cls({s.track} if s.track else set(), s.point, s.box, s.view, s.at)

    def update(self, s):
        if s.track:
            self.tracks.add(s.track)
        self.point = s.point or self.point
        if s.box:
            self.box, self.view = s.box, s.view
        self.last_seen = max(self.last_seen, s.at)


def _iou(a, b):
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def same_person(body, s):
    """True, False, or None when it cannot be told (design 3)."""
    if s.track and s.track in body.tracks:
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
    purpose: str = 'approach'    # approach, revisit (after waiting), recheck, stood_up, again
    idle: Optional[str] = None   # open but not being checked: nobody (no person verified) or person
    reverified: bool = False     # looked again from where it was first seen
    compare_at_view: bool = False
    keep_open: bool = False      # the first person was never verified (design 6)
    asked: bool = False
    urgent_sent: bool = False
    posture: Optional[str] = None
    looks: List[str] = field(default_factory=list)
    next_look_at: Optional[float] = None
    waiting_since: Optional[float] = None
    wait_alerted: bool = False

    @property
    def fall_seen(self):
        return bool(self.fall_moments) or self.first.fall_seen


@dataclass
class Candidate:
    sighting: Sighting
    body: Body
    drops: List[str] = field(default_factory=list)
    next_look_at: Optional[float] = None


@dataclass
class Memory:
    """Someone checked or filtered as not a fall, or a filtered object (track only)."""

    body: Body
    kind: str                     # person or object
    place: Optional[str] = None   # floor or off_floor, for a person
    since: float = 0.0
    incident_id: Optional[str] = None
    source: Optional[Sighting] = None


class FallFlow:
    def __init__(self):
        self.commands = []
        self.incidents: Dict[str, Incident] = {}
        self.candidates: Dict[str, Candidate] = {}
        self.memory: List[Memory] = []
        self.queue: List[str] = []
        self.active: Optional[str] = None
        self._ids = itertools.count(1)

    def drain(self):
        out, self.commands = self.commands, []
        return out

    # ------------------------------------------------------------ status

    def status(self):
        """For the fall check status: which incident, which step, and why it went."""
        incident = self.incidents.get(self.active)
        if incident is None:
            return dict(active=False, incident_id=None, step=None, purpose=None)
        step = 'wait' if incident.waiting_since is not None else incident.step
        return dict(active=True, incident_id=incident.incident_id, step=step,
                    purpose=incident.purpose)

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
            if s.kind == 'up' and memory.kind == 'person':
                self._recheck(memory, 'stood_up', now)
            elif (memory.kind == 'person' and memory.place == OFF_FLOOR
                  and s.kind in ('lying', 'cloud')):
                self.memory.remove(memory)  # Closed off the floor, now down (decision F).
                self._candidate(s, now)
            return
        if s.suspicion:
            self._candidate(s, now)

    def filter_answered(self, sighting_id, verdict, now):
        """Cloud's first filter: suspect, not_person, reflection, ordinary, failed or guard."""
        candidate = self.candidates.get(sighting_id)
        if candidate is None:
            return
        if verdict in DROPS and not (verdict == 'ordinary' and candidate.sighting.fall_seen):
            candidate.drops.append(verdict)
            if len(candidate.drops) < 2:
                candidate.next_look_at = now + FILTER_AGAIN_S
                return
            del self.candidates[sighting_id]
            self._record('candidate_dropped', None, verdict)
            if verdict == 'ordinary':
                place = OFF_FLOOR if candidate.sighting.on_floor is False else FLOOR
                self.memory.append(Memory(candidate.body, 'person', place, now,
                                          source=candidate.sighting))
            elif candidate.sighting.track:
                self.memory.append(Memory(Body({candidate.sighting.track}, last_seen=now),
                                          'object', since=now))
            return
        del self.candidates[sighting_id]
        self._open(candidate.sighting, candidate.body, now, guard=verdict == 'guard')

    def arrival_answered(self, incident_id, verdict, now):
        """First sight vs arrival (design 6)."""
        incident = self._current(incident_id, 'compare')
        if incident is None:
            return
        incident.waiting_since = None
        self._record('arrival_compared', incident, verdict)
        at_view = incident.compare_at_view
        if verdict == 'failed':
            verdict = 'person_unsure'  # Cannot compare by Cloud: keep checking the person.
        if verdict == 'same_person':
            self._to_view_or_posture(incident, at_view, now)
        elif verdict == 'person_unsure':
            if at_view:
                self._differ(incident)  # Still unsure with the whole body in view.
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
                self._command('look', incident, what='departure', before=incident.first.photo,
                              box=incident.first.box)
            else:
                self._idle(incident, 'nobody', 'could_not_compare', now)

    def departure_answered(self, incident_id, verdict, now):
        """What left the spot, from the frames between first sight and arrival."""
        incident = self._current(incident_id, 'departure')
        if incident is None:
            return
        self._record('departure_reviewed', incident, verdict)
        if verdict in ('animal_left', 'person_walked'):
            self._close(incident, 'left', now, memory=None)
        elif verdict == 'person_crawled':
            self._idle(incident, 'nobody', 'person_crawled_away', now)
        else:
            self._idle(incident, 'nobody', 'left_unseen', now)

    def posture_answered(self, incident_id, verdict, earlier, now):
        """Where the body rests now, and (fall seen) whether the earlier moment was a fall."""
        incident = self._current(incident_id, 'posture')
        if incident is None:
            return
        self._record('posture_checked', incident, verdict)
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
        reason = 'floor_activity_verified' if verdict == 'floor_active' else 'off_floor_verified'
        self._close(incident, reason, now, memory='person')

    # ------------------------------------------------------------ inputs: the link

    def drive_done(self, incident_id, purpose, outcome, now):
        incident = self.incidents.get(incident_id)
        if incident is None or incident_id != self.active:
            return
        incident.waiting_since = None
        if purpose == 'approach':
            if outcome == 'no_map':
                self._ask(incident)  # No place on the map: ask where it stands.
                return
            # Arrived on the circle, or facing the spot from the best place it reached.
            self._compare(incident)
        elif purpose == 'reverify':
            if outcome == 'arrived':
                self._compare(incident)
            else:
                self._idle(incident, 'nobody', 'could_not_compare', now)
        elif purpose == 'view':
            if incident.compare_at_view:
                self._compare(incident)
            else:
                self._posture(incident, now)
        elif purpose == 'closer':
            self._ask(incident)  # From the nearest place it reached (design 7).

    def asked(self, incident_id, answer, now):
        """okay, help (help or no answer), unclear (who answered) or failed (no question)."""
        incident = self.incidents.get(incident_id)
        if incident is None or incident_id != self.active or incident.step != 'ask':
            return
        incident.waiting_since = None
        self._record('question_answered', incident, answer)
        if answer == 'okay':
            if incident.keep_open:
                self._idle(incident, 'nobody', None, now)  # The first person is still unknown.
            else:
                self._close(incident, 'okay', now, memory='person')
        elif answer == 'help':
            self._idle(incident, 'person', 'confirmation_help_required', now)
        elif answer == 'unclear':
            self._idle(incident, 'person', 'scene_answer_unconfirmed', now)
        else:
            self._idle(incident, 'person', 'robot_could_not_confirm', now)

    def blocked(self, incident_id, reason, now):
        """The Manager refused or interrupted the step; the link sends it again."""
        incident = self.incidents.get(incident_id)
        if incident is None or incident_id != self.active:
            return
        if incident.waiting_since is None:
            incident.waiting_since = now
            self._record('check_waiting', incident, reason)

    def voice(self, kind, now):
        """"도와줘" or "그만" heard during a check (2026-10-13)."""
        incident = self.incidents.get(self.active)
        if incident is None:
            return
        if kind == 'help':
            self._command('stop', incident)
            self._idle(incident, 'person', 'help_requested', now)
        elif kind == 'stop':
            self._command('stop', incident)
            self._record('check_stopped', incident, 'user_stop')
            self._idle(incident, 'person', 'check_stopped_by_user', now)

    def guardian_closed(self, incident_id, now):
        incident = self.incidents.get(incident_id)
        if incident is None or incident.state == 'closed':
            return
        if incident_id == self.active:
            self._command('stop', incident)
        self._close(incident, 'guardian_closed', now, memory=None, quiet=True)

    def tick(self, now):
        for sighting_id, candidate in list(self.candidates.items()):
            if candidate.next_look_at is not None and now >= candidate.next_look_at:
                candidate.next_look_at = None
                self._filter_look(candidate)
        incident = self.incidents.get(self.active)
        if incident is not None:
            if incident.next_look_at is not None and now >= incident.next_look_at:
                incident.next_look_at = None
                self._posture_look(incident)
            if incident.waiting_since is not None:
                waited = now - incident.waiting_since
                if waited >= WAIT_ALERT_S and not incident.wait_alerted:
                    incident.wait_alerted = True
                    incident.purpose = 'revisit'
                    self._notify(incident, 'robot_could_not_confirm')
                if waited >= WAIT_LIMIT_S:
                    self._command('stop', incident)
                    self._idle(incident, 'person', None, now)
        for memory in list(self.memory):
            if now - memory.body.last_seen >= FORGET_S and now - memory.since >= FORGET_S:
                self.memory.remove(memory)  # Unseen for 30 minutes: start afresh next time.
            elif (memory.kind == 'person' and memory.place == FLOOR
                  and now - memory.since >= RECHECK_S):
                self._recheck(memory, 'recheck', now)

    def restore(self, incidents, now):
        """After a restart: every open incident is checked again from the start."""
        for incident in incidents:
            if incident.state != 'open':
                continue
            self.incidents[incident.incident_id] = incident
            incident.idle = None
            self._enqueue(incident, 'again')
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
        # A fall motion is never dropped as ordinary: sitting after a fall looks it (design 4).
        self.commands.append(dict(kind='look', what='filter', sighting_id=s.sighting_id,
                                  photo=s.photo, box=s.box, ordinary_drops=not s.fall_seen))

    def _open(self, s, body, now, *, guard=False):
        incident = Incident(f'i{next(self._ids)}', s, body, now)
        if s.fall_moment:
            incident.fall_moments.append(s.fall_moment)
        self.incidents[incident.incident_id] = incident
        self._record('incident_opened', incident, s.kind)
        if guard:  # Consent or connection off: no Cloud look, no question (unchanged).
            incident.idle = 'person'
            incident.step = 'guard'
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
        incident.step = 'approach'
        self._command('drive', incident, purpose='approach', target=incident.body.point,
                      from_point=incident.first.robot, standoff=STANDOFF_M)

    def _compare(self, incident):
        incident.step = 'compare'
        self._command('look', incident, what='arrival', before=incident.first.photo,
                      box=incident.first.box)

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
        self._command('look', incident, what='posture',
                      fall_moments=tuple(moments[:1] + moments[1:][-1:]))

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
        self._command('ask', incident)

    def _differ(self, incident):
        if not incident.keep_open:
            incident.keep_open = True
            self._notify(incident, 'person_may_differ')

    def _close(self, incident, reason, now, *, memory, quiet=False):
        incident.state = 'closed'
        incident.step = 'closed'
        incident.idle = None
        self._record('incident_resolved', incident, reason)
        if incident.urgent_sent and not quiet:
            self._notify(incident, 'closed_after_alert')
        if memory == 'person':
            place = POSTURE_PLACE.get(incident.posture, FLOOR)
            self._forget(incident.incident_id)
            self.memory.append(Memory(incident.body, 'person', place, now, incident.incident_id))
        elif memory == 'object' and incident.body.tracks:
            self.memory.append(Memory(Body(set(incident.body.tracks), last_seen=now), 'object',
                                      since=now))
        self._finish(incident, now, go_back=not incident.asked and not quiet)

    def _idle(self, incident, who, notice, now):
        """Left open for the guardian: nobody verified there, or a person who needs help."""
        incident.idle = who
        incident.step = 'open'
        if notice:
            self._notify(incident, notice)
        self._finish(incident, now, go_back=False)

    def _finish(self, incident, now, *, go_back):
        if go_back:
            self._command('drive', incident, purpose='return')
        incident.waiting_since = None
        incident.next_look_at = None
        if self.active == incident.incident_id:
            self.active = None
        self._next(now)

    def _restart(self, incident, purpose, now):
        self._enqueue(incident, purpose)
        incident.idle = None
        self._next(now)

    def _recheck(self, memory, purpose, now):
        """Check again someone remembered after a close (decision G, standing up)."""
        self.memory.remove(memory)
        incident = self.incidents.get(memory.incident_id)
        if incident is not None:
            incident.state = 'open'
            incident.body = memory.body
            incident.asked = incident.keep_open = False
            self._record('incident_reopened', incident, purpose)
            self._restart(incident, purpose, now)
        elif memory.source is not None:  # Filtered, never opened: filter again.
            s = replace(memory.source, sighting_id=f'{memory.source.sighting_id}+{int(now)}',
                        at=now)
            self._candidate(s, now)

    def _forget(self, incident_id):
        self.memory = [m for m in self.memory if m.incident_id != incident_id]

    def _attach(self, incident, s):
        incident.body.update(s)
        if s.fall_moment and s.fall_moment not in incident.fall_moments:
            incident.fall_moments.append(s.fall_moment)
            del incident.fall_moments[1:-1]  # The first and the last fall (design 5).

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

    def _current(self, incident_id, step):
        incident = self.incidents.get(incident_id)
        if incident is None or incident_id != self.active or incident.step != step:
            return None
        return incident

    # ------------------------------------------------------------ outputs

    def _command(self, kind, incident, **fields):
        self.commands.append(dict(kind=kind, incident_id=incident.incident_id, **fields))

    def _record(self, event, incident, reason):
        self.commands.append(dict(kind='record', event=event,
                                  incident_id=incident.incident_id if incident else None,
                                  reason=reason))

    def _notify(self, incident, reason):
        level = NOTICES[reason]
        if level == URGENT:
            incident.urgent_sent = True
        self._command('notify', incident, reason=reason, level=level)
