"""Cloud crosscheck association, pending review waits, and discovery tracking."""

from dataclasses import replace
from typing import Any, Callable, Dict, Optional, Sequence
from uuid import uuid4

from malbut_agent_server.application.fall_cloud_association import (
    associate_timed_finding,
    supplement_samples,
    timed_association_evidence,
)
from malbut_agent_server.application.fall_deferred_association import (
    DeferredDiscovery,
    DiscoveryLinkResult,
    DiscoveryTrack,
)
from malbut_agent_server.application.fall_pending_association import PendingAssociation
from malbut_agent_server.application.fall_scene_place import CASE_GAP_S
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind,
    CloudAnalysisExplanation,
    CloudAssociationReview,
    CloudDiscovery,
    CloudDiscoveryLink,
    CloudFallReply,
    CloudPersonFinding,
    CloudPoseLink,
    FallIncident,
    FallRuntimeEvent,
    IncidentState,
    NotificationLevel,
    VideoAssessment,
    VoiceAnswer,
    timestamp,
)
from malbut_agent_server.ports.fall_event_journal import FallJournalError


def scene_incident_versions(monitor: Any) -> Dict[str, Any]:
    """Snapshot open/closed incident versions to detect concurrent mutations."""
    return {
        i.incident_id: (i.subject_key, i.revision, i.state, i.last_observed_at, i.pending)
        for i in monitor._incidents.values()
    }


def new_candidate_covered(
    current: Optional[FallIncident],
    before: Dict[str, Any],
    after: Dict[str, Any],
    times: Sequence[float],
) -> bool:
    """True when an unanswered initial candidate is covered by this Cloud video."""
    # Only a first, unanswered/unanalysed candidate already present in
    # this video may consume its Cloud result. Newer falls need new video.
    return (
        not before and len(after) == 1 and current is not None
        and current.state is IncidentState.VERIFYING
        and current.revision == 1 and current.answer is None
        and current.attempts == 0 and current.pending
        and times[0] - 0.1 <= current.last_observed_at <= times[-1]
    )


def pose_link_proof(
    monitor: Any,
    source_id: Any,
    revision: int,
    token: str,
    times: Sequence[float],
    finding: Any,
    measured: Any,
) -> CloudPoseLink:
    """Build a CloudPoseLink proof from timed Pose samples."""
    return CloudPoseLink(
        source_id,
        revision,
        token,
        monitor._now(),
        tuple(times[r.frame_index] for r in finding.regions),
        tuple(tuple(t for t, _ in measured[r.frame_index]) for r in finding.regions),
    )


def prune_discoveries(monitor: Any) -> None:
    """Drop expired deferred discovery entries older than 60 seconds."""
    for did, entry in tuple(monitor._discoveries.items()):
        if monitor._now() - entry.received_at > 60:
            del monitor._discoveries[did]


def discovery_link_block(monitor: Any, entry: DeferredDiscovery) -> Optional[str]:
    """Return a block reason preventing a deferred discovery from linking, or None."""
    if (not monitor._enabled or not monitor._camera or monitor._storage_failed
            or entry.epoch != monitor._epoch
            or monitor._runtime_cloud_block in {
                'waiting_settings', 'disabled', 'camera_off', 'mapping',
                'control_unavailable', 'runtime_error'}):
        return 'tracking_session_expired'
    source = monitor._incidents.get(entry.discovery.incident_id)
    if (source is None or source.state is IncidentState.RESOLVED
            or source.revision != entry.source_revision):
        return 'source_incident_changed'
    return None


def make_room(monitor: Any) -> bool:
    """Free a memory slot from finished history; the journal keeps every case.

    Only resolved cases or scene cases quiet for CASE_GAP_S with no open
    question, both last observed over CASE_GAP_S ago, are released.
    """
    if len(monitor._incidents) < monitor.policy.max_incidents:
        return True
    now = monitor._now()
    settled = {iid for _, iid in monitor._settled_subjects.values()}
    for iid, incident in monitor._incidents.items():
        last = max(incident.last_observed_at, incident.scene_seen_until or 0.0)
        finished = (
            incident.state is IncidentState.RESOLVED
            or (incident.subject_key is None
                and (incident.question_id is None or incident.answer is not None))
        )
        if (finished and now - last > CASE_GAP_S and not incident.pending
                and iid != monitor._active_incident and iid not in settled
                and not incident.merged_into_incident_ids):
            del monitor._incidents[iid]
            monitor._questions.pop(iid, None)
            monitor._case_places.pop(iid, None)
            monitor._association_wait.anchors.pop(iid, None)
            for key in [k for k in monitor._clip_offsets
                        if k == iid or (isinstance(k, tuple) and k[0] == iid)]:
                del monitor._clip_offsets[key]
            return True
    return False


