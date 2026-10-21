"""Sealed patrol batches: one vision request, atomic per-photo observations."""

import hashlib
import json
import re
import time

from malbut_agent_server.patrol_memory import validate_analysis


POLICY = 'patrol_mosaic_v1'
MAX_FRAMES = 24
OUTCOMES = {'completed', 'aborted', 'canceled', 'idle', 'shutdown', 'interrupted'}


def closed_patrol(store, patrol_id):
    """Read the producer's manifest; an active patrol has no complete manifest."""
    if not re.fullmatch('[a-f0-9]{32}', patrol_id):
        raise ValueError('invalid patrol batch identity')
    path = store.directory / 'runs' / (patrol_id + '.json')
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError('invalid patrol batch manifest')
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or data.get('version') != 1
            or data.get('patrol_id') != patrol_id
            or data.get('outcome') not in OUTCOMES
            or not isinstance(data.get('captures'), list)
            or not 1 <= len(data['captures']) <= MAX_FRAMES):
        raise ValueError('invalid closed patrol')
    seen = set()
    for item in data['captures']:
        if (not isinstance(item, dict) or set(item) != {'observation_id', 'manifest_sha256'}
                or not isinstance(item['observation_id'], str)
                or not re.fullmatch('[a-f0-9]{32}', item['observation_id'])
                or item['observation_id'] in seen
                or not isinstance(item['manifest_sha256'], str)
                or not re.fullmatch('[a-f0-9]{64}', item['manifest_sha256'])):
            raise ValueError('invalid batch frame')
        seen.add(item['observation_id'])
    return data


def claim_batch(store):
    """Lease all pending frames of one sealed run; never mix patrols or owners."""
    with store.connect() as connection:
        pending = connection.execute('''SELECT * FROM observations WHERE user_id=?
            AND status='pending' AND retry_at<=? ORDER BY captured_at''',
                                     (store.owner_id, time.time())).fetchall()
    checked = set()
    for candidate in pending:
        metadata = json.loads(candidate['metadata'])
        if metadata.get('analysis_batch') != POLICY:
            continue
        patrol_id = metadata['patrol_id']
        if patrol_id in checked:
            continue
        checked.add(patrol_id)
        try:
            manifest = closed_patrol(store, patrol_id)
        except (ValueError, OSError, TypeError, KeyError):
            store.fail(candidate['id'])
            continue
        if manifest is None:
            continue
        ids = [item['observation_id'] for item in manifest['captures']]
        if candidate['id'] not in ids:
            store.fail(candidate['id'])
            continue
        placeholders = ','.join('?' for _ in ids)
        with store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute(
                'SELECT * FROM observations WHERE user_id=? AND id IN (' + placeholders + ')',
                [store.owner_id] + ids).fetchall()
            if len(rows) != len(ids):  # Ingestion may still be catching up.
                continue
            if any(row['status'] not in {'pending', 'ready'} for row in rows):
                connection.execute("UPDATE observations SET status='failed', "
                                   "error='batch_source_unavailable' WHERE user_id=? "
                                   "AND status='pending' AND id IN (" + placeholders + ')',
                                   [store.owner_id] + ids)
                continue
            if any(row['status'] != 'pending' or row['retry_at'] > time.time() for row in rows):
                continue
            frames = {row['id']: dict(row) for row in rows}
            if any(json.loads(row['metadata']).get('patrol_id') != patrol_id
                   or json.loads(row['metadata']).get('analysis_batch') != POLICY for row in rows):
                raise ValueError('batch contains another patrol')
            connection.execute('UPDATE observations SET retry_at=? WHERE user_id=? AND id IN ('
                               + placeholders + ')', [time.time() + 120, store.owner_id] + ids)
        return manifest, [frames[identity] for identity in ids]
    return None


def verify_batch_files(store, manifest):
    """Bind each frame to the exact committed manifest, not just a matching ID."""
    for item in manifest['captures']:
        path = store.directory / 'captures' / (item['observation_id'] + '.json')
        if (path.is_symlink() or path.stat().st_size > 16384
                or hashlib.sha256(path.read_bytes()).hexdigest() != item['manifest_sha256']):
            raise ValueError('sealed patrol capture changed')


def complete_batch(store, manifest, results, model, usage, mosaic):
    """Commit every observation plus successful-call usage in one transaction."""
    expected = {item['observation_id'] for item in manifest['captures']}
    if set(results) != expected:
        raise ValueError('batch result identities differ')
    for analysis in results.values():
        validate_analysis(analysis, localized=True)
    measured = {name: value for name, value in usage.items()
                if name in {'input_tokens', 'output_tokens', 'total_tokens', 'cached_input_tokens',
                            'cache_write_tokens', 'cache_write_30m_tokens',
                            'cache_write_12h_tokens'}
                and type(value) is int and value >= 0}
    with store.connect() as connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS patrol_analysis_batches (
            patrol_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, model TEXT NOT NULL,
            observation_ids TEXT NOT NULL, usage TEXT NOT NULL, mosaic TEXT NOT NULL,
            analyzed_at REAL NOT NULL)''')
        connection.execute('''INSERT INTO patrol_analysis_batches VALUES (?,?,?,?,?,?,?)''',
                           (manifest['patrol_id'], store.owner_id, model,
                            json.dumps(sorted(expected)), json.dumps(measured),
                            json.dumps(mosaic, ensure_ascii=False), time.time()))
        for identity, analysis in results.items():
            cursor = connection.execute('''UPDATE observations SET status='ready', analysis=?,
                model=?, analyzed_at=?, error=NULL
                WHERE id=? AND user_id=? AND status='pending' ''',
                                        (json.dumps(analysis, ensure_ascii=False), model,
                                         time.time(), identity, store.owner_id))
            if cursor.rowcount != 1:
                raise ValueError('patrol batch state changed')
