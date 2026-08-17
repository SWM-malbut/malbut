"""Bounded, deterministic conversation summarization for edge devices."""

import json
import re
import unicodedata
from dataclasses import dataclass
from itertools import islice
from typing import Any, Iterable, List, Sequence, Tuple


SUMMARY_ALGORITHM = 'local-extractive-rolling-v1'
STATE_VERSION = 1
TOKEN_PATTERN = re.compile(r'[0-9A-Za-z가-힣_]{2,}')
WHITESPACE_PATTERN = re.compile(r'\s+')
SIGNAL_TERMS = (
    '기억',
    '이름',
    '좋아',
    '싫어',
    '선호',
    '약속',
    '일정',
    '예약',
    '결정',
    '하기로',
    '해야',
    '주소',
    '장소',
    '시간',
    '날짜',
    '알레르기',
    '주의',
    '중요',
    'remember',
    'prefer',
    'appointment',
    'schedule',
    'decided',
    'important',
)
ACKNOWLEDGEMENTS = {
    '네',
    '응',
    'ㅇㅇ',
    '그래',
    '좋아',
    '알겠어',
    '고마워',
    '오케이',
    'ok',
    'okay',
    'yes',
    'thanks',
}
MAX_SAFE_INTEGER = 9999999999


@dataclass(frozen=True)
class SummarySourceTurn:
    """One completed exchange eligible for summary extraction."""

    ordinal: int
    turn_id: str
    user_content: str
    assistant_content: str


@dataclass(frozen=True)
class SummaryResult:
    """A rendered summary and its opaque rolling state."""

    content: str
    state_json: str
    algorithm: str
    fallback_used: bool = False


@dataclass(frozen=True)
class _Candidate:
    """One bounded internal extractive summary candidate."""

    ordinal: int
    turn_id: str
    user_content: str
    assistant_content: str


