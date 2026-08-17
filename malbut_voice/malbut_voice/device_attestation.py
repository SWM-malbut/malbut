"""Static and live attestation for the fixed ALSA capture device."""

import hashlib
import json
import os
import stat
from pathlib import Path

from malbut_voice.config import AudioBinding
from malbut_voice.errors import DeviceAttestationError, chain_free_boundary
from malbut_voice.provenance import DeviceAttestation


def _raise(code, cause=None):
    raise DeviceAttestationError(code)


def _hash_protected_root_file(
    path,
    expected_sha256,
    label,
    expected_resolved_path=None,
):
    current = path.parent
    while True:
        try:
            parent = os.lstat(current)
        except OSError as exc:
            _raise(f'{label}_parent_unavailable', exc)
        if (
            stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != 0
            or parent.st_mode & 0o022
        ):
            _raise(f'{label}_parent_not_protected')
        if current.parent == current:
            break
        current = current.parent
    try:
        entry_before = os.lstat(path)
    except OSError as exc:
        _raise(f'{label}_unavailable', exc)
    open_path = path
    link_target = None
    if expected_resolved_path is None:
        if stat.S_ISLNK(entry_before.st_mode):
            _raise(f'{label}_symlink')
    else:
        if (
            not stat.S_ISLNK(entry_before.st_mode)
            or entry_before.st_uid != 0
            or entry_before.st_nlink != 1
        ):
            _raise(f'{label}_link_invalid')
        try:
            link_target = os.readlink(path)
        except OSError as exc:
            _raise(f'{label}_link_unavailable', exc)
        expected = Path(expected_resolved_path)
        if (
            Path(link_target).is_absolute()
            or link_target != expected.name
            or path.parent / link_target != expected
            or expected.resolve(strict=False) != expected
        ):
            _raise(f'{label}_link_target_mismatch')
        open_path = expected
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(open_path, flags)
    except OSError as exc:
        _raise(f'{label}_open_failed', exc)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _raise(f'{label}_not_regular')
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            _raise(f'{label}_not_protected')
        if metadata.st_nlink != 1:
            _raise(f'{label}_link_count')
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    try:
        entry_after = os.lstat(path)
        target_after = os.lstat(open_path)
    except OSError as exc:
        _raise(f'{label}_changed', exc)
    if (
        entry_after.st_dev != entry_before.st_dev
        or entry_after.st_ino != entry_before.st_ino
        or target_after.st_dev != metadata.st_dev
        or target_after.st_ino != metadata.st_ino
        or target_after.st_size != metadata.st_size
    ):
        _raise(f'{label}_changed')
    if link_target is not None:
        try:
            link_after = os.readlink(path)
        except OSError as exc:
            _raise(f'{label}_changed', exc)
        if link_after != link_target:
            _raise(f'{label}_changed')
    if digest.hexdigest() != expected_sha256:
        _raise(f'{label}_hash_mismatch')
    return metadata


def _read_text(path, maximum_bytes, label):
    try:
        with path.open('rb') as stream:
            payload = stream.read(maximum_bytes + 1)
    except OSError as exc:
        _raise(f'{label}_unavailable', exc)
    if not payload or len(payload) > maximum_bytes:
        _raise(f'{label}_invalid')
    try:
        result = payload.decode('ascii').strip()
    except UnicodeDecodeError as exc:
        _raise(f'{label}_invalid', exc)
    if not result or any(ord(character) < 32 for character in result):
        _raise(f'{label}_invalid')
    return result