def record_crosscheck(
    monitor: Any,
    request: Any,
    reply: Any,
    snapshot: Any,
    versions: Dict[str, Any],
    *,
    pose_snapshot: Any = None,
    pose_generation: Optional[int] = None,
    associate_finding: Callable[..., Any] = associate_timed_finding,
) -> None:
    """Associate periodic Cloud crosscheck findings to Pose subjects or scenes."""
    if reply.assessment not in (
        VideoAssessment.OBSERVED_FALL, VideoAssessment.SUSPECTED_FALL,
    ):
        return  # A scene-level normal result never clears a person's case.
    times = tuple(f.captured_at for f in request.window.frames)
    analysis = CloudAnalysisExplanation.from_reply(request, reply)
    if pose_snapshot is None:
        # Compatibility for internal callers with an exact-only snapshot.
        pose_snapshot = tuple(
            ((t, observations),) if observations else ()
            for t, observations in zip(times, snapshot)
        )
    if pose_generation is None:
        pose_generation = monitor._subject_evidence.generation
    measured = pose_snapshot
    if pose_generation == monitor._subject_evidence.generation:
        measured = supplement_samples(
            pose_snapshot, monitor._subject_evidence.association_samples(times),
        )
    findings = reply.findings or (CloudPersonFinding(
        reply.assessment,
        CandidateKind.MOTION_SEEN
        if reply.assessment is VideoAssessment.OBSERVED_FALL
        else CandidateKind.UNKNOWN,
    ),)
    # Strongest evidence first if a provider accidentally repeats a person.
    order = sorted(
        enumerate(findings),
        key=lambda pair: pair[1].assessment is not VideoAssessment.OBSERVED_FALL,
    )
    linked: Dict[str, str] = {}
    scene_closed_during_scan = any(
        v[0] is None and v[2] is not IncidentState.RESOLVED
        and monitor._incidents[iid].state is IncidentState.RESOLVED
        for iid, v in versions.items()
    )
    for index, finding in order:
        match = associate_finding(finding, measured)
        reason = 'invalid_locations' if reply.localization_failed else match.reason
        iid: Optional[str] = None
        if reason == 'matched':
            latest = monitor._subject_evidence.latest(match.subject_key)
            if (latest is None or latest[1] != match.token
                    or monitor._now() - latest[0] > monitor.policy.max_person_observation_age_s):
                reason = 'current_target_unavailable'
            elif match.subject_key in linked:
                iid = linked[match.subject_key]
            else:
                before = {k: v for k, v in versions.items() if v[0] == match.subject_key}
                after = {
                    k: v for k, v in scene_incident_versions(monitor).items()
                    if v[0] == match.subject_key
                }
                current = next((
                    i for i in monitor._incidents.values()
                    if i.subject_key == match.subject_key
                    and i.state is not IncidentState.RESOLVED
                ), None)
                consume_initial = new_candidate_covered(current, before, after, times)
                if before != after and not consume_initial:
                    reason = 'incident_changed_during_scan'
                elif current and current.subject_association_token != match.token:
                    reason = 'incident_target_continuity_unverified'
                elif (current is None and finding.assessment
                      is not VideoAssessment.OBSERVED_FALL
                      and monitor._settled_subject(
                          match.subject_key, match.token, finding) is None
                      and monitor._cleared_place(monitor._map_points(times, finding))):
                    # The direct path below records why; no new person case.
                    reason = 'cleared_place'
                else:
                    iid = merge_cloud_person(
                        monitor, request, finding, match.subject_key, match.token,
                        consume_initial=consume_initial, analysis=analysis,
                    )
                    if iid is None:
                        reason = 'incident_capacity'
                    else:
                        linked[match.subject_key] = iid
                        if monitor._incidents[iid].state is IncidentState.RESOLVED:
                            reason = 'settled_episode_continues'
        subject_key = match.subject_key if iid else None
        if iid is None and scene_closed_during_scan:
            reason = 'incident_changed_during_scan'
        wait_for = None
        if (iid is None and len(findings) == 1 and reason in {
                'no_matching_track', 'current_target_unavailable', 'track_unusable',
                'incident_target_continuity_unverified'}):
            wait_for = monitor._association_wait.candidate(
                finding, times, monitor._incidents, versions,
                now=monitor._now(), policy=monitor.policy,
            )
        if iid is None and wait_for is None and reason != 'incident_changed_during_scan':
            if monitor._on_furniture(times, finding):
                reason = 'on_furniture'
            else:
                held = monitor._held_by_place(times, finding)
                if held is not None:
                    reason = held
                else:
                    iid = record_unidentified_scene(
                        monitor, request, finding, analysis=analysis,
                    )
                    if iid is None:
                        reason = 'incident_capacity'
        proof = None
        if subject_key and iid is not None and (
            measured != pose_snapshot
            or any(len(group) == 2 for group in measured)
        ):
            proof = pose_link_proof(
                monitor, iid, monitor._incidents[iid].revision, match.token,
                times, finding, measured,
            )
        discovery = CloudDiscovery(
            str(uuid4()), request.request_id, index, finding,
            tuple(f.captured_at for f in request.window.frames), reason,
            subject_key, iid, timed_association_evidence(finding, measured), proof,
        )
        deadline = 0.0
        if wait_for is not None:
            deadline = monitor._association_wait.deadline(
                wait_for, monitor._now(), monitor.policy,
            )
            discovery = replace(discovery, association_review=CloudAssociationReview(
                wait_for.incident_id, wait_for.revision, deadline,
            ))
        if iid and subject_key is None:
            scene = monitor._incidents[iid]
            if len(scene.unresolved_discovery_ids) < 256:
                scene.unresolved_discovery_ids += (discovery.discovery_id,)
            else:
                scene.discovery_overflow = True
        # Every finding remains distinct, including discoveries sharing a
        # scene-level verification. An incident ID does not prove identity.
        monitor._emit('cloud_discovery', reason=reason, discovery=discovery)
        if wait_for is not None:
            monitor._association_wait.pending[discovery.discovery_id] = PendingAssociation(
                discovery, monitor._epoch, monitor._now(), wait_for.incident_id,
                wait_for.revision, deadline,
            )
        prune_discoveries(monitor)
        monitor._discoveries[discovery.discovery_id] = DeferredDiscovery(
            discovery, monitor._epoch, monitor._now(),
            monitor._incidents[iid].revision if iid else 0,
            pose_snapshot=measured, pose_generation=pose_generation,
            incident_versions=versions, analysis=analysis,
        )
        while len(monitor._discoveries) > 256:
            monitor._discoveries.popitem(last=False)


