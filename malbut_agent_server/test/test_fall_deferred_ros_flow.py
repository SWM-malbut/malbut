"""Opt-in ROS transport tests for the local deferred-link boundary.

Real settings, camera/Pose topics, candidate core, event transport and SQLite.
Pixels, Pose estimates, Cloud finding and visual-track boxes are fixtures.
The local tracking hook is called in-process: no new production ROS API.
"""

import asyncio
import json
import time
from uuid import uuid4

import pytest

from test_fall_pc_flow import PcFlow, RUN_ROS, pose_fixture
from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, CloudFallReply, CloudFallRequest, CloudPersonFinding,
    CloudPersonRegion, IncidentState, VideoAssessment,
)
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator


pytestmark = pytest.mark.skipif(not RUN_ROS, reason='opt-in live ROS deferred-link check')


@pytest.mark.parametrize('mode', ['match', 'other_person', 'camera_off', 'delayed_result'])
def test_deferred_link_through_real_ros_events_and_journal(tmp_path, mode):
    async def run():
        flow = PcFlow(tmp_path)
        monitor = flow.vlm.monitor
        try:
            await flow.start()
            await flow.until(lambda: any(e['kind'] == 'question_requested' for e in flow.events))
            original_question = next(e for e in flow.events if e['kind'] == 'question_requested')
            iid = original_question['incident_id']
            subject = original_question['subject_key']
            await flow.until(lambda: (
                monitor._subject_evidence.latest(subject) is not None
                and monitor._subject_evidence.latest(subject)[1] is not None
                and monitor.buffer.contains(monitor._subject_evidence.latest(subject)[0])))
            latest = monitor._subject_evidence.latest(subject)
            window = monitor.buffer.window(end=latest[0], duration_s=5, max_images=1, max_age_s=2)
            assert window.frames[0].captured_at == latest[0]
            # Single Cloud localization deliberately cannot directly associate.
            # The subsequent 3+ real-timestamp Pose observations must do so.
            box = pose_fixture(True).box if mode != 'other_person' else (.01, .01, .15, .3)
            finding = CloudPersonFinding(
                VideoAssessment.SUSPECTED_FALL, CandidateKind.ALREADY_DOWN,
                (CloudPersonRegion(0, box),))
            request = CloudFallRequest(
                'local-fixture-' + uuid4().hex, 'crosscheck', monitor.device_id,
                monitor.boot_id, None, None, 0, window, None)
            monitor._record_crosscheck(request, CloudFallReply(
                VideoAssessment.SUSPECTED_FALL, 'test-only observation', (finding,)),
                monitor._subject_evidence.snapshot(window), monitor._scene_incident_versions())
            discovery = next(e.discovery for e in monitor._events if e.discovery)
            source_id = discovery.incident_id
            source = monitor.incident(source_id)
            sid = monitor.begin_discovery_tracking(discovery.discovery_id)
            result = monitor.ingest_discovery_track(sid, observed_at=latest[0], box=box)
            assert result.incident_id is None
            last_stamp = latest[0]
            if mode == 'camera_off':
                flow.change_settings(camera_enabled=False)
                await flow.until(lambda: flow.reports[-1].requested_revision == flow.revision)
                result = monitor.ingest_discovery_track(sid, observed_at=last_stamp, box=box)
                assert result.reason == 'unknown_tracking_session'
            elif mode == 'delayed_result':
                delayed = []
                deadline = time.monotonic() + 3
                while len(delayed) < 3:
                    assert time.monotonic() < deadline, 'no new ROS Pose observations'
                    await flow.pump(.025)
                    latest = monitor._subject_evidence.latest(subject)
                    if (latest and latest[0] > last_stamp
                            and monitor.buffer.contains(latest[0])):
                        last_stamp = latest[0]
                        delayed.append(last_stamp)
                # Actual wall time passes while camera/heartbeats continue.
                # Do not rewrite the old capture timestamps to receipt time.
                await flow.pump(2.2)
                assert time.monotonic() - delayed[-1] > 2
                for captured_at in delayed:
                    result = monitor.ingest_discovery_track(
                        sid, observed_at=captured_at, box=box)
                assert result.reason == 'current_target_unavailable'
            else:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and result.incident_id is None:
                    await flow.pump(.025)
                    latest = monitor._subject_evidence.latest(subject)
                    if not latest or latest[0] <= last_stamp:
                        continue
                    if not monitor.buffer.contains(latest[0]):
                        continue
                    last_stamp = latest[0]
                    result = monitor.ingest_discovery_track(sid, observed_at=last_stamp, box=box)
            await flow.pump(.2)
            links = [e for e in flow.events if e['kind'] == 'cloud_discovery_linked']
            if mode == 'match':
                assert result.reason == 'matched_after_tracking', result
                assert result.incident_id == iid and len(links) == 1
                assert links[0]['incident_id'] == iid
                assert links[0]['discovery']['association_link']['source_incident_id'] == source_id
                assert monitor.incident(iid).question_id == original_question['question_id']
                before = monitor.incident(iid)
                assert monitor.ingest_discovery_track(
                    sid, observed_at=last_stamp, box=box).reason == 'already_linked'
                assert monitor.incident(iid) == before
                assert len(flow.provider.requests) == 1
                coordinator = FallConfirmationCoordinator(runtime_id=flow.ids['vlm'])
                for event in flow.events:
                    coordinator.receive(json.dumps(event))
                assert {r.incident_id for r in coordinator.requests.values()} == {iid}
                # A late answer to the retired scene cannot clear the person.
                scene_question = next(
                    e for e in flow.events
                    if e['kind'] == 'question_requested' and e['incident_id'] == source_id)
                flow.confirm(scene_question, situation_assessment='resolved', help_needed=False)
                await flow.pump(.3)
                assert monitor.incident(iid).state is not IncidentState.RESOLVED
                assert monitor.incident(source_id).state is IncidentState.RESOLVED
                assert monitor.incident(source_id).close_reason == 'findings_associated'
                assert monitor.incident(source_id).answer is None
            else:
                assert not links
                assert monitor.incident(source_id).question_id == source.question_id
            persisted = flow.journal.discoveries()
            assert len(persisted) == (2 if mode == 'match' else 1)
        finally:
            await flow.close()
        reopened = SqliteFallJournal(flow.db, device_id=flow.settings.device_id)
        try:
            assert reopened.discoveries() == persisted
        finally:
            reopened.close()

    asyncio.run(run())
