"""Environment-based settings with explicit OpenAI safety bounds."""

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from malbut_agent_server.endpoint_policy import (
    OFFICIAL_OPENAI_BASE_URL,
    is_official_openai_base_url,
)
from malbut_agent_server.homecam_semantic import HomecamSemanticConfig
from malbut_agent_server.providers.openai_responses import (
    REASONING_EFFORTS,
)


SUPPORTED_PROVIDERS = frozenset({'mock', 'openai'})
SUPPORTED_TOOL_MODES = frozenset({'proposal', 'simulation'})
DEFAULT_OPENAI_MODEL = 'gpt-5.6-terra'
DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS = 5
DEFAULT_PROVIDER_TOTAL_TIMEOUT_SECONDS = 11
DEFAULT_FAILED_AUTH_ATTEMPTS_PER_MINUTE = 30
MAX_FAILED_AUTH_ATTEMPTS_PER_MINUTE = 10000
MIN_SCRIPTED_AUTH_TOKEN_LENGTH = 32
MAX_SCRIPTED_AUTH_TOKEN_LENGTH = 512
MAX_MONITORABLE_ROOMS = 32
MAX_MONITORABLE_ROOM_LENGTH = 80
MIN_GAZEBO_AUTHORITY_SECRET_LENGTH = 32
MAX_GAZEBO_AUTHORITY_SECRET_LENGTH = 512
MAX_GAZEBO_PREPARE_TIMEOUT_SECONDS = 30
MIN_GAZEBO_PREPARE_LEASE_SECONDS = 1
MAX_GAZEBO_PREPARE_LEASE_SECONDS = 300


