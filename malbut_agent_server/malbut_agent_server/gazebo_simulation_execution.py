"""
Explicit production seam for one approved Gazebo simulation handoff.

The seam is intentionally synchronous and one-shot.  It never owns a worker
thread and never drains the durable outbox in the background.  A trusted
server caller names one already-resolved confirmation; the server authority
atomically consumes it and creates the Gazebo outbox row, then the dispatcher
performs at most one protected Unix-socket preparation attempt.
"""

from dataclasses import dataclass, field, fields, is_dataclass
import hashlib
import json
import re
import threading
from typing import Any, Dict, Optional, Tuple
import weakref

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.execution_ledger import SimulationAssuranceError
from malbut_agent_server.gazebo_execution_outbox import (
    GazeboExecutionOutboxConflictError,
    GazeboPreparedExecutionAuthority,
    GazeboSimulationConsumeResult,
)
from malbut_agent_server.gazebo_prepare_dispatcher import (
    GazeboPrepareDispatchResult,
    GazeboPrepareDispatcher,
    GazeboPrepareDispatcherError,
)
from malbut_agent_server.gazebo_simulation_authority import (
    ServerGazeboSimulationApprovalConsumer,
)
from malbut_agent_server.schemas import ValidationError, validate_user_id


GAZEBO_SIMULATION_EXECUTION_SCHEMA_VERSION = 1
_CONFIRMATION_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_ERROR_CODE = re.compile(r'^gazebo_simulation_[a-z0-9_]{1,80}$')
_SEAL_LOCK = threading.RLock()
_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)
_RESULT_SEAL_LOCK = threading.RLock()
_RESULT_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)


def _immutable_snapshot(value: Any) -> Any:
    """Snapshot the exact frozen DTO graph without invoking its methods."""
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if type(value) is tuple:
        return tuple(_immutable_snapshot(item) for item in value)
    if not is_dataclass(value):
        raise GazeboSimulationExecutionError(
            'gazebo_simulation_result_invalid'
        )
    names = tuple(item.name for item in fields(value))
    try:
        attributes = tuple(
            (
                name,
                _immutable_snapshot(
                    object.__getattribute__(value, name)
                ),
            )
            for name in names
        )
        namespace = object.__getattribute__(value, '__dict__')
    except Exception:
        raise GazeboSimulationExecutionError(
            'gazebo_simulation_result_invalid'
        ) from None
    if (
        type(namespace) is not dict
        or not set(namespace).issubset(set(names))
    ):
        raise GazeboSimulationExecutionError(
            'gazebo_simulation_result_invalid'
        )
    return (type(value), tuple(sorted(namespace)), attributes)


