"""Validate actual Humble speech transports in an isolated, non-actuating graph."""

import importlib.util
from pathlib import Path
from threading import RLock
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def speech_graph(tmp_path, monkeypatch):
    pytest.importorskip('rclpy')
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechRequest, SpeechTranscript
    from malbut_interfaces.srv import ClassifySpeechAddressee, ControlSpeechPlayback
    from malbut_system_manager.speech_coordinator import SpeechCoordinator

    # Import the shipped code, not a copied test implementation.
    source = Path(__file__).with_name('test_guided_smoke.py')
    spec = importlib.util.spec_from_file_location('guided_smoke_loader', source)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    module = loader.smoke.__wrapped__()
    monkeypatch.setenv('ROS_DOMAIN_ID', '181')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    robot = module.RosRobot()
    worker = Node('smoke_fake_tts')
    manager = Node('smoke_fake_manager')
    agent = Node('smoke_fake_agent')
    executor = SingleThreadedExecutor()
    for node in (robot.node, worker, manager, agent):
        executor.add_node(node)
    robot.spin = lambda: executor.spin_once(timeout_sec=0.02)
    status = worker.create_publisher(SpeechPlaybackStatus, '/malbut/tts/playback_status', 100)
    received, controls = [], []
    emit_finished = [True]

    def request(message):
        received.append(message)
        if emit_finished[0]:
            status.publish(SpeechPlaybackStatus(
                playback_id=message.playback_id, request_id=message.request_id, state='finished'))

    def stop(request, response):
        controls.append(request.playback_id)
        response.accepted = True  # Receipt alone is deliberately not a stopped event.
        return response

    worker.create_subscription(SpeechRequest, '/malbut/tts/request', request, 100)
    worker.create_service(ControlSpeechPlayback, '/malbut/tts/playback_control', stop)
    agent.create_publisher(SpeechRequest, '/malbut/speech/response', 100)
    agent.create_subscription(SpeechTranscript, '/malbut/speech/transcript', lambda _: None, 100)
    agent.create_service(ClassifySpeechAddressee, '/malbut/speech/classify_addressee',
                         lambda _, response: response)
    coordinator = SpeechCoordinator(
        manager, lock=RLock(), state=SimpleNamespace(active=lambda: [], pending={}),
        mission_for_goal=lambda _: None, changed=lambda: None)
    args = module.parser().parse_args(['--execute', '--test-alerts-confirmed'])
    args.ready_timeout = args.turn_timeout = args.cleanup_timeout = 2.0
    report = module.Report(tmp_path / 'reports')
    runner = module.Runner(robot, args, report)
    try:
        deadline = time.monotonic() + 10
        while not (robot.speech_ready()
                   and coordinator.requests.get_subscription_count() >= 1
                   and coordinator.client.service_is_ready()):
            assert time.monotonic() < deadline, 'fake speech peers did not discover one another'
            robot.spin()
        yield SimpleNamespace(module=module, runner=runner, received=received, controls=controls,
                              emit_finished=emit_finished, robot=robot)
    finally:
        coordinator.close()
        for node in (robot.node, worker, manager, agent):
            executor.remove_node(node)
        worker.destroy_node()
        manager.destroy_node()
        agent.destroy_node()
        executor.shutdown(timeout_sec=2)
        robot.close()


def test_guidance_uses_real_manager_admission_and_worker_completion(speech_graph):
    graph = speech_graph
    graph.runner.guide('테스트 안내')
    assert len(graph.received) == 1
    assert graph.runner.tts_finished == 1
    assert graph.received[0].request_type == 1
    assert graph.received[0].audio_id == ''  # Actual configured synthesis, not a cached notice.


def test_real_cancel_service_receipt_without_worker_terminal_is_not_silence(speech_graph):
    graph = speech_graph
    graph.emit_finished[0] = False
    with pytest.raises(graph.module.SmokeError, match='guide_stop_unconfirmed'):
        graph.runner.guide('실패 검사')
    assert graph.controls and graph.runner.tts_finished == 0


def test_simulation_timestamp_validation_uses_actual_ros_clock(speech_graph):
    from rclpy.parameter import Parameter
    from rosgraph_msgs.msg import Clock
    graph = speech_graph
    graph.robot.node.set_parameters([Parameter('use_sim_time', value=True)])
    assert not graph.robot.stamp_fresh(0)
    publisher = graph.robot.node.create_publisher(Clock, '/clock', 10)
    message = Clock()
    message.clock.sec = 43
    deadline = time.monotonic() + 5
    while not graph.robot.stamp_fresh(42):
        assert time.monotonic() < deadline
        publisher.publish(message)
        graph.robot.spin()
    assert not graph.robot.stamp_fresh(30)
    assert not graph.robot.stamp_fresh(44)
    assert not graph.robot.stamp_fresh(float('nan'))
