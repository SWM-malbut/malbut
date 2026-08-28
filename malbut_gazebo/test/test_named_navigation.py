"""Tests for pure, content-bound semantic named navigation."""

import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
import math
from pathlib import Path

import pytest

from malbut_gazebo.named_navigation import (
    NamedNavigationError,
    NamedNavigationTarget,
    parse_named_navigation_catalog,
    resolve_named_navigation_target,
)
from malbut_gazebo.user_map_builder import load_slam_map


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GAZEBO_ROOT = REPOSITORY_ROOT / 'malbut_gazebo'
SCENARIO_ROOT = REPOSITORY_ROOT / 'malbut_scenarios'
DEVICE_ID = 'malbut-gazebo-small-house-01'


def _room(
    room_id: str,
    name: str,
    minimum_x: float,
    maximum_x: float,
    category: str = 'custom',
) -> dict:
    return {
        'type': 'Feature',
        'id': room_id,
        'properties': {
            'role': 'room',
            'room_id': room_id,
            'name': name,
            'category': category,
            'representative_point': [
                (minimum_x + maximum_x) / 2.0,
                2.0,
            ],
            'clearance_m': min((maximum_x - minimum_x) / 2.0, 2.0),
            'area_m2': round((maximum_x - minimum_x) * 4.0, 2),
        },
        'geometry': {
            'type': 'Polygon',
            'coordinates': [[
                [minimum_x, 0.0],
                [maximum_x, 0.0],
                [maximum_x, 4.0],
                [minimum_x, 4.0],
                [minimum_x, 0.0],
            ]],
        },
    }


def _user_map(*rooms: dict) -> dict:
    return {
        'type': 'FeatureCollection',
        'format': 'malbut-user-map-v1',
        'map_id': 'map-home',
        'map_revision': 'rev-home-001',
        'legacy_map_ids': ['map-home-legacy'],
        'frame_id': 'map',
        'generated_at': '2026-08-28T00:00:00+00:00',
        'source': {'type': 'test'},
        'room_segmentation': {
            'method': 'test',
            'room_count': len(rooms),
        },
        'features': [
            {
                'type': 'Feature',
                'id': 'walkable-area',
                'properties': {'role': 'walkable_area'},
                'geometry': {
                    'type': 'Polygon',
                    'coordinates': [],
                },
            },
            *rooms,
        ],
    }


def _catalog(
    user_map=None,
    device_id='malbut-sim-01',
    source_digest=None,
):
    return parse_named_navigation_catalog(
        _user_map(
            _room('room-living', '거실', 0.0, 4.0, 'living_room'),
            _room('room-kitchen', '주방', 6.0, 10.0, 'kitchen'),
        ) if user_map is None else user_map,
        device_id=device_id,
        expected_map_id='map-home',
        expected_map_revision='rev-home-001',
        source_digest=source_digest,
    )


def test_exact_normalized_name_returns_one_neutral_target() -> None:
    """NFKC, whitespace, and case are the only lookup normalization."""
    value = _user_map(
        _room(
            'room-living',
            'Ｌｉｖｉｎｇ　Ｒｏｏｍ',
            0.0,
            4.0,
            'living_room',
        )
    )
    catalog = _catalog(value)

    target = resolve_named_navigation_target(
        catalog,
        '  living   room  ',
    )

    assert catalog.room_count == 1
    assert target.room_id == 'room-living'
    assert target.room_name == 'Living Room'
    assert target.room_category == 'living_room'
    assert (target.x, target.y, target.yaw) == (2.0, 2.0, 0.0)
    assert target.frame_id == 'map'
    assert len(target.semantic_digest) == 64
    assert len(target.source_digest) == 64
    assert len(target.binding_digest) == 64
    with pytest.raises(FrozenInstanceError):
        target.x = 99.0


def test_unknown_substring_and_machine_id_fail_closed() -> None:
    """Never guess by substring, edit distance, or internal room ID."""
    catalog = _catalog()

    for location in ('거', 'room-living', '침실', '', None):
        with pytest.raises(NamedNavigationError) as error:
            catalog.resolve(location)
        assert error.value.code == 'target_not_found'


def test_duplicate_normalized_names_are_ambiguous() -> None:
    """Never choose the first feature when two names normalize equally."""
    value = _user_map(
        _room('room-a', '가족 공간', 0.0, 4.0),
        _room('room-b', '  가족   공간 ', 6.0, 10.0),
    )

    with pytest.raises(NamedNavigationError) as error:
        _catalog(value).resolve('가족 공간')

    assert error.value.code == 'target_ambiguous'


