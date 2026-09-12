"""Exercise angular normalization with synthetic data, without running a robot."""

import math
import struct
from types import SimpleNamespace

import pytest

from malbut_bringup.scan_geometry import AngularGrid
from malbut_bringup.scan_normalizer import normalize_scan


def scan(count=506, increment=None, angle_min=0.0, angle_max=math.tau):
    """Make a small message-shaped value that also tests timestamp preservation."""
    return SimpleNamespace(
        header=SimpleNamespace(frame_id='laser', stamp=(123, 456)),
        angle_min=angle_min, angle_max=angle_max,
        angle_increment=increment or math.tau / count,
        ranges=[2.0] * count, intensities=[],
        range_min=0.1, range_max=12.0,
        scan_time=0.1, time_increment=0.1 / count)


@pytest.mark.parametrize('count', [505, 506, 507, 508])
def test_fixed_metadata_with_variable_count_has_fixed_output(count):
    """Match the actual reported 505--508 rays with nominal 506 geometry."""
    original = scan(count=count, increment=math.tau / 506)
    output, grid = normalize_scan(original)
    assert grid.count == 506
    assert len(output.ranges) == 506
    assert len(original.ranges) == count  # Never mutate the driver's message.
    assert round((output.angle_max - output.angle_min) / output.angle_increment) + 1 == 506
    assert output.header.stamp == original.header.stamp
    assert output.scan_time == original.scan_time
    assert output.header.frame_id == 'laser'
    assert output.time_increment == 0.0  # No fabricated per-beam timing.
    if count == 505:
        assert math.isnan(output.ranges[-1])


@pytest.mark.parametrize('count', [505, 507, 508])
def test_varying_source_increment_keeps_obstacle_bearing(count):
    """Map by angle, not by the index in a different-resolution scan."""
    _, grid = normalize_scan(scan())
    source = scan(count=count)
    source.ranges = [math.nan] * count
    source_index = count // 4
    source.ranges[source_index] = 0.7
    output, reused_grid = normalize_scan(source, grid)
    assert reused_grid is grid
    hits = [i for i, value in enumerate(output.ranges) if math.isfinite(value)]
    assert len(hits) == 1
    target_angle = output.angle_min + hits[0] * output.angle_increment
    original_angle = source.angle_min + source_index * source.angle_increment
    assert abs(target_angle - original_angle) <= output.angle_increment / 2
    assert output.ranges[hits[0]] == 0.7


def test_full_circle_duplicate_endpoint_keeps_nearest_obstacle():
    """The 360-degree seam must not discard a narrow or close obstacle."""
    original = scan(count=507, increment=math.tau / 506)
    original.ranges[0] = 3.0
    original.ranges[-1] = 0.25
    original.intensities = [1.0] * 507
    original.intensities[-1] = 99.0
    output, _ = normalize_scan(original)
    assert output.ranges[0] == 0.25
    assert output.intensities[0] == 99.0


def test_inclusive_full_circle_without_duplicate_endpoint_is_supported():
    """Common [0, 2pi-increment] geometry also defines exactly one circle."""
    message = scan()
    message.angle_max = math.tau - message.angle_increment
    output, grid = normalize_scan(message)
    assert grid.full_circle
    assert len(output.ranges) == 506


def test_noisy_ranges_do_not_turn_into_free_space_or_averaged_obstacles():
    """Invalid directions remain NaN, while an explicit positive infinity survives."""
    message = scan(8)
    message.ranges = [math.nan, -math.inf, -1.0, 0.09, 13.0, math.inf, 0.2, 0.3]
    output, _ = normalize_scan(message)
    assert all(math.isnan(v) for v in output.ranges[:5])
    assert output.ranges[5] == math.inf
    assert output.ranges[6:] == [0.2, 0.3]


def test_partial_scan_keeps_gaps_and_does_not_extrapolate():
    """A shorter range array is not a measurement of the unobserved final bin."""
    message = scan(count=4, increment=0.5, angle_min=-1.0, angle_max=1.0)
    output, grid = normalize_scan(message)
    assert not grid.full_circle
    assert len(output.ranges) == 5
    assert output.ranges[:4] == [2.0] * 4
    assert math.isnan(output.ranges[4])


def test_partial_scan_contradicting_its_endpoint_is_rejected():
    """Do not hide corrupt partial-scan metadata by truncating extra rays."""
    message = scan(count=8, increment=0.5, angle_min=-1.0, angle_max=1.0)
    with pytest.raises(ValueError, match='declared endpoint'):
        normalize_scan(message)


def test_moved_partial_field_of_view_is_rejected():
    """A second sensor or changed FOV must not silently inherit the old geometry."""
    _, grid = normalize_scan(scan(5, increment=0.5, angle_min=-1.0, angle_max=1.0))
    moved = scan(5, increment=0.5, angle_min=0.0, angle_max=2.0)
    with pytest.raises(ValueError, match='established angular grid'):
        normalize_scan(moved, grid)


@pytest.mark.parametrize('field,value', [
    ('angle_increment', 0.0), ('angle_increment', -0.1),
    ('angle_increment', math.nan), ('angle_min', math.inf),
    ('angle_max', 9.0), ('range_min', -1.0), ('range_max', 0.0),
])
def test_invalid_metadata_is_refused(field, value):
    """Do not publish plausibly shaped data when geometry is unrecoverable."""
    message = scan()
    setattr(message, field, value)
    with pytest.raises(ValueError):
        normalize_scan(message)


def test_wrong_intensity_count_is_rejected():
    """Do not detach intensities from the actual obstacle return."""
    message = scan()
    message.intensities = [1.0]
    with pytest.raises(ValueError, match='intensities'):
        normalize_scan(message)


def test_partial_endpoint_not_on_a_ray_is_rejected():
    """Angle_max cannot define an arbitrary fractional partial-scan beam."""
    with pytest.raises(ValueError, match='inconsistent'):
        AngularGrid.from_metadata(-1.0, 1.1, 0.5)


@pytest.mark.parametrize('first_count', [505, 506, 507, 508])
def test_float32_output_matches_karto_cached_number_of_readings(first_count):
    """ROS float32 serialization must retain Karto's inclusive endpoint count."""
    def float32(value):
        return struct.unpack('f', struct.pack('f', value))[0]

    original = scan(first_count, angle_min=-math.pi, angle_max=math.pi)
    for name in ('angle_min', 'angle_max', 'angle_increment'):
        setattr(original, name, float32(getattr(original, name)))
    _, grid = normalize_scan(original)
    expected = None
    for count in (505, 506, 507, 508):
        source = scan(count, angle_min=-math.pi, angle_max=math.pi)
        output, _ = normalize_scan(source, grid)
        start, end, increment = map(float32, (
            output.angle_min, output.angle_max, output.angle_increment))
        # Humble LaserAssistant explicitly treats round(span / increment) + 1
        # equal to ranges.size() as inclusive, including almost-360 scanners.
        karto_count = int(math.floor((end - start) / increment + 0.5)) + 1
        assert karto_count == len(output.ranges)
        geometry = start, end, increment, karto_count
        if expected is not None:
            assert geometry == expected
        expected = geometry