def cancel_pending_associations(monitor: Any) -> None:
    """Cancel all pending association reviews when runtime controls change."""
    waiting = tuple(monitor._association_wait.pending.values())
    monitor._association_wait.clear()
    if monitor._storage_failed:
        return  # _emit's storage-failure path must not recursively write.
    for pending in waiting:
        finish_pending_association(
            monitor, pending, 'cancelled', 'association_wait_control_changed',
        )


def finish_pending_association(
    monitor: Any,
    pending: PendingAssociation,
    status: str,
    reason: str,
    *,
    incident_id: Optional[str] = None,
    subject_key: Optional[str] = None,
    proof: Any = None,
) -> CloudDiscovery:
    """Emit the final status for one pending association review."""
    review = pending.discovery.association_review
    assert review is not None
    d = replace(
        pending.discovery,
        reason=reason,
        incident_id=incident_id,
        subject_key=subject_key,
        association_link=proof,
        association_review=replace(review, status=status),
    )
    monitor._emit(
        'cloud_discovery_linked' if subject_key else 'cloud_discovery',
        discovery=d, reason=reason,
    )
    monitor._association_wait.pending.pop(d.discovery_id, None)
    entry = monitor._discoveries.get(d.discovery_id)
    if entry is not None:
        entry.discovery = d
        entry.source_revision = (
            monitor._incidents[incident_id].revision if incident_id else 0
        )
    return d


def retry_pending_associations(
    monitor: Any,
    *,
    associate_finding: Callable[..., Any] = associate_timed_finding,
) -> None:
    """Only measured, same-token late Pose can turn a wait into a link.

    Old-position overlap is deliberately insufficient. The wait has no
    source scene/question/answer to copy, and never rewrites the candidate
    incident's help state or completed confirmation.
    """
    if not all((monitor._enabled, monitor._camera, monitor._consent, monitor._connected)):
        return
    if monitor._runtime_cloud_block is not None:
        return
    for pending in tuple(monitor._association_wait.pending.values()):
        d = pending.discovery
        entry = monitor._discoveries.get(d.discovery_id)
        target = monitor._incidents.get(pending.candidate_id)
        if (entry is None or pending.epoch != monitor._epoch
                or monitor._now() >= pending.deadline
                or monitor._now() - pending.received_at > 2
                or entry.pose_generation != monitor._subject_evidence.generation
                or target is None or target.state is IncidentState.RESOLVED
                or target.revision != pending.candidate_revision
                or monitor._active_incident == target.incident_id):
            continue
        measured = supplement_samples(
            entry.pose_snapshot,
            monitor._subject_evidence.association_samples(d.sample_times),
        )
        match = associate_finding(d.finding, measured)
        latest = monitor._subject_evidence.latest(match.subject_key)
        if (match.reason != 'matched' or match.subject_key != target.subject_key
                or match.token != target.subject_association_token or latest is None
                or latest[1] != match.token
                or monitor._now() - latest[0] > monitor.policy.max_person_observation_age_s):
            continue
        proof = pose_link_proof(
            monitor, target.incident_id, target.revision, match.token,
            d.sample_times, d.finding, measured,
        )
        finish_pending_association(
            monitor, pending, 'matched', 'matched_after_association_wait',
            incident_id=target.incident_id, subject_key=target.subject_key, proof=proof,
        )
        monitor._record_clip(
            target, d.sample_times[0], d.sample_times[-1],
            anchor_kind='cloud_window', found_down=True,
        )
        monitor._record_cloud_boxes(target, d.sample_times, d.finding)