class GazeboSimulationExecutionError(RuntimeError):
    """Content-free failure from the internal execution seam."""

    def __init__(
        self,
        code: str = 'gazebo_simulation_execution_unavailable',
    ) -> None:
        """Expose one bounded code and no collaborator detail."""
        normalized = (
            code
            if type(code) is str
            and _ERROR_CODE.fullmatch(code) is not None
            else 'gazebo_simulation_execution_unavailable'
        )
        super().__init__('Gazebo simulation execution is unavailable')
        self.code = normalized

    def __getattribute__(self, name: str) -> Any:
        """Keep private caught errors out of exception serializers."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


@dataclass(frozen=True, eq=False)
class GazeboSimulationExecutionResult:
    """Coordinate-free result of one consume-and-prepare attempt."""

    consume: GazeboSimulationConsumeResult
    preparation: Optional[GazeboPrepareDispatchResult]
    schema_version: int = GAZEBO_SIMULATION_EXECUTION_SCHEMA_VERSION
    _prepared_authority: Optional[
        GazeboPreparedExecutionAuthority
    ] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Require exact simulation-only component result types."""
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != GAZEBO_SIMULATION_EXECUTION_SCHEMA_VERSION
            or type(self.consume) is not GazeboSimulationConsumeResult
            or (
                self.preparation is not None
                and type(self.preparation)
                is not GazeboPrepareDispatchResult
            )
            or (
                self._prepared_authority is not None
                and type(self._prepared_authority)
                is not GazeboPreparedExecutionAuthority
            )
        ):
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_result_invalid'
            )
        authority = self._prepared_authority
        if authority is not None:
            try:
                authority.binding_digest
            except Exception:
                raise GazeboSimulationExecutionError(
                    'gazebo_simulation_result_invalid'
                ) from None
        if (
            (self.preparation is not None or authority is not None)
            and (
                self.consume.enqueue is None
                or (
                    self.preparation is not None
                    and (
                        self.preparation.outbox_id
                        != self.consume.enqueue.outbox_id
                        or self.preparation.operation_id
                        != self.consume.enqueue.operation_id
                    )
                )
                or (
                    authority is not None
                    and (
                        authority.confirmation_request_id
                        != self.consume.receipt.confirmation_request_id
                        or authority.outbox_id
                        != self.consume.enqueue.outbox_id
                        or authority.operation_id
                        != self.consume.enqueue.operation_id
                    )
                )
                or (
                    authority is not None
                    and self.preparation is not None
                    and authority.claim_fence
                    != self.preparation.claim_fence
                )
            )
        ):
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_result_invalid'
            )
        if (
            self.preparation is not None
            and authority is None
        ) or (
            self.consume.enqueue is not None
            and self.consume.enqueue.state == 'prepared'
            and authority is None
        ):
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_result_invalid'
            )
        public = {
            'schema_version': self.schema_version,
            'consume': GazeboSimulationConsumeResult.to_public_dict(
                self.consume
            ),
            'preparation': (
                None
                if self.preparation is None
                else GazeboPrepareDispatchResult.to_public_dict(
                    self.preparation
                )
            ),
            'prepared': authority is not None,
            'runtime_mode': 'gazebo',
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
        }
        try:
            public_bytes = json.dumps(
                public,
                sort_keys=True,
                separators=(',', ':'),
                ensure_ascii=True,
                allow_nan=False,
            ).encode('ascii')
            snapshot = _immutable_snapshot(
                (self.consume, self.preparation, authority)
            )
        except GazeboSimulationExecutionError:
            raise
        except Exception:
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_result_invalid'
            ) from None
        with _RESULT_SEAL_LOCK:
            _RESULT_SEALS[self] = (
                self.consume,
                self.preparation,
                authority,
                self.schema_version,
                snapshot,
                public_bytes,
            )

    def _attest(self) -> Tuple[Any, ...]:
        """Revalidate exact nested DTOs against the external result seal."""
        expected = None
        current = None
        try:
            with _RESULT_SEAL_LOCK:
                expected = _RESULT_SEALS.get(self)
            current = (
                object.__getattribute__(self, 'consume'),
                object.__getattribute__(self, 'preparation'),
                object.__getattribute__(self, '_prepared_authority'),
                object.__getattribute__(self, 'schema_version'),
            )
        except Exception:
            expected = None
            current = None
        if (
            type(self) is not GazeboSimulationExecutionResult
            or expected is None
            or current is None
            or len(expected) != 6
            or current[0] is not expected[0]
            or current[1] is not expected[1]
            or current[2] is not expected[2]
            or current[3] != expected[3]
        ):
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_result_invalid'
            )
        try:
            snapshot = _immutable_snapshot(current[:3])
            if snapshot != expected[4]:
                raise GazeboSimulationExecutionError(
                    'gazebo_simulation_result_invalid'
                )
            if current[2] is not None:
                current[2].binding_digest
            if current[1] is not None:
                GazeboPrepareDispatchResult._attest(current[1])
        except GazeboSimulationExecutionError:
            raise
        except Exception:
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_result_invalid'
            ) from None
        return expected

    @property
    def prepared_authority(
        self,
    ) -> Optional[GazeboPreparedExecutionAuthority]:
        """Return the re-attested server-private runner selector."""
        return GazeboSimulationExecutionResult._attest(self)[2]

    @property
    def prepared(self) -> bool:
        """Return whether a durable ACK was reverified for later driving."""
        return GazeboSimulationExecutionResult._attest(self)[2] is not None

    def to_public_dict(self) -> Dict[str, Any]:
        """Return no target, map, device, coordinate, token, or socket data."""
        expected = GazeboSimulationExecutionResult._attest(self)
        try:
            value = json.loads(expected[5].decode('ascii'))
        except (UnicodeDecodeError, ValueError):
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_result_invalid'
            ) from None
        if type(value) is not dict:
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_result_invalid'
            )
        return value

    def __repr__(self) -> str:
        """Render only safe execution state and replay metadata."""
        public = GazeboSimulationExecutionResult.to_public_dict(self)
        consume = public['consume']
        enqueue = consume['gazebo_execution']
        receipt = consume['simulation_receipt']
        return (
            'GazeboSimulationExecutionResult('
            f'receipt_state={receipt["state"]!r}, '
            'outbox_state='
            f'{None if enqueue is None else enqueue["state"]!r}, '
            'prepared='
            f'{public["prepared"]!r}, '
            f'replayed={receipt["replayed"]!r}, '
            "runtime_mode='gazebo', simulation=True, "
            'physical_authorized=False, physical_effects=False, '
            'viewer_live=False, camera_coverage_validated=False, '
            'coverage_achieved=False)'
        )


