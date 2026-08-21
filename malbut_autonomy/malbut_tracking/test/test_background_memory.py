"""Tests for retiring settled clusters into learned background."""

import pytest

import math

from malbut_tracking.background_memory import (
    BackgroundMemory,
    select_acquisition_turn,
)
from malbut_tracking.costmap_tracking import ObstacleCluster, Point2D


def _cluster(x: float, y: float) -> ObstacleCluster:
    return ObstacleCluster(
        position=Point2D(x, y), point_count=12, extent_m=0.4
    )


def test_a_still_object_becomes_background_after_the_settle_window():
    """
    Furniture moved in after mapping must stop proposing itself.

    The static field is built once from the saved map, so a chair the owner
    added stays a foreground cluster forever and would be offered as a
    person candidate on every scan.
    """
    memory = BackgroundMemory(settle_seconds=8.0, settle_radius_m=0.25)
    chair = _cluster(2.0, 1.0)

    for tick in range(9):
        memory.observe([chair], float(tick))

    assert memory.is_background(chair.position, 9.0) is True
    assert memory.filter_moving([chair], 9.0) == []


def test_an_object_is_not_background_before_it_settles():
    memory = BackgroundMemory(settle_seconds=8.0)
    chair = _cluster(2.0, 1.0)

    memory.observe([chair], 0.0)
    memory.observe([chair], 3.0)

    assert memory.is_background(chair.position, 3.0) is False
    assert memory.filter_moving([chair], 3.0) == [chair]


def test_a_walking_person_never_settles():
    """Each step lands outside the settle radius, so no place matures."""
    memory = BackgroundMemory(settle_seconds=8.0, settle_radius_m=0.25)

    walked = []
    for tick in range(20):
        person = _cluster(1.0 + 0.5 * tick, 0.0)
        memory.observe([person], float(tick))
        walked.append(memory.is_background(person.position, float(tick)))

    assert not any(walked)


def test_background_releases_the_spot_once_the_object_leaves():
    """A chair carried away must not blank that floor forever."""
    memory = BackgroundMemory(
        settle_seconds=4.0, settle_radius_m=0.25, forget_seconds=10.0
    )
    chair = _cluster(2.0, 1.0)
    for tick in range(6):
        memory.observe([chair], float(tick))
    assert memory.is_background(chair.position, 6.0) is True

    # 의자가 치워진 뒤에는 아무것도 관측되지 않는다.
    memory.observe([], 20.0)

    assert memory.is_background(chair.position, 20.0) is False


def test_settled_background_does_not_hide_a_person_walking_past_it():
    memory = BackgroundMemory(settle_seconds=4.0, settle_radius_m=0.25)
    chair = _cluster(2.0, 1.0)
    for tick in range(6):
        memory.observe([chair], float(tick))

    person = _cluster(2.0, 2.0)
    remaining = memory.filter_moving([chair, person], 6.0)

    assert remaining == [person]


def test_configuration_rejects_unusable_values():
    with pytest.raises(ValueError, match='settle seconds'):
        BackgroundMemory(settle_seconds=0.0)
    with pytest.raises(ValueError, match='settle radius'):
        BackgroundMemory(settle_radius_m=0.0)
    with pytest.raises(ValueError, match='forget seconds'):
        BackgroundMemory(forget_seconds=-1.0)


def _sized(x: float, y: float, extent: float = 0.4) -> ObstacleCluster:
    return ObstacleCluster(
        position=Point2D(x, y), point_count=12, extent_m=extent
    )


def test_turn_targets_the_nearest_candidate_the_camera_cannot_see():
    """
    A person walking behind the robot must draw the camera around.

    LiDAR sees every bearing while the camera sees 74 degrees, so an
    unconfirmed candidate outside that wedge is exactly the case the
    follower used to ignore.
    """
    behind = _sized(-2.0, 0.0)
    far_behind = _sized(-5.0, 0.0)

    turn = select_acquisition_turn(
        [far_behind, behind],
        Point2D(0.0, 0.0),
        0.0,
        camera_half_fov_rad=math.radians(37.0),
        maximum_distance_m=8.0,
    )

    assert turn is not None
    assert abs(abs(turn) - math.pi) < 1e-6


def test_no_turn_when_the_candidate_is_already_in_view():
    """The detector already had its chance; turning would gain nothing."""
    assert select_acquisition_turn(
        [_sized(2.0, 0.0)],
        Point2D(0.0, 0.0),
        0.0,
        camera_half_fov_rad=math.radians(37.0),
        maximum_distance_m=8.0,
    ) is None


def test_turn_ignores_candidates_that_are_not_person_sized():
    assert select_acquisition_turn(
        [_sized(-2.0, 0.0, extent=3.0), _sized(-2.5, 0.0, extent=0.02)],
        Point2D(0.0, 0.0),
        0.0,
        camera_half_fov_rad=math.radians(37.0),
        maximum_distance_m=8.0,
    ) is None


def test_turn_ignores_candidates_beyond_the_range_limit():
    assert select_acquisition_turn(
        [_sized(-20.0, 0.0)],
        Point2D(0.0, 0.0),
        0.0,
        camera_half_fov_rad=math.radians(37.0),
        maximum_distance_m=8.0,
    ) is None


def test_turn_selection_rejects_unusable_configuration():
    with pytest.raises(ValueError, match='maximum distance'):
        select_acquisition_turn(
            [], Point2D(0.0, 0.0), 0.0,
            camera_half_fov_rad=1.0, maximum_distance_m=0.0,
        )
    with pytest.raises(ValueError, match='half field of view'):
        select_acquisition_turn(
            [], Point2D(0.0, 0.0), 0.0,
            camera_half_fov_rad=0.0, maximum_distance_m=5.0,
        )