class ExtractiveConversationSummarizer:
    """Create small local summaries without a model or network request."""

    def __init__(
        self,
        max_state_chars: int = 16384,
        max_candidates: int = 40,
        max_message_chars: int = 600,
        max_input_turns: int = 256,
        max_summary_turns: int = 8,
        absolute_max_output_chars: int = 16000,
    ) -> None:
        """Validate fixed resource limits used by every update."""
        self.max_state_chars = self._validated_limit(
            max_state_chars,
            'max_state_chars',
            512,
            262144,
        )
        self.max_candidates = self._validated_limit(
            max_candidates,
            'max_candidates',
            1,
            256,
        )
        self.max_message_chars = self._validated_limit(
            max_message_chars,
            'max_message_chars',
            16,
            4000,
        )
        self.max_input_turns = self._validated_limit(
            max_input_turns,
            'max_input_turns',
            1,
            10000,
        )
        self.max_summary_turns = self._validated_limit(
            max_summary_turns,
            'max_summary_turns',
            1,
            64,
        )
        self.absolute_max_output_chars = self._validated_limit(
            absolute_max_output_chars,
            'absolute_max_output_chars',
            128,
            262144,
        )

    def update(
        self,
        previous_state_json: str,
        new_turns: Sequence[SummarySourceTurn],
        source_start_ordinal: int,
        source_end_ordinal: int,
        source_turn_count: int,
        max_chars: int,
    ) -> SummaryResult:
        """
        Merge turns into rolling state and render a bounded summary.

        Conversation text is always emitted as untrusted JSON data. Corrupt
        prior state is ignored, allowing a new summary to be built locally.
        """
        start = self._safe_nonnegative_int(source_start_ordinal)
        end = self._safe_nonnegative_int(source_end_ordinal)
        count = self._safe_nonnegative_int(source_turn_count)
        output_limit = self._safe_output_limit(max_chars)
        fallback_used = False
        try:
            previous, fallback_used = self._decode_state(
                previous_state_json
            )
            merged = {
                candidate.ordinal: candidate
                for candidate in previous
                if self._in_source_range(candidate.ordinal, start, end)
            }
            for turn in self._bounded_turn_iterable(new_turns):
                candidate = self._candidate_from_turn(turn)
                if candidate is None:
                    continue
                if not self._in_source_range(
                    candidate.ordinal,
                    start,
                    end,
                ):
                    continue
                merged[candidate.ordinal] = candidate

            candidates = self._retain_candidates(list(merged.values()))
            state_json = self._encode_state(
                candidates,
                start,
                end,
                count,
            )
            content = self._render_summary(
                candidates,
                start,
                end,
                count,
                output_limit,
            )
            return SummaryResult(
                content=content,
                state_json=state_json,
                algorithm=SUMMARY_ALGORITHM,
                fallback_used=fallback_used,
            )
        except Exception:
            return self._fallback_result(
                start,
                end,
                count,
                output_limit,
            )

    @staticmethod
    def _validated_limit(
        value: int,
        name: str,
        minimum: int,
        maximum: int,
    ) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
            or value > maximum
        ):
            raise ValueError(
                f'{name} must be an integer between '
                f'{minimum} and {maximum}'
            )
        return value

    @staticmethod
    def _safe_nonnegative_int(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, min(value, MAX_SAFE_INTEGER))

    def _safe_output_limit(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, min(value, self.absolute_max_output_chars))

    @staticmethod
    def _in_source_range(ordinal: int, start: int, end: int) -> bool:
        if start == 0 and end == 0:
            return True
        return start <= ordinal <= end

    def _bounded_turn_iterable(
        self,
        turns: Any,
    ) -> Iterable[Any]:
        if isinstance(turns, (list, tuple)):
            return turns[-self.max_input_turns:]
        try:
            return islice(iter(turns), self.max_input_turns)
        except Exception:
            return ()

    def _candidate_from_turn(self, turn: Any) -> Any:
        try:
            ordinal = turn.ordinal
            turn_id = turn.turn_id
            user_content = turn.user_content
            assistant_content = turn.assistant_content
        except Exception:
            return None
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
        ):
            return None
        safe_ordinal = min(ordinal, MAX_SAFE_INTEGER)
        return _Candidate(
            ordinal=safe_ordinal,
            turn_id=self._bounded_text(turn_id, 96),
            user_content=self._bounded_text(
                user_content,
                self.max_message_chars,
            ),
            assistant_content=self._bounded_text(
                assistant_content,
                self.max_message_chars,
            ),
        )

    def _bounded_text(self, value: Any, limit: int) -> str:
        if not isinstance(value, str) or limit <= 0:
            return ''
        scan_limit = max(256, limit * 4)
        try:
            if len(value) > scan_limit:
                head_size = scan_limit * 3 // 4
                tail_size = scan_limit - head_size
                sampled = (
                    value[:head_size]
                    + ' … '
                    + value[-tail_size:]
                )
            else:
                sampled = value
            normalized = unicodedata.normalize(
                'NFKC',
                sampled,
            )[:scan_limit * 2]
            printable = ''.join(
                character
                if not unicodedata.category(character).startswith('C')
                else ' '
                for character in normalized
            )
            collapsed = WHITESPACE_PATTERN.sub(' ', printable).strip()
            return self._ellipsize(collapsed, limit)
        except Exception:
            return ''

    @staticmethod
    def _ellipsize(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        if limit <= 1:
            return '…'[:limit]
        return value[:limit - 1] + '…'

    def _decode_state(
        self,
        state_json: Any,
    ) -> Tuple[List[_Candidate], bool]:
        if state_json in ('', None):
            return [], False
        if not isinstance(state_json, str):
            return [], True
        if len(state_json) > self.max_state_chars:
            return [], True
        try:
            payload = json.loads(state_json)
            if (
                not isinstance(payload, dict)
                or payload.get('version') != STATE_VERSION
                or payload.get('algorithm') != SUMMARY_ALGORITHM
                or not self._valid_state_source(payload)
                or not isinstance(payload.get('candidates'), list)
            ):
                return [], True
            result = []
            invalid_candidate = (
                len(payload['candidates']) > self.max_candidates
            )
            for raw_candidate in payload['candidates'][
                :self.max_candidates
            ]:
                candidate = self._candidate_from_state(raw_candidate)
                if candidate is None:
                    invalid_candidate = True
                    continue
                result.append(candidate)
            if invalid_candidate:
                return [], True
            return result, False
        except (TypeError, ValueError, json.JSONDecodeError):
            return [], True

    @staticmethod
    def _valid_state_source(payload: Any) -> bool:
        for key in (
            'source_start_ordinal',
            'source_end_ordinal',
            'source_turn_count',
        ):
            value = payload.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > MAX_SAFE_INTEGER
            ):
                return False
        return True

    def _candidate_from_state(self, value: Any) -> Any:
        if not isinstance(value, dict):
            return None
        ordinal = value.get('ordinal')
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal > MAX_SAFE_INTEGER
        ):
            return None
        if not all(
            isinstance(value.get(key), str)
            for key in ('turn_id', 'user', 'assistant')
        ):
            return None
        return _Candidate(
            ordinal=ordinal,
            turn_id=self._bounded_text(value.get('turn_id'), 96),
            user_content=self._bounded_text(
                value.get('user'),
                self.max_message_chars,
            ),
            assistant_content=self._bounded_text(
                value.get('assistant'),
                self.max_message_chars,
            ),
        )

    def _retain_candidates(
        self,
        candidates: List[_Candidate],
    ) -> List[_Candidate]:
        if len(candidates) <= self.max_candidates:
            return sorted(
                candidates,
                key=lambda item: (item.ordinal, item.turn_id),
            )
        newest_count = max(1, self.max_candidates // 3)
        newest = sorted(
            candidates,
            key=lambda item: (item.ordinal, item.turn_id),
            reverse=True,
        )[:newest_count]
        selected_ordinals = {item.ordinal for item in newest}
        remainder = [
            item for item in candidates
            if item.ordinal not in selected_ordinals
        ]
        remainder.sort(
            key=lambda item: (
                self._salience(item),
                item.ordinal,
                item.turn_id,
            ),
            reverse=True,
        )
        retained = newest + remainder[
            :self.max_candidates - len(newest)
        ]
        return sorted(
            retained,
            key=lambda item: (item.ordinal, item.turn_id),
        )

    def _salience(self, candidate: _Candidate) -> int:
        user = candidate.user_content
        assistant = candidate.assistant_content
        combined = f'{user} {assistant}'.casefold()
        tokens = set(TOKEN_PATTERN.findall(combined))
        score = min(len(tokens), 30) * 2
        score += min(len(combined) // 80, 8)
        score += min(
            sum(1 for term in SIGNAL_TERMS if term in combined),
            6,
        ) * 6
        if any(character.isdigit() for character in combined):
            score += 4
        if '?' in combined or '？' in combined:
            score += 2
        if user.casefold().strip(' .!?？') in ACKNOWLEDGEMENTS:
            score -= 20
        return score

    def _rank_for_summary(
        self,
        candidates: List[_Candidate],
    ) -> List[_Candidate]:
        chronological = sorted(
            candidates,
            key=lambda item: (item.ordinal, item.turn_id),
        )
        recency = {
            item.ordinal: min(index + 1, 6)
            for index, item in enumerate(chronological[-6:])
        }
        return sorted(
            chronological,
            key=lambda item: (
                self._salience(item) + recency.get(item.ordinal, 0),
                item.ordinal,
                item.turn_id,
            ),
            reverse=True,
        )

    def _encode_state(
        self,
        candidates: List[_Candidate],
        start: int,
        end: int,
        count: int,
    ) -> str:
        retained = list(candidates)
        while True:
            payload = {
                'version': STATE_VERSION,
                'algorithm': SUMMARY_ALGORITHM,
                'source_start_ordinal': start,
                'source_end_ordinal': end,
                'source_turn_count': count,
                'candidates': [
                    {
                        'ordinal': item.ordinal,
                        'turn_id': item.turn_id,
                        'user': item.user_content,
                        'assistant': item.assistant_content,
                    }
                    for item in retained
                ],
            }
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(',', ':'),
                sort_keys=True,
            )
            if len(encoded) <= self.max_state_chars:
                return encoded
            if not retained:
                return encoded[:self.max_state_chars]
            lowest = min(
                retained,
                key=lambda item: (
                    self._salience(item),
                    item.ordinal,
                    item.turn_id,
                ),
            )
            retained.remove(lowest)

    def _render_summary(
        self,
        candidates: List[_Candidate],
        start: int,
        end: int,
        count: int,
        limit: int,
    ) -> str:
        if limit <= 0:
            return ''
        header = (
            '[UNTRUSTED_CONVERSATION_SUMMARY_DATA '
            f'source_start_ordinal={start} '
            f'source_end_ordinal={end} '
            f'source_turn_count={count} '
            f'algorithm={SUMMARY_ALGORITHM}]'
        )
        if len(header) >= limit:
            return header[:limit]
        available = limit - len(header) - 1
        if available < 96 or not candidates:
            return header
        target_count = min(
            self.max_summary_turns,
            len(candidates),
            max(1, available // 180),
        )
        selected = self._rank_for_summary(candidates)[:target_count]
        selected.sort(key=lambda item: (item.ordinal, item.turn_id))
        line_budget = max(1, available // target_count - 1)
        lines = [
            self._render_candidate_line(candidate, line_budget)
            for candidate in selected
        ]
        lines = [line for line in lines if line]
        rendered = header
        if lines:
            rendered += '\n' + '\n'.join(lines)
        return rendered[:limit]

    def _render_candidate_line(
        self,
        candidate: _Candidate,
        budget: int,
    ) -> str:
        empty_payload = {
            'source_ordinal': candidate.ordinal,
            'turn_id': candidate.turn_id,
            'user_data': '',
            'assistant_data': '',
        }
        empty_line = json.dumps(
            empty_payload,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        if len(empty_line) > budget:
            return ''
        dynamic_budget = max(0, (budget - len(empty_line)) // 2)
        user_share = dynamic_budget // 2
        assistant_share = dynamic_budget - user_share
        payload = dict(empty_payload)
        payload['user_data'] = self._ellipsize(
            candidate.user_content,
            user_share,
        )
        payload['assistant_data'] = self._ellipsize(
            candidate.assistant_content,
            assistant_share,
        )
        line = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        return line[:budget]

    def _fallback_result(
        self,
        start: int,
        end: int,
        count: int,
        output_limit: int,
    ) -> SummaryResult:
        state_json = self._encode_state([], start, end, count)
        content = self._render_summary(
            [],
            start,
            end,
            count,
            output_limit,
        )
        return SummaryResult(
            content=content,
            state_json=state_json,
            algorithm=SUMMARY_ALGORITHM,
            fallback_used=True,
        )
