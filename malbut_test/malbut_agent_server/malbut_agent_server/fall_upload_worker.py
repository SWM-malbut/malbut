"""Upload persisted fall metadata; no VLM, audio playback or robot commands."""

import argparse
import os
from pathlib import Path
import stat
import sqlite3
import time

from malbut_agent_server.adapters.outbound.homecam_fall_events import (
    FallEventUploader, HomecamFallEventClient,
)
from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal


def _read_token(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
                or stat.S_IMODE(info.st_mode) & 0o027):
            raise ValueError('token file must be protected (0600 or 0640)')
        content = os.read(fd, 4097)
        if len(content) > 4096:
            raise ValueError('token file too large')
        return content.decode('utf-8').strip()
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--device-id', required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--allow-host', action='append', required=True)
    parser.add_argument('--token-file', default=os.environ.get('HOMECAM_DEVICE_TOKEN_FILE'))
    parser.add_argument('--execute', action='store_true', help='Actually upload stored metadata')
    parser.add_argument('--once', action='store_true', help='Process at most one pending event')
    parser.add_argument('--retry-auth-failed', action='store_true',
                        help='Explicitly requeue 401/403 records after repairing credentials')
    args = parser.parse_args(argv)
    if not args.journal.is_absolute():
        parser.error('--journal must be absolute')
    options = dict(base_url=args.base_url, device_id=args.device_id,
                   allowed_hosts=set(args.allow_host))
    try:
        # Dry-run validation does not read a credential, create a DB, or call HTTP.
        HomecamFallEventClient(device_token='validation-only', **options)
        if not args.execute:
            print('configuration: ok (no upload)')
            return 0
        if not args.token_file:
            parser.error('--token-file or HOMECAM_DEVICE_TOKEN_FILE is required for --execute')
        client = HomecamFallEventClient(device_token=_read_token(args.token_file), **options)
        journal = SqliteFallJournal(args.journal, device_id=args.device_id)
        try:
            if args.retry_auth_failed:
                journal.retry_auth_failed()
            uploader = FallEventUploader(journal, client)
            while True:
                processed = uploader.run_once()
                if args.once:
                    break
                if not processed:
                    time.sleep(1)
        finally:
            journal.close()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, sqlite3.Error):
        print('configuration or journal access failed; check protected paths and settings')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
