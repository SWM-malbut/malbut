#!/usr/bin/env python3
"""Synthetic ordering tests through the incident core, SQLite, and fall coordinator.

No network, inference, ROS, spoken questions, or operating settings are changed.
Different-person boxes and missing observations are explicit test fixtures.
"""
import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path

from replay_reviewed_pose_cloud import (
    REPO, CloudFallMonitor, CloudFallReply, CloudPersonFinding, CloudPersonRegion,
    CandidateKind, VideoAssessment, FallFrameBuffer, FallRuntimePolicy, RgbFrame,
    SqliteFallJournal, digest, read, require, save,
)
from malbut_agent_server.domain.fall_monitoring import (
    FallCandidate, SubjectFrame, SubjectPose, SubjectCheckState,
)
from malbut_agent_server.fall_runtime import event_metadata
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator

BOX = (.1, .4, .7, .9)
OTHER = (.75, .1, .95, .95)
MODES = ('pose_first_direct', 'pose_first_deferred', 'cloud_first_deferred',
         'different_person', 'ambiguous_people', 'identity_switch', 'stale_track',
         'camera_off', 'old_answer', 'candidate_during_cloud')


class Clock:
    value = 100.0
    def __call__(self):
        return self.value


class Provider:
    execution_target = 'cloud'
    def __init__(self, sparse=False):
        regions = (CloudPersonRegion(0, BOX), CloudPersonRegion(1, BOX))
        self.finding = CloudPersonFinding(VideoAssessment.SUSPECTED_FALL,
            CandidateKind.ALREADY_DOWN, regions[:1] if sparse else regions)
        self.calls = []
        self.started = asyncio.Event()
        self.release = None

    async def analyze(self, request):
        self.calls.append(request)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'synthetic fixture',
                              (self.finding,) if request.purpose == 'crosscheck' else ())


def person(key='P1', box=BOX):
    return SubjectPose(key, box, SubjectCheckState.UNKNOWN, True)


