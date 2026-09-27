"""Synchronous durable handoff before an event is exposed to consumers."""

from typing import Protocol

from malbut_agent_server.domain.fall_monitoring import FallIncident, FallRuntimeEvent


class FallJournalError(RuntimeError):
    """Persistence failed; the host must stop/recover, not keep discarding data."""


class FallEventJournal(Protocol):
    def append(self, *, device_id: str, boot_id: str,
               event: FallRuntimeEvent, incident: FallIncident) -> None:
        ...

    def append_discovery(self, *, device_id: str, boot_id: str,
                         event: FallRuntimeEvent) -> None:
        ...

    def append_association(self, *, device_id: str, boot_id: str,
                           events: tuple, incident: FallIncident) -> None:
        """Commit the target's events and per-discovery link in ONE transaction."""
        ...