def release_pending_associations(monitor: Any) -> None:
    """A wait is finite; unresolved identity falls back to scene verification.

    No extra Cloud call, no stored JPEG and no transfer of the old answer.
    Reuse the original capture times for clips/overlays, not the deadline.
    """
    for pending in tuple(monitor._association_wait.pending.values()):
        target = monitor._incidents.get(pending.candidate_id)
        changed = (
            target is None or target.state is IncidentState.RESOLVED
            or target.revision != pending.candidate_revision
        )
        scene_exists = any(
            i.subject_key is None and i.state is not IncidentState.RESOLVED
            for i in monitor._incidents.values()
        )
        if not (changed or scene_exists or monitor._now() >= pending.deadline):
            continue
        d = pending.discovery
        if monitor._on_furniture(d.sample_times, d.finding):
            finish_pending_association(monitor, pending, 'cancelled', 'on_furniture')
            continue
        held = monitor._held_by_place(d.sample_times, d.finding)
        if held is not None:
            finish_pending_association(monitor, pending, 'cancelled', held)
            continue
        entry = monitor._discoveries.get(d.discovery_id)
        iid = record_unidentified_observation(
            monitor, d.sample_times, d.finding,
            analysis=entry.analysis if entry else None,
        )
        if iid is not None:
            scene = monitor._incidents[iid]
            if len(scene.unresolved_discovery_ids) < 256:
                scene.unresolved_discovery_ids += (d.discovery_id,)
            else:
                scene.discovery_overflow = True
        reason = (
            'incident_capacity' if iid is None else
            'association_wait_candidate_changed' if changed else
            'association_wait_scene_verification' if scene_exists else
            'association_wait_expired'
        )
        finish_pending_association(
            monitor, pending, 'verification_required', reason, incident_id=iid,
        )


def retry_pose_associations(
    monitor: Any,
    *,
    associate_finding: Callable[..., Any] = associate_timed_finding,
) -> None:
    """Recheck recent unmatched findings when measured Pose arrives.

    This path handles discoveries that already started scene verification;
    the bounded no-scene wait uses retry_pending_associations instead.
    Never call Cloud again or transfer a scene's answer to a person. Source
    scene lifecycle remains explicit because it can contain other people.
    """
    prune_discoveries(monitor)
    for entry in tuple(monitor._discoveries.values()):
        d = entry.discovery
        if (d.subject_key is not None or d.association_link is not None
                or d.reason not in {'no_matching_track', 'current_target_unavailable'}
                or not entry.pose_snapshot or entry.incident_versions is None
                or entry.pose_generation != monitor._subject_evidence.generation
                or monitor._now() - entry.received_at > 2
                or discovery_link_block(monitor, entry)):
            continue
        measured = supplement_samples(
            entry.pose_snapshot,
            monitor._subject_evidence.association_samples(d.sample_times),
        )
        match = associate_finding(d.finding, measured)
        if match.reason != 'matched':
            continue
        key, token = match.subject_key, match.token
        latest = monitor._subject_evidence.latest(key)
        if (latest is None or latest[1] != token
                or monitor._now() - latest[0] > monitor.policy.max_person_observation_age_s):
            continue
        current = next((
            i for i in monitor._incidents.values()
            if i.subject_key == key and i.state is not IncidentState.RESOLVED
        ), None)
        # Do not create another question/person case solely because a late
        # Pose appeared. A later Pose candidate can supply the target case.
        if (current is None or current.subject_association_token != token
                or monitor._active_incident == current.incident_id):
            continue
        before = {k: v for k, v in entry.incident_versions.items() if v[0] == key}
        after = {k: v for k, v in scene_incident_versions(monitor).items() if v[0] == key}
        if before != after:
            # A new candidate from the requested video is allowed. Revised,
            # answered or closed pre-existing cases must not be overwritten.
            if not new_candidate_covered(current, before, after, d.sample_times):
                continue
        if any(other.discovery.discovery_id != d.discovery_id
               and other.discovery.request_id == d.request_id
               and other.discovery.subject_key == key
               for other in monitor._discoveries.values()):
            continue
        proof = pose_link_proof(
            monitor, d.incident_id, entry.source_revision, token,
            d.sample_times, d.finding, measured,
        )
        attach_discovery(
            monitor, entry, key, token, latest[0], current,
            proof=proof, reason='matched_after_pose_arrival',
        )


