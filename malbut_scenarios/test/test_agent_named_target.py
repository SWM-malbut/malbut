"""Contracts for the non-actuating Agent target adapter."""

import hashlib
import json

import pytest

from malbut_gazebo.named_navigation import (
    NamedNavigationError,
    parse_named_navigation_catalog,
)
from malbut_scenarios.agent_named_target import CatalogNamedTargetResolver


def _catalog(name='거실', *, revision='revision-1'):
    value = {
        'type': 'FeatureCollection',
        'format': 'malbut-user-map-v1',
        'map_id': 'map-1',
        'map_revision': revision,
        'frame_id': 'map',
        'room_segmentation': {'room_count': 1},
        'features': [{
            'type': 'Feature',
            'id': 'private-room-id',
            'properties': {
                'role': 'room',
                'room_id': 'private-room-id',
                'name': name,
                'category': 'living_room',
                'area_m2': 16.0,
                'representative_point': [1.0, 1.0],
                'clearance_m': 1.0,
            },
            'geometry': {
                'type': 'Polygon',
                'coordinates': [[
                    [0.0, 0.0], [4.0, 0.0], [4.0, 4.0],
                    [0.0, 4.0], [0.0, 0.0],
                ]],
            },
        }],
    }
    return parse_named_navigation_catalog(
        value,
        device_id='private-device',
        expected_map_id='map-1',
        expected_map_revision=revision,
        source_digest=hashlib.sha256(
            json.dumps(value, sort_keys=True).encode()
        ).hexdigest(),
    )


def test_resolver_returns_redacted_binding_without_motion_calls():
    loads = []

    def load():
        loads.append('load')
        return _catalog()

    target = CatalogNamedTargetResolver(load).resolve('거실')

    assert loads == ['load']
    assert len(target.binding_digest) == 64
    assert target.to_public_dict() == {
        'room_name': '거실',
        'room_category': 'living_room',
        'execution_authorized': False,
    }
    assert 'private-device' not in repr(target)
    assert 'private-room-id' not in repr(target)


def test_unknown_name_fails_closed_without_a_fallback_target():
    resolver = CatalogNamedTargetResolver(_catalog)
    with pytest.raises(NamedNavigationError) as caught:
        resolver.resolve('아무데나')
    assert caught.value.code == 'target_not_found'


def test_binding_changes_when_authoritative_map_revision_changes():
    first = CatalogNamedTargetResolver(
        lambda: _catalog(revision='revision-1')
    ).resolve('거실')
    second = CatalogNamedTargetResolver(
        lambda: _catalog(revision='revision-2')
    ).resolve('거실')
    assert first.binding_digest != second.binding_digest
