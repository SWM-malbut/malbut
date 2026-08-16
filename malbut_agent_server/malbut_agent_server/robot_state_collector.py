"""
ROS-independent trusted robot-state snapshot collector core.

The module owns only an in-process tri-state snapshot and a bounded local
Unix-domain socket protocol.  It intentionally imports no ROS package,
performs no network I/O, and cannot issue physical commands.  A future ROS
adapter may feed authenticated observations into
:class:`RobotStateSnapshotStore`.
"""

import copy
import json
import math
import os
import re
import secrets
import socket
import stat
import struct
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from malbut_agent_server.robot_state import (
    MAX_ROBOT_STATE_FRAME_BYTES,
    MAX_ROBOT_STATE_LIFETIME_NS,
    MAX_ROBOT_STATE_SEQUENCE,
    MAX_ROBOT_STATE_TIMEOUT_SECONDS,
    TRUSTED_ROBOT_STATE_SCHEMA_VERSION,
    TRUSTED_ROBOT_STATE_SOURCE_KIND,
    trusted_boottime_ns,
)


_LOWER_HEX_64 = re.compile(r'^[0-9a-f]{64}$')
_SAFE_IDENTIFIER = re.compile(
    r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'
)
_ERROR_CODE = re.compile(r'^robot_state_collector_[a-z0-9_]{1,48}$')
_STATE_FIELDS = (
    'battery_percent',
    'navigation_available',
    'localization_ok',
    'emergency_stop',
    'camera_available',
    'privacy_mode',
    'docked',
    'forbidden_zones',
)
_MAP_SCOPED_FIELDS = frozenset(
    {'navigation_available', 'localization_ok', 'forbidden_zones'}
)
_REQUEST_FIELDS = frozenset({'schema_version', 'nonce'})
_MAX_SOCKET_PATH_BYTES = 103
_SOCKET_MODE = 0o660
_LISTEN_BACKLOG = 8


class RobotStateCollectorError(RuntimeError):
    """Fail-closed collector error with stable, content-free diagnostics."""

    def __init__(self, code: str) -> None:
        """Create an error without retaining paths, payloads, or peers."""
        if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
            raise ValueError('collector error code is invalid')
        super().__init__('robot state collector is unavailable')
        self.code = code


@dataclass(frozen=True)
class _FieldRecord:
    value: Any
    source: str
    received_boottime_ns: int
    valid_for_ns: int


@dataclass(frozen=True)
class RobotStateFieldUpdate:
    """One nullable value plus trusted receipt and lifetime metadata."""

    value: Any
    source: Optional[str] = None
    received_boottime_ns: Optional[int] = None
    valid_for_ns: Optional[int] = None


@dataclass(frozen=True)
class RobotStateBindingToken:
    """Opaque in-process token binding an update to one map generation."""

    device_id: str
    instance_id: str
    map_id: str
    map_revision: str
    generation: int

    def __post_init__(self) -> None:
        """Validate the exact store-scoped capability shape."""
        object.__setattr__(
            self,
            'device_id',
            _identifier(self.device_id, 'device_id'),
        )
        object.__setattr__(
            self,
            'instance_id',
            _canonical_uuid(self.instance_id, 'instance_id'),
        )
        object.__setattr__(
            self,
            'map_id',
            _identifier(self.map_id, 'map_id'),
        )
        object.__setattr__(
            self,
            'map_revision',
            _identifier(self.map_revision, 'map_revision'),
        )
        object.__setattr__(
            self,
            'generation',
            _exact_integer(self.generation, 'generation', minimum=0),
        )


def _error(suffix: str) -> RobotStateCollectorError:
    return RobotStateCollectorError(f'robot_state_collector_{suffix}')


def _exact_integer(
    value: Any,
    field_name: str,
    *,
    minimum: int,
    maximum: int = MAX_ROBOT_STATE_SEQUENCE,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(f'{field_name} is invalid')
    return value


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f'{field_name} is invalid')
    return value


def _canonical_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{field_name} is invalid')
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f'{field_name} is invalid') from None
    if str(parsed) != value:
        raise ValueError(f'{field_name} is invalid')
    return value


