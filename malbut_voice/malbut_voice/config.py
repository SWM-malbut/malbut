"""Strict loading for the fixed local microphone and model binding."""

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

from malbut_voice.errors import ConfigSecurityError, chain_free_boundary


CONFIG_SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 65536
SHA256_PATTERN = re.compile(r'^[0-9a-f]{64}$')
ALSA_SELECTOR_PATTERN = re.compile(
    r'^(?:hw|plughw):CARD=([A-Za-z0-9_-]{1,32}),DEV=([0-9]{1,2})$'
)
DEVICE_NODE_PATTERN = re.compile(r'^/dev/snd/pcmC([0-9]+)D([0-9]+)c$')
SAFE_DRIVER_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
FIXED_SOURCE = 'local-hardware-faster-whisper-v1'
_PROTECTED_CONFIG_SECRET = secrets.token_bytes(32)


def _reject_unknown(value, allowed, label):
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigSecurityError('config_unknown_field')
    missing = set(allowed) - set(value)
    if missing:
        raise ConfigSecurityError('config_missing_field')


def _object(value, label):
    if not isinstance(value, dict):
        raise ConfigSecurityError('config_invalid_object')
    return value


def _string(value, label, maximum=256):
    if not isinstance(value, str):
        raise ConfigSecurityError('config_invalid_string')
    if value != value.strip() or not value or len(value) > maximum:
        raise ConfigSecurityError('config_invalid_string')
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ConfigSecurityError('config_invalid_string')
    return value


def _integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigSecurityError('config_invalid_integer')
    if value < minimum or value > maximum:
        raise ConfigSecurityError('config_invalid_integer')
    return value


def _sha256(value, label):
    result = _string(value, label, 64)
    if SHA256_PATTERN.fullmatch(result) is None:
        raise ConfigSecurityError('config_invalid_sha256')
    return result


def _absolute_path(value, label):
    result = Path(_string(value, label, 4096))
    if not result.is_absolute() or '..' in result.parts:
        raise ConfigSecurityError('config_invalid_path')
    return result


def _parse_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigSecurityError('config_duplicate_field')
        result[key] = value
    return result


def _validate_parent_directories(path):
    current = path.parent
    while True:
        try:
            metadata = os.lstat(current)
        except OSError:
            raise ConfigSecurityError('config_parent_unavailable')
        if not stat.S_ISDIR(metadata.st_mode):
            raise ConfigSecurityError('config_parent_not_directory')
        if stat.S_ISLNK(metadata.st_mode):
            raise ConfigSecurityError('config_parent_symlink')
        writable = metadata.st_mode & 0o022
        sticky_root = (
            bool(metadata.st_mode & stat.S_ISVTX)
            and metadata.st_uid == 0
        )
        if writable and not sticky_root:
            raise ConfigSecurityError('config_parent_writable')
        if current.parent == current:
            break
        current = current.parent


