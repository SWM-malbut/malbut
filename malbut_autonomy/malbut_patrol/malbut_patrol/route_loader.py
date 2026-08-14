"""Load and validate patrol route configuration files."""

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

import yaml


FAILURE_ACTIONS = frozenset({'abort', 'skip'})
SCHEDULE_MODES = frozenset({'interval', 'manual'})


class RouteConfigError(ValueError):
    """Raised when a patrol route configuration is invalid."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise RouteConfigError(f'duplicate YAML key: {key}')
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class Waypoint:
    """One named navigation goal and its patrol behavior."""

    name: str
    x: float
    y: float
    yaw: float
    dwell_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    on_failure: str


@dataclass(frozen=True)
class Schedule:
    """How patrol runs repeat after one finite route run."""

    mode: str
    interval_seconds: float


@dataclass(frozen=True)
class PatrolRoute:
    """A validated ordered patrol route."""

    name: str
    map_id: str
    frame_id: str
    cycles_per_run: int
    waypoints: tuple[Waypoint, ...]
    schedule: Schedule


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RouteConfigError(f'{location} must be a mapping')
    return value


def _reject_unknown(
    value: dict[str, Any],
    allowed: set[str],
    location: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        names = ', '.join(unknown)
        raise RouteConfigError(f'{location} has unknown fields: {names}')


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RouteConfigError(f'{location} must be a non-empty string')
    return value.strip()


def _number(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RouteConfigError(f'{location} must be a finite number')
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise RouteConfigError(
            f'{location} must be a finite number'
        ) from error
    if not math.isfinite(number):
        raise RouteConfigError(f'{location} must be a finite number')
    return number


def _nonnegative_number(value: Any, location: str) -> float:
    number = _number(value, location)
    if number < 0.0:
        raise RouteConfigError(f'{location} must be zero or greater')
    return number


def _nonnegative_integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RouteConfigError(f'{location} must be a non-negative integer')
    return value


def _positive_integer(value: Any, location: str) -> int:
    number = _nonnegative_integer(value, location)
    if number == 0:
        raise RouteConfigError(f'{location} must be greater than zero')
    return number


def _choice(value: Any, choices: frozenset[str], location: str) -> str:
    selected = _string(value, location)
    if selected not in choices:
        expected = ', '.join(sorted(choices))
        raise RouteConfigError(
            f'{location} must be one of: {expected}'
        )
    return selected


def _waypoint(
    value: Any,
    index: int,
    defaults: dict[str, Any],
) -> Waypoint:
    location = f'route.waypoints[{index}]'
    waypoint = _mapping(value, location)
    _reject_unknown(
        waypoint,
        {
            'dwell_seconds',
            'max_retries',
            'name',
            'on_failure',
            'pose',
            'retry_backoff_seconds',
        },
        location,
    )
    pose = _mapping(waypoint.get('pose'), f'{location}.pose')
    _reject_unknown(pose, {'x', 'y', 'yaw'}, f'{location}.pose')
    for required in ('x', 'y', 'yaw'):
        if required not in pose:
            raise RouteConfigError(
                f'{location}.pose.{required} is required'
            )

    dwell = waypoint.get(
        'dwell_seconds',
        defaults['dwell_seconds'],
    )
    retries = waypoint.get(
        'max_retries',
        defaults['max_retries'],
    )
    retry_backoff = waypoint.get(
        'retry_backoff_seconds',
        defaults['retry_backoff_seconds'],
    )
    failure_action = waypoint.get(
        'on_failure',
        defaults['on_failure'],
    )
    return Waypoint(
        name=_string(waypoint.get('name'), f'{location}.name'),
        x=_number(pose['x'], f'{location}.pose.x'),
        y=_number(pose['y'], f'{location}.pose.y'),
        yaw=_number(pose['yaw'], f'{location}.pose.yaw'),
        dwell_seconds=_nonnegative_number(
            dwell,
            f'{location}.dwell_seconds',
        ),
        max_retries=_nonnegative_integer(
            retries,
            f'{location}.max_retries',
        ),
        retry_backoff_seconds=_nonnegative_number(
            retry_backoff,
            f'{location}.retry_backoff_seconds',
        ),
        on_failure=_choice(
            failure_action,
            FAILURE_ACTIONS,
            f'{location}.on_failure',
        ),
    )


def load_route(path: str | Path) -> PatrolRoute:
    """Load one route YAML file and return a validated patrol route."""
    route_path = Path(path).expanduser()
    if not route_path.is_file():
        raise RouteConfigError(f'route file does not exist: {route_path}')

    try:
        document = yaml.load(
            route_path.read_text(encoding='utf-8'),
            Loader=_UniqueKeyLoader,
        )
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise RouteConfigError(
            f'cannot read route file {route_path}: {error}'
        ) from error

    root = _mapping(document, 'document')
    _reject_unknown(root, {'route', 'schedule', 'schema_version'}, 'document')
    schema_version = root.get('schema_version')
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise RouteConfigError('schema_version must be 1')

    route_data = _mapping(root.get('route'), 'route')
    _reject_unknown(
        route_data,
        {
            'cycles_per_run',
            'defaults',
            'frame_id',
            'map_id',
            'name',
            'waypoints',
        },
        'route',
    )
    frame_id = _string(route_data.get('frame_id', 'map'), 'route.frame_id')
    if frame_id.startswith('/'):
        raise RouteConfigError('route.frame_id must be a relative frame name')

    defaults_data = _mapping(route_data.get('defaults', {}), 'route.defaults')
    _reject_unknown(
        defaults_data,
        {
            'dwell_seconds',
            'max_retries',
            'on_failure',
            'retry_backoff_seconds',
        },
        'route.defaults',
    )
    defaults = {
        'dwell_seconds': _nonnegative_number(
            defaults_data.get('dwell_seconds', 0.0),
            'route.defaults.dwell_seconds',
        ),
        'max_retries': _nonnegative_integer(
            defaults_data.get('max_retries', 0),
            'route.defaults.max_retries',
        ),
        'retry_backoff_seconds': _nonnegative_number(
            defaults_data.get('retry_backoff_seconds', 0.0),
            'route.defaults.retry_backoff_seconds',
        ),
        'on_failure': _choice(
            defaults_data.get('on_failure', 'abort'),
            FAILURE_ACTIONS,
            'route.defaults.on_failure',
        ),
    }

    waypoint_values = route_data.get('waypoints')
    if not isinstance(waypoint_values, list) or not waypoint_values:
        raise RouteConfigError('route.waypoints must be a non-empty list')
    waypoints = tuple(
        _waypoint(value, index, defaults)
        for index, value in enumerate(waypoint_values)
    )
    names = [waypoint.name for waypoint in waypoints]
    if len(names) != len(set(names)):
        raise RouteConfigError('route waypoint names must be unique')

    schedule_data = _mapping(root.get('schedule', {}), 'schedule')
    _reject_unknown(
        schedule_data,
        {'interval_seconds', 'mode'},
        'schedule',
    )
    schedule_mode = _choice(
        schedule_data.get('mode', 'manual'),
        SCHEDULE_MODES,
        'schedule.mode',
    )
    interval_seconds = _nonnegative_number(
        schedule_data.get('interval_seconds', 0.0),
        'schedule.interval_seconds',
    )
    if schedule_mode == 'interval' and interval_seconds <= 0.0:
        raise RouteConfigError(
            'schedule.interval_seconds must be greater than zero '
            'for interval mode'
        )
    if schedule_mode == 'manual' and interval_seconds != 0.0:
        raise RouteConfigError(
            'schedule.interval_seconds must be zero or omitted '
            'for manual mode'
        )

    return PatrolRoute(
        name=_string(route_data.get('name'), 'route.name'),
        map_id=_string(route_data.get('map_id'), 'route.map_id'),
        frame_id=frame_id,
        cycles_per_run=_positive_integer(
            route_data.get('cycles_per_run', 1),
            'route.cycles_per_run',
        ),
        waypoints=waypoints,
        schedule=Schedule(
            mode=schedule_mode,
            interval_seconds=interval_seconds,
        ),
    )
