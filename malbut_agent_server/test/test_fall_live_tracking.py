"""Received-frame controller and subprocess protocol, without Cloud/model calls."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from malbut_agent_server.adapters.outbound.sam_tracking import (
    SamTrackingSettings, SamTrackingWorker,
)
from malbut_agent_server.application.fall_live_tracking import LiveDiscoveryTracking
from malbut_agent_server.domain.fall_monitoring import SubjectFrame
from malbut_agent_server.fall_runtime import FallNodeSettings
from malbut_agent_server.ports.fall_event_journal import FallJournalError
from test_cloud_fall_monitor import candidate, frame
from test_fall_cloud_association import BOX, feed, pose
from test_fall_deferred_association import setup
from test_fall_runtime import config


class Tracker:
    def __init__(self, *, box=BOX, error=None, hold=False):
        self.box, self.error, self.hold = box, error, hold
        self.frames = []
        self.closed = False
        self.started = asyncio.Event()

    async def step(self, frame):
        self.frames.append(frame)
        self.started.set()
        if self.hold:
            await asyncio.Event().wait()
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return self.box

    async def close(self):
        self.closed = True


def controller(monitor, clock, tracker):
    calls, reasons = [], []

    def factory(**seed):
        calls.append(seed)
        return tracker
    return (LiveDiscoveryTracking(monitor, factory, clock=clock, report=reasons.append),
            calls, reasons)


@pytest.mark.parametrize('existing', [False, True])
def test_received_frames_link_without_second_cloud_call_or_new_scene_question(existing):
    m, c, p, discoveries, _ = setup()
    for t in (160.25, 160.5, 160.75):
        feed(m, c, t)
    iid = m.candidate(candidate(c())) if existing else None
    tracker = Tracker()
    live, calls, reasons = controller(m, c, tracker)

    async def run():
        live.offer(discoveries[0])
        await asyncio.wait_for(live.task, 1)
        live.maintain(accepting_images=True)
        await live.close()
    asyncio.run(run())
    links = [e for e in m.drain_events() if e.kind == 'cloud_discovery_linked']
    assert len(links) == 1
    assert iid is None or links[0].incident_id == iid
    assert links[0].subject_key == 'person-1'
    assert len(p.calls) == 1
    assert [f.captured_at for f in tracker.frames] == [159.5, 160, 160.25, 160.5, 160.75]
    assert calls == [dict(seed_time=159.5, seed_box=BOX)]
    assert tracker.closed and reasons == ['discovery_tracking_matched_after_tracking']


@pytest.mark.parametrize('action', ['camera_off', 'consent_off', 'closed'])
def test_cancel_stops_backend_and_does_not_apply_delayed_result(action):
    m, c, _, ds, _ = setup()
    tracker = Tracker(hold=True)
    live, _, _ = controller(m, c, tracker)

    async def run():
        live.offer(ds[0])
        await tracker.started.wait()
        if action == 'closed':
            await live.close()
        else:
            m.configure(enabled=True, camera_enabled=action != 'camera_off',
                        cloud_consent=False, connected=True)
            for _ in range(3):
                live.maintain(accepting_images=action != 'camera_off')
            await live.close()
        assert live.task.done()
    asyncio.run(run())
    assert tracker.closed
    assert not any(e.kind == 'cloud_discovery_linked' for e in m.drain_events())


def test_cancel_before_task_starts_and_busy_discovery():
    m, c, _, ds, _ = setup()
    live, calls, reasons = controller(m, c, Tracker())
    async def run():
        live.offer(ds[0])
        sid = live.session_id
        live.offer(ds[0])
        assert reasons == ['discovery_tracking_busy']
        await live.close()
        assert m.tracking_session_reason(sid) == 'visual_track_broken'
        live.maintain(accepting_images=False)
    asyncio.run(run())
    assert not calls


@pytest.mark.parametrize('mode,expected', [
    ('missing_mask', 'visual_track_broken'), ('backend', 'backend_failed'),
    ('gap', 'visual_track_broken'), ('evicted', 'seed_unavailable'),
])
def test_failures_never_invent_identity(mode, expected):
    m, c, _, ds, _ = setup()
    tracker = Tracker(box=None if mode == 'missing_mask' else BOX,
                      error=RuntimeError('private text') if mode == 'backend' else None)
    live, calls, reasons = controller(m, c, tracker)
    if mode == 'gap':
        feed(m, c, 161)
    elif mode == 'evicted':
        c.value = 191
    async def run():
        live.offer(ds[0])
        if live.task:
            await asyncio.wait_for(live.task, 1)
        await live.close()
    asyncio.run(run())
    assert reasons[-1] == 'discovery_tracking_' + expected
    assert not any(e.kind == 'cloud_discovery_linked' for e in m.drain_events())
    if mode == 'evicted':
        assert not calls
        with pytest.raises(ValueError):
            m.begin_discovery_tracking(ds[0].discovery_id)


def test_ambiguous_pose_is_not_chosen_then_session_closes():
    m, c, _, ds, _ = setup()
    for t in (160.25, 160.5, 160.75):
        feed(m, c, t, (pose(), pose('other')))
    tracker = Tracker()
    live, _, _ = controller(m, c, tracker)
    async def run():
        live.offer(ds[0])
        for _ in range(20):
            await asyncio.sleep(.005)
            if len(tracker.frames) == 5:
                break
        await live.close()
    asyncio.run(run())
    assert len(tracker.frames) == 5
    assert not any(e.kind == 'cloud_discovery_linked' for e in m.drain_events())


def test_waits_for_pose_callback_but_not_for_future_images():
    m, c, _, ds, _ = setup()
    tracker = Tracker()
    live, _, _ = controller(m, c, tracker)
    async def run():
        live.offer(ds[0])
        for t in (160.25, 160.5, 160.75):
            c.value = t
            m.ingest_rgb(frame(t))
            await asyncio.sleep(.02)
            m.ingest_subject_frame(SubjectFrame(t, (pose(),), .5))
            await asyncio.sleep(.02)
        await asyncio.wait_for(live.task, 1)
        await live.close()
    asyncio.run(run())
    assert len(tracker.frames) == 5
    assert len([e for e in m.drain_events() if e.kind == 'cloud_discovery_linked']) == 1


def test_persistence_error_is_not_hidden_as_backend_failure(monkeypatch):
    m, c, _, ds, _ = setup()
    live, _, _ = controller(m, c, Tracker())
    def fail(*args, **kwargs):
        raise FallJournalError('storage failed')
    monkeypatch.setattr(m, 'ingest_discovery_track', fail)
    async def run():
        live.offer(ds[0])
        await asyncio.sleep(.02)
        with pytest.raises(FallJournalError):
            live.maintain(accepting_images=True)
        with pytest.raises(FallJournalError):
            live.offer(ds[0])
        with pytest.raises(FallJournalError):
            await live.close()
    asyncio.run(run())


def tracking_config():
    return dict(python_executable='/opt/fall/bin/python', source_path='/opt/sam2',
                checkpoint_path='/opt/sam2.1_hiera_tiny.pt')


def test_tracking_is_explicit_and_config_validation_has_no_import_or_file_effects(tmp_path):
    data = config(tmp_path)
    assert FallNodeSettings.parse(json.dumps(data)).tracking is None
    data['tracking'] = tracking_config()
    parsed = FallNodeSettings.parse(json.dumps(data))
    assert parsed.tracking.source_path == '/opt/sam2'
    assert parsed.tracking.python_paths == ()
    data['tracking'] = None
    assert FallNodeSettings.parse(json.dumps(data)).tracking is None


@pytest.mark.parametrize('change', [
    {'source_path': 'relative'}, {'python_executable': ''},
    {'python_paths': 'bad'}, {'python_paths': ['relative']},
    {'extra': True}, {'checkpoint_path': '/tmp/a:b'},
])
def test_bad_tracking_configuration_is_rejected(change):
    with pytest.raises(ValueError):
        SamTrackingSettings.parse(tracking_config() | change)


class Process:
    def __init__(self, lines):
        self.stdout = SimpleNamespace(readline=AsyncMock(side_effect=lines))
        self.sent = []
        self.stdin = SimpleNamespace(write=self.sent.append, drain=AsyncMock())
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def test_worker_protocol_excludes_secrets_and_checks_exact_capture(monkeypatch):
    process = Process([b'{"ready":true}\n', b'{"captured_at":10,"box":[0.1,0.4,0.7,0.9]}\n'])
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setenv('OLLAMA_API_KEY', 'DO_NOT_PASS')
    monkeypatch.setenv('PYTHONPATH', '/untrusted/inherited/path')
    settings = SamTrackingSettings.parse(tracking_config())
    worker = SamTrackingWorker(settings, seed_time=10, seed_box=BOX)
    async def run():
        assert await worker.step(frame(10)) == BOX
        await worker.close()
    asyncio.run(run())
    env = launch.call_args.kwargs['env']
    assert 'OLLAMA_API_KEY' not in env
    assert '/untrusted' not in env['PYTHONPATH']
    sent = json.loads(process.sent[0])
    assert set(sent) == {'captured_at', 'jpeg', 'seed_box'}
    assert process.killed


@pytest.mark.parametrize('reply', [
    b'not JSON', b'{}', b'{"captured_at":11,"box":null}',
    b'{"captured_at":10,"box":[0,0,2,1]}',
])
def test_bad_worker_reply_cannot_supply_track(monkeypatch, reply):
    process = Process([b'{"ready":true}\n', reply])
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    worker = SamTrackingWorker(SamTrackingSettings.parse(tracking_config()),
                               seed_time=10, seed_box=BOX)
    async def run():
        try:
            with pytest.raises(ValueError):
                await worker.step(frame(10))
        finally:
            await worker.close()
    asyncio.run(run())
    assert process.killed


def test_tracking_buffer_requires_exact_retained_history():
    m, c, _, _, _ = setup()
    assert m.buffer.tracking_frame(159.5, now=160, first=True).captured_at == 159.5
    assert m.buffer.tracking_frame(159.5, now=160).captured_at == 160
    assert m.buffer.tracking_frame(160, now=160) is None
    with pytest.raises(ValueError):
        m.buffer.tracking_frame(159.6, now=160, first=True)
    with pytest.raises(ValueError):
        m.buffer.tracking_frame(159.5, now=191)


def test_cancellation_during_spawn_reaps_new_child(monkeypatch):
    process = Process([])
    worker = SamTrackingWorker(SamTrackingSettings.parse(tracking_config()),
                               seed_time=10, seed_box=BOX)
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def launch(*args, **kwargs):
            started.set()
            await release.wait()
            return process
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
        task = asyncio.create_task(worker.step(frame(10)))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.killed and worker.closed
    asyncio.run(run())


@pytest.mark.parametrize('ready', [b'', b'wrong protocol'])
def test_unavailable_worker_is_closed(monkeypatch, ready):
    process = Process([ready])
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    worker = SamTrackingWorker(SamTrackingSettings.parse(tracking_config()),
                               seed_time=10, seed_box=BOX)
    async def run():
        try:
            with pytest.raises(ValueError):
                await worker.step(frame(10))
        finally:
            await worker.close()
        assert process.killed
    asyncio.run(run())
