"""Bind saved observation information to its capture without retaining visual features."""

import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def commit_receipt(connection, identity, metadata, analysis):
    """Commit checksums with the analysis; never store pixels or appearance vectors."""
    connection.execute('''CREATE TABLE IF NOT EXISTS patrol_observation_evidence (
        observation_id TEXT PRIMARY KEY, metadata_sha256 TEXT NOT NULL,
        analysis_sha256 TEXT NOT NULL)''')
    connection.execute('INSERT OR REPLACE INTO patrol_observation_evidence VALUES (?,?,?)',
                       (identity, digest(metadata), digest(analysis)))


def receipt(connection, row):
    """Detect changed records after source release, without requiring an old photo."""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='patrol_observation_evidence'").fetchone()
    if not exists:
        return False
    saved = connection.execute('SELECT * FROM patrol_observation_evidence WHERE observation_id=?',
                               (row['id'],)).fetchone()
    if saved is None:
        return False
    if (saved['metadata_sha256'] != digest(json.loads(row['metadata']))
            or saved['analysis_sha256'] != digest(json.loads(row['analysis']))):
        raise ValueError('observation information changed after commit')
    return True
