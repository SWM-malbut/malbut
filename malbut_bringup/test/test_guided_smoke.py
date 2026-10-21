"""Exercise the shipped single-file runner without API calls or robot motion."""

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'malbut_test/smoke.sh'


@pytest.fixture
def smoke():
    source = SCRIPT.read_text().split("<<'MALBUT_SMOKE_PY'\n", 1)[1]
    source = source.rsplit('\nMALBUT_SMOKE_PY', 1)[0]
    module = ModuleType('guided_smoke_under_test')
    exec(compile(source, str(SCRIPT), 'exec'), module.__dict__)
    return module


def speech_ledger(path, uid, provider='openai', response_id='resp_test'):
    request_id = 'speech-request-' + hashlib.sha256(uid.encode()).hexdigest()
    value = dict(request_id=request_id, provider=dict(
        provider=provider, response_id=response_id, model='fixture', usage={}))
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO conversation_turns VALUES(?,?,?)',
                           (request_id, 'completed', json.dumps(value)))


class FakeManager:
    def __init__(self, robot):
        self.robot, self.calls = robot, []
        self.record = None
        self.cancel_stalls = False
        self.start_state = 'RUNNING'
        self.visible = True
        self._client = SimpleNamespace(server_is_ready=lambda: True)

    def submit(self, capability, arguments, request_id=None):
        request_id = request_id or 'test-owned-request'
        self.calls.append(('submit', capability, arguments, request_id))
        self.record = dict(state=self.start_state, terminal=False,
                           feedback_yaml='target_visible: true\n')
        self.robot.busy('follow_person')
        return request_id

    def snapshot(self, request_id):
        value = dict(self.record)
        value['feedback_yaml'] = 'target_visible: ' + str(self.visible).lower() + '\n'
        return value

    def cancel(self, request_id):
        self.calls.append(('cancel', request_id))
        if not self.cancel_stalls:
            self.record.update(state='CANCELED', terminal=True)
            self.robot.idle()

    def close(self):
        self.calls.append(('close',))


class FakeRobot:
    def __init__(self, smoke, args):
        self.smoke, self.args, self.now = smoke, args, 0.0
        self.evidence = smoke.Evidence(lambda: self.now)
        self.manager = FakeManager(self)
        self.tasks = []
        self.spoken, self.stopped, self.fall_stops = [], [], 0
        self.images_released = False
        self.sensors_started = False
        self.fall_active = False
        self.closed = False
        self.emit_audio = True
        self.make_dialogue = True
        self.cloud_verdict = 'floor'
        self.persist_closure = True
        self.answer_okay = True
        self.parameter_failure = False
        self.audio_delay, self.dialogue_delay = 0.1, 0.5
        self.incident_delay, self.closure_delay, self.cloud_delay = 0.5, 1.0, 0.0
        self.ledger_delay = 0.0
        self.idle()

    def idle(self):
        self.evidence.add('state', dict(system_state=1, control_mode=0, missions=[]))
        self.evidence.add('runtime', dict(ready=True, error='', active_groups=[], capabilities=[]))

    def busy(self, capability):
        self.evidence.add('state', dict(system_state=2, control_mode=0,
                                        missions=[dict(id='owned', capability=capability)]))
        self.evidence.add('runtime', dict(ready=True, error='', active_groups=['motion'],
                                          capabilities=[capability]))

    def later(self, delay, callback):
        self.tasks.append((self.now + delay, callback))

    def spin(self):
        self.now += 0.1
        pending, self.tasks = self.tasks, []
        for deadline, callback in pending:
            if deadline <= self.now:
                callback()
            else:
                self.tasks.append((deadline, callback))
        self.evidence.add('odom', dict(valid=True, linear=0.0, angular=0.0))
        self.evidence.add('fall_check', dict(
            active=self.fall_active, incident_id='incident-1' if self.fall_active else None))
        self.evidence.add('speech_ready', 'ready')
        self.evidence.add('fall_status', dict(
            sequence=1, enabled=True, camera_enabled=True, cloud_consent=True,
            accepting_images=True, runtime_state='ready', last_frame_age_s=0,
            analysis_state='idle'))

    def endpoints(self):
        return True

    def sensors_ready(self):
        return self.sensors_started

    def start_sensors(self):
        self.sensors_started = True

    def speech_ready(self):
        return True

    def actions_ready(self, stage):
        return True

    def parameters(self, name, keys, wait):
        if self.parameter_failure:
            raise self.smoke.SmokeError('parameter_response_timeout')
        return {key: SimpleNamespace(type=1, bool_value=key == 'flow_enabled',
                                     string_value='openai') for key in keys}

    def release_images(self):
        self.images_released = True

    def audio(self, pid, state='finished'):
        self.evidence.add('audio', dict(playback_id=pid, request_id=pid, state=state))
        self.evidence.add('worker_audio', dict(playback_id=pid, request_id=pid, state=state))

    def say(self, text, pid):
        self.spoken.append((text, pid))
        if self.emit_audio:
            self.later(self.audio_delay, lambda: self.audio(pid))
        if text.startswith('호출어') and self.make_dialogue:
            self.later(self.dialogue_delay, self.dialogue)
        if text.startswith('낙상 상황'):
            self.later(self.incident_delay, self.fall_open)
            self.later(self.closure_delay, self.fall_close)

    def dialogue(self):
        uid = 'utterance-' + str(len(self.spoken))
        if self.ledger_delay:
            self.later(self.ledger_delay, lambda: speech_ledger(self.args.conversation_db, uid))
        else:
            speech_ledger(self.args.conversation_db, uid)
        self.evidence.add('transcript', dict(utterance_id=uid, session_id='', stop_word=False))
        self.evidence.add('reply', dict(playback_id=uid, request_id=uid, interim=False,
                                        owner_goal_id='', has_text=True, audio_id=''))
        self.audio(uid)

    def fall_open(self):
        self.fall_active = True
        self.evidence.add('fall_decision', dict(
            action='record', event='incident_opened',
            incident_id='incident-1', sighting_id='sight-1'))
        self.evidence.add('fall_decision', dict(
            action='look', look_id='look-1', incident_id='incident-1'))
        cloud = dict(kind='fall_look_result', look_id='look-1', verdict=self.cloud_verdict)
        if self.cloud_delay:
            self.later(self.cloud_delay, lambda: self.evidence.add('fall_event', cloud))
        else:
            self.evidence.add('fall_event', cloud)
        self.evidence.add('fall_decision', dict(
            action='record', event='question_requested', incident_id='incident-1'))
        self.busy('fall_confirmation')

    def fall_close(self):
        if self.answer_okay:
            self.evidence.add('fall_decision', dict(
                action='record', event='confirmation_completed',
                incident_id='incident-1', answer='okay'))
        self.evidence.add('fall_decision', dict(
            action='record', event='incident_resolved',
            incident_id='incident-1', reason='response_completed'))
        with sqlite3.connect(self.args.journal) as connection:
            connection.execute('INSERT OR REPLACE INTO flow_incidents VALUES(?,?)',
                               ('incident-1', 0))
            if self.persist_closure:
                connection.execute('INSERT INTO incident_events(incident_id,payload) VALUES(?,?)',
                                   ('incident-1', json.dumps(dict(eventKind='incident_resolved'))))
        self.fall_active = False
        self.idle()

    def stop_audio(self, pid):
        self.stopped.append(pid)
        self.audio(pid, 'stopped')

    def stop_fall(self):
        self.fall_stops += 1
        self.fall_active = False
        self.idle()

    def close(self):
        self.closed = True