def _ttl_ns(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError('collector TTL is invalid')
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError('collector TTL is invalid') from None
    if (
        not math.isfinite(numeric)
        or numeric <= 0
        or numeric > MAX_ROBOT_STATE_LIFETIME_NS / 1_000_000_000
    ):
        raise ValueError('collector TTL is invalid')
    result = int(numeric * 1_000_000_000)
    if result < 1 or result > MAX_ROBOT_STATE_LIFETIME_NS:
        raise ValueError('collector TTL is invalid')
    return result


def _timeout_seconds(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError('collector timeout is invalid')
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError('collector timeout is invalid') from None
    if (
        not math.isfinite(numeric)
        or numeric <= 0
        or numeric > MAX_ROBOT_STATE_TIMEOUT_SECONDS
    ):
        raise ValueError('collector timeout is invalid')
    return numeric


def _canonical_zones(value: Any) -> Optional[Tuple[str, ...]]:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) > 50:
        raise ValueError('forbidden_zones is invalid')
    result = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 128:
            raise ValueError('forbidden_zones is invalid')
        normalized = unicodedata.normalize('NFKC', item)
        normalized = ' '.join(normalized.split()).casefold()
        if (
            not normalized
            or len(normalized) > 128
            or any(
                unicodedata.category(character).startswith('C')
                for character in normalized
            )
        ):
            raise ValueError('forbidden_zones is invalid')
        result.append(normalized)
    if len(set(result)) != len(result):
        raise ValueError('forbidden_zones is invalid')
    return tuple(result)


def _field_value(name: str, value: Any) -> Any:
    if name not in _STATE_FIELDS:
        raise ValueError('collector field is invalid')
    if name == 'battery_percent':
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('battery_percent is invalid')
        try:
            result = float(value)
        except (OverflowError, TypeError, ValueError):
            raise ValueError('battery_percent is invalid') from None
        if not math.isfinite(result) or result < 0 or result > 100:
            raise ValueError('battery_percent is invalid')
        return result
    if name == 'forbidden_zones':
        return _canonical_zones(value)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f'{name} is invalid')
    return value


def _wire_value(name: str, value: Any) -> Any:
    if name == 'forbidden_zones' and value is not None:
        return list(value)
    return value


def _audit_timestamp(value: Any) -> str:
    if not isinstance(value, datetime):
        raise ValueError('collector UTC clock is invalid')
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('collector UTC clock is invalid')
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat()


def _read_boot_id() -> str:
    unavailable = False
    try:
        with open(
            '/proc/sys/kernel/random/boot_id',
            'r',
            encoding='ascii',
        ) as stream:
            value = stream.read(64).strip()
        result = _canonical_uuid(value, 'host_boot_id')
    except (OSError, UnicodeError, ValueError):
        unavailable = True
        result = ''
    if unavailable:
        raise _error('boot_unavailable')
    return result


def _secret_instance_id() -> str:
    unavailable = False
    try:
        result = str(uuid.UUID(bytes=secrets.token_bytes(16), version=4))
    except (OSError, TypeError, ValueError):
        unavailable = True
        result = ''
    if unavailable:
        raise _error('identity_unavailable')
    return result


def _canonical_json(value: Any) -> bytes:
    failed = False
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (OverflowError, TypeError, ValueError):
        failed = True
        result = b''
    if failed:
        raise _error('serialization_failed')
    return result


