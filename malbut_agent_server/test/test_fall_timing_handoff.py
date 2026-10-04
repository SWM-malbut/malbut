"""Measure timing outcomes through the real Manager handoff codec/queue.

Pose boxes, camera data, clock and Cloud replies are fixtures. No actual
speech, remote model, guardian notification or person-recognition accuracy
is tested. Queue cancellation uses the real coordinator, not actual speech.
"""

import asyncio
import json

import pytest

from malbut_agent_server.fall_runtime import event_metadata
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator
from test_fall_association_input_timing import prepare, pose


@pytest.mark.parametrize('mode,linked,expected_cases', [
    ('same_frame', True, 1),
    ('different_rgb_frames', True, 1),
    ('late_during_cloud', True, 1),
    ('late_after_cloud', True, 1),
    ('pose_empty', False, 2),
    ('pose_unusable', False, 2),
    ('ambiguous_people', False, 2),
])
def test_arrival_order_incidents_and_unique_manager_questions(
        mode, linked, expected_cases, record_property):
    async def run():
        adapter, monitor, clock, provider, original_id, qid, _, _ = await prepare(mode)
        coordinator = FallConfirmationCoordinator(runtime_id='vlm-test')
        relayed = []

        def relay(events):
            for event in events:
                metadata = dict(event_metadata(event), boot_id=monitor.boot_id,
                                runtime_id='vlm-test')
                coordinator.receive(json.dumps(metadata))
                relayed.append(metadata)

        relay(monitor.pending_questions())
        assert set(coordinator.requests) == {qid}

        if mode == 'late_during_cloud':
            provider.release = asyncio.Event()
            task = asyncio.create_task(monitor.run_once())
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            try:
                pose(adapter, clock, 1060, received_at=1060.15)
            finally:
                provider.release.set()
            assert await task
        else:
            assert await monitor.run_once()
        relay(monitor.drain_events())

        calls_before_late = len(provider.calls)
        if mode == 'late_after_cloud':
            pose(adapter, clock, 1060, received_at=1060.15)
            relay(monitor.drain_events())
        assert not await monitor.run_once()
        relay(monitor.drain_events())

        discoveries = [event['discovery'] for event in relayed if 'discovery' in event]
        final = discoveries[-1]
        associated = final['incident_id'] == original_id and final['subject_key'] is not None
        assert associated is linked
        live = [i for i in monitor._incidents.values() if i.state.value != 'resolved']
        assert len(live) == expected_cases
        assert len(coordinator.requests) == expected_cases
        assert len(provider.calls) == calls_before_late == 2
        assert monitor.incident(original_id).question_id == qid

        # Periodic transport replay must not create additional conversations.
        for _ in range(3):
            relay(monitor.pending_questions())
        unique_ids = {event['question_id'] for event in relayed
                      if event['kind'] == 'question_requested'}
        assert len(coordinator.requests) == expected_cases
        assert len(unique_ids) == expected_cases + (mode == 'late_after_cloud')
        if mode == 'late_after_cloud':
            # Keep the historical scene and its issued question, but retire
            # its queue entry. The original person question is reused.
            assert {r.subject_key for r in coordinator.requests.values()} == {
                monitor.incident(original_id).subject_key}
            assert final['association_link']['source_incident_id'] != original_id
            assert len(monitor._incidents) == 2
            assert monitor.incident(final['association_link']['source_incident_id']).close_reason == 'findings_associated'

        result = dict(
            mode=mode, person_linked=associated,
            final_association_reason=final['reason'],
            incident_count=len(monitor._incidents),
            active_incident_count=len(live),
            unique_question_count=len(unique_ids),
            queued_confirmation_count=len(coordinator.requests),
            provider_fixture_calls=len(provider.calls),
            extra_calls_after_reassociation=len(provider.calls) - calls_before_late,
            actual_cloud_api_calls=0,
        )
        record_property('timing_handoff', json.dumps(result, sort_keys=True))
        print(json.dumps(result, sort_keys=True))
    asyncio.run(run())