def begin_discovery_tracking(monitor: Any, discovery_id: str) -> str:
    """Opt-in trusted local tracker boundary, NOT a Cloud/ROS command.

    Seed the tracker with the earliest region in this stored discovery and
    its exact RGB frame. Feed measured output with ingest_discovery_track.
    A broken track may not be restarted under this discovery's identity.
    """
    prune_discoveries(monitor)
    entry = monitor._discoveries.get(discovery_id)
    if entry is None:
        raise ValueError('unknown or expired discovery')
    discovery = entry.discovery
    if discovery.subject_key is not None:
        raise ValueError('discovery already associated')
    block = discovery_link_block(monitor, entry)
    if block:
        raise ValueError(block)
    if (not discovery.finding.regions or discovery.reason in {
            'invalid_locations', 'invalid_sample_index', 'incident_changed_during_scan'}
            or any(r.frame_index >= len(discovery.sample_times)
                   for r in discovery.finding.regions)):
        raise ValueError('discovery has no usable seed')
    if entry.track is None:
        entry.track = DiscoveryTrack(discovery)
        entry.session_id = str(uuid4())
    if entry.track.broken:
        raise ValueError('visual_track_broken')
    assert entry.session_id is not None
    return entry.session_id


def tracking_session_reason(monitor: Any, session_id: str) -> Optional[str]:
    """Read-only lifecycle gate for the local asynchronous tracker."""
    prune_discoveries(monitor)
    entry = next((
        e for e in monitor._discoveries.values()
        if e.session_id is not None and e.session_id == session_id
    ), None)
    if entry is None:
        return 'unknown_tracking_session'
    if entry.discovery.association_link is not None:
        return 'already_linked'
    return discovery_link_block(monitor, entry) or (
        'visual_track_broken' if entry.track.broken else None
    )


def end_discovery_tracking(monitor: Any, session_id: str) -> None:
    """A lost/stopped session cannot reacquire another person under its seed."""
    for entry in monitor._discoveries.values():
        if entry.session_id is not None and entry.session_id == session_id:
            if entry.discovery.association_link is None:
                entry.track.broken = True
            return


def ingest_discovery_track(
    monitor: Any,
    session_id: str,
    *,
    observed_at: float,
    box: Any,
) -> DiscoveryLinkResult:
    """Associate a tracked discovery using exact, current measured Pose.

    Caller cannot choose the target ID. Lost masks, temporal gaps and
    ambiguous Pose do not establish a link. This never runs an inference.
    """
    timestamp(observed_at)
    prune_discoveries(monitor)
    entry = next((
        e for e in monitor._discoveries.values()
        if e.session_id is not None and e.session_id == session_id
    ), None)
    if entry is None:
        return DiscoveryLinkResult('unknown_tracking_session')
    if entry.discovery.association_link is not None:
        return DiscoveryLinkResult('already_linked', entry.discovery.incident_id)
    block = discovery_link_block(monitor, entry)
    if block:
        return DiscoveryLinkResult(block)
    if observed_at > monitor._now():
        raise ValueError('future visual observation')
    if not monitor.buffer.contains(observed_at):
        entry.track.broken = True
        return DiscoveryLinkResult('rgb_sample_unavailable')
    reason, match = entry.track.step(
        observed_at, box, monitor._subject_evidence.at(observed_at),
    )
    if match is None:
        return DiscoveryLinkResult(reason)
    key, token = match
    latest = monitor._subject_evidence.latest(key)
    if (latest is None or latest[0] != observed_at or latest[1] != token
            or monitor._now() - observed_at > monitor.policy.max_person_observation_age_s):
        return DiscoveryLinkResult('current_target_unavailable')
    # Different findings from one Cloud reply are not declared the same
    # person merely because their trackers converge on one Pose box.
    if any(other.discovery.discovery_id != entry.discovery.discovery_id
           and other.discovery.request_id == entry.discovery.request_id
           and other.discovery.subject_key == key
           for other in monitor._discoveries.values()):
        return DiscoveryLinkResult('target_claimed_by_other_finding')
    current = next((
        i for i in monitor._incidents.values()
        if i.subject_key == key and i.state is not IncidentState.RESOLVED
    ), None)
    if current is not None:
        if current.subject_association_token != token:
            return DiscoveryLinkResult('incident_target_continuity_unverified')
        if monitor._active_incident == current.incident_id:
            return DiscoveryLinkResult('target_analysis_in_flight')
    elif any(i.subject_key == key and i.subject_association_token == token
             and i.state is IncidentState.RESOLVED
             for i in monitor._incidents.values()):
        return DiscoveryLinkResult('target_incident_closed')
    elif not make_room(monitor):
        return DiscoveryLinkResult('incident_capacity')
    return attach_discovery(monitor, entry, key, token, observed_at, current)


