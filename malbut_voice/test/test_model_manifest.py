"""Tests for local model containment, hashes, and runtime pins."""

import pytest

from conftest import create_model_tree, write_protected_json
from malbut_voice.config import ModelBinding
from malbut_voice.errors import ModelSecurityError
from malbut_voice.model_manifest import verify_model_binding


def test_model_manifest_verifies_exact_files_and_versions(
    tmp_path,
    expected_versions,
):
    """Accept a contained protected snapshot with exact content hashes."""
    root, snapshot, manifest_path, _manifest = create_model_tree(tmp_path)

    verified = verify_model_binding(
        ModelBinding(root=root, manifest=manifest_path),
        version_lookup=expected_versions.__getitem__,
    )

    assert verified.snapshot_path == snapshot
    assert verified.attestation.model_id == 'test-small'
    assert verified.attestation.snapshot_revision == '1' * 40
    assert len(verified.attestation.model_digest) == 64


def test_model_manifest_rejects_hash_mismatch(tmp_path, expected_versions):
    """Reject changed model bytes before constructing faster-whisper."""
    root, _snapshot, manifest_path, manifest = create_model_tree(tmp_path)
    manifest['files'][0]['sha256'] = '0' * 64
    write_protected_json(manifest_path, manifest)

    with pytest.raises(ModelSecurityError, match='hash_mismatch'):
        verify_model_binding(
            ModelBinding(root=root, manifest=manifest_path),
            version_lookup=expected_versions.__getitem__,
        )


def test_model_manifest_rejects_symlink_escape(tmp_path, expected_versions):
    """Reject a model file whose resolved target escapes the protected root."""
    root, snapshot, manifest_path, manifest = create_model_tree(tmp_path)
    outside = tmp_path / 'outside-model.bin'
    outside.write_bytes(b'model')
    outside.chmod(0o600)
    target = snapshot / 'model.bin'
    target.unlink()
    target.symlink_to(outside)
    write_protected_json(manifest_path, manifest)

    with pytest.raises(ModelSecurityError, match='path_escape'):
        verify_model_binding(
            ModelBinding(root=root, manifest=manifest_path),
            version_lookup=expected_versions.__getitem__,
        )


def test_model_manifest_rejects_runtime_version_drift(tmp_path):
    """Reject an installed STT runtime that differs from the exact pin."""
    root, _snapshot, manifest_path, _manifest = create_model_tree(tmp_path)

    with pytest.raises(ModelSecurityError, match='version_mismatch'):
        verify_model_binding(
            ModelBinding(root=root, manifest=manifest_path),
            version_lookup=lambda _package: 'unexpected',
        )


def test_model_manifest_rejects_writable_snapshot_parent(
    tmp_path,
    expected_versions,
):
    """Reject a directory that could swap the snapshot after verification."""
    root, _snapshot, manifest_path, _manifest = create_model_tree(tmp_path)
    (root / 'snapshots').chmod(0o770)

    with pytest.raises(ModelSecurityError, match='root_writable'):
        verify_model_binding(
            ModelBinding(root=root, manifest=manifest_path),
            version_lookup=expected_versions.__getitem__,
        )


def test_model_manifest_rejects_writable_resolved_blob_parent(
    tmp_path,
    expected_versions,
):
    """Protect every in-root directory leading to a symlinked model blob."""
    root, snapshot, manifest_path, _manifest = create_model_tree(tmp_path)
    blobs = root / 'blobs'
    blobs.mkdir(mode=0o770)
    blobs.chmod(0o770)
    target = snapshot / 'model.bin'
    blob = blobs / 'model-blob'
    target.rename(blob)
    target.symlink_to(blob)

    with pytest.raises(ModelSecurityError, match='root_writable'):
        verify_model_binding(
            ModelBinding(root=root, manifest=manifest_path),
            version_lookup=expected_versions.__getitem__,
        )


def test_model_manifest_rejects_unpinned_snapshot_file(
    tmp_path,
    expected_versions,
):
    """Do not let the pathname-based loader consume undeclared model input."""
    root, snapshot, manifest_path, _manifest = create_model_tree(tmp_path)
    extra = snapshot / 'preprocessor_config.json'
    extra.write_text('{}', encoding='utf-8')
    extra.chmod(0o600)

    with pytest.raises(ModelSecurityError, match='snapshot_file_set'):
        verify_model_binding(
            ModelBinding(root=root, manifest=manifest_path),
            version_lookup=expected_versions.__getitem__,
        )
