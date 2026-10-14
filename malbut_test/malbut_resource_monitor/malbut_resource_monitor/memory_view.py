"""Read-only, bounded inspection of explicitly configured conversation databases.

No agent imports, migrations, recovery, consent changes or model calls occur here.
Missing trace rows are unknown, never reconstructed from temporal proximity.
"""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3


DEFAULT_MEMORY_DB = '~/.local/state/malbut/speech-dialogue.sqlite3'
DEFAULT_MEMORY_CONFIG = '~/.config/malbut/resource-viewer-memory.json'


def configured_databases(paths=None, config=None):
    """CLI overrides a persistent, locally configured database allowlist."""
    if paths is not None:
        return paths
    path = Path(config or DEFAULT_MEMORY_CONFIG).expanduser()
    if config is None and not path.exists():
        return None
    try:
        values = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f'Cannot read memory database config: {path}') from error
    if (not isinstance(values, list) or not 1 <= len(values) <= 32
            or any(not isinstance(value, str) or not value.strip() for value in values)):
        raise ValueError('Memory database config must be a JSON array of 1–32 paths')
    return values


def decoded(value, default):
    try:
        result = json.loads(value)
        return result if isinstance(result, type(default)) else default
    except (ValueError, TypeError):
        return default


class MemoryView:
    def __init__(self, paths=None):
        self.paths = {}
        for value in paths if paths is not None else [DEFAULT_MEMORY_DB]:
            path = Path(value).expanduser().resolve()
            key = hashlib.sha256(str(path).encode()).hexdigest()[:16]
            self.paths[key] = path

    @contextmanager
    def connection(self, key):
        if key not in self.paths:
            raise ValueError('Unknown memory database')
        path = self.paths[key]
        if not path.is_file():
            raise ValueError('대화 DB가 아직 없습니다. 대화 실행 후 다시 확인하세요.')
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.25)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            yield conn
        finally:
            conn.close()

    @staticmethod
    def tables(conn):
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def databases(self):
        return [{'id': key, 'path': str(path), 'exists': path.is_file()}
                for key, path in self.paths.items()]

    @staticmethod
    def activity(conn, tables, user, request):
        """Observed commits/model inputs and current job states, never inferred success."""
        result = {'saved': 0, 'no_change': False, 'model_input': None,
                  'failure_attempts': 0, 'jobs': [], 'trace_observed': False}
        if 'memory_trace_events' in tables:
            for row in conn.execute(
                    'SELECT event,count(*) AS n FROM memory_trace_events '
                    'WHERE user_id=? AND request_id=? GROUP BY event', (user, request)):
                result['trace_observed'] = True
                if row['event'] in ('story_committed', 'fact_committed'):
                    result['saved'] += row['n']
                if row['event'] == 'story_no_change':
                    result['no_change'] = True
                if row['event'] == 'story_extraction_failed':
                    result['failure_attempts'] = row['n']
            row = conn.execute(
                "SELECT data_json FROM memory_trace_events WHERE user_id=? AND request_id=? "
                "AND event='model_input' ORDER BY sequence DESC LIMIT 1", (user, request)).fetchone()
            if row:
                data = decoded(row[0], {})
                result['model_input'] = {'stories': len(data.get('stories', [])),
                                         'facts': len(data.get('fact_ids', [])),
                                         'history_messages': data.get('history_messages')}
        for table, fields in (
                ('story_runtime_jobs', 'job_id,state,attempts,error_code'),
                ('automatic_memory_jobs', 'state,reason')):
            if table in tables:
                result['jobs'].extend({'type': table, **dict(row)} for row in conn.execute(
                    'SELECT ' + fields + ' FROM ' + table +
                    ' WHERE user_id=? AND request_id=? LIMIT 100', (user, request)))
        return result

    def attention(self, conn, tables, user):
        """Keep failures discoverable even when their turns left the recent-turn window."""
        requests = set()
        if 'memory_trace_events' in tables:
            requests.update(row[0] for row in conn.execute(
                "SELECT request_id FROM memory_trace_events WHERE user_id=? "
                "AND event='story_extraction_failed' ORDER BY sequence DESC LIMIT 100", (user,)))
        for table in ('story_runtime_jobs', 'automatic_memory_jobs'):
            if table in tables:
                requests.update(row[0] for row in conn.execute(
                    'SELECT request_id FROM ' + table +
                    " WHERE user_id=? AND state='failed' ORDER BY created_at DESC LIMIT 51", (user,)))
        results = []
        for request in requests:
            turn = conn.execute('SELECT created_at,user_content FROM conversation_turns '
                                'WHERE user_id=? AND request_id=?', (user, request)).fetchone()
            results.append({'request_id': request, 'created_at': turn['created_at'] if turn else 0,
                            'user_content': turn['user_content'] if turn else '',
                            'activity': self.activity(conn, tables, user, request)})
        results.sort(key=lambda row: (row['created_at'], row['request_id']), reverse=True)
        return results[:50], len(results) > 50

    def snapshot(self, key, user='', conversation='', query=''):
        if len(query) > 300 or len(user) > 128 or len(conversation) > 128:
            raise ValueError('Memory filter too long')
        with self.connection(key) as conn:
            tables = self.tables(conn)
            if 'conversation_turns' not in tables:
                raise ValueError('대화 DB 형식이 아닙니다.')
            users = [row[0] for row in conn.execute(
                'SELECT user_id FROM conversation_sessions GROUP BY user_id ORDER BY user_id LIMIT 200')]
            user = user or (users[0] if users else '')
            clauses, params = ['user_id=?'], [user]
            if conversation:
                clauses.append('conversation_id=?')
                params.append(conversation)
            if query:
                clauses.append('(request_id LIKE ? OR user_content LIKE ? OR assistant_content LIKE ?)')
                params.extend(['%' + query + '%'] * 3)
            turns = [dict(row) for row in conn.execute(
                'SELECT request_id,conversation_id,turn_id,generation,ordinal,status,'
                'user_content,assistant_content,created_at,completed_at FROM conversation_turns WHERE '
                + ' AND '.join(clauses) + ' ORDER BY created_at DESC,request_id DESC LIMIT 101', params)]
            if 'memory_trace_events' in tables:
                # Failed inference removes its pending turn. Retain its observed
                # request IDs without inventing dialogue text or a completion.
                extra, arguments = '', [user]
                if conversation:
                    extra += ' AND e.conversation_id=?'
                    arguments.append(conversation)
                if query:
                    extra += ' AND e.request_id LIKE ?'
                    arguments.append('%' + query + '%')
                for row in conn.execute(
                        'SELECT e.request_id,e.conversation_id,e.turn_id,min(e.wall_time) AS created_at '
                        'FROM memory_trace_events e LEFT JOIN conversation_turns t '
                        'ON t.user_id=e.user_id AND t.request_id=e.request_id '
                        "WHERE e.user_id=? AND e.request_id<>'' AND t.request_id IS NULL" + extra +
                        ' GROUP BY e.request_id ORDER BY created_at DESC LIMIT 101', arguments):
                    turns.append({**dict(row), 'generation': None, 'ordinal': None,
                                  'status': 'trace_only', 'user_content': '',
                                  'assistant_content': '', 'completed_at': None})
                turns.sort(key=lambda item: (item['created_at'], item['request_id']), reverse=True)
            conversations = [dict(row) for row in conn.execute(
                'SELECT conversation_id,status,generation,updated_at FROM conversation_sessions '
                'WHERE user_id=? ORDER BY updated_at DESC LIMIT 100', (user,))]
            policies = {}
            for name in ('memory_policy_state', 'story_runtime_policy'):
                if name in tables:
                    row = conn.execute('SELECT enabled,revision FROM ' + name + ' WHERE user_id=?', (user,)).fetchone()
                    policies[name] = dict(row) if row else {'enabled': 0, 'revision': 0}
            stories = []
            if 'story_runtime_stories' in tables:
                for row in conn.execute(
                        'SELECT story_id,title,current_json,version,updated_at,active FROM story_runtime_stories '
                        'WHERE user_id=? ORDER BY updated_at DESC LIMIT 101', (user,)):
                    story = dict(row)
                    story['current'] = decoded(story.pop('current_json'), [])
                    stories.append(story)
            facts = []
            if 'memories' in tables:
                facts = [dict(row) for row in conn.execute(
                    'SELECT id,kind,content,created_at,expires_at FROM memories WHERE user_id=? '
                    'ORDER BY created_at DESC LIMIT 101', (user,))]
            jobs = {}
            for table in ('story_runtime_jobs', 'automatic_memory_jobs'):
                if table in tables:
                    jobs[table] = {row[0]: row[1] for row in conn.execute(
                        'SELECT state,count(*) FROM ' + table + ' WHERE user_id=? GROUP BY state', (user,))}
            for turn in turns[:100]:
                turn['activity'] = self.activity(conn, tables, user, turn['request_id'])
            attention, attention_truncated = self.attention(conn, tables, user)
            return {'database': self.paths[key].name, 'user': user, 'users': users,
                    'conversations': conversations, 'policies': policies, 'jobs': jobs,
                    'trace_available': 'memory_trace_events' in tables,
                    'attention': attention, 'attention_truncated': attention_truncated,
                    'turns': turns[:100], 'turns_truncated': len(turns) > 100,
                    'stories': stories[:100], 'stories_truncated': len(stories) > 100,
                    'facts': facts[:100], 'facts_truncated': len(facts) > 100}

    def turn(self, key, user, request):
        if not user or not request or len(user) > 128 or len(request) > 128:
            raise ValueError('User and request ID required')
        with self.connection(key) as conn:
            tables = self.tables(conn)
            row = conn.execute('SELECT * FROM conversation_turns WHERE user_id=? AND request_id=?',
                               (user, request)).fetchone()
            events = []
            if 'memory_trace_events' in tables:
                for event in conn.execute(
                        'SELECT sequence,wall_time,event,data_json FROM memory_trace_events '
                        'WHERE user_id=? AND request_id=? ORDER BY sequence LIMIT 501', (user, request)):
                    item = dict(event)
                    item['data'] = decoded(item.pop('data_json'), {})
                    events.append(item)
            jobs = []
            for table, fields in (
                    ('story_runtime_jobs', 'job_id,state,attempts,error_code,created_at'),
                    ('automatic_memory_jobs', 'state,reason,created_at,started_at,finished_at')):
                if table in tables:
                    jobs.extend({'type': table, **dict(job)} for job in conn.execute(
                        'SELECT ' + fields + ' FROM ' + table + ' WHERE user_id=? AND request_id=? LIMIT 100',
                        (user, request)))
            response = decoded(row['response_json'], {}) if row else {}
            return {'events': events[:500], 'events_truncated': len(events) > 500,
                    'jobs': jobs, 'turn_present': row is not None,
                    'legacy_fact_ids': response.get('memory', {}).get('ids', []),
                    'story_binding': response.get('story_binding'),
                    'provider': response.get('provider', {})}

    def evidence(self, key, user, story_id):
        """Resolve current exact spans only; redacted/stale text never reappears."""
        with self.connection(key) as conn:
            row = conn.execute('SELECT refs_json,version,active FROM story_runtime_stories '
                               'WHERE user_id=? AND story_id=?', (user, story_id)).fetchone()
            if row is None:
                raise ValueError('이야기가 없거나 삭제되었습니다.')
            refs = decoded(row['refs_json'], [])
            sources = []
            for ref in refs[:100]:
                if not isinstance(ref, dict) or ref.get('role') not in ('user', 'assistant'):
                    continue
                turn = conn.execute(
                    'SELECT request_id,user_content,assistant_content,completed_at FROM conversation_turns '
                    "WHERE user_id=? AND conversation_id=? AND session_instance_id=? AND generation=? AND turn_id=? AND status='completed'",
                    (user, ref.get('conversation_id'), ref.get('session_instance_id'),
                     ref.get('generation'), ref.get('turn_id'))).fetchone()
                text = (turn[ref['role'] + '_content'] or '') if turn else ''
                start, end = ref.get('start'), ref.get('end')
                valid = (bool(row['active']) and bool(turn) and type(start) is int and type(end) is int
                         and 0 <= start < end <= len(text)
                         and hashlib.sha256(text.encode()).hexdigest() == ref.get('sha256'))
                sources.append({'ref': ref, 'available': valid,
                                'request_id': turn['request_id'] if turn else None,
                                'completed_at': turn['completed_at'] if turn else None,
                                'text': text[start:end] if valid else None})
            return {'story_id': story_id, 'version': row['version'],
                    'sources': sources, 'truncated': len(refs) > 100}
