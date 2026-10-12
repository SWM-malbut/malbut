"""Bounded, opt-in flight recorder for suspicious final STT utterances.

Matching only requests an evidence snapshot; it never rejects or rewrites speech.
The existing diagnostic model wrapper observes the actual lazy decoder results.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from threading import Condition, Thread, local
import time
import unicodedata
from uuid import uuid4
import wave

from malbut_stt.diagnostics import TranscriptionDiagnostics, _json
from malbut_stt.incident_trace import AGENT_EVENTS, ROSOUT_LOGGERS, transactions


DEFAULT_CONTAINS = ('구독', '시청해주셔서', '자막제공')
DEFAULT_EXACT = ('감사합니다', '고신을주셔서감사합니다', '다음편에참아')


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFC', text)
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))


@dataclass(frozen=True)
class _Entry:
    at: float
    record: bytes
    pcm: bytes = b''

    @property
    def size(self):
        return len(self.record) + len(self.pcm)


class IncidentRecorder(TranscriptionDiagnostics):
    """Keep recent PCM/events in RAM and write triggered windows on one worker.

    The byte cap covers retained payload, not Python allocator overhead. A writer
    snapshot and one queued snapshot may each retain an additional buffer.
    Both bytes and entry counts are bounded. Already saved incidents are immutable.
    """

    def __init__(self, directory, *, pre_seconds=30.0, post_seconds=15.0,
                 max_buffer_bytes=32 * 1024 * 1024, max_bytes=512 * 1024 * 1024,
                 max_entries=12000, contains=DEFAULT_CONTAINS, exact=DEFAULT_EXACT,
                 metadata=None, clock=time.monotonic, wall_ns=time.time_ns,
                 on_saved=None, on_error=None, save_audio=True):
        if type(save_audio) is not bool:
            raise ValueError('save_audio must be a boolean')
        self.save_audio = save_audio
        for value in (pre_seconds, post_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError('incident windows must be finite and nonnegative')
        for value in (max_buffer_bytes, max_bytes, max_entries):
            if type(value) is not int or value <= 0:
                raise ValueError('incident limits must be positive integers')
        for patterns in (contains, exact):
            if not isinstance(patterns, (tuple, list)) or any(
                    not isinstance(p, str) or not normalize(p) for p in patterns):
                raise ValueError('trigger patterns must be nonempty text lists')
        if not contains and not exact:
            raise ValueError('at least one trigger pattern is required')
        self.directory = Path(directory).expanduser()
        self.pre_seconds, self.post_seconds = pre_seconds, post_seconds
        self.max_buffer_bytes, self.max_bytes = max_buffer_bytes, max_bytes
        self.max_entries = max_entries
        self.contains = tuple(normalize(p) for p in contains)
        self.exact = tuple(normalize(p) for p in exact)
        self.metadata = json.loads(_json(metadata or {}))
        self.clock, self.wall_ns, self.on_saved = clock, wall_ns, on_saved
        self.on_error = on_error
        self._local = local()
        self._condition = Condition()
        self._entries = deque()
        self._buffer_bytes = 0
        self._evictions = 0
        self._sample_index = 0
        self._active = None
        self._ready = deque()
        self._skipped_triggers = 0
        self._seen = OrderedDict()
        self._closing = False
        self._disabled = False
        self.error = None
        self.last_incident = None
        self._worker = Thread(target=self._run, name='stt-incident-writer', daemon=True)
        self._worker.start()

    def _fail(self, error):
        already_disabled = self._disabled
        super()._fail(error)
        if not already_disabled and self.on_error is not None:
            try:
                self.on_error(self.error)
            except Exception:
                pass  # Even a shutdown logger must not disrupt speech processing.

    def _append(self, event, *, pcm=b'', **fields):
        if self._disabled or self._closing:
            return None
        at = self.clock()
        record = dict(event=event, monotonic_s=at, wall_ns=self.wall_ns(), **fields)
        content = _json(record)
        if len(content) > 65536:
            raise ValueError('incident event exceeds 64 KiB')
        entry = _Entry(at, content, pcm)
        with self._condition:
            if self._closing:
                return None
            cutoff = at - self.pre_seconds
            if self._active is not None:
                cutoff = min(cutoff, self._active['started'] - self.pre_seconds)
            while self._entries and self._entries[0].at < cutoff:
                self._buffer_bytes -= self._entries.popleft().size
            if entry.size > self.max_buffer_bytes:
                self._evictions += 1
                return None
            while self._entries and (self._buffer_bytes + entry.size > self.max_buffer_bytes
                                     or len(self._entries) >= self.max_entries):
                self._buffer_bytes -= self._entries.popleft().size
                self._evictions += 1
            self._entries.append(entry)
            self._buffer_bytes += entry.size
        return record

    def event(self, name, **metadata):
        try:
            record = self._append(name, context=getattr(self._local, 'metadata', None) or {},
                                  **metadata)
            # Keep evidence of matched text stopped by the quality guard too.
            # Ordinary wake/preview text and TTS echoes do not trigger a bundle.
            if record is not None and name in ('transcript_published', 'recognition_rejected'):
                self._trigger(record)
        except Exception as error:
            self._fail(error)

    def _trigger(self, record):
        text = normalize(record.get('text', ''))
        matches = ([p for p in self.contains if p in text]
                   + [p for p in self.exact if p == text])
        uid = record.get('utterance_id')
        if not matches or not uid:
            return
        with self._condition:
            if uid in self._seen or self._closing:
                return
            if (self._active is not None and record['monotonic_s']
                    > self._active['started'] + self.post_seconds):
                # A slow writer must not merge a later trigger into an already
                # expired window. Queue one completed snapshot without blocking.
                if self._ready:
                    self._skipped_triggers += 1
                    return
                self._ready.append(self._snapshot(self._active['started'] + self.post_seconds))
            self._seen[uid] = None
            if len(self._seen) > 128:
                self._seen.popitem(last=False)
            if self._active is None:
                self._active = dict(started=record['monotonic_s'],
                                    wall_ns=record['wall_ns'], triggers=[])
            if len(self._active['triggers']) < 64:
                self._active['triggers'].append(dict(
                    utterance_id=uid, text=record['text'], matched=matches,
                    event=record['event'],
                    monotonic_s=record['monotonic_s'], wall_ns=record['wall_ns']))
            self._condition.notify_all()

    def audio(self, pcm, **metadata):
        """Observe microphone chunks before gating, including blocked speaker tails."""
        if self._disabled or self._closing:
            return
        try:
            pcm = bytes(pcm)
            if len(pcm) % 2:
                raise ValueError('microphone evidence requires PCM16')
            start = self._sample_index
            self._sample_index += len(pcm) // 2
            self._append('microphone_chunk', pcm=pcm if self.save_audio else b'',
                         sample_index=start, samples=len(pcm) // 2,
                         audio_saved=self.save_audio, **metadata)
        except Exception as error:
            self._fail(error)

    def ros_log(self, message):
        """Observe selected ROS logs; never treat a publication as Agent receipt."""
        if message.name not in ROSOUT_LOGGERS or self._disabled or self._closing:
            return
        try:
            raw = message.msg.encode('utf-8')
            truncated = len(raw) > 16384
            text = raw[:16384].decode('utf-8', errors='replace')
            try:
                payload = json.loads(text) if not truncated else None
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                payload = None
            uid = None
            if (message.name == 'malbut_agent_communication' and payload
                    and payload.get('event') in AGENT_EVENTS):
                uid = payload.get('utterance_id')
                if not isinstance(uid, str):
                    uid = None
            self.event('program_log', logger=message.name, level=message.level,
                       message=text, truncated=truncated, payload=payload, utterance_id=uid,
                       source_stamp_ns=message.stamp.sec * 1000000000 + message.stamp.nanosec,
                       source_file=message.file[:1024], source_function=message.function[:1024],
                       source_line=message.line)
        except Exception as error:
            self._fail(error)

    def wrap_vad(self, is_speech, *, kind):
        def observed(frame, rate):
            decision = is_speech(frame, rate)
            if not self._disabled and not self._closing:
                try:
                    import numpy as np

                    values = np.frombuffer(frame, dtype='<i2').astype(np.float32) / 32768
                    self.event('vad_frame', kind=kind, speech=bool(decision), sample_rate=rate,
                               samples=len(values), pcm_sha256=hashlib.sha256(frame).hexdigest(),
                               rms=float(np.sqrt(np.mean(values * values))),
                               peak=float(np.max(np.abs(values))))
                except Exception as error:
                    self._fail(error)
            return decision
        return observed

    def _begin(self, audio, options):
        if self._disabled or self._closing:
            return None
        try:
            import numpy as np

            samples = np.asarray(audio)
            pcm = np.rint(np.clip(samples * 32768, -32768, 32767)).astype('<i2')
            if samples.ndim != 1 or not np.array_equal(samples, pcm.astype(np.float32) / 32768):
                raise ValueError('model audio cannot be saved losslessly as PCM16')
            raw = pcm.tobytes()
            name = 'decode-' + uuid4().hex
            record = dict(decode_id=name, context=getattr(self._local, 'metadata', None) or {},
                          wav='decodes/' + name + '.wav' if self.save_audio else None,
                          sample_rate=16000, audio_saved=self.save_audio,
                          sample_count=len(pcm), pcm_sha256=hashlib.sha256(raw).hexdigest(),
                          options=options, segments=[], segments_truncated=False, error=None)
            self._append('decode_input', pcm=raw if self.save_audio else b'', **record)
            return record
        except Exception as error:
            self._fail(error)
            return None

    # _segment, context and wrap_model are shared with continuous diagnostics.
    # They preserve lazy consumption and pass through original objects/errors.
    def _finish(self, record, error=None):
        if record is None:
            return
        try:
            if error is not None:
                record['error'] = dict(type=type(error).__name__, message=str(error)[:2048])
            self._append('decode_result', **record)
        except Exception as failure:
            self._fail(failure)

    def _snapshot(self, end):
        """Freeze one window while holding the condition; all entries are immutable."""
        active = self._active
        deadline = active['started'] + self.post_seconds
        entries = sorted((e for e in self._entries
                          if active['started'] - self.pre_seconds <= e.at <= end),
                         key=lambda e: e.at)
        manifest = dict(
            schema_version=1, purpose='observation_only', metadata=self.metadata,
            triggers=active['triggers'], pre_seconds=self.pre_seconds,
            post_seconds=self.post_seconds, post_window_complete=end >= deadline,
            finalized_by='shutdown' if end < deadline else 'post_window_elapsed',
            window_start_monotonic_s=active['started'] - self.pre_seconds,
            window_end_monotonic_s=end, retained_payload_bytes=sum(e.size for e in entries),
            buffer_evictions_total=self._evictions, max_buffer_bytes=self.max_buffer_bytes,
            max_entries=self.max_entries, max_disk_bytes=self.max_bytes,
            skipped_triggers_total=self._skipped_triggers,
            audio_saved=self.save_audio, rosout_loggers=list(ROSOUT_LOGGERS),
            capture_error=self.error,
            contains=list(self.contains), exact=list(self.exact),
        )
        self._active = None
        return manifest, entries

    def _run(self):
        while True:
            with self._condition:
                while self._active is None and not self._ready and not self._closing:
                    self._condition.wait()
                if self._active is None and not self._ready:
                    return
                if self._ready:
                    manifest, entries = self._ready.popleft()
                else:
                    deadline = self._active['started'] + self.post_seconds
                    remaining = deadline - self.clock()
                    if remaining > 0 and not self._closing:
                        self._condition.wait(timeout=min(remaining, 60.0))
                        continue
                    manifest, entries = self._snapshot(min(deadline, self.clock()))
            try:
                self._save(manifest, entries)
            except Exception as error:
                self._fail(error)

    @staticmethod
    def _write(path, data):
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
            f.write(data)

    @staticmethod
    def _wav(path, chunks):
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
            with wave.open(f, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                for pcm in chunks:
                    wav.writeframesraw(pcm)

    def _save(self, manifest, entries):
        """The only disk-writing path; never executes on the audio/ROS/ASR threads."""
        events, records, mic, chunks, decodes = [], [], [], [], {}
        offset = 0
        for entry in entries:
            record = json.loads(entry.record)
            records.append(record)
            events.append(entry.record + b'\n')
            if record['event'] == 'microphone_chunk':
                mic.append(entry.pcm)
                chunks.append(_json(dict(record, wav_offset_samples=(
                    offset if self.save_audio else None))) + b'\n')
                offset += len(entry.pcm) // 2
            elif record['event'] in ('decode_input', 'decode_result'):
                item = decodes.setdefault(record['decode_id'], {})
                item['record'] = record
                if record['event'] == 'decode_input' and self.save_audio:
                    item['pcm'] = entry.pcm
                if record['event'] == 'decode_input':
                    item['input_observed'] = True
        manifest['microphone_samples'] = offset
        manifest['microphone_layout'] = (
            'concatenated_chunks; see microphone_chunks.jsonl for gaps'
            if self.save_audio else 'audio_not_saved')
        manifest['events'] = len(events)
        manifest['decodes'] = len(decodes)
        manifest['observed_start_monotonic_s'] = entries[0].at if entries else None
        manifest['observed_end_monotonic_s'] = entries[-1].at if entries else None
        files = {'incident.json': _json(manifest) + b'\n',
                 'events.jsonl': b''.join(events), 'microphone_chunks.jsonl': b''.join(chunks),
                 'rosout.jsonl': b''.join(_json(r) + b'\n' for r in records
                                          if r['event'] == 'program_log'),
                 'transactions.json': json.dumps(
                     transactions(records, manifest['triggers']), ensure_ascii=False,
                     indent=2, allow_nan=False).encode('utf-8') + b'\n'}
        waves = {'microphone.wav': mic} if self.save_audio else {}
        for name, item in decodes.items():
            record = item['record']
            record['input_available'] = 'pcm' in item
            record['input_observed'] = item.get('input_observed', False)
            record['result_available'] = record['event'] == 'decode_result'
            files['decodes/' + name + '.json'] = _json(record) + b'\n'
            if 'pcm' in item:
                waves[record['wav']] = [item['pcm']]
        required = sum(map(len, files.values())) + sum(
            44 + sum(map(len, parts)) for parts in waves.values())
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Serialize quota checks with another recorder using the same directory.
        with os.fdopen(os.open(self.directory / '.writer.lock', os.O_RDWR | os.O_CREAT,
                               0o600), 'rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            used = sum(p.stat().st_size for p in self.directory.rglob('*') if p.is_file())
            if used + required > self.max_bytes:
                raise OSError('incident disk quota reached; existing evidence retained')
            name = 'incident-' + str(manifest['triggers'][0]['wall_ns']) + '-' + uuid4().hex[:8]
            pending = self.directory / ('.partial-' + name)
            pending.mkdir(mode=0o700)
            (pending / 'decodes').mkdir(mode=0o700)
            for filename, data in files.items():
                self._write(pending / filename, data)
            for filename, parts in waves.items():
                self._wav(pending / filename, parts)
            path = self.directory / name
            pending.rename(path)
        self.last_incident = path
        if self.on_saved is not None:
            self.on_saved(path)

    def close(self):
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._worker.join(timeout=5.0)
        if self._worker.is_alive():
            self.error = 'incident writer shutdown pending'


def from_environment(environ, *, metadata=None, on_saved=None, on_error=None):
    """Return an incident recorder only for an explicitly selected test run."""
    raw = environ.get('MALBUT_STT_INCIDENT_CONFIG', '').strip()
    if not raw:
        return None
    if environ.get('MALBUT_STT_DIAGNOSTIC_DIR', '').strip():
        raise ValueError('select either continuous diagnostics or incident capture')
    options = json.loads(raw)
    if not isinstance(options, dict):
        raise ValueError('incident configuration must be a JSON object')
    return IncidentRecorder(**options, metadata=metadata, on_saved=on_saved, on_error=on_error)
