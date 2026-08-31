"""Load benchmark scenarios and prepare measurement-only Gazebo worlds."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import tempfile
from xml.etree import ElementTree

from ament_index_python.packages import get_package_share_directory
import yaml


@dataclass(frozen=True)
class Pose:
    """One configured simulator spawn pose."""

    x: float
    y: float
    z: float
    yaw: float


@dataclass(frozen=True)
class BenchmarkScenario:
    """Resolved files and poses for one benchmark run."""

    name: str
    world_name: str
    world_file: Path
    map_file: Path
    actor_file: Path
    trajectory: str
    robot_pose: Pose
    actor_pose: Pose
    measurement_duration_s: float


def _package_file(value: object, label: str) -> Path:
    if not isinstance(value, dict):
        raise ValueError(f'{label} must contain package and path')
    package = str(value.get('package', '')).strip()
    relative = str(value.get('path', '')).strip()
    if not package or not relative or Path(relative).is_absolute():
        raise ValueError(f'{label} must use a package-relative path')
    result = Path(get_package_share_directory(package)) / relative
    if not result.is_file():
        raise FileNotFoundError(f'{label} does not exist: {result}')
    return result


def _pose(value: object, label: str) -> Pose:
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object')
    fields = []
    for name in ('x', 'y', 'z', 'yaw'):
        try:
            field = float(value[name])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f'{label}.{name} must be numeric') from error
        if not math.isfinite(field):
            raise ValueError(f'{label}.{name} must be finite')
        fields.append(field)
    return Pose(*fields)


def load_scenario(catalog_file: Path, scenario_name: str) -> BenchmarkScenario:
    """Resolve one named scenario from the installed YAML catalog."""
    raw = yaml.safe_load(catalog_file.read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or raw.get('schema_version') != 1:
        raise ValueError('benchmark scenario catalog schema_version must be 1')
    scenarios = raw.get('scenarios')
    if not isinstance(scenarios, dict) or scenario_name not in scenarios:
        available = ', '.join(sorted(scenarios or {}))
        raise ValueError(
            f'unknown benchmark scenario {scenario_name!r}; '
            f'available: {available}'
        )
    value = scenarios[scenario_name]
    if not isinstance(value, dict):
        raise ValueError(f'scenario {scenario_name!r} must be an object')
    world_name = str(value.get('world_name', '')).strip()
    trajectory = str(value.get('trajectory', '')).strip()
    if world_name not in {'test_arena', 'small_house'}:
        raise ValueError(
            'benchmarks currently support test_arena or small_house'
        )
    if not trajectory:
        raise ValueError('scenario trajectory label is required')
    try:
        measurement_duration_s = float(
            value.get('measurement_duration_s', 180.0)
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            'scenario measurement_duration_s must be numeric'
        ) from error
    if not math.isfinite(measurement_duration_s) or measurement_duration_s <= 0:
        raise ValueError(
            'scenario measurement_duration_s must be positive'
        )
    return BenchmarkScenario(
        name=scenario_name,
        world_name=world_name,
        world_file=_package_file(value.get('world_file'), 'world_file'),
        map_file=_package_file(value.get('map_file'), 'map_file'),
        actor_file=_package_file(value.get('actor_file'), 'actor_file'),
        trajectory=trajectory,
        robot_pose=_pose(value.get('robot_pose'), 'robot_pose'),
        actor_pose=_pose(value.get('actor_pose'), 'actor_pose'),
        measurement_duration_s=measurement_duration_s,
    )


def optional_file(value: str, fallback: Path, label: str) -> Path:
    """Resolve an optional absolute override without hiding missing files."""
    if not value.strip():
        return fallback
    result = Path(value).expanduser().resolve()
    if not result.is_file():
        raise FileNotFoundError(f'{label} does not exist: {result}')
    return result


def instrument_world(
    source: Path,
    *,
    robot_name: str,
    actor_name: str,
    topic: str,
    publish_rate_hz: float,
) -> Path:
    """Create a temporary world with passive ground-truth instrumentation."""
    tree = ElementTree.parse(source)
    world = tree.getroot().find('world')
    if world is None:
        raise ValueError(f'world element is missing from {source}')
    for plugin in world.findall('plugin'):
        if plugin.get('name') == 'malbut::gazebo::BenchmarkGroundTruthSystem':
            world.remove(plugin)
    plugin = ElementTree.Element(
        'plugin',
        {
            'filename': 'libbenchmark_ground_truth_system.so',
            'name': 'malbut::gazebo::BenchmarkGroundTruthSystem',
        },
    )
    for name, value in (
        ('robot_name', robot_name),
        ('actor_name', actor_name),
        ('topic', topic),
        ('publish_rate', f'{publish_rate_hz:.9g}'),
    ):
        ElementTree.SubElement(plugin, name).text = value
    world.insert(0, plugin)
    destination = Path(
        tempfile.mkdtemp(prefix='malbut-tracking-benchmark-')
    ) / source.name
    tree.write(destination, encoding='utf-8', xml_declaration=True)
    return destination
