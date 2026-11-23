"""Rule-decision dispatch, journal persistence, and clip/overlay tracking."""

import math
from typing import Any, Callable, Dict, Optional, Sequence
from uuid import uuid4

from malbut_agent_server.application.fall_clip_planner import RecordedClip
from malbut_agent_server.application.fall_sensor.constants import (
    ANSWERS,
    CHECK_CLIP_AFTER_S,
    CHECK_CLIP_EVERY_S,
    CHECK_CLIP_MAX_S,
    CLIP_MARKS,
    LOOKS,
    RECENT_S,
    RECORD_KINDS,
    RECORD_PURPOSE,
    STATES,
    FlowIncident,
    Look,
    _box,
    _id,
    _req_id,
)
from malbut_agent_server.ports.fall_event_journal import FallJournalError


def get_incident(sensor: Any, incident_id: str) -> Optional[FlowIncident]:
    """Load an incident from memory or rehydrate it from the journal."""
    incident = sensor._incidents.get(incident_id)
    if incident is None and sensor._journal is not None:
        stored = sensor._journal.flow_incident(incident_id)
        if stored is not None:  # Opened before this process started.
            incident = FlowIncident(
                incident_id,
                stored['boot_id'],
                stored['payload'].get('first') or {},
                sensor._now(),
                stored['open'],
                stored['check_round'],
                stored.get('left'),
            )
            sensor._incidents[incident_id] = incident
    return incident


def emit_ready(sensor: Any) -> None:
    """For a restarted Agent: the open incidents and the latest suspicions."""
    open_incidents = []
    now = sensor._now()
    stored = (
        sensor._journal.open_flow_incidents()
        if sensor._journal is not None
        else [
            dict(
                incident_id=i.incident_id,
                boot_id=i.boot_id,
                payload=dict(first=i.first),
                check_round=i.check_round,
                left=i.left,
                seen_ago=None if i.seen_at is None else max(0.0, now - i.seen_at),
            )
            for i in sensor._incidents.values() if i.open
        ]
    )
    for row in stored:
        first = row['payload'].get('first')
        if not isinstance(first, dict):
            continue
        open_incidents.append(dict(
            incident_id=row['incident_id'],
            first=first,
            check_round=row['check_round'],
            fall_moments=row['payload'].get('fall_moments', []),
            left=row.get('left'),
            seen_ago=row.get('seen_ago'),
        ))
    recent = [
        message for message, *_ in sensor._sightings.values()
        if now - message['at'] <= RECENT_S
        and message['kind'] in ('low', 'lying', 'motion', 'cloud')
    ]
    sensor._messages.append(dict(
        kind='flow_ready',
        flow=True,
        open_incidents=open_incidents[-64:],
        recent=recent[-32:],
    ))


def parse_round(data: Dict[str, Any]) -> int:
    """Validate and return the 1-based check round counter."""
    value = data.get('check_round', 1)
    if type(value) is not int or not 1 <= value <= 10000:
        raise ValueError('invalid check round')
    return value


