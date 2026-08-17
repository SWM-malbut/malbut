"""Protected local faster-whisper model manifest verification."""

import hashlib
import importlib.metadata
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from malbut_voice.config import (
    ModelBinding,
    SHA256_PATTERN,
    read_protected_file,
)
from malbut_voice.errors import ModelSecurityError, chain_free_boundary
from malbut_voice.provenance import ModelAttestation


MODEL_MANIFEST_SCHEMA_VERSION = 1
REQUIRED_MODEL_FILES = frozenset(
    {'config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt'}
)
REQUIRED_RUNTIME_PACKAGES = frozenset(
    {
        'av',
        'ctranslate2',
        'faster-whisper',
        'numpy',
        'onnxruntime',
        'tokenizers',
    }
)
MAX_MANIFEST_BYTES = 32768
REVISION_PATTERN = re.compile(r'^[0-9a-f]{40}$')
MODEL_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')


def _fail(code):
    raise ModelSecurityError(code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('model_manifest_duplicate_field')
        result[key] = value
    return result


def _exact_fields(value, fields, code):
    if not isinstance(value, dict) or set(value) != set(fields):
        _fail(code)
    return value


def _safe_component(value, code, maximum=128):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or '/' in value
        or value in {'.', '..'}
        or any(ord(character) < 32 for character in value)
    ):
        _fail(code)
    return value


def _protected_directory(path):
    try:
        metadata = os.lstat(path)
    except OSError:
        raise ModelSecurityError('model_root_unavailable')
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail('model_root_not_directory')
    if metadata.st_uid not in {0, os.geteuid()}:
        _fail('model_root_owner')
    if metadata.st_mode & 0o022:
        _fail('model_root_writable')


def _contained(root, candidate):
    try:
        candidate.relative_to(root)
    except ValueError:
        _fail('model_path_escape')


def _protected_root_tree(root):
    current = root
    while True:
        try:
            metadata = os.lstat(current)
        except OSError:
            raise ModelSecurityError('model_root_unavailable')
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            _fail('model_root_not_directory')
        if metadata.st_uid not in {0, os.geteuid()}:
            _fail('model_root_owner')
        writable = metadata.st_mode & 0o022
        sticky_root = (
            bool(metadata.st_mode & stat.S_ISVTX)
            and metadata.st_uid == 0
        )
        if writable and not sticky_root:
            _fail('model_root_writable')
        if current.parent == current:
            break
        current = current.parent


def _protected_descendant_tree(root, directory):
    _contained(root, directory)
    try:
        relative = directory.relative_to(root)
    except ValueError:
        _fail('model_path_escape')
    current = root
    fence = []
    components = [root]
    for part in relative.parts:
        current = current / part
        components.append(current)
    for component in components:
        try:
            metadata = os.lstat(component)
        except OSError:
            raise ModelSecurityError('model_root_unavailable')
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            _fail('model_root_not_directory')
        if metadata.st_uid not in {0, os.geteuid()}:
            _fail('model_root_owner')
        if metadata.st_mode & 0o022:
            _fail('model_root_writable')
        fence.append((component, metadata.st_dev, metadata.st_ino))
    return tuple(fence)


def _verify_directory_fence(fence):
    for component, expected_device, expected_inode in fence:
        try:
            metadata = os.lstat(component)
        except OSError:
            _fail('model_directory_changed')
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != expected_device
            or metadata.st_ino != expected_inode
            or metadata.st_mode & 0o022
        ):
            _fail('model_directory_changed')


def _hash_model_file(root, snapshot, relative_path, expected_sha256):
    declared = snapshot / relative_path
    try:
        resolved = declared.resolve(strict=True)
    except OSError:
        raise ModelSecurityError('model_file_unavailable')
    _contained(root, resolved)
    directory_fence = _protected_descendant_tree(root, resolved.parent)
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags)
    except OSError:
        raise ModelSecurityError('model_file_open_failed')
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail('model_file_not_regular')
        if metadata.st_uid not in {0, os.geteuid()}:
            _fail('model_file_owner')
        if metadata.st_mode & 0o022:
            _fail('model_file_writable')
        if metadata.st_nlink != 1:
            _fail('model_file_link_count')
        digest = hashlib.sha256()
        while True:
            try:
                chunk = os.read(descriptor, 1024 * 1024)
            except OSError:
                raise ModelSecurityError('model_file_read_failed')
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    try:
        after = os.lstat(resolved)
        declared_after = declared.resolve(strict=True)
    except OSError:
        raise ModelSecurityError('model_file_unavailable')
    if (
        stat.S_ISLNK(after.st_mode)
        or after.st_dev != metadata.st_dev
        or after.st_ino != metadata.st_ino
        or after.st_size != metadata.st_size
        or declared_after != resolved
    ):
        _fail('model_file_identity_changed')
    _verify_directory_fence(directory_fence)
    if digest.hexdigest() != expected_sha256:
        _fail('model_file_hash_mismatch')


