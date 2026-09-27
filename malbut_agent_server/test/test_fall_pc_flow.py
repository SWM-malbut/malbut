"""Opt-in PC flow checks using real ROS messages, never a remote model.

Requires MALBUT_RUN_FALL_ROS_TESTS=1 and ROS_LOCALHOST_ONLY=1. Every topic and
Service is remapped under a new UUID, away from robot topics. The fall settings relay,
VLM node, tracker, candidate rules, Cloud JSON codec and SQLite journal are real.
Camera pixels, pose estimates, server replies and confirmation results are test data.
Only the Cloud provider's HTTP transport is replaced; no credential file is read.
"""

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import pytest


from malbut_agent_server.adapters.outbound.ollama_cloud_fall import (  # noqa: E402
    OllamaCloudFallProvider,
)


RUN_ROS = os.environ.get('MALBUT_RUN_FALL_ROS_TESTS') == '1'
# Module-level pytest.skip can abort collection of sibling files with the
# Humble launch_testing plugin. Skip individual tests and defer heavy imports.
pytestmark = pytest.mark.skipif(not RUN_ROS, reason='opt-in live ROS PC check')
if RUN_ROS:
    if os.environ.get('ROS_LOCALHOST_ONLY') != '1':
        raise RuntimeError('PC flow checks require ROS_LOCALHOST_ONLY=1')
    # Explicit execution must fail on missing dependencies, not skip checks.
    import numpy as np
    from PIL import Image as PilImage
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data,
    )
    from sensor_msgs.msg import Image
    from std_msgs.msg import String

    from homecam_detector.fall_candidate import FallCandidateDetector
    from homecam_detector.fall_pose_control import FallPoseControl
    from homecam_detector.pose import PersonPose, PoseKeypoint
    from homecam_detector.pose_tracker import PersonPoseTracker
    from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
    from malbut_agent_server.fall_runtime import FallNodeSettings
    from malbut_agent_server.ros_fall_monitor import create_fall_node, spin_runtime
    from malbut_interfaces.msg import FallRuntimeStatus, FallSettingsReport, FallSettingsSnapshot
    from malbut_fall_coordinator.fall_settings_link import FallSettingsLink


TOPICS = (
    '/malbut/falls/settings/apply', '/malbut/falls/settings/snapshot',
    '/malbut/falls/settings/report', '/malbut/falls/status',
    '/malbut/falls/control/heartbeat', '/malbut/falls/runtime/events',
    '/malbut/falls/runtime/agent_reply', '/malbut/falls/runtime/subject_observation',
    '/malbut/falls/runtime/decision', '/homecam/person_poses',
    '/homecam/fall_candidates', '/depth_cam/rgb0/image_raw',
)


def pose_fixture(lying):
    """Synthetic keypoints, not an ONNX inference or an accuracy ground truth."""
    if lying:
        box = (190, 250, 570, 350)
        points = ((235, 282), (235, 315), (365, 282), (365, 315),
                  (455, 284), (455, 317), (550, 286), (550, 320))
    else:
        box = (220, 60, 340, 360)
        points = ((260, 100), (300, 100), (264, 220), (296, 220),
                  (265, 280), (295, 280), (266, 345), (294, 345))
    names = [f'{side}_{joint}' for joint in ('shoulder', 'hip', 'knee', 'ankle')
             for side in ('left', 'right')]
    return PersonPose(.8, tuple(v / (640 if i % 2 == 0 else 400)
                                for i, v in enumerate(box)), tuple(
        PoseKeypoint(name, x / 640, y / 400, .9)
        for name, (x, y) in zip(names, points)), 8)