def store_call(sensor: Any, method: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Execute one durable journal call, halting the sensor on storage failure."""
    if sensor._storage_failed:
        raise FallJournalError('recover journal and restart before writing')
    try:
        method(*args, **kwargs)
    except Exception:
        sensor._storage_failed = True
        sensor.configure(
            enabled=False, camera_enabled=False,
            cloud_consent=False, connected=False,
        )
        raise FallJournalError('fall event persistence failed') from None


def append_event(
    sensor: Any,
    incident: FlowIncident,
    event: str,
    data: Dict[str, Any],
    *,
    reason: Optional[str],
    analysis: Any = None,
    level: Optional[str] = None,
) -> None:
    """Validate and append one flow event row to the journal."""
    state, answer = data.get('state'), data.get('answer')
    if state not in STATES or (answer is not None and answer not in ANSWERS):
        raise ValueError('invalid record state')
    if reason is not None and (not isinstance(reason, str) or len(reason) > 80):
        raise ValueError('invalid reason')
    fall_seen = data.get('fall_seen') is True
    incident.check_round = parse_round(data)
    if sensor._journal is None:
        return
    store_call(
        sensor,
        sensor._journal.append_flow_event,
        device_id=sensor.device_id,
        boot_id=incident.boot_id,
        event_id=str(uuid4()),
        incident_id=incident.incident_id,
        kind=event,
        state=state,
        fall_seen=fall_seen,
        assessment='observed_fall' if fall_seen else 'suspected_fall',
        answer=answer,
        reason=reason,
        notification_level=level,
        check_round=incident.check_round,
        analysis=analysis,
    )


def mark_incident(sensor: Any, incident: FlowIncident, round_: int) -> None:
    """Persist open/round/left-open metadata for one flow incident."""
    if sensor._journal is not None:
        store_call(
            sensor,
            sensor._journal.mark_flow_incident,
            incident.incident_id,
            open=incident.open,
            check_round=round_,
            left=incident.left,
        )


def record_clip(
    sensor: Any,
    incident: FlowIncident,
    start: float,
    end: float,
    *,
    anchor_kind: str,
    found_down: bool = False,
    check_round: Optional[int] = None,
    pre_s: Optional[float] = None,
    post_s: Optional[float] = None,
) -> None:
    """Record clip segments and overlay tracks for an incident window."""
    try:
        changed = sensor._clips.observe(
            incident.incident_id,
            start,
            end,
            anchor_kind=anchor_kind,
            found_down=found_down,
            pre_s=pre_s,
            post_s=post_s,
            check_round=check_round,
        )
    except ValueError:
        sensor.clip_storage_failures += 1
        return
    if not changed:
        return
    offset = sensor._wall_clock() - sensor._now()
    first = sensor._clip_offsets.setdefault(incident.incident_id, offset)
    stepped = abs(offset - first) > 1.0
    for segment in changed:
        fixed = sensor._clip_offsets.setdefault(
            (incident.incident_id, segment.segment_index), offset,
        )
        min_revision = 0
        if sensor._journal is not None and hasattr(sensor._journal, 'people_revision'):
            try:
                min_revision = sensor._journal.people_revision(
                    incident.incident_id, segment.segment_index,
                )
            except Exception:
                min_revision = 0
        sensor._people_call(
            sensor._people.segment,
            incident.incident_id,
            incident.person or incident.first.get('track'),
            segment.segment_index,
            segment.start,
            segment.end,
            boot_id=incident.boot_id,
            min_revision=min_revision,
        )
        clip = RecordedClip(
            incident.incident_id,
            incident.boot_id,
            segment.segment_index,
            segment.revision,
            segment.start + fixed,
            segment.end + fixed,
            segment.anchor_kinds,
            segment.found_down,
            stepped,
            segment.check_round,
        )
        if sensor._journal is not None and hasattr(sensor._journal, 'append_clip'):
            try:
                sensor._journal.append_clip(device_id=sensor.device_id, clip=clip)
            except Exception:
                sensor.clip_storage_failures += 1


def clip_from(sensor: Any, incident: FlowIncident, sighting: Sequence[Any]) -> None:
    """Attach a sighting's evidence interval to the incident's clip planner."""
    message, start, end = sighting[0], sighting[1], sighting[2]
    anchor = {
        'motion': 'pose_motion',
        'lying': 'pose_found_down',
        'low': 'pose_found_down',
        'cloud': 'cloud_window',
    }.get(message['kind'])
    if anchor is None:
        return
    record_clip(
        sensor, incident, start, end, anchor_kind=anchor,
        found_down=message['kind'] != 'motion',
    )


def extend_check(sensor: Any, incident: FlowIncident, now: float, *, after: float) -> None:
    """Extend the active check-round clip segment up to now."""
    assert incident.check_clip is not None
    round_, since, _ = incident.check_clip
    incident.check_clip[2] = now
    record_clip(
        sensor,
        incident,
        since,
        min(now, since + CHECK_CLIP_MAX_S),
        anchor_kind='check' if round_ == 1 else 'recheck',
        check_round=round_,
        pre_s=0.0,
        post_s=after,
    )


def check_clip(sensor: Any, incident: FlowIncident, mark: str, round_: int) -> None:
    """A mark of the robot's check: a range starts now, or ends."""
    now = sensor._now()
    if incident.check_clip is not None:
        extend_check(
            sensor,
            incident,
            now,
            after=0.0 if mark == 'pause' else CHECK_CLIP_AFTER_S,
        )
        incident.check_clip = None
    if mark == 'start':
        # Started soon after the first suspicion, it joins that range (-10 s): one
        # 확인. After a wait it is its own range, and the first one stays 발견.
        incident.check_clip = [round_, now, now]
        extend_check(sensor, incident, now, after=CHECK_CLIP_EVERY_S)


def write_record(sensor: Any, data: Dict[str, Any]) -> None:
    """Record one fall-rules event (opening/resolving/rechecking as needed)."""
    event, incident_id = data.get('event'), data.get('incident_id')
    if event not in RECORD_KINDS:
        raise ValueError('unsupported record')
    if incident_id is None:
        if sensor._journal is not None:  # Local only (a filtered candidate).
            store_call(
                sensor,
                sensor._journal.append_flow_note,
                dict(
                    event=event,
                    reason=data.get('reason'),
                    sighting_id=data.get('sighting_id'),
                ),
            )
        return
    req_incident_id = _req_id(incident_id)
    if event == 'incident_opened' and req_incident_id not in sensor._incidents:
        sighting = sensor._sightings.get(data.get('sighting_id'))
        first = sighting[0] if sighting is not None else {}
        now = sensor._now()
        incident = FlowIncident(req_incident_id, sensor.boot_id, first, now, seen_at=now)
        sensor._incidents[req_incident_id] = incident
        if sensor._journal is not None:
            store_call(
                sensor,
                sensor._journal.save_flow_incident,
                incident_id=req_incident_id,
                boot_id=sensor.boot_id,
                payload=dict(
                    first=first,
                    fall_moments=[first.get('fall_moment')] if first.get('fall_moment') else [],
                ),
            )
        if sighting is not None:
            clip_from(sensor, incident, sighting)
    existing_incident = get_incident(sensor, req_incident_id)
    if existing_incident is None:
        raise ValueError('unknown incident')
    incident = existing_incident
    analysis = None
    look_id = data.get('look_id')
    if look_id is not None and RECORD_PURPOSE.get(event):
        analysis = sensor._analyses.get(look_id)
        if analysis is not None and analysis.purpose not in RECORD_PURPOSE[event]:
            analysis = None
    reason = data.get('reason')
    if event == 'approach_started' and reason is None:
        running = sensor._running_missions
        reason = (
            'patrol_stopped' if 'patrol' in running
            else 'follow_stopped' if 'follow_person' in running else None
        )
    round_ = parse_round(data)
    append_event(sensor, incident, event, data, reason=reason, analysis=analysis)
    if event == 'incident_resolved':
        incident.open = False
        incident.middle.clear()
        mark_incident(sensor, incident, round_)
        if incident.check_clip is not None:  # The judgment, even without an end mark.
            check_clip(sensor, incident, 'end', round_)
    elif event == 'recheck_started':
        incident.open = True
        incident.left = None
        incident.middle.clear()
        incident.middle_at = -math.inf
        mark_incident(sensor, incident, round_)


def write_notice(sensor: Any, data: Dict[str, Any]) -> None:
    """Record a guardian notification requested by the fall rules."""
    incident = get_incident(sensor, _req_id(data.get('incident_id')))
    if incident is None:
        raise ValueError('unknown incident')
    level = data.get('level')
    if level not in ('info', 'check', 'urgent'):
        raise ValueError('invalid level')
    append_event(
        sensor, incident, 'notification_requested', data,
        reason=data.get('reason'), level=level,
    )


def apply_people_numbers(sensor: Any, data: Dict[str, Any]) -> None:
    """Who each YOLO track is from now on, and whose each incident is (the Agent decides)."""
    tracks, incidents = data.get('tracks') or {}, data.get('incidents') or {}
    if not isinstance(tracks, dict) or not isinstance(incidents, dict):
        raise ValueError('invalid people numbers')
    at = data.get('at')
    if tracks and (isinstance(at, bool) or not isinstance(at, (int, float))
                   or not math.isfinite(at)):
        raise ValueError('invalid people numbers')
    if tracks:
        assert isinstance(at, (int, float))
        at_float = float(at)
        for track, person in tracks.items():
            sensor._people_call(
                sensor._people.assign,
                _req_id(track),
                _req_id(person),
                at_float,
            )
    for incident_id, person in incidents.items():
        incident = get_incident(sensor, _req_id(incident_id))
        if incident is not None and person is not None:
            incident.person = _req_id(person)
            sensor._people_call(sensor._people.retarget, incident.incident_id, incident.person)


def apply_decision(sensor: Any, data: Any) -> None:
    """One decision of the Agent's fall rules (/malbut/falls/runtime/decision)."""
    if not isinstance(data, dict):
        raise ValueError('invalid decision')
    if sensor._flow_log is not None:
        sensor._flow_log.write('from_rules', data)
    action = data.get('action')
    if action == 'flow_hello':
        emit_ready(sensor)
    elif action == 'look':
        what = data.get('what')
        if what not in LOOKS:
            raise ValueError('invalid look')
        look = Look(
            _req_id(data.get('look_id')),
            what,
            _id(data.get('sighting_id')),
            _id(data.get('incident_id')),
            _id(data.get('photo') or data.get('before')),
            _box(data.get('box')),
            _id(data.get('track')),
            tuple(_req_id(m) for m in (data.get('fall_moments') or ()))[:2],
        )
        if (look.sighting_id is None) == (look.incident_id is None):
            raise ValueError('a look is about a sighting or an incident')
        if len(sensor._looks) >= 32:
            raise ValueError('too many looks')
        if not sensor._cloud_allowed():
            sensor._answer(look, 'guard', reason=sensor._guard_reason())
        else:
            sensor._looks.append(look)
    elif action == 'record':
        write_record(sensor, data)
    elif action == 'notify':
        write_notice(sensor, data)
    elif action == 'link':
        sighting = sensor._sightings.get(_req_id(data.get('sighting_id')))
        incident_id = _req_id(data.get('incident_id'))
        incident = get_incident(sensor, incident_id)
        if data.get('scene') not in (None, True, False):
            raise ValueError('invalid link')
        # Not a scene of its own (too soon after the last one kept): only seen.
        if sighting is not None and incident is not None and data.get('scene') is not False:
            clip_from(sensor, incident, sighting)
        # Seen now: for a restarted Agent, whoever is at its place for 30 more minutes.
        if sensor._journal is not None:
            store_call(sensor, sensor._journal.seen_flow_incident, incident_id)
        elif incident is not None:
            incident.seen_at = sensor._now()
    elif action == 'people':
        apply_people_numbers(sensor, data)
    elif action == 'left_open':
        incident = get_incident(sensor, _req_id(data.get('incident_id')))
        if incident is None or data.get('who') not in ('nobody', 'person'):
            raise ValueError('invalid left open')
        incident.left = data['who']
        mark_incident(sensor, incident, incident.check_round)
    elif action == 'compare':
        request_id = _req_id(data.get('request_id'))
        mode = data.get('mode', 'reappear')
        if mode not in ('reappear', 'check'):
            raise ValueError('invalid appearance comparison')
        sides = []
        for side in (data.get('a'), data.get('b')):
            at = side.get('at') if isinstance(side, dict) else None
            if (isinstance(at, bool) or not isinstance(at, (int, float))
                    or not math.isfinite(at)):
                raise ValueError('invalid appearance comparison')
            assert isinstance(side, dict)
            sides.append((_req_id(side.get('track')), float(at)))
        now = sensor._now()
        sensor._comparisons[request_id] = (sides[0], sides[1], mode, now)
        while len(sensor._comparisons) > 32:
            sensor._comparisons.popitem(last=False)
        if mode == 'reappear' or (not sensor._appearance_ready(sides[1], mode)
                                  and sensor._appearance.in_view(sides[1][0], now)):
            sensor._appearance.want(*sides[1])  # Its pair now, not on the five-second round.
        sensor._answer_comparisons()
    elif action == 'clip':
        incident = get_incident(sensor, _req_id(data.get('incident_id')))
        if incident is None or data.get('mark') not in CLIP_MARKS:
            raise ValueError('invalid clip mark')
        check_clip(sensor, incident, data['mark'], parse_round(data))
    else:
        raise ValueError('unsupported decision')