@pytest.fixture
def runtime(smoke, tmp_path, monkeypatch):
    args = smoke.parser().parse_args(['--execute', '--test-alerts-confirmed', '--no-review',
                                      '--report-dir', str(tmp_path / 'reports')])
    args.conversation_db = tmp_path / 'conversation.sqlite3'
    args.journal = tmp_path / 'fall.sqlite3'
    args.follow_seconds = 1.0
    args.prepare_seconds = 0.2
    args.ready_timeout = args.turn_timeout = args.fall_timeout = args.cleanup_timeout = 2.0
    args.transition_timeout = args.rpc_timeout = 2.0
    with sqlite3.connect(args.conversation_db) as connection:
        connection.execute('CREATE TABLE conversation_turns(request_id TEXT,status TEXT,'
                           'response_json TEXT)')
    with sqlite3.connect(args.journal) as connection:
        connection.execute('CREATE TABLE flow_incidents(incident_id TEXT PRIMARY KEY,open INT)')
        connection.execute('CREATE TABLE incident_events(sequence INTEGER PRIMARY KEY,'
                           'incident_id TEXT,payload TEXT)')
    preflight = ModuleType('malbut_agent_server.fall_preflight')
    preflight.read_settings = lambda _: SimpleNamespace(journal_path=args.journal)
    monkeypatch.setitem(sys.modules, preflight.__name__, preflight)
    report = smoke.Report(args.report_dir)
    robot = FakeRobot(smoke, args)
    runner = smoke.Runner(robot, args, report, lambda: robot.now)
    return SimpleNamespace(smoke=smoke, args=args, report=report, robot=robot, runner=runner)