@dataclass(frozen=True)
class VerifiedModel:
    """Verified absolute snapshot and content-free model attestation."""

    snapshot_path: Path
    runtime_versions: dict
    attestation: ModelAttestation


@chain_free_boundary
def verify_model_binding(binding, *, version_lookup=None):
    """Verify containment, file hashes, and exact runtime package pins."""
    if not isinstance(binding, ModelBinding):
        _fail('model_binding_type')
    root = binding.root.resolve(strict=False)
    if root != binding.root:
        _fail('model_root_must_be_canonical')
    _protected_root_tree(root)
    try:
        payload = read_protected_file(
            binding.manifest,
            maximum_bytes=MAX_MANIFEST_BYTES,
        )
        raw = json.loads(
            payload.decode('utf-8'),
            object_pairs_hook=_pairs,
        )
    except ModelSecurityError:
        raise
    except Exception:
        raise ModelSecurityError('model_manifest_invalid')
    body = _exact_fields(
        raw,
        {
            'schema_version', 'model_id', 'snapshot_revision',
            'snapshot_path', 'runtime_versions', 'files',
        },
        'model_manifest_fields',
    )
    if (
        type(body['schema_version']) is not int
        or body['schema_version'] != MODEL_MANIFEST_SCHEMA_VERSION
    ):
        _fail('model_manifest_schema')
    model_id = _safe_component(body['model_id'], 'model_id')
    if MODEL_ID_PATTERN.fullmatch(model_id) is None:
        _fail('model_id')
    revision = _safe_component(
        body['snapshot_revision'], 'model_snapshot_revision', 128
    )
    if REVISION_PATTERN.fullmatch(revision) is None:
        _fail('model_snapshot_revision')
    snapshot_text = body['snapshot_path']
    if not isinstance(snapshot_text, str) or not snapshot_text:
        _fail('model_snapshot_path')
    snapshot_relative = Path(snapshot_text)
    if (
        snapshot_relative.is_absolute()
        or snapshot_relative != Path('snapshots') / revision
    ):
        _fail('model_snapshot_path')
    declared_snapshot = root / snapshot_relative
    _protected_descendant_tree(root, declared_snapshot)
    try:
        snapshot = declared_snapshot.resolve(strict=True)
    except OSError:
        raise ModelSecurityError('model_snapshot_path')
    _contained(root, snapshot)
    _protected_directory(snapshot)
    try:
        snapshot_entries = {entry.name for entry in snapshot.iterdir()}
    except OSError:
        raise ModelSecurityError('model_snapshot_path')
    if snapshot_entries != REQUIRED_MODEL_FILES:
        _fail('model_snapshot_file_set')
    versions = _exact_fields(
        body['runtime_versions'],
        REQUIRED_RUNTIME_PACKAGES,
        'model_runtime_fields',
    )
    for package, version in versions.items():
        _safe_component(package, 'model_runtime_package')
        _safe_component(version, 'model_runtime_version')
    files = body['files']
    if not isinstance(files, list) or len(files) != len(REQUIRED_MODEL_FILES):
        _fail('model_file_manifest')
    declared = {}
    for entry in files:
        item = _exact_fields(
            entry,
            {'path', 'sha256'},
            'model_file_entry',
        )
        relative = _safe_component(item['path'], 'model_file_path')
        digest = item['sha256']
        if (
            not isinstance(digest, str)
            or SHA256_PATTERN.fullmatch(digest) is None
        ):
            _fail('model_file_sha256')
        if relative in declared:
            _fail('model_file_duplicate')
        declared[relative] = digest
    if set(declared) != REQUIRED_MODEL_FILES:
        _fail('model_file_set')
    for relative, expected in sorted(declared.items()):
        _hash_model_file(root, snapshot, relative, expected)
    lookup = (
        importlib.metadata.version
        if version_lookup is None
        else version_lookup
    )
    for package, expected in sorted(versions.items()):
        try:
            actual = lookup(package)
        except Exception:
            raise ModelSecurityError('model_runtime_unavailable')
        if actual != expected:
            _fail('model_runtime_version_mismatch')
    manifest_digest = hashlib.sha256(payload).hexdigest()
    combined_digest = hashlib.sha256(
        (
            'malbut-faster-whisper-model-v1\0'
            f'{model_id}\0{revision}\0{manifest_digest}'
        ).encode('utf-8')
    ).hexdigest()
    return VerifiedModel(
        snapshot_path=snapshot,
        runtime_versions=dict(versions),
        attestation=ModelAttestation(
            model_digest=combined_digest,
            model_id=model_id,
            snapshot_revision=revision,
        ),
    )
