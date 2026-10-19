"""Who is who across YOLO-Pose frames, for the fall rules (SWM25-267, 2026-10-19).

The VLM sends every frame as numbers (``fall_people``): each person's YOLO track,
box, posture and map place by depth. This module decides who is the same person
(input, judgment and action: the judgment belongs to the Agent). The fall rules
then attach an incident to a person, not to a sighting.

A frame's observations go to people in this order (design decision 3):
  1. the same YOLO track seen within a second, unless the place jumped further than
     the person could have gone (YOLO handed the track to someone else, 10-17 95bd);
  2. the nearest person seen within five seconds who could have gone that far;
  3. without a map place (no depth): the same view's box overlap;
  4. someone unseen for up to 30 minutes within 1 m of where they were last seen;
  5. otherwise a new person.
Two people too close to tell apart are "uncertain" (decision 4): the tracking goes on
with the likeliest one, and the fall rules check a suspicion of either separately.
Without a map place only someone near counts: the same view's box overlapping, or
someone just gone from another view, whose box cannot be compared (design decision 2).
It clears when one of them stayed put while the other came and left, when depth comes
back for a number made without it at the last place of one of them (decision 2, as
reappearing within 1 m), or when the other is forgotten (30 minutes unseen). Nothing
else clears it; Cloud's "same person" does not (it told different people apart 1 of 8
times on 10-18).

ReID only splits (decision 10): someone reappearing where one unseen person was is that
person, uncertain until the VLM says how alike the two looked (two looks against two);
clearly unlike (below REID_SPLIT) is someone else. It never joins anyone.

Pure: no ROS, transport or clock. Starting values, to be tuned on recorded days:
~/fall-person-tracking-design-20261019.md (decisions 3-5).
"""

from collections import deque
from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

TRACK_GAP_S = 1.0       # a YOLO track names a person this long after it was last seen
BASE_M = 0.5            # how far someone could have gone: BASE_M + WALK_MPS x time unseen
WALK_MPS = 1.5
MAX_M = 3.0
AMBIGUOUS_M = 0.5       # two candidates closer than this to each other: cannot choose
BOX_IOU = 0.3           # without a map place, the same view's boxes must overlap this much
SHORT_GONE_S = 5.0      # unseen longer than this: "unseen", matched only by reappearing
REAPPEAR_M = 1.0        # an unseen person seen again within 1 m is the same person
FORGET_S = 1800.0       # unseen for 30 minutes: forgotten (the same as the fall rules)
MAX_PEOPLE = 64
MERGE_M = 0.5           # two people this close: who is who becomes uncertain
MERGE_IOU = 0.5         # the same without map places
STAY_S, STAY_M = 10.0, 0.3   # stayed put before the encounter: within STAY_M for STAY_S
RESOLVE_M = 0.5         # after they part, the one observation near that place is that person
# Appearance similarity below this: someone else. The robot's encoder on the Jetson,
# two against two, 10-17 pairs (10-19): 5 of 8 others split, 0 of 9 same people.
REID_SPLIT = 0.62
REID_WAIT_S = 5.0       # no answer by then: by place alone


@dataclass(frozen=True)
class Observation:
    """One person in one frame, as the VLM measured it."""

    track: Optional[str]
    box: Optional[Tuple[float, float, float, float]]
    point: Optional[Tuple[float, float]]
    state: str = 'unknown'   # clear, suspected, unknown (YOLO-Pose posture check)


@dataclass(frozen=True)
class Frame:
    at: float
    view: int
    people: Tuple[Observation, ...]


@dataclass
class Person:
    person_id: str
    track: Optional[str]
    point: Optional[Tuple[float, float]]
    point_at: float
    box: Optional[Tuple[float, float, float, float]]
    view: Optional[int]
    first_seen: float
    last_seen: float
    posture: str = 'unknown'
    places: deque = field(default_factory=lambda: deque(maxlen=64))  # (at, point)
    unplaced: bool = False   # made without a map place while uncertain: recovered by place


@dataclass
class Group:
    """People who may have swapped; ``anchors``: who stayed put before, and where."""

    members: set
    since: float
    anchors: Dict[str, Tuple[float, float]] = field(default_factory=dict)


@dataclass
class FrameResult:
    assignments: Dict[str, str]                         # YOLO track -> person in this frame
    changed: Dict[str, str]                             # tracks whose person changed
    stood_up: List[str]                                 # posture suspected -> clear
    merged: Dict[str, str] = field(default_factory=dict)  # retired number -> that person


def _iou(a, b):
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