@pytest.mark.parametrize('arguments', [[], ['--help']])
def test_plan_and_help_never_require_ros_or_execute_actions(arguments):
    result = subprocess.run(['bash', str(SCRIPT), *arguments], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'Traceback' not in result.stderr


def test_shell_syntax_and_dangerous_operations_are_absent():
    assert subprocess.run(['bash', '-n', str(SCRIPT)], timeout=5).returncode == 0
    source = SCRIPT.read_text()
    for forbidden in ('subprocess.', 'os.system(', 'command=\'stop_all\'', 'cmd_vel',
                      'pkill', 'cancel-all'):
        # Comments document the forbidden operations; executable lines may not use them.
        assert all(forbidden not in line for line in source.splitlines()
                   if not line.lstrip().startswith(('#', '"""')))


def test_execution_requires_the_single_explicit_safety_opt_in(tmp_path):
    result = subprocess.run(
        ['bash', str(SCRIPT), '--execute', '--report-dir', str(tmp_path / 'r')],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert '--test-alerts-confirmed' in result.stderr
    assert not (tmp_path / 'r').exists()


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', '0', '901'])
def test_nonfinite_or_unbounded_timeouts_rejected_before_ros(smoke, value):
    with pytest.raises(SystemExit):
        smoke.parser().parse_args(['--turn-timeout', value])


def test_read_only_database_does_not_create_missing_files(smoke, tmp_path):
    path = tmp_path / 'missing.sqlite'
    with pytest.raises(smoke.SmokeError, match='ledger_missing'):
        smoke.read_rows(path, 'SELECT 1')
    assert not path.exists()


def test_database_reads_are_bounded_during_an_exclusive_writer(runtime):
    with sqlite3.connect(runtime.args.conversation_db) as connection:
        connection.execute('BEGIN EXCLUSIVE')
        with pytest.raises(runtime.smoke.SmokeError, match='ledger_busy'):
            runtime.smoke.read_rows(
                runtime.args.conversation_db, 'SELECT 1 FROM conversation_turns')
        assert runtime.smoke.api_evidence(runtime.args.conversation_db, 'uid') is None


@pytest.mark.parametrize('provider,response_id', [
    ('mock', 'resp_a'), ('reliable-fallback', 'resp_a'),
    ('openai', ''), ('openai', None),
])
def test_mock_fallback_and_missing_api_id_never_pass(runtime, provider, response_id):
    speech_ledger(runtime.args.conversation_db, 'u1', provider, response_id)
    with pytest.raises(runtime.smoke.SmokeError, match='real_llm_api'):
        runtime.smoke.api_evidence(runtime.args.conversation_db, 'u1')


def test_api_proof_is_correlated_to_the_current_stt_turn(runtime):
    speech_ledger(runtime.args.conversation_db, 'previous')
    assert runtime.smoke.api_evidence(runtime.args.conversation_db, 'new') is None
    speech_ledger(runtime.args.conversation_db, 'new')
    assert runtime.smoke.api_evidence(runtime.args.conversation_db, 'new')['response_id']


def test_guidance_accepted_interim_and_unrelated_playback_are_not_answers(smoke):
    evidence = smoke.Evidence()
    evidence.add('transcript', dict(utterance_id='u', session_id=''))
    evidence.add('reply', dict(request_id='u', playback_id='p', interim=True,
                               owner_goal_id='', has_text=True))
    evidence.add('audio', dict(playback_id='p', state='finished'))
    assert evidence.dialogue(0) is None
    evidence.add('reply', dict(request_id='u', playback_id='actual', interim=False,
                               owner_goal_id='', has_text=True))
    evidence.add('audio', dict(playback_id='guide', state='finished'))
    evidence.add('audio', dict(playback_id='actual', state='accepted'))
    assert evidence.dialogue(0) is None
    evidence.add('audio', dict(playback_id='actual', state='finished'))
    assert evidence.dialogue(0) == 'u'
    assert evidence.dialogue(evidence.sequence) is None


def test_confirmation_session_is_not_ordinary_dialogue(smoke):
    evidence = smoke.Evidence()
    evidence.add('transcript', dict(utterance_id='u', session_id='fall-session'))
    evidence.add('reply', dict(request_id='u', playback_id='p', interim=False,
                               owner_goal_id='', has_text=True))
    evidence.add('audio', dict(playback_id='p', state='finished'))
    assert evidence.dialogue(0) is None


def test_cross_topic_reordering_and_duplicate_turns(smoke):
    evidence = smoke.Evidence()
    evidence.add('audio', dict(playback_id='p', state='finished'))
    evidence.add('reply', dict(request_id='u', playback_id='p', interim=False,
                               owner_goal_id='', has_text=True))
    evidence.add('transcript', dict(utterance_id='u', session_id=''))
    assert evidence.dialogue(0) == 'u'
    assert evidence.dialogue(0, {'u'}) is None


def test_full_automatic_sequence_and_final_human_review(runtime):
    runtime.runner.run()
    runtime.runner.cleanup()
    assert [step['automatic'] for step in runtime.report.data['steps']] == ['PASS'] * 6
    assert runtime.robot.images_released
    assert [call[0] for call in runtime.robot.manager.calls] == ['submit', 'cancel']
    assert runtime.report.finish() == 2  # Automatic success is not a human visual pass.
    assert runtime.report.review('')
    assert runtime.report.finish() == 0
    assert (runtime.report.path.stat().st_mode & 0o777) == 0o600


def test_check_mode_does_not_issue_speech_missions_or_cancellation(runtime):
    runtime.args.check, runtime.args.execute = True, False
    runtime.runner.run()
    runtime.runner.cleanup()
    assert runtime.report.data['steps'][0]['automatic'] == 'PASS'
    assert runtime.robot.manager.calls == [] and not runtime.robot.spoken
    assert runtime.report.data['cleanup'] == 'NOT_NEEDED'


def test_prepare_failure_preserves_existing_work_and_does_not_fake_cleanup(runtime):
    runtime.robot.parameter_failure = True
    runtime.runner.run()
    runtime.runner.cleanup()
    assert runtime.report.data['steps'][0]['automatic'] == 'FAIL'
    assert runtime.report.data['cleanup'] == 'NOT_NEEDED'
    assert not runtime.robot.spoken and not runtime.robot.manager.calls


def test_guide_timeout_uses_scoped_stop_then_terminal_text_fallback(runtime):
    runtime.robot.emit_audio = False
    runtime.runner.guide('guide')
    assert len(runtime.robot.stopped) == 1
    assert not runtime.runner.guides
    assert runtime.report.data['steps'][5]['automatic'] == 'FAIL'


def test_manager_failed_is_not_evidence_that_worker_audio_stopped(runtime):
    runtime.robot.emit_audio = False
    runtime.robot.stop_audio = lambda pid: runtime.robot.evidence.add(
        'audio', dict(playback_id=pid, request_id=pid, state='failed'))
    with pytest.raises(runtime.smoke.SmokeError, match='guide_stop_unconfirmed'):
        runtime.runner.guide('guide')


def test_a_late_api_result_is_not_silently_abandoned_on_cleanup(runtime):
    runtime.runner.started_work = True
    runtime.runner.dialogue_boundary = 0
    runtime.robot.evidence.add('transcript', dict(utterance_id='pending', session_id=''))
    runtime.runner.cleanup()
    assert runtime.report.data['cleanup'] == 'UNCONFIRMED'


def test_unknown_goal_response_is_not_retried_and_is_canceled_in_cleanup(runtime):
    runtime.robot.manager.start_state = 'UNKNOWN'
    with pytest.raises(runtime.smoke.SmokeError, match='follow_start_unknown'):
        runtime.runner.follow()
    runtime.runner.cleanup()
    assert [call[0] for call in runtime.robot.manager.calls] == ['submit', 'cancel']
    assert runtime.report.data['cleanup'] == 'CONFIRMED'


def test_running_without_a_visible_target_does_not_pass_tracking(runtime):
    runtime.robot.manager.visible = False
    with pytest.raises(runtime.smoke.SmokeError, match='follow_target'):
        runtime.runner.follow()
    runtime.runner.cleanup()
    assert runtime.report.data['cleanup'] == 'CONFIRMED'


def test_cancel_ack_without_terminal_result_does_not_advance_to_fall(runtime):
    runtime.robot.manager.cancel_stalls = True
    runtime.runner.run()
    runtime.runner.cleanup()
    assert runtime.report.data['steps'][2]['automatic'] == 'FAIL'
    assert runtime.report.data['steps'][3]['automatic'] == 'NOT_RUN'
    assert runtime.report.data['cleanup'] == 'UNCONFIRMED'
    assert runtime.report.finish() == 3


def test_terminated_mission_with_lingering_runtime_or_motion_is_not_idle(runtime):
    runtime.robot.spin()
    assert runtime.runner.idle()
    runtime.robot.evidence.add('runtime', dict(
        ready=True, error='', active_groups=['motion'], capabilities=[]))
    assert not runtime.runner.idle()
    runtime.robot.idle()
    runtime.robot.evidence.add('odom', dict(valid=True, linear=float('nan'), angular=0))
    assert not runtime.runner.idle()
    runtime.robot.spin()
    runtime.robot.now += 4
    assert not runtime.runner.idle()


@pytest.mark.parametrize('verdict', ['failed', 'guard'])
def test_failed_cloud_look_cannot_pass_because_a_final_event_exists(runtime, verdict):
    runtime.robot.cloud_verdict = verdict
    runtime.runner.journal = runtime.args.journal
    with pytest.raises(runtime.smoke.SmokeError, match='fall_cloud_result_not_verified'):
        runtime.runner.fall()


def test_resolved_decision_without_durable_final_record_is_not_success(runtime):
    runtime.robot.persist_closure = False
    runtime.runner.journal = runtime.args.journal
    with pytest.raises(runtime.smoke.SmokeError, match='fall_final_result_timeout'):
        runtime.runner.fall()


def test_requested_question_requires_the_actual_confirmation_answer(runtime):
    runtime.robot.answer_okay = False
    runtime.runner.journal = runtime.args.journal
    with pytest.raises(runtime.smoke.SmokeError, match='fall_question_answer_not_verified'):
        runtime.runner.fall()


def test_cleanup_never_stops_an_unowned_fall_check(runtime):
    runtime.runner.started_work = True
    runtime.robot.fall_active = True
    runtime.runner.cleanup()
    assert runtime.robot.fall_stops == 0
    assert runtime.report.data['cleanup'] == 'UNCONFIRMED'


def test_cleanup_stops_only_the_fall_first_observed_in_this_test(runtime):
    runtime.runner.started_work = True
    runtime.runner.incident = 'incident-1'
    runtime.robot.fall_active = True
    runtime.robot.spin()
    runtime.runner.cleanup()
    assert runtime.robot.fall_stops == 1
    assert runtime.report.data['cleanup'] == 'CONFIRMED'


def test_cleanup_refuses_global_fall_stop_when_incident_identity_is_ambiguous(runtime):
    runtime.runner.started_work = True
    runtime.runner.incident = 'incident-1'
    runtime.runner.ambiguous_fall = True
    runtime.robot.fall_active = True
    runtime.robot.spin()
    runtime.runner.cleanup()
    assert runtime.robot.fall_stops == 0
    assert runtime.report.data['cleanup'] == 'UNCONFIRMED'


def test_detection_before_fall_guide_finishes_is_not_lost(runtime):
    original = runtime.robot.say

    def slow_guide(text, pid):
        if not text.startswith('낙상 상황'):
            return original(text, pid)
        runtime.robot.later(0.2, runtime.robot.fall_open)
        runtime.robot.later(0.4, lambda: runtime.robot.audio(pid, 'stopped'))
        runtime.robot.later(0.8, runtime.robot.fall_close)
    runtime.robot.say = slow_guide
    runtime.runner.journal = runtime.args.journal
    runtime.runner.fall()
    assert runtime.report.data['steps'][3]['automatic'] == 'PASS'
    assert runtime.runner.tts_failed == 0


def test_normal_standing_recheck_after_a_fall_is_allowed_to_finish(runtime):
    runtime.runner.incident = 'incident-1'
    original = runtime.robot.say

    def recheck_when_standing(text, pid):
        if not text.startswith('낙상 검사를'):
            return original(text, pid)
        runtime.robot.later(0.1, lambda: setattr(runtime.robot, 'fall_active', True))
        runtime.robot.later(0.3, lambda: runtime.robot.audio(pid, 'stopped'))
        runtime.robot.later(0.7, lambda: setattr(runtime.robot, 'fall_active', False))
    runtime.robot.say = recheck_when_standing
    runtime.runner.recovery()
    assert runtime.report.data['steps'][4]['automatic'] == 'PASS'
    assert runtime.runner.tts_failed == 0


def test_interrupt_cancels_owned_mission_while_still_spinning(runtime):
    runtime.runner.started_work = True
    runtime.runner.tracking = runtime.robot.manager.submit('follow_person', {})
    runtime.runner.interrupted = True
    runtime.runner.cleanup()
    assert runtime.report.data['cleanup'] == 'CONFIRMED'
    assert runtime.report.finish(interrupted=True) == 130


def test_enter_does_not_override_automatic_failure_or_unexecuted_checks(runtime):
    runtime.report.set(0, 'FAIL', 'sensor_timeout')
    assert runtime.report.review('')
    assert runtime.report.finish() == 1
    assert runtime.report.data['steps'][1]['observation'] == 'UNREVIEWED'
    assert not runtime.report.review('2')


def test_final_review_accepts_one_line_failure_numbers_and_a_bounded_note(runtime):
    for index in range(6):
        runtime.report.set(index, 'PASS')
    runtime.report.data['cleanup'] = 'CONFIRMED'
    assert runtime.report.review('2,4:질문이 안 들림')
    assert runtime.report.finish() == 1
    assert runtime.report.data['steps'][3]['note'] == '질문이 안 들림'
    assert not runtime.report.review('0,7')


def test_no_tty_cannot_be_confused_with_an_explicit_enter(runtime, monkeypatch):
    def missing_tty(*_args, **_kwargs):
        raise OSError('no tty')
    monkeypatch.setattr(runtime.smoke.os, 'open', missing_tty)
    runtime.smoke.final_review(runtime.report, 0.1)
    assert all(item['observation'] == 'UNREVIEWED' for item in runtime.report.data['steps'])


def test_final_review_reads_the_terminal_not_the_embedded_program_stdin(runtime, monkeypatch):
    import os
    import pty

    for index in range(6):
        runtime.report.set(index, 'PASS')
    original_open = os.open
    master, slave = pty.openpty()
    try:
        monkeypatch.setattr(os, 'open', lambda path, *args, **kwargs: (
            os.dup(slave) if path == '/dev/tty' else original_open(path, *args, **kwargs)))
        os.write(master, '2,4:질문이 안 들림\n'.encode())
        runtime.smoke.final_review(runtime.report, 1)
        assert runtime.report.data['steps'][1]['observation'] == 'FAIL'
        assert runtime.report.data['steps'][3]['note'] == '질문이 안 들림'
    finally:
        os.close(master)
        os.close(slave)


def test_evidence_is_bounded(smoke):
    evidence = smoke.Evidence()
    for number in range(5000):
        evidence.add('reply', {'number': number})
    assert len(evidence.events) == 4096
    assert evidence.sequence == 5000


def test_high_rate_odometry_cannot_evict_fall_or_dialogue_evidence(smoke):
    evidence = smoke.Evidence()
    evidence.add('fall_event', {'kind': 'fall_look_result'})
    for number in range(20000):
        evidence.add('odom', {'number': number})
    assert len(evidence.since(0, 'fall_event')) == 1
    assert evidence.value('odom')['number'] == 19999


def test_main_runs_cleanup_and_restores_signal_handlers_even_if_close_fails(runtime, monkeypatch):
    smoke = runtime.smoke
    original = smoke.Runner
    monkeypatch.setattr(smoke, 'RosRobot', lambda **_: runtime.robot)
    monkeypatch.setattr(smoke, 'Runner', lambda robot, args, report: original(
        robot, args, report, lambda: robot.now))

    def broken_close():
        runtime.robot.closed = True
        raise RuntimeError('private details must not be printed')
    runtime.robot.close = broken_close
    signals = (smoke.signal.SIGINT, smoke.signal.SIGTERM)
    before = {sig: smoke.signal.getsignal(sig) for sig in signals}
    assert smoke.main(['--execute', '--test-alerts-confirmed', '--no-review',
                       '--conversation-db', str(runtime.args.conversation_db),
                       '--follow-seconds', '0.2', '--prepare-seconds', '0.1',
                       '--report-dir', str(runtime.args.report_dir)]) == 1
    assert runtime.robot.closed
    assert all(smoke.signal.getsignal(sig) == handler for sig, handler in before.items())
    reports = list(runtime.args.report_dir.glob('*/report.json'))
    result = next(json.loads(path.read_text()) for path in reports
                  if 'infrastructure_errors' in json.loads(path.read_text()))
    assert result['cleanup'] == 'CONFIRMED'
    assert result['overall'] == 'FAIL'


def test_report_write_failure_does_not_claim_a_persisted_final_pass(runtime, monkeypatch, capsys):
    for index in range(6):
        runtime.report.set(index, 'PASS')
    runtime.report.data['cleanup'] = 'CONFIRMED'
    assert runtime.report.review('')

    def no_space():
        raise OSError('disk full')
    monkeypatch.setattr(runtime.report, 'save', no_space)
    assert runtime.report.finish() == 1
    assert '저장 실패' in capsys.readouterr().err


def test_transient_ledger_lock_retries_only_reads_while_spinning(runtime, monkeypatch):
    calls = []

    def locked_then_ready(*_args):
        calls.append(True)
        if len(calls) < 3:
            raise runtime.smoke.SmokeError('ledger_busy')
        return []
    monkeypatch.setattr(runtime.smoke, 'read_rows', locked_then_ready)
    assert runtime.runner.ledger(Path('unused'), 'SELECT 1') == []
    assert len(calls) == 3
    assert runtime.robot.now >= 0.3


def test_default_budgets_cover_existing_bringup_startup_and_lifecycle(smoke):
    args = smoke.parser().parse_args([])
    assert args.ready_timeout >= 120  # Speech's existing cold-start budget is 120 s.
    assert args.transition_timeout >= 120  # Manager's existing dependency preparation budget.
    assert args.rpc_timeout >= 30
    assert args.cleanup_timeout >= 30


def test_staggered_cold_boot_does_not_stream_images_or_start_work_early(runtime):
    robot, runner = runtime.robot, runtime.runner
    runtime.args.ready_timeout = 180
    robot.evidence.add('state', dict(system_state=0, control_mode=0, missions=[]))
    robot.evidence.add('runtime', dict(ready=False, active_groups=[], capabilities=[]))
    robot.endpoints = lambda: robot.now >= 65
    robot.speech_ready = lambda: robot.now >= 125
    robot.actions_ready = lambda stage: robot.now >= 140
    robot.later(100, robot.idle)
    start_sensors = robot.start_sensors

    def start():
        assert robot.now >= 140
        assert not robot.spoken and not robot.manager.calls
        start_sensors()

    robot.start_sensors = start
    sensors_ready = robot.sensors_ready
    robot.sensors_ready = lambda: robot.now >= 145 and sensors_ready()
    parameters = robot.parameters

    def late_parameters(*args):
        # Large temporary subscriptions must be gone before further RPC waits.
        assert robot.images_released
        return parameters(*args)

    # runtime_control is queried before sensors, the other nodes afterwards.
    robot.parameters = lambda name, *args: (
        parameters(name, *args) if name == '/runtime_control'
        else late_parameters(name, *args))
    runner.prepare()
    assert runtime.report.data['steps'][0]['automatic'] == 'PASS'
    assert robot.now >= 145 and robot.images_released
    assert not robot.spoken and not robot.manager.calls


def test_sensor_probe_is_released_even_when_sensor_or_tf_never_becomes_ready(runtime):
    runtime.robot.sensors_ready = lambda: False
    with pytest.raises(runtime.smoke.SmokeError, match='sensor_or_tf_unavailable'):
        runtime.runner.prepare()
    assert runtime.robot.images_released


def test_actual_parameter_probe_waits_for_delayed_discovery_and_reply(runtime):
    from concurrent.futures import Future
    robot, runner, smoke = runtime.robot, runtime.runner, runtime.smoke
    future, destroyed = Future(), []
    client = SimpleNamespace(service_is_ready=lambda: robot.now >= 12)

    def call(request):
        assert request.names == ['use_sim_time']
        robot.later(18, lambda: future.set_result(SimpleNamespace(values=[False])))
        return future

    client.call_async = call
    client.remove_pending_request = lambda _: pytest.fail('read-only RPC was abandoned early')
    probe = object.__new__(smoke.RosRobot)
    probe.rpc_timeout = 30
    probe.node = SimpleNamespace(create_client=lambda *_: client,
                                 destroy_client=destroyed.append)
    probe.GetParameters = SimpleNamespace(Request=lambda **kwargs: SimpleNamespace(**kwargs))
    assert probe.parameters('/runtime_control', ['use_sim_time'], runner.wait) == {
        'use_sim_time': False}
    assert robot.now >= 30 and destroyed == [client]


@pytest.mark.parametrize('multiplier', [0.5, 1.0, 1.5])
def test_entire_sequence_with_slow_callbacks_lifecycle_and_reordered_cloud(runtime, multiplier):
    robot, runner, args = runtime.robot, runtime.runner, runtime.args
    args.ready_timeout, args.transition_timeout, args.rpc_timeout = 180, 120, 30
    args.turn_timeout, args.fall_timeout, args.cleanup_timeout = 90, 240, 60
    robot.audio_delay, robot.dialogue_delay = 4 * multiplier, 12 * multiplier
    robot.incident_delay, robot.closure_delay = 6 * multiplier, 20 * multiplier
    robot.cloud_delay = 20 * multiplier  # Arrives AFTER incident_resolved on the other Topic.
    robot.ledger_delay = 10 * multiplier  # A final reply alone is still not API proof.
    submit = robot.manager.submit

    def delayed_submit(*args, **kwargs):
        uid = submit(*args, **kwargs)
        robot.manager.record['state'] = 'PENDING'
        robot.manager.visible = False

        def active():
            robot.manager.record['state'] = 'RUNNING'
            robot.manager.visible = True

        robot.later(50 * multiplier, active)
        return uid

    def delayed_cancel(uid):
        robot.manager.calls.append(('cancel', uid))

        def terminal():
            robot.manager.record.update(state='CANCELED', terminal=True)
            # The Action result precedes Nav2/perception's actual idle state.
            robot.later(35 * multiplier, robot.idle)

        robot.later(4 * multiplier, terminal)

    robot.manager.submit, robot.manager.cancel = delayed_submit, delayed_cancel
    runner.run()
    runner.cleanup()
    assert [step['automatic'] for step in runtime.report.data['steps']] == ['PASS'] * 6
    assert runtime.report.data['cleanup'] == 'CONFIRMED'
    assert [entry[0] for entry in robot.manager.calls] == ['submit', 'cancel']
    assert runtime.report.data['steps'][3]['evidence']['cloud_looks_completed'] == 1
    timing = runtime.report.data['steps'][2]['evidence']
    assert 1.0 <= timing['observed_seconds'] < 1.2
    assert timing['stop_and_idle_seconds'] >= 39 * multiplier


def test_pending_feature_server_is_discovered_before_follow_is_submitted(runtime):
    robot = runtime.robot
    runtime.args.ready_timeout = 15
    robot.actions_ready = lambda stage: robot.now >= 10
    submit = robot.manager.submit

    def checked_submit(*args, **kwargs):
        assert robot.now >= 10
        return submit(*args, **kwargs)

    robot.manager.submit = checked_submit
    runtime.runner.follow()
    assert runtime.report.data['steps'][2]['automatic'] == 'PASS'


def test_missing_feature_server_never_submits_an_action(runtime):
    runtime.robot.actions_ready = lambda stage: False
    with pytest.raises(runtime.smoke.SmokeError, match='follow_dependencies_not_discovered'):
        runtime.runner.follow()
    assert not runtime.robot.manager.calls and not runtime.robot.spoken


def test_in_progress_periodic_cloud_scan_does_not_block_normal_fall_preparation(runtime):
    runtime.robot.spin()
    value = dict(runtime.robot.evidence.value('fall_status'), analysis_state='waiting_response')
    runtime.robot.evidence.add('fall_status', value)
    assert runtime.runner.fall_ready()


def test_user_reply_received_as_instruction_finishes_is_not_lost(runtime):
    robot = runtime.robot

    def early_reply(text, pid):
        robot.spoken.append((text, pid))
        robot.later(0.1, robot.dialogue)
        robot.later(0.2, lambda: robot.audio(pid))

    robot.say = early_reply
    assert runtime.runner.dialogue_turn('호출어를 부르고 이야기하세요.')['provider'] == 'openai'


def test_normal_speech_barge_in_yields_guide_without_failing_tts(runtime):
    robot = runtime.robot

    def interrupted_guide(text, pid):
        robot.spoken.append((text, pid))
        robot.later(0.1, lambda: robot.audio(pid, 'stopped'))
        robot.later(0.4, robot.dialogue)

    robot.say = interrupted_guide
    assert runtime.runner.dialogue_turn('호출어를 부르고 이야기하세요.')['provider'] == 'openai'
    assert runtime.runner.tts_failed == 0


def test_startup_journal_creation_is_waited_for_without_creating_it(runtime):
    robot, args, runner = runtime.robot, runtime.args, runtime.runner
    args.ready_timeout = 20
    existing = args.conversation_db
    args.conversation_db = existing.with_name('late-start.sqlite3')
    robot.later(12, lambda: existing.rename(args.conversation_db))
    runner.prepare()
    assert runtime.report.data['steps'][0]['automatic'] == 'PASS' and robot.now >= 12


@pytest.fixture
def speech_probe(smoke, monkeypatch):
    # Do not let a loaded CI worker turn the cache test into a wall-time race.
    monkeypatch.setattr(smoke.time, 'monotonic', lambda: 100.0)
    robot = object.__new__(smoke.RosRobot)
    publishers, subscribers, queries = {}, {}, []

    def add(mapping, topic, name, kind):
        mapping.setdefault(topic, []).append(SimpleNamespace(
            node_namespace='/', node_name=name, topic_type='malbut_interfaces/msg/' + kind))

    add(publishers, '/malbut/speech/playback_status', 'manager', 'SpeechPlaybackStatus')
    add(publishers, '/malbut/tts/playback_status', 'tts', 'SpeechPlaybackStatus')
    add(publishers, '/malbut/speech/response', 'agent', 'SpeechRequest')
    add(publishers, '/malbut/tts/request', 'manager', 'SpeechRequest')
    add(subscribers, '/malbut/speech/response', 'manager', 'SpeechRequest')
    add(subscribers, '/malbut/tts/playback_status', 'manager', 'SpeechPlaybackStatus')
    add(subscribers, '/malbut/tts/request', 'tts', 'SpeechRequest')
    add(subscribers, '/malbut/speech/transcript', 'agent', 'SpeechTranscript')
    # The runner itself sees and publishes speech: those endpoints prove nothing.
    add(publishers, '/malbut/speech/response', 'malbut_smoke', 'SpeechRequest')
    add(subscribers, '/malbut/speech/response', 'malbut_smoke', 'SpeechRequest')
    add(subscribers, '/malbut/speech/transcript', 'malbut_smoke', 'SpeechTranscript')

    def query(mapping, topic):
        queries.append(topic)
        return mapping.get(topic, [])

    robot.node = SimpleNamespace(
        count_publishers=lambda _: 1, get_namespace=lambda: '/', get_name=lambda: 'malbut_smoke',
        get_publishers_info_by_topic=lambda topic: query(publishers, topic),
        get_subscriptions_info_by_topic=lambda topic: query(subscribers, topic))
    robot.speech = SimpleNamespace(get_subscription_count=lambda: 2)
    robot.speech_control = robot.worker_control = robot.classifier = SimpleNamespace(
        service_is_ready=lambda: True)
    robot._graph_at, robot._graph = -float('inf'), {}
    return SimpleNamespace(robot=robot, publishers=publishers, subscribers=subscribers,
                           queries=queries)


def test_actual_speech_probe_checks_the_complete_route_not_just_stt_presence(speech_probe):
    probe = speech_probe
    assert probe.robot.speech_ready()
    probe.subscribers['/malbut/speech/transcript'] = [
        item for item in probe.subscribers['/malbut/speech/transcript']
        if item.node_name == 'malbut_smoke']
    probe.robot._graph_at = -float('inf')
    assert not probe.robot.speech_ready()


def test_actual_speech_probe_does_not_confuse_stt_with_manager(speech_probe):
    probe = speech_probe
    for item in probe.subscribers['/malbut/speech/response']:
        if item.node_name == 'manager':
            item.node_name = 'stt'
    assert not probe.robot.speech_ready()


def test_speech_probe_rejects_an_incomplete_worker_even_if_the_node_exists(speech_probe):
    speech_probe.subscribers['/malbut/tts/request'] = []
    assert not speech_probe.robot.speech_ready()


def test_graph_queries_are_throttled_but_readiness_is_rechecked(speech_probe):
    probe = speech_probe
    assert probe.robot.speech_ready()
    calls = len(probe.queries)
    for _ in range(100):
        assert probe.robot.endpoints() and probe.robot.speech_ready()
    assert len(probe.queries) == calls
    probe.subscribers['/malbut/tts/request'] = []
    probe.robot._graph_at = -float('inf')
    assert not probe.robot.speech_ready() and len(probe.queries) > calls


def test_guide_waits_for_actual_speech_route_before_publishing(runtime):
    robot = runtime.robot
    runtime.args.ready_timeout = 15
    robot.speech_ready = lambda: robot.now >= 10
    say = robot.say

    def checked_say(*args):
        assert robot.now >= 10
        say(*args)

    robot.say = checked_say
    runtime.runner.guide('연결이 준비된 뒤에만 한 번 안내합니다.')
    assert len(robot.spoken) == 1


def test_missing_cloud_evidence_still_times_out_instead_of_passing(runtime):
    runtime.robot.cloud_delay = 100
    runtime.runner.journal = runtime.args.journal
    with pytest.raises(runtime.smoke.SmokeError, match='fall_final_result_timeout'):
        runtime.runner.fall()
    assert runtime.report.data['steps'][3]['automatic'] != 'PASS'


def test_startup_never_creates_a_missing_ledger_or_waits_forever(runtime):
    runtime.args.conversation_db = runtime.args.conversation_db.with_name('absent.sqlite3')
    with pytest.raises(runtime.smoke.SmokeError, match='ledger_read_timeout'):
        runtime.runner.prepare()
    assert not runtime.args.conversation_db.exists()
    assert not runtime.robot.spoken and not runtime.robot.manager.calls


def test_actual_parameter_timeout_cancels_only_its_pending_read_and_destroys_client(runtime):
    from concurrent.futures import Future
    smoke, runner = runtime.smoke, runtime.runner
    future, removed, destroyed = Future(), [], []
    client = SimpleNamespace(service_is_ready=lambda: True, call_async=lambda _: future,
                             remove_pending_request=removed.append)
    probe = object.__new__(smoke.RosRobot)
    probe.rpc_timeout = 2
    probe.node = SimpleNamespace(create_client=lambda *_: client,
                                 destroy_client=destroyed.append)
    probe.GetParameters = SimpleNamespace(Request=lambda **kwargs: SimpleNamespace(**kwargs))
    with pytest.raises(smoke.SmokeError, match='parameter_response_timeout'):
        probe.parameters('/runtime_control', ['use_sim_time'], runner.wait)
    assert removed == [future] and destroyed == [client] and future.cancelled()


def test_interrupt_while_asr_is_running_waits_for_late_transcript_and_audio(runtime):
    runner, robot = runtime.runner, runtime.robot
    runner.started_work = True
    runner.dialogue_boundary = robot.evidence.sequence
    robot.evidence.add('input_status', dict(
        state='started', utterance_id='recognizing', session_id=''))
    robot.later(1, lambda: robot.evidence.add('transcript', dict(
        utterance_id='recognizing', session_id='')))
    robot.later(1.2, lambda: robot.evidence.add('reply', dict(
        request_id='recognizing', playback_id='late', interim=False)))
    runner.interrupted = True
    runner.cleanup()
    assert robot.now >= 1.2 and 'late' in robot.stopped
    assert runtime.report.data['cleanup'] == 'CONFIRMED'


def test_unfinished_asr_cannot_be_reported_as_confirmed_cleanup(runtime):
    runtime.runner.started_work = True
    runtime.runner.dialogue_boundary = 0
    runtime.robot.evidence.add('input_status', dict(
        state='started', utterance_id='recognizing', session_id=''))
    runtime.runner.cleanup()
    assert runtime.report.data['cleanup'] == 'UNCONFIRMED'


def test_failed_recognition_does_not_wait_for_a_nonexistent_api_reply(runtime):
    runtime.runner.started_work = True
    runtime.runner.dialogue_boundary = 0
    for state in ('started', 'failed'):
        runtime.robot.evidence.add('input_status', dict(
            state=state, utterance_id='failed-asr', session_id=''))
    runtime.runner.cleanup()
    assert runtime.report.data['cleanup'] == 'CONFIRMED'


def test_fall_detected_during_interrupted_guidance_is_stopped(runtime):
    robot, runner = runtime.robot, runtime.runner
    runner.journal = runtime.args.journal

    def interrupt(text, pid):
        robot.later(0.1, robot.fall_open)
        robot.later(0.2, lambda: setattr(runner, 'interrupted', True))

    robot.say = interrupt
    with pytest.raises(runtime.smoke.SmokeError, match='interrupted'):
        runner.fall()
    runner.cleanup()
    assert robot.fall_stops == 1 and runner.incident == 'incident-1'
    assert runtime.report.data['cleanup'] == 'CONFIRMED'


def test_simulation_flag_does_not_allow_a_wall_clock_manager(runtime):
    runtime.args.simulation = True
    with pytest.raises(runtime.smoke.SmokeError, match='simulation_clock_required'):
        runtime.runner.prepare()
    assert not runtime.robot.spoken and not runtime.robot.manager.calls


def test_fall_ownership_arriving_during_cleanup_is_not_abandoned(runtime):
    runner, robot = runtime.runner, runtime.robot
    runner.started_work = True
    runner.fall_boundary = robot.evidence.sequence
    runner.interrupted = True
    robot.later(0.2, lambda: setattr(robot, 'fall_active', True))
    robot.later(0.5, robot.fall_open)
    runner.cleanup()
    assert robot.fall_stops == 1
    assert runtime.report.data['cleanup'] == 'CONFIRMED'


def test_cleanup_never_selects_one_of_multiple_new_incidents(runtime):
    runner, robot = runtime.runner, runtime.robot
    runner.started_work = True
    runner.fall_boundary = robot.evidence.sequence
    robot.fall_open()
    robot.evidence.add('fall_decision', dict(
        action='record', event='incident_opened', incident_id='unrelated-incident'))
    runner.cleanup()
    assert runner.ambiguous_fall and robot.fall_stops == 0
    assert runtime.report.data['cleanup'] == 'UNCONFIRMED'
