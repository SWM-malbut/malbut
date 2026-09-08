"""Exercise the installed receipt-only Agent over real DDS, without audio."""

import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time

import pytest


def test_installed_receiver_delivery_and_restart(tmp_path, monkeypatch):
    """Preserve final text, deduplicate receipts, and shut down cleanly."""
    rclpy = pytest.importorskip('rclpy')
    messages = pytest.importorskip('malbut_interfaces.msg')
    if not hasattr(messages, 'SpeechTranscript'):
        pytest.skip('Build and source malbut_interfaces first')
    packages = pytest.importorskip('ament_index_python.packages')
    try:
        prefix = Path(packages.get_package_prefix('malbut_agent_server'))
    except packages.PackageNotFoundError:
        pytest.skip('Build and source malbut_agent_server first')
    executable = prefix / 'lib' / 'malbut_agent_server' / 'speech_receiver'
    if not executable.is_file():
        pytest.skip('Installed speech_receiver entrypoint is required')

    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import (
        DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
    )

    # Keep the test away from the existing Manager and physical robot domains.
    monkeypatch.setenv('ROS_DOMAIN_ID', '191')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    from malbut_agent_server.speech_receipts import SpeechReceiptStore
    from malbut_agent_server.speech_receiver import create_receiver_node

    rclpy.init()
    sender = rclpy.create_node('speech_delivery_test')
    executor = SingleThreadedExecutor()
    executor.add_node(sender)
    qos = QoSProfile(
        history=HistoryPolicy.KEEP_LAST, depth=10,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )
    topic = '/malbut/speech/transcript'
    publisher = sender.create_publisher(messages.SpeechTranscript, topic, qos)
    database = tmp_path / 'receipts.sqlite3'
    log_path = tmp_path / 'first.log'
    process = None

    def output():
        return log_path.read_text() if log_path.exists() else ''

    def wait_for(condition, reason):
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if condition():
                return
            if process is not None and process.poll() is not None:
                pytest.fail(f'Receiver exited: {output()}')
            executor.spin_once(timeout_sec=0.05)
        pytest.fail(f'{reason}: {output()}')

    def start(filename):
        nonlocal process, log_path
        log_path = tmp_path / filename
        with log_path.open('w') as stream:
            process = subprocess.Popen(
                [str(executable), '--db-path', str(database)],
                stdin=subprocess.DEVNULL, stdout=stream,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
        wait_for(lambda: 'Listening on' in output(), 'Receiver not ready')
        wait_for(lambda: publisher.get_subscription_count() == 1,
                 'DDS subscription not discovered')

    def stop():
        nonlocal process
        os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=5)
        assert process.returncode == 0, output()
        assert 'Traceback' not in output(), output()
        process = None

    def publish(utterance_id, text):
        publisher.publish(messages.SpeechTranscript(
            utterance_id=utterance_id, text=text,
        ))

    def rows():
        with sqlite3.connect(database) as connection:
            return connection.execute(
                'SELECT utterance_id, text_sha256, received_at '
                'FROM speech_receipts ORDER BY utterance_id'
            ).fetchall()

    def events():
        return [json.loads(line[line.index('{'):])
                for line in output().splitlines()
                if '"event": "speech_transcript"' in line]

    try:
        receipts = SpeechReceiptStore(':memory:')
        probe = create_receiver_node(receipts)
        try:
            local_qos = next(probe.subscriptions).qos_profile
            assert local_qos.history == HistoryPolicy.KEEP_LAST
            assert local_qos.depth == 10
            assert local_qos.reliability == ReliabilityPolicy.RELIABLE
            assert local_qos.durability == DurabilityPolicy.VOLATILE
        finally:
            probe.destroy_node()
            receipts.close()
        publish('before-subscriber', '과거 발화')
        start('first.log')
        infos = sender.get_subscriptions_info_by_topic(topic)
        assert len(infos) == 1
        actual = infos[0].qos_profile
        assert actual.reliability == ReliabilityPolicy.RELIABLE
        assert actual.durability == DurabilityPolicy.VOLATILE
        # Humble FastDDS discovery does not expose remote history or depth;
        # the live local subscription above verifies those configured values.
        for _ in range(4):
            executor.spin_once(timeout_sec=0.05)
        assert rows() == []

        text = '  같은 문장\n'
        publish('utterance-1', text)
        wait_for(lambda: any(e['status'] == 'received' for e in events()),
                 'New transcript not received')
        assert events()[0]['text'] == text
        first = rows()[0]
        assert first[0] == 'utterance-1'
        assert first[1] == hashlib.sha256(text.encode('utf-8')).hexdigest()
        assert isinstance(first[2], float)

        publish('utterance-1', text)
        wait_for(lambda: any(e['status'] == 'duplicate' for e in events()),
                 'Duplicate not detected')
        publish('utterance-1', '다른 문장')
        wait_for(lambda: any(e['status'] == 'conflict' for e in events()),
                 'Conflicting ID not detected')
        assert rows() == [first]
        publish('utterance-2', text)
        wait_for(lambda: len(rows()) == 2, 'New ID with same text not received')
        publish('', '비어 있는 ID')
        wait_for(lambda: 'invalid: blank ID or text' in output(),
                 'Invalid ID not rejected')
        assert len(rows()) == 2
        stop()
        wait_for(lambda: publisher.get_subscription_count() == 0,
                 'Old subscriber was not removed')

        start('restart.log')
        publish('utterance-1', text)
        wait_for(lambda: any(e['status'] == 'duplicate' for e in events()),
                 'Restart lost the receipt')
        assert not any(e['status'] == 'received' for e in events())
        assert len(rows()) == 2
        stop()
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        executor.shutdown()
        sender.destroy_node()
        rclpy.shutdown()