class PersonTracker:
    def __init__(self, run_id=None):
        # A new prefix each start: an Agent restarted under a running VLM never reuses
        # a person number for someone else (the people boxes are kept by number).
        self.run_id = run_id or uuid4().hex[:4]
        self.people: Dict[str, Person] = {}
        self.groups: List[Group] = []
        self._count = 0
        self._sent: Dict[str, str] = {}   # track -> person last reported
        self.reid = True
        self.pending: Dict[str, dict] = {}   # ReID request -> who reappeared, and as what
        self._requests: List[dict] = []

    # ------------------------------------------------------------ queries

    def uncertain(self, person_id):
        return (any(person_id in g.members for g in self.groups)
                or any(p['person'] == person_id for p in self.pending.values()))

    def take_requests(self):
        """ReID comparisons to ask the VLM: request_id, a (before) and b (after)."""
        out, self._requests = self._requests, []
        return out

    def person_of(self, track):
        return self._sent.get(track)

    # ------------------------------------------------------------ inputs

    def update(self, frame):
        """One pose frame: who each observation is, and what changed."""
        t = frame.at
        self._forget(t)
        self.expire(t)
        taken = {}
        stood_up = []
        for obs, person in self._match(frame.people, t, frame.view):
            taken[id(obs)] = person
            if self._apply(person, obs, t, frame.view):
                stood_up.append(person.person_id)
        seen = [(obs, taken[id(obs)]) for obs in frame.people if id(obs) in taken]
        merged = self._recover(seen, t)
        self._merge(seen, t)
        self._release(seen)
        # After a release may have swapped two people: each one's track now.
        assignments = {person.track: person.person_id for _, person in seen if person.track}
        changed = {k: v for k, v in assignments.items() if self._sent.get(k) != v}
        self._sent.update(changed)
        return FrameResult(assignments, changed, stood_up, merged)

    def expire(self, t):
        """ReID questions unanswered for REID_WAIT_S: by place alone."""
        for request_id, wait in list(self.pending.items()):
            if t - wait['since'] > REID_WAIT_S:
                del self.pending[request_id]

    def assign(self, obs, at, view):
        """A suspicion, which the VLM sends before its frame: (person, uncertain)."""
        self._forget(at)
        self.expire(at)
        (_, person), = self._match((obs,), at, view)
        self._apply(person, obs, at, view)
        # Its track's number goes to the VLM with the next frame, which follows at once.
        return person.person_id, self.uncertain(person.person_id)

    # ------------------------------------------------------------ matching

    def _reach(self, person, t):
        return min(MAX_M, BASE_M + WALK_MPS * max(0.0, t - person.point_at))

    def _match(self, observations, t, view):
        """Each observation with its person (an existing one or a new one)."""
        result, used, rest = [], set(), []
        # 1. The same YOLO track, unless the place jumped (the track went to someone else).
        for obs in observations:
            person = next((p for p in self.people.values() if obs.track and p.track == obs.track
                           and t - p.last_seen <= TRACK_GAP_S and p.person_id not in used), None)
            if (person is not None and obs.point and person.point
                    and math.dist(obs.point, person.point) > self._reach(person, t)):
                person.track = None
                person = None
            if person is None:
                rest.append(obs)
            else:
                used.add(person.person_id)
                result.append((obs, person))
        # 2. The nearest person seen within five seconds who could have gone that far.
        recent = [p for p in self.people.values() if t - p.last_seen <= SHORT_GONE_S]
        pairs = sorted(
            ((math.dist(obs.point, p.point), i, p) for i, obs in enumerate(rest) if obs.point
             for p in recent if p.point and math.dist(obs.point, p.point) <= self._reach(p, t)),
            key=lambda x: (x[0], x[1]))
        placed = set()
        for distance, i, person in pairs:
            if i in placed or person.person_id in used:
                continue
            obs = rest[i]
            rivals = [p for d, j, p in pairs if j == i and p is not person
                      and p.person_id not in used and d - distance < AMBIGUOUS_M]
            placed.add(i)
            used.add(person.person_id)
            result.append((obs, person))
            if rivals:
                self._group([person, *rivals], t)  # Cannot choose: the likeliest, uncertain.
        rest = [obs for i, obs in enumerate(rest) if i not in placed]
        # 3. No map place: the same view's box overlap.
        left = []
        for obs in rest:
            person = None
            if obs.point is None and obs.box is not None:
                scored = [(_iou(obs.box, p.box), p) for p in recent
                          if p.person_id not in used and p.box and p.view == view]
                best = max(scored, key=lambda x: x[0], default=(0.0, None))
                person = best[1] if best[0] >= BOX_IOU else None
            if person is None:
                left.append(obs)
            else:
                used.add(person.person_id)
                result.append((obs, person))
        # 4-5. Seen again after a while, or a new person.
        for obs in left:
            person = self._reappear(obs, t, view, used)
            used.add(person.person_id)
            result.append((obs, person))
        return result

    def _reappear(self, obs, t, view, used):
        gone = [p for p in self.people.values() if p.person_id not in used]
        if obs.point is not None:
            near = sorted(((math.dist(obs.point, p.point), p) for p in gone
                           if p.point and t - p.last_seen > SHORT_GONE_S
                           and math.dist(obs.point, p.point) <= REAPPEAR_M), key=lambda x: x[0])
            if not near:
                return self._new(obs, t)
            person = near[0][1]
            rivals = [p for d, p in near[1:] if d - near[0][0] < AMBIGUOUS_M]
            if rivals:
                self._group([person, *rivals], t)
            elif self.reid and person.track and obs.track:
                self._ask_appearance(person, obs, t)
            return person
        # No map place: anyone near who could be this one makes it uncertain (exception
        # "else decision 4"); only when nobody could be is it simply a new person. Near
        # is the same view's last box overlapping; boxes from another view do not
        # compare (the robot moved), so there anyone gone within five seconds could be.
        # (10-19, design decision 2 "근처": it had counted anyone gone within five
        # seconds anywhere on screen.)
        plausible = []
        for p in gone:
            if obs.box and p.box and p.view == view:
                if _iou(obs.box, p.box) > 0:
                    plausible.append(p)
            elif t - p.last_seen <= SHORT_GONE_S:
                plausible.append(p)
        person = self._new(obs, t)
        if plausible:
            person.unplaced = True
            self._group([person, *plausible], t)
        return person

    def _ask_appearance(self, person, obs, t):
        request_id = uuid4().hex
        self.pending[request_id] = dict(
            person=person.person_id, since=t,
            before={k: getattr(person, k) for k in (
                'track', 'point', 'point_at', 'box', 'view', 'last_seen', 'posture')})
        self._requests.append(dict(request_id=request_id,
                                   a=dict(track=person.track, at=person.last_seen),
                                   b=dict(track=obs.track, at=t)))

    def answer(self, request_id, similarity):
        """The VLM's appearance similarity; a split returns (old, new person, since)."""
        wait = self.pending.pop(request_id, None)
        person = self.people.get(wait['person']) if wait else None
        if person is None or similarity is None or similarity >= REID_SPLIT:
            return None  # Alike enough, or unknown: by place.
        new = self._new(Observation(None, None, None), wait['since'])
        new.first_seen = wait['since']
        for name in ('track', 'point', 'point_at', 'box', 'view', 'last_seen', 'posture'):
            setattr(new, name, getattr(person, name))
            setattr(person, name, wait['before'][name])
        new.places = deque((p for p in person.places if p[0] >= wait['since']), maxlen=64)
        person.places = deque((p for p in person.places if p[0] < wait['since']), maxlen=64)
        if new.track:
            self._sent[new.track] = new.person_id
        return person.person_id, new.person_id, wait['since']

    def _new(self, obs, t):
        self._count += 1
        person = Person(f'{self.run_id}:P{self._count}', None, None, -math.inf, None, None, t, t)
        self.people[person.person_id] = person
        while len(self.people) > MAX_PEOPLE:
            oldest = min(self.people.values(), key=lambda p: p.last_seen)
            self._drop(oldest.person_id)
        return person

    def _apply(self, person, obs, t, view):
        """Update a matched person; True when they stood up (suspected -> clear)."""
        if obs.track:
            for other in self.people.values():
                if other is not person and other.track == obs.track:
                    other.track = None
            person.track = obs.track
        if obs.point is not None:
            person.point, person.point_at = tuple(obs.point), t
            person.places.append((t, person.point))
        if obs.box is not None:
            person.box, person.view = tuple(obs.box), view
        person.last_seen = max(person.last_seen, t)
        stood_up = person.posture == 'suspected' and obs.state == 'clear'
        if obs.state != 'unknown':
            person.posture = obs.state
        return stood_up

    # ------------------------------------------------------------ uncertain

    def _recover(self, seen, t):
        """Depth back for a number made without it while uncertain (design decision 2):
        the one unseen member of its group last measured within 1 m is that person, as
        reappearing (decision 3), checked by ReID the same way. Two such members closer
        than AMBIGUOUS_M to each other: still uncertain. Returns {retired: person}."""
        merged = {}
        here = {p.person_id for _, p in seen}
        for i, (obs, person) in enumerate(seen):
            if not person.unplaced or obs.point is None:
                continue
            person.unplaced = False
            group = next((g for g in self.groups if person.person_id in g.members), None)
            if group is None:
                continue
            near = sorted(((math.dist(obs.point, self.people[m].point), self.people[m])
                           for m in group.members
                           if m not in here and m in self.people and self.people[m].point
                           and math.dist(obs.point, self.people[m].point) <= REAPPEAR_M),
                          key=lambda x: x[0])
            if not near or (len(near) > 1 and near[1][0] - near[0][0] < AMBIGUOUS_M):
                continue
            old = near[0][1]
            if self.reid and old.track and obs.track:
                self._ask_appearance(old, obs, t)
            for name in ('track', 'point', 'point_at', 'box', 'view', 'posture'):
                setattr(old, name, getattr(person, name))
            old.last_seen = max(old.last_seen, person.last_seen)
            old.places.extend(person.places)
            group.members -= {person.person_id, old.person_id}
            group.anchors.pop(old.person_id, None)
            if len(group.members) < 2:
                self.groups.remove(group)
            self._drop(person.person_id)
            merged[person.person_id] = old.person_id
            here.add(old.person_id)
            seen[i] = (obs, old)
        return merged

    def _stayed(self, person, t):
        """Where this person stayed for the last STAY_S, or None."""
        places = [point for at, point in person.places if at >= t - STAY_S]
        if (person.point is None or not person.places
                or person.places[0][0] > t - STAY_S or len(places) < 2):
            return None  # Not watched long enough to tell.
        return person.point if all(math.dist(p, person.point) <= STAY_M for p in places) else None

    def _group(self, people, t):
        members = {p.person_id for p in people}
        joined = [g for g in self.groups if g.members & members]
        anchors = {}
        for g in joined:
            members |= g.members
            anchors.update(g.anchors)
            self.groups.remove(g)
        for person in people:
            stayed = self._stayed(person, t)
            if stayed is not None and person.person_id not in anchors:
                anchors[person.person_id] = stayed
        self.groups.append(Group(members, t, anchors))

    def _merge(self, seen, t):
        """Two people this close in one frame may swap: uncertain from now on."""
        for i, (a, pa) in enumerate(seen):
            for b, pb in seen[i + 1:]:
                if a.point and b.point:
                    close = math.dist(a.point, b.point) <= MERGE_M
                else:
                    close = bool(a.box and b.box) and _iou(a.box, b.box) >= MERGE_IOU
                if close and not self._together(pa.person_id, pb.person_id):
                    self._group([pa, pb], t)

    def _together(self, a, b):
        return any(a in g.members and b in g.members for g in self.groups)

    def _release(self, seen):
        """Clear a group once they parted and the one who stayed put is plain to see."""
        here = {p.person_id: obs for obs, p in seen}
        for group in list(self.groups):
            visible = [here[m] for m in group.members if m in here]
            if any(a.point and b.point and math.dist(a.point, b.point) <= MERGE_M
                   for i, a in enumerate(visible) for b in visible[i + 1:]):
                continue  # Still together.
            for member, anchor in list(group.anchors.items()):
                near = [(obs, p) for obs, p in seen if p.person_id in group.members
                        and obs.point and math.dist(obs.point, anchor) <= RESOLVE_M]
                if len(near) != 1:
                    continue
                obs, holder = near[0]
                if holder.person_id != member:
                    self._swap(self.people[member], holder)
                group.members.discard(member)
                group.anchors.pop(member, None)
            if len(group.members) < 2 and group in self.groups:
                self.groups.remove(group)

    def _swap(self, a, b):
        """The tracking had them the other way round: exchange what each was last seen as."""
        for name in ('track', 'point', 'point_at', 'box', 'view', 'last_seen', 'posture',
                     'places'):
            value = getattr(a, name)
            setattr(a, name, getattr(b, name))
            setattr(b, name, value)

    # ------------------------------------------------------------ forgetting

    def _forget(self, t):
        for person in list(self.people.values()):
            if t - person.last_seen >= FORGET_S:
                self._drop(person.person_id)

    def _drop(self, person_id):
        self.people.pop(person_id, None)
        self._sent = {k: v for k, v in self._sent.items() if v != person_id}
        for group in list(self.groups):
            group.members.discard(person_id)
            group.anchors.pop(person_id, None)
            if len(group.members) < 2:
                self.groups.remove(group)
