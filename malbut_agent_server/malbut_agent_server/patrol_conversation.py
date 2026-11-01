"""Select everyday conversation topics from verified, pixel-free patrol memory."""

from dataclasses import dataclass
import json
import math
import re
import time

from malbut_agent_server.patrol_evidence import receipt


# Deliberately small: observations are conversation cues, never inferred interests,
# identities, ownership, diagnoses, changes, or instructions copied from a scene.
TOPICS = (
    ('plants', r'화분|관엽\s*식물|potted plant',
     '식물 키우는 거 좋아하세요?'),
    ('books', r'(?:^|[\s,·])(?:책|책들|책더미|도서)(?=$|[\s,·])|\bbooks?\b',
     '요즘 재미있게 읽는 책 있으세요?'),
    ('music', r'기타$|피아노|우쿨렐레|바이올린|\bguitar\b|\bpiano\b|\bukulele\b|\bviolin\b',
     '평소 어떤 음악 좋아하세요?'),
    ('exercise', r'덤벨|아령|바벨|요가\s*매트|운동\s*공|dumbbell|barbell|yoga mat',
     '평소 어떤 운동 좋아하세요?'),
)
TOPIC_MAX_AGE_S = 24 * 60 * 60
TOPIC_REPEAT_S = 7 * 24 * 60 * 60
CHAT_COOLDOWN_S = 30 * 60


@dataclass(frozen=True)
class Opener:
    """A fixed utterance bound to one verified historical observation."""

    topic: str
    text: str
    observation_id: str
    captured_at: str

    def context(self):
        """No pixels, person IDs, room names, or raw model-generated prose."""
        return {'topic': self.topic, 'assistant_utterance': self.text,
                'observation_id': self.observation_id, 'captured_at': self.captured_at,
                'source': 'past_patrol_observation', 'person_identity': 'unknown'}


class PatrolConversationTopics:
    """Read verified observations and persist only bounded topic-attempt history."""

    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock

    def set_enabled(self, enabled):
        """A direct spoken opt-out survives process restarts, separately from topics."""
        if self.store is None:
            raise ValueError('patrol memory unavailable')
        with self.store.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS patrol_conversation_preferences (
                user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL)''')
            connection.execute('INSERT OR REPLACE INTO patrol_conversation_preferences VALUES (?,?)',
                               (self.store.owner_id, int(enabled)))

    def _enabled(self, connection):
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                   "AND name='patrol_conversation_preferences'").fetchone()
        if not table:
            return False
        row = connection.execute('SELECT enabled FROM patrol_conversation_preferences WHERE user_id=?',
                                 (self.store.owner_id,)).fetchone()
        return row is not None and bool(row['enabled'])

    def reserve(self):
        """Off-thread selection; atomically suppress repeated attempts across restarts."""
        if self.store is None:
            return None
        now = self.clock()
        if not math.isfinite(now):
            return None
        candidates = []
        with self.store.connect() as connection:
            if not self._enabled(connection):
                return None
            rows = connection.execute(
                "SELECT * FROM observations WHERE user_id=? AND status='ready' "
                'AND captured_at BETWEEN ? AND ? ORDER BY captured_at DESC, id DESC LIMIT 100',
                (self.store.owner_id, now - TOPIC_MAX_AGE_S, now)).fetchall()
        for row in rows:
            try:
                # Unlike a file path or a matching keyword, the committed receipt
                # remains verifiable after the raw images have been released.
                with self.store.connect() as connection:
                    if not receipt(connection, row):
                        continue
                metadata, analysis = json.loads(row['metadata']), json.loads(row['analysis'])
                for obj in analysis['objects']:
                    if obj.get('confidence') != 'high' or obj.get('kind', 'object') != 'object':
                        continue
                    name = obj['name']
                    # Shelf alone does not establish books; prefer explicit contents.
                    if name in ('책장', '책꽂이', 'bookshelf'):
                        continue
                    for topic, pattern, text in TOPICS:
                        if re.search(pattern, name, re.IGNORECASE):
                            candidates.append(Opener(topic, text, row['id'], metadata['captured_at']))
            except (ValueError, OSError, TypeError, KeyError):
                continue
        if not candidates:
            return None
        with self.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if not self._enabled(connection):
                return None
            connection.execute('''CREATE TABLE IF NOT EXISTS patrol_conversation_topics (
                user_id TEXT NOT NULL, topic TEXT NOT NULL, attempted_at REAL NOT NULL,
                observation_id TEXT NOT NULL, PRIMARY KEY(user_id, topic))''')
            history = dict(connection.execute(
                'SELECT topic, attempted_at FROM patrol_conversation_topics WHERE user_id=?',
                (self.store.owner_id,)).fetchall())
            # A backwards wall clock must not reset a cooldown.
            if history and now - max(history.values()) < CHAT_COOLDOWN_S:
                return None
            for opener in candidates:
                if opener.topic in history and now - history[opener.topic] < TOPIC_REPEAT_S:
                    continue
                connection.execute('INSERT OR REPLACE INTO patrol_conversation_topics '
                                   'VALUES (?,?,?,?)', (self.store.owner_id, opener.topic,
                                                       now, opener.observation_id))
                return opener
        return None


class EncounterGate:
    """Require a stable current person and suppress one-sided repeated approaches."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.present_since = None
        self.last_present = None
        self.absent_since = None
        self.used = False
        self.last_activity = clock()

    def observe(self, present):
        """Only explicit empty frames establish a new encounter, not a camera outage."""
        now = self.clock()
        if present is None:  # Several people: no individual conversational encounter.
            self.present_since = self.absent_since = None
            return
        if present:
            if self.last_present is None or now - self.last_present > 1.5:
                self.present_since = now
            self.last_present = now
            self.absent_since = None
        else:
            self.present_since = None
            if self.absent_since is None:
                self.absent_since = now
            if now - self.absent_since >= 10:
                self.used = False

    def activity(self):
        self.last_activity = self.clock()

    def ready(self):
        now = self.clock()
        return (not self.used and self.present_since is not None
                and self.last_present is not None and 0 <= now - self.last_present <= 1.5
                and now - self.present_since >= 3 and now - self.last_activity >= 30)


def ends_conversation(text):
    """Do not turn a refusal or departure into another question."""
    normalized = re.sub(r'[\s.!?~。！？]', '', text)
    return normalized in {'됐어', '됐어요',
                          '나중에', '다음에', '지금바빠', '지금바빠요', '그만', '그만해',
                          '그만말해', '말걸지마', '말걸지마세요', '대화그만', '조용히해줘'}


def conversation_preference(text):
    """Only direct explicit requests change the durable spontaneous-speech switch."""
    text = re.sub(r'[\s.!?~。！？]', '', text)
    if text in {'말걸지마', '말걸지마세요', '먼저말걸지마', '먼저말걸지마세요',
                '먼저말걸지말아줘', '먼저말걸지말아주세요'}:
        return False
    if text in {'먼저말걸어도돼', '먼저말걸어도돼요', '먼저말걸어줘', '먼저말걸어주세요'}:
        return True
    return None
