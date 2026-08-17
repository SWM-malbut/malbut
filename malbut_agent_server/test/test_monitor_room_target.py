"""Tests for strict semantic-room target binding."""

import copy
import dataclasses
import hashlib
import math

import pytest

from malbut_agent_server.monitor_room_target import (
    Effects,
    TargetBinding,
    TargetResolutionError,
    parse_trusted_semantic_snapshot,
    resolve_monitor_room_target,
)
from malbut_agent_server.schemas import ValidationError


def _room(
    room_id: str,
    name: str,
    category: str,
    minimum_x: float,
    maximum_x: float,
) -> dict:
    return {
        'type': 'Feature',
        'id': room_id,
        'properties': {
            'role': 'room',
            'room_id': room_id,
            'name': name,
            'category': category,
            'area_m2': round((maximum_x - minimum_x) * 4.0, 2),
            'representative_point': [
                (minimum_x + maximum_x) / 2.0,
                2.0,
            ],
            'clearance_m': min((maximum_x - minimum_x) / 2.0, 2.0),
            'color': '#dce8ff',
            'generated': False,
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


def _payload(*rooms: dict) -> dict:
    walkable = {
        'type': 'Feature',
        'id': 'walkable-area',
        'properties': {'role': 'walkable_area'},
        'geometry': {
            'type': 'Polygon',
            'coordinates': [[
                [0.0, 0.0],
                [10.0, 0.0],
                [10.0, 4.0],
                [0.0, 4.0],
                [0.0, 0.0],
            ]],
        },
    }
    return {
        'revision': '20260815T010203Z-aabbccddee',
        'mapId': 'map-home',
        'mapRevision': 'rev-grid-001',
        'userMap': {
            'type': 'FeatureCollection',
            'format': 'malbut-user-map-v1',
            'map_id': 'map-home',
            'map_revision': 'rev-grid-001',
            'legacy_map_ids': ['map-legacy-home'],
            'frame_id': 'map',
            'generated_at': '2026-08-15T01:02:03+00:00',
            'source': {'resolution': 0.05},
            'room_segmentation': {
                'method': 'user_edited',
                'room_count': len(rooms),
            },
            'features': [walkable, *rooms],
        },
        'zones': None,
    }


def _home_payload() -> dict:
    return _payload(
        _room('room-living', '거실', 'living_room', 0.0, 4.0),
        _room('room-kitchen', '주방', 'kitchen', 6.0, 10.0),
    )


def _effects(**updates) -> Effects:
    value = {
        'physical_navigation': True,
        'camera_capture': True,
        'external_video_stream': True,
        'video_recording': False,
        'audio_capture': False,
        'max_duration_seconds': 300,
        'coverage_mode': 'whole_room',
        'viewer_scope': 'requesting_user',
        'talkback_allowed': False,
    }
    value.update(updates)
    return Effects(**value)


def _snapshot(payload=None):
    return parse_trusted_semantic_snapshot(
        _home_payload() if payload is None else payload,
        device_id='malbut-sim-01',
        device_binding_revision='membership-rev-001',
        source_is_finalized=True,
    )


def test_exact_name_builds_immutable_content_binding() -> None:
    """A finalized exact room name binds all target and effect evidence."""
    effects = _effects()
    snapshot = _snapshot()
    binding = resolve_monitor_room_target(snapshot, '  거실  ', effects)

    assert binding.device_id == 'malbut-sim-01'
    assert binding.device_binding_revision == 'membership-rev-001'
    assert binding.source_revision == '20260815T010203Z-aabbccddee'
    assert binding.map_id == 'map-home'
    assert binding.map_revision == 'rev-grid-001'
    assert binding.semantic_revision == snapshot.semantic_revision
    assert binding.frame_id == 'map'
    assert binding.room_id == 'room-living'
    assert binding.room_name == '거실'
    assert binding.room_category == 'living_room'
    assert binding.representative_point == (2.0, 2.0)
    assert binding.area_m2 == 16.0
    assert len(binding.geometry_digest) == 64
    assert len(binding.effects_digest) == 64
    assert len(binding.binding_digest) == 64
    assert binding.source_arguments_digest == hashlib.sha256(
        '{"location":"  거실  "}'.encode('utf-8')
    ).hexdigest()
    assert binding.effects_digest == effects.digest
    assert binding.matches_snapshot(snapshot) is True

    public = binding.to_dict()
    assert public['effects'] == effects.to_dict()
    assert public['effects']['schema_version'] == 1
    assert public['execution_authorized'] is False
    assert set(public) == {
        'schema_version',
        'room_name',
        'room_category',
        'effects',
        'effects_digest',
        'binding_digest',
        'execution_authorized',
    }
    private = binding.to_private_dict()
    assert private['geometry']['type'] == 'Polygon'
    assert private['device_id'] == 'malbut-sim-01'
    assert private['device_binding_revision'] == 'membership-rev-001'
    assert private['source_arguments_digest'] == (
        binding.source_arguments_digest
    )
    private['geometry']['coordinates'][0][0][0] = 999.0
    assert binding.geometry_dict()['coordinates'][0][0] == [0.0, 0.0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.room_id = 'room-kitchen'


def test_name_normalization_and_fixed_aliases_are_exact() -> None:
    """NFKC names and fixed aliases work without substring matching."""
    payload = _payload(
        _room(
            'room-study',
            'Ｌｉｖｉｎｇ\u3000Ｒｏｏｍ',
            'workspace',
            0.0,
            4.0,
        ),
        _room('room-kitchen', '조리 공간', 'kitchen', 6.0, 10.0),
    )
    snapshot = _snapshot(payload)

    exact = resolve_monitor_room_target(
        snapshot,
        'living room',
        _effects(),
    )
    alias = resolve_monitor_room_target(snapshot, '부엌', _effects())

    assert exact.room_id == 'room-study'
    assert exact.room_name == 'Living Room'
    assert alias.room_id == 'room-kitchen'
    for unsupported in ('living', '조리', 'room-kitchen', 'room-kitchan'):
        with pytest.raises(TargetResolutionError) as error:
            resolve_monitor_room_target(snapshot, unsupported, _effects())
        assert error.value.code == 'target_not_found'


def test_exact_name_precedes_a_category_alias() -> None:
    """A user's exact room name wins before a fixed category alias."""
    payload = _payload(
        _room('room-named', '부엌', 'workspace', 0.0, 4.0),
        _room('room-category', '조리 공간', 'kitchen', 6.0, 10.0),
    )
    binding = resolve_monitor_room_target(
        _snapshot(payload),
        '부엌',
        _effects(),
    )
    assert binding.room_id == 'room-named'


def test_duplicate_name_or_category_is_ambiguous() -> None:
    """The resolver never selects the first room from multiple matches."""
    duplicate_name = _payload(
        _room('room-a', '가족 공간', 'living_room', 0.0, 4.0),
        _room('room-b', ' 가족   공간 ', 'workspace', 6.0, 10.0),
    )
    duplicate_category = _payload(
        _room('room-a', '첫 주방', 'kitchen', 0.0, 4.0),
        _room('room-b', '둘째 주방', 'kitchen', 6.0, 10.0),
    )

    for payload, query in (
        (duplicate_name, '가족 공간'),
        (duplicate_category, 'kitchen'),
    ):
        with pytest.raises(TargetResolutionError) as error:
            resolve_monitor_room_target(
                _snapshot(payload),
                query,
                _effects(),
            )
        assert error.value.code == 'target_ambiguous'


@pytest.mark.parametrize(
    'mutation',
    [
        lambda value: value.update({'extra': True}),
        lambda value: value['userMap'].update({'map_id': 'map-other'}),
        lambda value: value['userMap'].update(
            {'map_revision': 'rev-other'}
        ),
        lambda value: value['userMap'].update({'frame_id': 'odom'}),
        lambda value: value['userMap'].update(
            {'format': 'malbut-user-map-v2'}
        ),
        lambda value: value.update({'userMap': None}),
    ],
)
def test_snapshot_identity_and_shape_fail_closed(mutation) -> None:
    """Top-level and nested finalized identities must match exactly."""
    payload = _home_payload()
    mutation(payload)
    with pytest.raises(TargetResolutionError) as error:
        _snapshot(payload)
    assert error.value.code == 'invalid_semantic_snapshot'


def test_draft_or_untrusted_container_cannot_resolve() -> None:
    """A draft source or raw dictionary cannot obtain a target binding."""
    with pytest.raises(TargetResolutionError):
        parse_trusted_semantic_snapshot(
            _home_payload(),
            device_id='malbut-sim-01',
            device_binding_revision='membership-rev-001',
            source_is_finalized=False,
        )
    with pytest.raises(TargetResolutionError) as error:
        resolve_monitor_room_target(
            _home_payload(),
            '거실',
            _effects(),
        )
    assert error.value.code == 'invalid_semantic_snapshot'


@pytest.mark.parametrize(
    'mutate_room',
    [
        lambda room: room.update({'id': 'different-room'}),
        lambda room: room['properties'].update({'name': ''}),
        lambda room: room['properties'].update({'category': 'garage'}),
        lambda room: room['properties'].update(
            {'representative_point': [0.0, 2.0]}
        ),
        lambda room: room['properties'].update({'area_m2': 999.0}),
        lambda room: room['geometry']['coordinates'][0].pop(),
        lambda room: room['geometry'].update({'bbox': [0, 0, 4, 4]}),
    ],
)
def test_malformed_room_metadata_or_geometry_fails_closed(
    mutate_room,
) -> None:
    """Room identity, navigation metadata, and geometry are revalidated."""
    payload = _home_payload()
    room = payload['userMap']['features'][1]
    mutate_room(room)
    with pytest.raises(TargetResolutionError) as error:
        _snapshot(payload)
    assert error.value.code == 'invalid_semantic_snapshot'


def test_self_intersection_hole_and_nonfinite_values_are_rejected() -> None:
    """Invalid topology and non-finite direct Python values never hash."""
    bow_tie = _home_payload()
    bow_tie_room = bow_tie['userMap']['features'][1]
    bow_tie_room['geometry']['coordinates'] = [[
        [0.0, 0.0],
        [4.0, 4.0],
        [0.0, 4.0],
        [4.0, 0.0],
        [0.0, 0.0],
    ]]
    with pytest.raises(TargetResolutionError):
        _snapshot(bow_tie)

    hole = _home_payload()
    hole_room = hole['userMap']['features'][1]
    hole_room['geometry']['coordinates'].append([
        [1.0, 1.0],
        [3.0, 1.0],
        [3.0, 3.0],
        [1.0, 3.0],
        [1.0, 1.0],
    ])
    hole_room['properties']['area_m2'] = 12.0
    with pytest.raises(TargetResolutionError, match='representative_point'):
        _snapshot(hole)

    nonfinite = _home_payload()
    nonfinite['userMap']['features'][1]['geometry']['coordinates'][0][0][
        0
    ] = math.nan
    with pytest.raises(TargetResolutionError, match='non-finite'):
        _snapshot(nonfinite)


def test_room_id_and_historical_metadata_are_not_lookup_aliases() -> None:
    """Machine IDs and split history cannot silently redirect a request."""
    payload = _home_payload()
    room = payload['userMap']['features'][1]
    room['properties'].update(
        {
            'split_parent_name': '옛 거실',
            'merged_from_names': ['예전 방'],
        }
    )
    snapshot = _snapshot(payload)
    for query in ('room-living', '옛 거실', '예전 방'):
        with pytest.raises(TargetResolutionError) as error:
            resolve_monitor_room_target(snapshot, query, _effects())
        assert error.value.code == 'target_not_found'


def test_semantic_revision_is_canonical_and_order_independent() -> None:
    """Room order is irrelevant while meaningful semantics remain bound."""
    first_payload = _home_payload()
    reordered = copy.deepcopy(first_payload)
    reordered['userMap']['features'][1:] = reversed(
        reordered['userMap']['features'][1:]
    )
    first = _snapshot(first_payload)
    second = _snapshot(reordered)
    assert first.semantic_revision == second.semantic_revision

    renamed = copy.deepcopy(first_payload)
    renamed['userMap']['features'][1]['properties']['name'] = '가족 거실'
    third = _snapshot(renamed)
    assert third.semantic_revision != first.semantic_revision

    shifted = copy.deepcopy(first_payload)
    shifted_room = shifted['userMap']['features'][1]
    shifted_room['geometry']['coordinates'][0] = [
        [0.5, 0.0],
        [4.5, 0.0],
        [4.5, 4.0],
        [0.5, 4.0],
        [0.5, 0.0],
    ]
    shifted_room['properties']['representative_point'] = [2.5, 2.0]
    fourth = _snapshot(shifted)
    assert fourth.semantic_revision != first.semantic_revision


def test_zone_changes_invalidate_semantic_revision() -> None:
    """Restriction semantics are included even before Nav2 preflight."""
    payload = _home_payload()
    payload['zones'] = {
        'type': 'FeatureCollection',
        'format': 'malbut-semantic-zones-v1',
        'map_id': 'map-home',
        'map_revision': 'rev-grid-001',
        'features': [],
    }
    first = _snapshot(payload)
    changed = copy.deepcopy(payload)
    changed['zones']['features'].append(
        {'type': 'Feature', 'properties': {'behavior': 'restricted'}}
    )
    second = _snapshot(changed)
    assert second.semantic_revision != first.semantic_revision


def test_source_and_effect_changes_invalidate_complete_binding() -> None:
    """Upload revisions and disclosed effects are both binding evidence."""
    payload = _home_payload()
    first_snapshot = _snapshot(payload)
    first = resolve_monitor_room_target(
        first_snapshot,
        '거실',
        _effects(),
    )

    reuploaded = copy.deepcopy(payload)
    reuploaded['revision'] = '20260815T020304Z-ffeeddccbb'
    second_snapshot = _snapshot(reuploaded)
    second = resolve_monitor_room_target(
        second_snapshot,
        '거실',
        _effects(),
    )
    assert first.semantic_revision == second.semantic_revision
    assert first.binding_digest != second.binding_digest
    assert first.matches_snapshot(second_snapshot) is False

    same_target_new_arguments = resolve_monitor_room_target(
        first_snapshot,
        ' 거실 ',
        _effects(),
    )
    assert same_target_new_arguments.room_id == first.room_id
    assert (
        same_target_new_arguments.source_arguments_digest
        != first.source_arguments_digest
    )
    assert same_target_new_arguments.binding_digest != first.binding_digest

    membership_changed = parse_trusted_semantic_snapshot(
        payload,
        device_id='malbut-sim-01',
        device_binding_revision='membership-rev-002',
        source_is_finalized=True,
    )
    membership_binding = resolve_monitor_room_target(
        membership_changed,
        '거실',
        _effects(),
    )
    assert membership_changed.semantic_revision == first.semantic_revision
    assert membership_binding.binding_digest != first.binding_digest
    assert first.matches_snapshot(membership_changed) is False

    recording = resolve_monitor_room_target(
        first_snapshot,
        '거실',
        _effects(video_recording=True),
    )
    assert first.geometry_digest == recording.geometry_digest
    assert first.effects_digest != recording.effects_digest
    assert first.binding_digest != recording.binding_digest


def test_effects_are_strict_bounded_and_deterministic() -> None:
    """No truthy coercion, omitted effect, or unbounded TTL is accepted."""
    value = _effects().to_dict()
    assert Effects.from_dict(value).digest == Effects.from_dict(value).digest
    for invalid in (
        {**value, 'physical_navigation': 1},
        {**value, 'max_duration_seconds': 0},
        {**value, 'max_duration_seconds': 3601},
        {**value, 'coverage_mode': 'representative_point'},
        {**value, 'viewer_scope': 'all_family'},
        {**value, 'talkback_allowed': True},
        {**value, 'schema_version': 2},
        {key: item for key, item in value.items() if key != 'audio_capture'},
        {**value, 'unknown': False},
    ):
        with pytest.raises(ValidationError):
            Effects.from_dict(invalid)


def test_private_binding_round_trip_revalidates_every_digest() -> None:
    """A stored private record reconstructs only with exact evidence."""
    original = resolve_monitor_room_target(
        _snapshot(),
        '거실',
        _effects(),
    )
    restored = TargetBinding.from_private_dict(original.to_private_dict())
    assert restored == original
    assert restored.binding_digest == original.binding_digest
    assert restored.to_private_dict() == original.to_private_dict()


@pytest.mark.parametrize(
    'mutation',
    [
        lambda value: value.update({'unknown': True}),
        lambda value: value.update({'execution_authorized': True}),
        lambda value: value.update({'device_id': 'bad device'}),
        lambda value: value.update({'frame_id': 'odom'}),
        lambda value: value.update({'semantic_revision': '0' * 63}),
        lambda value: value.update(
            {'source_arguments_digest': '0' * 64}
        ),
        lambda value: value.update({'geometry_digest': '0' * 64}),
        lambda value: value['geometry']['coordinates'][0][1].__setitem__(
            0,
            5.0,
        ),
        lambda value: value.update({'representative_point': [9.0, 9.0]}),
        lambda value: value.update({'area_m2': 99.0}),
        lambda value: value['effects'].update({'talkback_allowed': True}),
        lambda value: value.update({'effects_digest': '0' * 64}),
        lambda value: value.update({'binding_digest': '0' * 64}),
    ],
)
def test_private_binding_tamper_matrix_fails_closed(mutation) -> None:
    """Stored IDs, geometry, effects, and digests cannot be altered."""
    binding = resolve_monitor_room_target(
        _snapshot(),
        '거실',
        _effects(),
    )
    private = binding.to_private_dict()
    mutation(private)
    with pytest.raises(ValidationError):
        TargetBinding.from_private_dict(private)


@pytest.mark.parametrize(
    ('field_name', 'invalid'),
    [
        ('device_id', 'bad device'),
        ('device_binding_revision', ''),
        ('source_revision', 'bad revision'),
        ('map_id', 'bad map'),
        ('map_revision', 'bad revision'),
        ('semantic_revision', '0' * 63),
        ('frame_id', 'odom'),
        ('room_id', 'bad room'),
        ('room_category', 'garage'),
        ('source_arguments_digest', '0' * 63),
        ('geometry_digest', '0' * 64),
        ('representative_point', (9.0, 9.0)),
        ('clearance_m', math.nan),
        ('area_m2', 99.0),
    ],
)
def test_manual_binding_construction_revalidates_fields(
    field_name: str,
    invalid,
) -> None:
    """Frozen dataclass construction alone cannot bypass validation."""
    binding = resolve_monitor_room_target(
        _snapshot(),
        '거실',
        _effects(),
    )
    with pytest.raises(ValidationError):
        dataclasses.replace(binding, **{field_name: invalid})


@pytest.mark.parametrize(
    'revision',
    ['', 'membership revision', 'x' * 129, None],
)
def test_device_binding_revision_is_required_and_strict(revision) -> None:
    """Membership changes use a server-owned bounded revision fence."""
    with pytest.raises(TargetResolutionError):
        parse_trusted_semantic_snapshot(
            _home_payload(),
            device_id='malbut-sim-01',
            device_binding_revision=revision,
            source_is_finalized=True,
        )


def test_duplicate_ids_and_resource_bounds_fail_closed() -> None:
    """Duplicate authority IDs and excessive features are rejected early."""
    duplicate = _payload(
        _room('room-same', '방 A', 'custom', 0.0, 4.0),
        _room('room-same', '방 B', 'custom', 6.0, 10.0),
    )
    with pytest.raises(TargetResolutionError, match='unique'):
        _snapshot(duplicate)

    excessive = _home_payload()
    feature = excessive['userMap']['features'][0]
    excessive['userMap']['features'] = [
        copy.deepcopy(feature) for _ in range(1025)
    ]
    with pytest.raises(TargetResolutionError, match='unbounded'):
        _snapshot(excessive)