def load_env_file(
    path: Path,
    target: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Load simple KEY=VALUE entries without logging their values."""
    destination = target if target is not None else os.environ
    if not path.exists():
        return destination

    with path.open('r', encoding='utf-8') as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            if not key or not key.replace('_', '').isalnum():
                continue
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {'"', "'"}
            ):
                value = value[1:-1]
            destination.setdefault(key, value)
    return destination


def _env_int(
    environ: Mapping[str, str],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = environ.get(key)
    if value is None:
        return default
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f'{key} must be an integer') from error
    if result < minimum or result > maximum:
        raise ValueError(
            f'{key} must be between {minimum} and {maximum}'
        )
    return result


def _env_bool(
    environ: Mapping[str, str],
    key: str,
    default: bool,
) -> bool:
    """Parse one explicit boolean without truthy-string surprises."""
    value = environ.get(key)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {'1', 'true'}:
        return True
    if normalized in {'0', 'false'}:
        return False
    raise ValueError(f'{key} must be true, false, 1, or 0')


def _env_optional_int(
    environ: Mapping[str, str],
    key: str,
    minimum: int,
    maximum: int,
) -> Optional[int]:
    """Parse one optional bounded integer without sentinel values."""
    value = environ.get(key)
    if value is None or not value.strip():
        return None
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f'{key} must be an integer') from error
    if result < minimum or result > maximum:
        raise ValueError(
            f'{key} must be between {minimum} and {maximum}'
        )
    return result


def _env_monitorable_rooms(
    environ: Mapping[str, str],
) -> Tuple[str, ...]:
    """Parse an explicit, bounded server-owned room allowlist."""
    raw = environ.get('MALBUT_AGENT_MONITORABLE_ROOMS')
    if raw is None or not raw.strip():
        return ()
    values = raw.split(',')
    if len(values) > MAX_MONITORABLE_ROOMS:
        raise ValueError(
            'MALBUT_AGENT_MONITORABLE_ROOMS has too many items'
        )
    normalized = []
    canonical_keys = set()
    for value in values:
        room = unicodedata.normalize('NFKC', value)
        room = ' '.join(room.split())
        if (
            not room
            or len(room) > MAX_MONITORABLE_ROOM_LENGTH
            or any(
                unicodedata.category(character).startswith('C')
                for character in room
            )
        ):
            raise ValueError(
                'MALBUT_AGENT_MONITORABLE_ROOMS contains an invalid room'
            )
        canonical_key = room.casefold()
        if canonical_key in canonical_keys:
            raise ValueError(
                'MALBUT_AGENT_MONITORABLE_ROOMS contains duplicates'
            )
        canonical_keys.add(canonical_key)
        normalized.append(room)
    return tuple(normalized)


def _valid_model_id(value: str) -> bool:
    """Accept a bounded printable model identifier."""
    return (
        bool(value)
        and len(value) <= 128
        and value.isascii()
        and all(32 < ord(character) < 127 for character in value)
    )


def validate_scripted_auth_token(value: object) -> str:
    """Require one bounded visible-ASCII bearer for scripted ingress."""
    if (
        not isinstance(value, str)
        or len(value) < MIN_SCRIPTED_AUTH_TOKEN_LENGTH
        or len(value) > MAX_SCRIPTED_AUTH_TOKEN_LENGTH
        or not value.isascii()
        or any(
            ord(character) < 33 or ord(character) > 126
            for character in value
        )
    ):
        raise ValueError(
            'scripted speech requires a 32 to 512 character '
            'visible-ASCII MALBUT_AGENT_AUTH_TOKEN'
        )
    return value


@dataclass(frozen=True)
class Settings:
    """Runtime settings whose representations never expose credentials."""

    provider: str = 'mock'
    host: str = '127.0.0.1'
    port: int = 8765
    database_path: str = ':memory:'
    user_id: str = 'local-user'
    memory_limit: int = 5
    conversation_ttl_seconds: int = 1800
    conversation_history_limit: int = 10
    conversation_summary_max_chars: int = 2000
    max_model_input_chars: int = 20000
    max_conversation_sessions: int = 100
    max_conversation_turns: int = 1000
    max_request_bytes: int = 65536
    max_concurrent_requests: int = 8
    requests_per_minute: int = 60
    failed_auth_attempts_per_minute: int = (
        DEFAULT_FAILED_AUTH_ATTEMPTS_PER_MINUTE
    )
    socket_timeout_seconds: int = 10
    request_timeout_seconds: int = (
        DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS
    )
    provider_total_timeout_seconds: int = (
        DEFAULT_PROVIDER_TOTAL_TIMEOUT_SECONDS
    )
    provider_max_retries: int = 0
    provider_retry_base_delay_ms: int = 250
    provider_retry_max_delay_ms: int = 1000
    provider_failure_threshold: int = 2
    provider_recovery_timeout_seconds: int = 30
    openai_api_key: str = ''
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_fallback_model: str = ''
    openai_base_url: str = OFFICIAL_OPENAI_BASE_URL
    openai_reasoning_effort: str = 'none'
    openai_max_output_tokens: int = 500
    auth_token: str = ''
    tool_mode: str = 'proposal'
    enable_scripted_speech: bool = False
    homecam_origin: str = ''
    homecam_agent_token: str = ''
    homecam_signing_secret: str = ''
    homecam_principal_subject_digest: str = ''
    homecam_device_id: str = ''
    homecam_timeout_seconds: int = 3
    robot_state_socket_path: str = ''
    robot_state_expected_uid: Optional[int] = None
    robot_state_device_id: str = ''
    robot_state_timeout_seconds: int = 2
    monitorable_rooms: Tuple[str, ...] = ()
    enable_gazebo_simulation_execution: bool = False
    gazebo_simulation_authority_secret: str = ''
    gazebo_prepare_socket_path: str = ''
    gazebo_prepare_expected_uid: Optional[int] = None
    gazebo_prepare_timeout_seconds: int = 2
    gazebo_prepare_lease_seconds: int = 30

    def __repr__(self) -> str:
        """Return safe diagnostics with every credential redacted."""
        return (
            'Settings('
            f'provider={self.provider!r}, '
            f'host={self.host!r}, '
            f'port={self.port!r}, '
            f'database_path={self.database_path!r}, '
            f'user_id={self.user_id!r}, '
            f'memory_limit={self.memory_limit!r}, '
            'conversation_ttl_seconds='
            f'{self.conversation_ttl_seconds!r}, '
            'conversation_history_limit='
            f'{self.conversation_history_limit!r}, '
            'conversation_summary_max_chars='
            f'{self.conversation_summary_max_chars!r}, '
            f'max_model_input_chars={self.max_model_input_chars!r}, '
            'request_timeout_seconds='
            f'{self.request_timeout_seconds!r}, '
            'provider_total_timeout_seconds='
            f'{self.provider_total_timeout_seconds!r}, '
            f'provider_max_retries={self.provider_max_retries!r}, '
            'provider_retry_base_delay_ms='
            f'{self.provider_retry_base_delay_ms!r}, '
            'provider_retry_max_delay_ms='
            f'{self.provider_retry_max_delay_ms!r}, '
            'provider_failure_threshold='
            f'{self.provider_failure_threshold!r}, '
            'provider_recovery_timeout_seconds='
            f'{self.provider_recovery_timeout_seconds!r}, '
            'openai_api_key=<redacted>, '
            f'openai_model={self.openai_model!r}, '
            f'openai_fallback_model={self.openai_fallback_model!r}, '
            f'openai_base_url={self.openai_base_url!r}, '
            'openai_reasoning_effort='
            f'{self.openai_reasoning_effort!r}, '
            'openai_max_output_tokens='
            f'{self.openai_max_output_tokens!r}, '
            f'tool_mode={self.tool_mode!r}, '
            'enable_scripted_speech='
            f'{self.enable_scripted_speech!r}, '
            'homecam_origin='
            f'{"<configured>" if self.homecam_origin else "<unconfigured>"}, '
            'homecam_agent_token=<redacted>, '
            'homecam_signing_secret=<redacted>, '
            'homecam_principal_subject_digest=<redacted>, '
            f'homecam_device_id={self.homecam_device_id!r}, '
            'homecam_timeout_seconds='
            f'{self.homecam_timeout_seconds!r}, '
            'robot_state_socket_path=<redacted>, '
            'robot_state_expected_uid='
            f'{self.robot_state_expected_uid!r}, '
            f'robot_state_device_id={self.robot_state_device_id!r}, '
            'robot_state_timeout_seconds='
            f'{self.robot_state_timeout_seconds!r}, '
            f'monitorable_rooms={self.monitorable_rooms!r}, '
            'enable_gazebo_simulation_execution='
            f'{self.enable_gazebo_simulation_execution!r}, '
            'gazebo_simulation_authority_secret=<redacted>, '
            'gazebo_prepare_socket_path=<redacted>, '
            'gazebo_prepare_expected_uid='
            f'{self.gazebo_prepare_expected_uid!r}, '
            'gazebo_prepare_timeout_seconds='
            f'{self.gazebo_prepare_timeout_seconds!r}, '
            'gazebo_prepare_lease_seconds='
            f'{self.gazebo_prepare_lease_seconds!r}, '
            'auth_token=<redacted>)'
        )

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> 'Settings':
        """Create bounded settings from environment variables."""
        source = environ if environ is not None else os.environ
        provider = source.get(
            'MALBUT_AGENT_PROVIDER',
            'mock',
        ).strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError('MALBUT_AGENT_PROVIDER is unsupported')
        enable_scripted_speech = _env_bool(
            source,
            'MALBUT_AGENT_ENABLE_SCRIPTED_SPEECH',
            False,
        )
        raw_auth_token = source.get('MALBUT_AGENT_AUTH_TOKEN', '')
        auth_token = (
            raw_auth_token
            if enable_scripted_speech
            else raw_auth_token.strip()
        )

        default_database = str(
            Path.home()
            / '.local'
            / 'share'
            / 'malbut-agent'
            / 'memory.sqlite3'
        )
        return cls(
            provider=provider,
            host=source.get(
                'MALBUT_AGENT_HOST',
                '127.0.0.1',
            ).strip(),
            port=_env_int(
                source,
                'MALBUT_AGENT_PORT',
                8765,
                1,
                65535,
            ),
            database_path=source.get(
                'MALBUT_AGENT_DB',
                default_database,
            ).strip(),
            user_id=source.get(
                'MALBUT_AGENT_USER_ID',
                'local-user',
            ).strip(),
            memory_limit=_env_int(
                source,
                'MALBUT_AGENT_MEMORY_LIMIT',
                DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS,
                1,
                10,
            ),
            conversation_ttl_seconds=_env_int(
                source,
                'MALBUT_AGENT_CONVERSATION_TTL_SECONDS',
                1800,
                60,
                2592000,
            ),
            conversation_history_limit=_env_int(
                source,
                'MALBUT_AGENT_CONVERSATION_HISTORY_LIMIT',
                10,
                10,
                50,
            ),
            conversation_summary_max_chars=_env_int(
                source,
                'MALBUT_AGENT_CONVERSATION_SUMMARY_MAX_CHARS',
                2000,
                256,
                8000,
            ),
            max_model_input_chars=_env_int(
                source,
                'MALBUT_AGENT_MAX_MODEL_INPUT_CHARS',
                20000,
                4096,
                1000000,
            ),
            max_conversation_sessions=_env_int(
                source,
                'MALBUT_AGENT_MAX_CONVERSATION_SESSIONS',
                100,
                1,
                1000,
            ),
            max_conversation_turns=_env_int(
                source,
                'MALBUT_AGENT_MAX_CONVERSATION_TURNS',
                1000,
                10,
                10000,
            ),
            max_request_bytes=_env_int(
                source,
                'MALBUT_AGENT_MAX_REQUEST_BYTES',
                65536,
                1024,
                1048576,
            ),
            max_concurrent_requests=_env_int(
                source,
                'MALBUT_AGENT_MAX_CONCURRENT_REQUESTS',
                8,
                1,
                64,
            ),
            requests_per_minute=_env_int(
                source,
                'MALBUT_AGENT_REQUESTS_PER_MINUTE',
                60,
                1,
                10000,
            ),
            failed_auth_attempts_per_minute=_env_int(
                source,
                'MALBUT_AGENT_FAILED_AUTH_ATTEMPTS_PER_MINUTE',
                DEFAULT_FAILED_AUTH_ATTEMPTS_PER_MINUTE,
                1,
                MAX_FAILED_AUTH_ATTEMPTS_PER_MINUTE,
            ),
            socket_timeout_seconds=_env_int(
                source,
                'MALBUT_AGENT_SOCKET_TIMEOUT_SECONDS',
                10,
                1,
                120,
            ),
            request_timeout_seconds=_env_int(
                source,
                'MALBUT_AGENT_TIMEOUT_SECONDS',
                DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS,
                1,
                120,
            ),
            provider_total_timeout_seconds=_env_int(
                source,
                'MALBUT_AGENT_PROVIDER_TOTAL_TIMEOUT_SECONDS',
                DEFAULT_PROVIDER_TOTAL_TIMEOUT_SECONDS,
                1,
                300,
            ),
            provider_max_retries=_env_int(
                source,
                'MALBUT_AGENT_PROVIDER_MAX_RETRIES',
                0,
                0,
                3,
            ),
            provider_retry_base_delay_ms=_env_int(
                source,
                'MALBUT_AGENT_PROVIDER_RETRY_BASE_DELAY_MS',
                250,
                0,
                5000,
            ),
            provider_retry_max_delay_ms=_env_int(
                source,
                'MALBUT_AGENT_PROVIDER_RETRY_MAX_DELAY_MS',
                1000,
                0,
                10000,
            ),
            provider_failure_threshold=_env_int(
                source,
                'MALBUT_AGENT_PROVIDER_FAILURE_THRESHOLD',
                2,
                1,
                20,
            ),
            provider_recovery_timeout_seconds=_env_int(
                source,
                'MALBUT_AGENT_PROVIDER_RECOVERY_TIMEOUT_SECONDS',
                30,
                1,
                3600,
            ),
            openai_api_key=source.get(
                'OPENAI_API_KEY',
                '',
            ).strip(),
            openai_model=source.get(
                'OPENAI_MODEL',
                DEFAULT_OPENAI_MODEL,
            ).strip(),
            openai_fallback_model=source.get(
                'OPENAI_FALLBACK_MODEL',
                '',
            ).strip(),
            openai_base_url=source.get(
                'OPENAI_BASE_URL',
                OFFICIAL_OPENAI_BASE_URL,
            ).strip(),
            openai_reasoning_effort=source.get(
                'OPENAI_REASONING_EFFORT',
                'none',
            ).strip().lower(),
            openai_max_output_tokens=_env_int(
                source,
                'OPENAI_MAX_OUTPUT_TOKENS',
                500,
                64,
                4096,
            ),
            auth_token=auth_token,
            tool_mode=source.get(
                'MALBUT_AGENT_TOOL_MODE',
                'proposal',
            ).strip().lower(),
            enable_scripted_speech=enable_scripted_speech,
            homecam_origin=source.get(
                'MALBUT_HOMECAM_ORIGIN',
                '',
            ).strip(),
            homecam_agent_token=source.get(
                'MALBUT_HOMECAM_AGENT_TOKEN',
                '',
            ).strip(),
            homecam_signing_secret=source.get(
                'MALBUT_HOMECAM_SIGNING_SECRET',
                '',
            ).strip(),
            homecam_principal_subject_digest=source.get(
                'MALBUT_HOMECAM_PRINCIPAL_SUBJECT_DIGEST',
                '',
            ).strip(),
            homecam_device_id=source.get(
                'MALBUT_HOMECAM_DEVICE_ID',
                '',
            ).strip(),
            homecam_timeout_seconds=_env_int(
                source,
                'MALBUT_HOMECAM_TIMEOUT_SECONDS',
                3,
                1,
                10,
            ),
            robot_state_socket_path=source.get(
                'MALBUT_ROBOT_STATE_SOCKET_PATH',
                '',
            ).strip(),
            robot_state_expected_uid=_env_optional_int(
                source,
                'MALBUT_ROBOT_STATE_EXPECTED_UID',
                0,
                (1 << 31) - 1,
            ),
            robot_state_device_id=source.get(
                'MALBUT_ROBOT_STATE_DEVICE_ID',
                '',
            ).strip(),
            robot_state_timeout_seconds=_env_int(
                source,
                'MALBUT_ROBOT_STATE_TIMEOUT_SECONDS',
                2,
                1,
                5,
            ),
            monitorable_rooms=_env_monitorable_rooms(source),
            enable_gazebo_simulation_execution=_env_bool(
                source,
                'MALBUT_AGENT_ENABLE_GAZEBO_SIMULATION_EXECUTION',
                False,
            ),
            gazebo_simulation_authority_secret=source.get(
                'MALBUT_AGENT_GAZEBO_SIMULATION_AUTHORITY_SECRET',
                '',
            ),
            gazebo_prepare_socket_path=source.get(
                'MALBUT_AGENT_GAZEBO_PREPARE_SOCKET_PATH',
                '',
            ).strip(),
            gazebo_prepare_expected_uid=_env_optional_int(
                source,
                'MALBUT_AGENT_GAZEBO_PREPARE_EXPECTED_UID',
                0,
                (1 << 31) - 1,
            ),
            gazebo_prepare_timeout_seconds=_env_int(
                source,
                'MALBUT_AGENT_GAZEBO_PREPARE_TIMEOUT_SECONDS',
                2,
                1,
                MAX_GAZEBO_PREPARE_TIMEOUT_SECONDS,
            ),
            gazebo_prepare_lease_seconds=_env_int(
                source,
                'MALBUT_AGENT_GAZEBO_PREPARE_LEASE_SECONDS',
                30,
                MIN_GAZEBO_PREPARE_LEASE_SECONDS,
                MAX_GAZEBO_PREPARE_LEASE_SECONDS,
            ),
        )

    def validate_for_server(self) -> None:
        """Reject unsafe binds and incomplete live-provider settings."""
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError('MALBUT_AGENT_PROVIDER is unsupported')
        if self.tool_mode not in SUPPORTED_TOOL_MODES:
            raise ValueError('MALBUT_AGENT_TOOL_MODE is unsupported')
        if self.host not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError(
                'The MVP server is loopback-only; use an authenticated '
                'TLS proxy for remote access'
            )
        if not self.database_path:
            raise ValueError('MALBUT_AGENT_DB must not be empty')
        if not self.user_id or len(self.user_id) > 128:
            raise ValueError('MALBUT_AGENT_USER_ID is invalid')
        if not isinstance(self.enable_scripted_speech, bool):
            raise ValueError(
                'MALBUT_AGENT_ENABLE_SCRIPTED_SPEECH must be a boolean'
            )
        if self.enable_scripted_speech and not self.auth_token:
            raise ValueError(
                'scripted speech requires MALBUT_AGENT_AUTH_TOKEN'
            )
        if self.enable_scripted_speech:
            validate_scripted_auth_token(self.auth_token)
        elif self.auth_token and (
            not isinstance(self.auth_token, str)
            or not self.auth_token.isascii()
        ):
            raise ValueError(
                'MALBUT_AGENT_AUTH_TOKEN must contain ASCII only'
            )
        if (
            isinstance(self.failed_auth_attempts_per_minute, bool)
            or not isinstance(
                self.failed_auth_attempts_per_minute,
                int,
            )
            or self.failed_auth_attempts_per_minute < 1
            or self.failed_auth_attempts_per_minute
            > MAX_FAILED_AUTH_ATTEMPTS_PER_MINUTE
        ):
            raise ValueError(
                'MALBUT_AGENT_FAILED_AUTH_ATTEMPTS_PER_MINUTE is invalid'
            )
        if (
            self.provider_retry_max_delay_ms
            < self.provider_retry_base_delay_ms
        ):
            raise ValueError(
                'retry max delay must be at least the base delay'
            )
        if (
            self.provider_total_timeout_seconds
            < self.request_timeout_seconds
        ):
            raise ValueError(
                'provider total timeout must be at least one request '
                'timeout'
            )
        homecam_values = (
            self.homecam_origin,
            self.homecam_agent_token,
            self.homecam_signing_secret,
            self.homecam_principal_subject_digest,
            self.homecam_device_id,
        )
        if any(homecam_values) and not all(homecam_values):
            raise ValueError(
                'all MALBUT_HOMECAM_* semantic settings must be '
                'configured together'
            )
        if all(homecam_values):
            HomecamSemanticConfig(
                origin=self.homecam_origin,
                service_token=self.homecam_agent_token,
                envelope_signing_secret=self.homecam_signing_secret,
                agent_user_id=self.user_id,
                principal_subject_digest=(
                    self.homecam_principal_subject_digest
                ),
                device_id=self.homecam_device_id,
                timeout_seconds=self.homecam_timeout_seconds,
            )
        if not isinstance(self.robot_state_socket_path, str):
            raise ValueError(
                'MALBUT_ROBOT_STATE_SOCKET_PATH must be a string'
            )
        if not isinstance(self.robot_state_device_id, str):
            raise ValueError(
                'MALBUT_ROBOT_STATE_DEVICE_ID must be a string'
            )
        if (
            self.robot_state_expected_uid is not None
            and (
                isinstance(self.robot_state_expected_uid, bool)
                or not isinstance(self.robot_state_expected_uid, int)
                or self.robot_state_expected_uid < 0
                or self.robot_state_expected_uid > (1 << 31) - 1
            )
        ):
            raise ValueError(
                'MALBUT_ROBOT_STATE_EXPECTED_UID is invalid'
            )
        robot_state_configured = (
            bool(self.robot_state_socket_path)
            or self.robot_state_expected_uid is not None
            or bool(self.robot_state_device_id)
        )
        robot_state_complete = (
            bool(self.robot_state_socket_path)
            and self.robot_state_expected_uid is not None
            and bool(self.robot_state_device_id)
        )
        if robot_state_configured:
            if not robot_state_complete:
                raise ValueError(
                    'all MALBUT_ROBOT_STATE_* binding settings must be '
                    'configured together'
                )
            if not Path(self.robot_state_socket_path).is_absolute():
                raise ValueError(
                    'MALBUT_ROBOT_STATE_SOCKET_PATH must be absolute'
                )
        if (
            isinstance(self.robot_state_timeout_seconds, bool)
            or not isinstance(self.robot_state_timeout_seconds, int)
            or self.robot_state_timeout_seconds < 1
            or self.robot_state_timeout_seconds > 5
        ):
            raise ValueError(
                'MALBUT_ROBOT_STATE_TIMEOUT_SECONDS is invalid'
            )
        try:
            configured_rooms = _env_monitorable_rooms(
                {
                    'MALBUT_AGENT_MONITORABLE_ROOMS': ','.join(
                        self.monitorable_rooms
                    )
                }
            )
        except (TypeError, ValueError):
            raise ValueError(
                'MALBUT_AGENT_MONITORABLE_ROOMS is invalid'
            ) from None
        if configured_rooms != self.monitorable_rooms:
            raise ValueError(
                'MALBUT_AGENT_MONITORABLE_ROOMS is invalid'
            )
        if configured_rooms:
            if not all(homecam_values):
                raise ValueError(
                    'monitorable rooms require complete MALBUT_HOMECAM_* '
                    'semantic settings'
                )
            if not robot_state_complete:
                raise ValueError(
                    'monitorable rooms require complete '
                    'MALBUT_ROBOT_STATE_* settings'
                )
            if self.robot_state_device_id != self.homecam_device_id:
                raise ValueError(
                    'Homecam and RobotState device IDs must match'
                )
        if not isinstance(
            self.enable_gazebo_simulation_execution,
            bool,
        ):
            raise ValueError(
                'MALBUT_AGENT_ENABLE_GAZEBO_SIMULATION_EXECUTION '
                'must be a boolean'
            )
        if (
            isinstance(self.gazebo_prepare_timeout_seconds, bool)
            or not isinstance(
                self.gazebo_prepare_timeout_seconds,
                int,
            )
            or self.gazebo_prepare_timeout_seconds < 1
            or self.gazebo_prepare_timeout_seconds
            > MAX_GAZEBO_PREPARE_TIMEOUT_SECONDS
        ):
            raise ValueError(
                'MALBUT_AGENT_GAZEBO_PREPARE_TIMEOUT_SECONDS is invalid'
            )
        if (
            isinstance(self.gazebo_prepare_lease_seconds, bool)
            or not isinstance(self.gazebo_prepare_lease_seconds, int)
            or self.gazebo_prepare_lease_seconds
            < MIN_GAZEBO_PREPARE_LEASE_SECONDS
            or self.gazebo_prepare_lease_seconds
            > MAX_GAZEBO_PREPARE_LEASE_SECONDS
        ):
            raise ValueError(
                'MALBUT_AGENT_GAZEBO_PREPARE_LEASE_SECONDS is invalid'
            )
        if type(self.gazebo_simulation_authority_secret) is not str:
            raise ValueError(
                'MALBUT_AGENT_GAZEBO_SIMULATION_AUTHORITY_SECRET '
                'must be a string'
            )
        if type(self.gazebo_prepare_socket_path) is not str:
            raise ValueError(
                'MALBUT_AGENT_GAZEBO_PREPARE_SOCKET_PATH '
                'must be a string'
            )
        if (
            self.gazebo_prepare_expected_uid is not None
            and (
                isinstance(self.gazebo_prepare_expected_uid, bool)
                or not isinstance(
                    self.gazebo_prepare_expected_uid,
                    int,
                )
                or self.gazebo_prepare_expected_uid < 0
                or self.gazebo_prepare_expected_uid > (1 << 31) - 1
            )
        ):
            raise ValueError(
                'MALBUT_AGENT_GAZEBO_PREPARE_EXPECTED_UID is invalid'
            )
        gazebo_binding_configured = (
            bool(self.gazebo_simulation_authority_secret)
            or bool(self.gazebo_prepare_socket_path)
            or self.gazebo_prepare_expected_uid is not None
        )
        if (
            not self.enable_gazebo_simulation_execution
            and gazebo_binding_configured
        ):
            raise ValueError(
                'Gazebo simulation execution bindings require explicit '
                'enablement'
            )
        if self.enable_gazebo_simulation_execution:
            if self.tool_mode != 'simulation':
                raise ValueError(
                    'Gazebo simulation execution requires '
                    'MALBUT_AGENT_TOOL_MODE=simulation'
                )
            if self.database_path == ':memory:' or not Path(
                self.database_path
            ).expanduser().is_absolute():
                raise ValueError(
                    'Gazebo simulation execution requires an absolute '
                    'file-backed MALBUT_AGENT_DB'
                )
            try:
                validate_scripted_auth_token(self.auth_token)
            except ValueError:
                raise ValueError(
                    'Gazebo simulation execution requires a strong '
                    'MALBUT_AGENT_AUTH_TOKEN'
                ) from None
            secret = self.gazebo_simulation_authority_secret
            if (
                type(secret) is not str
                or len(secret) < MIN_GAZEBO_AUTHORITY_SECRET_LENGTH
                or len(secret) > MAX_GAZEBO_AUTHORITY_SECRET_LENGTH
                or not secret.isascii()
                or any(
                    ord(character) < 33 or ord(character) > 126
                    for character in secret
                )
            ):
                raise ValueError(
                    'MALBUT_AGENT_GAZEBO_SIMULATION_AUTHORITY_SECRET '
                    'is invalid'
                )
            socket_path = self.gazebo_prepare_socket_path
            if (
                type(socket_path) is not str
                or not socket_path
                or '\x00' in socket_path
                or not Path(socket_path).is_absolute()
                or os.path.normpath(socket_path) != socket_path
                or Path(socket_path).name == ''
            ):
                raise ValueError(
                    'MALBUT_AGENT_GAZEBO_PREPARE_SOCKET_PATH is invalid'
                )
            if (
                isinstance(self.gazebo_prepare_expected_uid, bool)
                or not isinstance(
                    self.gazebo_prepare_expected_uid,
                    int,
                )
                or self.gazebo_prepare_expected_uid < 0
                or self.gazebo_prepare_expected_uid > (1 << 31) - 1
            ):
                raise ValueError(
                    'MALBUT_AGENT_GAZEBO_PREPARE_EXPECTED_UID is invalid'
                )
            if not configured_rooms:
                raise ValueError(
                    'Gazebo simulation execution requires at least one '
                    'monitorable room'
                )
            if not all(homecam_values) or not robot_state_complete:
                raise ValueError(
                    'Gazebo simulation execution requires complete '
                    'Homecam and RobotState bindings'
                )
            if self.homecam_device_id != self.robot_state_device_id:
                raise ValueError(
                    'Gazebo simulation execution device bindings differ'
                )
        if self.provider == 'mock':
            return
        if not self.auth_token:
            raise ValueError(
                'OpenAI mode requires MALBUT_AGENT_AUTH_TOKEN'
            )
        if not self.openai_api_key:
            raise ValueError('OPENAI_API_KEY is required')
        if not _valid_model_id(self.openai_model):
            raise ValueError('OPENAI_MODEL is invalid')
        if self.openai_fallback_model:
            if not _valid_model_id(self.openai_fallback_model):
                raise ValueError('OPENAI_FALLBACK_MODEL is invalid')
            if self.openai_fallback_model == self.openai_model:
                raise ValueError(
                    'OPENAI_FALLBACK_MODEL must differ from OPENAI_MODEL'
                )
        if not is_official_openai_base_url(self.openai_base_url):
            raise ValueError(
                'OPENAI_BASE_URL must be the official OpenAI API origin'
            )
        if self.openai_reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                'OPENAI_REASONING_EFFORT is unsupported'
            )
