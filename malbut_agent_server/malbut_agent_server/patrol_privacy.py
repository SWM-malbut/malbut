"""Explicitly migrate legacy patrol media to verified information-only memory.

Stop legacy patrol capture before running:
python -m malbut_agent_server.patrol_privacy /path/to/patrol-observations
The command irreversibly removes raw media; it does not run at startup.
"""

import argparse
import fcntl
import json
from pathlib import Path
import re
import sqlite3

from malbut_agent_server.patrol_evidence import receipt
from malbut_agent_server.patrol_memory import PatrolMemoryStore, read_capture, validate_analysis


def migrate(store):
    """Commit evidence before erasure, preserving observation and analysis times."""
    folders = [store.directory / name for name in ('captures', 'batches')]
    if any(folder.is_symlink() for folder in folders):
        raise ValueError('refuse symlinked patrol media directory')
    lock_path = store.directory / 'worker.lock'
    if lock_path.is_symlink():
        raise ValueError('refuse symlinked patrol worker lock')
    converted, rejected, deleted = 0, 0, 0
    with lock_path.open('a') as lock:
        # The existing analysis worker holds this same lock during a batch.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with store.connect() as connection:
            rows = connection.execute('SELECT * FROM observations WHERE user_id=?',
                                      (store.owner_id,)).fetchall()
        for row in rows:
            if row['status'] != 'ready':
                continue
            try:
                with store.connect() as connection:
                    if receipt(connection, row):
                        continue
                if not re.fullmatch('[a-f0-9]{32}', row['id']):
                    raise ValueError('invalid observation identity')
                path = store.directory / 'captures' / (row['id'] + '.json')
                metadata, image = read_capture(path)
                del image
                if metadata != json.loads(row['metadata']):
                    raise ValueError('legacy metadata changed')
                analysis = json.loads(row['analysis'])
                validate_analysis(analysis, localized=True)
            except (OSError, ValueError, TypeError, KeyError):
                with store.connect() as connection:
                    connection.execute("UPDATE observations SET status='failed', "
                                       "error='legacy_evidence_unverifiable' "
                                       "WHERE id=? AND user_id=?",
                                       (row['id'], store.owner_id))
                rejected += 1
                continue
            # A database/commit failure propagates before *any* file deletion.
            with store.connect() as connection:
                store.commit_evidence(connection, row['id'], analysis)
            converted += 1
        with store.connect() as connection:
            for row in rows:
                if row['status'] != 'pending':
                    continue
                try:
                    metadata = json.loads(row['metadata'])
                except (ValueError, TypeError):
                    metadata = {}
                if (not isinstance(metadata, dict)
                        or metadata.get('media_storage') != 'volatile_v1'):
                    connection.execute("UPDATE observations SET status='failed', "
                                       "error='raw_media_removed' WHERE id=? AND user_id=?",
                                       (row['id'], store.owner_id))
        # Leave manifests, run records and database intact. Do not recurse or
        # follow links: unlinking a media symlink removes only the link itself.
        suffixes = ('.jpg', '.jpeg', '.png', '.depth.npy', '.lidar.npy')
        for folder in folders:
            for path in folder.glob('*'):
                if any(path.name.endswith(suffix + ending)
                       for suffix in suffixes for ending in ('', '.tmp')):
                    path.unlink()
                    deleted += 1
    return {'converted': converted, 'unverifiable': rejected, 'raw_files_deleted': deleted}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    # Read an existing database only; a typo must not create a new memory store.
    uri = (directory / 'observations.sqlite3').as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        owner = db.execute('SELECT user_id FROM owner WHERE id=1').fetchone()[0]
        simulated = bool(db.execute('SELECT simulated FROM dataset WHERE id=1').fetchone()[0])
    print(json.dumps(migrate(PatrolMemoryStore(directory, owner, simulation=simulated))))


if __name__ == '__main__':
    main()
