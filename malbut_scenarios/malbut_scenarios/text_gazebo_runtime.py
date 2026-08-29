"""Concrete read-only adapters for the SWM25-133 acceptance workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import http.client
import json
import math
from pathlib import Path
import re
import socket
import sqlite3
import stat
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote

from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboScenarioProfile,
    scenario_spec,
)


_MAX_HTTP_BODY_BYTES = 1_000_000
_MAX_FILE_BYTES = 16 * 1024 * 1024
_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class TextGazeboRuntimeError(RuntimeError):
    """Expose one stable code without retaining private payloads or paths."""

    _CODES = frozenset({
        'agent_http_unavailable',
        'agent_http_response_invalid',
        'agent_proposal_invalid',
        'agent_approval_invalid',
        'agent_replay_invalid',
        'agent_late_approval_invalid',
        'ledger_unavailable',
        'ledger_snapshot_invalid',
        'ledger_terminal_failed',
        'ledger_terminal_timeout',
        'installed_artifact_invalid',
        'runtime_binding_invalid',
        'loopback_port_unavailable',
    })

    def __init__(self, code: str) -> None:
        """Normalize failures to one public-safe runtime code."""
        normalized = (
            code if code in self._CODES else 'agent_http_response_invalid'
        )
        super().__init__(normalized)
        self.code = normalized


@dataclass(frozen=True, repr=False, slots=True)
class ProposalReceipt:
    """Private correlation for one validated public proposal response."""

    confirmation_request_id: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """Content-free projection of the fresh acceptance database."""

    confirmation_count: int
    approved_confirmation_count: int
    confirmation_state: Optional[str]
    confirmation_disposition: Optional[str]
    confirmation_result_code: Optional[str]
    robot_action_count: int
    action_state: Optional[str]
    action_result_code: Optional[str]
    dispatch_intent_count: int
    dispatch_state: Optional[str]
    dispatch_result_code: Optional[str]
    simulation: Optional[bool]
    physical_authorized: Optional[bool]

    def is_preapproval(self) -> bool:
        """Return the exact no-action state after proposal creation."""
        return bool(
            self.confirmation_count == 1
            and self.approved_confirmation_count == 0
            and self.confirmation_state == 'pending'
            and self.confirmation_disposition == 'pending'
            and self.confirmation_result_code == 'confirmation_pending'
            and self.robot_action_count == 0
            and self.action_state is None
            and self.dispatch_intent_count == 0
            and self.dispatch_state is None
            and self.simulation is None
            and self.physical_authorized is None
        )

    def is_known_success(self) -> bool:
        """Return true only for the exact sealed success projection."""
        return bool(
            self.confirmation_count == 1
            and self.approved_confirmation_count == 1
            and self.confirmation_state == 'resolved'
            and self.confirmation_disposition == 'approved'
            and self.confirmation_result_code == 'confirmation_approved'
            and self.robot_action_count == 1
            and self.action_state == 'SUCCEEDED'
            and self.action_result_code == 'NAVIGATION_SUCCEEDED'
            and self.dispatch_intent_count == 1
            and self.dispatch_state == 'TERMINAL'
            and self.dispatch_result_code == 'NAVIGATION_SUCCEEDED'
            and self.simulation is True
            and self.physical_authorized is False
        )


class TextAgentHTTPClient:
    """Drive only the authenticated conversation and text-turn endpoints."""

    def __init__(
        self,
        port: int,
        *,
        token: str,
        user_id: str,
        run_nonce: str,
        timeout_seconds: float = 5.0,
        scenario_profile: TextGazeboScenarioProfile | str = (
            TextGazeboScenarioProfile.HAPPY_PATH
        ),
    ) -> None:
        """Validate private identity without opening a socket."""
        scenario = scenario_spec(scenario_profile)
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise ValueError('Agent HTTP port is invalid')
        for value, label, maximum in (
            (token, 'token', 512),
            (user_id, 'user_id', 128),
            (run_nonce, 'run_nonce', 64),
        ):
            if (
                type(value) is not str
                or not value
                or len(value) > maximum
                or any(ord(character) < 33 or ord(character) > 126
                       for character in value)
            ):
                raise ValueError(f'{label} is invalid')
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.0 < float(timeout_seconds) <= 60.0
        ):
            raise ValueError('Agent HTTP timeout is invalid')
        self._port = port
        self._token = token
        self._user_id = user_id
        self._conversation_id = 'swm25-133-' + run_nonce
        self._timeout_seconds = float(timeout_seconds)
        self._request_body = {
            'request_id': 'request-' + run_nonce,
            'conversation_id': self._conversation_id,
            'turn_id': 'turn-request-' + run_nonce,
            'text': scenario.request_text,
        }
        self._approval_body = {
            'request_id': 'approval-' + run_nonce,
            'conversation_id': self._conversation_id,
            'turn_id': 'turn-approval-' + run_nonce,
            'text': '네',
        }
        self._late_body = {
            'request_id': 'late-' + run_nonce,
            'conversation_id': self._conversation_id,
            'turn_id': 'turn-late-' + run_nonce,
            'text': '네',
        }
        self._scenario = scenario

    def __repr__(self) -> str:
        """Do not render token, port, text, or private request identities."""
        return 'TextAgentHTTPClient(configured=True)'

    def await_health(self, timeout_seconds: float) -> None:
        """Wait for the public content-free health contract."""
        deadline = time.monotonic() + _bounded_seconds(timeout_seconds)
        while True:
            try:
                status, value = self._request('GET', '/healthz', None)
                if status == 200 and value == {
                    'status': 'ok',
                    'service': 'malbut_agent_server',
                }:
                    return
            except TextGazeboRuntimeError:
                pass
            if time.monotonic() >= deadline:
                raise TextGazeboRuntimeError('agent_http_unavailable')
            time.sleep(0.1)

    def create_conversation(self) -> None:
        """Create the fresh owner-scoped conversation over public HTTP."""
        status, value = self._request(
            'POST',
            '/v1/conversations',
            {
                'user_id': self._user_id,
                'conversation_id': self._conversation_id,
            },
        )
        try:
            conversation = value['conversation']
            valid = bool(
                status == 201
                and type(conversation) is dict
                and conversation.get('conversation_id')
                == self._conversation_id
                and conversation.get('user_id') == self._user_id
                and conversation.get('generation') == 1
            )
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise TextGazeboRuntimeError('agent_http_response_invalid')

    def request_navigation(self) -> ProposalReceipt:
        """Send one natural-language request and validate its proposal."""
        status, value = self._request(
            'POST', '/v1/text/turns', self._request_body
        )
        execution = value.get('execution')
        proposal = value.get('proposal')
        confirmation_id = value.get('confirmation_request_id')
        if not (
            status == 200
            and value.get('status') == 'awaiting_confirmation'
            and value.get('result_code') == 'confirmation_pending'
            and value.get('cached') is False
            and type(proposal) is dict
            and proposal.get('tool_name') == 'navigate'
            and proposal.get('arguments') == {
                'location': self._scenario.location,
            }
            and _non_authorizing(execution)
            and _private_identifier(confirmation_id)
        ):
            raise TextGazeboRuntimeError('agent_proposal_invalid')
        return ProposalReceipt(str(confirmation_id))

    def approve_navigation(self) -> None:
        """Resolve the pending confirmation without claiming execution."""
        status, value = self._request(
            'POST', '/v1/text/turns', self._approval_body
        )
        if not (
            status == 200
            and value.get('status') == 'approved'
            and value.get('result_code') == 'confirmation_approved'
            and value.get('cached') is False
            and _non_authorizing(value.get('execution'))
        ):
            raise TextGazeboRuntimeError('agent_approval_invalid')

    def replay_approval(self) -> None:
        """Replay the exact approval and require the cached result."""
        status, value = self._request(
            'POST', '/v1/text/turns', self._approval_body
        )
        if not (
            status == 200
            and value.get('status') == 'approved'
            and value.get('result_code') == 'confirmation_approved'
            and value.get('cached') is True
            and _non_authorizing(value.get('execution'))
        ):
            raise TextGazeboRuntimeError('agent_replay_invalid')

    def send_late_approval(self) -> None:
        """Require a new late yes to remain non-authorizing."""
        status, value = self._request(
            'POST', '/v1/text/turns', self._late_body
        )
        if not (
            status == 200
            and value.get('status') == 'no_pending_confirmation'
            and value.get('result_code') == 'confirmation_not_pending'
            and _non_authorizing(value.get('execution'))
        ):
            raise TextGazeboRuntimeError(
                'agent_late_approval_invalid'
            )

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        payload = None
        headers = {'Accept': 'application/json'}
        if body is not None:
            payload = json.dumps(
                body,
                ensure_ascii=False,
                allow_nan=False,
                separators=(',', ':'),
            ).encode('utf-8')
            headers.update({
                'Authorization': 'Bearer ' + self._token,
                'Content-Type': 'application/json',
            })
        connection = http.client.HTTPConnection(
            '127.0.0.1',
            self._port,
            timeout=self._timeout_seconds,
        )
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            content_type = response.getheader('Content-Type', '')
            if content_type.split(';', 1)[0].strip() != 'application/json':
                raise TextGazeboRuntimeError(
                    'agent_http_response_invalid'
                )
            declared = response.getheader('Content-Length')
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError:
                    raise TextGazeboRuntimeError(
                        'agent_http_response_invalid'
                    ) from None
                if not 0 <= declared_size <= _MAX_HTTP_BODY_BYTES:
                    raise TextGazeboRuntimeError(
                        'agent_http_response_invalid'
                    )
            raw = response.read(_MAX_HTTP_BODY_BYTES + 1)
            if len(raw) > _MAX_HTTP_BODY_BYTES:
                raise TextGazeboRuntimeError(
                    'agent_http_response_invalid'
                )
            return response.status, _strict_json_object(raw)
        except TextGazeboRuntimeError:
            raise
        except (
            ConnectionError,
            OSError,
            TimeoutError,
            http.client.HTTPException,
        ) as error:
            raise TextGazeboRuntimeError(
                'agent_http_unavailable'
            ) from error
        finally:
            connection.close()


class SQLiteAcceptanceObserver:
    """Observe the Agent ledger using SELECT-only short connections."""

    def __init__(self, database: Path) -> None:
        """Bind one new private database path without opening it."""
        if not isinstance(database, Path) or not database.is_absolute():
            raise ValueError('acceptance database path is invalid')
        self._database = database

    def __repr__(self) -> str:
        """Keep the private database path out of diagnostics."""
        return 'SQLiteAcceptanceObserver(configured=True)'

    def snapshot(self, confirmation_request_id: str) -> LedgerSnapshot:
        """Read the exact confirmation, Action, and outbox projection."""
        if not _private_identifier(confirmation_request_id):
            raise TextGazeboRuntimeError('ledger_snapshot_invalid')
        try:
            with self._connect() as connection:
                confirmation_count = _scalar_count(
                    connection,
                    'SELECT COUNT(*) FROM confirmation_intents',
                )
                approved_count = _scalar_count(
                    connection,
                    "SELECT COUNT(*) FROM confirmation_intents "
                    "WHERE state = 'resolved' AND disposition = 'approved'",
                )
                confirmation = connection.execute(
                    'SELECT state, disposition, result_code '
                    'FROM confirmation_intents '
                    'WHERE confirmation_request_id = ?',
                    (confirmation_request_id,),
                ).fetchone()
                action_count = _scalar_count(
                    connection,
                    'SELECT COUNT(*) FROM robot_actions',
                )
                action = connection.execute(
                    'SELECT action_id, state, result_code, simulation, '
                    'physical_authorized FROM robot_actions '
                    'WHERE confirmation_request_id = ?',
                    (confirmation_request_id,),
                ).fetchone()
                outbox_count = _scalar_count(
                    connection,
                    'SELECT COUNT(*) FROM execution_outbox',
                )
                outbox = None
                if action is not None:
                    outbox = connection.execute(
                        'SELECT state, result_code, simulation, '
                        'physical_authorized FROM execution_outbox '
                        'WHERE action_id = ?',
                        (action['action_id'],),
                    ).fetchone()
        except (OSError, sqlite3.Error) as error:
            raise TextGazeboRuntimeError('ledger_unavailable') from error
        if confirmation is None or confirmation_count != 1:
            raise TextGazeboRuntimeError('ledger_snapshot_invalid')
        simulation = None
        physical = None
        if action is not None:
            simulation = bool(action['simulation'])
            physical = bool(action['physical_authorized'])
            if outbox is not None and (
                bool(outbox['simulation']) != simulation
                or bool(outbox['physical_authorized']) != physical
            ):
                raise TextGazeboRuntimeError('ledger_snapshot_invalid')
        return LedgerSnapshot(
            confirmation_count=confirmation_count,
            approved_confirmation_count=approved_count,
            confirmation_state=confirmation['state'],
            confirmation_disposition=confirmation['disposition'],
            confirmation_result_code=confirmation['result_code'],
            robot_action_count=action_count,
            action_state=(action['state'] if action is not None else None),
            action_result_code=(
                action['result_code'] if action is not None else None
            ),
            dispatch_intent_count=outbox_count,
            dispatch_state=(outbox['state'] if outbox is not None else None),
            dispatch_result_code=(
                outbox['result_code'] if outbox is not None else None
            ),
            simulation=simulation,
            physical_authorized=physical,
        )

    def await_known_success(
        self,
        confirmation_request_id: str,
        *,
        timeout_seconds: float,
        poll_seconds: float = 0.25,
    ) -> LedgerSnapshot:
        """Poll until exact known success, never converting UNKNOWN to pass."""
        deadline = time.monotonic() + _bounded_seconds(timeout_seconds)
        poll = _bounded_seconds(poll_seconds)
        terminal_failures = {
            'FAILED', 'CANCELED', 'BLOCKED', 'UNKNOWN',
        }
        while True:
            snapshot = self.snapshot(confirmation_request_id)
            if snapshot.is_known_success():
                return snapshot
            if snapshot.action_state in terminal_failures:
                raise TextGazeboRuntimeError('ledger_terminal_failed')
            if time.monotonic() >= deadline:
                raise TextGazeboRuntimeError('ledger_terminal_timeout')
            time.sleep(min(poll, max(0.0, deadline - time.monotonic())))

    def quick_check(self) -> bool:
        """Run SQLite's read-only integrity check after the writer exits."""
        try:
            with self._connect() as connection:
                row = connection.execute('PRAGMA quick_check').fetchone()
        except sqlite3.Error as error:
            raise TextGazeboRuntimeError('ledger_unavailable') from error
        return row is not None and row[0] == 'ok'

    def _connect(self) -> sqlite3.Connection:
        encoded = quote(str(self._database), safe='/')
        connection = sqlite3.connect(
            f'file:{encoded}?mode=ro',
            uri=True,
            timeout=0.2,
        )
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only = ON')
        connection.execute('PRAGMA trusted_schema = OFF')
        return connection


