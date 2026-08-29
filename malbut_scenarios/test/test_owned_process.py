"""Contracts for bounded ownership of one child-process session."""

import ast
import hashlib
import os
from pathlib import Path
import signal
import sys
import time

import pytest

from malbut_scenarios import owned_process as owned_process_module
from malbut_scenarios.owned_process import OwnedProcess, OwnedProcessError


_PYTHON = Path(sys.executable).resolve(strict=True)


def _wait_until(predicate, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('bounded process condition was not reached')


def _python_owner(tmp_path, source, *, maximum_output_bytes=4096):
    return OwnedProcess(
        'test-child',
        (str(_PYTHON), '-c', source),
        cwd=tmp_path,
        environment={'MALBUT_TEST_PRIVATE': 'private-environment-value'},
        maximum_output_bytes=maximum_output_bytes,
    )


@pytest.fixture
def process_owners():
    """Ensure a failed assertion cannot leave a test-owned session alive."""
    owners = []
    yield owners
    for owner in reversed(owners):
        try:
            owner.stop(
                interrupt_seconds=0.5,
                terminate_seconds=0.5,
                kill_seconds=0.5,
            )
        except Exception:  # pragma: no cover - last-resort test hygiene
            pass
        session_id = getattr(owner, '_session_id', None)
        if session_id is None:
            continue
        try:
            remaining = owned_process_module._pids_in_session(
                session_id,
                os.getuid(),
            )
        except OwnedProcessError:  # pragma: no cover - /proc failure
            continue
        if not remaining:
            continue
        try:
            os.killpg(session_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_constructor_is_side_effect_free_and_repr_hides_private_values(
    tmp_path,
    monkeypatch,
):
    """Validation alone creates no process and exposes no launch material."""
    calls = []

    def unexpected_popen(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('construction must not start a process')

    monkeypatch.setattr(
        owned_process_module.subprocess,
        'Popen',
        unexpected_popen,
    )
    private_argument = 'private-command-argument'
    private_environment = 'private-environment-value'
    owner = OwnedProcess(
        'safe_owner',
        (str(_PYTHON), '-c', private_argument),
        cwd=tmp_path,
        environment={'PRIVATE_TOKEN': private_environment},
        maximum_output_bytes=4096,
    )

    rendered = repr(owner)
    assert calls == []
    assert owner.started is False
    assert owner.running is False
    assert owner.returncode is None
    assert rendered == "OwnedProcess(label='safe_owner', started=False)"
    assert private_argument not in rendered
    assert private_environment not in rendered
    assert str(tmp_path) not in rendered
    assert str(_PYTHON) not in rendered
    assert owner.output_evidence().bytes_observed == 0

    first_cleanup = owner.stop()
    assert owner.stop() is first_cleanup
    assert first_cleanup.process_started is False
    assert first_cleanup.cleanup_complete is True
    assert calls == []


@pytest.mark.parametrize(
    'label',
    ('', 'private label', '../owner', 'x' * 65, 42),
)
def test_constructor_rejects_unsafe_labels(label, tmp_path) -> None:
    """Labels are bounded identifiers and cannot carry private text."""
    with pytest.raises(
        OwnedProcessError,
        match='^process_config_invalid$',
    ):
        OwnedProcess(
            label,
            (str(_PYTHON), '-c', 'pass'),
            cwd=tmp_path,
            environment={},
            maximum_output_bytes=4096,
        )


@pytest.mark.parametrize(
    'environment',
    (
        {'BAD=KEY': 'value'},
        {'BAD\x00KEY': 'value'},
        {'KEY': 'bad\x00value'},
        {'KEY': 42},
    ),
)
def test_constructor_rejects_unsafe_environment(environment, tmp_path) -> None:
    """Invalid environment entries fail with one content-free code."""
    with pytest.raises(
        OwnedProcessError,
        match='^process_config_invalid$',
    ):
        OwnedProcess(
            'safe-owner',
            (str(_PYTHON), '-c', 'pass'),
            cwd=tmp_path,
            environment=environment,
            maximum_output_bytes=4096,
        )


@pytest.mark.parametrize('maximum', (True, 4095, 64 * 1024 * 1024 + 1))
def test_constructor_rejects_unbounded_output_limits(
    maximum,
    tmp_path,
) -> None:
    """Output retention limits are explicit bounded integers."""
    with pytest.raises(
        OwnedProcessError,
        match='^process_config_invalid$',
    ):
        OwnedProcess(
            'safe-owner',
            (str(_PYTHON), '-c', 'pass'),
            cwd=tmp_path,
            environment={},
            maximum_output_bytes=maximum,
        )


def test_constructor_rejects_relative_and_symlink_executables(
    tmp_path,
) -> None:
    """Only an absolute, non-symlink executable can be owned."""
    symlink = tmp_path / 'python-link'
    symlink.symlink_to(_PYTHON)
    for executable in ('python3', str(symlink)):
        with pytest.raises(
            OwnedProcessError,
            match='^process_config_invalid$',
        ):
            OwnedProcess(
                'safe-owner',
                (executable, '-c', 'pass'),
                cwd=tmp_path,
                environment={},
                maximum_output_bytes=4096,
            )


def test_sigint_stops_one_owned_session_without_forced_escalation(
    tmp_path,
    process_owners,
) -> None:
    """A cooperative child exits on SIGINT and drains digest-only output."""
    ready = tmp_path / 'cooperative-ready'
    source = """
from pathlib import Path
import signal
import time

def stop(_selected_signal, _frame):
    print('stopped', flush=True)
    raise SystemExit(0)

signal.signal(signal.SIGINT, stop)
Path(%r).write_text('ready', encoding='utf-8')
print('ready', flush=True)
while True:
    time.sleep(0.05)
""" % str(ready)
    owner = _python_owner(tmp_path, source)
    process_owners.append(owner)
    owner.start()
    _wait_until(ready.exists)

    assert owner.started is True
    owner.require_running()
    cleanup = owner.stop(
        interrupt_seconds=2.0,
        terminate_seconds=0.5,
        kill_seconds=0.5,
    )
    output = owner.output_evidence()

    assert cleanup.process_started is True
    assert cleanup.remaining_process_count == 0
    assert cleanup.forced_termination_count == 0
    assert cleanup.output_collector_stopped is True
    assert cleanup.output_overflowed is False
    assert cleanup.cleanup_complete is True
    assert owner.running is False
    assert owner.returncode == 0
    assert output.bytes_observed == output.bytes_hashed
    assert output.bytes_observed > 0
    assert len(output.digest) == 64
    assert 'ready' not in repr(output)
    assert 'stopped' not in repr(output)


def test_stop_cleans_descendant_after_session_leader_has_exited(
    tmp_path,
    process_owners,
) -> None:
    """Ownership follows the session when its original leader is gone."""
    ready = tmp_path / 'descendant-ready'
    descendant_source = """
from pathlib import Path
import signal
import time

def stop(_selected_signal, _frame):
    raise SystemExit(0)

signal.signal(signal.SIGINT, stop)
Path(%r).write_text('ready', encoding='utf-8')
print('descendant-ready', flush=True)
while True:
    time.sleep(0.05)
""" % str(ready)
    leader_source = """
import subprocess

subprocess.Popen([%r, '-c', %r])
raise SystemExit(0)
""" % (str(_PYTHON), descendant_source)
    owner = _python_owner(tmp_path, leader_source)
    process_owners.append(owner)
    owner.start()

    _wait_until(
        lambda: owner.returncode is not None
        and owner.running
        and ready.exists(),
    )
    cleanup = owner.stop(
        interrupt_seconds=2.0,
        terminate_seconds=0.5,
        kill_seconds=0.5,
    )

    assert cleanup.remaining_process_count == 0
    assert cleanup.forced_termination_count == 0
    assert cleanup.output_collector_stopped is True
    assert cleanup.cleanup_complete is True
    assert owner.running is False


def test_output_overflow_is_detected_bounded_and_fails_cleanup_closed(
    tmp_path,
    process_owners,
) -> None:
    """Only the bounded prefix is hashed and overflow cannot look healthy."""
    source = """
import os
import signal
import time

def stop(_selected_signal, _frame):
    raise SystemExit(0)

signal.signal(signal.SIGINT, stop)
os.write(1, b'x' * (128 * 1024))
while True:
    time.sleep(0.05)
"""
    owner = _python_owner(
        tmp_path,
        source,
        maximum_output_bytes=4096,
    )
    process_owners.append(owner)
    owner.start()
    _wait_until(lambda: owner.output_evidence().overflowed)

    with pytest.raises(
        OwnedProcessError,
        match='^process_output_overflow$',
    ):
        owner.require_running()

    cleanup = owner.stop(
        interrupt_seconds=2.0,
        terminate_seconds=0.5,
        kill_seconds=0.5,
    )
    output = owner.output_evidence()

    assert output.bytes_observed > 4096
    assert output.bytes_hashed == 4096
    assert output.digest == hashlib.sha256(b'x' * 4096).hexdigest()
    assert output.overflowed is True
    assert cleanup.remaining_process_count == 0
    assert cleanup.output_overflowed is True
    assert cleanup.cleanup_complete is False


def test_small_stalled_output_overflow_is_detected_before_eof(
    tmp_path,
    process_owners,
) -> None:
    """Do not wait for a 64 KiB buffer or EOF to enforce a 4 KiB cap."""
    source = """
import os
import signal
import time

signal.signal(signal.SIGINT, lambda _signal, _frame: exit(0))
os.write(1, b'x' * 8192)
while True:
    time.sleep(0.05)
"""
    owner = _python_owner(
        tmp_path,
        source,
        maximum_output_bytes=4096,
    )
    process_owners.append(owner)
    owner.start()

    _wait_until(lambda: owner.output_evidence().overflowed)

    with pytest.raises(OwnedProcessError, match='process_output_overflow'):
        owner.require_running()


def test_recycled_session_leader_fails_before_group_signal(
    tmp_path,
    monkeypatch,
) -> None:
    """Never signal a numeric SID whose leader PID has been recycled."""
    owner = _python_owner(tmp_path, 'pass')
    session_id = 4242
    owner._process = object()
    owner._session_id = session_id
    owner._leader_start_ticks = 100
    killpg_calls = []

    monkeypatch.setattr(
        owned_process_module,
        '_process_uid',
        lambda pid: os.getuid() if pid == session_id else None,
    )
    monkeypatch.setattr(
        owned_process_module,
        '_read_process_stat',
        lambda pid: owned_process_module._ProcessStat(
            process_group_id=pid,
            session_id=pid,
            start_ticks=101,
        ),
    )
    monkeypatch.setattr(
        owned_process_module,
        '_pids_in_session',
        lambda *_args: pytest.fail(
            'a recycled leader must fail before scanning session members'
        ),
    )
    monkeypatch.setattr(
        owned_process_module.os,
        'killpg',
        lambda *args: killpg_calls.append(args),
    )

    with pytest.raises(
        OwnedProcessError,
        match='^process_identity_unavailable$',
    ):
        owner.stop(
            interrupt_seconds=0.1,
            terminate_seconds=0.1,
            kill_seconds=0.1,
        )

    assert killpg_calls == []


def test_stop_is_idempotent_after_a_started_process(
    tmp_path,
    process_owners,
    monkeypatch,
) -> None:
    """A second stop returns the frozen receipt without another signal."""
    ready = tmp_path / 'idempotent-ready'
    source = """
from pathlib import Path
import signal
import time

signal.signal(signal.SIGINT, lambda _signal, _frame: exit(0))
Path(%r).write_text('ready', encoding='utf-8')
print('ready', flush=True)
while True:
    time.sleep(0.05)
""" % str(ready)
    owner = _python_owner(tmp_path, source)
    process_owners.append(owner)
    owner.start()
    _wait_until(ready.exists)
    first = owner.stop(
        interrupt_seconds=2.0,
        terminate_seconds=0.5,
        kill_seconds=0.5,
    )
    first_output = owner.output_evidence()

    def unexpected_signal(_selected_signal):
        raise AssertionError('idempotent stop must not signal again')

    monkeypatch.setattr(owner, '_signal_owned', unexpected_signal)
    second = owner.stop(
        interrupt_seconds=0.1,
        terminate_seconds=0.1,
        kill_seconds=0.1,
    )

    assert second is first
    assert second.cleanup_complete is True
    assert owner.output_evidence() == first_output


def test_source_launch_is_owned_session_and_has_no_broad_kill() -> None:
    """Static launch invariants prevent shell and machine-wide cleanup."""
    source = Path(owned_process_module.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    popen_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == 'subprocess'
        and node.func.attr == 'Popen'
    ]

    assert len(popen_calls) == 1
    popen = popen_calls[0]
    keywords = {keyword.arg: keyword.value for keyword in popen.keywords}
    assert isinstance(popen.args[0], ast.Call)
    assert isinstance(popen.args[0].func, ast.Name)
    assert popen.args[0].func.id == 'list'
    assert isinstance(keywords['shell'], ast.Constant)
    assert keywords['shell'].value is False
    assert isinstance(keywords['start_new_session'], ast.Constant)
    assert keywords['start_new_session'].value is True

    syntax_words = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            syntax_words.append(node.id)
        elif isinstance(node, ast.Attribute):
            syntax_words.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            syntax_words.append(node.value)
    normalized = '\n'.join(syntax_words).lower()
    assert 'killall' not in normalized
    assert 'pkill' not in normalized