async def trial(mode, destination):
    require(mode in MODES, 'unknown trial')
    require(not destination.exists(), 'preserve outputs')
    destination.mkdir(mode=0o700, parents=True)
    clock, provider = Clock(), Provider(mode == 'pose_first_deferred')
    config = read(REPO / 'malbut_agent_server/config/fall_runtime.example.json')
    journal = SqliteFallJournal(destination / 'events.sqlite', device_id='ordering')
    m = CloudFallMonitor(device_id='ordering', boot_id='ordering-boot', clock=clock,
        provider=provider, journal=journal, policy=FallRuntimePolicy.agreed(**config['policy']),
        buffer=FallFrameBuffer(retention_s=config['retention_s'], max_bytes=config['buffer_bytes'],
                               max_frames=config['buffer_frames']))
    m.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    manager = FallConfirmationCoordinator(runtime_id='ordering-runtime')
    events, phases = [], []

    def drain():
        batch = m.drain_events()
        for e in batch:
            metadata = event_metadata(e)
            events.append(metadata)
            manager.receive(json.dumps(dict(metadata, boot_id='ordering-boot',
                                            runtime_id='ordering-runtime')))
        return batch

    def phase(name):
        drain()
        phases.append(dict(name=name, incident_ids=list(m._incidents),
            questions=[dict(incident_id=r.incident_id, question_id=r.question_id,
                            subject_key=r.subject_key) for r in manager.requests.values()],
            fake_provider_calls=len(provider.calls)))

    def feed(t, people=(person(),)):
        clock.value = t
        require(m.ingest_rgb(RgbFrame(t, b'\xff\xd8synthetic-test\xff\xd9')), 'RGB rejected')
        require(m.ingest_subject_frame(SubjectFrame(t, people, .5)), 'Pose rejected')

    def candidate(t, cid='candidate-1', change=False):
        return m.candidate(FallCandidate(cid, 'P1', 'yolo_pose', CandidateKind.MOTION_SEEN,
                                        t, significant_change=change))

    target_id = None
    reason = None
    late_answer_rejected = None
    try:
        pose_first = mode.startswith('pose_first')
        initially_visible = pose_first or mode == 'candidate_during_cloud'
        feed(159.5, (person(),) if initially_visible else ())
        if pose_first:
            target_id = candidate(159.5)
            require(await m.run_once(), 'initial incident analysis not run')
            phase('pose_first_analysis')
        feed(160, (person(),) if initially_visible else ())
        if mode == 'candidate_during_cloud':
            provider.release = asyncio.Event()
            task = asyncio.create_task(m.run_once())
            await provider.started.wait()
            feed(160.25)
            target_id = candidate(160.25)
            provider.release.set()
            require(await task, 'in-flight scan not run')
        else:
            require(await m.run_once(), 'crosscheck not run')
        batch = drain()
        discoveries = [e.discovery for e in batch if e.discovery is not None]
        require(len(discoveries) == 1, 'one finding expected')
        discovery = discoveries[0]
        phase('cloud_reply')
        if discovery.subject_key is not None:
            reason = discovery.reason
            require(discovery.incident_id == target_id, 'direct match changed existing ID')
        elif mode == 'candidate_during_cloud':
            reason = discovery.reason
            try:
                m.begin_discovery_tracking(discovery.discovery_id)
            except ValueError:
                pass
            else:
                raise AssertionError('changed incident should not accept this stale scan')
            # Rejecting the stale scan must not abandon the newer Pose case.
            require(m.incident(target_id).pending, 'new Pose evidence lost its analysis')
            require(await m.run_once(), 'newer Pose evidence was not processed')
            phase('newer_pose_analysis')
        else:
            sid = m.begin_discovery_tracking(discovery.discovery_id)
            for t in (159.5, 160):
                m.ingest_discovery_track(sid, observed_at=t, box=BOX)
            for index, t in enumerate((160.25, 160.5, 160.75)):
                people = ((person('OTHER', OTHER),) if mode == 'different_person' else
                    (person(), person('OTHER', BOX)) if mode == 'ambiguous_people' else
                    (person('CHANGED'),) if mode == 'identity_switch' and index == 2 else
                    (person(),))
                feed(t, people)
                if mode != 'stale_track':
                    if mode == 'camera_off' and index == 2:
                        m.configure(enabled=True, camera_enabled=False, cloud_consent=True, connected=True)
                    result = m.ingest_discovery_track(sid, observed_at=t, box=BOX)
            if mode == 'stale_track':
                clock.value = 163.0
                for t in (160.25, 160.5, 160.75):
                    result = m.ingest_discovery_track(sid, observed_at=t, box=BOX)
            reason = result.reason
            if result.incident_id:
                if target_id is not None:
                    require(target_id == result.incident_id, 'deferred match changed existing ID')
                target_id = result.incident_id
            phase('tracking_finished')
            if result.incident_id:
                before = (len(m._incidents), len(provider.calls), len(manager.requests))
                for _ in range(30):
                    require(m.ingest_discovery_track(sid, observed_at=clock(), box=BOX).reason
                            == 'already_linked', 'duplicate track changed result')
                drain()
                require(before == (len(m._incidents), len(provider.calls), len(manager.requests)),
                        'duplicate tracking created work')
        # A successful link can precede the final sample; later samples then
        # correctly report already_linked. Count the actual transition event.
        linked = (discovery.subject_key is not None or
                  any(e['kind'] == 'cloud_discovery_linked' for e in events))
        duplicate_growth = None
        if linked:
            phase('before_duplicate_delivery')
            baseline = (len(m._incidents), len(provider.calls), len(manager.requests))
            old_events = list(events)
            for _ in range(30):
                require(candidate(clock(), 'duplicate-pose') == target_id, 'Pose made another case')
                for e in old_events:
                    manager.receive(json.dumps(dict(e, boot_id='ordering-boot', runtime_id='ordering-runtime')))
            drain()
            require(not await m.run_once(), 'duplicate evidence queued an unnecessary analysis')
            phase('after_duplicate_delivery')
            duplicate_growth = tuple(b-a for a, b in zip(baseline,
                (len(m._incidents), len(provider.calls), len(manager.requests))))
            require(duplicate_growth == (0, 0, 0), 'duplicate delivery created work')
        if mode == 'old_answer':
            old = m.incident(target_id)
            feed(161.0)
            require(candidate(161, 'changed-action', True) == target_id, 'new evidence changed ID')
            late_answer_rejected = not m.confirmation_result(incident_id=target_id,
                question_id=old.question_id, subject_key='P1', evidence_revision=old.revision,
                situation_assessment='resolved', help_needed=False)
            require(late_answer_rejected, 'obsolete answer accepted')
            phase('obsolete_answer_rejected')
        unresolved, discovery_rows = journal.unresolved(), journal.discoveries()
        result = dict(mode=mode, reason=reason, linked=linked, target_id=target_id,
            phases=phases, events=events, fake_provider_calls=len(provider.calls),
            call_purposes=[r.purpose for r in provider.calls],
            person_cases=sum(i.subject_key is not None for i in m._incidents.values()),
            scene_cases=sum(i.subject_key is None for i in m._incidents.values()),
            manager_questions=len(manager.requests), duplicate_growth=duplicate_growth,
            obsolete_answer_rejected=late_answer_rejected, real_api_calls=0)
    finally:
        journal.close()
    reopened = SqliteFallJournal(destination / 'events.sqlite', device_id='ordering')
    try:
        require(reopened.unresolved() == unresolved and reopened.discoveries() == discovery_rows,
                'persisted records changed on reopen')
    finally:
        reopened.close()
    result['journal_reopen_verified'] = True
    save(destination / 'result.json', result)
    return {k: v for k, v in result.items() if k not in {'events', 'phases', 'target_id'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve outputs')
    args.output.mkdir(mode=0o700, parents=True)
    code = {str(p): digest(p) for p in
            (REPO / 'malbut_agent_server/malbut_agent_server').rglob('*.py')}
    for p in (Path(__file__), REPO / 'malbut_agent_server/config/fall_runtime.example.json',
              REPO / 'malbut_fall_coordinator/malbut_fall_coordinator/fall_confirmation.py'):
        code[str(p)] = digest(p)
    save(args.output / 'plan.json', dict(code=code, synthetic=True, real_api_calls=0,
        modes=list(MODES), scope='incident core + SQLite + fall coordinator, not ROS'))
    results = [asyncio.run(trial(mode, args.output / mode)) for mode in MODES]
    save(args.output / 'summary.json', results)
    for path, expected in code.items():
        require(digest(path) == expected, 'code changed during validation')
    save(args.output / 'completed.json', dict(complete=True, synthetic=True, new_api_calls=0,
        code=code,
        files={str(p.relative_to(args.output)): digest(p)
               for p in args.output.rglob('*') if p.is_file()}))
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