def attach_discovery(
    monitor: Any,
    entry: DeferredDiscovery,
    key: str,
    token: str,
    observed_at: float,
    current: Optional[FallIncident],
    *,
    proof: Any = None,
    reason: str = 'matched_after_tracking',
) -> DiscoveryLinkResult:
    """Atomically link evidence and retire only a fully associated scene.

    Scene answers/alerts are never attributed to a person. A scene with
    another unidentified finding or an unassigned help request stays open.
    """
    discovery = entry.discovery
    finding = discovery.finding
    new = current is None
    evidence_time = discovery.sample_times[finding.regions[-1].frame_index]
    end = discovery.sample_times[-1]
    target = (
        FallIncident(
            str(uuid4()), key, finding.kind, monitor._now(), evidence_time,
            attempts=1, pending=False, subject_association_token=token,
            next_attempt_at=monitor._now() + monitor.policy.retry_interval_s,
        )
        if current is None else replace(current)
    )
    # Reuse the completed crosscheck for an unstarted first analysis when
    # it covers this candidate. Do not erase newer evidence or a requested
    # recheck, and never reset an already consumed attempt/recheck budget.
    if target.pending and target.attempts == 0 and end >= target.last_observed_at:
        target.pending = False
        target.attempts = 1
        target.next_attempt_at = monitor._now() + monitor.policy.retry_interval_s
    # Preserve confirmation already based on suspicious video of this
    # SAME target. An earlier answer with no video (or normal video) must
    # not clear the first newly attached suspicion.
    if (target.state is not IncidentState.HELP_REQUIRED
            and target.answer is not VoiceAnswer.HELP
            and ((target.answer is not None and target.video is None)
                 or (target.video is not None
                     and target.video.assessment is VideoAssessment.NORMAL_ACTIVITY))):
        target.revision += 1
        target.question_id = monitor._pending_question_id(target)
        target.answer = None
        target.answer_question_played = False
        target.situation_assessment = target.help_needed = None
        target.state = IncidentState.VERIFYING
    if 'cloud_crosscheck' not in target.candidate_sources:
        target.candidate_sources += ('cloud_crosscheck',)
    target.last_observed_at = max(target.last_observed_at, evidence_time)
    target.last_window_end = max(target.last_window_end, end)
    target.fall_seen |= finding.assessment is VideoAssessment.OBSERVED_FALL
    target.auto_normal_blocked = True
    target.normal_checks = ()
    target.subject_observation = None
    target.normal_evidence_after = max(target.normal_evidence_after, end, observed_at)
    if (target.video is None
            or target.video.assessment is not VideoAssessment.OBSERVED_FALL):
        target.video = CloudFallReply(finding.assessment, '해당 대상에 연결한 영상 근거')
        target.kind = finding.kind
    target.video_revision = target.revision
    if proof is None:
        assert entry.track is not None
        assert discovery.incident_id is not None
        proof = CloudDiscoveryLink(
            discovery.incident_id, entry.source_revision, token, entry.track.seed_time,
            observed_at, entry.track.samples, entry.track.pending[2],
        )
    linked = replace(
        discovery, reason=reason, subject_key=key,
        incident_id=target.incident_id, association_link=proof,
    )
    events: list[FallRuntimeEvent] = []

    def stage(kind: str, **kwargs: Any) -> None:
        events.append(FallRuntimeEvent(
            str(uuid4()), kind, incident_id=target.incident_id, subject_key=key,
            evidence_revision=target.revision, **kwargs,
        ))

    stage('incident_opened' if new else 'incident_updated', reason='cloud_crosscheck')
    stage('analysis_completed', reply=target.video, analysis=entry.analysis)
    question: Optional[FallIncident] = None
    if target.answer is None:
        if target.question_id is None:
            target.question_id = str(uuid4())
        # Keep an ongoing target conversation bound to its original
        # evidence even while the separate source scene is retired.
        question = monitor._questions.get(target.incident_id)
        if question is None or question.answer is not None:
            question = replace(target)
            stage('question_requested', question_id=target.question_id, reply=target.video)
    stage('cloud_discovery_linked', discovery=linked, reason=reason)
    source = monitor._incidents[discovery.incident_id]
    source = replace(
        source,
        unresolved_discovery_ids=tuple(
            did for did in source.unresolved_discovery_ids
            if did != discovery.discovery_id
        ),
        associated_incident_ids=tuple(dict.fromkeys(
            (*source.associated_incident_ids, target.incident_id),
        )),
    )
    if (source.subject_key is None and not source.unresolved_discovery_ids
            and not source.discovery_overflow
            and len(source.associated_incident_ids) <= 128
            and discovery.discovery_id
            in monitor._incidents[source.incident_id].unresolved_discovery_ids
            and source.state is not IncidentState.HELP_REQUIRED
            and source.answer is not VoiceAnswer.HELP
            and source.notification_level is not NotificationLevel.URGENT):
        source.state = IncidentState.RESOLVED
        source.pending = False
        source.close_reason = 'findings_associated'
        source.merged_into_incident_ids = source.associated_incident_ids
        # Cancel the source before exposing a target question to Manager.
        events.insert(0, FallRuntimeEvent(
            str(uuid4()), 'incident_merged', incident_id=source.incident_id,
            evidence_revision=source.revision, question_id=source.question_id,
            confirmation_scope='scene', reason=source.close_reason,
            merged_into_incident_ids=source.merged_into_incident_ids,
        ))
    if monitor._journal is not None:
        try:
            monitor._journal.append_association(
                device_id=monitor.device_id, boot_id=monitor.boot_id,
                events=tuple(events), incident=target, source_incident=source,
            )
        except Exception:
            monitor._storage_failed = True
            monitor.configure(
                enabled=False, camera_enabled=False,
                cloud_consent=False, connected=False,
            )
            raise FallJournalError('fall association persistence failed') from None
    # Atomic journal success precedes state/events visible to consumers.
    monitor._incidents[target.incident_id] = target
    monitor._incidents[source.incident_id] = source
    if target.answer is None and question is not None:
        monitor._questions[target.incident_id] = question
    entry.discovery = linked
    monitor._events.extend(events)
    monitor._record_clip(
        target, discovery.sample_times[0], discovery.sample_times[-1],
        anchor_kind='cloud_window',
        found_down=finding.kind is CandidateKind.ALREADY_DOWN,
    )
    monitor._record_cloud_boxes(target, discovery.sample_times, finding)
    return DiscoveryLinkResult(reason, target.incident_id)


