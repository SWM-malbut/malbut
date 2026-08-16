"""Shared strict configuration fixtures for voice boundary tests."""

import hashlib
import json

import pytest


def voice_config_dict(tmp_path):
    """Return one syntactically valid fixed configuration dictionary."""
    return {
        'schema_version': 1,
        'audio': {
            'alsa_device': 'plughw:CARD=PCH,DEV=0',
            'device_node': '/dev/snd/pcmC1D0c',
            'rdev_major': 116,
            'rdev_minor': 9,
            'device_uid': 0,
            'device_gid': 29,
            'device_mode': 0o660,
            'card_id': 'PCH',
            'driver': 'snd_hda_intel',
            'arecord_path': '/usr/bin/arecord',
            'arecord_resolved_path': '/usr/bin/aplay',
            'arecord_sha256': 'a' * 64,
            'alsa_config_path': '/usr/share/alsa/alsa.conf',
            'alsa_config_sha256': 'b' * 64,
        },
        'capture': {
            'sample_rate_hz': 16000,
            'channels': 1,
            'sample_format': 'S16_LE',
            'default_duration_seconds': 1,
            'maximum_duration_seconds': 30,
            'maximum_stderr_bytes': 8192,
            'attestation_timeout_ms': 1000,
            'completion_grace_ms': 1500,
            'term_grace_ms': 500,
            'kill_grace_ms': 500,
        },
        'model': {
            'root': str(tmp_path / 'model'),
            'manifest': str(tmp_path / 'manifest.json'),
        },
        'binding': {
            'user_id': 'local-operator',
            'speaker_id': 'operator-unverified',
            'speech_session_id': 'one-shot-session',
            'conversation_id': 'one-shot-conversation',
            'capture_epoch': 1,
        },
    }


def write_protected_json(path, value):
    """Write a test JSON file with the production protection mode."""
    path.write_text(
        json.dumps(value, sort_keys=True),
        encoding='utf-8',
    )
    path.chmod(0o600)
    return path


def create_model_tree(tmp_path):
    """Create a tiny protected model tree and matching manifest."""
    revision = '1' * 40
    root = tmp_path / 'model'
    snapshot = root / 'snapshots' / revision
    snapshot.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    (root / 'snapshots').chmod(0o700)
    snapshot.chmod(0o700)
    contents = {
        'config.json': b'{}',
        'model.bin': b'model',
        'tokenizer.json': b'{"tokenizer":true}',
        'vocabulary.txt': b'hello\n',
    }
    files = []
    for name, payload in contents.items():
        target = snapshot / name
        target.write_bytes(payload)
        target.chmod(0o600)
        files.append(
            {
                'path': name,
                'sha256': hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest = {
        'schema_version': 1,
        'model_id': 'test-small',
        'snapshot_revision': revision,
        'snapshot_path': f'snapshots/{revision}',
        'runtime_versions': {
            'av': '17.1.0',
            'faster-whisper': '1.2.1',
            'ctranslate2': '4.8.1',
            'numpy': '2.2.6',
            'onnxruntime': '1.23.2',
            'tokenizers': '0.23.1',
        },
        'files': files,
    }
    manifest_path = write_protected_json(tmp_path / 'manifest.json', manifest)
    return root, snapshot, manifest_path, manifest


@pytest.fixture
def config_file(tmp_path):
    """Provide a protected, syntactically valid configuration file."""
    value = voice_config_dict(tmp_path)
    return write_protected_json(tmp_path / 'voice.json', value)


@pytest.fixture
def expected_versions():
    """Provide exact fake package versions used by model tests."""
    return {
        'av': '17.1.0',
        'faster-whisper': '1.2.1',
        'ctranslate2': '4.8.1',
        'numpy': '2.2.6',
        'onnxruntime': '1.23.2',
        'tokenizers': '0.23.1',
    }
