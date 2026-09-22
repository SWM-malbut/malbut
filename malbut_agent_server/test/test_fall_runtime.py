"""Runtime control/ROS wiring with no external ROS graph or Cloud call."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_agent_server.application.fall_detector_input import FallDetectorInput
from malbut_agent_server.domain.fall_monitoring import (
    CloudFallReply, FallRuntimeEvent, VideoAssessment,
)
from malbut_agent_server.fall_runtime import (
    FallNodeSettings, FallRuntimeControl, apply_decision, event_metadata, parse_agent_reply,
    parse_subject_observation,
)
from malbut_agent_server.ros_fall_monitor import create_fall_node, main
from test_cloud_fall_monitor import make, candidate, enable, frame


def config(tmp_path):
    # Test-only values for still-unagreed settings, never production defaults.
    return dict(device_id='robot', journal_path=str(tmp_path / 'private' / 'events.sqlite'),
                cloud_key_file=str(tmp_path / 'not-read.key'), model='gemma4:31b',
                image_topic='/camera/color/image_raw', retention_s=30, buffer_bytes=10**7,
                buffer_frames=100, input_fps=5, max_source_age_s=1, control_lease_s=5,
                policy=dict(retry_interval_s=3, max_person_observation_age_s=2,
                            clip_window_s=10, max_frame_age_s=2, max_calls_per_minute=5,
                            max_incidents=10, max_images=12))


@pytest.mark.parametrize('purpose', ['incident', 'crosscheck'])
def test_example_sends_twelve_frames_from_latest_five_seconds_in_one_call(purpose):
    example = json.loads((Path(__file__).parents[1]
                          / 'config/fall_runtime.example.json').read_text())
    video = example['policy']
    assert (video['clip_window_s'], video['max_images']) == (5, 12)

    async def run():
        monitor, clock, provider = make(clip_window_s=video['clip_window_s'],
                                        max_images=video['max_images'])
        enable(monitor)
        clock.value = 160
        # Keep ten seconds in the buffer; only the latest five may be sent.
        for index in range(41):
            monitor.ingest_rgb(frame(150 + index / 4))
        if purpose == 'incident':
            monitor.candidate(candidate(clock()))
        assert await monitor.run_once()
        assert len(provider.calls) == 1
        request = provider.calls[0]
        assert request.purpose == purpose
        stamps = [item.captured_at for item in request.window.frames]
        assert len(stamps) == 12
        assert stamps == sorted(set(stamps))
        assert stamps[0] == 155 and stamps[-1] == 160
    asyncio.run(run())


def control():
    monitor, clock, provider = make()
    bridge = FallDetectorInput(monitor, max_source_age_s=2)
    ctl = FallRuntimeControl(bridge, lease_s=5, clock=clock)
    return ctl, monitor, clock, provider


def settings(ctl, **changes):
    return json.dumps(dict(runtimeId=ctl.runtime_id, revision=1, enabled=True,
                           cloudConsent=True, connected=True) | changes)


def test_control_requires_media_permission_and_current_lease():
    ctl, monitor, clock, _ = control()
    assert not ctl.accepting_images
    ctl.settings(settings(ctl))
    assert not ctl.accepting_images
    ctl.media_permission(True)
    assert ctl.accepting_images
    clock.value += 5
    ctl.refresh()
    assert not ctl.accepting_images and monitor.buffer.stored_bytes == 0
    ctl.settings(settings(ctl))
    assert ctl.accepting_images
    ctl.media_permission(False)
    assert not ctl.accepting_images


def test_control_rejects_previous_process_revision_conflicts_and_fake_agent_no_response():
    ctl, _, _, _ = control()
    with pytest.raises(ValueError):
        ctl.settings(settings(ctl, runtimeId='past-process'))
    ctl.settings(settings(ctl, revision=2))
    for update in (settings(ctl), settings(ctl, revision=2, cloudConsent=False)):
        with pytest.raises(ValueError):
            ctl.settings(update)
    reply = dict(incident_id='i', question_id='q', subject_key='s', evidence_revision=1,
                 answer='no_response', question_played=False)
    with pytest.raises(ValueError):
        parse_agent_reply(json.dumps(reply))
    reply['question_played'] = True
    assert parse_agent_reply(json.dumps(reply)).question_played


def test_consent_revocation_cancels_call_without_stopping_pose_monitoring():
    async def run():
        ctl, monitor, _, provider = control()
        ctl.media_permission(True)
        ctl.settings(settings(ctl))
        from test_cloud_fall_monitor import frame
        monitor.ingest_rgb(frame(100))
        monitor.candidate(candidate())
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        ctl.settings(settings(ctl, revision=2, cloudConsent=False))
        await task
        assert ctl.accepting_images
        assert 'cloud_permission_changed' in [e.reason for e in monitor.drain_events()]
    asyncio.run(run())


def test_configuration_checks_do_not_read_token_open_journal_or_import_ros(tmp_path, capsys):
    path = tmp_path / 'runtime.json'
    path.write_text(json.dumps(config(tmp_path)))
    assert main(['--config', str(path)]) == 0
    assert 'no Cloud call' in capsys.readouterr().out
    assert not (tmp_path / 'private').exists()
    assert not (tmp_path / 'not-read.key').exists()
    broken = config(tmp_path)
    broken['policy']['max_images'] = None
    path.write_text(json.dumps(broken))
    assert main(['--config', str(path)]) == 2


@pytest.mark.parametrize('field,value', [
    ('model', 'gemma4:31b-cloud'), ('retention_s', 5), ('input_fps', 0),
    ('buffer_bytes', True), ('journal_path', 'relative'),
    ('control_lease_s', 10), ('control_lease_s', None),
])
def test_runtime_config_rejects_bad_or_unagreed_values(tmp_path, field, value):
    data = config(tmp_path)
    data[field] = value
    with pytest.raises((ValueError, TypeError)):
        FallNodeSettings.parse(json.dumps(data))


def test_event_handoff_excludes_pixels_and_untrusted_model_text():
    event = FallRuntimeEvent('event', 'crosscheck_completed', reply=CloudFallReply(
        VideoAssessment.SUSPECTED_FALL, 'untrusted instructions'))
    encoded = json.dumps(event_metadata(event))
    assert 'suspected_fall' in encoded and 'untrusted instructions' not in encoded


@pytest.mark.parametrize('change', [
    {'evidence_revision': True}, {'evidence_revision': 0}, {'association_verified': 1},
    {'observed_at': -1}, {'state': 'not_seen'}, {'request_id': ''}, {'extra': 'field'},
])
def test_subject_observation_contract_rejects_invalid_fields(change):
    data = dict(incident_id='i', subject_key='p', evidence_revision=1, request_id='request',
                observed_at=100, state='clear', association_verified=True)
    assert parse_subject_observation(json.dumps(data)).association_verified
    with pytest.raises(ValueError):
        parse_subject_observation(json.dumps(data | change))


def test_explicit_decisions_preserve_revision_and_closure_guards():
    ctl, monitor, _, _ = control()
    ctl.media_permission(True)
    ctl.settings(settings(ctl))
    iid = monitor.candidate(candidate())
    command = dict(incident_id=iid, evidence_revision=1, action='recheck')
    assert apply_decision(monitor, json.dumps(command))
    command['evidence_revision'] = 2
    with pytest.raises(ValueError, match='stale'):
        apply_decision(monitor, json.dumps(command))
    command.update(evidence_revision=1, action='resolve', reason='normal_verified')
    with pytest.raises(ValueError):
        apply_decision(monitor, json.dumps(command))
    command['action'] = 'drive_to_person'
    with pytest.raises(ValueError):
        apply_decision(monitor, json.dumps(command))


def test_real_ros_callbacks_on_fake_node_keep_images_independent_of_pose(tmp_path, monkeypatch):
    node_module = pytest.importorskip('rclpy.node')
    sensor = pytest.importorskip('sensor_msgs.msg')
    std = pytest.importorskip('std_msgs.msg')
    from malbut_interfaces.msg import FallControlHeartbeat
    from malbut_interfaces.srv import ApplyFallSettings
    from test_fall_control import settings as control_settings, heartbeat
    import numpy as np

    class Node:
        def __init__(self, name):
            self.subscriptions = {}
            self.publishers = {}
            self.services = {}
            self.timers = []

        def declare_parameter(self, name, default, descriptor):
            assert name == 'manager_runtime_id' and default == '' and descriptor.read_only
            return SimpleNamespace(value='manager-1')

        def create_service(self, srv, name, callback):
            self.services[name] = callback

        def create_subscription(self, msg, topic, callback, qos):
            self.subscriptions[topic] = callback

        def create_publisher(self, msg, topic, qos):
            publisher = Mock()
            self.publishers[topic] = publisher
            return publisher

        def create_timer(self, *args, **kwargs):
            from rclpy.clock import ClockType
            assert kwargs['clock'].clock_type == ClockType.STEADY_TIME
            self.timers.append(args)

        def get_clock(self):
            return SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=10**12))

        def get_logger(self):
            return Mock()

    monkeypatch.setattr(node_module, 'Node', Node)
    _, clock, provider = make()
    node = create_fall_node(FallNodeSettings.parse(json.dumps(config(tmp_path))),
                            provider=provider, journal=None, clock=clock)
    image = sensor.Image()
    image.header.stamp.sec = 1000
    image.header.frame_id = 'rgb'
    image.height, image.width, image.step = 400, 640, 640 * 3
    image.encoding = 'bgr8'
    image.data = np.zeros((400, 640, 3), dtype=np.uint8).tobytes()
    node.on_image(image)
    assert node.monitor.buffer.stored_bytes == 0
    node.publish_status()
    assert node.publishers['/malbut/falls/status'].publish.call_args.args[0].last_frame_age_s == -1
    apply = node.services['/malbut/falls/settings/apply']
    result = apply(ApplyFallSettings.Request(**control_settings(node.control)),
                   ApplyFallSettings.Response())
    assert result.applied and result.reason_code == 'applied'
    node.subscriptions['/malbut/falls/control/heartbeat'](
        FallControlHeartbeat(**heartbeat(node.control, clock)))
    assert '/homecam/monitoring_enabled' not in node.subscriptions
    assert '/malbut/falls/runtime/settings' not in node.subscriptions
    node.on_image(image)
    assert node.monitor.buffer.stored_bytes > 0
    assert not provider.calls  # No network before the configured scheduling gate.
    node.publish_status()
    status = node.publishers['/malbut/falls/status'].publish.call_args.args[0]
    assert status.accepting_images and status.last_frame_age_s == 0
    assert status.analysis_state == 'idle' and status.request_id == ''
    assert status.sequence == 2 and node.timers[0][0] == 1.0
    assert '/cmd_vel' not in node.publishers
    assert '/malbut/speech/response' not in node.publishers
    subject_callback = node.subscriptions['/malbut/falls/runtime/subject_observation']
    subject_callback(std.String(data=json.dumps(dict(
        incident_id='absent', subject_key='person', evidence_revision=1, request_id='request',
        observed_at=100, state='clear', association_verified=True))))
    clock.value += 1
    image.width = 1
    node.on_image(image)
    node.publish_status()
    status = node.publishers['/malbut/falls/status'].publish.call_args.args[0]
    assert status.last_frame_age_s == 1  # Rejected RGB does not refresh this field.
    result = apply(ApplyFallSettings.Request(**control_settings(
        node.control, settings_revision=2, camera_enabled=False)), ApplyFallSettings.Response())
    assert result.applied and not result.camera_enabled
    assert node.monitor.buffer.stored_bytes == 0