@pytest.mark.parametrize(
    'mutation',
    [
        lambda value: value.update({'map_id': 'map-other'}),
        lambda value: value.update({'map_revision': 'rev-other'}),
        lambda value: value.update({'frame_id': 'odom'}),
        lambda value: value.update({'format': 'malbut-user-map-v2'}),
        lambda value: value.update({'type': 'Feature'}),
        lambda value: value.update({'unexpected': True}),
    ],
)
def test_user_map_identity_and_contract_fail_closed(mutation) -> None:
    """The server-owned map identity must match the nested User Map."""
    value = _user_map(_room('room-a', '방 A', 0.0, 4.0))
    mutation(value)

    with pytest.raises(NamedNavigationError) as error:
        _catalog(value)

    assert error.value.code in {'identity_mismatch', 'invalid_user_map'}


@pytest.mark.parametrize(
    'device_id',
    ['', 'device id', 'x' * 129, None],
)
def test_device_identity_is_strict_and_server_owned(device_id) -> None:
    """Invalid or presentation-style device IDs never enter a binding."""
    with pytest.raises(NamedNavigationError) as error:
        _catalog(device_id=device_id)
    assert error.value.code == 'invalid_device'


@pytest.mark.parametrize(
    'mutation',
    [
        lambda room: room['properties'].update(
            {'representative_point': [True, 2.0]}
        ),
        lambda room: room['properties'].update(
            {'representative_point': [math.nan, 2.0]}
        ),
        lambda room: room['properties'].update(
            {'representative_point': [2.0]}
        ),
        lambda room: room['properties'].update(
            {'representative_point': [0.0, 2.0]}
        ),
        lambda room: room['properties'].update(
            {'representative_point': [20.0, 20.0]}
        ),
        lambda room: room['properties'].update({'area_m2': 999.0}),
        lambda room: room.update({'id': 'room-other'}),
    ],
)
def test_representative_point_and_room_metadata_fail_closed(mutation) -> None:
    """Only a finite interior point tied to the same room is accepted."""
    room = _room('room-a', '방 A', 0.0, 4.0)
    mutation(room)

    with pytest.raises(NamedNavigationError) as error:
        _catalog(_user_map(room))

    assert error.value.code == 'invalid_user_map'


def test_self_intersecting_geometry_fails_closed() -> None:
    """A representative point cannot legitimize ambiguous room geometry."""
    room = _room('room-a', '방 A', 0.0, 4.0)
    room['geometry']['coordinates'] = [[
        [0.0, 0.0],
        [4.0, 4.0],
        [0.0, 4.0],
        [4.0, 0.0],
        [0.0, 0.0],
    ]]

    with pytest.raises(NamedNavigationError, match='self-intersects'):
        _catalog(_user_map(room))


