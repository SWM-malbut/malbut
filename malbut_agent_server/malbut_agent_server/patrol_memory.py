"""Durable photo observations, scoped to the configured robot owner."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time


DEFAULT_DIRECTORY = '~/.local/share/malbut/patrol-observations'
_ID = re.compile(r'[a-f0-9]{32}')
_DIGEST = re.compile(r'[a-f0-9]{64}')


def observation_directory():
    """Use the same robot-local evidence folder as the patrol process."""
    return Path(os.environ.get('MALBUT_PATROL_OBSERVATION_DIR', DEFAULT_DIRECTORY)).expanduser()


def timestamp(value):
    """Require a timezone so past observations cannot silently shift in time."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('invalid observation time')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('observation time requires timezone')
    return parsed.timestamp()


def read_capture(path):
    """Validate a committed manifest and its exact local JPEG before inference."""
    if path.is_symlink() or path.stat().st_size > 16384 or not _ID.fullmatch(path.stem):
        raise ValueError('invalid capture manifest')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') not in (1, 2):
        raise ValueError('invalid capture version')
    for key, pattern in [('observation_id', _ID), ('patrol_id', _ID),
                         ('map_id', _DIGEST), ('image_sha256', _DIGEST)]:
        if not isinstance(data.get(key), str) or not pattern.fullmatch(data[key]):
            raise ValueError('invalid capture identity')
    if data['observation_id'] != path.stem:
        raise ValueError('capture identity mismatch')
    timestamp(data.get('captured_at'))
    pose = data.get('camera_pose')
    if (not isinstance(pose, dict) or set(pose) != {'x', 'y', 'yaw'}
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in pose.values())):
        raise ValueError('invalid capture pose')
    rooms = data.get('capture_rooms')
    if (not isinstance(rooms, list) or len(rooms) > 16
            or any(not isinstance(room, str) or not room.strip() or len(room) > 120
                   for room in rooms)):
        raise ValueError('invalid capture rooms')
    for key in ('map_frame', 'room_map', 'camera_frame'):
        if not isinstance(data.get(key), str) or len(data[key]) > 256:
            raise ValueError('invalid capture frame')
    if type(data.get('simulated')) is not bool:
        raise ValueError('invalid capture clock')
    if 'camera_hfov_rad' in data:
        fov = data['camera_hfov_rad']
        if type(fov) not in (int, float) or not math.isfinite(fov) or not 0 < fov < math.pi:
            raise ValueError('invalid camera field of view')
    if 'analysis_batch' in data and data['analysis_batch'] != 'patrol_mosaic_v1':
        raise ValueError('unsupported capture batch policy')
    photo = path.with_suffix('.jpg')
    if photo.is_symlink() or not 0 < photo.stat().st_size <= 4 * 1024 * 1024:
        raise ValueError('invalid capture photo')
    image = photo.read_bytes()
    if (not image.startswith(b'\xff\xd8') or not image.endswith(b'\xff\xd9')
            or hashlib.sha256(image).hexdigest() != data['image_sha256']):
        raise ValueError('capture photo integrity check failed')
    from malbut_agent_server.patrol_spatial import read_spatial
    read_spatial(path, data)
    from malbut_agent_server.patrol_lidar import read_lidar
    read_lidar(path, data)
    return data, image


