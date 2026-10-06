"""Ten-minute offline lifecycle: rest after clearance, recover, then fall again.

Real candidate rules, input adapter, monitor, coordinator and dialogue state.
Pose estimates/IDs, RGB bytes, Cloud/semantic replies and clock are fixtures.
No YOLO/VLM inference, ROS transport, speech recognition/playback or push.
"""

import asyncio
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path

from malbut_agent_server.application.cloud_fall_monitor import CloudFallMonitor
from malbut_agent_server.application.fall_detector_input import FallDetectorInput
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, CloudFallReply, CloudPersonFinding, CloudPersonRegion,
    FallRuntimePolicy, IncidentState, VideoAssessment, VoiceAnswer,
)
from malbut_agent_server.fall_runtime import apply_decision, event_metadata
from malbut_agent_server.situation_dialogue import SituationDialogue, SituationInterpretation
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator


def test_continuous_rest_is_one_episode_but_recovery_then_fall_starts_another(
        monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / 'homecam_agent/homecam_detector'))
    monkeypatch.syspath_prepend(str(root / 'homecam_agent/homecam_detector/test'))
    from homecam_detector.fall_candidate import FallCandidateDetector
    from test_fall_candidate import body, step

    class Clock:
        value = 100.0

        def __call__(self):
            return self.value

    clock = Clock()

    class CloudFixture:
        execution_target = 'cloud'  # Port marker only; no network transport.

        def __init__(self):
            self.calls = []

        async def analyze(self, request):
            elapsed = round(clock() - 100, 3)
            self.calls.append(dict(t=elapsed, purpose=request.purpose,
                                   images=len(request.window.frames)))
            if request.purpose == 'incident':
                return CloudFallReply(VideoAssessment.OBSERVED_FALL, 'fixture: fall')
            if 190 <= elapsed < 245.4:
                # The whole five-second window is upright at the t=240 scan.
                return CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, 'fixture: upright')
            last = len(request.window.frames) - 1
            assert last > 0
            finding = CloudPersonFinding(
                VideoAssessment.SUSPECTED_FALL, CandidateKind.ALREADY_DOWN,
                (CloudPersonRegion(0, body('lying').box),
                 CloudPersonRegion(last, body('lying').box)))
            return CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'fixture: still down',
                                  (finding,))

    class SemanticFixture:
        def __init__(self, help_needed):
            self.help_needed = help_needed

        def evaluate(self, context):
            if context.answer is None:
                return SituationInterpretation('unknown', None, '도움이 필요하세요?')
            return SituationInterpretation(
                'confirmed_incident' if self.help_needed else 'resolved',
                self.help_needed, '')

    async def run():
        config = json.loads((root / 'malbut_agent_server/config/fall_runtime.example.json').read_text())
        provider = CloudFixture()
        monitor = CloudFallMonitor(
            device_id='offline-repeat-test', boot_id='offline-repeat-boot',
            policy=FallRuntimePolicy.agreed(**config['policy']),
            buffer=FallFrameBuffer(retention_s=config['retention_s'],
                                  max_bytes=config['buffer_bytes'],
                                  max_frames=config['buffer_frames']),
            provider=provider, clock=clock)
        adapter = FallDetectorInput(monitor, max_source_age_s=config['max_source_age_s'])
        adapter.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
        coordinator = FallConfirmationCoordinator(runtime_id='offline-repeat')
        detector = FallCandidateDetector()
        events, candidates, dialogues, pose_changes = [], [], [], []
        counters = Counter()
        active = None
        previous_pose_check = None

        def relay(event, *, replay=False):
            if not replay:
                counters[event.kind] += 1
                events.append(dict(t=round(clock() - 100, 3), kind=event.kind,
                                   incident=event.incident_id, question=event.question_id,
                                   reason=event.reason,
                                   discovery_incident=(event.discovery.incident_id
                                                       if event.discovery else None)))
            coordinator.receive(json.dumps(dict(
                event_metadata(event), boot_id=monitor.boot_id, runtime_id='offline-repeat')))

        def flush():
            for event in monitor.drain_events():
                relay(event)

        try:
            for tick in range(3001):
                elapsed = tick / 5
                clock.value = 100 + elapsed
                capture = 1000 + elapsed
                assert adapter.rgb(b'\xff\xd8fixture\xff\xd9', capture=capture,
                                   frame_id='fixture-camera', source_now=capture, now=clock())
                upright = tick < 2 or 185 <= elapsed < 245.4
                output = step(detector, {'person-1': body('upright' if upright else 'lying')},
                              capture)
                output['frameId'] = 'fixture-camera'
                check = output['tracks'][0]['subjectCheck']['state']
                if check != previous_pose_check:
                    pose_changes.append(dict(t=elapsed, state=check))
                    previous_pose_check = check
                candidates.extend(dict(t=elapsed, kind=c['candidateKind'])
                                  for c in output['candidates'])
                adapter.candidates(json.dumps(output), source_now=capture, now=clock())
                flush()
                for _ in range(4):
                    if not await monitor.run_once():
                        break
                    flush()
                else:
                    raise AssertionError('unbounded ready Cloud work')

                # Production heartbeats may deliver the same question again.
                if tick % 5 == 0:
                    for event in monitor.pending_questions():
                        relay(event, replay=True)
                        relay(event, replay=True)

                if active is not None and elapsed >= active['finish_at']:
                    utterance = '도움이 필요해요' if active['help_needed'] else '괜찮아요'
                    final = active['dialogue'].answer(utterance)
                    assert final.result is not None
                    assert coordinator.complete(active['request'], **asdict(final.result))
                    active['record'].update(finished_t=elapsed, result=asdict(final.result))
                    active = None
                for command in coordinator.drain_commands():
                    assert apply_decision(monitor, json.dumps(command)) is not False
                flush()
                if active is None and coordinator.requests:
                    request = next(iter(coordinator.requests.values()))
                    help_needed = elapsed >= 245.4
                    dialogue = SituationDialogue(SemanticFixture(help_needed))
                    assert dialogue.start(request.request_id, 'fall', request.summary).result is None
                    record = dict(started_t=elapsed, incident=request.incident_id,
                                  question=request.question_id)
                    dialogues.append(record)
                    active = dict(request=request, dialogue=dialogue, help_needed=help_needed,
                                  finish_at=elapsed + 5, record=record)

                if 6 <= elapsed < 245.4:
                    # No extra case or dialogue while resting OR recovering.
                    assert counters['incident_opened'] == 1, f'extra incident at t={elapsed}s'
                    assert len(dialogues) == 1
                    assert monitor.incident(dialogues[0]['incident']).state is IncidentState.RESOLVED

            opened = [e for e in events if e['kind'] == 'incident_opened']
            questions = [e for e in events if e['kind'] == 'question_requested']
            notifications = [e for e in events if e['kind'] == 'notification_requested']
            assert len(candidates) == len(opened) == len(questions) == len(dialogues) == 2
            assert [c['kind'] for c in candidates] == ['fall_suspected', 'fall_suspected']
            assert opened[0]['t'] < 1 and 245.4 <= opened[1]['t'] <= 246
            assert opened[0]['incident'] != opened[1]['incident']
            assert dialogues[0]['question'] != dialogues[1]['question']
            assert any(p['state'] == 'clear' and 187 <= p['t'] < 245.4 for p in pose_changes)
            first, second = [monitor.incident(e['incident']) for e in opened]
            assert first.subject_key == second.subject_key == 'pose:0:person-1'
            assert first.state is IncidentState.RESOLVED and first.answer is VoiceAnswer.OKAY
            assert second.state is IncidentState.HELP_REQUIRED and second.answer is VoiceAnswer.HELP
            assert len(notifications) == 1 and notifications[0]['incident'] == second.incident_id
            assert active is None and not coordinator.requests and not monitor.pending_questions()
            continued = [e for e in events if e['reason'] == 'settled_episode_continues']
            assert len(continued) == 3
            assert all(e['discovery_incident'] == first.incident_id for e in continued)
            assert Counter(c['purpose'] for c in provider.calls) == {'incident': 2, 'crosscheck': 10}
            assert counters['agent_check_failed'] == 0
            report = dict(
                scope=__doc__, duration_s=600, frames=3001,
                real_cloud_calls=0, real_audio_playbacks=0, real_notifications=0,
                counts=dict(counters), candidates=candidates, dialogue_requests=dialogues,
                pose_check_changes=pose_changes, cloud_calls=provider.calls,
                final_states=[dict(incident=i.incident_id, state=i.state.value, answer=i.answer.value)
                              for i in (first, second)], timeline=events)
            artifact = tmp_path / 'recovery_then_second_fall.json'
            artifact.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(f'Lifecycle result: {artifact}')
        finally:
            coordinator.close()
            await monitor.close()

    asyncio.run(run())
