"""Auxiliary clip-range and person-overlay recording for CloudFallMonitor."""

from typing import Any, Callable, Sequence

from malbut_agent_server.application.fall_clip_planner import RecordedClip
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind,
    FallCandidate,
    FallIncident,
)


def people_call(monitor: Any, method: Callable[..., Any], *args: Any) -> None:
    """Invoke a people-recorder hook without letting auxiliary errors block fall flow."""
    try:
        method(*args)
    except Exception:
        monitor.people_storage_failures += 1


def flush_people(monitor: Any) -> None:
    """Persist segments whose boxes are complete; never stops fall handling."""
    try:
        ready = monitor._people.due(monitor._now())
    except Exception:
        monitor.people_storage_failures += 1
        return
    for people in ready:
        if monitor._journal is not None and hasattr(monitor._journal, 'append_people'):
            try:
                monitor._journal.append_people(device_id=monitor.device_id, people=people)
            except Exception:
                monitor.people_storage_failures += 1
                continue
        monitor._people_ready.append(people)


def record_cloud_boxes(
    monitor: Any,
    incident: FallIncident,
    times: Sequence[float],
    finding: Any,
) -> None:
    """Record Cloud-localized bounding boxes against the incident's overlay track."""
    for region in finding.regions:
        if region.frame_index < len(times):
            people_call(
                monitor,
                monitor._people.cloud,
                incident.incident_id,
                times[region.frame_index],
                region.box,
            )


def record_clip(
    monitor: Any,
    incident: FallIncident,
    start: float,
    end: float,
    *,
    anchor_kind: str,
    found_down: bool = False,
) -> None:
    """Map an evidence window (monotonic) to wall-clock recording ranges."""
    try:
        changed = monitor._clips.observe(
            incident.incident_id,
            start,
            end,
            anchor_kind=anchor_kind,
            found_down=found_down,
        )
    except ValueError:
        monitor.clip_storage_failures += 1
        return
    if not changed:
        return
    offset = monitor._wall_clock() - monitor._now()
    first = monitor._clip_offsets.setdefault(incident.incident_id, offset)
    stepped = abs(offset - first) > 1.0
    for segment in changed:
        # Each segment keeps the offset it was first recorded with: frames
        # archived before a clock step stay at their original wall time.
        fixed = monitor._clip_offsets.setdefault(
            (incident.incident_id, segment.segment_index), offset,
        )
        people_call(
            monitor,
            monitor._people.segment,
            incident.incident_id,
            incident.subject_key,
            segment.segment_index,
            segment.start,
            segment.end,
        )
        clip = RecordedClip(
            incident.incident_id,
            monitor.boot_id,
            segment.segment_index,
            segment.revision,
            segment.start + fixed,
            segment.end + fixed,
            segment.anchor_kinds,
            segment.found_down,
            stepped,
        )
        if monitor._journal is not None and hasattr(monitor._journal, 'append_clip'):
            try:
                monitor._journal.append_clip(device_id=monitor.device_id, clip=clip)
            except Exception:
                monitor.clip_storage_failures += 1
                continue
        monitor._clip_ranges.append(clip)


def record_window_clip(
    monitor: Any,
    incident: FallIncident,
    request: Any,
    finding: Any,
) -> None:
    """Record the full analyzed Cloud window and its localized person boxes."""
    # The analyzed window, not the reply arrival time; the motion inside it
    # cannot be pinned down further, so the whole window is kept.
    frames = request.window.frames
    record_clip(
        monitor,
        incident,
        frames[0].captured_at,
        frames[-1].captured_at,
        anchor_kind='cloud_window',
        found_down=finding.kind is CandidateKind.ALREADY_DOWN,
    )
    record_cloud_boxes(monitor, incident, tuple(f.captured_at for f in frames), finding)


def record_candidate_clip(
    monitor: Any,
    incident: FallIncident,
    candidate: FallCandidate,
) -> None:
    """Record the Pose candidate's clip anchor window."""
    if candidate.kind is CandidateKind.ALREADY_DOWN:
        # The fall itself happened before discovery; anchor on the discovery.
        record_clip(
            monitor,
            incident,
            candidate.observed_at,
            candidate.observed_at,
            anchor_kind='pose_found_down',
        )
        return
    start = (
        candidate.evidence_started_at
        if candidate.evidence_started_at is not None
        else candidate.observed_at
    )
    record_clip(monitor, incident, start, candidate.observed_at, anchor_kind='pose_motion')