class RobotStateSnapshotStore:
    """Thread-safe immutable-sequence tri-state snapshot store."""

    def __init__(
        self,
        device_id: str,
        map_id: str,
        map_revision: str,
        *,
        ttl_seconds: float = 1.0,
        physical_authority: bool = False,
    ) -> None:
        """Build a production store using only host clocks and secrets."""
        failure = None
        try:
            host_boot_id = _read_boot_id()
            instance_id = _secret_instance_id()
        except RobotStateCollectorError as error:
            failure = error
            host_boot_id = ''
            instance_id = ''
        except Exception:
            failure = _error('identity_unavailable')
            host_boot_id = ''
            instance_id = ''
        if failure is not None:
            failure.__cause__ = None
            failure.__context__ = None
            failure.__suppress_context__ = True
            raise failure.with_traceback(None)
        self._initialize(
            device_id,
            map_id,
            map_revision,
            ttl_seconds=ttl_seconds,
            physical_authority=physical_authority,
            host_boot_id=host_boot_id,
            instance_id=instance_id,
            boottime_ns=trusted_boottime_ns,
            utc_now=lambda: datetime.now(timezone.utc),
            initial_sequence=0,
        )

    @classmethod
    def _for_test(
        cls,
        device_id: str,
        map_id: str,
        map_revision: str,
        *,
        ttl_seconds: float,
        physical_authority: bool,
        host_boot_id: str,
        instance_id: str,
        boottime_ns: Callable[[], int],
        utc_now: Callable[[], datetime],
        initial_sequence: int = 0,
    ) -> 'RobotStateSnapshotStore':
        """Create a deterministic store through a private test seam."""
        instance = cls.__new__(cls)
        instance._initialize(
            device_id,
            map_id,
            map_revision,
            ttl_seconds=ttl_seconds,
            physical_authority=physical_authority,
            host_boot_id=host_boot_id,
            instance_id=instance_id,
            boottime_ns=boottime_ns,
            utc_now=utc_now,
            initial_sequence=initial_sequence,
        )
        return instance

    def _initialize(
        self,
        device_id: str,
        map_id: str,
        map_revision: str,
        *,
        ttl_seconds: float,
        physical_authority: bool,
        host_boot_id: str,
        instance_id: str,
        boottime_ns: Callable[[], int],
        utc_now: Callable[[], datetime],
        initial_sequence: int,
    ) -> None:
        if type(physical_authority) is not bool:
            raise ValueError('physical_authority is invalid')
        if not callable(boottime_ns) or not callable(utc_now):
            raise TypeError('collector clocks are required')
        self._device_id = _identifier(device_id, 'device_id')
        self._map_id = _identifier(map_id, 'map_id')
        self._map_revision = _identifier(
            map_revision,
            'map_revision',
        )
        self._host_boot_id = _canonical_uuid(
            host_boot_id,
            'host_boot_id',
        )
        self._instance_id = _canonical_uuid(
            instance_id,
            'instance_id',
        )
        self._ttl_ns = _ttl_ns(ttl_seconds)
        self._physical_authority = physical_authority
        self._boottime_ns = boottime_ns
        self._utc_now = utc_now
        self._sequence = _exact_integer(
            initial_sequence,
            'initial_sequence',
            minimum=0,
        )
        self._records: Dict[str, Optional[_FieldRecord]] = {
            name: None for name in _STATE_FIELDS
        }
        self._receipt_high_water: Dict[str, Optional[int]] = {
            name: None for name in _STATE_FIELDS
        }
        self._binding_generation = 0
        self._last_assembled_ns: Optional[int] = None
        self._clock_high_water_ns: Optional[int] = None
        self._lock = threading.RLock()
        with self._lock:
            assembled = self._clock_now()
            self._body = self._build_body_locked(assembled)
            self._last_assembled_ns = assembled

    @property
    def device_id(self) -> str:
        """Return the immutable deployment device identity."""
        return self._device_id

    @property
    def map_id(self) -> str:
        """Return the current trusted map identity."""
        with self._lock:
            return self._map_id

    @property
    def map_revision(self) -> str:
        """Return the current trusted map revision."""
        with self._lock:
            return self._map_revision

    @property
    def host_boot_id(self) -> str:
        """Return the immutable host boot UUID."""
        return self._host_boot_id

    @property
    def instance_id(self) -> str:
        """Return the immutable per-process collector UUID."""
        return self._instance_id

    @property
    def sequence(self) -> int:
        """Return the current unsigned 64-bit material sequence."""
        with self._lock:
            return self._sequence

    @property
    def physical_authority(self) -> bool:
        """Return the immutable provenance activation flag."""
        return self._physical_authority

    def binding_token(self) -> RobotStateBindingToken:
        """Return the current in-process map generation token."""
        with self._lock:
            return RobotStateBindingToken(
                device_id=self._device_id,
                instance_id=self._instance_id,
                map_id=self._map_id,
                map_revision=self._map_revision,
                generation=self._binding_generation,
            )

    def validate_binding_token(
        self,
        binding_token: RobotStateBindingToken,
    ) -> int:
        """Fence a read-only adapter against the current map generation."""
        if not isinstance(binding_token, RobotStateBindingToken):
            raise TypeError('binding_token is required')
        with self._lock:
            current = RobotStateBindingToken(
                device_id=self._device_id,
                instance_id=self._instance_id,
                map_id=self._map_id,
                map_revision=self._map_revision,
                generation=self._binding_generation,
            )
            if binding_token != current:
                raise _error('binding_mismatch')
            return self._sequence

    def snapshot(self, nonce: str) -> Dict[str, Any]:
        """Return one nonce-bound copy without mutating snapshot identity."""
        if not isinstance(nonce, str) or not _LOWER_HEX_64.fullmatch(nonce):
            raise ValueError('nonce is invalid')
        with self._lock:
            result = copy.deepcopy(self._body)
        result['nonce'] = nonce
        return result

    def update_field(
        self,
        name: str,
        value: Any,
        *,
        source: Optional[str] = None,
        received_boottime_ns: Optional[int] = None,
        valid_for_ns: Optional[int] = None,
        binding_token: Optional[RobotStateBindingToken] = None,
    ) -> int:
        """Atomically record one nullable observation and return sequence."""
        return self.update_fields(
            {
                name: RobotStateFieldUpdate(
                    value=value,
                    source=source,
                    received_boottime_ns=received_boottime_ns,
                    valid_for_ns=valid_for_ns,
                )
            },
            binding_token=binding_token,
        )

    def update_fields(
        self,
        updates: Mapping[str, RobotStateFieldUpdate],
        *,
        binding_token: Optional[RobotStateBindingToken] = None,
    ) -> int:
        """Publish one atomic multi-field observation as one sequence."""
        failure = None
        try:
            return self._update_fields_impl(
                updates,
                binding_token=binding_token,
            )
        except RobotStateCollectorError as error:
            failure = error
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__suppress_context__ = True
        raise failure.with_traceback(None)

    def _update_fields_impl(
        self,
        updates: Mapping[str, RobotStateFieldUpdate],
        *,
        binding_token: Optional[RobotStateBindingToken],
    ) -> int:
        if (
            not isinstance(updates, Mapping)
            or not updates
            or len(updates) > len(_STATE_FIELDS)
        ):
            raise ValueError('collector field updates are invalid')
        requested = []
        for name, update in updates.items():
            if name not in _STATE_FIELDS or not isinstance(
                update,
                RobotStateFieldUpdate,
            ):
                raise ValueError('collector field updates are invalid')
            normalized = _field_value(name, update.value)
            if normalized is None:
                if (
                    update.source is not None
                    or update.valid_for_ns is not None
                ):
                    raise ValueError('unknown field evidence is invalid')
                requested.append((name, None, update))
            else:
                normalized_source = _identifier(
                    update.source,
                    'field source',
                )
                valid_for_ns = (
                    self._ttl_ns
                    if update.valid_for_ns is None
                    else _exact_integer(
                        update.valid_for_ns,
                        'field valid_for_ns',
                        minimum=1,
                        maximum=self._ttl_ns,
                    )
                )
                requested.append(
                    (
                        name,
                        (normalized, normalized_source, valid_for_ns),
                        update,
                    )
                )
        with self._lock:
            has_map_scoped_update = any(
                name in _MAP_SCOPED_FIELDS for name, _value, _item in requested
            )
            if has_map_scoped_update:
                if binding_token != RobotStateBindingToken(
                    device_id=self._device_id,
                    instance_id=self._instance_id,
                    map_id=self._map_id,
                    map_revision=self._map_revision,
                    generation=self._binding_generation,
                ):
                    raise _error('binding_mismatch')
            elif binding_token is not None and binding_token != (
                RobotStateBindingToken(
                    device_id=self._device_id,
                    instance_id=self._instance_id,
                    map_id=self._map_id,
                    map_revision=self._map_revision,
                    generation=self._binding_generation,
                )
            ):
                raise _error('binding_mismatch')
            assembled = self._clock_now()
            candidate = dict(self._records)
            candidate_high_water = dict(self._receipt_high_water)
            for name, record_value, update in requested:
                if record_value is None:
                    assert update is not None
                    clear_receipt = (
                        assembled
                        if update.received_boottime_ns is None
                        else _exact_integer(
                            update.received_boottime_ns,
                            'received_boottime_ns',
                            minimum=1,
                        )
                    )
                    high_water = self._receipt_high_water[name]
                    if (
                        clear_receipt > assembled
                        or clear_receipt + self._ttl_ns <= assembled
                    ):
                        raise _error('stale_update')
                    if (
                        high_water is not None
                        and clear_receipt < high_water
                    ):
                        raise _error('receipt_regression')
                    if (
                        high_water is not None
                        and clear_receipt == high_water
                        and self._records[name] is not None
                    ):
                        raise _error('receipt_conflict')
                    replacement = None
                    candidate_high_water[name] = clear_receipt
                else:
                    assert update is not None
                    receipt = (
                        assembled
                        if update.received_boottime_ns is None
                        else _exact_integer(
                            update.received_boottime_ns,
                            'received_boottime_ns',
                            minimum=1,
                        )
                    )
                    if (
                        receipt > assembled
                        or receipt + record_value[2] <= assembled
                    ):
                        raise _error('stale_update')
                    previous = self._records[name]
                    high_water = self._receipt_high_water[name]
                    if high_water is not None and receipt < high_water:
                        raise _error('receipt_regression')
                    if (
                        high_water is not None
                        and receipt == high_water
                        and previous is None
                    ):
                        raise _error('receipt_replay')
                    if previous is not None:
                        if receipt < previous.received_boottime_ns:
                            raise _error('receipt_regression')
                        if (
                            receipt == previous.received_boottime_ns
                            and (
                                record_value[0] != previous.value
                                or record_value[1] != previous.source
                                or record_value[2] != previous.valid_for_ns
                            )
                        ):
                            raise _error('receipt_conflict')
                    replacement = _FieldRecord(
                        value=record_value[0],
                        source=record_value[1],
                        received_boottime_ns=receipt,
                        valid_for_ns=record_value[2],
                    )
                    candidate_high_water[name] = receipt
                candidate[name] = replacement
            candidate = self._expire_records(candidate, assembled)
            if (
                candidate == self._records
                and candidate_high_water == self._receipt_high_water
            ):
                return self._sequence
            self._publish_locked(
                candidate,
                assembled,
                receipt_high_water=candidate_high_water,
            )
            return self._sequence

    def update_binding(self, map_id: str, map_revision: str) -> int:
        """Atomically replace the server-owned map binding and sequence."""
        failure = None
        try:
            return self._update_binding_impl(map_id, map_revision)
        except RobotStateCollectorError as error:
            failure = error
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__suppress_context__ = True
        raise failure.with_traceback(None)

    def _update_binding_impl(
        self,
        map_id: str,
        map_revision: str,
    ) -> int:
        normalized_map = _identifier(map_id, 'map_id')
        normalized_revision = _identifier(
            map_revision,
            'map_revision',
        )
        with self._lock:
            if (
                normalized_map == self._map_id
                and normalized_revision == self._map_revision
            ):
                return self._sequence
            assembled = self._clock_now()
            candidate = dict(self._records)
            candidate_high_water = dict(self._receipt_high_water)
            for name in _MAP_SCOPED_FIELDS:
                candidate[name] = None
                previous_high_water = candidate_high_water[name]
                candidate_high_water[name] = max(
                    previous_high_water or 0,
                    assembled,
                )
            candidate = self._expire_records(candidate, assembled)
            self._ensure_sequence_available()
            previous_map = self._map_id
            previous_revision = self._map_revision
            previous_generation = self._binding_generation
            self._map_id = normalized_map
            self._map_revision = normalized_revision
            self._binding_generation += 1
            try:
                self._publish_locked(
                    candidate,
                    assembled,
                    sequence_checked=True,
                    receipt_high_water=candidate_high_water,
                )
            except Exception:
                self._map_id = previous_map
                self._map_revision = previous_revision
                self._binding_generation = previous_generation
                raise
            return self._sequence

    def _clock_now(self) -> int:
        unavailable = False
        try:
            value = self._boottime_ns()
            result = _exact_integer(value, 'boottime_ns', minimum=1)
            if (
                self._clock_high_water_ns is not None
                and result < self._clock_high_water_ns
            ):
                unavailable = True
        except Exception:
            unavailable = True
        if unavailable:
            raise _error('clock_unavailable')
        self._clock_high_water_ns = result
        return result

    def _expire_records(
        self,
        records: Mapping[str, Optional[_FieldRecord]],
        assembled: int,
    ) -> Dict[str, Optional[_FieldRecord]]:
        return {
            name: (
                record
                if record is None
                or record.received_boottime_ns + record.valid_for_ns
                > assembled
                else None
            )
            for name, record in records.items()
        }

    def _ensure_sequence_available(self) -> None:
        if self._sequence >= MAX_ROBOT_STATE_SEQUENCE:
            raise _error('sequence_exhausted')

    def _publish_locked(
        self,
        records: Dict[str, Optional[_FieldRecord]],
        assembled: int,
        *,
        sequence_checked: bool = False,
        receipt_high_water: Dict[str, Optional[int]],
    ) -> None:
        if not sequence_checked:
            self._ensure_sequence_available()
        previous_records = self._records
        previous_sequence = self._sequence
        previous_assembled = self._last_assembled_ns
        previous_high_water = self._receipt_high_water
        self._records = records
        self._receipt_high_water = dict(receipt_high_water)
        self._sequence += 1
        try:
            body = self._build_body_locked(assembled)
        except Exception:
            self._records = previous_records
            self._sequence = previous_sequence
            self._last_assembled_ns = previous_assembled
            self._receipt_high_water = previous_high_water
            raise
        self._body = body
        self._last_assembled_ns = assembled

    def _build_body_locked(self, assembled: int) -> Dict[str, Any]:
        if assembled > MAX_ROBOT_STATE_SEQUENCE - self._ttl_ns:
            raise _error('clock_unavailable')
        non_null = [
            record for record in self._records.values() if record is not None
        ]
        valid_until = assembled + self._ttl_ns
        if non_null:
            valid_until = min(
                valid_until,
                *(
                    record.received_boottime_ns + record.valid_for_ns
                    for record in non_null
                ),
            )
        if valid_until <= assembled:
            raise _error('stale_snapshot')
        unavailable = False
        try:
            assembled_at = _audit_timestamp(self._utc_now())
        except Exception:
            unavailable = True
            assembled_at = ''
        if unavailable:
            raise _error('clock_unavailable')
        state = {}
        evidence = {}
        for name in _STATE_FIELDS:
            record = self._records[name]
            state[name] = (
                None if record is None else _wire_value(name, record.value)
            )
            evidence[name] = (
                None
                if record is None
                else {
                    'source': record.source,
                    'received_boottime_ns': str(
                        record.received_boottime_ns
                    ),
                }
            )
        body = {
            'schema_version': TRUSTED_ROBOT_STATE_SCHEMA_VERSION,
            'source': {
                'kind': TRUSTED_ROBOT_STATE_SOURCE_KIND,
                'host_boot_id': self._host_boot_id,
                'instance_id': self._instance_id,
                'sequence': str(self._sequence),
                'physical_authority': self._physical_authority,
            },
            'binding': {
                'device_id': self._device_id,
                'map_id': self._map_id,
                'map_revision': self._map_revision,
            },
            'assembled_at': assembled_at,
            'assembled_boottime_ns': str(assembled),
            'valid_until_boottime_ns': str(valid_until),
            'state': state,
            'evidence': evidence,
        }
        sized_envelope = copy.deepcopy(body)
        sized_envelope['nonce'] = '0' * 64
        if len(_canonical_json(sized_envelope)) > (
            MAX_ROBOT_STATE_FRAME_BYTES
        ):
            raise _error('snapshot_too_large')
        return body