def record_unidentified_scene(
    monitor: Any,
    request: Any,
    finding: Any,
    *,
    analysis: Any = None,
) -> Optional[str]:
    """Start verification without Pose, reusing the completed Cloud result.

    A finding joins an open scene case only at the same place in the same
    camera view; otherwise it opens its own case and question (bounded by
    max_open_scene_cases). This is not person association: separate
    discoveries retain their own boxes, timestamps and reasons. No
    geometry/appearance guess joins a Pose case.
    """
    return record_unidentified_observation(
        monitor,
        tuple(f.captured_at for f in request.window.frames),
        finding,
        request=request,
        analysis=analysis,
    )


def record_unidentified_observation(
    monitor: Any,
    times: Sequence[float],
    finding: Any,
    *,
    request: Any = None,
    analysis: Any = None,
) -> Optional[str]:
    """Open or update a scene-scoped incident for an unidentified Cloud finding."""
    boxes = tuple(dict.fromkeys(r.box for r in finding.regions))
    points = monitor._map_points(times, finding)
    current = monitor._scene_case_for(
        times, boxes, points, request.request_id if request else None,
    )
    end = times[-1]
    new = current is None
    if new:
        if not make_room(monitor):
            return None
        current = FallIncident(
            str(uuid4()), None, finding.kind, monitor._now(), end,
            attempts=1, pending=False, candidate_sources=('cloud_crosscheck',),
            auto_normal_blocked=True, normal_evidence_after=end,
        )
        monitor._incidents[current.incident_id] = current
    assert current is not None
    if boxes:
        current.scene_boxes = boxes
    if points:
        current.scene_points = points
    current.scene_seen_from = (
        times[0] if current.scene_seen_from is None
        else max(current.scene_seen_from, times[0])
    )
    current.scene_seen_until = (
        end if current.scene_seen_until is None
        else max(current.scene_seen_until, end)
    )
    if request is not None:
        current.scene_request_id = request.request_id
    # A changed Cloud label alone is not a new episode. Keep the room's
    # outstanding/completed/failed question, retaining the stronger finding
    # for decisions without restarting the same confirmation conversation.
    escalation = (
        current.video is not None
        and current.video.assessment is not VideoAssessment.OBSERVED_FALL
        and finding.assessment is VideoAssessment.OBSERVED_FALL
    )
    if escalation:
        monitor._emit('incident_updated', current, reason='target_unidentified')
    current.last_observed_at = max(current.last_observed_at, end)
    current.last_window_end = max(current.last_window_end, end)
    current.fall_seen |= finding.assessment is VideoAssessment.OBSERVED_FALL
    if current.video is None or finding.assessment is VideoAssessment.OBSERVED_FALL:
        current.video = CloudFallReply(finding.assessment, '대상 미확인 영상의 의심 상황')
        current.kind = finding.kind
    current.video_revision = current.revision
    if new:
        monitor._emit('incident_opened', current, reason='target_unidentified')
    monitor._record_clip(
        current, times[0], times[-1], anchor_kind='cloud_window',
        found_down=finding.kind is CandidateKind.ALREADY_DOWN,
    )
    monitor._record_cloud_boxes(current, times, finding)
    monitor._emit(
        'analysis_completed', current, request=request, reply=current.video,
        reason='target_unidentified', analysis=analysis,
    )
    if current.question_id is None:
        monitor.ask_question(current.incident_id)
    if current.answer is not None:
        monitor._emit('decision_required', current, reason='target_unidentified')
    return current.incident_id