class OfflineCloud(OllamaCloudFallProvider):
    """Use production payload/parser; replace the network boundary only."""

    def __init__(self, mode='fall'):
        super().__init__(model='gemma4:31b', api_key='offline-test-only')
        self.mode = mode
        self.requests = []
        self.payloads = []
        self.canceled = 0

    async def analyze(self, request):
        self.requests.append(request)
        return await super().analyze(request)

    async def _post(self, body):
        payload = json.loads(body)
        self.payloads.append(payload)
        if self.mode == 'waiting':
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.canceled += 1
                raise
        if self.mode == 'invalid':
            content = 'not a JSON reply'
        elif self.requests[-1].purpose == 'crosscheck':
            content = json.dumps(dict(
                assessment='suspected_fall', explanation='테스트용 의심 결과',
                findings=[dict(assessment='suspected_fall', kind='already_down',
                               regions=[])]))
        else:
            content = json.dumps(dict(assessment='observed_fall', explanation='테스트용 낙상 결과'))
        return json.dumps(dict(done=True, done_reason='stop',
                               message=dict(role='assistant', content=content))).encode()


class PcFlow:
    """Actual DDS graph with a test server/camera/Agent and production relay."""

    def __init__(self, tmp_path, *, mode='fall', lying=True):
        self.scope = uuid4().hex
        self.ids = {k: f'{k}-{self.scope}' for k in ('manager', 'bridge', 'vlm')}
        args = ['--ros-args']
        for topic in TOPICS:
            args.extend(['-r', f'{topic}:=/fall_pc_check_{self.scope}{topic}'])
        args.extend(['-p', f'manager_runtime_id:={self.ids["manager"]}',
                     '-p', f'runtime_id:={self.ids["vlm"]}'])
        rclpy.init(args=args)
        self.nodes = []
        self.task = None
        self.journal = None
        self.link = None
        self.manager_running = True
        try:
            config_path = Path(__file__).parents[1] / 'config/fall_runtime.example.json'
            config = json.loads(config_path.read_text())
            self.db = tmp_path / 'private' / 'fall.sqlite'
            config.update(device_id=f'pc-{self.scope}', journal_path=str(self.db),
                          cloud_key_file=str(tmp_path / 'DO_NOT_READ.key'))
            self.settings = FallNodeSettings.parse(json.dumps(config))
            self.provider = OfflineCloud(mode)
            self.journal = SqliteFallJournal(self.db, device_id=self.settings.device_id)
            self.vlm = create_fall_node(self.settings, provider=self.provider, journal=self.journal)
            self.nodes.append(self.vlm)
            self.manager = Node('fall_pc_manager')
            self.nodes.append(self.manager)
            self.link = FallSettingsLink(self.manager, manager_id=self.ids['manager'],
                                         bridge_id=self.ids['bridge'], vlm_id=self.ids['vlm'])
            self.source = Node('fall_pc_inputs')
            self.nodes.append(self.source)
            latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                                 reliability=ReliabilityPolicy.RELIABLE)
            self.snapshots = self.source.create_publisher(
                FallSettingsSnapshot, '/malbut/falls/settings/snapshot', latched)
            self.images = self.source.create_publisher(
                Image, '/depth_cam/rgb0/image_raw', qos_profile_sensor_data)
            self.poses = self.source.create_publisher(
                String, '/homecam/person_poses', qos_profile_sensor_data)
            self.candidates = self.source.create_publisher(String, '/homecam/fall_candidates', 10)
            self.decisions = self.source.create_publisher(
                String, '/malbut/falls/runtime/decision', 10)
            self.statuses, self.reports, self.events = [], [], []
            self.pose_control = FallPoseControl(self.ids['vlm'])
            self.source.create_subscription(
                FallRuntimeStatus, '/malbut/falls/status', self.on_status, 10)
            self.source.create_subscription(
                FallSettingsReport, '/malbut/falls/settings/report', self.reports.append, latched)
            self.source.create_subscription(
                String, '/malbut/falls/runtime/events',
                lambda msg: self.events.append(json.loads(msg.data)), 50)
            self.tracker = PersonPoseTracker()
            self.detector = FallCandidateDetector()
            self.server_enabled = False
            self.flags = dict(enabled=True, camera_enabled=True, cloud_consent=True)
            self.revision = self.sequence = 0
            self.last_server = self.last_camera = -float('inf')
            self.lying = lying
            self.frames = self.pose_frames = 0
            self.candidate_messages = []
            self.task = asyncio.create_task(spin_runtime(self.vlm))
        except BaseException:
            self.cleanup()
            raise

    def on_status(self, message):
        self.statuses.append(message)
        self.pose_control.receive(message)

    def snapshot(self):
        now = time.monotonic()
        self.sequence += 1
        self.last_server = now
        self.snapshots.publish(FallSettingsSnapshot(
            bridge_runtime_id=self.ids['bridge'], sequence=self.sequence, observed_at=now,
            check_state='confirmed', settings_revision=self.revision,
            server_checked_at=now, reason_code='none', **self.flags))

    def change_settings(self, **changes):
        self.flags.update(changes)
        self.revision += 1
        self.server_enabled = True
        self.snapshot()

    def publish_camera(self):
        now = time.monotonic()
        self.last_camera = now
        stamp = self.source.get_clock().now().to_msg()
        capture = stamp.sec + stamp.nanosec / 1e9
        pixels = np.full((400, 640, 3), self.frames % 256, dtype=np.uint8)
        message = Image(height=400, width=640, encoding='rgb8', step=640 * 3,
                        data=pixels.tobytes())
        message.header.stamp, message.header.frame_id = stamp, 'pc_test_rgb'
        self.images.publish(message)
        self.frames += 1
        if not self.pose_control.active():
            self.tracker.reset()
            self.detector.reset()
            return
        self.pose_frames += 1
        tracking = self.tracker.update((pose_fixture(self.lying),), now)
        self.poses.publish(String(data=json.dumps(dict(
            schemaVersion=1, status='ok', frameId='pc_test_rgb',
            captureStamp=dict(sec=stamp.sec, nanosec=stamp.nanosec),
            persons=[dict(observed=t.pose is not None, pose=t.pose.as_dict() if t.pose else None,
                          confidenceLevel=t.confidence_level) for t in tracking.tracks],
            unassigned=[]))))
        result = self.detector.update(tracking, capture_time=capture,
                                      image_size=(640, 400), robot_motion='stationary')
        result['frameId'] = 'pc_test_rgb'
        if result['candidates']:
            self.candidate_messages.append(result)
        self.candidates.publish(String(data=json.dumps(result)))

    async def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.task.done():
                self.task.result()
                raise AssertionError('VLM worker unexpectedly stopped')
            now = time.monotonic()
            if self.server_enabled and now - self.last_server >= .5:
                self.snapshot()
            if now - self.last_camera >= .22:
                self.publish_camera()
            if self.manager_running:
                rclpy.spin_once(self.manager, timeout_sec=0)
            rclpy.spin_once(self.source, timeout_sec=0)
            await asyncio.sleep(.005)

    async def until(self, predicate, *, timeout=10):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                latest = self.statuses[-1] if self.statuses else None
                raise AssertionError(f'condition timed out; latest status={latest}')
            await self.pump(.025)

    async def start(self):
        await self.until(lambda: bool(self.statuses))
        assert self.statuses[-1].runtime_state == 'waiting_settings'
        assert self.vlm.monitor.buffer.stored_bytes == 0
        assert not self.provider.requests and self.pose_frames == 0
        self.change_settings()
        await self.until(lambda: bool(self.reports) and self.reports[-1].applied
                         and self.pose_control.active())
        assert self.reports[-1].requested_revision == self.revision
        assert self.reports[-1].runtime_id == self.ids['vlm']

    def stored(self):
        with sqlite3.connect(self.db) as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM incident_events ORDER BY sequence')]

    def confirm(self, question, *, situation_assessment, help_needed):
        self.decisions.publish(String(data=json.dumps(dict(
            action='confirmation_result', boot_id=question['boot_id'],
            incident_id=question['incident_id'], question_id=question['question_id'],
            subject_key=question['subject_key'], evidence_revision=question['evidence_revision'],
            situation_assessment=situation_assessment, help_needed=help_needed))))

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.cleanup()

    def cleanup(self):
        if self.link is not None:
            self.link.close()
        for node in reversed(self.nodes):
            node.destroy_node()
        if self.journal is not None:
            self.journal.close()
        if rclpy.ok():
            rclpy.shutdown()


