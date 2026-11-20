"""Shared SQLite Unit of Work: connection, RLock, clock, and transactions."""

from contextlib import contextmanager
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable, Iterator

from malbut_agent_server.conversation_store.schema import (
    initialize_conversation_schema,
)


class ConversationDatabase:
    """Own one SQLite connection, RLock, clock, and atomic transaction boundary."""

    def __init__(
        self,
        database_path: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Open SQLite, initialize schema, and tighten file permissions."""
        if not database_path:
            raise ValueError('database_path must not be empty')
        self.database_path = database_path
        self.clock = clock
        if database_path != ':memory:':
            Path(database_path).expanduser().parent.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
        self.connection = sqlite3.connect(
            str(Path(database_path).expanduser())
            if database_path != ':memory:'
            else database_path,
            check_same_thread=False,
            timeout=5.0,
        )
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.initialize()
        self.secure_file_permissions()

    def initialize(self) -> None:
        """Apply SQLite pragmas and create or validate the conversation schema."""
        with self.lock:
            if self.database_path != ':memory:':
                self.connection.execute('PRAGMA journal_mode=WAL')
                self.connection.execute('PRAGMA synchronous=NORMAL')
            self.connection.execute('PRAGMA foreign_keys=ON')
            self.connection.execute('PRAGMA busy_timeout=5000')
            initialize_conversation_schema(self.connection)
            self.connection.commit()

    def secure_file_permissions(self) -> None:
        """Restrict on-disk SQLite files to owner read/write permissions."""
        if self.database_path == ':memory:':
            return
        expanded = str(Path(self.database_path).expanduser())
        for suffix in ('', '-wal', '-shm'):
            candidate = expanded + suffix
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)

    def now(self) -> float:
        """Read the current finite timestamp from the active clock."""
        value = float(self.clock())
        if not math.isfinite(value):
            raise RuntimeError('conversation clock is not finite')
        return value

    def begin_immediate(self) -> None:
        """Start an immediate write transaction on the shared connection."""
        self.connection.execute('BEGIN IMMEDIATE')

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        """Acquire the shared RLock without starting a write transaction."""
        with self.lock:
            yield self.connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run one atomic BEGIN IMMEDIATE transaction under the shared RLock.

        Supports reentrant callers that already started a transaction on the
        same thread without prematurely committing or rolling back the outer
        transaction.
        """
        with self.lock:
            owns_transaction = not self.connection.in_transaction
            if owns_transaction:
                self.begin_immediate()
            try:
                yield self.connection
            except BaseException:
                if owns_transaction and self.connection.in_transaction:
                    self.connection.rollback()
                raise
            else:
                if owns_transaction and self.connection.in_transaction:
                    self.connection.commit()

    def close(self) -> None:
        """Close the underlying database connection under the shared RLock."""
        with self.lock:
            self.connection.close()