@chain_free_boundary
def read_protected_file(path, *, maximum_bytes=MAX_CONFIG_BYTES):
    """Read one absolute protected regular file exactly once by descriptor."""
    candidate = Path(path)
    if not candidate.is_absolute() or '..' in candidate.parts:
        raise ConfigSecurityError('protected_file_path_invalid')
    _validate_parent_directories(candidate)
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError:
        raise ConfigSecurityError('protected_file_open_failed')
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigSecurityError('protected_file_not_regular')
        if metadata.st_nlink != 1:
            raise ConfigSecurityError('protected_file_link_count')
        if metadata.st_uid not in {0, os.geteuid()}:
            raise ConfigSecurityError('protected_file_owner')
        if metadata.st_mode & 0o022:
            raise ConfigSecurityError('protected_file_writable')
        if metadata.st_size < 1 or metadata.st_size > maximum_bytes:
            raise ConfigSecurityError('protected_file_size')
        chunks = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b''.join(chunks)
        if len(payload) > maximum_bytes:
            raise ConfigSecurityError('protected_file_size')
        return payload
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class AudioBinding:
    """Immutable physical ALSA capture-device and binary identity."""

    alsa_device: str
    device_node: Path
    rdev_major: int
    rdev_minor: int
    device_uid: int
    device_gid: int
    device_mode: int
    card_id: str
    driver: str
    arecord_path: Path
    arecord_resolved_path: Path
    arecord_sha256: str
    alsa_config_path: Path
    alsa_config_sha256: str

    @classmethod
    def from_dict(cls, value):
        """Validate an exact hardware and executable binding."""
        body = _object(value, 'audio')
        allowed = {
            'alsa_device', 'device_node', 'rdev_major', 'rdev_minor',
            'device_uid', 'device_gid', 'device_mode', 'card_id',
            'driver', 'arecord_path', 'arecord_sha256',
            'arecord_resolved_path',
            'alsa_config_path', 'alsa_config_sha256',
        }
        _reject_unknown(body, allowed, 'audio')
        selector = _string(body['alsa_device'], 'alsa_device')
        selector_match = ALSA_SELECTOR_PATTERN.fullmatch(selector)
        if selector_match is None or int(selector_match.group(2)) > 31:
            raise ConfigSecurityError('config_alsa_selector')
        card_id = _string(body['card_id'], 'card_id', 32)
        if selector_match.group(1) != card_id:
            raise ConfigSecurityError('config_alsa_card_mismatch')
        device_node = _absolute_path(body['device_node'], 'device_node')
        node_match = DEVICE_NODE_PATTERN.fullmatch(str(device_node))
        if node_match is None:
            raise ConfigSecurityError('config_device_node')
        if int(node_match.group(2)) != int(selector_match.group(2)):
            raise ConfigSecurityError('config_device_number_mismatch')
        driver = _string(body['driver'], 'driver', 64)
        if (
            SAFE_DRIVER_PATTERN.fullmatch(driver) is None
            or driver == 'snd_aloop'
        ):
            raise ConfigSecurityError('config_driver')
        arecord_path = _absolute_path(body['arecord_path'], 'arecord_path')
        arecord_resolved_path = _absolute_path(
            body['arecord_resolved_path'],
            'arecord_resolved_path',
        )
        alsa_path = _absolute_path(
            body['alsa_config_path'],
            'alsa_config_path',
        )
        if str(arecord_path) != '/usr/bin/arecord':
            raise ConfigSecurityError('config_arecord_path')
        if str(arecord_resolved_path) != '/usr/bin/aplay':
            raise ConfigSecurityError('config_arecord_resolved_path')
        rdev_major = _integer(
            body['rdev_major'], 'rdev_major', 0, (1 << 20) - 1
        )
        device_uid = _integer(
            body['device_uid'], 'device_uid', 0, (1 << 31) - 1
        )
        device_mode = _integer(
            body['device_mode'], 'device_mode', 0, 0o7777
        )
        if rdev_major != 116 or device_uid != 0 or device_mode != 0o660:
            raise ConfigSecurityError('config_device_security')
        return cls(
            alsa_device=selector,
            device_node=device_node,
            rdev_major=rdev_major,
            rdev_minor=_integer(
                body['rdev_minor'], 'rdev_minor', 0, (1 << 20) - 1
            ),
            device_uid=device_uid,
            device_gid=_integer(
                body['device_gid'], 'device_gid', 0, (1 << 31) - 1
            ),
            device_mode=device_mode,
            card_id=card_id,
            driver=driver,
            arecord_path=arecord_path,
            arecord_resolved_path=arecord_resolved_path,
            arecord_sha256=_sha256(
                body['arecord_sha256'], 'arecord_sha256'
            ),
            alsa_config_path=alsa_path,
            alsa_config_sha256=_sha256(
                body['alsa_config_sha256'], 'alsa_config_sha256'
            ),
        )

    @property
    def card_number(self):
        """Return the kernel ALSA card number encoded by the devnode."""
        match = DEVICE_NODE_PATTERN.fullmatch(str(self.device_node))
        if match is None:
            raise ConfigSecurityError('config_device_node')
        return int(match.group(1))


@dataclass(frozen=True)
class CapturePolicy:
    """Strict PCM shape, deadline, and teardown bounds."""

    sample_rate_hz: int
    channels: int
    sample_format: str
    default_duration_seconds: int
    maximum_duration_seconds: int
    maximum_stderr_bytes: int
    attestation_timeout_ms: int
    completion_grace_ms: int
    term_grace_ms: int
    kill_grace_ms: int

    @classmethod
    def from_dict(cls, value):
        """Validate capture bounds without accepting command fragments."""
        body = _object(value, 'capture')
        allowed = {
            'sample_rate_hz', 'channels', 'sample_format',
            'default_duration_seconds', 'maximum_duration_seconds',
            'maximum_stderr_bytes', 'attestation_timeout_ms',
            'completion_grace_ms', 'term_grace_ms', 'kill_grace_ms',
        }
        _reject_unknown(body, allowed, 'capture')
        rate = _integer(
            body['sample_rate_hz'], 'sample_rate_hz', 8000, 48000
        )
        channels = _integer(body['channels'], 'channels', 1, 2)
        if rate != 16000 or channels != 1:
            raise ConfigSecurityError('config_pcm_shape')
        sample_format = _string(body['sample_format'], 'sample_format', 16)
        if sample_format != 'S16_LE':
            raise ConfigSecurityError('config_sample_format')
        maximum = _integer(
            body['maximum_duration_seconds'],
            'maximum_duration_seconds',
            1,
            30,
        )
        default = _integer(
            body['default_duration_seconds'],
            'default_duration_seconds',
            1,
            maximum,
        )
        return cls(
            sample_rate_hz=rate,
            channels=channels,
            sample_format=sample_format,
            default_duration_seconds=default,
            maximum_duration_seconds=maximum,
            maximum_stderr_bytes=_integer(
                body['maximum_stderr_bytes'],
                'maximum_stderr_bytes',
                256,
                65536,
            ),
            attestation_timeout_ms=_integer(
                body['attestation_timeout_ms'],
                'attestation_timeout_ms',
                50,
                5000,
            ),
            completion_grace_ms=_integer(
                body['completion_grace_ms'],
                'completion_grace_ms',
                50,
                5000,
            ),
            term_grace_ms=_integer(
                body['term_grace_ms'], 'term_grace_ms', 10, 5000
            ),
            kill_grace_ms=_integer(
                body['kill_grace_ms'], 'kill_grace_ms', 10, 5000
            ),
        )

    def expected_bytes(self, duration_seconds):
        """Return the exact bounded raw S16_LE payload size."""
        duration = _integer(
            duration_seconds,
            'duration_seconds',
            1,
            self.maximum_duration_seconds,
        )
        return duration * self.sample_rate_hz * self.channels * 2


