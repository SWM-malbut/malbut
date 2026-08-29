"""
Bounded local HTTP client for Robot Web destination navigation.

This module intentionally has no ROS dependency.  It preserves Robot Web's
same-origin browser boundary while giving trusted local code a small typed API.
Navigation commands are never retried: losing a start or cancel response is an
unknown outcome which callers must reconcile through ``status()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from http.client import HTTPException
from http.cookiejar import CookieJar
import hmac
from ipaddress import ip_address
import json
import math
from threading import RLock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)


SESSION_COOKIE = "malbut_editor_session"
DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
MAX_TIMEOUT_SECONDS = 60.0
MAX_READINESS_STATE_AGE_SECONDS = 2.0
REQUIRED_NAV2_LIFECYCLE_NODES = frozenset({
    "amcl",
    "bt_navigator",
    "collision_monitor",
    "controller_server",
    "global_costmap",
    "planner_server",
})


class RobotWebNavigationClientError(RuntimeError):
    """Base class for safe, typed local Robot Web client failures."""

    code = "CLIENT_ERROR"


class RobotWebConfigurationError(RobotWebNavigationClientError):
    """The client URL or the bootstrapped Robot Web config is unsafe."""

    code = "CONFIGURATION_ERROR"


class RobotWebProtocolError(RobotWebNavigationClientError):
    """Robot Web returned a response outside the bounded JSON contract."""

    code = "PROTOCOL_ERROR"

    def __init__(self, code: str = "PROTOCOL_ERROR") -> None:
        self.code = code
        super().__init__(f"Robot Web protocol failure ({code})")


class RobotWebHTTPError(RobotWebNavigationClientError):
    """Robot Web rejected a request with a structured HTTP response."""

    def __init__(self, http_status: int, error_code: str) -> None:
        self.http_status = http_status
        self.error_code = error_code
        self.code = error_code
        super().__init__(
            f"Robot Web rejected the request ({http_status}, {error_code})"
        )


class RobotWebOutcomeUnknown(RobotWebNavigationClientError):
    """A command may have taken effect even though no result was obtained."""

    code = "OUTCOME_UNKNOWN"

    def __init__(
        self,
        operation: str,
        *,
        cause_code: str,
        http_status: int | None = None,
    ) -> None:
        self.operation = operation
        self.cause_code = cause_code
        self.http_status = http_status
        super().__init__(
            f"Robot Web {operation} outcome is unknown ({cause_code})"
        )


@dataclass(frozen=True)
class NavigationTimeouts:
    """Per-operation socket time bounds, all comfortably below one minute."""

    bootstrap_s: float = 3.0
    preview_s: float = 20.0
    start_s: float = 25.0
    status_s: float = 3.0
    cancel_s: float = 5.0

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 < float(value) <= MAX_TIMEOUT_SECONDS
            ):
                raise ValueError(f"{name} must be within (0, 60] seconds")


class EditorConfig:
    """Map identity advertised by one bootstrapped Robot Web process."""

    __slots__ = (
        "map_id",
        "map_revision",
        "navigation_enabled",
        "device_id",
        "simulation",
        "_csrf_token",
    )

    def __init__(
        self,
        map_id: str,
        map_revision: str,
        navigation_enabled: bool,
        csrf_token: str,
        device_id: str,
        simulation: bool,
    ) -> None:
        self.map_id = map_id
        self.map_revision = map_revision
        self.navigation_enabled = navigation_enabled
        self.device_id = device_id
        self.simulation = simulation
        self._csrf_token = csrf_token

    def __repr__(self) -> str:
        return (
            "EditorConfig("
            f"map_id={self.map_id!r}, "
            f"map_revision={self.map_revision!r}, "
            f"navigation_enabled={self.navigation_enabled!r}, "
            f"device_id={self.device_id!r}, "
            f"simulation={self.simulation!r})"
        )


@dataclass(frozen=True, repr=False)
class RobotWebReadiness:
    """Bounded Robot Web identity and readiness without pose disclosure."""

    device_id: str = field(repr=False)
    map_id: str = field(repr=False)
    map_revision: str = field(repr=False)
    simulation: bool
    navigation_enabled: bool
    nav2_all_active: bool
    localization_ok: bool
    pose_available: bool = field(repr=False)
    snapshot_sequence: int = field(repr=False)
    _source_age_seconds: float = field(repr=False)
    _content_fingerprint: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject a forged or unbounded readiness value."""
        for name, value, maximum, allow_empty in (
            ("device_id", self.device_id, 128, False),
            ("map_id", self.map_id, 256, False),
            ("map_revision", self.map_revision, 256, True),
        ):
            if (
                not isinstance(value, str)
                or len(value) > maximum
                or (not allow_empty and not value)
                or any(ord(character) < 32 for character in value)
            ):
                raise RobotWebProtocolError(f"INVALID_{name.upper()}")
        for name in (
            "simulation",
            "navigation_enabled",
            "nav2_all_active",
            "localization_ok",
            "pose_available",
        ):
            if not isinstance(getattr(self, name), bool):
                raise RobotWebProtocolError(f"INVALID_{name.upper()}")
        if (
            isinstance(self.snapshot_sequence, bool)
            or not isinstance(self.snapshot_sequence, int)
            or self.snapshot_sequence < 0
        ):
            raise RobotWebProtocolError("INVALID_STATUS_SEQUENCE")
        if (
            isinstance(self._source_age_seconds, bool)
            or not isinstance(self._source_age_seconds, (int, float))
            or not math.isfinite(float(self._source_age_seconds))
            or not 0.0 <= float(self._source_age_seconds)
            <= MAX_READINESS_STATE_AGE_SECONDS
        ):
            raise RobotWebProtocolError("INVALID_READINESS_SOURCE_AGE")
        try:
            _required_digest(
                self._content_fingerprint,
                "readiness content fingerprint",
            )
        except RobotWebConfigurationError as error:
            raise RobotWebProtocolError(
                "INVALID_READINESS_FINGERPRINT"
            ) from error
        if self.localization_ok != self.pose_available:
            raise RobotWebProtocolError("INCONSISTENT_LOCALIZATION_STATUS")

    @property
    def ready_for_navigation(self) -> bool:
        """Return the complete non-authorizing readiness decision."""
        return bool(
            self.navigation_enabled
            and self.nav2_all_active
            and self.localization_ok
        )

    def matches_runtime(
        self,
        *,
        device_id: str,
        map_id: str,
        map_revision: str,
    ) -> bool:
        """Compare the private runtime binding without rendering it."""
        return bool(
            isinstance(device_id, str)
            and isinstance(map_id, str)
            and isinstance(map_revision, str)
            and _same_text(self.device_id, device_id)
            and _same_text(self.map_id, map_id)
            and _same_text(self.map_revision, map_revision)
        )

    def content_fingerprint(self) -> str:
        """Return a digest for downstream evidence derivation."""
        return self._content_fingerprint

    def conservative_source_age_seconds(self) -> float:
        """Return private upstream age for trusted freshness accounting."""
        return float(self._source_age_seconds)

    def to_public_dict(self) -> dict[str, bool]:
        """Expose readiness flags, never identity, raw status, or pose."""
        return {
            "simulation": self.simulation,
            "navigation_enabled": self.navigation_enabled,
            "nav2_all_active": self.nav2_all_active,
            "localization_ok": self.localization_ok,
            "ready_for_navigation": self.ready_for_navigation,
            "physical_authorized": False,
        }

    def __repr__(self) -> str:
        """Render only the bounded public readiness flags."""
        values = self.to_public_dict()
        rendered = ", ".join(
            f"{name}={value!r}" for name, value in values.items()
        )
        return f"RobotWebReadiness({rendered})"


