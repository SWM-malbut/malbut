"""Tests for read-only active-map evidence used by the Gazebo gateway."""

from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest
import yaml

import malbut_gazebo.gazebo_monitor_room_active_map as active_map_module
from malbut_gazebo.gazebo_monitor_room_active_map import (
    MAX_ACTIVE_MANIFEST_BYTES,
    ActiveMapStaticNavigationProjection,
    ActiveMapChangedError,
    ActiveMapEvidence,
    ActiveMapEvidenceInvalidError,
    ActiveMapEvidenceResolver,
    ActiveMapProjectionInvalidError,
    ActiveMapResolverConfig,
    ActiveMapValidationError,
)
from malbut_gazebo.gazebo_monitor_room_navigation_safety import (
    StaticClearanceGrid,
)
from malbut_gazebo.map_lifecycle import MapGrid, persist_map_revision


def _grid() -> MapGrid:
    cells = np.full((40, 50), -1, dtype=np.int16)
    cells[5:35, 5:45] = 0
    cells[5, 5:45] = 100
    cells[34, 5:45] = 100
    cells[5:35, 5] = 100
    cells[5:35, 44] = 100
    cells.setflags(write=False)
    return MapGrid(50, 40, 0.1, -2.5, -2.0, 0.25, cells)


def _fixture(
    tmp_path: Path,
    grid: MapGrid | None = None,
) -> tuple[Path, dict]:
    store = tmp_path / 'protected-map-store'
    store.mkdir(mode=0o700)
    manifest = persist_map_revision(_grid() if grid is None else grid, store)
    os.chmod(store, 0o700)
    os.chmod(store / 'versions', 0o700)
    os.chmod(store / 'versions' / manifest['revision'], 0o700)
    for field_name in ('map_yaml', 'map_image', 'user_map'):
        os.chmod(store / manifest[field_name], 0o600)
    os.chmod(store / 'active.json', 0o600)
    return store, manifest


def _projection_grid(
    cells: np.ndarray | None = None,
    *,
    resolution: float = 0.1,
    yaw: float = 0.0,
) -> MapGrid:
    if cells is None:
        mutable = np.zeros((40, 50), dtype=np.int16)
    else:
        mutable = np.asarray(cells, dtype=np.int16).copy()
    mutable.setflags(write=False)
    height, width = mutable.shape
    return MapGrid(
        width,
        height,
        resolution,
        -2.5,
        -2.0,
        yaw,
        mutable,
    )


def _resolver(store: Path) -> ActiveMapEvidenceResolver:
    return ActiveMapEvidenceResolver(ActiveMapResolverConfig(str(store)))


def _clearance_at(
    projection: ActiveMapStaticNavigationProjection,
    row: int,
    column: int,
) -> float:
    grid = projection.static_clearance_grid
    width = object.__getattribute__(grid, '_width')
    values = object.__getattribute__(grid, '_clearances_m')
    return values[row * width + column]


def _rewrite_json(path: Path, update) -> None:
    value = json.loads(path.read_text(encoding='utf-8'))
    update(value)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    os.chmod(path, 0o600)


def _rewrite_yaml(path: Path, update) -> None:
    value = yaml.safe_load(path.read_text(encoding='utf-8'))
    update(value)
    path.write_text(
        yaml.safe_dump(value, sort_keys=False),
        encoding='utf-8',
    )
    os.chmod(path, 0o600)


