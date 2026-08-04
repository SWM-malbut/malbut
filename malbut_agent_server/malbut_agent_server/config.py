"""Mock-only runtime settings for the local multi-turn MVP."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional


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


@dataclass(frozen=True)
class Settings:
    """Bounded settings for the offline Mock session server."""

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
    auth_token: str = ''

    def __repr__(self) -> str:
        """Never expose a configured bearer token in debug output."""
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
            'max_conversation_sessions='
            f'{self.max_conversation_sessions!r}, '
            'max_conversation_turns='
            f'{self.max_conversation_turns!r}, '
            'auth_token=<redacted>)'
        )

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> 'Settings':
        """Build settings while rejecting live providers in SWM25-70."""
        source = environ if environ is not None else os.environ
        provider = source.get(
            'MALBUT_AGENT_PROVIDER',
            'mock',
        ).strip().lower()
        if provider != 'mock':
            raise ValueError(
                'SWM25-70 supports only the offline mock provider'
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
                5,
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
            auth_token=source.get(
                'MALBUT_AGENT_AUTH_TOKEN',
                '',
            ).strip(),
        )

    def validate_for_server(self) -> None:
        """Keep the MVP local and reject unsafe identity settings."""
        if self.provider != 'mock':
            raise ValueError('Only the mock provider is available')
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
