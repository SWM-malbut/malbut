"""Command line entry point for the Malbut agent service."""

import argparse
import os
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

from malbut_agent_server.config import Settings, load_env_file
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.http_server import make_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Run the Malbut Mock, OpenAI, or isolated RAI sidecar '
            'agent service.'
        ),
    )
    parser.add_argument(
        '--env-file',
        default='.env.local',
        help='Load local KEY=VALUE settings without overwriting the shell.',
    )
    parser.add_argument(
        '--provider',
        choices=('mock', 'openai', 'rai-sidecar'),
        help=(
            'Select offline Mock, official OpenAI Responses API, or '
            'the explicitly configured isolated RAI sidecar.'
        ),
    )
    parser.add_argument(
        '--model',
        help='Override OPENAI_MODEL without accepting credentials on CLI.',
    )
    parser.add_argument(
        '--fallback-model',
        help='Optionally override OPENAI_FALLBACK_MODEL.',
    )
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--database')
    parser.add_argument(
        '--check',
        action='store_true',
        help='Validate configuration and exit without an API request.',
    )
    return parser


def server_main(argv: Optional[List[str]] = None) -> int:
    """Load configuration and run the loopback-only service."""
    args = _parser().parse_args(argv)
    load_env_file(Path(args.env_file).expanduser())
    settings = Settings.from_env(os.environ)
    overrides = {}
    if args.provider is not None:
        overrides['provider'] = args.provider
    if args.model is not None:
        overrides['openai_model'] = args.model
    if args.fallback_model is not None:
        overrides['openai_fallback_model'] = args.fallback_model
    if args.host is not None:
        overrides['host'] = args.host
    if args.port is not None:
        overrides['port'] = args.port
    if args.database is not None:
        overrides['database_path'] = args.database
    settings = replace(settings, **overrides)
    settings.validate_for_server()
    orchestrator = build_orchestrator(settings)
    if args.check:
        orchestrator.conversation_store.close()
        orchestrator.memory_store.close()
        print('configuration: ok')
        return 0

    server = make_server(
        settings.host,
        settings.port,
        orchestrator,
        max_request_bytes=settings.max_request_bytes,
        auth_token=settings.auth_token,
        allowed_user_id=settings.user_id,
        max_concurrent_requests=settings.max_concurrent_requests,
        requests_per_minute=settings.requests_per_minute,
        socket_timeout_seconds=settings.socket_timeout_seconds,
    )
    print(
        'Malbut agent server listening on '
        f'http://{settings.host}:{settings.port} '
        f'(provider={settings.provider})'
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        orchestrator.conversation_store.close()
        orchestrator.memory_store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(server_main())