@dataclass(frozen=True)
class ModelBinding:
    """Protected local model root and signed-by-hash manifest paths."""

    root: Path
    manifest: Path

    @classmethod
    def from_dict(cls, value):
        """Validate fixed absolute model paths."""
        body = _object(value, 'model')
        _reject_unknown(body, {'root', 'manifest'}, 'model')
        return cls(
            root=_absolute_path(body['root'], 'model.root'),
            manifest=_absolute_path(body['manifest'], 'model.manifest'),
        )


@dataclass(frozen=True)
class SpeechBinding:
    """Protected routing labels that do not prove speaker identity."""

    user_id: str
    speaker_id: str
    speech_session_id: str
    conversation_id: str
    capture_epoch: int

    @classmethod
    def from_dict(cls, value):
        """Validate bounded server-owned transcript routing labels."""
        body = _object(value, 'binding')
        allowed = {
            'user_id', 'speaker_id', 'speech_session_id',
            'conversation_id', 'capture_epoch',
        }
        _reject_unknown(body, allowed, 'binding')
        return cls(
            user_id=_string(body['user_id'], 'user_id', 128),
            speaker_id=_string(body['speaker_id'], 'speaker_id', 128),
            speech_session_id=_string(
                body['speech_session_id'], 'speech_session_id', 128
            ),
            conversation_id=_string(
                body['conversation_id'], 'conversation_id', 128
            ),
            capture_epoch=_integer(
                body['capture_epoch'], 'capture_epoch', 1, (1 << 63) - 1
            ),
        )


@dataclass(frozen=True)
class VoiceConfig:
    """Complete immutable configuration for one local voice source."""

    audio: AudioBinding
    capture: CapturePolicy
    model: ModelBinding
    binding: SpeechBinding
    _protection_proof: str = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_dict(cls, value):
        """Validate the strict top-level configuration schema."""
        body = _object(value, 'config')
        allowed = {'schema_version', 'audio', 'capture', 'model', 'binding'}
        _reject_unknown(body, allowed, 'config')
        if (
            type(body['schema_version']) is not int
            or body['schema_version'] != CONFIG_SCHEMA_VERSION
        ):
            raise ConfigSecurityError('config_schema_version')
        return cls(
            audio=AudioBinding.from_dict(body['audio']),
            capture=CapturePolicy.from_dict(body['capture']),
            model=ModelBinding.from_dict(body['model']),
            binding=SpeechBinding.from_dict(body['binding']),
        )

    @property
    def is_protected(self):
        """Return whether this config came from the protected fd loader."""
        if not isinstance(self._protection_proof, str):
            return False
        try:
            expected = _voice_config_proof(self)
        except Exception:
            return False
        return hmac.compare_digest(self._protection_proof, expected)


