"""SWM25-130 semantic catalog adapter for Agent confirmations."""

from typing import Callable

from malbut_agent_server.named_target import BoundNamedTarget
from malbut_gazebo.named_navigation import NamedNavigationCatalog


class CatalogNamedTargetResolver:
    """Resolve names only; never preview, start, cancel, or call Robot Web."""

    def __init__(
        self,
        load_catalog: Callable[[], NamedNavigationCatalog],
    ) -> None:
        """Inject the authoritative current-catalog loader."""
        if not callable(load_catalog):
            raise TypeError('load_catalog must be callable')
        self._load_catalog = load_catalog

    def resolve(self, location: str) -> BoundNamedTarget:
        """Project one private SWM25-130 target into the Agent port."""
        target = self._load_catalog().resolve(location)
        return BoundNamedTarget(
            room_name=target.room_name,
            room_category=target.room_category,
            binding_digest=target.binding_digest,
        )