class LoopbackPortReservation:
    """Hold one random loopback port until its intended owner starts."""

    def __init__(self) -> None:
        """Reserve one kernel-selected local port without listening."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(('127.0.0.1', 0))
        self._port = int(self._socket.getsockname()[1])
        self._released = False

    @property
    def port(self) -> int:
        """Return the reserved local TCP port."""
        return self._port

    def release(self) -> None:
        """Release the reservation idempotently."""
        if not self._released:
            self._socket.close()
            self._released = True

    def __enter__(self) -> 'LoopbackPortReservation':
        """Return this reservation for a bounded context."""
        return self

    def __exit__(self, *_args) -> None:
        """Release the reservation when leaving its context."""
        self.release()


def loopback_listener_present(port: int) -> bool:
    """Return whether a TCP listener currently accepts local connections."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('loopback port is invalid')
    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    candidate.settimeout(0.2)
    try:
        return candidate.connect_ex(('127.0.0.1', port)) == 0
    finally:
        candidate.close()


def runtime_binding_digest(
    *,
    device_id: str,
    map_id: str,
    map_revision: str,
) -> str:
    """Digest private semantic identity without rendering its fields."""
    values = (device_id, map_id, map_revision)
    if any(
        type(value) is not str
        or not value
        or len(value) > 256
        or any(ord(character) < 32 for character in value)
        for value in values
    ):
        raise TextGazeboRuntimeError('runtime_binding_invalid')
    return _canonical_digest(
        'malbut-swm25-133-runtime-binding-v1',
        {
            'device_id': device_id,
            'map_id': map_id,
            'map_revision': map_revision,
        },
    )