@pytest.mark.parametrize('situation_assessment,help_needed', [
    ('confirmed_incident', False), ('unknown', True),
])
def test_manager_pose_cloud_confirmation_and_record(tmp_path, situation_assessment, help_needed):
    async def run():
        flow = PcFlow(tmp_path)
        try:
            await flow.start()
            await flow.until(lambda: any(e['kind'] == 'analysis_completed' for e in flow.events))
            assert len(flow.provider.requests) == 1
            request = flow.provider.requests[0]
            assert request.purpose == 'incident' and request.subject_key
            assert 1 <= len(request.window.frames) <= 12
            assert request.window.requested_end - request.window.requested_start == 5
            assert flow.candidate_messages
            question = next(e for e in flow.events if e['kind'] == 'question_requested')
            kinds = [e['kind'] for e in flow.events]
            assert kinds.index('analysis_completed') < kinds.index('question_requested')
            flow.confirm(question, situation_assessment=situation_assessment,
                         help_needed=help_needed)
            await flow.until(lambda: any(
                e['kind'] == 'confirmation_completed' for e in flow.events))
            await flow.pump(.8)
            records = flow.stored()
            assert records[-1]['fallSeen']
            assert records[-1]['state'] == ('help_required' if help_needed else 'resolved')
            reason = 'confirmation_help_required' if help_needed else 'response_completed'
            kind = 'notification_requested' if help_needed else 'incident_resolved'
            assert any(e['reason'] == reason and e['kind'] == kind
                       for e in flow.events)
            assert len(flow.reports) == 1  # Polling the same settings does not reapply.
            assert len(flow.provider.requests) == 1  # Repeated posture does not create calls.
            assert len({r['incidentId'] for r in records}) == 1
            assert 'jpeg' not in json.dumps(records)
            assert all('explanation' not in r for r in records)
            assert flow.db.stat().st_mode & 0o777 == 0o600
        finally:
            await flow.close()
        reopened = SqliteFallJournal(flow.db, device_id=flow.settings.device_id)
        try:
            assert reopened.pending()['payload']
            assert flow.stored()[-1]['fallSeen']
            assert bool(reopened.unresolved()) is help_needed
        finally:
            reopened.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode,code', [
    ('invalid', 'cloud_invalid_response'), ('waiting', 'cloud_timeout'),
])
def test_cloud_failure_is_recorded_without_normal_closure(tmp_path, mode, code):
    async def run():
        flow = PcFlow(tmp_path, mode=mode)
        try:
            await flow.start()
            await flow.until(lambda: any(s.analysis_state == 'failed' for s in flow.statuses),
                             timeout=27)
            assert flow.statuses[-1].last_error_code == code
            assert len(flow.provider.requests) == 1
            records = flow.stored()
            assert any(r['eventKind'] == 'analysis_unavailable' and r['reason'] == code
                       for r in records)
            assert all(r['state'] != 'resolved' and r['assessment'] != 'normal_activity'
                       for r in records)
        finally:
            await flow.close()
    asyncio.run(run())


