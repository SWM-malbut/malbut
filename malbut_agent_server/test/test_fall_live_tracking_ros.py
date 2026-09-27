"""Automatic ROS -> native Cloud reply -> tracking -> Pose -> journal wiring.

Real DDS and production runtime/codec/controller. RGB, Pose and tracking boxes
are fixtures; HTTP is replaced. No live camera, speech, network or model call.
"""

import asyncio
import json

import pytest

from test_fall_pc_flow import PcFlow, RUN_ROS, pose_fixture
from test_fall_live_tracking import Tracker

pytestmark = pytest.mark.skipif(not RUN_ROS, reason='opt-in automatic tracking ROS check')


@pytest.mark.parametrize('mode', ['match', 'other_person', 'camera_off', 'backend_failure'])
def test_ros_discovery_automatically_starts_and_finishes_tracking(tmp_path, mode):
    async def run():
        tracker = Tracker(
            box=pose_fixture(True).box if mode != 'other_person' else (.01, .01, .15, .3),
            hold=mode == 'camera_off',
            error=RuntimeError('fixture') if mode == 'backend_failure' else None)
        seeds = []
        def factory(**seed):
            seeds.append(seed)
            return tracker
        flow = PcFlow(tmp_path, tracker_factory=factory)
        try:
            await flow.start()
            await flow.until(lambda: any(e['kind'] == 'question_requested' for e in flow.events))
            question = next(e for e in flow.events if e['kind'] == 'question_requested')
            original_post = flow.provider._post

            async def scene_post(body):
                if flow.provider.requests[-1].purpose != 'crosscheck':
                    return await original_post(body)
                payload = json.loads(body)
                assert 'box_2d' in payload['messages'][0]['content']
                flow.provider.payloads.append(payload)
                request = flow.provider.requests[-1]
                # Only one localized image: requires later RGB tracking/Pose;
                # direct multi-sample association cannot pass here.
                left, top, right, bottom = pose_fixture(True).box
                content = dict(assessment='suspected_fall', explanation='fixture',
                    findings=[dict(assessment='suspected_fall', kind='already_down',
                        regions=[dict(frame_index=len(request.window.frames) - 1,
                                      box_2d=[round(v * 1000)
                                              for v in (top, left, bottom, right)])])])
                return json.dumps(dict(done=True, done_reason='stop',
                    message=dict(role='assistant', content=json.dumps(content)))).encode()
            flow.provider._post = scene_post
            # Test schedule only: no waiting 60s, production policy remains 60s.
            flow.vlm.monitor._scan_anchor -= 60
            await flow.until(lambda: bool(seeds))
            scene_question = lambda: any(
                e['kind'] == 'question_requested' and e['incident_id'] != question['incident_id']
                for e in flow.events)
            await flow.until(scene_question)
            if mode == 'camera_off':
                flow.change_settings(camera_enabled=False)
                await flow.until(lambda: tracker.closed)
            elif mode == 'match':
                await flow.until(lambda: any(e['kind'] == 'cloud_discovery_linked'
                                             for e in flow.events))
            elif mode == 'backend_failure':
                await flow.until(lambda: tracker.closed)
            else:
                await flow.pump(1.5)
            links = [e for e in flow.events if e['kind'] == 'cloud_discovery_linked']
            assert len(seeds) == 1
            assert set(seeds[0]) == {'seed_time', 'seed_box'}
            assert len(flow.provider.requests) == 2
            if mode == 'match':
                assert len(links) == 1 and links[0]['incident_id'] == question['incident_id']
                assert flow.vlm.monitor.incident(question['incident_id']).question_id == question['question_id']
                assert len(flow.journal.discoveries()) == 2
            else:
                assert not links and len(flow.journal.discoveries()) == 1
            assert not flow.task.done()
        finally:
            await flow.close()
        assert tracker.closed
    asyncio.run(run())

