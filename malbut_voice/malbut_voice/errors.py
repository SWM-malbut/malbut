"""Content-free failure types for the local voice boundary."""

from functools import wraps


PUBLIC_ERROR_CODES = frozenset(
    {
        'alsa_card_id_invalid',
        'alsa_card_id_mismatch',
        'alsa_card_id_unavailable',
        'alsa_config_changed',
        'alsa_config_hash_mismatch',
        'alsa_config_link_count',
        'alsa_config_not_protected',
        'alsa_config_not_regular',
        'alsa_config_open_failed',
        'alsa_config_parent_not_protected',
        'alsa_config_parent_unavailable',
        'alsa_config_symlink',
        'alsa_config_unavailable',
        'alsa_driver_mismatch',
        'alsa_driver_unavailable',
        'arecord_binary_changed',
        'arecord_binary_hash_mismatch',
        'arecord_binary_link_count',
        'arecord_binary_link_invalid',
        'arecord_binary_link_target_mismatch',
        'arecord_binary_link_unavailable',
        'arecord_binary_not_protected',
        'arecord_binary_not_regular',
        'arecord_binary_open_failed',
        'arecord_binary_parent_not_protected',
        'arecord_binary_parent_unavailable',
        'arecord_binary_symlink',
        'arecord_binary_unavailable',
        'boot_id_invalid',
        'boot_id_unavailable',
        'capture_capability_invalid',
        'capture_capability_rejected',
        'capture_child_attestation_timeout',
        'capture_child_exe_mismatch',
        'capture_child_exe_unavailable',
        'capture_child_fds_unavailable',
        'capture_child_pid_invalid',
        'capture_child_rejected',
        'capture_child_unattested',
        'capture_configuration_changed',
        'capture_deadline_exceeded',
        'capture_descendant_remained',
        'capture_device_attestation_changed',
        'capture_integrity_rejected',
        'capture_pcm_overflow',
        'capture_pcm_truncated',
        'capture_pipe_read_failed',
        'capture_pipes_unavailable',
        'capture_process_failed',
        'capture_reap_failed',
        'capture_sequence_exhausted',
        'capture_spawn_failed',
        'capture_stderr_overflow',
        'capture_teardown_timeout',
        'clock_boottime_failed',
        'clock_boottime_invalid',
        'clock_boottime_unavailable',
        'config_alsa_card_mismatch',
        'config_alsa_selector',
        'config_arecord_path',
        'config_arecord_resolved_path',
        'config_device_node',
        'config_device_number_mismatch',
        'config_device_security',
        'config_driver',
        'config_duplicate_field',
        'config_invalid_integer',
        'config_invalid_json',
        'config_invalid_object',
        'config_invalid_path',
        'config_invalid_sha256',
        'config_invalid_string',
        'config_integrity_rejected',
        'config_missing_field',
        'config_nonfinite_number',
        'config_parent_not_directory',
        'config_parent_symlink',
        'config_parent_unavailable',
        'config_parent_writable',
        'config_pcm_shape',
        'config_sample_format',
        'config_schema_version',
        'config_security_failed',
        'config_unknown_field',
        'device_attestation_failed',
        'device_node_not_character',
        'device_node_unavailable',
        'device_permissions_mismatch',
        'device_rdev_mismatch',
        'device_sysfs_mismatch',
        'device_sysfs_unavailable',
        'faster_whisper_unavailable',
        'final_capability_rejected',
        'local_model_load_failed',
        'model_binding_type',
        'model_binding_changed',
        'model_attestation_changed',
        'model_directory_changed',
        'model_file_duplicate',
        'model_file_hash_mismatch',
        'model_file_identity_changed',
        'model_file_link_count',
        'model_file_manifest',
        'model_file_not_regular',
        'model_file_open_failed',
        'model_file_owner',
        'model_file_read_failed',
        'model_file_set',
        'model_file_sha256',
        'model_file_unavailable',
        'model_file_writable',
        'model_id',
        'model_manifest_duplicate_field',
        'model_manifest_fields',
        'model_manifest_invalid',
        'model_manifest_schema',
        'model_path_escape',
        'model_root_must_be_canonical',
        'model_root_not_directory',
        'model_root_owner',
        'model_root_unavailable',
        'model_root_writable',
        'model_runtime_fields',
        'model_runtime_package',
        'model_runtime_unavailable',
        'model_runtime_version',
        'model_runtime_version_mismatch',
        'model_security_failed',
        'model_snapshot_path',
        'model_snapshot_revision',
        'model_snapshot_file_set',
        'numpy_unavailable',
        'offline_environment_required',
        'pcm_conversion_failed',
        'protected_file_link_count',
        'protected_file_not_regular',
        'protected_file_open_failed',
        'protected_file_owner',
        'protected_file_path_invalid',
        'protected_file_size',
        'protected_file_writable',
        'source_busy',
        'speech_binding_invalid',
        'speech_event_invalid',
        'stt_confidence_invalid',
        'stt_final_transcript_empty',
        'stt_inference_failed',
        'stt_segment_text_invalid',
        'stt_segments_excessive',
        'stt_segments_invalid',
        'stt_transcript_excessive',
        'stt_word_evidence_invalid',
        'stt_word_evidence_missing',
        'stt_word_probability_invalid',
        'stt_word_timestamp_invalid',
        'stt_word_timestamp_out_of_bounds',
        'stt_words_excessive',
        'transcript_capability_rejected',
        'transcript_integrity_rejected',
        'transcript_security_failed',
        'voice_boundary_failed',
        'voice_internal_error',
    }
)


class VoiceBoundaryError(RuntimeError):
    """Base class for expected, content-free voice boundary failures."""

    code = 'voice_boundary_failed'

    def __init__(self, code=None):
        """Create a failure with a stable public code."""
        candidate = self.code if code is None else code
        public_code = (
            candidate
            if isinstance(candidate, str) and candidate in PUBLIC_ERROR_CODES
            else self.code
        )
        super().__init__(public_code)
        self.code = public_code

    def __getattribute__(self, name):
        """Hide Python exception-chain objects at this public boundary."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


class ConfigSecurityError(VoiceBoundaryError):
    """Reject an untrusted or malformed fixed configuration."""

    code = 'config_security_failed'


class DeviceAttestationError(VoiceBoundaryError):
    """Reject a microphone or capture binary that is not configured."""

    code = 'device_attestation_failed'


class CaptureError(VoiceBoundaryError):
    """Reject an incomplete, excessive, or unattested audio capture."""

    code = 'capture_failed'


class ModelSecurityError(VoiceBoundaryError):
    """Reject an unpinned or unavailable local STT model runtime."""

    code = 'model_security_failed'


class TranscriptSecurityError(VoiceBoundaryError):
    """Reject a transcript without intact private capture provenance."""

    code = 'transcript_security_failed'


def chain_free_boundary(function):
    """Replace an internal exception chain with one stable public failure."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        failure = None
        try:
            return function(*args, **kwargs)
        except VoiceBoundaryError as error:
            failure = (type(error), error.code)
        except Exception:
            failure = (VoiceBoundaryError, 'voice_internal_error')
        error_type, code = failure
        public_error = error_type(code)
        public_error.__cause__ = None
        public_error.__context__ = None
        public_error.__traceback__ = None
        raise public_error

    return wrapped


def clear_exception_details(error):
    """Clear traceback and chain metadata before a public projection."""
    error.__cause__ = None
    error.__context__ = None
    error.__traceback__ = None