def test_help_confirmation_is_processed_while_recheck_is_waiting(tmp_path):
    async def run():
        flow = PcFlow(tmp_path)
        try:
            await flow.start()
            await flow.until(lambda: any(e['kind'] == 'question_requested' for e in flow.events))
            question = next(e for e in flow.events if e['kind'] == 'question_requested')
            flow.provider.mode = 'waiting'
            flow.decisions.publish(String(data=json.dumps(dict(
                action='recheck', incident_id=question['incident_id'],
                evidence_revision=question['evidence_revision']))))
            await flow.until(lambda: len(flow.provider.payloads) == 2)
            flow.confirm(question, situation_assessment='unknown', help_needed=True)
            await flow.until(lambda: any(
                e['reason'] == 'confirmation_help_required' for e in flow.events))
            notification = next(e for e in flow.events
                                if e['reason'] == 'confirmation_help_required')
            assert notification['notification_level'] == 'urgent'
            assert flow.vlm.monitor.analysis_status.state == 'waiting_response'
            assert flow.stored()[-1]['state'] == 'help_required'
        finally:
            await flow.close()
    asyncio.run(run())


def test_recheck_messages_allow_only_two_additional_calls(tmp_path):
    async def run():
        flow = PcFlow(tmp_path, mode='invalid')
        try:
            await flow.start()
            await flow.until(lambda: any(e['kind'] == 'analysis_unavailable' for e in flow.events))
            incident = next(e for e in flow.events if e['kind'] == 'incident_opened')
            decision = String(data=json.dumps(dict(
                action='recheck', incident_id=incident['incident_id'],
                evidence_revision=incident['evidence_revision'])))
            for count in (2, 3):
                flow.decisions.publish(decision)
                await flow.until(lambda: sum(
                    e['kind'] == 'analysis_unavailable' for e in flow.events) == count)
            flow.decisions.publish(decision)
            await flow.until(lambda: any(e['reason'] == 'recheck_limit' for e in flow.events))
            await flow.pump(.5)
            assert len(flow.provider.requests) == 3
            assert len({r.incident_id for r in flow.provider.requests}) == 1
            assert len({r.request_id for r in flow.provider.requests}) == 3
            assert all(r['state'] != 'resolved' for r in flow.stored())
        finally:
            await flow.close()
    asyncio.run(run())