class GazeboSimulationExecutionSeam:
    """Sealed, explicitly invoked composition of consumer and dispatcher."""

    __slots__ = (
        '_consumer',
        '_dispatcher',
        '_store',
        '_user_id',
        '__weakref__',
    )

    def __init__(
        self,
        consumer: ServerGazeboSimulationApprovalConsumer,
        dispatcher: GazeboPrepareDispatcher,
        *,
        user_id: str,
    ) -> None:
        """Fix one owner, store, authority consumer, and UDS dispatcher."""
        if (
            type(consumer) is not ServerGazeboSimulationApprovalConsumer
            or type(dispatcher) is not GazeboPrepareDispatcher
        ):
            raise TypeError(
                'Gazebo simulation execution dependencies are invalid'
            )
        normalized_user = validate_user_id(user_id)
        store = object.__getattribute__(consumer, '_store')
        if object.__getattribute__(dispatcher, '_store') is not store:
            raise ValueError(
                'Gazebo simulation execution dependencies do not share '
                'one store'
            )
        ServerGazeboSimulationApprovalConsumer._attest_configuration(
            consumer
        )
        GazeboPrepareDispatcher._attest_configuration(dispatcher)
        object.__setattr__(self, '_consumer', consumer)
        object.__setattr__(self, '_dispatcher', dispatcher)
        object.__setattr__(self, '_store', store)
        object.__setattr__(self, '_user_id', normalized_user)
        with _SEAL_LOCK:
            _SEALS[self] = (
                consumer,
                dispatcher,
                store,
                normalized_user,
            )

    def __setattr__(self, _name: str, _value: Any) -> None:
        """Keep the internal execution composition sealed."""
        raise AttributeError('Gazebo simulation execution seam is sealed')

    def matches_runtime(self, store: Any, user_id: Any) -> bool:
        """Return whether an HTTP composition uses the exact owner/store."""
        try:
            GazeboSimulationExecutionSeam._attest_configuration(self)
            normalized_user = validate_user_id(user_id)
            return store is self._store and normalized_user == self._user_id
        except (GazeboSimulationExecutionError, ValidationError):
            return False

    def consume_and_prepare(
        self,
        confirmation_request_id: str,
    ) -> GazeboSimulationExecutionResult:
        """Consume one approval and perform one synchronous prepare attempt."""
        failure: Optional[GazeboSimulationExecutionError] = None
        result: Optional[GazeboSimulationExecutionResult] = None
        try:
            GazeboSimulationExecutionSeam._attest_configuration(self)
            normalized = self._confirmation_id(confirmation_request_id)
            consume = ServerGazeboSimulationApprovalConsumer.consume(
                self._consumer,
                normalized,
            )
            preparation = None
            prepared_authority = None
            if consume.enqueue is not None:
                preparation = GazeboPrepareDispatcher.dispatch_once(
                    self._dispatcher,
                    self._dispatch_request_id(normalized),
                    expected_outbox_id=consume.enqueue.outbox_id,
                    expected_operation_id=consume.enqueue.operation_id,
                    expected_confirmation_request_id=normalized,
                )
                try:
                    prepared_authority = (
                        SQLiteConversationStore
                        .resolve_prepared_gazebo_execution(
                            self._store,
                            confirmation_request_id=normalized,
                            expected_user_id=self._user_id,
                        )
                    )
                except GazeboExecutionOutboxConflictError:
                    if (
                        preparation is not None
                        or consume.enqueue.state == 'prepared'
                    ):
                        raise GazeboSimulationExecutionError(
                            'gazebo_simulation_result_invalid'
                        ) from None
            result = GazeboSimulationExecutionResult(
                consume=consume,
                preparation=preparation,
                _prepared_authority=prepared_authority,
            )
        except GazeboSimulationExecutionError as error:
            failure = error
        except SimulationAssuranceError:
            failure = GazeboSimulationExecutionError(
                'gazebo_simulation_not_authorized'
            )
        except GazeboPrepareDispatcherError:
            failure = GazeboSimulationExecutionError(
                'gazebo_simulation_prepare_unavailable'
            )
        except (TypeError, ValidationError, ValueError):
            failure = GazeboSimulationExecutionError(
                'gazebo_simulation_not_authorized'
            )
        except Exception:
            failure = GazeboSimulationExecutionError()
        if failure is not None:
            failure.__cause__ = None
            failure.__context__ = None
            failure.__traceback__ = None
            raise failure
        if result is None:
            raise GazeboSimulationExecutionError()
        return result

    @staticmethod
    def _confirmation_id(value: Any) -> str:
        if (
            type(value) is not str
            or _CONFIRMATION_ID.fullmatch(value) is None
        ):
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_not_authorized'
            )
        return value

    def _dispatch_request_id(self, confirmation_request_id: str) -> str:
        payload = json.dumps(
            {
                'contract': 'malbut-gazebo-explicit-dispatch-v1',
                'user_id': self._user_id,
                'confirmation_request_id': confirmation_request_id,
            },
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        ).encode('ascii')
        return f'gazebo-dispatch-{hashlib.sha256(payload).hexdigest()}'

    def _attest_configuration(self) -> None:
        expected = None
        try:
            with _SEAL_LOCK:
                expected = _SEALS.get(self)
            current = (
                object.__getattribute__(self, '_consumer'),
                object.__getattribute__(self, '_dispatcher'),
                object.__getattribute__(self, '_store'),
                object.__getattribute__(self, '_user_id'),
            )
        except Exception:
            expected = None
            current = None
        if (
            type(self) is not GazeboSimulationExecutionSeam
            or expected is None
            or current is None
            or len(expected) != 4
            or current[0] is not expected[0]
            or current[1] is not expected[1]
            or current[2] is not expected[2]
            or current[3] != expected[3]
        ):
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_configuration_changed'
            )
        ServerGazeboSimulationApprovalConsumer._attest_configuration(
            current[0]
        )
        GazeboPrepareDispatcher._attest_configuration(current[1])


__all__ = [
    'GAZEBO_SIMULATION_EXECUTION_SCHEMA_VERSION',
    'GazeboSimulationExecutionError',
    'GazeboSimulationExecutionResult',
    'GazeboSimulationExecutionSeam',
]
