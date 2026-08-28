"""Environment-based settings with explicit OpenAI safety bounds."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional

from malbut_agent_server.endpoint_policy import (
    OFFICIAL_OPENAI_BASE_URL,
    is_official_openai_base_url,
)
from malbut_agent_server.providers.openai_responses import (
    REASONING_EFFORTS,
)
from malbut_agent_server.rai_sidecar_protocol import (
    MAX_MODEL_INPUT_LENGTH as RAI_MAX_MODEL_INPUT_LENGTH,
)


SUPPORTED_PROVIDERS = frozenset({'mock', 'openai', 'rai-sidecar'})
SUPPORTED_TOOL_MODES = frozenset({'proposal', 'simulation'})
DEFAULT_OPENAI_MODEL = 'gpt-5.6-terra'
DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS = 5
DEFAULT_PROVIDER_TOTAL_TIMEOUT_SECONDS = 11
DEFAULT_RAI_SIDECAR_TIMEOUT_SECONDS = 5


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


def _valid_model_id(value: str) -> bool:
    """Accept a bounded printable model identifier."""
    return (
        bool(value)
        and len(value) <= 128
        and value.isascii()
        and all(32 < ord(character) < 127 for character in value)
    )


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
    rai_sidecar_python: str = ''
    rai_sidecar_working_directory: str = ''
    rai_sidecar_timeout_seconds: int = (
        DEFAULT_RAI_SIDECAR_TIMEOUT_SECONDS
    )
    rai_model: str = ''

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
            'auth_token=<redacted>, '
            'rai_sidecar_python=<redacted>, '
            'rai_sidecar_working_directory=<redacted>, '
            'rai_sidecar_timeout_seconds='
            f'{self.rai_sidecar_timeout_seconds!r}, '
            f'rai_model={self.rai_model!r})'
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
            auth_token=source.get(
                'MALBUT_AGENT_AUTH_TOKEN',
                '',
            ).strip(),
            tool_mode=source.get(
                'MALBUT_AGENT_TOOL_MODE',
                'proposal',
            ).strip().lower(),
            rai_sidecar_python=source.get(
                'MALBUT_RAI_SIDECAR_PYTHON',
                '',
            ).strip(),
            rai_sidecar_working_directory=source.get(
                'MALBUT_RAI_SIDECAR_CWD',
                '',
            ).strip(),
            rai_sidecar_timeout_seconds=_env_int(
                source,
                'MALBUT_RAI_SIDECAR_TIMEOUT_SECONDS',
                DEFAULT_RAI_SIDECAR_TIMEOUT_SECONDS,
                1,
                120,
            ),
            rai_model=source.get(
                'MALBUT_RAI_MODEL',
                '',
            ).strip(),
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
        if self.auth_token and not self.auth_token.isascii():
            raise ValueError(
                'MALBUT_AGENT_AUTH_TOKEN must contain ASCII only'
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
        if self.provider == 'mock':
            return
        if not self.auth_token:
            raise ValueError(
                'Live provider mode requires MALBUT_AGENT_AUTH_TOKEN'
            )
        if self.provider == 'rai-sidecar':
            self.validate_rai_sidecar()
            return
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

    def validate_rai_sidecar(self) -> None:
        """Reject implicit process lookup and non-isolated RAI startup."""
        if self.provider != 'rai-sidecar':
            raise ValueError('RAI sidecar validation requires RAI mode')
        if not self.auth_token:
            raise ValueError(
                'RAI sidecar mode requires MALBUT_AGENT_AUTH_TOKEN'
            )
        if type(self.rai_sidecar_timeout_seconds) is not int or not (
            1 <= self.rai_sidecar_timeout_seconds <= 120
        ):
            raise ValueError(
                'MALBUT_RAI_SIDECAR_TIMEOUT_SECONDS must be between '
                '1 and 120'
            )
        executable = Path(self.rai_sidecar_python)
        if not executable.is_absolute() or not executable.is_file() or (
            not os.access(executable, os.X_OK)
        ):
            raise ValueError(
                'MALBUT_RAI_SIDECAR_PYTHON must be an absolute '
                'executable file'
            )
        venv_root = executable.parent.parent
        if executable.parent.name not in {'bin', 'Scripts'} or not (
            venv_root / 'pyvenv.cfg'
        ).is_file():
            raise ValueError(
                'MALBUT_RAI_SIDECAR_PYTHON must be a Python virtual '
                'environment interpreter'
            )
        directory = Path(self.rai_sidecar_working_directory)
        if not directory.is_absolute() or not directory.is_dir() or (
            directory.resolve() == Path('/')
        ):
            raise ValueError(
                'MALBUT_RAI_SIDECAR_CWD must be an isolated absolute '
                'directory'
            )
        try:
            directory.resolve().relative_to(venv_root.resolve())
        except ValueError:
            pass
        else:
            raise ValueError(
                'MALBUT_RAI_SIDECAR_CWD must be outside the Python '
                'virtual environment'
            )
        if not self.openai_api_key:
            raise ValueError(
                'RAI sidecar mode requires OPENAI_API_KEY'
            )
        if not _valid_model_id(self.rai_model):
            raise ValueError('MALBUT_RAI_MODEL is invalid')
        if self.max_model_input_chars > RAI_MAX_MODEL_INPUT_LENGTH:
            raise ValueError(
                'MALBUT_AGENT_MAX_MODEL_INPUT_CHARS exceeds the RAI '
                'sidecar protocol limit'
            )