def _rewrite_projection_sources(
    store: Path,
    manifest: dict,
    *,
    update_pixels=None,
    update_metadata=None,
) -> dict:
    """Rewrite one test revision while preserving all identity bindings."""
    old_revision = manifest['revision']
    revision_dir = store / 'versions' / old_revision
    yaml_path = revision_dir / 'map.yaml'
    image_path = revision_dir / 'map.pgm'
    user_map_path = revision_dir / 'user-map.geojson'

    metadata = yaml.safe_load(yaml_path.read_text(encoding='utf-8'))
    if update_metadata is not None:
        update_metadata(metadata)
    yaml_path.write_text(
        yaml.safe_dump(metadata, sort_keys=False),
        encoding='utf-8',
    )
    os.chmod(yaml_path, 0o600)

    image_bytes = image_path.read_bytes()
    width, height, pixels = active_map_module._parse_pgm(image_bytes)
    image = np.frombuffer(pixels, dtype=np.uint8).reshape(
        height, width
    ).copy()
    if update_pixels is not None:
        update_pixels(image)
    header_size = len(image_bytes) - width * height
    image_bytes = image_bytes[:header_size] + image.tobytes()
    image_path.write_bytes(image_bytes)
    os.chmod(image_path, 0o600)

    parsed_metadata = active_map_module._parse_map_yaml(
        yaml_path.read_bytes()
    )
    map_id, map_revision = active_map_module._identities(
        width,
        height,
        image.tobytes(),
        parsed_metadata,
    )
    user_map = json.loads(user_map_path.read_text(encoding='utf-8'))
    user_map['map_id'] = map_id
    user_map['map_revision'] = map_revision
    source = user_map['source']
    source['resolution'] = parsed_metadata['resolution']
    source['occupied_thresh'] = parsed_metadata['occupied_thresh']
    source['free_thresh'] = parsed_metadata['free_thresh']
    user_map_path.write_text(
        json.dumps(user_map, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    os.chmod(user_map_path, 0o600)

    revision_digest = hashlib.sha256(
        image_bytes + map_revision.encode('ascii')
    ).hexdigest()[:10]
    new_revision = f'20260101T000000Z-{revision_digest}'
    new_revision_dir = store / 'versions' / new_revision
    revision_dir.rename(new_revision_dir)
    os.chmod(new_revision_dir, 0o700)

    updated = dict(manifest)
    updated['revision'] = new_revision
    updated['map_id'] = map_id
    updated['map_revision'] = map_revision
    for field_name, name in (
        ('map_yaml', 'map.yaml'),
        ('map_image', 'map.pgm'),
        ('user_map', 'user-map.geojson'),
    ):
        updated[field_name] = f'versions/{new_revision}/{name}'
    (store / 'active.json').write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    os.chmod(store / 'active.json', 0o600)
    return updated


def test_active_map_evidence_is_deterministic_and_bound_to_raw_bytes(tmp_path):
    """Repeated reads of one active revision must produce one exact proof."""
    store, manifest = _fixture(tmp_path)
    first = _resolver(store).resolve()
    second = _resolver(store).resolve()

    assert first.map_id == manifest['map_id']
    assert first.map_revision == manifest['map_revision']
    assert first.frame_id == 'map'
    assert first.active_manifest_revision == manifest['revision']
    assert first.manifest_revision == manifest['revision']
    assert (first.width, first.height) == (50, 40)
    assert first.resolution == pytest.approx(0.1)
    assert (first.origin_x, first.origin_y, first.origin_yaw) == pytest.approx(
        (-2.5, -2.0, 0.25)
    )
    assert first.manifest_sha256 == hashlib.sha256(
        (store / 'active.json').read_bytes()
    ).hexdigest()
    assert first.map_yaml_sha256 == hashlib.sha256(
        (store / manifest['map_yaml']).read_bytes()
    ).hexdigest()
    assert first.map_image_sha256 == hashlib.sha256(
        (store / manifest['map_image']).read_bytes()
    ).hexdigest()
    assert first.user_map_sha256 == hashlib.sha256(
        (store / manifest['user_map']).read_bytes()
    ).hexdigest()
    assert first.evidence_digest == second.evidence_digest
    assert first.canonical_copy().evidence_digest == first.evidence_digest


def test_combined_digest_commits_every_public_identity_field(tmp_path):
    """The aggregate digest commits hashes, frame, revision, and geometry."""
    store, _manifest = _fixture(tmp_path)
    evidence = _resolver(store).resolve()
    payload = {
        'schema_version': evidence.schema_version,
        'map_id': evidence.map_id,
        'map_revision': evidence.map_revision,
        'frame_id': evidence.frame_id,
        'active_manifest_revision': evidence.active_manifest_revision,
        'manifest_sha256': evidence.manifest_sha256,
        'map_yaml_sha256': evidence.map_yaml_sha256,
        'map_image_sha256': evidence.map_image_sha256,
        'user_map_sha256': evidence.user_map_sha256,
        'width': evidence.width,
        'height': evidence.height,
        'resolution': evidence.resolution,
        'origin': [
            evidence.origin_x,
            evidence.origin_y,
            evidence.origin_yaw,
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    assert evidence.evidence_digest == hashlib.sha256(encoded).hexdigest()


def test_resolver_has_one_fixed_absolute_store_and_redacts_it(tmp_path):
    """A request cannot redirect the resolver to an LLM-selected path."""
    store, _manifest = _fixture(tmp_path)
    config = ActiveMapResolverConfig(str(store))
    resolver = ActiveMapEvidenceResolver(config)

    assert str(store) not in repr(config)
    with pytest.raises(TypeError):
        resolver.resolve(tmp_path / 'other')
    with pytest.raises(ValueError, match='absolute'):
        ActiveMapResolverConfig('relative/store')
    with pytest.raises(ValueError, match='absolute'):
        ActiveMapResolverConfig(str(store) + '/../protected-map-store')


def test_config_and_evidence_reject_subclasses_and_public_construction(
    tmp_path,
):
    """Subclass tricks and caller-made evidence must not gain provenance."""
    store, _manifest = _fixture(tmp_path)

    class StringSubclass(str):
        pass

    with pytest.raises(ValueError):
        ActiveMapResolverConfig(StringSubclass(str(store)))
    with pytest.raises(TypeError):
        ActiveMapEvidenceResolver(type(
            'ConfigSubclass',
            (ActiveMapResolverConfig,),
            {},
        )(str(store)))
    with pytest.raises(TypeError, match='resolver'):
        ActiveMapEvidence(
            map_id='map-000000000000',
            map_revision='rev-000000000000',
            frame_id='map',
            active_manifest_revision='20260101T000000Z-0000000000',
            manifest_sha256='0' * 64,
            map_yaml_sha256='0' * 64,
            map_image_sha256='0' * 64,
            user_map_sha256='0' * 64,
            evidence_digest='0' * 64,
            width=1,
            height=1,
            resolution=1.0,
            origin_x=0.0,
            origin_y=0.0,
            origin_yaw=0.0,
            _manifest_bytes=b'x',
            _map_yaml_bytes=b'x',
            _map_image_bytes=b'x',
            _user_map_bytes=b'x',
        )


@pytest.mark.parametrize(
    ('field_name', 'replacement'),
    (
        ('map_id', 'map-000000000000'),
        ('frame_id', 'odom'),
        ('width', 51),
        ('resolution', 0.2),
        ('manifest_sha256', '0' * 64),
    ),
)
def test_canonical_copy_detects_public_current_value_mutation(
    tmp_path,
    field_name,
    replacement,
):
    """Even object-level bypasses must invalidate the issued evidence."""
    store, _manifest = _fixture(tmp_path)
    evidence = _resolver(store).resolve()
    object.__setattr__(evidence, field_name, replacement)

    with pytest.raises(ActiveMapEvidenceInvalidError):
        evidence.canonical_copy()


def test_canonical_copy_detects_subclass_and_private_snapshot_mutation(
    tmp_path,
):
    """Private bytes and exact scalar types remain bound to issuance."""
    store, _manifest = _fixture(tmp_path)
    evidence = _resolver(store).resolve()

    class StringSubclass(str):
        pass

    object.__setattr__(evidence, 'map_id', StringSubclass(evidence.map_id))
    with pytest.raises(ActiveMapEvidenceInvalidError):
        evidence.canonical_copy()

    evidence = _resolver(store).resolve()
    object.__setattr__(
        evidence,
        '_manifest_bytes',
        evidence._manifest_bytes + b' ',
    )
    with pytest.raises(ActiveMapEvidenceInvalidError):
        evidence.canonical_copy()


def test_evidence_is_frozen_redacted_and_does_not_expose_paths(tmp_path):
    """Ordinary mutation and logging must reveal neither map nor file data."""
    store, _manifest = _fixture(tmp_path)
    evidence = _resolver(store).resolve()

    with pytest.raises(FrozenInstanceError):
        evidence.map_id = 'map-000000000000'
    rendered = repr(evidence)
    assert rendered == 'ActiveMapEvidence(<redacted>)'
    assert str(store) not in rendered
    assert evidence.map_id not in rendered
    assert 'FeatureCollection' not in rendered
    assert 'map.pgm' not in rendered


def test_canonical_copy_uses_no_filesystem_after_issuance(
    tmp_path,
    monkeypatch,
):
    """An issued snapshot stays self-contained after descriptors close."""
    store, _manifest = _fixture(tmp_path)
    evidence = _resolver(store).resolve()

    def forbidden(*_args, **_kwargs):
        raise AssertionError('unexpected filesystem access')

    monkeypatch.setattr(active_map_module.os, 'open', forbidden)
    monkeypatch.setattr(active_map_module.os, 'read', forbidden)
    monkeypatch.setattr(active_map_module.os, 'stat', forbidden)
    monkeypatch.setattr(active_map_module.os, 'fstat', forbidden)
    copy = evidence.canonical_copy()
    assert copy.evidence_digest == evidence.evidence_digest


@pytest.mark.parametrize(
    ('field_name', 'value'),
    (
        ('format', 'other-format'),
        ('map_id', 'map-000000000000'),
        ('map_revision', 'rev-000000000000'),
        ('revision', 7),
        ('map_yaml', '../../private/map.yaml'),
        ('map_image', '/private/map.pgm'),
        ('user_map', 'versions/../user-map.geojson'),
    ),
)
def test_manifest_changes_types_and_traversal_fail_closed(
    tmp_path,
    field_name,
    value,
):
    """Only the exact manifest and contained persisted paths are used."""
    store, _manifest = _fixture(tmp_path)
    _rewrite_json(
        store / 'active.json',
        lambda manifest: manifest.__setitem__(field_name, value),
    )

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


@pytest.mark.parametrize(
    'replacement',
    (
        b'{not-json',
        b'[]\n',
        b'{"format":"malbut-map-store/v1",'
        b'"format":"malbut-map-store/v1"}',
    ),
)
def test_malformed_manifest_json_is_rejected(tmp_path, replacement):
    """Syntax, shape, and duplicate-key ambiguity are invalid."""
    store, _manifest = _fixture(tmp_path)
    (store / 'active.json').write_bytes(replacement)
    os.chmod(store / 'active.json', 0o600)

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


@pytest.mark.parametrize(
    ('field_name', 'value'),
    (
        ('image', '../map.pgm'),
        ('mode', 'scale'),
        ('resolution', True),
        ('resolution', float('nan')),
        ('origin', [0.0, 0.0]),
        ('origin', [0.0, 0.0, float('inf')]),
        ('negate', True),
        ('free_thresh', 0.9),
    ),
)
def test_yaml_reference_types_bounds_and_mode_are_strict(
    tmp_path,
    field_name,
    value,
):
    """The map YAML cannot redirect or alter the persisted interpretation."""
    store, manifest = _fixture(tmp_path)
    path = store / manifest['map_yaml']
    _rewrite_yaml(
        path,
        lambda metadata: metadata.__setitem__(field_name, value),
    )

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


@pytest.mark.parametrize(
    'replacement',
    (
        b'image: [\n',
        b'image: map.pgm\nimage: map.pgm\n',
        b'!!python/object:os.system {}\n',
    ),
)
def test_malformed_or_ambiguous_yaml_is_rejected(tmp_path, replacement):
    """Unsafe tags, duplicate keys, and syntax errors fail parsing."""
    store, manifest = _fixture(tmp_path)
    path = store / manifest['map_yaml']
    path.write_bytes(replacement)
    os.chmod(path, 0o600)

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


@pytest.mark.parametrize(
    'mutator',
    (
        lambda value: b'P2' + value[2:],
        lambda value: value.replace(b'\n255\n', b'\n254\n', 1),
        lambda value: value[:-1],
        lambda value: value + b'\x00',
        lambda value: value.replace(b'50 40\n', b'050 40\n', 1),
    ),
)
def test_only_exact_persisted_p5_image_format_is_accepted(tmp_path, mutator):
    """Alternate encodings and inconsistent PGM payloads fail closed."""
    store, manifest = _fixture(tmp_path)
    path = store / manifest['map_image']
    path.write_bytes(mutator(path.read_bytes()))
    os.chmod(path, 0o600)

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


@pytest.mark.parametrize(
    ('field_name', 'value'),
    (
        ('type', 'Feature'),
        ('format', 'other-format'),
        ('map_id', 'map-000000000000'),
        ('map_revision', 'rev-000000000000'),
        ('frame_id', 'odom'),
        ('features', {}),
    ),
)
def test_user_map_identity_frame_and_geojson_shape_are_bound(
    tmp_path,
    field_name,
    value,
):
    """Manifest identity must agree with a map-frame GeoJSON collection."""
    store, manifest = _fixture(tmp_path)
    path = store / manifest['user_map']
    _rewrite_json(
        path,
        lambda user_map: user_map.__setitem__(field_name, value),
    )

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


@pytest.mark.parametrize(
    'replacement',
    (
        b'{broken',
        b'[]\n',
        b'{"type":"FeatureCollection","type":"FeatureCollection"}',
    ),
)
def test_malformed_or_ambiguous_user_map_json_is_rejected(
    tmp_path,
    replacement,
):
    """Malformed semantic content cannot become a private evidence snapshot."""
    store, manifest = _fixture(tmp_path)
    path = store / manifest['user_map']
    path.write_bytes(replacement)
    os.chmod(path, 0o600)

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


def test_invalid_geojson_geometry_is_rejected(tmp_path):
    """A syntactically valid but open polygon ring is not trusted GeoJSON."""
    store, manifest = _fixture(tmp_path)
    path = store / manifest['user_map']

    def open_first_ring(user_map):
        for feature in user_map['features']:
            geometry = feature['geometry']
            if geometry['type'] == 'Polygon':
                geometry['coordinates'][0].pop()
                return

    _rewrite_json(path, open_first_ring)
    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


def test_user_map_source_metadata_must_match_image_and_yaml(tmp_path):
    """Vector metadata cannot claim dimensions from another occupancy map."""
    store, manifest = _fixture(tmp_path)
    path = store / manifest['user_map']
    _rewrite_json(
        path,
        lambda value: value['source'].__setitem__('width', 51),
    )

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


def test_changed_image_pixels_recompute_map_identity(tmp_path):
    """Raw pixel changes cannot retain the old manifest and User Map IDs."""
    store, manifest = _fixture(tmp_path)
    path = store / manifest['map_image']
    value = bytearray(path.read_bytes())
    value[-1] ^= 0xff
    path.write_bytes(value)
    os.chmod(path, 0o600)

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


@pytest.mark.parametrize('target_field', ('active', 'yaml', 'image', 'user'))
def test_symlink_sources_are_rejected(tmp_path, target_field):
    """Neither the manifest nor any referenced source may be a symlink."""
    store, manifest = _fixture(tmp_path)
    paths = {
        'active': store / 'active.json',
        'yaml': store / manifest['map_yaml'],
        'image': store / manifest['map_image'],
        'user': store / manifest['user_map'],
    }
    target = paths[target_field]
    replacement = tmp_path / f'{target_field}-replacement'
    replacement.write_bytes(target.read_bytes())
    os.chmod(replacement, 0o600)
    target.unlink()
    target.symlink_to(replacement)

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


def test_symlink_in_fixed_store_path_is_rejected(tmp_path):
    """O_NOFOLLOW applies to each component of the configured path."""
    store, _manifest = _fixture(tmp_path)
    alias = tmp_path / 'store-alias'
    alias.symlink_to(store, target_is_directory=True)

    with pytest.raises(ActiveMapValidationError):
        _resolver(alias).resolve()


@pytest.mark.parametrize('target_field', ('active', 'yaml', 'image', 'user'))
def test_hardlinked_sources_are_rejected(tmp_path, target_field):
    """A second pathname invalidates a snapped file's ownership proof."""
    store, manifest = _fixture(tmp_path)
    paths = {
        'active': store / 'active.json',
        'yaml': store / manifest['map_yaml'],
        'image': store / manifest['map_image'],
        'user': store / manifest['user_map'],
    }
    os.link(paths[target_field], tmp_path / f'{target_field}-alias')

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


def test_fifo_is_rejected_without_blocking(tmp_path):
    """A named pipe cannot substitute for a bounded regular-file snapshot."""
    store, manifest = _fixture(tmp_path)
    path = store / manifest['user_map']
    path.unlink()
    os.mkfifo(path, mode=0o600)

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


@pytest.mark.parametrize(
    'target_field',
    ('store', 'revision', 'active', 'yaml'),
)
def test_group_or_world_writable_sources_are_rejected(tmp_path, target_field):
    """The complete path beneath the store must remain owner-controlled."""
    store, manifest = _fixture(tmp_path)
    paths = {
        'store': store,
        'revision': store / 'versions' / manifest['revision'],
        'active': store / 'active.json',
        'yaml': store / manifest['map_yaml'],
    }
    mode = 0o720 if paths[target_field].is_dir() else 0o620
    os.chmod(paths[target_field], mode)

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


def test_owner_mismatch_is_rejected(tmp_path):
    """The configured owner is checked independently of process identity."""
    store, _manifest = _fixture(tmp_path)
    resolver = ActiveMapEvidenceResolver(ActiveMapResolverConfig(
        str(store),
        owner_uid=os.geteuid() + 1,
    ))

    with pytest.raises(ActiveMapValidationError):
        resolver.resolve()


def test_oversized_manifest_is_rejected_before_json_parsing(tmp_path):
    """Every source has a hard read bound before content parsing begins."""
    store, _manifest = _fixture(tmp_path)
    path = store / 'active.json'
    path.write_bytes(b'{' + b' ' * MAX_ACTIVE_MANIFEST_BYTES + b'}')
    os.chmod(path, 0o600)

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve()


def test_in_place_read_race_is_reported_as_changed(tmp_path, monkeypatch):
    """Before/after fstat prevents accepting bytes from a changing file."""
    store, manifest = _fixture(tmp_path)
    yaml_path = store / manifest['map_yaml']
    original_read = active_map_module.os.read
    changed = False

    def racing_read(descriptor, size):
        nonlocal changed
        value = original_read(descriptor, size)
        if not changed and value.startswith(b'image: map.pgm'):
            changed = True
            yaml_path.write_bytes(yaml_path.read_bytes() + b'\n')
            os.chmod(yaml_path, 0o600)
        return value

    monkeypatch.setattr(active_map_module.os, 'read', racing_read)
    with pytest.raises(ActiveMapChangedError):
        _resolver(store).resolve()
    assert changed is True


def test_atomic_manifest_swap_during_read_is_reported_as_changed(
    tmp_path,
    monkeypatch,
):
    """An atomic active.json replacement cannot produce stale-current proof."""
    store, _manifest = _fixture(tmp_path)
    manifest_path = store / 'active.json'
    original_read = active_map_module.os.read
    changed = False

    def racing_read(descriptor, size):
        nonlocal changed
        value = original_read(descriptor, size)
        if not changed and value.startswith(b'{'):
            changed = True
            replacement = store / '.active-replacement'
            replacement.write_bytes(manifest_path.read_bytes())
            os.chmod(replacement, 0o600)
            os.replace(replacement, manifest_path)
        return value

    monkeypatch.setattr(active_map_module.os, 'read', racing_read)
    with pytest.raises(ActiveMapChangedError):
        _resolver(store).resolve()
    assert changed is True


def test_public_errors_are_content_free_and_chain_free(tmp_path):
    """Private paths and source contents never escape through typed errors."""
    store, manifest = _fixture(tmp_path)
    secret = 'PRIVATE-MAP-CONTENT-DO-NOT-LOG'
    path = store / manifest['map_yaml']
    path.write_text(secret, encoding='utf-8')
    os.chmod(path, 0o600)

    with pytest.raises(ActiveMapValidationError) as captured:
        _resolver(store).resolve()
    error = captured.value
    rendered = repr(error)
    assert error.code == 'active_map_invalid'
    assert secret not in rendered
    assert str(store) not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_static_projection_is_deterministic_redacted_and_evidence_bound(
    tmp_path,
):
    """One snapshot binds canonical map evidence to static clearance only."""
    store, _manifest = _fixture(tmp_path, _projection_grid())
    first = _resolver(store).resolve_static_navigation_projection()
    second = _resolver(store).resolve_static_navigation_projection()

    assert type(first) is ActiveMapStaticNavigationProjection
    assert type(first.active_map_evidence) is ActiveMapEvidence
    assert type(first.static_clearance_grid) is StaticClearanceGrid
    assert first.active_map_evidence.evidence_digest == (
        _resolver(store).resolve().evidence_digest
    )
    assert first.static_clearance_grid.digest == (
        second.static_clearance_grid.digest
    )
    assert first.projection_digest == second.projection_digest
    payload = {
        'schema_version': first.schema_version,
        'scope': first.scope,
        'active_map_evidence_digest': (
            first.active_map_evidence.evidence_digest
        ),
        'static_clearance_digest': first.static_clearance_grid.digest,
        'occupancy_semantics': 'ros-map-server-trinary-v1',
        'unknown_is_obstacle': True,
        'off_map_is_obstacle': True,
        'clearance_metric': 'euclidean-cell-center-v1',
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    assert first.projection_digest == hashlib.sha256(encoded).hexdigest()
    assert repr(first) == (
        'ActiveMapStaticNavigationProjection(<redacted>)'
    )
    assert first.active_map_evidence.map_id not in repr(first)
    assert not hasattr(first, 'restricted_zones')
    assert not hasattr(first, 'cost_grid')


def test_static_projection_is_frozen_and_resolver_issued(tmp_path):
    """Callers cannot construct or ordinarily mutate a trusted bundle."""
    store, _manifest = _fixture(tmp_path, _projection_grid())
    projection = _resolver(store).resolve_static_navigation_projection()

    with pytest.raises(FrozenInstanceError):
        projection.projection_digest = '0' * 64
    with pytest.raises(TypeError, match='resolver'):
        ActiveMapStaticNavigationProjection(
            active_map_evidence=projection.active_map_evidence,
            static_clearance_grid=projection.static_clearance_grid,
            projection_digest=projection.projection_digest,
        )


def test_static_projection_canonical_copy_detects_nested_mutation(tmp_path):
    """A bypassed clearance mutation cannot retain the issued binding."""
    store, _manifest = _fixture(tmp_path, _projection_grid())
    projection = _resolver(store).resolve_static_navigation_projection()
    canonical = projection.canonical_copy()
    assert canonical.projection_digest == projection.projection_digest

    grid = projection.static_clearance_grid
    values = object.__getattribute__(grid, '_clearances_m')
    object.__setattr__(grid, '_clearances_m', (999.0,) + values[1:])
    with pytest.raises(ActiveMapProjectionInvalidError) as captured:
        projection.canonical_copy()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_projection_restores_ros_bottom_row_and_blocks_unknown(tmp_path):
    """PGM top rows flip back to ROS rows and unknown is an obstacle."""
    cells = np.zeros((40, 50), dtype=np.int16)
    cells[7, 12] = 100
    cells[13, 31] = -1
    store, _manifest = _fixture(
        tmp_path,
        _projection_grid(cells),
    )
    projection = _resolver(store).resolve_static_navigation_projection()

    assert _clearance_at(projection, 7, 12) == 0.0
    assert _clearance_at(projection, 13, 31) == 0.0
    assert _clearance_at(projection, 32, 12) > 0.0
    assert _clearance_at(projection, 26, 31) > 0.0


def test_projection_uses_exact_euclidean_cell_clearance(tmp_path):
    """Axial and diagonal distance use exact cell-center Euclidean metres."""
    cells = np.zeros((40, 50), dtype=np.int16)
    cells[20, 25] = 100
    store, _manifest = _fixture(
        tmp_path,
        _projection_grid(cells, resolution=0.1),
    )
    projection = _resolver(store).resolve_static_navigation_projection()

    assert _clearance_at(projection, 20, 28) == pytest.approx(0.3)
    assert _clearance_at(projection, 23, 29) == pytest.approx(0.5)
    assert _clearance_at(projection, 0, 25) == pytest.approx(0.1)


def test_projection_applies_strict_default_trinary_thresholds(tmp_path):
    """Occupied and threshold-band pixels block while strict free passes."""
    store, manifest = _fixture(tmp_path, _projection_grid())
    values = (0, 89, 90, 205, 206, 255)

    def update_pixels(image):
        image[20, 20:26] = values

    _rewrite_projection_sources(
        store,
        manifest,
        update_pixels=update_pixels,
    )
    projection = _resolver(store).resolve_static_navigation_projection()
    ros_row = 40 - 1 - 20

    assert tuple(
        _clearance_at(projection, ros_row, column)
        for column in range(20, 24)
    ) == (0.0, 0.0, 0.0, 0.0)
    assert _clearance_at(projection, ros_row, 24) > 0.0
    assert _clearance_at(projection, ros_row, 25) > 0.0


def test_projection_applies_negate_and_strict_equal_thresholds(tmp_path):
    """Negated equality stays unknown; only values strictly below are free."""
    store, manifest = _fixture(tmp_path, _projection_grid())
    values = (0, 49, 50, 165, 166, 167, 255)

    def update_metadata(metadata):
        metadata['negate'] = 1
        metadata['free_thresh'] = 50.0 / 255.0
        metadata['occupied_thresh'] = 166.0 / 255.0

    def update_pixels(image):
        image[20, 20:27] = values

    _rewrite_projection_sources(
        store,
        manifest,
        update_pixels=update_pixels,
        update_metadata=update_metadata,
    )
    projection = _resolver(store).resolve_static_navigation_projection()
    ros_row = 40 - 1 - 20

    assert _clearance_at(projection, ros_row, 20) > 0.0
    assert _clearance_at(projection, ros_row, 21) > 0.0
    assert tuple(
        _clearance_at(projection, ros_row, column)
        for column in range(22, 27)
    ) == (0.0, 0.0, 0.0, 0.0, 0.0)


def test_projection_requires_zero_yaw_without_changing_resolve(tmp_path):
    """Evidence remains backward compatible while aligned projection fails."""
    store, _manifest = _fixture(
        tmp_path,
        _projection_grid(yaw=0.25),
    )
    resolver = _resolver(store)

    assert resolver.resolve().origin_yaw == pytest.approx(0.25)
    with pytest.raises(ActiveMapValidationError):
        resolver.resolve_static_navigation_projection()


def test_projection_has_an_explicit_bounded_cell_budget(
    tmp_path,
    monkeypatch,
):
    """Projection refuses work above its fixed safety-grid cell budget."""
    store, _manifest = _fixture(tmp_path, _projection_grid())
    monkeypatch.setattr(
        active_map_module,
        'MAX_STATIC_PROJECTION_CELLS',
        100,
    )

    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve_static_navigation_projection()


def test_projection_revalidates_sources_after_distance_transform(
    tmp_path,
    monkeypatch,
):
    """A source mutation during projection invalidates the whole snapshot."""
    store, manifest = _fixture(tmp_path, _projection_grid())
    image_path = store / manifest['map_image']
    original_transform = active_map_module.cv2.distanceTransform
    changed = False

    def racing_transform(*args, **kwargs):
        nonlocal changed
        result = original_transform(*args, **kwargs)
        value = bytearray(image_path.read_bytes())
        value[-1] ^= 0xff
        image_path.write_bytes(value)
        os.chmod(image_path, 0o600)
        changed = True
        return result

    monkeypatch.setattr(
        active_map_module.cv2,
        'distanceTransform',
        racing_transform,
    )
    with pytest.raises(ActiveMapChangedError):
        _resolver(store).resolve_static_navigation_projection()
    assert changed is True


@pytest.mark.parametrize('invalid_output', ('nan', 'negative', 'dtype'))
def test_projection_rejects_invalid_distance_transform_output(
    tmp_path,
    monkeypatch,
    invalid_output,
):
    """Nonfinite, negative, or wrong-typed clearance cannot be evidence."""
    store, _manifest = _fixture(tmp_path, _projection_grid())

    def invalid_transform(image, *_args, **_kwargs):
        if invalid_output == 'nan':
            return np.full(image.shape, np.nan, dtype=np.float32)
        if invalid_output == 'negative':
            return np.full(image.shape, -1.0, dtype=np.float32)
        return np.zeros(image.shape, dtype=np.float64)

    monkeypatch.setattr(
        active_map_module.cv2,
        'distanceTransform',
        invalid_transform,
    )
    with pytest.raises(ActiveMapValidationError):
        _resolver(store).resolve_static_navigation_projection()


def test_projection_hides_opencv_exception_content_and_chain(
    tmp_path,
    monkeypatch,
):
    """Raw image-library failures cross the boundary as fixed errors only."""
    store, _manifest = _fixture(tmp_path, _projection_grid())
    secret = 'PRIVATE-OPENCV-MAP-PATH'

    def failing_transform(*_args, **_kwargs):
        raise active_map_module.cv2.error(secret)

    monkeypatch.setattr(
        active_map_module.cv2,
        'distanceTransform',
        failing_transform,
    )
    with pytest.raises(ActiveMapValidationError) as captured:
        _resolver(store).resolve_static_navigation_projection()
    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_projection_opens_the_protected_store_once(tmp_path, monkeypatch):
    """Evidence and clearance share one root-open and revalidation cycle."""
    store, _manifest = _fixture(tmp_path, _projection_grid())
    original_open = active_map_module._open_root_directory
    calls = 0

    def counting_open(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr(
        active_map_module,
        '_open_root_directory',
        counting_open,
    )
    projection = _resolver(store).resolve_static_navigation_projection()

    assert type(projection) is ActiveMapStaticNavigationProjection
    assert calls == 1