def _strict_request(payload: bytes) -> str:
    def reject_duplicate(pairs: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError('non-finite JSON number')

    invalid_utf8 = False
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError:
        invalid_utf8 = True
        text = ''
    if invalid_utf8:
        raise _error('request_invalid_utf8')
    invalid_json = False
    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
        invalid_json = True
        value = None
    if invalid_json:
        raise _error('request_invalid_json')
    if not isinstance(value, dict) or set(value) != _REQUEST_FIELDS:
        raise _error('request_invalid')
    if (
        type(value['schema_version']) is not int
        or value['schema_version'] != TRUSTED_ROBOT_STATE_SCHEMA_VERSION
        or not isinstance(value['nonce'], str)
        or not _LOWER_HEX_64.fullmatch(value['nonce'])
    ):
        raise _error('request_invalid')
    return value['nonce']


def _socket_path(value: Any) -> str:
    encoded = b''
    invalid_encoding = False
    if isinstance(value, str):
        try:
            encoded = os.fsencode(value)
        except (UnicodeError, ValueError):
            invalid_encoding = True
    if (
        not isinstance(value, str)
        or not value
        or invalid_encoding
        or '\x00' in value
        or not os.path.isabs(value)
        or os.path.normpath(value) != value
        or not Path(value).name
        or len(encoded) > _MAX_SOCKET_PATH_BYTES
    ):
        raise ValueError('collector socket path is invalid')
    return value


def _transport_now() -> float:
    unavailable = False
    try:
        value = time.monotonic()
        if isinstance(value, bool) or not isinstance(
            value,
            (int, float),
        ):
            unavailable = True
            numeric = 0.0
        else:
            numeric = float(value)
            if not math.isfinite(numeric) or numeric < 0:
                unavailable = True
    except (OSError, OverflowError, RuntimeError, TypeError, ValueError):
        unavailable = True
        numeric = 0.0
    if unavailable:
        raise _error('clock_unavailable')
    return numeric


class RobotStateCollectorServer:
    """Serve bounded nonce-bound snapshots over one fixed local UDS."""

    def __init__(
        self,
        store: RobotStateSnapshotStore,
        socket_path: str,
        expected_agent_uid: int,
        *,
        timeout_seconds: float = 1.0,
    ) -> None:
        """Bind server configuration without touching the filesystem."""
        if not isinstance(store, RobotStateSnapshotStore):
            raise TypeError('collector store is required')
        self._store = store
        self._socket_path = _socket_path(socket_path)
        self._expected_agent_uid = _exact_integer(
            expected_agent_uid,
            'expected_agent_uid',
            minimum=0,
            maximum=(1 << 31) - 1,
        )
        self._timeout_seconds = _timeout_seconds(timeout_seconds)
        self._lifecycle_lock = threading.RLock()
        self._serve_lock = threading.Lock()
        self._listener: Optional[socket.socket] = None
        self._socket_identity: Optional[Tuple[int, int]] = None
        self._active_connections = set()
        self._closed = False
        self._ever_started = False

    @property
    def socket_path(self) -> str:
        """Return the fixed configured socket path."""
        return self._socket_path

    @property
    def expected_agent_uid(self) -> int:
        """Return the only UID allowed to request snapshots."""
        return self._expected_agent_uid

    def start(self) -> None:
        """Create the socket while rejecting symlinks and existing paths."""
        failure = None
        try:
            return self._start_impl()
        except RobotStateCollectorError as error:
            failure = error
        except (OSError, RuntimeError):
            failure = _error('socket_unavailable')
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__suppress_context__ = True
        raise failure.with_traceback(None)

    def _start_impl(self) -> None:
        with self._lifecycle_lock:
            if self._closed or self._ever_started:
                raise _error('lifecycle_invalid')
            parents_before = self._validate_parents()
            try:
                os.lstat(self._socket_path)
            except FileNotFoundError:
                pass
            except OSError:
                raise _error('socket_unavailable') from None
            else:
                # Crash residues are deliberately a supervisor concern.
                # Auto-unlink without a cooperating process lock can delete
                # a concurrent collector between bind() and listen().
                raise _error('socket_exists')

            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            identity: Optional[Tuple[int, int]] = None
            try:
                listener.bind(self._socket_path)
                metadata = os.lstat(self._socket_path)
                if (
                    not stat.S_ISSOCK(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                ):
                    raise _error('socket_path_changed')
                identity = (metadata.st_dev, metadata.st_ino)
                os.chmod(self._socket_path, _SOCKET_MODE)
                metadata = os.lstat(self._socket_path)
                if (
                    (metadata.st_dev, metadata.st_ino) != identity
                    or not stat.S_ISSOCK(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != _SOCKET_MODE
                ):
                    raise _error('socket_path_changed')
                if self._validate_parents() != parents_before:
                    raise _error('socket_path_changed')
                listener.listen(_LISTEN_BACKLOG)
            except Exception:
                listener.close()
                if identity is not None:
                    self._unlink_if_owned(identity)
                raise
            self._listener = listener
            self._socket_identity = identity
            self._ever_started = True

    def serve_once(self) -> None:
        """Accept and serve exactly one request under one total deadline."""
        failure = None
        try:
            return self._serve_once_impl()
        except RobotStateCollectorError as error:
            failure = error
        except socket.timeout:
            failure = _error('request_timeout')
        except (OSError, RuntimeError, struct.error):
            with self._lifecycle_lock:
                closed = self._closed
            if closed:
                failure = _error('closed')
            else:
                failure = _error('transport_unavailable')
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__suppress_context__ = True
        raise failure.with_traceback(None)

    def _serve_once_impl(self) -> None:
        with self._serve_lock:
            with self._lifecycle_lock:
                listener = self._listener
                if listener is None or self._closed:
                    raise _error('not_started')
            deadline = _transport_now() + self._timeout_seconds
            connection = self._accept(listener, deadline)
            with self._lifecycle_lock:
                if self._closed:
                    connection.close()
                    raise _error('closed')
                self._active_connections.add(connection)
            try:
                try:
                    self._check_peer(connection)
                    header = self._recv_exact(connection, 4, deadline)
                    size = struct.unpack('!I', header)[0]
                    if size < 1:
                        raise _error('request_truncated')
                    if size > MAX_ROBOT_STATE_FRAME_BYTES:
                        raise _error('request_too_large')
                    payload = self._recv_exact(connection, size, deadline)
                    self._set_timeout(connection, deadline)
                    trailing = connection.recv(1)
                    if trailing:
                        raise _error('request_extra_data')
                    nonce = _strict_request(payload)
                    response = _canonical_json(self._store.snapshot(nonce))
                    if (
                        not response
                        or len(response) > MAX_ROBOT_STATE_FRAME_BYTES
                    ):
                        raise _error('response_too_large')
                    frame = struct.pack('!I', len(response)) + response
                    self._set_timeout(connection, deadline)
                    connection.sendall(frame)
                    try:
                        connection.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                except RobotStateCollectorError:
                    raise
                except socket.timeout:
                    raise
                except OSError:
                    with self._lifecycle_lock:
                        if self._closed:
                            raise _error('closed')
                    raise _error('connection_unavailable') from None
            finally:
                with self._lifecycle_lock:
                    self._active_connections.discard(connection)
                connection.close()

    def serve_forever(self) -> None:
        """Serve sequentially until close, containing malformed clients."""
        while True:
            with self._lifecycle_lock:
                if self._closed:
                    return
            try:
                self.serve_once()
            except RobotStateCollectorError as error:
                with self._lifecycle_lock:
                    if self._closed:
                        return
                if error.code in {
                    'robot_state_collector_request_timeout',
                    'robot_state_collector_request_truncated',
                    'robot_state_collector_request_too_large',
                    'robot_state_collector_request_extra_data',
                    'robot_state_collector_request_invalid_utf8',
                    'robot_state_collector_request_invalid_json',
                    'robot_state_collector_request_invalid',
                    'robot_state_collector_peer_uid_mismatch',
                    'robot_state_collector_peer_unavailable',
                    'robot_state_collector_connection_unavailable',
                }:
                    continue
                raise

    def close(self) -> None:
        """Close idempotently and unlink only the socket inode we bound."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            listener = self._listener
            identity = self._socket_identity
            connections = tuple(self._active_connections)
            self._active_connections.clear()
            self._listener = None
            self._socket_identity = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if identity is not None:
            self._unlink_if_owned(identity)

    def _validate_parents(
        self,
    ) -> Tuple[Tuple[str, int, int, int, int], ...]:
        parent = str(Path(self._socket_path).parent)
        current = Path(parent).anchor
        paths = [current]
        for component in Path(parent).parts[1:]:
            current = os.path.join(current, component)
            paths.append(current)
        result = []
        euid = os.geteuid()
        for index, path in enumerate(paths):
            try:
                metadata = os.lstat(path)
            except OSError:
                raise _error('socket_parent_invalid') from None
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise _error('socket_parent_invalid')
            if metadata.st_uid not in {0, euid}:
                raise _error('socket_parent_insecure')
            writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            if writable and not (
                metadata.st_uid == 0
                and metadata.st_mode & stat.S_ISVTX
            ):
                raise _error('socket_parent_insecure')
            if index == len(paths) - 1 and (
                metadata.st_uid != euid or writable
            ):
                raise _error('socket_parent_insecure')
            result.append(
                (
                    path,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_uid,
                )
            )
        return tuple(result)

    def _unlink_if_owned(self, identity: Tuple[int, int]) -> None:
        try:
            metadata = os.lstat(self._socket_path)
            if (
                stat.S_ISSOCK(metadata.st_mode)
                and (metadata.st_dev, metadata.st_ino) == identity
            ):
                os.unlink(self._socket_path)
        except FileNotFoundError:
            return
        except OSError:
            return

    def _check_peer(self, connection: socket.socket) -> None:
        option = getattr(socket, 'SO_PEERCRED', None)
        if option is None:
            raise _error('peer_unavailable')
        try:
            credentials = connection.getsockopt(
                socket.SOL_SOCKET,
                option,
                struct.calcsize('3i'),
            )
            _pid, uid, _gid = struct.unpack('3i', credentials)
        except (OSError, struct.error):
            raise _error('peer_unavailable') from None
        if uid != self._expected_agent_uid:
            raise _error('peer_uid_mismatch')

    @staticmethod
    def _set_timeout(connection: socket.socket, deadline: float) -> None:
        remaining = deadline - _transport_now()
        if not math.isfinite(remaining) or remaining <= 0:
            raise socket.timeout()
        connection.settimeout(remaining)

    def _accept(
        self,
        listener: socket.socket,
        deadline: float,
    ) -> socket.socket:
        while True:
            remaining = deadline - _transport_now()
            if not math.isfinite(remaining) or remaining <= 0:
                raise socket.timeout()
            listener.settimeout(min(remaining, 0.1))
            try:
                connection, _address = listener.accept()
                return connection
            except socket.timeout:
                with self._lifecycle_lock:
                    if self._closed:
                        raise _error('closed')

    @classmethod
    def _recv_exact(
        cls,
        connection: socket.socket,
        size: int,
        deadline: float,
    ) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            cls._set_timeout(connection, deadline)
            chunk = connection.recv(remaining)
            if not chunk:
                raise _error('request_truncated')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)


__all__ = [
    'RobotStateBindingToken',
    'RobotStateCollectorError',
    'RobotStateCollectorServer',
    'RobotStateFieldUpdate',
    'RobotStateSnapshotStore',
]