class AlsaDeviceInspector:
    """Verify a fixed physical ALSA device and capture executable."""

    def __init__(
        self,
        binding,
        *,
        sysfs_root=None,
        proc_root=None,
        device_lstat=None,
        file_attestor=None,
    ):
        """Bind the inspector to one immutable configuration object."""
        if not isinstance(binding, AudioBinding):
            raise TypeError('binding must be AudioBinding')
        self._binding = binding
        self._sysfs_root = (
            Path('/sys') if sysfs_root is None else Path(sysfs_root)
        )
        self._proc_root = (
            Path('/proc') if proc_root is None else Path(proc_root)
        )
        self._device_lstat = os.lstat if device_lstat is None else device_lstat
        self._file_attestor = (
            _hash_protected_root_file
            if file_attestor is None
            else file_attestor
        )

    @chain_free_boundary
    def attest(self):
        """Perform a static check without opening the microphone device."""
        binding = self._binding
        try:
            metadata = self._device_lstat(binding.device_node)
        except OSError as exc:
            _raise('device_node_unavailable', exc)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISCHR(metadata.st_mode)
        ):
            _raise('device_node_not_character')
        if os.major(metadata.st_rdev) != binding.rdev_major:
            _raise('device_rdev_mismatch')
        if os.minor(metadata.st_rdev) != binding.rdev_minor:
            _raise('device_rdev_mismatch')
        if (
            metadata.st_uid != binding.device_uid
            or metadata.st_gid != binding.device_gid
            or stat.S_IMODE(metadata.st_mode) != binding.device_mode
        ):
            _raise('device_permissions_mismatch')
        card_number = binding.card_number
        sys_card = self._sysfs_root / 'class' / 'sound' / f'card{card_number}'
        card_id = _read_text(sys_card / 'id', 64, 'alsa_card_id')
        if card_id != binding.card_id:
            _raise('alsa_card_id_mismatch')
        try:
            driver = (sys_card / 'device' / 'driver').resolve(strict=True)
        except OSError as exc:
            _raise('alsa_driver_unavailable', exc)
        if driver.name == 'snd_aloop' or driver.name != binding.driver:
            _raise('alsa_driver_mismatch')
        sys_dev = (
            self._sysfs_root
            / 'dev'
            / 'char'
            / f'{binding.rdev_major}:{binding.rdev_minor}'
        )
        try:
            sys_dev_resolved = sys_dev.resolve(strict=True)
        except OSError as exc:
            _raise('device_sysfs_unavailable', exc)
        if (
            sys_dev_resolved.name != binding.device_node.name
            or f'card{card_number}' not in sys_dev_resolved.parts
        ):
            _raise('device_sysfs_mismatch')
        binary = self._file_attestor(
            binding.arecord_path,
            binding.arecord_sha256,
            'arecord_binary',
            binding.arecord_resolved_path,
        )
        self._file_attestor(
            binding.alsa_config_path,
            binding.alsa_config_sha256,
            'alsa_config',
            None,
        )
        fields = {
            'alsa_device': binding.alsa_device,
            'device_node': str(binding.device_node),
            'rdev_major': binding.rdev_major,
            'rdev_minor': binding.rdev_minor,
            'device_uid': binding.device_uid,
            'device_gid': binding.device_gid,
            'device_mode': binding.device_mode,
            'card_id': binding.card_id,
            'card_number': card_number,
            'driver': binding.driver,
            'arecord_resolved_path': str(binding.arecord_resolved_path),
            'arecord_sha256': binding.arecord_sha256,
            'alsa_config_sha256': binding.alsa_config_sha256,
        }
        digest = hashlib.sha256(
            json.dumps(
                fields,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('ascii')
        ).hexdigest()
        return DeviceAttestation(
            binding_digest=digest,
            binary_device=binary.st_dev,
            binary_inode=binary.st_ino,
            rdev_major=binding.rdev_major,
            rdev_minor=binding.rdev_minor,
        )

    @chain_free_boundary
    def child_has_attested_device(self, pid, attestation):
        """Verify the running child executable and exact open capture rdev."""
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            _raise('capture_child_pid_invalid')
        proc = self._proc_root / str(pid)
        try:
            executable = os.stat(proc / 'exe')
        except OSError as exc:
            _raise('capture_child_exe_unavailable', exc)
        if (
            executable.st_dev != attestation.binary_device
            or executable.st_ino != attestation.binary_inode
        ):
            _raise('capture_child_exe_mismatch')
        try:
            descriptors = tuple((proc / 'fd').iterdir())
        except OSError as exc:
            _raise('capture_child_fds_unavailable', exc)
        for descriptor in descriptors:
            try:
                metadata = os.stat(descriptor)
            except OSError:
                continue
            if (
                stat.S_ISCHR(metadata.st_mode)
                and os.major(metadata.st_rdev) == attestation.rdev_major
                and os.minor(metadata.st_rdev) == attestation.rdev_minor
            ):
                return True
        return False