def _voice_config_payload(config):
    return {
        'schema_version': CONFIG_SCHEMA_VERSION,
        'audio': {
            'alsa_device': config.audio.alsa_device,
            'device_node': str(config.audio.device_node),
            'rdev_major': config.audio.rdev_major,
            'rdev_minor': config.audio.rdev_minor,
            'device_uid': config.audio.device_uid,
            'device_gid': config.audio.device_gid,
            'device_mode': config.audio.device_mode,
            'card_id': config.audio.card_id,
            'driver': config.audio.driver,
            'arecord_path': str(config.audio.arecord_path),
            'arecord_resolved_path': str(
                config.audio.arecord_resolved_path
            ),
            'arecord_sha256': config.audio.arecord_sha256,
            'alsa_config_path': str(config.audio.alsa_config_path),
            'alsa_config_sha256': config.audio.alsa_config_sha256,
        },
        'capture': {
            'sample_rate_hz': config.capture.sample_rate_hz,
            'channels': config.capture.channels,
            'sample_format': config.capture.sample_format,
            'default_duration_seconds': (
                config.capture.default_duration_seconds
            ),
            'maximum_duration_seconds': (
                config.capture.maximum_duration_seconds
            ),
            'maximum_stderr_bytes': config.capture.maximum_stderr_bytes,
            'attestation_timeout_ms': config.capture.attestation_timeout_ms,
            'completion_grace_ms': config.capture.completion_grace_ms,
            'term_grace_ms': config.capture.term_grace_ms,
            'kill_grace_ms': config.capture.kill_grace_ms,
        },
        'model': {
            'root': str(config.model.root),
            'manifest': str(config.model.manifest),
        },
        'binding': {
            'user_id': config.binding.user_id,
            'speaker_id': config.binding.speaker_id,
            'speech_session_id': config.binding.speech_session_id,
            'conversation_id': config.binding.conversation_id,
            'capture_epoch': config.binding.capture_epoch,
        },
    }


def _voice_config_proof(config):
    payload = json.dumps(
        _voice_config_payload(config),
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    ).encode('ascii')
    return hmac.new(
        _PROTECTED_CONFIG_SECRET,
        b'malbut-protected-voice-config-v1\0' + payload,
        hashlib.sha256,
    ).hexdigest()


def _configuration_fingerprint(payload):
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        ).encode('ascii')
    except Exception:
        raise ConfigSecurityError('config_integrity_rejected')
    return hashlib.sha256(encoded).hexdigest()


def _audio_binding_fingerprint(binding):
    if type(binding) is not AudioBinding:
        raise ConfigSecurityError('config_integrity_rejected')
    body = {
        'alsa_device': binding.alsa_device,
        'device_node': str(binding.device_node),
        'rdev_major': binding.rdev_major,
        'rdev_minor': binding.rdev_minor,
        'device_uid': binding.device_uid,
        'device_gid': binding.device_gid,
        'device_mode': binding.device_mode,
        'card_id': binding.card_id,
        'driver': binding.driver,
        'arecord_path': str(binding.arecord_path),
        'arecord_resolved_path': str(binding.arecord_resolved_path),
        'arecord_sha256': binding.arecord_sha256,
        'alsa_config_path': str(binding.alsa_config_path),
        'alsa_config_sha256': binding.alsa_config_sha256,
    }
    if AudioBinding.from_dict(body) != binding:
        raise ConfigSecurityError('config_integrity_rejected')
    return _configuration_fingerprint(body)


def _capture_policy_fingerprint(policy):
    if type(policy) is not CapturePolicy:
        raise ConfigSecurityError('config_integrity_rejected')
    body = {
        'sample_rate_hz': policy.sample_rate_hz,
        'channels': policy.channels,
        'sample_format': policy.sample_format,
        'default_duration_seconds': policy.default_duration_seconds,
        'maximum_duration_seconds': policy.maximum_duration_seconds,
        'maximum_stderr_bytes': policy.maximum_stderr_bytes,
        'attestation_timeout_ms': policy.attestation_timeout_ms,
        'completion_grace_ms': policy.completion_grace_ms,
        'term_grace_ms': policy.term_grace_ms,
        'kill_grace_ms': policy.kill_grace_ms,
    }
    if CapturePolicy.from_dict(body) != policy:
        raise ConfigSecurityError('config_integrity_rejected')
    return _configuration_fingerprint(body)


def _model_binding_fingerprint(binding):
    if type(binding) is not ModelBinding:
        raise ConfigSecurityError('config_integrity_rejected')
    body = {'root': str(binding.root), 'manifest': str(binding.manifest)}
    if ModelBinding.from_dict(body) != binding:
        raise ConfigSecurityError('config_integrity_rejected')
    return _configuration_fingerprint(body)


@chain_free_boundary
def load_protected_config(path):
    """Load strict JSON from one protected, non-symlinked file."""
    payload = read_protected_file(Path(path))
    try:
        text = payload.decode('utf-8')
        raw = json.loads(
            text,
            object_pairs_hook=_parse_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ConfigSecurityError('config_nonfinite_number')
            ),
        )
    except ConfigSecurityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigSecurityError('config_invalid_json')
    config = VoiceConfig.from_dict(raw)
    object.__setattr__(
        config,
        '_protection_proof',
        _voice_config_proof(config),
    )
    return config