def validate_analysis(value, *, localized=False):
    """Accept bounded observations only, never arbitrary model-owned metadata."""
    if not isinstance(value, dict) or set(value) != {'summary', 'objects', 'uncertainties'}:
        raise ValueError('invalid observation analysis')
    if not isinstance(value['summary'], str) or not 1 <= len(value['summary']) <= 600:
        raise ValueError('invalid observation summary')
    if not isinstance(value['objects'], list) or len(value['objects']) > 20:
        raise ValueError('invalid observed objects')
    for item in value['objects']:
        required = {'name', 'location', 'confidence'}
        optional = {'image_bbox', 'kind', 'image_anchors'}
        if localized:
            optional.add('spatial')
        if (not isinstance(item, dict) or not required <= set(item)
                or not set(item) <= required | optional
                or any(not isinstance(item[k], str) or not 1 <= len(item[k]) <= 120
                       for k in ('name', 'location'))
                or item['confidence'] not in ('high', 'uncertain')):
            raise ValueError('invalid observed object')
        bbox = item.get('image_bbox')
        if bbox is not None and (
                not isinstance(bbox, list) or len(bbox) != 4
                or any(type(v) is not int or not 0 <= v <= 1000 for v in bbox)
                or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
            raise ValueError('invalid object bounding box')
        if 'kind' in item and item['kind'] not in ('person', 'box', 'object'):
            raise ValueError('invalid object kind')
        anchors = item.get('image_anchors', [])
        if (not isinstance(anchors, list) or len(anchors) > 3
                or any(not isinstance(p, list) or len(p) != 2
                       or any(type(v) is not int or not 0 <= v <= 1000 for v in p)
                       for p in anchors)):
            raise ValueError('invalid object anchors')
        if 'spatial' in item:
            spatial = item['spatial']
            if (not isinstance(spatial, dict)
                    or spatial.get('status') not in ('located', 'unknown')
                    or spatial.get('method') != 'registered_rgbd'
                    or len(json.dumps(spatial)) > 1000):
                raise ValueError('invalid localized object')
    if (not isinstance(value['uncertainties'], list) or len(value['uncertainties']) > 5
            or any(not isinstance(item, str) or not 1 <= len(item) <= 160
                   for item in value['uncertainties'])):
        raise ValueError('invalid observation uncertainty')
    return value


class PatrolMemoryStore:
    """Keep image provenance and processing state across restarts and sessions."""

    def __init__(self, directory, owner_id, *, simulation=False):
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError('patrol observation owner required')
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'observations.sqlite3'
        self.owner_id = owner_id
        self.simulation = bool(simulation)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner'"
            ).fetchone()
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS owner (
                    id INTEGER PRIMARY KEY CHECK(id=1), user_id TEXT);
                CREATE TABLE IF NOT EXISTS dataset (
                    id INTEGER PRIMARY KEY CHECK(id=1), simulated INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, captured_at REAL NOT NULL,
                    metadata TEXT NOT NULL, analysis TEXT, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    error TEXT, model TEXT, analyzed_at REAL);
                CREATE INDEX IF NOT EXISTS observations_owner_time
                    ON observations(user_id, captured_at DESC);
            ''')
            # Legacy databases always belong to the real robot. A simulation
            # must explicitly create a separate database, never repurpose one.
            connection.execute('INSERT OR IGNORE INTO dataset VALUES (1, ?)',
                               (0 if existing else int(self.simulation),))
            mode = connection.execute(
                'SELECT simulated FROM dataset WHERE id=1').fetchone()[0]
            if bool(mode) != self.simulation:
                raise ValueError('simulation requires a separate observation directory')
            connection.execute('INSERT OR IGNORE INTO owner VALUES (1, ?)', (owner_id,))
            saved_owner = connection.execute('SELECT user_id FROM owner WHERE id=1').fetchone()[0]
            if saved_owner != owner_id:
                raise ValueError('observation directory belongs to another robot owner')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        """Use short transactions; never hold a DB lock during inference."""
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def ingest(self):
        """Import complete evidence once, keeping real and simulated data separate."""
        folder = self.directory / 'captures'
        imported = 0
        for path in sorted(folder.glob('*.json')):
            if not _ID.fullmatch(path.stem):
                continue
            with self.connect() as connection:
                existing = connection.execute(
                    'SELECT 1 FROM observations WHERE id=?', (path.stem,)).fetchone()
                if existing:
                    continue
            try:
                metadata, _ = read_capture(path)
                status = 'pending'
                if metadata['simulated'] != self.simulation:
                    status = 'simulated' if metadata['simulated'] else 'real_excluded'
                captured_at = timestamp(metadata['captured_at'])
                error = None
            except (ValueError, OSError, TypeError, KeyError):
                metadata, captured_at, status, error = {}, 0, 'failed', 'invalid_capture'
            with self.connect() as connection:
                connection.execute('''INSERT OR IGNORE INTO observations
                    (id,user_id,captured_at,metadata,status,error) VALUES (?,?,?,?,?,?)''',
                                   (path.stem, self.owner_id, captured_at,
                                    json.dumps(metadata, ensure_ascii=False), status, error))
            imported += 1
            if imported >= 64:
                break
        return imported

    def next_pending(self):
        """Claim one attempt durably; a crash retries only after its lease expires."""
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute('''SELECT * FROM observations WHERE user_id=?
                AND status='pending' AND retry_at <= ? ORDER BY captured_at''',
                                      (self.owner_id, time.time())).fetchall()
            row = next((row for row in rows
                        if 'analysis_batch' not in json.loads(row['metadata'])), None)
            if row is None:
                return None
            connection.execute('UPDATE observations SET retry_at=? WHERE id=?',
                               (time.time() + 90, row['id']))
            return dict(row)

    def complete(self, identifier, analysis, model):
        validate_analysis(analysis, localized=True)
        with self.connect() as connection:
            connection.execute('''UPDATE observations SET status='ready', analysis=?,
                model=?, analyzed_at=?, error=NULL WHERE id=? AND user_id=?''',
                               (json.dumps(analysis, ensure_ascii=False), model, time.time(),
                                identifier, self.owner_id))

    def fail(self, identifier, *, missing_key=False):
        """Keep photos when unavailable; limit paid inference retries to three."""
        with self.connect() as connection:
            if missing_key:
                connection.execute('''UPDATE observations SET retry_at=?, error='key_unavailable'
                    WHERE id=? AND user_id=?''', (time.time() + 30, identifier, self.owner_id))
            else:
                connection.execute('''UPDATE observations SET attempts=attempts+1,
                    status=CASE WHEN attempts>=2 THEN 'failed' ELSE 'pending' END,
                    retry_at=?, error='analysis_failed' WHERE id=? AND user_id=?''',
                                   (time.time() + 60, identifier, self.owner_id))

    def search(self, user_id, arguments, *, include_recent_context=False):
        """Search past evidence without ever treating it as a live camera view."""
        from malbut_agent_server.tools import validate_tool_arguments

        validate_tool_arguments('search_patrol_observations', arguments)
        if user_id != self.owner_id:
            raise PermissionError('patrol observation owner mismatch')
        terms = (arguments['query'] or '').casefold().split()
        clauses = ["user_id=?", "status='ready'"]
        values = [user_id]
        for key, operator in (('since', '>='), ('until', '<')):
            if arguments[key] is not None:
                clauses.append('captured_at ' + operator + ' ?')
                values.append(timestamp(arguments[key]))
        recent_clauses, recent_values = list(clauses), list(values)
        for term in terms:
            clauses.append('instr(lower(analysis || metadata), ?) > 0')
            values.append(term)
        if (arguments['since'] and arguments['until']
                and timestamp(arguments['since']) >= timestamp(arguments['until'])):
            raise ValueError('invalid observation time range')
        with self.connect() as connection:
            counts = dict(connection.execute('''SELECT status, count(*) FROM observations
                WHERE user_id=? GROUP BY status''', (user_id,)).fetchall())
            rows = connection.execute('SELECT * FROM observations WHERE ' + ' AND '.join(clauses)
                                      + ' ORDER BY captured_at DESC, id DESC LIMIT ?',
                                      values + [arguments['limit']]).fetchall()
            matching_ids = {row['id'] for row in rows}
            history = self._confirmed_history(connection, clauses, values, terms)
            if include_recent_context and terms:
                recent = connection.execute(
                    'SELECT * FROM observations WHERE ' + ' AND '.join(recent_clauses)
                    + ' ORDER BY captured_at DESC, id DESC LIMIT ?',
                    recent_values + [arguments['limit']]).fetchall()
                if rows and arguments['limit'] > 1:
                    # Reserve keyword evidence while allowing newer/differently named objects.
                    selected = rows[:max(1, arguments['limit'] - 2)]
                    identities = {row['id'] for row in selected}
                    for row in recent + rows:
                        if row['id'] not in identities and len(selected) < arguments['limit']:
                            selected.append(row)
                            identities.add(row['id'])
                else:
                    selected = recent
                rows = sorted(selected, key=lambda row: (row['captured_at'], row['id']),
                              reverse=True)
        result = {'observations': [], 'counts': counts,
                  'environment': 'simulation' if self.simulation else 'real',
                  'queried_at': datetime.now(timezone.utc).isoformat(),
                  'ordering': 'captured_at_desc',
                  'object_identity': 'untracked_visual_descriptions',
                  'time_semantics': 'past_photo_observations_not_current_state'}
        if history:
            # Reserve this compact evidence before trimming verbose scene records.
            # It must never replace the latest unknown observation or claim identity.
            result['last_confirmed_observations'] = history
        if include_recent_context and terms:
            result['matching_semantics'] = 'keyword_evidence_and_unconfirmed_recent_context'
        for row in rows:
            metadata = json.loads(row['metadata'])
            # Do not return assertions whose original photo was removed or changed.
            try:
                current, _ = read_capture(self.directory / 'captures' / (row['id'] + '.json'))
                if current != metadata:
                    continue
            except (ValueError, OSError, TypeError, KeyError):
                continue
            item = {key: metadata[key] for key in (
                'observation_id', 'patrol_id', 'captured_at',
                'map_id', 'capture_rooms', 'camera_pose', 'simulated')}
            item['analysis_model'] = row['model']
            if include_recent_context and terms:
                item['match_kind'] = 'keyword' if row['id'] in matching_ids else 'recent_context'
            item['capture_regions'] = metadata.get('capture_regions', [])[:4]
            item.update(json.loads(row['analysis']))
            item['capture_rooms'] = item['capture_rooms'][:4]
            objects = item['objects']
            # Navigation-only aiming hints must not become asserted object locations in answers.
            for obj in objects:
                if isinstance(obj.get('spatial'), dict):
                    obj['spatial'].pop('reobserve_hint', None)
            # Keep matching objects first when one scene contains many items.
            objects.sort(key=lambda obj: -sum(
                term in json.dumps(obj, ensure_ascii=False).casefold() for term in terms))
            item['objects'] = objects[:5]
            item['objects_truncated'] = len(objects) > 5
            item['uncertainties'] = item['uncertainties'][:2]
            item['photo'] = 'captures/' + row['id'] + '.jpg'
            result['observations'].append(item)
            while len(json.dumps(result, ensure_ascii=False)) > 4000 and len(item['objects']) > 1:
                item['objects'].pop()
                item['objects_truncated'] = True
            # Leave room for the existing conversation-memory wrapper.
            if len(json.dumps(result, ensure_ascii=False)) > 4000:
                result['observations'].pop()
                break
        return result

    def _confirmed_history(self, connection, clauses, values, terms):
        """Find bounded, verified historical room evidence independently of hit limit."""
        if not terms:
            return []
        candidates = connection.execute(
            'SELECT * FROM observations WHERE ' + ' AND '.join(clauses)
            + ' AND instr(analysis, ?) > 0 ORDER BY captured_at DESC, id DESC LIMIT 200',
            values + ['"located"']).fetchall()
        result, newest, seen = [], {}, set()
        for row in candidates:
            metadata = json.loads(row['metadata'])
            matches = []
            for obj in json.loads(row['analysis'])['objects']:
                spatial = obj.get('spatial', {})
                region = spatial.get('region')
                if (spatial.get('status') != 'located' or not region
                        or obj.get('confidence') != 'high'):
                    continue
                text = (obj['name'] + ' ' + region['name']).casefold()
                name = obj['name'].casefold()
                if (row['captured_at'] < newest.get(name, -math.inf)
                        or not all(term in text for term in terms)):
                    continue
                matches.append((name, obj, region))
            if not matches:
                continue
            try:
                current, _ = read_capture(self.directory/'captures'/(row['id']+'.json'))
                if current != metadata:
                    continue
            except (ValueError, OSError, TypeError, KeyError):
                continue
            for name, obj, region in matches:
                identity = (name, metadata['captured_at'], metadata['map_id'], region['id'])
                if identity in seen:
                    continue
                item = {'name': obj['name'], 'kind': obj.get('kind', 'object'),
                        'captured_at': metadata['captured_at'], 'region': region,
                        'observation_id': row['id'], 'map_id': metadata['map_id'],
                        'simulated': metadata['simulated']}
                if len(json.dumps(result+[item], ensure_ascii=False)) > 1400:
                    return result
                result.append(item)
                newest[name] = row['captured_at']
                seen.add(identity)
                if len(result) == 3:
                    return result
        return result