class NavigationPreview:
    """Opaque, one-use preview capability bound to its creating client."""

    __slots__ = (
        "_token",
        "_owner",
        "_target_binding_digest",
        "_consumed",
        "expires_in_s",
    )

    def __init__(
        self,
        token: str,
        owner: object,
        expires_in_s: float,
        target_binding_digest: str,
    ) -> None:
        self._token = token
        self._owner = owner
        self._target_binding_digest = target_binding_digest
        self._consumed = False
        self.expires_in_s = expires_in_s

    def __repr__(self) -> str:
        state = "consumed" if self._consumed else "ready"
        return f"NavigationPreview(state={state!r})"

    def matches_target_binding(self, digest: str) -> bool:
        """Check one private target binding without exposing either value."""
        return _same_digest(self._target_binding_digest, digest)


class NavigationSession:
    """Opaque navigation-session capability accepted by ``cancel()``."""

    __slots__ = (
        "_session_id", "_owner", "_target_binding_digest", "state"
    )

    def __init__(
        self,
        session_id: str,
        owner: object,
        state: str,
        target_binding_digest: str | None,
    ) -> None:
        self._session_id = session_id
        self._owner = owner
        self._target_binding_digest = target_binding_digest
        self.state = state

    def __repr__(self) -> str:
        return f"NavigationSession(state={self.state!r})"

    def matches_target_binding(self, digest: str) -> bool:
        """Check one private target binding without exposing either value."""
        return _same_digest(self._target_binding_digest, digest)


