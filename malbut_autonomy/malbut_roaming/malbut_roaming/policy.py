"""Tunable, stochastic roaming policy for a known occupancy map."""

from collections import deque
from dataclasses import dataclass
import math
import random

from malbut_roaming.geometry import Point2D, distance
from malbut_roaming.grid_map import Candidate


@dataclass(frozen=True)
class PolicyConfig:
    """Weights and bounds used to choose the next destination."""

    minimum_goal_distance: float
    maximum_goal_distance: float
    preferred_goal_distance: float
    distance_scale: float
    open_clearance: float
    peripheral_clearance: float
    peripheral_probability: float
    revisit_horizon_seconds: float
    recent_goal_radius: float
    recent_memory_size: int
    failure_cooldown_seconds: float
    idleness_weight: float
    distance_weight: float
    clearance_weight: float
    novelty_weight: float
    top_k: int
    temperature: float

    def validate(self) -> None:
        """Reject parameter sets that cannot produce a meaningful policy."""
        numeric_values = (
            self.minimum_goal_distance,
            self.maximum_goal_distance,
            self.preferred_goal_distance,
            self.distance_scale,
            self.open_clearance,
            self.peripheral_clearance,
            self.peripheral_probability,
            self.revisit_horizon_seconds,
            self.recent_goal_radius,
            self.failure_cooldown_seconds,
            self.idleness_weight,
            self.distance_weight,
            self.clearance_weight,
            self.novelty_weight,
            self.temperature,
        )
        if not all(math.isfinite(value) for value in numeric_values):
            raise ValueError('policy values must be finite')
        if self.minimum_goal_distance < 0.0:
            raise ValueError('minimum_goal_distance must be non-negative')
        if self.maximum_goal_distance <= self.minimum_goal_distance:
            raise ValueError('maximum_goal_distance must exceed minimum')
        if not (
            self.minimum_goal_distance
            <= self.preferred_goal_distance
            <= self.maximum_goal_distance
        ):
            raise ValueError('preferred_goal_distance must be in bounds')
        if self.distance_scale <= 0.0:
            raise ValueError('distance_scale must be positive')
        if self.open_clearance <= self.peripheral_clearance:
            raise ValueError('open_clearance must exceed peripheral_clearance')
        if self.peripheral_clearance < 0.0:
            raise ValueError('peripheral_clearance must be non-negative')
        if not 0.0 <= self.peripheral_probability <= 1.0:
            raise ValueError('peripheral_probability must be in [0, 1]')
        if self.revisit_horizon_seconds <= 0.0:
            raise ValueError('revisit_horizon_seconds must be positive')
        if self.recent_goal_radius <= 0.0:
            raise ValueError('recent_goal_radius must be positive')
        if self.recent_memory_size <= 0:
            raise ValueError('recent_memory_size must be positive')
        if self.failure_cooldown_seconds < 0.0:
            raise ValueError('failure_cooldown_seconds must be non-negative')
        if min(
            self.idleness_weight,
            self.distance_weight,
            self.clearance_weight,
            self.novelty_weight,
        ) < 0.0:
            raise ValueError('policy weights must be non-negative')
        if self.top_k <= 0:
            raise ValueError('top_k must be positive')
        if self.temperature <= 0.0:
            raise ValueError('temperature must be positive')


class RoamingPolicy:
    """Choose varied goals while reducing long-term map idleness."""

    def __init__(
        self,
        config: PolicyConfig,
        random_seed: int | None = None,
    ) -> None:
        """Create a policy with optional deterministic randomness."""
        config.validate()
        self.config = config
        self._random = random.Random(random_seed)
        self._last_visit: dict[tuple[int, int], float] = {}
        self._failed_until: dict[tuple[int, int], float] = {}
        self._recent = deque(maxlen=config.recent_memory_size)

    def select(
        self,
        candidates: tuple[Candidate, ...],
        current: Point2D,
        now_seconds: float,
    ) -> tuple[Candidate, str] | None:
        """Select one goal and return its behavioral mode."""
        eligible = [
            candidate
            for candidate in candidates
            if self._eligible(candidate, current, now_seconds)
        ]
        if not eligible:
            return None

        mode = (
            'peripheral'
            if self._random.random() < self.config.peripheral_probability
            else 'open'
        )
        mode_candidates = self._for_mode(eligible, mode)
        if not mode_candidates:
            mode = 'open' if mode == 'peripheral' else 'peripheral'
            mode_candidates = self._for_mode(eligible, mode)
        if not mode_candidates:
            mode = 'fallback'
            mode_candidates = eligible

        scored = [
            (self._score(candidate, current, now_seconds, mode), candidate)
            for candidate in mode_candidates
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        shortlist = scored[:self.config.top_k]
        maximum = shortlist[0][0]
        weights = [
            math.exp(
                (score - maximum) / self.config.temperature
            )
            for score, _candidate in shortlist
        ]
        chosen = self._random.choices(
            [candidate for _score, candidate in shortlist],
            weights=weights,
            k=1,
        )[0]
        return chosen, mode

    def record_success(self, candidate: Candidate, now_seconds: float) -> None:
        """Reset idleness for a reached destination."""
        self._last_visit[candidate.key] = now_seconds
        self._recent.append(candidate)
        self._failed_until.pop(candidate.key, None)

    def record_failure(self, candidate: Candidate, now_seconds: float) -> None:
        """Temporarily suppress a destination rejected by the planner/Nav2."""
        self._failed_until[candidate.key] = (
            now_seconds + self.config.failure_cooldown_seconds
        )

    def _eligible(
        self,
        candidate: Candidate,
        current: Point2D,
        now_seconds: float,
    ) -> bool:
        travel_distance = distance(current, candidate.point)
        return (
            self.config.minimum_goal_distance
            <= travel_distance
            <= self.config.maximum_goal_distance
            and self._failed_until.get(candidate.key, 0.0) <= now_seconds
        )

    def _for_mode(
        self,
        candidates: list[Candidate],
        mode: str,
    ) -> list[Candidate]:
        if mode == 'open':
            return [
                candidate
                for candidate in candidates
                if candidate.clearance >= self.config.open_clearance
            ]
        return [
            candidate
            for candidate in candidates
            if candidate.clearance <= self.config.peripheral_clearance
        ]

    def _score(
        self,
        candidate: Candidate,
        current: Point2D,
        now_seconds: float,
        mode: str,
    ) -> float:
        last_visit = self._last_visit.get(candidate.key)
        idleness = (
            1.0
            if last_visit is None
            else min(
                1.0,
                max(0.0, now_seconds - last_visit)
                / self.config.revisit_horizon_seconds,
            )
        )
        travel_distance = distance(current, candidate.point)
        distance_score = math.exp(
            -abs(travel_distance - self.config.preferred_goal_distance)
            / self.config.distance_scale
        )
        if mode == 'peripheral':
            clearance_score = max(
                0.0,
                1.0
                - candidate.clearance
                / max(self.config.peripheral_clearance, 1e-6),
            )
        else:
            clearance_score = min(
                1.0,
                candidate.clearance / self.config.open_clearance,
            )
        if self._recent:
            nearest_recent = min(
                distance(candidate.point, recent.point)
                for recent in self._recent
            )
            novelty = min(
                1.0,
                nearest_recent / self.config.recent_goal_radius,
            )
        else:
            novelty = 1.0
        return (
            self.config.idleness_weight * idleness
            + self.config.distance_weight * distance_score
            + self.config.clearance_weight * clearance_score
            + self.config.novelty_weight * novelty
        )
