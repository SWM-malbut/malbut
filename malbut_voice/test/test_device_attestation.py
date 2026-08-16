"""Tests for ALSA static and child-process identity attestation."""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from malbut_voice.config import AudioBinding, VoiceConfig
from malbut_voice.device_attestation import AlsaDeviceInspector
from malbut_voice.errors import DeviceAttestationError
from malbut_voice.provenance import DeviceAttestation


def _binding(driver='snd_hda_intel'):
    return AudioBinding(
        alsa_device='plughw:CARD=PCH,DEV=0',
        device_node=Path('/dev/snd/pcmC1D0c'),
        rdev_major=116,
        rdev_minor=9,
        device_uid=0,
        device_gid=29,
        device_mode=0o660,
        card_id='PCH',
        driver=driver,
        arecord_path=Path('/usr/bin/arecord'),
        arecord_resolved_path=Path('/usr/bin/aplay'),
        arecord_sha256='a' * 64,
        alsa_config_path=Path('/usr/share/alsa/alsa.conf'),
        alsa_config_sha256='b' * 64,
    )


def _sysfs(tmp_path, driver='snd_hda_intel'):
    root = tmp_path / 'sys'
    card = root / 'class' / 'sound' / 'card1'
    card.mkdir(parents=True)
    (card / 'id').write_text('PCH\n', encoding='ascii')
    driver_directory = root / 'bus' / 'pci' / 'drivers' / driver
    driver_directory.mkdir(parents=True)
    device = card / 'device'
    device.mkdir()
    (device / 'driver').symlink_to(driver_directory)
    pcm = root / 'devices' / 'fake' / 'sound' / 'card1' / 'pcmC1D0c'
    pcm.mkdir(parents=True)
    dev_char = root / 'dev' / 'char'
    dev_char.mkdir(parents=True)
    (dev_char / '116:9').symlink_to(pcm)
    return root


def _device_metadata():
    return SimpleNamespace(
        st_mode=stat.S_IFCHR | 0o660,
        st_uid=0,
        st_gid=29,
        st_rdev=os.makedev(116, 9),
    )


def test_static_attestation_binds_rdev_card_driver_and_binary(tmp_path):
    """Produce one digest only when every configured identity agrees."""
    calls = []

    def file_attestor(path, digest, label, resolved_path):
        calls.append((path, digest, label, resolved_path))
        return SimpleNamespace(st_dev=7, st_ino=11)

    inspector = AlsaDeviceInspector(
        _binding(),
        sysfs_root=_sysfs(tmp_path),
        device_lstat=lambda _path: _device_metadata(),
        file_attestor=file_attestor,
    )

    attestation = inspector.attest()

    assert attestation.rdev_major == 116
    assert attestation.rdev_minor == 9
    assert attestation.binary_device == 7
    assert attestation.binary_inode == 11
    assert len(attestation.binding_digest) == 64
    assert [call[2] for call in calls] == [
        'arecord_binary',
        'alsa_config',
    ]
    assert calls[0][3] == Path('/usr/bin/aplay')
    assert calls[1][3] is None


def test_static_attestation_rejects_wrong_rdev_before_binary_check(tmp_path):
    """Reject a devnode substitution even if its path text is unchanged."""
    metadata = _device_metadata()
    metadata.st_rdev = os.makedev(116, 10)
    inspector = AlsaDeviceInspector(
        _binding(),
        sysfs_root=_sysfs(tmp_path),
        device_lstat=lambda _path: metadata,
        file_attestor=lambda *_args: SimpleNamespace(st_dev=7, st_ino=11),
    )

    with pytest.raises(DeviceAttestationError, match='rdev_mismatch'):
        inspector.attest()


def test_static_attestation_explicitly_rejects_loopback_driver(tmp_path):
    """Never treat snd-aloop as physical microphone provenance."""
    inspector = AlsaDeviceInspector(
        _binding(driver='snd_aloop'),
        sysfs_root=_sysfs(tmp_path, driver='snd_aloop'),
        device_lstat=lambda _path: _device_metadata(),
        file_attestor=lambda *_args: SimpleNamespace(st_dev=7, st_ino=11),
    )

    with pytest.raises(DeviceAttestationError, match='driver_mismatch'):
        inspector.attest()


def test_child_attestation_requires_exact_exe_inode_and_open_char_rdev(
    tmp_path,
):
    """Bind the running child to both arecord inode and microphone fd."""
    binary = tmp_path / 'arecord'
    binary.write_bytes(b'fake executable')
    process = tmp_path / 'proc' / '123'
    descriptors = process / 'fd'
    descriptors.mkdir(parents=True)
    (process / 'exe').symlink_to(binary)
    (descriptors / '7').symlink_to('/dev/null')
    binary_metadata = binary.stat()
    null_metadata = os.stat('/dev/null')
    attestation = DeviceAttestation(
        binding_digest='a' * 64,
        binary_device=binary_metadata.st_dev,
        binary_inode=binary_metadata.st_ino,
        rdev_major=os.major(null_metadata.st_rdev),
        rdev_minor=os.minor(null_metadata.st_rdev),
    )
    inspector = AlsaDeviceInspector(_binding(), proc_root=tmp_path / 'proc')

    assert inspector.child_has_attested_device(123, attestation) is True

    (descriptors / '7').unlink()
    (descriptors / '7').symlink_to('/dev/zero')
    assert inspector.child_has_attested_device(123, attestation) is False


def test_child_attestation_rejects_executable_substitution(tmp_path):
    """Reject an executable inode that differs from the static hash target."""
    binary = tmp_path / 'other-program'
    binary.write_bytes(b'not arecord')
    process = tmp_path / 'proc' / '456'
    (process / 'fd').mkdir(parents=True)
    (process / 'exe').symlink_to(binary)
    inspector = AlsaDeviceInspector(_binding(), proc_root=tmp_path / 'proc')
    attestation = DeviceAttestation(
        binding_digest='a' * 64,
        binary_device=999,
        binary_inode=999,
        rdev_major=116,
        rdev_minor=9,
    )

    with pytest.raises(DeviceAttestationError, match='exe_mismatch'):
        inspector.child_has_attested_device(456, attestation)


AUDITED_HOST_READY = (
    Path('/dev/snd/pcmC1D0c').exists()
    and Path('/usr/bin/arecord').is_symlink()
    and os.readlink('/usr/bin/arecord') == 'aplay'
)


@pytest.mark.skipif(
    not AUDITED_HOST_READY,
    reason='audited PCH/arecord host resources are absent',
)
def test_audited_host_static_binding_without_opening_microphone():
    """Verify the shipped host binding without spawning or opening audio."""
    package_root = Path(__file__).resolve().parents[1]
    raw = json.loads(
        (package_root / 'config' / 'microphone-stt.example.json').read_text(
            encoding='utf-8'
        )
    )
    config = VoiceConfig.from_dict(raw)

    attestation = AlsaDeviceInspector(config.audio).attest()

    assert attestation.rdev_major == 116
    assert attestation.rdev_minor == 9
