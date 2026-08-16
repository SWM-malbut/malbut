"""Pure safety and goal-update policy for following a selected person."""

from dataclasses import dataclass
from enum import Enum
import math

from .geometry import FollowGoal, Point2D, distance, make_follow_goal


class FollowCommand(Enum):
    """Motion decision made from one target observation."""

    HOLD = 'hold'
    ALIGN = 'align'
    RETREAT = 'retreat'
    NAVIGATE = 'navigate'


@dataclass(frozen=True)
class FollowSettings:
    """Validated settings for one follow action."""

    desired_distance_m: float
    minimum_distance_m: float
    distance_tolerance_m: float
    goal_update_distance_m: float
    goal_update_period_s: float
    maximum_linear_speed_mps: float
    temporary_lost_timeout_s: float
    target_lost_timeout_s: float

    def validate(self) -> None:
        """Reject settings that could violate the standoff contract."""
        if self.minimum_distance_m <= 0.0:
            raise ValueError('minimum distance must be positive')
        if self.desired_distance_m <= self.minimum_distance_m:
            raise ValueError('desired distance must exceed minimum distance')
        if self.distance_tolerance_m < 0.0:
            raise ValueError('distance tolerance must be non-negative')
        if self.distance_tolerance_m >= (
            self.desired_distance_m - self.minimum_distance_m
        ):
            raise ValueError(
                'distance tolerance must leave room above minimum distance'
            )
        if self.goal_update_distance_m <= 0.0:
            raise ValueError('goal update distance must be positive')
        if self.goal_update_period_s <= 0.0:
            raise ValueError('goal update period must be positive')
        if self.maximum_linear_speed_mps <= 0.0:
            raise ValueError('maximum linear speed must be positive')
        if self.temporary_lost_timeout_s < 0.0:
            raise ValueError('temporary lost timeout must be non-negative')
        if self.target_lost_timeout_s <= self.temporary_lost_timeout_s:
            raise ValueError(
                'target lost timeout must exceed temporary loss timeout'
            )


@dataclass(frozen=True)
class FollowDecision:
    """A safe hold or a Nav2 follow destination."""

    command: FollowCommand
    goal: FollowGoal
    reason: str


def target_loss_timed_out(
    last_seen_s: float | None,
    now_s: float,
    timeout_s: float,
) -> bool:
    """Expire only a target that was acquired and then disappeared."""
    if last_seen_s is None:
        return False
    return max(0.0, now_s - last_seen_s) >= timeout_s


def decide_follow_motion(
    robot: Point2D,
    target: Point2D,
    settings: FollowSettings,
    maximum_travel_m: float | None = None,
) -> FollowDecision:
    """Choose forward, hold, or reverse motion around the distance band."""
    settings.validate()
    target_distance = distance(robot, target)
    yaw = math.atan2(target.y - robot.y, target.x - robot.x)
    lower_bound = settings.desired_distance_m - settings.distance_tolerance_m
    if target_distance <= 1e-9:
        return FollowDecision(
            FollowCommand.HOLD,
            FollowGoal(robot, yaw, target_distance),
            'target position is indistinguishable from robot position',
        )
    if target_distance < lower_bound:
        retreat_distance = settings.desired_distance_m - target_distance
        retreat_goal = FollowGoal(
            Point2D(
                robot.x - retreat_distance * math.cos(yaw),
                robot.y - retreat_distance * math.sin(yaw),
            ),
            yaw,
            target_distance,
        )
        reason = (
            'minimum distance safety retreat'
            if target_distance <= settings.minimum_distance_m
            else 'target inside desired distance band'
        )
        return FollowDecision(
            FollowCommand.RETREAT,
            retreat_goal,
            reason,
        )
    goal = make_follow_goal(
        robot,
        target,
        settings.desired_distance_m,
        maximum_travel=maximum_travel_m,
    )
    if (
        goal.target_distance
        <= settings.desired_distance_m + settings.distance_tolerance_m
    ):
        aligned_goal = FollowGoal(robot, goal.yaw, goal.target_distance)
        return FollowDecision(
            FollowCommand.ALIGN,
            aligned_goal,
            'distance satisfied; keep camera facing target',
        )
    return FollowDecision(FollowCommand.NAVIGATE, goal, 'target ahead')


def should_update_goal(
    previous_goal: Point2D | None,
    candidate_goal: Point2D,
    elapsed_seconds: float,
    settings: FollowSettings,
    update_distance_m: float | None = None,
    update_period_s: float | None = None,
) -> bool:
    """Rate-limit Nav2 preemption while still reacting to target motion."""
    distance_threshold = (
        settings.goal_update_distance_m
        if update_distance_m is None
        else update_distance_m
    )
    period_threshold = (
        settings.goal_update_period_s
        if update_period_s is None
        else update_period_s
    )
    if distance_threshold <= 0.0:
        raise ValueError('goal update distance must be positive')
    if period_threshold <= 0.0:
        raise ValueError('goal update period must be positive')
    if previous_goal is None:
        return True
    # A meaningful target jump must preempt immediately. Even when the
    # filtered displacement is small, the period is a maximum refresh age so
    # a running path can never remain tied to one stale camera observation.
    return (
        distance(previous_goal, candidate_goal) >= distance_threshold
        or elapsed_seconds >= period_threshold
    )