@dataclass(frozen=True)
class NavigationStatus:
    """Redacted navigation state returned by ``/api/robot/status``."""

    state: str
    session: NavigationSession | None = field(repr=False)
    progress_ratio: float | None = None
    message_code: str | None = None
    message: str | None = field(default=None, repr=False)

    @property
    def terminal(self) -> bool:
        return self.state in {"succeeded", "canceled", "failed"}

    def belongs_to(self, session: NavigationSession) -> bool:
        """Return whether this snapshot is for exactly one opaque session."""
        return bool(
            isinstance(session, NavigationSession)
            and self.session is not None
            and self.session._owner is session._owner
            and _same_text(
                self.session._session_id,
                session._session_id,
            )
            and _same_digest(
                self.session._target_binding_digest,
                session._target_binding_digest,
            )
        )


@dataclass(frozen=True)
class CancelResult:
    """Result of one non-retried cancel request."""

    state: str
    already_terminal: bool


class _RedirectRejected(RobotWebProtocolError):
    def __init__(self) -> None:
        super().__init__("REDIRECT_REJECTED")


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward cookies, CSRF tokens, or commands to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        raise _RedirectRejected()


def _duplicate_checked_object(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _strict_json(payload: bytes) -> dict:
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_checked_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RobotWebProtocolError("INVALID_JSON") from error
    if not isinstance(value, dict):
        raise RobotWebProtocolError("JSON_OBJECT_REQUIRED")
    return value


def _required_string(
    value: dict, name: str, *, maximum: int = 512, allow_empty: bool = False
) -> str:
    item = value.get(name)
    if (
        not isinstance(item, str)
        or len(item) > maximum
        or (not allow_empty and not item)
    ):
        raise RobotWebProtocolError(f"INVALID_{name.upper()}")
    return item


def _optional_string(
    value: dict, name: str, *, maximum: int = 512
) -> str | None:
    item = value.get(name)
    if item is None:
        return None
    if not isinstance(item, str) or len(item) > maximum:
        raise RobotWebProtocolError(f"INVALID_{name.upper()}")
    return item


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RobotWebProtocolError(f"INVALID_{name.upper()}")
    result = float(value)
    if not math.isfinite(result):
        raise RobotWebProtocolError(f"INVALID_{name.upper()}")
    return result


def _same_text(first: str, second: str) -> bool:
    return hmac.compare_digest(
        first.encode("utf-8"),
        second.encode("utf-8"),
    )


def _same_digest(first: str | None, second: str | None) -> bool:
    return bool(
        isinstance(first, str)
        and isinstance(second, str)
        and len(first) == 64
        and len(second) == 64
        and _same_text(first, second)
    )


def _required_digest(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RobotWebConfigurationError(f"{name} is invalid")
    return value


def _readiness_from_status(
    config: EditorConfig,
    value: dict,
) -> RobotWebReadiness:
    """Reduce one raw status snapshot to a bounded, pose-free value."""
    status_map_id = _required_string(value, "map_id", maximum=256)
    status_map_revision = _required_string(
        value,
        "map_revision",
        maximum=256,
        allow_empty=True,
    )
    if not _same_text(status_map_id, config.map_id):
        raise RobotWebProtocolError("MAP_ID_MISMATCH")
    if not _same_text(status_map_revision, config.map_revision):
        raise RobotWebProtocolError("MAP_REVISION_MISMATCH")

    sequence = value.get("seq")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 0 <= sequence <= 9_223_372_036_854_775_807
    ):
        raise RobotWebProtocolError("INVALID_STATUS_SEQUENCE")
    server_time = _required_string(value, "server_time", maximum=128)

    nav2 = value.get("nav2")
    if not isinstance(nav2, dict):
        raise RobotWebProtocolError("INVALID_NAV2_STATUS")
    if set(nav2) != REQUIRED_NAV2_LIFECYCLE_NODES:
        raise RobotWebProtocolError("INCOMPLETE_NAV2_STATUS")
    bounded_nav2 = {}
    for name in sorted(REQUIRED_NAV2_LIFECYCLE_NODES):
        state = _required_string(nav2, name, maximum=64)
        if any(ord(character) < 32 for character in state):
            raise RobotWebProtocolError("INVALID_NAV2_STATE")
        bounded_nav2[name] = state
    nav2_all_active = all(
        state == "active" for state in bounded_nav2.values()
    )

    localization = value.get("localization")
    if not isinstance(localization, dict) or len(localization) > 16:
        raise RobotWebProtocolError("INVALID_LOCALIZATION_STATUS")
    localization_state = _required_string(
        localization,
        "state",
        maximum=64,
    )
    if any(ord(character) < 32 for character in localization_state):
        raise RobotWebProtocolError("INVALID_LOCALIZATION_STATE")
    tf_age = localization.get("tf_age_s")
    if tf_age is not None:
        tf_age = _finite_number(tf_age, "localization_tf_age_s")
        if tf_age < 0.0:
            raise RobotWebProtocolError("INVALID_LOCALIZATION_TF_AGE_S")

    pose = value.get("pose")
    pose_available = False
    pose_age = None
    if localization_state == "ok":
        if tf_age is None:
            raise RobotWebProtocolError("LOCALIZATION_TF_AGE_REQUIRED")
        if tf_age > MAX_READINESS_STATE_AGE_SECONDS:
            raise RobotWebProtocolError("LOCALIZATION_TF_STALE")
        if not isinstance(pose, dict) or not 5 <= len(pose) <= 16:
            raise RobotWebProtocolError("LOCALIZATION_POSE_REQUIRED")
        for name in ("x", "y", "yaw", "stamp", "age_s"):
            number = _finite_number(pose.get(name), f"pose_{name}")
            if name in {"stamp", "age_s"} and number < 0.0:
                raise RobotWebProtocolError(f"INVALID_POSE_{name.upper()}")
            if name == "age_s":
                pose_age = number
        if pose_age > MAX_READINESS_STATE_AGE_SECONDS:
            raise RobotWebProtocolError("LOCALIZATION_POSE_STALE")
        pose_available = True
    elif pose is not None:
        raise RobotWebProtocolError("INCONSISTENT_LOCALIZATION_POSE")

    source_age = (
        max(tf_age, pose_age)
        if localization_state == "ok"
        else 0.0
    )

    fingerprint_payload = json.dumps(
        {
            "device_id": config.device_id,
            "map_id": config.map_id,
            "map_revision": config.map_revision,
            "simulation": config.simulation,
            "navigation_enabled": config.navigation_enabled,
            "seq": sequence,
            "server_time": server_time,
            "nav2": bounded_nav2,
            "localization": {
                "state": localization_state,
                "tf_age_s": tf_age,
                "pose_age_s": pose_age,
                "pose_available": pose_available,
            },
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return RobotWebReadiness(
        device_id=config.device_id,
        map_id=config.map_id,
        map_revision=config.map_revision,
        simulation=config.simulation,
        navigation_enabled=config.navigation_enabled,
        nav2_all_active=nav2_all_active,
        localization_ok=localization_state == "ok",
        pose_available=pose_available,
        snapshot_sequence=sequence,
        _source_age_seconds=source_age,
        _content_fingerprint=hashlib.sha256(fingerprint_payload).hexdigest(),
    )


def _loopback_origin(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise RobotWebConfigurationError("base URL must be a string")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as error:
        raise RobotWebConfigurationError(
            "invalid Robot Web base URL"
        ) from error
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "%" in parsed.hostname
    ):
        raise RobotWebConfigurationError(
            "Robot Web base URL must be an HTTP loopback origin"
        )
    try:
        address = ip_address(parsed.hostname)
    except ValueError as error:
        raise RobotWebConfigurationError(
            "Robot Web hostname must be a literal loopback address"
        ) from error
    if not address.is_loopback:
        raise RobotWebConfigurationError(
            "Robot Web hostname must be a literal loopback address"
        )
    actual_port = 80 if port is None else port
    if actual_port == 0:
        raise RobotWebConfigurationError("Robot Web port must be nonzero")
    host = (
        f"[{address.compressed}]"
        if address.version == 6
        else address.compressed
    )
    return f"http://{host}:{actual_port}"


class RobotWebNavigationClient:
    """Same-origin, no-retry client for one local Robot Web process."""

    def __init__(
        self,
        base_url: str,
        *,
        timeouts: NavigationTimeouts | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= 5_000_000
        ):
            raise ValueError("max_response_bytes must be within [1, 5000000]")
        self._origin = _loopback_origin(base_url)
        self._timeouts = timeouts or NavigationTimeouts()
        self._max_response_bytes = max_response_bytes
        self._cookies = CookieJar()
        self._opener = build_opener(
            ProxyHandler({}),
            HTTPCookieProcessor(self._cookies),
            _RejectRedirects(),
        )
        self._owner = object()
        self._config: EditorConfig | None = None
        self._lock = RLock()

    def __repr__(self) -> str:
        state = "ready" if self._config is not None else "unbootstrapped"
        return f"RobotWebNavigationClient(state={state!r})"

    def _url(self, path: str) -> str:
        return self._origin + path

    def _decode_response(self, response: Any) -> dict:
        content_type = response.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise RobotWebProtocolError("JSON_CONTENT_TYPE_REQUIRED")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError as error:
                raise RobotWebProtocolError(
                    "INVALID_CONTENT_LENGTH"
                ) from error
            if declared < 0 or declared > self._max_response_bytes:
                raise RobotWebProtocolError("RESPONSE_TOO_LARGE")
        payload = response.read(self._max_response_bytes + 1)
        if len(payload) > self._max_response_bytes:
            raise RobotWebProtocolError("RESPONSE_TOO_LARGE")
        return _strict_json(payload)

    def _http_error(self, error: HTTPError) -> RobotWebHTTPError:
        try:
            content_type = error.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise ValueError
            payload = error.read(self._max_response_bytes + 1)
            if len(payload) > self._max_response_bytes:
                raise ValueError
            value = _strict_json(payload)
            code = value.get("error_code")
            if (
                not isinstance(code, str)
                or not 1 <= len(code) <= 128
                or not code[0].isalpha()
                or not all(
                    character.isupper()
                    or character.isdigit()
                    or character == "_"
                    for character in code
                )
            ):
                code = "HTTP_ERROR"
        except (
            HTTPException,
            OSError,
            ValueError,
            RobotWebProtocolError,
        ):
            code = "HTTP_ERROR"
        return RobotWebHTTPError(int(error.code), code)

    def _request(
        self,
        path: str,
        *,
        timeout: float,
        expected_status: int,
        operation: str,
        body: dict | None = None,
        ambiguous_command: bool = False,
    ) -> dict:
        headers = {"Accept": "application/json"}
        data = None
        method = "GET"
        if body is not None:
            config = self._config
            if config is None:
                raise RobotWebConfigurationError("client is not bootstrapped")
            method = "POST"
            try:
                data = json.dumps(
                    body,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise RobotWebConfigurationError(
                    "invalid request values"
                ) from error
            headers.update({
                "Content-Type": "application/json",
                "Origin": self._origin,
                "X-CSRF-Token": config._csrf_token,
            })
        request = Request(
            self._url(path), method=method, data=data, headers=headers
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                if response.status != expected_status:
                    raise RobotWebProtocolError("UNEXPECTED_HTTP_STATUS")
                return self._decode_response(response)
        except _RedirectRejected as error:
            if ambiguous_command:
                raise RobotWebOutcomeUnknown(
                    operation, cause_code=error.code
                ) from error
            raise
        except HTTPError as error:
            structured = self._http_error(error)
            if ambiguous_command and 500 <= error.code <= 599:
                raise RobotWebOutcomeUnknown(
                    operation,
                    cause_code=structured.error_code,
                    http_status=structured.http_status,
                ) from error
            raise structured from error
        except RobotWebProtocolError as error:
            if ambiguous_command:
                raise RobotWebOutcomeUnknown(
                    operation, cause_code=error.code
                ) from error
            raise
        except (TimeoutError, URLError, OSError, HTTPException) as error:
            if ambiguous_command:
                raise RobotWebOutcomeUnknown(
                    operation, cause_code="TRANSPORT_ERROR"
                ) from error
            raise RobotWebProtocolError("TRANSPORT_ERROR") from error

    def bootstrap(self) -> EditorConfig:
        """Establish a cookie/CSRF session and read its map identity."""
        with self._lock:
            value = self._request(
                "/api/editor-config",
                timeout=self._timeouts.bootstrap_s,
                expected_status=200,
                operation="bootstrap",
            )
            map_id = _required_string(value, "map_id", maximum=256)
            map_revision = _required_string(
                value, "map_revision", maximum=256, allow_empty=True
            )
            enabled = value.get("navigation_enabled")
            if not isinstance(enabled, bool):
                raise RobotWebProtocolError("INVALID_NAVIGATION_ENABLED")
            device_id = _required_string(value, "device_id", maximum=128)
            simulation = value.get("simulation")
            if not isinstance(simulation, bool):
                raise RobotWebProtocolError("INVALID_SIMULATION")
            csrf = _required_string(value, "csrf_token", maximum=64)
            if len(csrf) != 64 or any(
                character not in "0123456789abcdef"
                for character in csrf
            ):
                raise RobotWebProtocolError("INVALID_CSRF_TOKEN")
            if not any(
                cookie.name == SESSION_COOKIE and bool(cookie.value)
                for cookie in self._cookies
            ):
                raise RobotWebProtocolError("SESSION_COOKIE_REQUIRED")
            self._config = EditorConfig(
                map_id=map_id,
                map_revision=map_revision,
                navigation_enabled=enabled,
                csrf_token=csrf,
                device_id=device_id,
                simulation=simulation,
            )
            return self._config

    def _ready_config(self) -> EditorConfig:
        config = self._config
        if config is None:
            raise RobotWebConfigurationError("client is not bootstrapped")
        if not config.navigation_enabled:
            raise RobotWebConfigurationError(
                "Robot Web navigation is disabled"
            )
        return config

    def readiness(self) -> RobotWebReadiness:
        """Read fresh config and status as one bounded observation."""
        with self._lock:
            config = self.bootstrap()
            value = self._request(
                "/api/robot/status",
                timeout=self._timeouts.status_s,
                expected_status=200,
                operation="readiness",
            )
            return _readiness_from_status(config, value)

    def preview(
        self,
        *,
        map_id: str,
        map_revision: str,
        x: float,
        y: float,
        user_map_digest: str | None = None,
        target_binding_digest: str,
    ) -> NavigationPreview:
        """Validate one explicitly map-bound coordinate without exposing it."""
        with self._lock:
            config = self._ready_config()
            if map_id != config.map_id or map_revision != config.map_revision:
                raise RobotWebConfigurationError("target map binding is stale")
            if user_map_digest is not None:
                _required_digest(
                    user_map_digest,
                    "target User Map binding",
                )
            target_binding_digest = _required_digest(
                target_binding_digest,
                "target binding",
            )
            requested_x = _finite_number(x, "x")
            requested_y = _finite_number(y, "y")
            body = {
                "map_id": map_id,
                "map_revision": map_revision,
                "x": requested_x,
                "y": requested_y,
            }
            if user_map_digest is not None:
                body["user_map_digest"] = user_map_digest
            value = self._request(
                "/api/navigation/preview",
                timeout=self._timeouts.preview_s,
                expected_status=200,
                operation="preview",
                body=body,
            )
            token = _required_string(value, "preview_token", maximum=512)
            expires = _finite_number(value.get("expires_in_s"), "expires_in_s")
            if expires <= 0.0:
                raise RobotWebProtocolError("INVALID_EXPIRES_IN_S")
            resolved = value.get("resolved")
            if not isinstance(resolved, dict):
                raise RobotWebProtocolError("INVALID_RESOLVED")
            _finite_number(resolved.get("x"), "resolved_x")
            _finite_number(resolved.get("y"), "resolved_y")
            _finite_number(resolved.get("yaw"), "resolved_yaw")
            path = value.get("path")
            if not isinstance(path, dict):
                raise RobotWebProtocolError("INVALID_PATH")
            length = _finite_number(path.get("length_m"), "path_length_m")
            if length < 0.0:
                raise RobotWebProtocolError("INVALID_PATH_LENGTH_M")
            return NavigationPreview(
                token,
                self._owner,
                expires,
                target_binding_digest,
            )

    def start(self, preview: NavigationPreview) -> NavigationSession:
        """Consume and start one preview once, with no automatic retry."""
        with self._lock:
            self._ready_config()
            if (
                not isinstance(preview, NavigationPreview)
                or preview._owner is not self._owner
            ):
                raise RobotWebConfigurationError(
                    "preview belongs to another client"
                )
            if preview._consumed:
                raise RobotWebConfigurationError(
                    "preview was already consumed"
                )
            preview._consumed = True
            value = self._request(
                "/api/navigation/start",
                timeout=self._timeouts.start_s,
                expected_status=202,
                operation="start",
                body={"preview_token": preview._token},
                ambiguous_command=True,
            )
            try:
                session_id = _required_string(
                    value, "session_id", maximum=512
                )
                state = _required_string(value, "state", maximum=32)
                if state != "driving":
                    raise RobotWebProtocolError("INVALID_START_STATE")
            except RobotWebProtocolError as error:
                raise RobotWebOutcomeUnknown(
                    "start", cause_code=error.code
                ) from error
            return NavigationSession(
                session_id,
                self._owner,
                state,
                preview._target_binding_digest,
            )

    def status(self) -> NavigationStatus:
        """Read one bounded, no-store status snapshot for reconciliation."""
        with self._lock:
            config = self._config
            if config is None:
                raise RobotWebConfigurationError(
                    "client is not bootstrapped"
                )
            value = self._request(
                "/api/robot/status",
                timeout=self._timeouts.status_s,
                expected_status=200,
                operation="status",
            )
            try:
                status_map_id = _required_string(
                    value, "map_id", maximum=256
                )
                status_map_revision = _required_string(
                    value,
                    "map_revision",
                    maximum=256,
                    allow_empty=True,
                )
            except RobotWebProtocolError as error:
                raise RobotWebOutcomeUnknown(
                    "status", cause_code=error.code
                ) from error
            if status_map_id != config.map_id:
                raise RobotWebOutcomeUnknown(
                    "status", cause_code="MAP_ID_MISMATCH"
                )
            if status_map_revision != config.map_revision:
                raise RobotWebOutcomeUnknown(
                    "status", cause_code="MAP_REVISION_MISMATCH"
                )
            navigation = value.get("navigation")
            if not isinstance(navigation, dict):
                raise RobotWebProtocolError("INVALID_NAVIGATION_STATUS")
            state = _required_string(navigation, "state", maximum=32)
            allowed_states = {
                "idle",
                "driving",
                "canceling",
                "succeeded",
                "canceled",
                "failed",
            }
            if state not in allowed_states:
                raise RobotWebProtocolError("INVALID_NAVIGATION_STATE")
            session_value = navigation.get("session_id")
            session = None
            if session_value is not None:
                if (
                    not isinstance(session_value, str)
                    or not session_value
                    or len(session_value) > 512
                ):
                    raise RobotWebProtocolError("INVALID_SESSION_ID")
                session = NavigationSession(
                    session_value,
                    self._owner,
                    state,
                    None,
                )
            if state in {"driving", "canceling"} and session is None:
                raise RobotWebOutcomeUnknown(
                    "status", cause_code="SESSION_MISSING"
                )
            progress_value = navigation.get("progress_ratio")
            progress = None
            if progress_value is not None:
                progress = _finite_number(progress_value, "progress_ratio")
                if not 0.0 <= progress <= 1.0:
                    raise RobotWebProtocolError("INVALID_PROGRESS_RATIO")
            return NavigationStatus(
                state=state,
                session=session,
                progress_ratio=progress,
                message_code=_optional_string(
                    navigation, "message_code", maximum=128
                ),
                message=_optional_string(navigation, "message"),
            )

    def status_for(self, session: NavigationSession) -> NavigationStatus:
        """Reconcile status only when it belongs to the exact session."""
        if (
            not isinstance(session, NavigationSession)
            or session._owner is not self._owner
        ):
            raise RobotWebConfigurationError(
                "session belongs to another client"
            )
        try:
            status = self.status()
        except RobotWebOutcomeUnknown:
            raise
        except RobotWebHTTPError as error:
            raise RobotWebOutcomeUnknown(
                "status",
                cause_code=error.error_code,
                http_status=error.http_status,
            ) from error
        except RobotWebProtocolError as error:
            raise RobotWebOutcomeUnknown(
                "status", cause_code=error.code
            ) from error
        if status.session is None:
            raise RobotWebOutcomeUnknown(
                "status", cause_code="SESSION_MISSING"
            )
        if (
            status.session._owner is not session._owner
            or not _same_text(
                status.session._session_id,
                session._session_id,
            )
        ):
            raise RobotWebOutcomeUnknown(
                "status", cause_code="SESSION_MISMATCH"
            )
        if status.state == "idle":
            raise RobotWebOutcomeUnknown(
                "status", cause_code="INVALID_SESSION_STATE"
            )
        return NavigationStatus(
            state=status.state,
            session=session,
            progress_ratio=status.progress_ratio,
            message_code=status.message_code,
            message=status.message,
        )

    def cancel(self, session: NavigationSession) -> CancelResult:
        """Request cancellation; an absent response is an unknown outcome."""
        with self._lock:
            self._ready_config()
            if (
                not isinstance(session, NavigationSession)
                or session._owner is not self._owner
            ):
                raise RobotWebConfigurationError(
                    "session belongs to another client"
                )
            value = self._request(
                "/api/navigation/cancel",
                timeout=self._timeouts.cancel_s,
                expected_status=200,
                operation="cancel",
                body={"session_id": session._session_id},
                ambiguous_command=True,
            )
            try:
                returned_session = _required_string(
                    value, "session_id", maximum=512
                )
                if returned_session != session._session_id:
                    raise RobotWebProtocolError("SESSION_MISMATCH")
                state = _required_string(value, "state", maximum=32)
                if state not in {
                    "canceling", "succeeded", "canceled", "failed"
                }:
                    raise RobotWebProtocolError("INVALID_CANCEL_STATE")
                already_terminal = value.get("already_terminal")
                if not isinstance(already_terminal, bool):
                    raise RobotWebProtocolError("INVALID_CANCEL_RESULT")
            except RobotWebProtocolError as error:
                raise RobotWebOutcomeUnknown(
                    "cancel", cause_code=error.code
                ) from error
            return CancelResult(state=state, already_terminal=already_terminal)
