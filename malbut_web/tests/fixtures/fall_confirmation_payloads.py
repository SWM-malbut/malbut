"""Actual Manager-result journal payloads, without network or speech calls."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.application.cloud_fall_monitor import CloudFallMonitor
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, CloudFallReply, FallCandidate, FallRuntimePolicy, RgbFrame, VideoAssessment,
)


class Provider:
    execution_target = 'cloud'

    async def analyze(self, request):
        return CloudFallReply(VideoAssessment.OBSERVED_FALL, 'test observation')


with TemporaryDirectory() as directory:
    journal = SqliteFallJournal(Path(directory) / 'fall.sqlite', device_id='robot-a')
    now = [100.0]
    try:
        monitor = CloudFallMonitor(
            device_id='robot-a', boot_id='confirmation-test', journal=journal, provider=Provider(),
            policy=FallRuntimePolicy.agreed(
                retry_interval_s=3, max_person_observation_age_s=2, clip_window_s=10,
                max_frame_age_s=2, max_calls_per_minute=20, max_incidents=10, max_images=12),
            buffer=FallFrameBuffer(retention_s=30, max_bytes=10000, max_frames=100),
            clock=lambda: now[0])
        monitor.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
        for assessment in ('confirmed_incident', 'resolved', 'unknown'):
            for help_needed in (False, True):
                subject = f'{assessment}-{help_needed}'
                monitor.ingest_rgb(RgbFrame(now[0], b'\xff\xd8test\xff\xd9'))
                iid = monitor.candidate(FallCandidate(
                    subject, subject, 'yolo_pose', CandidateKind.MOTION_SEEN, now[0]))
                asyncio.run(monitor.run_once())
                # Exercise the newly emitted revision event as well as final outcomes.
                if assessment == 'confirmed_incident' and not help_needed:
                    now[0] += 3
                    monitor.ingest_rgb(RgbFrame(now[0], b'\xff\xd8test\xff\xd9'))
                    monitor.candidate(FallCandidate(
                        'changed', subject, 'yolo_pose', CandidateKind.MOTION_SEEN, now[0],
                        significant_change=True))
                    asyncio.run(monitor.run_once())
                incident = monitor.incident(iid)
                assert monitor.confirmation_result(
                    incident_id=iid, question_id=incident.question_id, subject_key=subject,
                    evidence_revision=incident.revision, situation_assessment=assessment,
                    help_needed=help_needed)
                now[0] += 3
        payloads = []
        while (row := journal.pending()) is not None:
            payloads.append(json.loads(row['payload']))
            journal.acknowledge(row['event_id'])
        print(json.dumps(payloads))
    finally:
        journal.close()
