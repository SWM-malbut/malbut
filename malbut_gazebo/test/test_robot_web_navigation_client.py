"""Contract tests for the local Robot Web navigation HTTP client."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Thread
import urllib.request

import pytest

from malbut_gazebo.robot_web_navigation_client import (
    NavigationTimeouts,
    RobotWebConfigurationError,
    RobotWebHTTPError,
    RobotWebNavigationClient,
    RobotWebOutcomeUnknown,
    RobotWebProtocolError,
)


DEVICE_ID = "malbut-sim-01"
TARGET_BINDING_DIGEST = "c" * 64
OTHER_TARGET_BINDING_DIGEST = "d" * 64


class _State:
    def __init__(self) -> None:
        self.calls = []
        self.preview_error = None
        self.redirect_start = False
        self.drop_start = False
        self.drop_cancel = False
        self.invalid_start_json = False
        self.invalid_start_shape = False
        self.invalid_cancel_shape = False
        self.start_error = None
        self.cancel_error = None
        self.sink_calls = 0
        self.status_map_id = "map-home"
        self.status_map_revision = "map-revision-1"
        self.status_error = None
        self.status_navigation = {
            "state": "driving",
            "session_id": "navigation-session-secret",
            "progress_ratio": 0.25,
            "message_code": None,
            "message": "moving",
            "goal": {"x": 1.25, "y": -0.5},
        }
        self.raw_status = None
        self.editor_config = {
            "map_id": "map-home",
            "map_revision": "map-revision-1",
            "navigation_enabled": True,
            "device_id": DEVICE_ID,
            "simulation": True,
            "csrf_token": "a" * 64,
        }


class _Handler(BaseHTTPRequestHandler):
    state: _State
    csrf = "a" * 64

    def log_message(self, _format, *_arguments) -> None:
        pass

    def _json(self, status: int, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _raw_json(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self.state.calls.append(("GET", self.path, None, dict(self.headers)))
        if self.path == "/api/editor-config":
            self.send_response(200)
            payload = json.dumps(
                self.state.editor_config,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header(
                "Set-Cookie",
                "malbut_editor_session=editor-session-secret; "
                "Path=/; HttpOnly; SameSite=Strict",
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/api/robot/status":
            if self.state.status_error is not None:
                self._json(self.state.status_error, {
                    "error_code": "STATUS_UNAVAILABLE",
                    "message": "server failure",
                })
                return
            if self.state.raw_status is not None:
                self._raw_json(200, self.state.raw_status)
            else:
                self._json(200, {
                    "map_id": self.state.status_map_id,
                    "map_revision": self.state.status_map_revision,
                    "navigation": self.state.status_navigation,
                })
            return
        if self.path == "/sink":
            self.state.sink_calls += 1
            self._json(200, {})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        body = json.loads(payload)
        self.state.calls.append(
            ("POST", self.path, body, dict(self.headers))
        )
        if self.path == "/api/navigation/preview":
            if self.state.preview_error is not None:
                status, value = self.state.preview_error
                self._json(status, value)
                return
            self._json(200, {
                "preview_token": "preview-token-secret",
                "expires_in_s": 30.0,
                "resolved": {"x": 1.25, "y": -0.5, "yaw": 0.75},
                "path": {"length_m": 2.5, "points": [[1.25, -0.5]]},
            })
            return
        if self.path == "/api/navigation/start":
            if self.state.start_error is not None:
                self._json(self.state.start_error, {
                    "error_code": "NAVIGATION_REJECTED",
                    "message": "server failure",
                })
                return
            if self.state.redirect_start:
                self.send_response(307)
                self.send_header("Location", "/sink")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.state.drop_start:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.state.invalid_start_json:
                self._raw_json(202, b'{"state":"driving","state":"failed"}')
                return
            if self.state.invalid_start_shape:
                self._json(202, {"state": "driving"})
                return
            self._json(202, {
                "session_id": "navigation-session-secret",
                "state": "driving",
                "goal": {"x": 1.25, "y": -0.5},
            })
            return
        if self.path == "/api/navigation/cancel":
            if self.state.cancel_error is not None:
                self._json(self.state.cancel_error, {
                    "error_code": "NAV2_TIMEOUT",
                    "message": "server failure",
                })
                return
            if self.state.drop_cancel:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.state.invalid_cancel_shape:
                self._json(200, {
                    "session_id": "navigation-session-secret",
                    "state": "unexpected 1.25,-0.5",
                    "already_terminal": False,
                })
                return
            self._json(200, {
                "session_id": "navigation-session-secret",
                "state": "canceling",
                "already_terminal": False,
            })
            return
        if self.path == "/sink":
            self.state.sink_calls += 1
            self._json(200, {})
            return
        self._json(404, {"error": "not found"})


@contextmanager
def _robot_web_server():
    state = _State()

    class Handler(_Handler):
        pass

    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _bootstrapped(origin: str) -> RobotWebNavigationClient:
    client = RobotWebNavigationClient(origin)
    config = client.bootstrap()
    assert config.map_id == "map-home"
    assert config.map_revision == "map-revision-1"
    assert config.navigation_enabled is True
    assert config.device_id == DEVICE_ID
    assert config.simulation is True
    return client


def _preview(
    client: RobotWebNavigationClient,
    **overrides,
):
    """Create one valid private-bound preview unless a test overrides it."""
    values = {
        "map_id": "map-home",
        "map_revision": "map-revision-1",
        "x": 1.25,
        "y": -0.5,
        "target_binding_digest": TARGET_BINDING_DIGEST,
    }
    values.update(overrides)
    return client.preview(**values)


@contextmanager
def _hostile_proxy():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_arguments) -> None:
            pass

        def do_GET(self) -> None:
            calls.append(("GET", self.path))
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            calls.append(("POST", self.path))
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("url", [
    "http://localhost:8765",
    "https://127.0.0.1:8765",
    "http://192.0.2.1:8765",
    "http://user@127.0.0.1:8765",
    "http://127.0.0.1:8765/api",
    "http://127.0.0.1:8765/?next=elsewhere",
    "http://127.0.0.1:0",
    "http://[::1%25lo]:8765",
])
def test_base_url_requires_a_literal_http_loopback_origin(url):
    with pytest.raises(RobotWebConfigurationError):
        RobotWebNavigationClient(url)


def test_timeout_and_response_bounds_reject_unsafe_configuration():
    with pytest.raises(ValueError):
        NavigationTimeouts(start_s=0)
    with pytest.raises(ValueError):
        NavigationTimeouts(cancel_s=61)
    with pytest.raises(ValueError):
        RobotWebNavigationClient("http://127.0.0.1", max_response_bytes=0)


@pytest.mark.parametrize(
    ("field", "invalid_value", "remove", "expected_code"),
    [
        ("device_id", None, True, "INVALID_DEVICE_ID"),
        ("device_id", "", False, "INVALID_DEVICE_ID"),
        ("device_id", 7, False, "INVALID_DEVICE_ID"),
        ("simulation", None, True, "INVALID_SIMULATION"),
        ("simulation", None, False, "INVALID_SIMULATION"),
        ("simulation", 1, False, "INVALID_SIMULATION"),
    ],
)
def test_bootstrap_rejects_missing_or_invalid_runtime_identity_shape(
    field, invalid_value, remove, expected_code
):
    with _robot_web_server() as (origin, state):
        if remove:
            state.editor_config.pop(field)
        else:
            state.editor_config[field] = invalid_value
        client = RobotWebNavigationClient(origin)
        with pytest.raises(RobotWebProtocolError) as caught:
            client.bootstrap()

    assert caught.value.code == expected_code


def test_environment_proxies_are_disabled_even_when_hostile(monkeypatch):
    with _hostile_proxy() as (proxy_origin, proxy_calls):
        monkeypatch.setenv("HTTP_PROXY", proxy_origin)
        monkeypatch.setenv("HTTPS_PROXY", proxy_origin)
        monkeypatch.setenv("http_proxy", proxy_origin)
        monkeypatch.setenv("https_proxy", proxy_origin)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.setattr(
            urllib.request, "proxy_bypass", lambda _host: False
        )
        with _robot_web_server() as (origin, _state):
            client = _bootstrapped(origin)
            status = client.status()

    assert status.state == "driving"
    assert proxy_calls == []


def test_happy_path_preserves_cookie_csrf_map_and_opaque_values():
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        preview = _preview(
            client,
            user_map_digest="b" * 64,
        )
        session = client.start(preview)
        raw_status = client.status()
        status = client.status_for(session)
        canceled = client.cancel(session)

    assert preview.matches_target_binding(TARGET_BINDING_DIGEST)
    assert session.matches_target_binding(TARGET_BINDING_DIGEST)
    assert session.state == "driving"
    assert status.state == "driving"
    assert status.session is not None
    assert status.progress_ratio == 0.25
    assert status.terminal is False
    assert not raw_status.belongs_to(session)
    assert status.belongs_to(session)
    assert canceled.state == "canceling"
    assert canceled.already_terminal is False

    posts = [call for call in state.calls if call[0] == "POST"]
    assert [call[1] for call in posts] == [
        "/api/navigation/preview",
        "/api/navigation/start",
        "/api/navigation/cancel",
    ]
    for _, _, _, headers in posts:
        assert headers["Origin"] == origin
        assert headers["X-Csrf-Token"] == "a" * 64
        assert headers["Content-Type"] == "application/json"
        assert headers["Cookie"] == (
            "malbut_editor_session=editor-session-secret"
        )
        assert headers["Host"] == origin.removeprefix("http://")
    assert posts[0][2] == {
        "map_id": "map-home",
        "map_revision": "map-revision-1",
        "x": 1.25,
        "y": -0.5,
        "user_map_digest": "b" * 64,
    }
    assert posts[1][2] == {"preview_token": "preview-token-secret"}
    assert posts[2][2] == {"session_id": "navigation-session-secret"}
    rendered_bodies = json.dumps(
        [call[2] for call in state.calls],
        separators=(",", ":"),
    )
    assert TARGET_BINDING_DIGEST not in rendered_bodies

    rendered = " ".join(map(repr, (
        client,
        preview,
        session,
        raw_status,
        raw_status.session,
        status,
        status.session,
        canceled,
    )))
    for secret in (
        "preview-token-secret",
        "navigation-session-secret",
        "editor-session-secret",
        "1.25",
        "-0.5",
        "0.75",
        TARGET_BINDING_DIGEST,
    ):
        assert secret not in rendered


@pytest.mark.parametrize(
    "target_binding_digest",
    [None, "", "c" * 63, "C" * 64, 7],
)
def test_preview_rejects_invalid_target_binding_before_http(
    target_binding_digest,
):
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        initial_calls = len(state.calls)
        with pytest.raises(
            RobotWebConfigurationError,
            match="target binding",
        ):
            _preview(
                client,
                target_binding_digest=target_binding_digest,
            )

        assert len(state.calls) == initial_calls


def test_same_session_id_cannot_cross_private_target_bindings():
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        first_preview = _preview(client)
        second_preview = _preview(
            client,
            target_binding_digest=OTHER_TARGET_BINDING_DIGEST,
        )
        first_session = client.start(first_preview)
        second_session = client.start(second_preview)
        first_status = client.status_for(first_session)

    assert first_preview.matches_target_binding(TARGET_BINDING_DIGEST)
    assert not first_preview.matches_target_binding(
        OTHER_TARGET_BINDING_DIGEST
    )
    assert second_preview.matches_target_binding(
        OTHER_TARGET_BINDING_DIGEST
    )
    assert first_session.matches_target_binding(TARGET_BINDING_DIGEST)
    assert second_session.matches_target_binding(
        OTHER_TARGET_BINDING_DIGEST
    )
    assert first_status.belongs_to(first_session)
    assert not first_status.belongs_to(second_session)

    rendered_bodies = json.dumps(
        [call[2] for call in state.calls],
        separators=(",", ":"),
    )
    rendered_handles = " ".join(map(repr, (
        first_preview,
        second_preview,
        first_session,
        second_session,
        first_status,
    )))
    for target_binding_digest in (
        TARGET_BINDING_DIGEST,
        OTHER_TARGET_BINDING_DIGEST,
    ):
        assert target_binding_digest not in rendered_bodies
        assert target_binding_digest not in rendered_handles


def test_preview_rejects_stale_map_and_nonfinite_coordinate_before_http():
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        initial_calls = len(state.calls)
        with pytest.raises(RobotWebConfigurationError, match="stale"):
            _preview(
                client,
                map_id="wrong",
                x=1.0,
                y=2.0,
            )
        with pytest.raises(RobotWebProtocolError):
            _preview(
                client,
                x=float("nan"),
                y=2.0,
            )
        with pytest.raises(RobotWebConfigurationError, match="User Map"):
            _preview(
                client,
                x=1.0,
                y=2.0,
                user_map_digest="invalid",
            )
        assert len(state.calls) == initial_calls


def test_preview_http_error_is_structured_without_server_message_exposure():
    with _robot_web_server() as (origin, state):
        state.preview_error = (422, {
            "error_code": "GOAL_OUTSIDE_MAP",
            "message": "unsafe coordinate 1.25,-0.5",
        })
        client = _bootstrapped(origin)
        with pytest.raises(RobotWebHTTPError) as caught:
            _preview(client)

    assert caught.value.http_status == 422
    assert caught.value.error_code == "GOAL_OUTSIDE_MAP"
    assert "1.25" not in str(caught.value)
    assert "-0.5" not in str(caught.value)


def test_start_redirect_is_rejected_not_followed_and_outcome_is_unknown():
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        preview = _preview(client)
        state.redirect_start = True
        with pytest.raises(RobotWebOutcomeUnknown) as caught:
            client.start(preview)

    assert caught.value.operation == "start"
    assert caught.value.cause_code == "REDIRECT_REJECTED"
    assert state.sink_calls == 0
    assert sum(
        call[1] == "/api/navigation/start" for call in state.calls
    ) == 1


@pytest.mark.parametrize("failure", ["drop", "invalid_json", "invalid_shape"])
def test_start_has_zero_automatic_retries_and_reports_ambiguity(failure):
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        preview = _preview(client)
        if failure == "drop":
            state.drop_start = True
        elif failure == "invalid_json":
            state.invalid_start_json = True
        else:
            state.invalid_start_shape = True
        with pytest.raises(RobotWebOutcomeUnknown) as caught:
            client.start(preview)
        with pytest.raises(RobotWebConfigurationError, match="consumed"):
            client.start(preview)

    assert caught.value.operation == "start"
    assert sum(
        call[1] == "/api/navigation/start" for call in state.calls
    ) == 1


def test_cancel_has_zero_automatic_retries_and_reports_ambiguity():
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        preview = _preview(client)
        session = client.start(preview)
        state.drop_cancel = True
        with pytest.raises(RobotWebOutcomeUnknown) as caught:
            client.cancel(session)

    assert caught.value.operation == "cancel"
    assert sum(
        call[1] == "/api/navigation/cancel" for call in state.calls
    ) == 1


def test_cancel_invalid_success_shape_is_unknown_and_redacted():
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        preview = _preview(client)
        session = client.start(preview)
        state.invalid_cancel_shape = True
        with pytest.raises(RobotWebOutcomeUnknown) as caught:
            client.cancel(session)

    assert caught.value.operation == "cancel"
    assert "1.25" not in str(caught.value)
    assert "-0.5" not in str(caught.value)


def test_status_strict_json_rejects_duplicate_keys_and_large_payload():
    with _robot_web_server() as (origin, state):
        client = RobotWebNavigationClient(origin, max_response_bytes=512)
        client.bootstrap()
        state.raw_status = (
            b'{"navigation":{"state":"idle","state":"driving"}}'
        )
        with pytest.raises(RobotWebProtocolError) as duplicate:
            client.status()
        assert duplicate.value.code == "INVALID_JSON"

        state.raw_status = json.dumps({
            "navigation": {"state": "idle"},
            "padding": "x" * 600,
        }).encode("utf-8")
        with pytest.raises(RobotWebProtocolError) as oversized:
            client.status()
        assert oversized.value.code == "RESPONSE_TOO_LARGE"


@pytest.mark.parametrize(
    ("map_id", "map_revision", "cause_code"),
    [
        ("other-map", "map-revision-1", "MAP_ID_MISMATCH"),
        ("map-home", "other-revision", "MAP_REVISION_MISMATCH"),
    ],
)
def test_status_rejects_top_level_map_identity_change(
    map_id, map_revision, cause_code
):
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        state.status_map_id = map_id
        state.status_map_revision = map_revision
        with pytest.raises(RobotWebOutcomeUnknown) as caught:
            client.status()

    assert caught.value.operation == "status"
    assert caught.value.cause_code == cause_code


@pytest.mark.parametrize(
    ("state_name", "session_id", "cause_code"),
    [
        ("driving", None, "SESSION_MISSING"),
        ("driving", "different-session", "SESSION_MISMATCH"),
        ("succeeded", None, "SESSION_MISSING"),
        ("succeeded", "different-session", "SESSION_MISMATCH"),
        ("idle", None, "SESSION_MISSING"),
    ],
)
def test_status_for_fails_closed_on_missing_mismatch_or_restart(
    state_name, session_id, cause_code
):
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        preview = _preview(client)
        expected = client.start(preview)
        state.status_navigation = {
            "state": state_name,
            "session_id": session_id,
            "progress_ratio": None,
            "message_code": None,
            "message": None,
        }
        with pytest.raises(RobotWebOutcomeUnknown) as caught:
            client.status_for(expected)

    assert caught.value.operation == "status"
    assert caught.value.cause_code == cause_code


@pytest.mark.parametrize("state_name", ["driving", "succeeded"])
def test_status_for_accepts_exact_active_and_terminal_session(state_name):
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        preview = _preview(client)
        expected = client.start(preview)
        state.status_navigation["state"] = state_name
        actual = client.status_for(expected)

    assert actual.state == state_name
    assert actual.belongs_to(expected)


def test_status_for_converts_status_failure_to_unknown_outcome():
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        preview = _preview(client)
        expected = client.start(preview)
        state.status_error = 503
        with pytest.raises(RobotWebOutcomeUnknown) as caught:
            client.status_for(expected)

    assert caught.value.operation == "status"
    assert caught.value.http_status == 503
    assert caught.value.cause_code == "STATUS_UNAVAILABLE"


@pytest.mark.parametrize(("operation", "status_code"), [
    ("start", 500),
    ("start", 502),
    ("start", 504),
    ("cancel", 500),
    ("cancel", 502),
    ("cancel", 504),
])
def test_every_5xx_command_response_has_unknown_outcome(
    operation, status_code
):
    with _robot_web_server() as (origin, state):
        client = _bootstrapped(origin)
        preview = _preview(client)
        if operation == "start":
            state.start_error = status_code
            with pytest.raises(RobotWebOutcomeUnknown) as caught:
                client.start(preview)
        else:
            session = client.start(preview)
            state.cancel_error = status_code
            with pytest.raises(RobotWebOutcomeUnknown) as caught:
                client.cancel(session)

    assert caught.value.operation == operation
    assert caught.value.http_status == status_code


def test_handles_are_bound_to_the_client_that_created_them():
    with _robot_web_server() as (origin, _state):
        first = _bootstrapped(origin)
        second = _bootstrapped(origin)
        preview = _preview(first)
        with pytest.raises(RobotWebConfigurationError, match="another client"):
            second.start(preview)

        session = first.start(preview)
        with pytest.raises(RobotWebConfigurationError, match="another client"):
            second.cancel(session)
