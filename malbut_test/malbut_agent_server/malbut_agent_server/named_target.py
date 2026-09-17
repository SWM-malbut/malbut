"""Neutral semantic target boundary between Agent and robot adapters."""

import re
from dataclasses import dataclass, field
from typing import Protocol


_SHA256 = re.compile(r'^[0-9a-f]{64}$')


@dataclass(frozen=True)
class BoundNamedTarget:
    """A private target binding with a redacted confirmation projection."""

    room_name: str
    room_category: str
    binding_digest: str = field(repr=False)

    def __post_init__(self) -> None:
        """Validate only fields required by the Agent-side contract."""
        for name in ('room_name', 'room_category'):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.strip()) > 128
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError(f'{name} is invalid')
            object.__setattr__(self, name, value.strip())
        if (
            not isinstance(self.binding_digest, str)
            or _SHA256.fullmatch(self.binding_digest) is None
        ):
            raise ValueError('binding_digest is invalid')

    def to_public_dict(self) -> dict:
        """Expose no coordinate, device identity, room ID, or digest."""
        return {
            'room_name': self.room_name,
            'room_category': self.room_category,
            'execution_authorized': False,
        }


class NamedTargetResolver(Protocol):
    """Resolve one server-owned semantic name without planning motion."""

    def resolve(self, location: str) -> BoundNamedTarget:
        """Return one exact private binding or fail closed."""
