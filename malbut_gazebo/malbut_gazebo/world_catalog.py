"""Validation and lookup helpers for Malbut simulation worlds."""

import math
import re
from pathlib import Path

import yaml


_WORLD_NAME = re.compile(r'^[a-z][a-z0-9_]*$')
_SPAWN_KEYS = ('x', 'y', 'z', 'yaw')


def load_world_catalog(catalog_file):
    """Load and validate the world catalog."""
    catalog_path = Path(catalog_file)
    with catalog_path.open(encoding='utf-8') as stream:
        raw = yaml.safe_load(stream)

    if not isinstance(raw, dict) or raw.get('schema_version') != 1:
        raise RuntimeError(
            f'Unsupported or missing world catalog schema: {catalog_path}'
        )

    worlds = raw.get('worlds')
    if not isinstance(worlds, dict) or not worlds:
        raise RuntimeError(f'World catalog is empty: {catalog_path}')

    normalized = {}
    for name, entry in worlds.items():
        if not isinstance(name, str) or not _WORLD_NAME.fullmatch(name):
            raise RuntimeError(f'Invalid world name in catalog: {name!r}')
        if not isinstance(entry, dict):
            raise RuntimeError(f'World entry must be a mapping: {name}')

        filename = entry.get('file')
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith('.sdf')
        ):
            raise RuntimeError(f'Invalid world filename for {name}: {filename}')

        spawn = entry.get('spawn')
        if not isinstance(spawn, dict) or set(spawn) != set(_SPAWN_KEYS):
            raise RuntimeError(
                f'World {name} must define spawn keys: {_SPAWN_KEYS}'
            )

        normalized_spawn = {}
        for key in _SPAWN_KEYS:
            value = spawn[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RuntimeError(f'World {name} spawn.{key} is not numeric')
            value = float(value)
            if not math.isfinite(value):
                raise RuntimeError(f'World {name} spawn.{key} is not finite')
            normalized_spawn[key] = value

        normalized[name] = {
            **entry,
            'file': filename,
            'spawn': normalized_spawn,
        }

    return normalized


def resolve_world(catalog_file, worlds_directory, world_name):
    """Resolve one configured world and ensure its SDF file exists."""
    catalog = load_world_catalog(catalog_file)
    if world_name not in catalog:
        choices = ', '.join(sorted(catalog))
        raise RuntimeError(
            f'Unknown world_name {world_name!r}; choose one of: {choices}'
        )

    worlds_path = Path(worlds_directory).resolve()
    world_file = worlds_path / catalog[world_name]['file']
    if world_file.parent != worlds_path or not world_file.is_file():
        raise RuntimeError(f'Configured world file does not exist: {world_file}')

    return world_file, catalog[world_name]