def test_semantic_and_binding_digests_are_deterministic_and_scoped() -> None:
    """Semantic content and exact source provenance remain separately bound."""
    first_map = _user_map(
        _room('room-a', '방 A', 0.0, 4.0),
        _room('room-b', '방 B', 6.0, 10.0),
    )
    reordered = copy.deepcopy(first_map)
    reordered['features'][1:] = reversed(reordered['features'][1:])
    first = _catalog(first_map)
    second = _catalog(reordered)
    first_target = first.resolve('방 A')
    second_target = second.resolve('방 A')

    expected_source_digest = hashlib.sha256(json.dumps(
        first_map,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')).hexdigest()
    assert first.source_digest == expected_source_digest
    assert first.semantic_digest == second.semantic_digest
    assert first.source_digest != second.source_digest
    assert first_target.binding_digest != second_target.binding_digest

    pinned = _catalog(reordered, source_digest=first.source_digest)
    assert pinned.semantic_digest == first.semantic_digest
    assert pinned.source_digest == first.source_digest
    assert (
        pinned.resolve('방 A').binding_digest
        == first_target.binding_digest
    )

    other_device = _catalog(first_map, device_id='malbut-sim-02')
    assert other_device.semantic_digest == first.semantic_digest
    assert (
        other_device.resolve('방 A').binding_digest
        != first_target.binding_digest
    )

    shifted = copy.deepcopy(first_map)
    shifted['features'][1]['properties']['representative_point'] = [
        2.5, 2.0,
    ]
    shifted_catalog = _catalog(shifted)
    assert shifted_catalog.semantic_digest != first.semantic_digest
    assert (
        shifted_catalog.resolve('방 A').binding_digest
        != first_target.binding_digest
    )


def test_public_output_redacts_device_coordinates_and_room_id() -> None:
    """Default serialization hides identity, coordinates, and fingerprints."""
    catalog = _catalog()
    target = catalog.resolve('거실')

    public = target.to_dict()
    private = target.to_private_dict()

    assert public == target.to_public_dict()
    assert set(public) == {
        'schema_version',
        'room_name',
        'room_category',
        'execution_authorized',
    }
    assert public['execution_authorized'] is False
    for key in ('device_id', 'map_id', 'map_revision', 'room_id', 'x', 'y'):
        assert key not in public
    assert private['device_id'] == 'malbut-sim-01'
    assert (private['x'], private['y']) == (2.0, 2.0)
    assert private['user_map_digest'] == catalog.source_digest
    assert 'source_digest' not in private
    assert private['execution_authorized'] is False
    assert catalog.source_digest not in repr(catalog)
    assert target.source_digest not in repr(target)


@pytest.mark.parametrize(
    'source_digest',
    ['', '0' * 63, '0' * 63 + 'G', 'A' * 64, 7],
)
def test_explicit_source_digest_must_be_exact_lowercase_sha256(
    source_digest,
) -> None:
    """Only an exact content fingerprint may override the fallback."""
    with pytest.raises(NamedNavigationError) as error:
        _catalog(source_digest=source_digest)
    assert error.value.code == 'invalid_identity'


def test_private_fixture_metadata_must_bind_the_same_device() -> None:
    """An isolated test fixture cannot redirect a target to another device."""
    value = _user_map(_room('room-a', '거실', 0.0, 4.0))
    value['fixture'] = {
        'format': 'malbut-named-navigation-fixture/v1',
        'device_id': 'malbut-sim-01',
        'purpose': 'SWM25-130 explicit Gazebo test only',
    }
    assert _catalog(value).resolve('거실').device_id == 'malbut-sim-01'

    value['fixture']['device_id'] = 'malbut-sim-02'
    with pytest.raises(NamedNavigationError) as error:
        _catalog(value)
    assert error.value.code == 'identity_mismatch'


def test_current_small_house_asset_resolves_one_fixed_candidate() -> None:
    """Pin the real Small House semantic asset to its canonical target."""
    slam_map = load_slam_map(GAZEBO_ROOT / 'maps' / 'small_house.yaml')
    user_map = json.loads((
        SCENARIO_ROOT / 'maps' / 'small_house_user_map.geojson'
    ).read_text(encoding='utf-8'))
    catalog = parse_named_navigation_catalog(
        user_map,
        device_id=DEVICE_ID,
        expected_map_id=slam_map.map_id,
        expected_map_revision=slam_map.map_revision,
    )

    target = catalog.resolve('  공간   1  ')

    assert catalog.room_count == 1
    assert target.device_id == DEVICE_ID
    assert target.map_id == 'map-a0843f4df527'
    assert target.map_revision == 'rev-5e8bdb2b88ea'
    assert target.frame_id == 'map'
    assert target.room_id == 'room-1'
    assert target.room_name == '공간 1'
    assert target.room_category == 'unassigned'
    assert (target.x, target.y, target.yaw) == (5.35, -1.8, 0.0)
    with pytest.raises(NamedNavigationError) as error:
        catalog.resolve('거실')
    assert error.value.code == 'target_not_found'


def test_module_has_no_ros_network_or_execution_dependency() -> None:
    """The resolver remains pure and cannot send or plan a Nav2 goal."""
    source = (
        GAZEBO_ROOT / 'malbut_gazebo' / 'named_navigation.py'
    ).read_text(encoding='utf-8')

    for forbidden in (
        'rclpy',
        'nav2_msgs',
        'ActionClient',
        'NavigateToPose',
        'send_goal',
        'requests',
        'urllib',
        'socket',
    ):
        assert forbidden not in source


def test_manual_target_rejects_nonzero_yaw() -> None:
    """No caller may smuggle a second pose choice into the fixed target."""
    with pytest.raises(NamedNavigationError) as error:
        NamedNavigationTarget(
            device_id='malbut-sim-01',
            map_id='map-home',
            map_revision='rev-home-001',
            semantic_digest='0' * 64,
            source_digest='1' * 64,
            frame_id='map',
            room_id='room-a',
            room_name='방 A',
            room_category='custom',
            x=2.0,
            y=2.0,
            yaw=1.0,
        )
    assert error.value.code == 'invalid_target'
