"""Exercise the reusable laboratory with real ROS and exclusively fake motion."""

from dataclasses import replace
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import textwrap
from threading import get_ident
import time

import pytest


pytest.importorskip('rclpy', reason='ROS 2 is not installed')

from malbut_agent_server.voice_lab_graph import VoiceLabGraph  # noqa: E402
from malbut_agent_server.config import Settings  # noqa: E402
from rclpy.action.graph import get_action_names_and_types  # noqa: E402


def until(graph, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        graph.spin()
    raise AssertionError('laboratory evidence did not arrive')


def test_mock_follow_cancel_replay_and_cleanup_are_isolated():
    output, threads = [], []
    old_domain = os.environ.get('ROS_DOMAIN_ID')
    graph = VoiceLabGraph(lambda event: (output.append(event), threads.append(get_ident())), 195)
    try:
        graph.start()
        temporary = graph.directory
        action_names = {name for name, _ in get_action_names_and_types(graph.agent)}
        assert graph.namespace + '/mission/execute' in action_names
        assert '/malbut/mission/execute' not in action_names
        uid = graph.send('따라와')
        graph.wait_reply(uid)
        until(graph, lambda: len(graph.goals) == 1)
        assert graph.goals[0]['capability_id'] == 'follow_person'
        assert graph.goals[0]['arguments']['target_mode'] == 0
        graph.send('따라와', uid)
        cancel = graph.send('멈춰')
        assert '취소' in graph.wait_reply(cancel)['text']
        until(graph, lambda: any(event['kind'] == 'canceled' for event in graph.events))
        until(graph, lambda: any('작업이 취소됐어요.' == item['text'] for item in graph.speech))
        assert len(graph.goals) == 1
        assert all(thread == get_ident() for thread in threads)
        assert {'reply', 'mission', 'goal', 'system', 'speech'} <= {
            event['event'] for event in output}
    finally:
        graph.close()
    assert not temporary.exists()
    assert not graph._thread.is_alive()
    assert os.environ.get('ROS_DOMAIN_ID') == old_domain
    graph.close()


def test_navigation_map_mismatch_reject_abort_and_default_success():
    graph = VoiceLabGraph(lambda event: None, 195)
    try:
        graph.start()
        graph.set_map(variant='other')
        graph.wait_reply(graph.send('거실로 가'))
        assert graph.goals == []
        graph.set_map()
        graph.set_behavior('navigate_to_pose', 'reject')
        graph.wait_reply(graph.send('거실로 가'))
        until(graph, lambda: any(event['kind'] == 'failed' for event in graph.events))
        assert graph.goals[-1]['accepted'] is False
        graph.set_behavior('navigate_to_pose', 'abort', delay_s=0.02)
        graph.wait_reply(graph.send('주방으로 가'))
        until(graph, lambda: sum(event['kind'] == 'failed' for event in graph.events) == 2)
        assert graph.goals[-1]['arguments']['pose']['pose']['position']['x'] == 2.0
        graph.reset_behaviors()
        graph.wait_reply(graph.send('현관으로 가'))
        until(graph, lambda: any(event['kind'] == 'succeeded' for event in graph.events))
        assert len(graph.goals) == 3
    finally:
        graph.close()


def test_manager_absent_does_not_reach_actuators():
    graph = VoiceLabGraph(lambda event: None, 195)
    try:
        graph.start(manager_enabled=False, navigation_enabled=False)
        graph.wait_reply(graph.send('따라와'))
        assert any(event['kind'] == 'unavailable' for event in graph.events)
        assert graph.goals == []
        assert graph.status()['manager_enabled'] is False
    finally:
        graph.close()


def test_default_provider_ignores_live_environment(monkeypatch):
    monkeypatch.setenv('MALBUT_AGENT_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'unused-test-environment-key')
    graph = VoiceLabGraph(lambda event: None)
    assert graph.dialogue_settings.provider == 'mock'
    assert graph.status()['provider'] == 'mock'
    assert graph.status()['model'] == 'malbut-korean-rules-v1'
    assert graph.reply_timeout_s == 8.0


def test_explicit_settings_use_temporary_storage_and_preserve_dialogue_and_missions(
    tmp_path, monkeypatch,
):
    from malbut_agent_server import factory

    production_db = tmp_path / 'production.sqlite3'
    production_db.write_bytes(b'existing user data')
    settings = Settings(
        provider='openai', openai_api_key='unused-test-provider-key',
        openai_model='voice-lab-test-model', database_path=str(production_db),
        user_id='production-user', provider_total_timeout_seconds=35,
    )
    captured, output = [], []
    build_orchestrator = factory.build_orchestrator

    def fake_provider_runtime(received, **kwargs):
        captured.append(received)
        # Keep the real stores, dialogue and mission integration, replacing only
        # model construction so this test cannot perform a live API request.
        return build_orchestrator(replace(received, provider='mock'), **kwargs)

    monkeypatch.setattr(factory, 'build_orchestrator', fake_provider_runtime)
    graph = VoiceLabGraph(output.append, 195, dialogue_settings=settings)
    try:
        graph.start()
        assert len(captured) == 1
        effective = captured[0]
        assert effective == replace(
            settings, database_path=str(graph.directory / 'dialogue.sqlite3'),
            user_id=graph.namespace.lstrip('/'),
        )
        assert Path(effective.database_path).is_file()
        assert settings.database_path == str(production_db)
        assert settings.user_id == 'production-user'
        assert graph.reply_timeout_s == 45.0
        ready = next(event for event in output if event.get('kind') == 'ready')
        assert ready['provider'] == graph.status()['provider'] == 'openai'
        assert ready['model'] == graph.status()['model'] == 'voice-lab-test-model'
        original = graph.wait_reply(graph.send('나는 산책을 좋아해'))
        history = graph.wait_reply(graph.send('내가 뭐라고 했어?'))
        assert '나는 산책을 좋아해' in history['text']
        assert original['conversation_id'] == history['conversation_id']
        graph.set_behavior('navigate_to_pose', 'hold')
        navigation = graph.wait_reply(graph.send('거실로 가'))
        until(graph, lambda: len(graph.goals) == 1)
        assert graph.goals[0]['capability_id'] == 'navigate_to_pose'
        assert graph.goals[0]['arguments']['pose']['pose']['position']['x'] == 1.0
        cancel = graph.wait_reply(graph.send('멈춰'))
        until(graph, lambda: any(event['kind'] == 'canceled' for event in graph.events))
        assert navigation['conversation_id'] == cancel['conversation_id'] == original[
            'conversation_id']
        assert 'unused-test-provider-key' not in json.dumps(output + [graph.status()])
    finally:
        graph.close()
    assert production_db.read_bytes() == b'existing user data'
    assert not graph.directory.exists()


@pytest.mark.parametrize('settings,error', [
    (Settings(provider='openai'), 'OPENAI_API_KEY is required'),
    (Settings(tool_mode='simulation'), 'proposal-only'),
    ('openai', 'must be Settings'),
])
def test_explicit_settings_fail_preflight_before_start(settings, error):
    with pytest.raises((TypeError, ValueError), match=error):
        VoiceLabGraph(lambda event: None, dialogue_settings=settings)


@pytest.mark.parametrize('outcome,delay', [('unknown', 0), ('hold', -1), ('hold', float('nan'))])
def test_fake_behavior_validation(outcome, delay):
    graph = VoiceLabGraph(lambda event: None)
    with pytest.raises(ValueError):
        graph.set_behavior('follow_person', outcome, delay_s=delay)


def test_sigint_with_active_follow_exits_process_and_cleans_ros():
    code = textwrap.dedent('''
        from malbut_agent_server.voice_lab_graph import VoiceLabGraph
        graph = VoiceLabGraph(lambda event: None, 195)
        try:
            graph.start()
            graph.set_behavior('follow_person', 'hold', cancel_delay_s=30.0)
            graph.wait_reply(graph.send('따라와'))
            while not graph.goals:
                graph.spin()
            print('READY', flush=True)
            while True:
                graph.spin()
        except KeyboardInterrupt:
            pass
        finally:
            graph.close()
        print('CLOSED', flush=True)
    ''')
    process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10.0)
        assert ready, 'child laboratory did not become ready'
        assert process.stdout.readline().strip() == 'READY'
        process.send_signal(signal.SIGINT)
        output, error = process.communicate(timeout=10.0)
        assert process.returncode == 0, error
        assert 'CLOSED' in output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5.0)