@pytest.mark.parametrize('cause', ['consent', 'camera', 'manager', 'server'])
def test_inflight_stop_and_local_pose_permissions(tmp_path, cause):
    async def run():
        flow = PcFlow(tmp_path, mode='waiting')
        try:
            await flow.start()
            await flow.until(lambda: bool(flow.provider.payloads))
            if cause == 'consent':
                flow.change_settings(cloud_consent=False)
            elif cause == 'camera':
                flow.change_settings(camera_enabled=False)
            elif cause == 'manager':
                flow.manager_running = False
                flow.link.close()
            else:
                flow.server_enabled = False
            await flow.until(lambda: any(s.analysis_state == 'canceled' for s in flow.statuses),
                             timeout=19)
            active = cause in {'consent', 'server'}
            assert flow.statuses[-1].accepting_images is active
            assert flow.pose_control.active() is active
            assert flow.provider.canceled == 1
            if cause == 'server':
                assert flow.vlm.control.cloud_block_reason == 'server_settings_stale'
            before = flow.pose_frames
            await flow.pump(.7)
            assert (flow.pose_frames > before) is active
            assert (flow.vlm.monitor.buffer.stored_bytes > 0) is active
            assert len(flow.provider.requests) == 1
            assert all(r['state'] != 'resolved' for r in flow.stored())
        finally:
            await flow.close()
    asyncio.run(run())


def test_periodic_cloud_check_without_pose_candidate(tmp_path):
    async def run():
        flow = PcFlow(tmp_path, lying=False)
        try:
            await flow.start()
            await flow.until(lambda: bool(flow.journal.discoveries()), timeout=67)
            assert not flow.candidate_messages
            assert len(flow.provider.requests) == 1
            request = flow.provider.requests[0]
            assert request.purpose == 'crosscheck' and request.incident_id is None
            assert len(request.window.frames) == 12
            assert request.window.requested_end - request.window.requested_start == 5
            images = flow.provider.payloads[0]['messages'][1]['images']
            assert len(images) == 12
            for encoded in images:
                with PilImage.open(io.BytesIO(base64.b64decode(encoded))) as image:
                    assert image.size == (640, 400)
            discovery = flow.journal.discoveries()[0]
            assert discovery['assessment'] == 'suspected_fall'
            assert discovery['association_status'] == 'unidentified'
            assert discovery['incident_id'] is None
            assert not flow.stored()  # No invented subject/incident.
        finally:
            await flow.close()
    asyncio.run(run())
