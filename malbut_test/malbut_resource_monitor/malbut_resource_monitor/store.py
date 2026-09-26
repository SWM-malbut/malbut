"""Append-only, timestamped JSONL. No robot commands or application imports."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from uuid import uuid4


def slug(value):
    return re.sub(r'[^a-zA-Z0-9_.-]+', '_', value).strip('._') or 'unnamed'


class Store:
    def __init__(self, root, interval, parent_pid):
        self.started_ns = time.monotonic_ns()
        self.wall_ns = time.time_ns()
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
        self.path = Path(root).expanduser().resolve() / name
        self.path.mkdir(parents=True, mode=0o700)
        self.lock = threading.RLock()
        self.files = {}
        self.metadata = {
            'schema': 1, 'session': name, 'hostname': socket.gethostname(),
            'started_wall_ns': self.wall_ns, 'started_monotonic_ns': self.started_ns,
            'interval_s': interval, 'parent_pid': parent_pid, 'collector_pid': os.getpid(),
            'finished': False, 'channels': {}, 'process_catalog': {},
            'limitations': [
                'Process CPU: one core = 100%; system CPU: entire machine = 100%.',
                'RSS includes shared pages; do not sum process RSS as physical RAM.',
                'Per-process GPU utilization is unavailable on this Jetson collector.',
                'Action times are status receipt times, not request or motor start times.',
                'Topic Hz/bytes are received CDR samples, '
                'not guaranteed publish Hz or wire traffic.',
                'Topic subscriptions and the collector consume resources; '
                'observer group records collector/tegrastats overhead, not added publisher work.',
                'Power rails are Jetson measurements, not robot/motor power; '
                'never sum overlapping rails.',
                'Null means unavailable; tegrastats stale data is not carried forward.',
            ],
        }
        self._metadata()

    def _metadata(self):
        temporary = self.path / 'metadata.tmp'
        with temporary.open('w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(self.metadata, stream, ensure_ascii=False, allow_nan=False, indent=2)
        temporary.replace(self.path / 'metadata.json')

    def register(self, channel, **info):
        with self.lock:
            previous = self.metadata['channels'].get(channel, {})
            merged = {**previous, **info}
            if channel not in self.metadata['channels'] or merged != previous:
                self.metadata['channels'][channel] = merged
                self._metadata()

    def update_metadata(self, **info):
        with self.lock:
            self.metadata.update(info)
            self._metadata()

    def process(self, identity, info):
        with self.lock:
            if self.metadata['process_catalog'].get(identity) != info:
                self.metadata['process_catalog'][identity] = info
                self._metadata()

    def stamp(self):
        return {'t': (time.monotonic_ns() - self.started_ns) / 1e9,
                'wall_ns': time.time_ns()}

    def write(self, channel, record):
        # Channel names are internal, never raw ROS names or URL paths.
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?', channel):
            raise ValueError('Invalid channel')
        with self.lock:
            if channel not in self.files:
                path = self.path / (channel + '.jsonl')
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.files[channel] = path.open('a', encoding='utf-8', buffering=1)
                os.chmod(path, 0o600)
            self.files[channel].write(json.dumps(
                {**self.stamp(), **record}, ensure_ascii=False, allow_nan=False) + '\n')

    def close(self, reason):
        with self.lock:
            self.metadata.update(finished=True, end=self.stamp(), stop_reason=reason)
            self._metadata()
            for stream in self.files.values():
                stream.close()