def installed_artifact_digest(files: Mapping[str, Path]) -> str:
    """Attest a small logical set without returning installed host paths."""
    if (
        not isinstance(files, Mapping)
        or not files
        or any(
            type(label) is not str
            or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', label)
            or not isinstance(path, Path)
            for label, path in files.items()
        )
    ):
        raise TextGazeboRuntimeError('installed_artifact_invalid')
    records = []
    for label, candidate in sorted(files.items()):
        if not candidate.is_absolute() or candidate.is_symlink():
            raise TextGazeboRuntimeError('installed_artifact_invalid')
        try:
            resolved = candidate.resolve(strict=True)
            before = resolved.stat()
            if (
                not stat.S_ISREG(before.st_mode)
                or not 0 < before.st_size <= _MAX_FILE_BYTES
            ):
                raise TextGazeboRuntimeError(
                    'installed_artifact_invalid'
                )
            digest = hashlib.sha256()
            total = 0
            with resolved.open('rb') as stream:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_FILE_BYTES:
                        raise TextGazeboRuntimeError(
                            'installed_artifact_invalid'
                        )
                    digest.update(chunk)
            after = resolved.stat()
        except (OSError, RuntimeError) as error:
            if isinstance(error, TextGazeboRuntimeError):
                raise
            raise TextGazeboRuntimeError(
                'installed_artifact_invalid'
            ) from error
        if (
            total != before.st_size
            or (before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns)
        ):
            raise TextGazeboRuntimeError('installed_artifact_invalid')
        records.append({
            'logical_name': label,
            'size': total,
            'sha256': digest.hexdigest(),
        })
    return _canonical_digest(
        'malbut-swm25-133-installed-artifacts-v1', records
    )


