"""
Project real laser rays onto a fixed angular grid for SLAM Toolbox.

Humble's Karto caches a laser model from the first scan. Some rotating drivers
vary the number/resolution of rays on each revolution. Use each ray's declared
angle, not its array position in the first scan, to form a stable laser model.
Unobserved directions stay NaN; they must not become clearing/free-space rays.
This cannot correct incorrect source angles, missing TF, or odometry drift.
"""

from dataclasses import dataclass
import math


_ANGLE_EPS = 1.0e-5  # Float32 angle metadata and accumulated rounding error.


def _metadata(angle_min, angle_max, angle_increment):
    if not all(math.isfinite(v) for v in (
            angle_min, angle_max, angle_increment)):
        raise ValueError('laser angles must be finite')
    span = angle_max - angle_min
    if angle_increment <= 0.0 or span <= 0.0:
        raise ValueError('a positive laser angle increment and span are required')
    if span > math.tau + _ANGLE_EPS or angle_increment > span:
        raise ValueError('laser angular extent is invalid')
    return span


@dataclass(frozen=True)
class AngularGrid:
    """Describe one fixed set of output ray angles, without a guessed beam count."""

    angle_min: float
    angle_increment: float
    count: int
    full_circle: bool

    @classmethod
    def from_metadata(cls, angle_min, angle_max, angle_increment):
        """Accept both inclusive and exclusive full-revolution endpoints."""
        span = _metadata(angle_min, angle_max, angle_increment)
        circular = (
            abs(span - math.tau) <= _ANGLE_EPS
            or abs(span + angle_increment - math.tau) <= _ANGLE_EPS)
        if (math.tau if circular else span) / angle_increment > 1000000:
            raise ValueError('laser grid has an excessive beam count')
        if circular:
            count = int(math.floor(math.tau / angle_increment + 0.5))
            increment = math.tau / count if count else 0.0
        else:
            intervals = int(math.floor(span / angle_increment + 0.5))
            if abs(span - intervals * angle_increment) > _ANGLE_EPS:
                raise ValueError('partial laser endpoint is inconsistent with its increment')
            count, increment = intervals + 1, angle_increment
        if not 2 <= count <= 1000000:
            raise ValueError('laser grid has an invalid or excessive beam count')
        return cls(float(angle_min), float(increment), count, circular)

    @property
    def angle_max(self):
        """Return the angle of the last output measurement, inclusive."""
        return self.angle_min + (self.count - 1) * self.angle_increment

    def project(self, angle_min, angle_max, angle_increment, ranges,
                range_min, range_max, intensities=()):
        """
        Bin actual angles and keep the nearest obstacle when rays coincide.

        Quantization moves a ray by at most half an output angular cell. We do
        not interpolate ranges across objects, duplicate rays to fill gaps, or
        silently reinterpret contradictory partial-scan endpoint metadata.
        """
        _metadata(angle_min, angle_max, angle_increment)
        if not (math.isfinite(range_min) and math.isfinite(range_max)
                and 0.0 <= range_min < range_max):
            raise ValueError('laser range limits are invalid')
        if len(ranges) < 2:
            raise ValueError('laser scan contains fewer than two rays')
        if len(intensities) not in (0, len(ranges)):
            raise ValueError('laser intensities do not match the source rays')
        source_last = angle_min + (len(ranges) - 1) * angle_increment
        if not self.full_circle and source_last > angle_max + _ANGLE_EPS:
            raise ValueError('partial scan has rays beyond its declared endpoint')

        output = [math.nan] * self.count
        output_intensities = [math.nan] * self.count if len(intensities) else []
        for source_index, value in enumerate(ranges):
            angle = angle_min + source_index * angle_increment
            offset = angle - self.angle_min
            if self.full_circle:
                offset %= math.tau
            target = int(math.floor(offset / self.angle_increment + 0.5))
            if self.full_circle:
                target %= self.count
            elif not 0 <= target < self.count:
                raise ValueError('partial scan moved outside the established angular grid')

            if math.isnan(value) or value == -math.inf:
                continue
            if math.isfinite(value) and not range_min <= value <= range_max:
                continue
            previous = output[target]
            if math.isnan(previous) or value < previous:
                output[target] = float(value)
                if output_intensities:
                    output_intensities[target] = float(intensities[source_index])
        return output, output_intensities
