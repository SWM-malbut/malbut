"""Pure helpers for independent mecanum translation and camera yaw control."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HeadingControllerSettings:
    """Tunable target-facing angular controller limits."""

    proportional_gain: float
    rate_feedforward_gain: float
    deadband_rad: float
    maximum_speed_rps: float
    maximum_acceleration_rps2: float


def target_angular_velocity(
    heading_error_rad: float,
    target_yaw_rate_rps: float,
    settings: HeadingControllerSettings,
) -> float:
    """Return bounded angular velocity while leaving translation untouched."""
    error = (
        0.0
        if abs(heading_error_rad) <= settings.deadband_rad
        else heading_error_rad
    )
    requested = (
        settings.proportional_gain * error
        + settings.rate_feedforward_gain * target_yaw_rate_rps
    )
    return max(
        -settings.maximum_speed_rps,
        min(settings.maximum_speed_rps, requested),
    )


def limit_angular_acceleration(
    current_rps: float,
    requested_rps: float,
    elapsed_s: float,
    maximum_acceleration_rps2: float,
) -> float:
    """Slew-limit a yaw command for a real mecanum chassis."""
    if elapsed_s <= 0.0:
        return current_rps
    maximum_change = maximum_acceleration_rps2 * elapsed_s
    change = max(
        -maximum_change,
        min(maximum_change, requested_rps - current_rps),
    )
    return current_rps + change
