"""Content-free per-turn latency waterfall recorder and inspection helpers."""

import argparse
from collections import Counter, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any, Deque, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
import urllib.request


DEFAULT_TURN_METRICS_CAPACITY = 100
MAX_TURN_METRICS_CAPACITY = 1000
MAX_IDENTIFIER_LENGTH = 256

STAGE_ORDER: Tuple[str, ...] = (
    'queue_wait_ms',
    'scene_prepare_ms',
    'begin_turn_ms',
    'memory_snapshot_ms',
    'provider_prep_ms',
    'headers_ms',
    'ttft_ms',
    'provider_ms',
    'safety_ms',
    'tool_followup_ms',
    'commit_ms',
    'tts_handoff_ms',
    'time_to_tts_ms',
    'total_ms',
)
_ALLOWED_STAGES = frozenset(STAGE_ORDER)
_ALLOWED_CHANNELS = frozenset({'speech', 'http', 'orchestrator'})
_ALLOWED_OUTCOMES = frozenset({'ok', 'cached', 'cancelled', 'error', 'companion'})
_ALLOWED_USAGE_KEYS = frozenset({
    'input_tokens',
    'output_tokens',
    'cached_input_tokens',
    'reasoning_output_tokens',
    'cache_hit_ratio',
    'attempt_count',
    'retry_delay_ms',
    'model_input_chars',
})
_ALLOWED_FLAGS = frozenset({
    'cached_response',
    'image_captured',
    'delivery_before_save',
    'prefetch_hit',
    'offline_fallback',
})

_current_trace: ContextVar[Optional['TurnWaterfallTrace']] = ContextVar(
    'malbut_turn_waterfall_trace',
    default=None,
)


def _clean_identifier(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > MAX_IDENTIFIER_LENGTH:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in stripped):
        return None
    return stripped


def _clean_ms(value: Any) -> Optional[float]:
    if type(value) not in (int, float):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0:
        return None
    return round(numeric, 2)


