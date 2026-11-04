"""Worker scheduling must be local, verifiable and optional for audio startup."""

from types import SimpleNamespace

import pytest

from malbut_stt.echo import scheduling, timing, worker


@pytest.fixture
def priority(monkeypatch):
    state = SimpleNamespace(value=0, gets=[], sets=[])
    monkeypatch.setattr(scheduling.os, 'PRIO_PROCESS', 0, raising=False)
    def get(which, who):
        state.gets.append((which, who))
        return state.value
    def set_(which, who, value):
        state.sets.append((which, who, value))
        state.value = value
    monkeypatch.setattr(scheduling.os, 'getpriority', get, raising=False)
    monkeypatch.setattr(scheduling.os, 'setpriority', set_, raising=False)
    return state


def test_sets_only_current_worker_and_verifies_effective_value(priority, capsys):
    result = scheduling.configure_audio_worker_priority()
    assert result == dict(target_nice=-5, effective_nice=-5, status='applied')
    assert priority.sets == [(scheduling.os.PRIO_PROCESS, 0, -5)]
    assert priority.gets == [(scheduling.os.PRIO_PROCESS, 0)] * 2
    line = capsys.readouterr().err
    assert line.count('\n') == 1
    assert 'INFO' in line and '"status": "applied"' in line


@pytest.mark.parametrize('existing', [-5, -10])
def test_preserves_existing_equal_or_stronger_priority(priority, existing, capsys):
    priority.value = existing
    result = scheduling.configure_audio_worker_priority()
    assert result['status'] == 'preserved'
    assert result['effective_nice'] == existing
    assert priority.sets == []
    assert priority.gets == [(scheduling.os.PRIO_PROCESS, 0)]
    assert 'INFO' in capsys.readouterr().err


def test_denied_permission_continues_with_explicit_warning(monkeypatch, priority, capsys):
    def denied(which, who, value):
        priority.sets.append((which, who, value))
        raise PermissionError('private OS detail must not enter the log')
    monkeypatch.setattr(scheduling.os, 'setpriority', denied)
    result = scheduling.configure_audio_worker_priority()
    assert result == dict(target_nice=-5, effective_nice=0, status='permission_denied')
    assert priority.sets == [(scheduling.os.PRIO_PROCESS, 0, -5)]
    line = capsys.readouterr().err
    assert 'WARNING' in line and 'private OS detail' not in line


def test_unsupported_api_continues_with_warning(monkeypatch, priority, capsys):
    monkeypatch.delattr(scheduling.os, 'setpriority')
    result = scheduling.configure_audio_worker_priority()
    assert result == dict(target_nice=-5, effective_nice=None, status='unsupported')
    assert priority.sets == []
    assert 'WARNING' in capsys.readouterr().err


def test_unavailable_initial_priority_never_overwrites_unknown_stronger_value(
        monkeypatch, priority, capsys):
    def unavailable(*args):
        raise OSError('unavailable')
    monkeypatch.setattr(scheduling.os, 'getpriority', unavailable)
    result = scheduling.configure_audio_worker_priority()
    assert result['status'] == 'priority_unavailable'
    assert priority.sets == []
    assert 'WARNING' in capsys.readouterr().err


def test_successful_setter_without_effective_change_is_not_reported_applied(
        monkeypatch, priority, capsys):
    monkeypatch.setattr(scheduling.os, 'setpriority', lambda *args: None)
    result = scheduling.configure_audio_worker_priority()
    assert result == dict(target_nice=-5, effective_nice=0, status='not_applied')
    assert 'WARNING' in capsys.readouterr().err


def test_failed_readback_does_not_claim_applied(monkeypatch, priority, capsys):
    calls = [0]
    def get(*args):
        calls[0] += 1
        if calls[0] > 1:
            raise OSError('readback failed')
        return 0
    monkeypatch.setattr(scheduling.os, 'getpriority', get)
    result = scheduling.configure_audio_worker_priority()
    assert result == dict(target_nice=-5, effective_nice=None, status='unverified')
    assert 'WARNING' in capsys.readouterr().err


def test_log_failure_cannot_break_audio_startup(monkeypatch, priority):
    def broken(*args):
        raise BrokenPipeError('closed log')
    monkeypatch.setattr(scheduling.sys, 'stderr', SimpleNamespace(write=broken))
    assert scheduling.configure_audio_worker_priority()['status'] == 'applied'


def test_worker_sets_priority_before_raw_capture_branch(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduling, 'configure_audio_worker_priority',
                        lambda: calls.append('priority'))
    monkeypatch.setattr(worker, 'run_raw', lambda *args: calls.append('raw'))
    worker.run({'raw_source': 'fixture'}, None, None)
    assert calls == ['priority', 'raw']


def test_worker_sets_priority_before_trace_writer_and_native_threads(monkeypatch):
    calls = []
    class StopBeforeTraceThread(Exception):
        pass
    def trace(*args):
        calls.append('trace')
        raise StopBeforeTraceThread()
    monkeypatch.setattr(scheduling, 'configure_audio_worker_priority',
                        lambda: calls.append('priority'))
    monkeypatch.setattr(timing, 'TimingTrace', trace)
    with pytest.raises(StopBeforeTraceThread):
        worker.run({}, None, None)
    assert calls == ['priority', 'trace']
