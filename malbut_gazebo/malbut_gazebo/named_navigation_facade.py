"""
Compose semantic names with the existing local Robot Web connector.

The façade is intentionally below the Agent approval boundary.  It accepts no
caller-supplied coordinates and defaults to preview-only operation.  The only
execution authority implemented here is an explicit simulation-test permit;
production approval and durable dispatch remain SWM25-132 responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

from malbut_gazebo.map_lifecycle import MAP_STORE_FORMAT
from malbut_gazebo.named_navigation import (
    NamedNavigationCatalog,
    NamedNavigationError,
    NamedNavigationTarget,
    parse_named_navigation_catalog,
)
from malbut_gazebo.robot_web_navigation_client import (
    CancelResult,
    NavigationPreview,
    NavigationSession,
    NavigationStatus,
    RobotWebNavigationClient,
)
from malbut_gazebo.user_map_builder import load_slam_map


MAX_MANIFEST_BYTES = 64 * 1024
MAX_USER_MAP_BYTES = 2 * 1024 * 1024
_SIMULATION_AUTHORITY_TOKEN = object()


class NamedNavigationFacadeError(RuntimeError):
    """Report a stable fail-closed façade or active-map error."""

    def __init__(self, code: str, message: str) -> None:
        """Create a redacted error with a machine-readable code."""
        self.code = code
        super().__init__(message)


def _fail(code: str, message: str) -> None:
    raise NamedNavigationFacadeError(code, message)


def _duplicate_checked_object(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate_json_key", "JSON contains a duplicate key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    _fail("invalid_json_number", "JSON contains a non-finite number")


def _read_json_snapshot(
    path: Path,
    *,
    maximum_bytes: int,
) -> tuple[dict, bytes]:
    try:
        size = path.stat().st_size
        if size < 2 or size > maximum_bytes:
            _fail("invalid_catalog_source", "catalog source size is invalid")
        payload = path.read_bytes()
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_checked_object,
            parse_constant=_reject_json_constant,
        )
    except NamedNavigationFacadeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NamedNavigationFacadeError(
            "invalid_catalog_source",
            "catalog source cannot be read",
        ) from error
    if not isinstance(value, dict):
        _fail("invalid_catalog_source", "catalog source must be an object")
    return value, payload


def _read_json_object(path: Path, *, maximum_bytes: int) -> dict:
    return _read_json_snapshot(
        path,
        maximum_bytes=maximum_bytes,
    )[0]


def _safe_store_file(store: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        _fail("invalid_active_map", "active map path is missing")
    candidate = store / value
    if candidate.is_symlink():
        _fail("invalid_active_map", "active map source cannot be a symlink")
    try:
        path = candidate.resolve(strict=True)
        path.relative_to(store)
    except (OSError, RuntimeError, ValueError) as error:
        raise NamedNavigationFacadeError(
            "invalid_active_map",
            "active map path escapes its store",
        ) from error
    if path.is_symlink() or not path.is_file():
        _fail("invalid_active_map", "active map source is not a regular file")
    return path


@dataclass(frozen=True)
class ActiveMapCatalogSource:
    """Reload a semantic catalog from one exact active map revision."""

    map_store: Path
    device_id: str

    def __post_init__(self) -> None:
        """Pin the source to an existing, non-symlink directory."""
        try:
            raw_source = Path(self.map_store).expanduser()
        except TypeError as error:
            raise NamedNavigationFacadeError(
                "invalid_map_store",
                "map store path is invalid",
            ) from error
        if raw_source.is_symlink():
            _fail("invalid_map_store", "map store cannot be a symlink")
        try:
            source = raw_source.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise NamedNavigationFacadeError(
                "invalid_map_store",
                "map store is unavailable",
            ) from error
        if source.is_symlink() or not source.is_dir():
            _fail("invalid_map_store", "map store must be a directory")
        object.__setattr__(self, "map_store", source)
        if not isinstance(self.device_id, str) or not self.device_id:
            _fail("invalid_device", "device identity is required")

    def load(self) -> NamedNavigationCatalog:
        """Read and independently verify the current active-map binding."""
        active_path = self.map_store / "active.json"
        if active_path.is_symlink() or not active_path.is_file():
            _fail("invalid_active_map", "active map manifest is unavailable")
        active = _read_json_object(
            active_path,
            maximum_bytes=MAX_MANIFEST_BYTES,
        )
        if active.get("format") != MAP_STORE_FORMAT:
            _fail("invalid_active_map", "active map format is unsupported")
        if active.get("device_id") not in {None, self.device_id}:
            _fail("device_mismatch", "active map belongs to another device")

        map_yaml = _safe_store_file(self.map_store, active.get("map_yaml"))
        _safe_store_file(self.map_store, active.get("map_image"))
        user_map_path = _safe_store_file(
            self.map_store,
            active.get("user_map"),
        )
        try:
            slam_map = load_slam_map(map_yaml)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise NamedNavigationFacadeError(
                "invalid_active_map",
                "active SLAM map cannot be verified",
            ) from error
        if (
            active.get("map_id") != slam_map.map_id
            or active.get("map_revision") != slam_map.map_revision
        ):
            _fail("map_binding_changed", "active map identity is stale")
        user_map, user_map_payload = _read_json_snapshot(
            user_map_path,
            maximum_bytes=MAX_USER_MAP_BYTES,
        )
        return parse_named_navigation_catalog(
            user_map,
            device_id=self.device_id,
            expected_map_id=slam_map.map_id,
            expected_map_revision=slam_map.map_revision,
            source_digest=hashlib.sha256(user_map_payload).hexdigest(),
        )


@dataclass(frozen=True, init=False)
class SimulationNavigationAuthority:
    """Explicitly permit one local simulation path, never physical motion."""

    simulation: bool
    physical_authorized: bool

    def __init__(self, token: object) -> None:
        """Prevent accidental construction from ordinary configuration data."""
        if token is not _SIMULATION_AUTHORITY_TOKEN:
            _fail(
                "simulation_authority_invalid",
                "use explicit_test_authority() for a simulation run",
            )
        object.__setattr__(self, "simulation", True)
        object.__setattr__(self, "physical_authorized", False)

    @classmethod
    def explicit_test_authority(cls) -> "SimulationNavigationAuthority":
        """Create the permit required by an operator-explicit test command."""
        return cls(_SIMULATION_AUTHORITY_TOKEN)


@dataclass(frozen=True)
class PreparedNamedNavigation:
    """One private preview bound to the exact semantic target that made it."""

    location: str = field(repr=False)
    target: NamedNavigationTarget = field(repr=False)
    preview: NavigationPreview = field(repr=False)

    def __post_init__(self) -> None:
        """Reject a preview accidentally paired with another target."""
        if (
            not isinstance(self.target, NamedNavigationTarget)
            or not isinstance(self.preview, NavigationPreview)
            or not self.preview.matches_target_binding(
                self.target.binding_digest
            )
        ):
            _fail(
                "preview_binding_mismatch",
                "preview belongs to another semantic target",
            )

    def to_public_dict(self) -> dict[str, Any]:
        """Expose only a redacted, non-authoritative preview summary."""
        return {
            "state": "previewed",
            "target": self.target.to_public_dict(),
            "simulation": True,
            "physical_authorized": False,
        }


@dataclass(frozen=True)
class NamedNavigationExecution:
    """Opaque active Robot Web session plus its immutable target binding."""

    target: NamedNavigationTarget = field(repr=False)
    session: NavigationSession = field(repr=False)

    def __post_init__(self) -> None:
        """Reject a session accidentally paired with another target."""
        if (
            not isinstance(self.target, NamedNavigationTarget)
            or not isinstance(self.session, NavigationSession)
            or not self.session.matches_target_binding(
                self.target.binding_digest
            )
        ):
            _fail(
                "session_binding_mismatch",
                "session belongs to another semantic target",
            )

    def to_public_dict(self) -> dict[str, Any]:
        """Report accepted simulation execution without identifiers or pose."""
        return {
            "state": self.session.state,
            "target": self.target.to_public_dict(),
            "simulation": True,
            "physical_authorized": False,
        }


class NamedNavigationFacade:
    """Resolve, revalidate, and invoke the existing Robot Web service."""

    def __init__(
        self,
        catalog_loader: Callable[[], NamedNavigationCatalog],
        client: RobotWebNavigationClient,
        *,
        authority: SimulationNavigationAuthority | None = None,
    ) -> None:
        """Compose pure semantics and local HTTP without performing I/O."""
        if not callable(catalog_loader):
            raise TypeError("catalog_loader must be callable")
        self._catalog_loader = catalog_loader
        self._client = client
        self._authority = authority

    def preview(self, location: str) -> PreparedNamedNavigation:
        """Plan one named target without starting physical movement."""
        catalog = self._catalog_loader()
        target = catalog.resolve(location)
        config = self._client.bootstrap()
        if config.device_id != target.device_id:
            _fail(
                "device_binding_changed",
                "Robot Web belongs to another device",
            )
        if not config.simulation:
            _fail(
                "simulation_runtime_required",
                "named navigation test requires a simulation runtime",
            )
        if (
            config.map_id != target.map_id
            or config.map_revision != target.map_revision
        ):
            _fail("map_binding_changed", "Robot Web map binding is stale")
        preview = self._client.preview(
            map_id=target.map_id,
            map_revision=target.map_revision,
            x=target.x,
            y=target.y,
            user_map_digest=target.source_digest,
            target_binding_digest=target.binding_digest,
        )
        return PreparedNamedNavigation(
            location=location,
            target=target,
            preview=preview,
        )

    def start(
        self,
        prepared: PreparedNamedNavigation,
    ) -> NamedNavigationExecution:
        """Revalidate semantics, then consume one preview at most once."""
        if self._authority is None:
            _fail(
                "simulation_authority_required",
                "named navigation defaults to preview-only",
            )
        if (
            not self._authority.simulation
            or self._authority.physical_authorized
        ):
            _fail(
                "simulation_authority_invalid",
                "only non-physical simulation authority is supported",
            )
        if not isinstance(prepared, PreparedNamedNavigation):
            raise TypeError("prepared navigation is required")
        current_catalog = self._catalog_loader()
        try:
            current_target = current_catalog.resolve(prepared.location)
        except NamedNavigationError as error:
            raise NamedNavigationFacadeError(
                "target_binding_changed",
                "semantic target changed after preview",
            ) from error
        if current_target.binding_digest != prepared.target.binding_digest:
            _fail(
                "target_binding_changed",
                "semantic target changed after preview",
            )
        config = self._client.bootstrap()
        if config.device_id != current_target.device_id:
            _fail(
                "device_binding_changed",
                "Robot Web belongs to another device",
            )
        if not config.simulation:
            _fail(
                "simulation_runtime_required",
                "named navigation test requires a simulation runtime",
            )
        if (
            config.map_id != current_target.map_id
            or config.map_revision != current_target.map_revision
        ):
            _fail("map_binding_changed", "Robot Web map binding is stale")
        session = self._client.start(prepared.preview)
        return NamedNavigationExecution(
            target=current_target,
            session=session,
        )

    def navigate(self, location: str) -> NamedNavigationExecution:
        """Preview and explicitly start one simulation destination by name."""
        return self.start(self.preview(location))

    def status(
        self,
        execution: NamedNavigationExecution,
    ) -> NavigationStatus:
        """Read only status bound to the exact opaque execution session."""
        if not isinstance(execution, NamedNavigationExecution):
            raise TypeError("named navigation execution is required")
        return self._client.status_for(execution.session)

    def cancel(self, execution: NamedNavigationExecution) -> CancelResult:
        """Cancel the exact opaque session once; never retry automatically."""
        if not isinstance(execution, NamedNavigationExecution):
            raise TypeError("named navigation execution is required")
        return self._client.cancel(execution.session)


def terminal_status_dict(
    execution: NamedNavigationExecution,
    status: NavigationStatus,
) -> dict[str, Any]:
    """Build a bounded redacted status for CLI and test evidence."""
    if not status.belongs_to(execution.session):
        _fail("status_binding_mismatch", "status belongs to another session")
    progress = status.progress_ratio
    if progress is not None and not math.isfinite(progress):
        _fail("invalid_status", "navigation progress is not finite")
    return {
        "state": status.state,
        "terminal": status.terminal,
        "progress_ratio": progress,
        "message_code": status.message_code,
        "target": execution.target.to_public_dict(),
        "simulation": True,
        "physical_authorized": False,
    }


__all__ = [
    "ActiveMapCatalogSource",
    "NamedNavigationExecution",
    "NamedNavigationFacade",
    "NamedNavigationFacadeError",
    "PreparedNamedNavigation",
    "SimulationNavigationAuthority",
    "terminal_status_dict",
]
