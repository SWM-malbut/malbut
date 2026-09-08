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
    minimum_follow_speed_mps: float
    maximum_linear_speed_mps: float
    full_speed_travel_distance_m: float
    observation_loss_debounce_s: float

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
        if self.minimum_follow_speed_mps <= 0.0:
            raise ValueError('minimum follow speed must be positive')
        if self.maximum_linear_speed_mps <= 0.0:
            raise ValueError('maximum linear speed must be positive')
        if self.minimum_follow_speed_mps > self.maximum_linear_speed_mps:
            raise ValueError(
                'minimum follow speed must not exceed maximum speed'
            )
        if self.full_speed_travel_distance_m <= 0.0:
            raise ValueError('full-speed travel distance must be positive')
        if self.observation_loss_debounce_s < 0.0:
            raise ValueError(
                'observation loss debounce must be non-negative'
            )


@dataclass(frozen=True)
class FollowDecision:
    """A safe hold or a Nav2 follow destination."""

    command: FollowCommand
    goal: FollowGoal
    reason: str


def speed_limit_for_travel_distance(
    travel_distance_m: float,
    settings: FollowSettings,
) -> float:
    """Scale the Nav2 speed cap linearly with the remaining path length."""
    settings.validate()
    if travel_distance_m < 0.0:
        raise ValueError('travel distance must be non-negative')
    ratio = min(
        1.0,
        travel_distance_m / settings.full_speed_travel_distance_m,
    )
    speed_range = (
        settings.maximum_linear_speed_mps
        - settings.minimum_follow_speed_mps
    )
    return settings.minimum_follow_speed_mps + ratio * speed_range


def directed_recovery_turn(
    last_camera_bearing_rad: float,
    last_sensor_bearing_rad: float,
    minimum_turn_rad: float,
) -> float | None:
    """Choose the first recovery turn toward the camera exit side."""
    if minimum_turn_rad <= 0.0:
        raise ValueError('recovery minimum turn must be positive')
    bearing = (
        last_camera_bearing_rad
        if abs(last_camera_bearing_rad) > 1e-3
        else last_sensor_bearing_rad
    )
    if abs(bearing) <= 1e-3:
        return None
    return math.copysign(max(abs(bearing), minimum_turn_rad), bearing)


def decide_follow_motion(
    robot: Point2D,
    target: Point2D,
    settings: FollowSettings,
    maximum_travel_m: float | None = None,
    target_velocity: Point2D | None = None,
    approach_prediction_horizon_s: float = 0.0,
    approach_speed_threshold_mps: float = 0.0,
) -> FollowDecision:
    """Choose motion using current range and bounded approach prediction."""
    settings.validate()
    if approach_prediction_horizon_s < 0.0:
        raise ValueError('approach prediction horizon must be non-negative')
    if approach_speed_threshold_mps < 0.0:
        raise ValueError('approach speed threshold must be non-negative')
    target_distance = distance(robot, target)
    yaw = math.atan2(target.y - robot.y, target.x - robot.x)
    lower_bound = settings.desired_distance_m - settings.distance_tolerance_m
    if target_distance <= 1e-9:
        return FollowDecision(
            FollowCommand.HOLD,
            FollowGoal(robot, yaw, target_distance),
            'target position is indistinguishable from robot position',
        )
    control_distance = target_distance
    approaching = False
    if target_velocity is not None and approach_prediction_horizon_s > 0.0:
        direction_x = (target.x - robot.x) / target_distance
        direction_y = (target.y - robot.y) / target_distance
        radial_speed = (
            target_velocity.x * direction_x
            + target_velocity.y * direction_y
        )
        if radial_speed <= -approach_speed_threshold_mps:
            control_distance = max(
                0.0,
                target_distance
                + radial_speed * approach_prediction_horizon_s,
            )
            approaching = control_distance < target_distance
    if control_distance < lower_bound:
        retreat_distance = settings.desired_distance_m - control_distance
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
            else (
                'approaching target predicted inside distance band'
                if approaching
                else 'target inside desired distance band'
            )
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
