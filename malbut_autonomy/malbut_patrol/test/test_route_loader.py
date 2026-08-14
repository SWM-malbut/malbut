"""Tests for strict patrol route configuration loading."""

from pathlib import Path

import pytest

from malbut_patrol.route_loader import RouteConfigError, load_route


VALID_ROUTE = """
schema_version: 1
route:
  name: test_route
  map_id: test_map
  frame_id: map
  cycles_per_run: 2
  defaults:
    dwell_seconds: 1.5
    max_retries: 1
    retry_backoff_seconds: 2.0
    on_failure: skip
  waypoints:
    - name: first
      pose: {x: 1.0, y: 2.0, yaw: 0.0}
    - name: second
      pose: {x: -1.0, y: 0.5, yaw: 1.57}
      dwell_seconds: 4.0
      max_retries: 2
      on_failure: abort
schedule:
  mode: interval
  interval_seconds: 30.0
"""


def _write(tmp_path: Path, text: str) -> Path:
    route_file = tmp_path / 'route.yaml'
    route_file.write_text(text, encoding='utf-8')
    return route_file


def test_load_route_applies_defaults_and_waypoint_overrides(tmp_path):
    route = load_route(_write(tmp_path, VALID_ROUTE))

    assert route.name == 'test_route'
    assert route.map_id == 'test_map'
    assert route.frame_id == 'map'
    assert route.cycles_per_run == 2
    assert route.schedule.mode == 'interval'
    assert route.schedule.interval_seconds == 30.0
    assert len(route.waypoints) == 2
    assert route.waypoints[0].dwell_seconds == 1.5
    assert route.waypoints[0].max_retries == 1
    assert route.waypoints[0].on_failure == 'skip'
    assert route.waypoints[1].dwell_seconds == 4.0
    assert route.waypoints[1].max_retries == 2
    assert route.waypoints[1].on_failure == 'abort'


@pytest.mark.parametrize(
    ('old', 'new', 'message'),
    [
        ('schema_version: 1', 'schema_version: 2', 'schema_version'),
        ('frame_id: map', 'frame_id: /map', 'relative frame'),
        ('cycles_per_run: 2', 'cycles_per_run: 0', 'greater than zero'),
        ('interval_seconds: 30.0', 'interval_seconds: 0', 'greater than zero'),
        ('name: second', 'name: first', 'names must be unique'),
        ('on_failure: abort', 'on_failure: reverse', 'must be one of'),
        ('yaw: 1.57', 'yaw: .nan', 'finite number'),
        ('schema_version: 1', 'schema_version: true', 'schema_version'),
        ('schema_version: 1', 'schema_version: 1.0', 'schema_version'),
    ],
)
def test_invalid_route_values_are_rejected(tmp_path, old, new, message):
    text = VALID_ROUTE.replace(old, new)

    with pytest.raises(RouteConfigError, match=message):
        load_route(_write(tmp_path, text))


def test_unknown_fields_are_rejected(tmp_path):
    text = VALID_ROUTE.replace(
        '  map_id: test_map',
        '  map_id: test_map\n  typo_field: true',
    )

    with pytest.raises(RouteConfigError, match='unknown fields: typo_field'):
        load_route(_write(tmp_path, text))


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    text = VALID_ROUTE.replace(
        '  mode: interval',
        '  mode: interval\n  mode: manual',
    )

    with pytest.raises(RouteConfigError, match='duplicate YAML key: mode'):
        load_route(_write(tmp_path, text))


def test_missing_route_file_is_reported(tmp_path):
    with pytest.raises(RouteConfigError, match='does not exist'):
        load_route(tmp_path / 'missing.yaml')


def test_manual_schedule_rejects_unused_interval(tmp_path):
    text = VALID_ROUTE.replace('mode: interval', 'mode: manual')

    with pytest.raises(RouteConfigError, match='zero or omitted'):
        load_route(_write(tmp_path, text))


def test_unrepresentable_number_has_a_configuration_error(tmp_path):
    enormous_number = '9' * 400
    text = VALID_ROUTE.replace('yaw: 1.57', f'yaw: {enormous_number}')

    with pytest.raises(RouteConfigError, match='finite number'):
        load_route(_write(tmp_path, text))