def sanitized_ros_environment(
    source: Mapping[str, str],
    *,
    private_home: Path,
    domain_id: int,
    gui: bool,
) -> dict[str, str]:
    """Copy only runtime plumbing and omit application credentials."""
    if type(domain_id) is not int or not 1 <= domain_id <= 100:
        raise ValueError('ROS domain ID must be isolated')
    allowed_exact = {
        'AMENT_PREFIX_PATH',
        'CMAKE_PREFIX_PATH',
        'COLCON_PREFIX_PATH',
        'LD_LIBRARY_PATH',
        'PATH',
        'PYTHONPATH',
        'LANG',
        'LC_ALL',
        'ROS_DISTRO',
        'ROS_PYTHON_VERSION',
        'ROS_VERSION',
        'RMW_IMPLEMENTATION',
        'IGN_GAZEBO_RESOURCE_PATH',
        'IGN_GUI_PLUGIN_PATH',
        'IGN_PARTITION',
        'GZ_SIM_RESOURCE_PATH',
        'QT_PLUGIN_PATH',
    }
    if gui:
        allowed_exact.update({'DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR'})
    result = {
        key: value
        for key, value in source.items()
        if key in allowed_exact
    }
    original_home = source.get('HOME')
    if isinstance(original_home, str) and original_home:
        result['HOME'] = original_home
    result.update({
        'ROS_HOME': str(private_home / 'ros'),
        'XDG_CACHE_HOME': str(private_home / 'cache'),
        'XDG_CONFIG_HOME': str(private_home / 'config'),
        'ROS_DOMAIN_ID': str(domain_id),
        'ROS_LOCALHOST_ONLY': '1',
        'ROS2CLI_NO_DAEMON': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    return result


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError

    try:
        value = json.loads(
            raw.decode('utf-8'),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise TextGazeboRuntimeError(
            'agent_http_response_invalid'
        ) from error
    if type(value) is not dict:
        raise TextGazeboRuntimeError('agent_http_response_invalid')
    return value


def _non_authorizing(value: object) -> bool:
    return bool(
        type(value) is dict
        and value.get('authorized') is False
        and value.get('execution_authorized') is False
        and value.get('consume_once') is False
        and value.get('tool_call_id') is None
        and value.get('physical_authorized') is False
        and value.get('nav2_start_count') == 0
        and value.get('nav2_cancel_count') == 0
    )


def _private_identifier(value: object) -> bool:
    return bool(
        type(value) is str
        and 1 <= len(value) <= 256
        and not any(ord(character) < 32 or ord(character) == 127
                    for character in value)
    )


def _scalar_count(connection: sqlite3.Connection, statement: str) -> int:
    row = connection.execute(statement).fetchone()
    if (
        row is None
        or isinstance(row[0], bool)
        or not isinstance(row[0], int)
        or row[0] < 0
    ):
        raise TextGazeboRuntimeError('ledger_snapshot_invalid')
    return int(row[0])


def _bounded_seconds(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 < float(value) <= 600.0
    ):
        raise ValueError('runtime timeout is invalid')
    return float(value)


def _canonical_digest(contract: str, value: object) -> str:
    payload = json.dumps(
        {'contract': contract, 'value': value},
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('ascii')
    result = hashlib.sha256(payload).hexdigest()
    if _DIGEST.fullmatch(result) is None:
        raise AssertionError('SHA-256 invariant failed')
    return result


__all__ = [
    'LedgerSnapshot',
    'LoopbackPortReservation',
    'ProposalReceipt',
    'SQLiteAcceptanceObserver',
    'TextAgentHTTPClient',
    'TextGazeboRuntimeError',
    'installed_artifact_digest',
    'loopback_listener_present',
    'runtime_binding_digest',
    'sanitized_ros_environment',
]