def merge_cloud_person(
    monitor: Any,
    request: Any,
    finding: Any,
    subject_key: str,
    token: str,
    *,
    consume_initial: bool = False,
    analysis: Any = None,
) -> Optional[str]:
    """Attach a matched Cloud finding to an existing or newly opened Pose incident."""
    current = next((
        i for i in monitor._incidents.values()
        if i.subject_key == subject_key and i.state is not IncidentState.RESOLVED
    ), None)
    if current is None:
        settled = monitor._settled_subject(subject_key, token, finding)
        if settled is not None:
            # Preserve the final answer/state. The caller records this new
            # finding in cloud_discoveries, including why no question was
            # made. Do not emit a nonterminal event for a closed web case.
            monitor._record_window_clip(settled, request, finding)
            return settled.incident_id
    observed_at = request.window.frames[finding.regions[-1].frame_index].captured_at
    end = request.window.frames[-1].captured_at
    new = current is None
    if new:
        if not make_room(monitor):
            return None
        # The verified result may take 20 seconds: retain its real capture
        # time, not a fabricated fresh timestamp passed through candidate().
        current = FallIncident(
            str(uuid4()), subject_key, finding.kind, monitor._now(), observed_at,
            attempts=1, pending=False,
            next_attempt_at=monitor._now() + monitor.policy.retry_interval_s,
            candidate_sources=('cloud_crosscheck',), subject_association_token=token,
        )
        monitor._incidents[current.incident_id] = current
    else:
        assert current is not None
        if 'cloud_crosscheck' not in current.candidate_sources:
            current.candidate_sources += ('cloud_crosscheck',)
        if consume_initial:
            current.pending = False
            current.attempts = 1
            current.next_attempt_at = monitor._now() + monitor.policy.retry_interval_s
        previous = current.video.assessment if current.video else None
        escalation = (
            previous not in (VideoAssessment.OBSERVED_FALL, VideoAssessment.SUSPECTED_FALL)
            or (previous is VideoAssessment.SUSPECTED_FALL
                and finding.assessment is VideoAssessment.OBSERVED_FALL)
        )
        if (escalation and not consume_initial
                and current.answer is not VoiceAnswer.HELP
                and current.question_id is None):
            current.revision += 1
            current.question_id = monitor._pending_question_id(current)
            current.answer = None
            current.answer_question_played = False
            current.situation_assessment = None
            current.help_needed = None
            monitor._emit('incident_updated', current, reason='cloud_crosscheck')
        current.last_observed_at = max(current.last_observed_at, observed_at)
    assert current is not None
    current.fall_seen |= finding.assessment is VideoAssessment.OBSERVED_FALL
    current.auto_normal_blocked = True
    current.normal_checks = ()
    current.subject_observation = None
    current.normal_evidence_after = max(current.normal_evidence_after, end)
    # A new suspicion must never downgrade an earlier observed fall.
    if (current.video is None
            or current.video.assessment is not VideoAssessment.OBSERVED_FALL
            or finding.assessment is VideoAssessment.OBSERVED_FALL):
        current.video = CloudFallReply(finding.assessment, '주기적 영상에서 해당 대상 확인')
        current.kind = finding.kind
    current.video_revision = current.revision
    current.last_window_end = max(current.last_window_end, end)
    current.last_failure = None
    if new:
        monitor._emit('incident_opened', current, reason='cloud_crosscheck')
    monitor._record_window_clip(current, request, finding)
    monitor._emit(
        'analysis_completed', current, request=request, reply=current.video,
        analysis=analysis,
    )
    if current.question_id is None:
        monitor.ask_question(current.incident_id)
    monitor._decision_needed(current)
    return current.incident_id
