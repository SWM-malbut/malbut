"""Cross-language contract fixture. No real model, HTTP, speech or push calls."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.application.cloud_fall_monitor import CloudFallMonitor
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.domain.fall_monitoring import (
    AgentCheckReply, CandidateKind, CloudFallReply, FallCandidate, FallRuntimePolicy,
    RgbFrame, VideoAssessment, VoiceAnswer,
)


class Provider:
    execution_target = 'cloud'

    async def analyze(self, request):
        return CloudFallReply(VideoAssessment.OBSERVED_FALL, 'test observation')


with TemporaryDirectory() as directory:
    journal = SqliteFallJournal(Path(directory) / 'fall.sqlite', device_id='robot-a')
    try:
        monitor = CloudFallMonitor(
            device_id='robot-a', boot_id='boot-test', journal=journal, provider=Provider(),
            policy=FallRuntimePolicy.agreed(
                retry_interval_s=3, max_person_observation_age_s=2, clip_window_s=10,
                max_frame_age_s=2, max_calls_per_minute=20, max_incidents=10, max_images=12),
            buffer=FallFrameBuffer(retention_s=30, max_bytes=10000, max_frames=100),
            clock=lambda: 100.0)
        monitor.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
        monitor.ingest_rgb(RgbFrame(100, b'\xff\xd8test\xff\xd9'))
        iid = monitor.candidate(FallCandidate('c', 'p', 'yolo_pose', CandidateKind.MOTION_SEEN, 100))
        asyncio.run(monitor.run_once())
        qid = monitor.incident(iid).question_id
        monitor.agent_reply(AgentCheckReply(iid, qid, 'p', 1, VoiceAnswer.OKAY, True))
        monitor.agent_reply(AgentCheckReply(iid, qid, 'p', 1, VoiceAnswer.HELP, True))
        payloads = []
        while (row := journal.pending()) is not None:
            payloads.append(json.loads(row['payload']))
            journal.acknowledge(row['event_id'])
        print(json.dumps(payloads))
    finally:
        journal.close()