def _percentile(sorted_values: Sequence[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return round(sorted_values[0], 2)
    index = quantile * (len(sorted_values) - 1)
    lower = int(math.floor(index))
    upper = int(math.ceil(index))
    if lower == upper:
        return round(sorted_values[lower], 2)
    weight = index - lower
    interpolated = (
        sorted_values[lower] * (1.0 - weight)
        + sorted_values[upper] * weight
    )
    return round(interpolated, 2)


@dataclass(frozen=True)
class TurnWaterfallRecord:
    """One strictly content-free per-turn latency waterfall record."""

    recorded_at: float
    channel: str
    outcome: str
    request_id: Optional[str] = None
    utterance_id: Optional[str] = None
    conversation_id: Optional[str] = None
    decision_type: Optional[str] = None
    tool_name: Optional[str] = None
    safety_code: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    stages_ms: Dict[str, float] = field(default_factory=dict)
    usage: Dict[str, Any] = field(default_factory=dict)
    flags: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a bounded, JSON-safe, content-free dictionary."""
        ordered_stages = {
            stage: self.stages_ms[stage]
            for stage in STAGE_ORDER
            if stage in self.stages_ms
        }
        return {
            'recorded_at': round(self.recorded_at, 3),
            'channel': self.channel,
            'outcome': self.outcome,
            'request_id': self.request_id,
            'utterance_id': self.utterance_id,
            'conversation_id': self.conversation_id,
            'decision_type': self.decision_type,
            'tool_name': self.tool_name,
            'safety_code': self.safety_code,
            'provider': self.provider,
            'model': self.model,
            'stages_ms': ordered_stages,
            'usage': dict(self.usage),
            'flags': dict(self.flags),
        }

    def to_compact_dict(self) -> Dict[str, Any]:
        """Serialize a compact summary suitable for one structured log line."""
        compact: Dict[str, Any] = {
            'outcome': self.outcome,
            'stages_ms': {
                stage: self.stages_ms[stage]
                for stage in STAGE_ORDER
                if stage in self.stages_ms
            },
        }
        for key in ('decision_type', 'tool_name', 'safety_code', 'provider', 'model'):
            value = getattr(self, key)
            if value is not None:
                compact[key] = value
        if self.usage:
            compact['usage'] = dict(self.usage)
        active_flags = {key: value for key, value in self.flags.items() if value}
        if active_flags:
            compact['flags'] = active_flags
        return compact


class TurnWaterfallTrace:
    """Mutable per-turn timer shared cleanly across speech and orchestrator."""

    def __init__(
        self,
        *,
        channel: str = 'orchestrator',
        started_at: Optional[float] = None,
        request_id: Optional[str] = None,
        utterance_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        self._lock = threading.Lock()
        self.channel = channel if channel in _ALLOWED_CHANNELS else 'orchestrator'
        self.started_at = (
            float(started_at) if started_at is not None else time.perf_counter()
        )
        self._emitted_at: Optional[float] = None
        self._finalized_record: Optional[TurnWaterfallRecord] = None
        self._outcome = 'ok'
        self._request_id = _clean_identifier(request_id)
        self._utterance_id = _clean_identifier(utterance_id)
        self._conversation_id = _clean_identifier(conversation_id)
        self._decision_type: Optional[str] = None
        self._tool_name: Optional[str] = None
        self._safety_code: Optional[str] = None
        self._provider: Optional[str] = None
        self._model: Optional[str] = None
        self._stages_ms: Dict[str, float] = {}
        self._usage: Dict[str, Any] = {}
        self._flags: Dict[str, bool] = {}

    @property
    def finalized(self) -> bool:
        """Return whether this trace has already been committed to a recorder."""
        with self._lock:
            return self._finalized_record is not None

    @property
    def record(self) -> Optional[TurnWaterfallRecord]:
        """Return the finalized record once committed, else None."""
        with self._lock:
            return self._finalized_record

    def set_stage_ms(self, name: str, duration_ms: float) -> None:
        """Set one whitelisted stage duration in milliseconds."""
        if name not in _ALLOWED_STAGES:
            return
        cleaned = _clean_ms(duration_ms)
        if cleaned is None:
            return
        with self._lock:
            self._stages_ms[name] = cleaned

    def add_stage_ms(self, name: str, duration_ms: float) -> None:
        """Accumulate milliseconds onto a whitelisted stage."""
        if name not in _ALLOWED_STAGES:
            return
        cleaned = _clean_ms(duration_ms)
        if cleaned is None:
            return
        with self._lock:
            self._stages_ms[name] = round(self._stages_ms.get(name, 0.0) + cleaned, 2)

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        """Measure one stage's wall time even when the block raises."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add_stage_ms(name, (time.perf_counter() - started) * 1000.0)

    def mark_dequeued(self, now: Optional[float] = None) -> None:
        """Record elapsed queue wait time from submit() until worker dequeue."""
        timestamp = float(now) if now is not None else time.perf_counter()
        elapsed = max(0.0, (timestamp - self.started_at) * 1000.0)
        self.set_stage_ms('queue_wait_ms', elapsed)

    def mark_emitted(self, now: Optional[float] = None) -> None:
        """Mark when a reply entered the ROS output queue."""
        timestamp = float(now) if now is not None else time.perf_counter()
        with self._lock:
            self._emitted_at = timestamp

    def mark_first_tts(self, now: Optional[float] = None) -> None:
        """Record elapsed time from turn start until the first TTS publish."""
        timestamp = float(now) if now is not None else time.perf_counter()
        with self._lock:
            if 'time_to_tts_ms' not in self._stages_ms:
                elapsed = max(0.0, (timestamp - self.started_at) * 1000.0)
                cleaned = _clean_ms(elapsed)
                if cleaned is not None:
                    self._stages_ms['time_to_tts_ms'] = cleaned

    def mark_tts_published(self, now: Optional[float] = None) -> None:
        """Record ROS TTS handoff delay and time-to-first-speech."""
        timestamp = float(now) if now is not None else time.perf_counter()
        with self._lock:
            if self._emitted_at is not None and 'tts_handoff_ms' not in self._stages_ms:
                handoff = max(0.0, (timestamp - self._emitted_at) * 1000.0)
                cleaned_handoff = _clean_ms(handoff)
                if cleaned_handoff is not None:
                    self._stages_ms['tts_handoff_ms'] = cleaned_handoff
            if 'time_to_tts_ms' not in self._stages_ms:
                elapsed = max(0.0, (timestamp - self.started_at) * 1000.0)
                cleaned_total = _clean_ms(elapsed)
                if cleaned_total is not None:
                    self._stages_ms['time_to_tts_ms'] = cleaned_total

    def set_flag(self, name: str, value: bool) -> None:
        """Set one whitelisted boolean flag."""
        if name not in _ALLOWED_FLAGS:
            return
        with self._lock:
            self._flags[name] = bool(value)

    def set_metadata(
        self,
        *,
        outcome: Optional[str] = None,
        request_id: Optional[str] = None,
        utterance_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        decision_type: Optional[str] = None,
        tool_name: Optional[str] = None,
        safety_code: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Attach content-free routing and decision metadata."""
        with self._lock:
            if outcome in _ALLOWED_OUTCOMES:
                self._outcome = str(outcome)
            if request_id is not None:
                self._request_id = _clean_identifier(request_id)
            if utterance_id is not None:
                self._utterance_id = _clean_identifier(utterance_id)
            if conversation_id is not None:
                self._conversation_id = _clean_identifier(conversation_id)
            if decision_type is not None:
                self._decision_type = _clean_identifier(decision_type)
            if tool_name is not None:
                self._tool_name = _clean_identifier(tool_name)
            if safety_code is not None:
                self._safety_code = _clean_identifier(safety_code)
            if provider is not None:
                self._provider = _clean_identifier(provider)
            if model is not None:
                self._model = _clean_identifier(model)

    def attach_provider_result(self, provider_result: Any) -> None:
        """Extract timing and token/cache metrics from a ProviderResult."""
        if provider_result is None:
            return
        provider = _clean_identifier(getattr(provider_result, 'provider', None))
        model = _clean_identifier(getattr(provider_result, 'model', None))
        api_timing = getattr(provider_result, 'api_timing', None)
        usage = getattr(provider_result, 'usage', None)
        input_chars = getattr(provider_result, 'input_chars', None)

        extracted_stages: Dict[str, float] = {}
        extracted_usage: Dict[str, Any] = {}

        if api_timing is not None:
            for src, dst in (
                ('preparation_ms', 'provider_prep_ms'),
                ('headers_ms', 'headers_ms'),
                ('first_output_delta_ms', 'ttft_ms'),
            ):
                cleaned = _clean_ms(getattr(api_timing, src, None))
                if cleaned is not None:
                    extracted_stages[dst] = cleaned
            attempt_count = getattr(api_timing, 'attempt_count', None)
            if type(attempt_count) is int and attempt_count >= 1:
                extracted_usage['attempt_count'] = attempt_count
            retry_delay = _clean_ms(getattr(api_timing, 'retry_delay_ms', None))
            if retry_delay is not None and retry_delay > 0.0:
                extracted_usage['retry_delay_ms'] = retry_delay

        if usage is not None:
            for field_name in (
                'input_tokens',
                'output_tokens',
                'cached_input_tokens',
                'reasoning_output_tokens',
            ):
                raw_val = getattr(usage, field_name, None)
                if type(raw_val) is int and raw_val >= 0:
                    extracted_usage[field_name] = raw_val
            input_tokens = extracted_usage.get('input_tokens')
            cached_tokens = extracted_usage.get('cached_input_tokens')
            if (
                type(input_tokens) is int
                and input_tokens > 0
                and type(cached_tokens) is int
                and cached_tokens >= 0
            ):
                ratio = min(1.0, cached_tokens / float(input_tokens))
                extracted_usage['cache_hit_ratio'] = round(ratio, 4)

        if type(input_chars) is int and input_chars >= 0:
            extracted_usage['model_input_chars'] = input_chars

        with self._lock:
            if provider is not None:
                self._provider = provider
            if model is not None:
                self._model = model
            for key, value in extracted_stages.items():
                self._stages_ms.setdefault(key, value)
            if 'input_tokens' in extracted_usage:
                for token_key in (
                    'input_tokens',
                    'output_tokens',
                    'cached_input_tokens',
                    'reasoning_output_tokens',
                    'cache_hit_ratio',
                ):
                    self._usage.pop(token_key, None)
            for key, value in extracted_usage.items():
                if key in _ALLOWED_USAGE_KEYS:
                    self._usage[key] = value

    def snapshot_compact(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Return a non-finalizing compact waterfall snapshot for live logs."""
        timestamp = float(now) if now is not None else time.perf_counter()
        with self._lock:
            if self._finalized_record is not None:
                return self._finalized_record.to_compact_dict()
            stages = dict(self._stages_ms)
            if 'total_ms' not in stages:
                elapsed = _clean_ms(max(0.0, (timestamp - self.started_at) * 1000.0))
                if elapsed is not None:
                    stages['total_ms'] = elapsed
            preview = TurnWaterfallRecord(
                recorded_at=time.time(),
                channel=self.channel,
                outcome=self._outcome,
                request_id=self._request_id,
                utterance_id=self._utterance_id,
                conversation_id=self._conversation_id,
                decision_type=self._decision_type,
                tool_name=self._tool_name,
                safety_code=self._safety_code,
                provider=self._provider,
                model=self._model,
                stages_ms=stages,
                usage=dict(self._usage),
                flags=dict(self._flags),
            )
            return preview.to_compact_dict()

    def finalize(
        self,
        recorder: Optional['TurnWaterfallRecorder'] = None,
        *,
        outcome: Optional[str] = None,
        now: Optional[float] = None,
    ) -> TurnWaterfallRecord:
        """Build an immutable record once and optionally commit to a recorder."""
        finished = float(now) if now is not None else time.perf_counter()
        with self._lock:
            if self._finalized_record is not None:
                return self._finalized_record
            if outcome in _ALLOWED_OUTCOMES:
                self._outcome = str(outcome)
            if 'total_ms' not in self._stages_ms:
                elapsed = max(0.0, (finished - self.started_at) * 1000.0)
                cleaned = _clean_ms(elapsed)
                if cleaned is not None:
                    self._stages_ms['total_ms'] = cleaned
            record = TurnWaterfallRecord(
                recorded_at=time.time(),
                channel=self.channel,
                outcome=self._outcome,
                request_id=self._request_id,
                utterance_id=self._utterance_id,
                conversation_id=self._conversation_id,
                decision_type=self._decision_type,
                tool_name=self._tool_name,
                safety_code=self._safety_code,
                provider=self._provider,
                model=self._model,
                stages_ms=dict(self._stages_ms),
                usage=dict(self._usage),
                flags=dict(self._flags),
            )
            self._finalized_record = record
        if recorder is not None:
            recorder.record(record)
        return record


@contextmanager
def turn_trace_scope(trace: Optional[TurnWaterfallTrace]) -> Iterator[Optional[TurnWaterfallTrace]]:
    """Bind one active TurnWaterfallTrace to the current foreground call."""
    token = _current_trace.set(trace)
    try:
        yield trace
    finally:
        _current_trace.reset(token)


def current_turn_trace() -> Optional[TurnWaterfallTrace]:
    """Return the active foreground TurnWaterfallTrace, or None."""
    return _current_trace.get()


class TurnWaterfallRecorder:
    """Thread-safe bounded ring buffer with rolling latency percentiles."""

    def __init__(self, capacity: int = DEFAULT_TURN_METRICS_CAPACITY) -> None:
        if type(capacity) is not int or not 1 <= capacity <= MAX_TURN_METRICS_CAPACITY:
            raise ValueError(
                f'capacity must be an integer between 1 and {MAX_TURN_METRICS_CAPACITY}'
            )
        self._capacity = capacity
        self._lock = threading.Lock()
        self._entries: Deque[TurnWaterfallRecord] = deque(maxlen=capacity)
        self._recorded_total = 0

    @property
    def capacity(self) -> int:
        """Maximum number of recent turns retained in memory."""
        return self._capacity

    def record(self, entry: TurnWaterfallRecord) -> TurnWaterfallRecord:
        """Append one content-free turn waterfall record."""
        if not isinstance(entry, TurnWaterfallRecord):
            raise TypeError('entry must be a TurnWaterfallRecord')
        with self._lock:
            self._entries.append(entry)
            self._recorded_total += 1
        return entry

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return up to ``limit`` most recent records (newest first)."""
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be a positive integer')
        bounded = min(limit, self._capacity)
        with self._lock:
            items = list(self._entries)
        return [entry.to_dict() for entry in reversed(items[-bounded:])]

    def summary(self) -> Dict[str, Any]:
        """Compute rolling stage p50/p95/max/avg and Prompt Cache hit rate."""
        with self._lock:
            items = list(self._entries)
            recorded_total = self._recorded_total
        return summarize_turn_records([item.to_dict() for item in items], recorded_total)

    def snapshot(self, limit: int = 20) -> Dict[str, Any]:
        """Return a consistent summary + recent turns payload."""
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be a positive integer')
        bounded = min(limit, self._capacity)
        with self._lock:
            items = list(self._entries)
            recorded_total = self._recorded_total
        serialized = [item.to_dict() for item in items]
        return {
            'capacity': self._capacity,
            'recorded_total': recorded_total,
            'buffered': len(serialized),
            'summary': summarize_turn_records(serialized, recorded_total),
            'turns': list(reversed(serialized[-bounded:])),
        }

    def write_snapshot(self, path: str, *, limit: int = 50) -> None:
        """Atomically persist a content-free JSON snapshot for local inspection."""
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            self.snapshot(limit=limit),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        fd, temp_name = tempfile.mkstemp(
            prefix='.turn-metrics-',
            suffix='.tmp',
            dir=str(target.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(payload)
            os.replace(temp_name, target)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


def summarize_turn_records(
    records: Sequence[Dict[str, Any]],
    recorded_total: Optional[int] = None,
) -> Dict[str, Any]:
    """Aggregate rolling p50/p95/max/avg and cache hit rate over turn dicts."""
    outcomes: Counter[str] = Counter()
    stage_samples: Dict[str, List[float]] = {}
    total_input_tokens = 0
    total_cached_tokens = 0
    cache_sample_turns = 0

    for item in records:
        outcome = item.get('outcome')
        if isinstance(outcome, str):
            outcomes[outcome] += 1
        stages = item.get('stages_ms')
        if isinstance(stages, dict):
            for stage in STAGE_ORDER:
                cleaned = _clean_ms(stages.get(stage))
                if cleaned is not None:
                    stage_samples.setdefault(stage, []).append(cleaned)
        usage = item.get('usage')
        if isinstance(usage, dict):
            inp = usage.get('input_tokens')
            cached = usage.get('cached_input_tokens')
            if (
                type(inp) is int
                and inp > 0
                and type(cached) is int
                and cached >= 0
            ):
                total_input_tokens += inp
                total_cached_tokens += min(cached, inp)
                cache_sample_turns += 1

    stages_summary: Dict[str, Dict[str, Any]] = {}
    for stage in STAGE_ORDER:
        values = stage_samples.get(stage)
        if not values:
            continue
        ordered = sorted(values)
        stages_summary[stage] = {
            'count': len(ordered),
            'p50': _percentile(ordered, 0.50),
            'p95': _percentile(ordered, 0.95),
            'max': round(ordered[-1], 2),
            'avg': round(sum(ordered) / len(ordered), 2),
        }

    cache_hit_rate: Optional[float] = None
    if cache_sample_turns > 0 and total_input_tokens > 0:
        cache_hit_rate = round(total_cached_tokens / float(total_input_tokens), 4)

    return {
        'window_turns': len(records),
        'recorded_total': (
            recorded_total if recorded_total is not None else len(records)
        ),
        'outcomes': dict(sorted(outcomes.items())),
        'cache_hit_rate': cache_hit_rate,
        'stages_ms': stages_summary,
    }


def parse_waterfall_logs(lines: Iterable[str]) -> Dict[str, Any]:
    """Build a waterfall snapshot from JSON log lines containing ``waterfall``."""
    parsed: List[Dict[str, Any]] = []
    for raw_line in lines:
        start = raw_line.find('{')
        if start < 0:
            continue
        try:
            event = json.loads(raw_line[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        waterfall = event.get('waterfall')
        if not isinstance(waterfall, dict):
            continue
        stages = waterfall.get('stages_ms')
        if not isinstance(stages, dict):
            continue
        parsed.append({
            'recorded_at': event.get('recorded_at', 0.0),
            'channel': 'speech',
            'outcome': waterfall.get('outcome', 'ok'),
            'request_id': _clean_identifier(event.get('request_id')),
            'utterance_id': _clean_identifier(event.get('utterance_id')),
            'conversation_id': _clean_identifier(event.get('conversation_id')),
            'decision_type': _clean_identifier(waterfall.get('decision_type')),
            'tool_name': _clean_identifier(waterfall.get('tool_name')),
            'safety_code': _clean_identifier(waterfall.get('safety_code')),
            'provider': _clean_identifier(waterfall.get('provider')),
            'model': _clean_identifier(waterfall.get('model')),
            'stages_ms': stages,
            'usage': waterfall.get('usage') if isinstance(waterfall.get('usage'), dict) else {},
            'flags': waterfall.get('flags') if isinstance(waterfall.get('flags'), dict) else {},
        })
    return {
        'capacity': len(parsed),
        'recorded_total': len(parsed),
        'buffered': len(parsed),
        'summary': summarize_turn_records(parsed, len(parsed)),
        'turns': list(reversed(parsed)),
    }


def format_waterfall_table(snapshot: Dict[str, Any], *, limit: int = 15) -> str:
    """Format a compact terminal table of rolling percentiles and recent turns."""
    summary = snapshot.get('summary') or {}
    lines: List[str] = []
    window = summary.get('window_turns', 0)
    total = summary.get('recorded_total', window)
    outcomes = summary.get('outcomes') or {}
    outcome_text = ', '.join(f'{k}={v}' for k, v in outcomes.items()) or 'none'
    cache_rate = summary.get('cache_hit_rate')
    cache_text = f'{cache_rate * 100:.1f}%' if isinstance(cache_rate, (int, float)) else 'n/a'
    lines.append(
        f'Turn Waterfall (window={window}, total={total}, '
        f'outcomes=[{outcome_text}], prompt_cache_hit={cache_text})'
    )

    stages_summary = summary.get('stages_ms') or {}
    if stages_summary:
        lines.append('Stage Summary (ms):')
        lines.append(f'  {"stage":<20} {"n":>4} {"p50":>8} {"p95":>8} {"max":>8} {"avg":>8}')
        for stage in STAGE_ORDER:
            stats = stages_summary.get(stage)
            if not isinstance(stats, dict):
                continue
            lines.append(
                f'  {stage:<20} {int(stats["count"]):>4} '
                f'{float(stats["p50"]):>8.1f} {float(stats["p95"]):>8.1f} '
                f'{float(stats["max"]):>8.1f} {float(stats["avg"]):>8.1f}'
            )

    turns = (snapshot.get('turns') or [])[:max(1, limit)]
    if turns:
        lines.append('Recent Turns (ms, newest first):')
        header = (
            f'  {"ch":<6} {"out":<7} {"total":>7} {"queue":>6} {"scene":>6} '
            f'{"mem":>6} {"ttft":>7} {"llm":>7} {"safe":>6} {"tool":>7} '
            f'{"save":>6} {"tts":>7} {"cache":>6}  model/tool'
        )
        lines.append(header)
        for item in turns:
            stages = item.get('stages_ms') or {}
            usage = item.get('usage') or {}
            ratio = usage.get('cache_hit_ratio')
            cache_cell = f'{ratio * 100:5.0f}%' if isinstance(ratio, (int, float)) else '     -'
            label = item.get('model') or item.get('provider') or '-'
            if item.get('tool_name'):
                label = f'{label} [{item["tool_name"]}]'
            lines.append(
                f'  {str(item.get("channel", "-"))[:6]:<6} '
                f'{str(item.get("outcome", "-"))[:7]:<7} '
                f'{_format_stage_cell(stages, "total_ms", 7)} '
                f'{_format_stage_cell(stages, "queue_wait_ms", 6)} '
                f'{_format_stage_cell(stages, "scene_prepare_ms", 6)} '
                f'{_format_stage_cell(stages, "memory_snapshot_ms", 6)} '
                f'{_format_stage_cell(stages, "ttft_ms", 7)} '
                f'{_format_stage_cell(stages, "provider_ms", 7)} '
                f'{_format_stage_cell(stages, "safety_ms", 6)} '
                f'{_format_stage_cell(stages, "tool_followup_ms", 7)} '
                f'{_format_stage_cell(stages, "commit_ms", 6)} '
                f'{_format_stage_cell(stages, "time_to_tts_ms", 7)} '
                f'{cache_cell}  {label}'
            )
    return '\n'.join(lines)


def _format_stage_cell(stages: Dict[str, Any], key: str, width: int) -> str:
    val = stages.get(key)
    if isinstance(val, (int, float)):
        return f'{float(val):>{width}.1f}'
    return f'{"-":>{width}}'


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Inspect turn latency waterfalls from HTTP, a snapshot file, or stdin logs."""
    parser = argparse.ArgumentParser(
        description='Inspect Malbut agent per-turn latency waterfalls.',
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--url', help='HTTP endpoint URL (e.g. http://127.0.0.1:8000/v1/observability/turns)')
    source.add_argument('--file', help='Path to a turn-metrics JSON snapshot file')
    source.add_argument('--from-logs', action='store_true', help='Read JSON log lines from stdin')
    parser.add_argument('--token', default='', help='Bearer token for --url')
    parser.add_argument('--limit', type=int, default=15, help='Number of recent turns to display')
    parser.add_argument('--json', action='store_true', dest='as_json', help='Print raw JSON snapshot')
    args = parser.parse_args(argv)

    if args.url:
        headers = {'Authorization': f'Bearer {args.token}'} if args.token else {}
        request = urllib.request.Request(args.url, headers=headers, method='GET')
        with urllib.request.urlopen(request, timeout=5) as response:
            snapshot = json.loads(response.read().decode('utf-8'))
    elif args.file:
        snapshot = json.loads(Path(args.file).expanduser().read_text(encoding='utf-8'))
    else:
        snapshot = parse_waterfall_logs(sys.stdin)

    if args.as_json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(format_waterfall_table(snapshot, limit=args.limit))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
