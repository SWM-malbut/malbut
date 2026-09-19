"""Generate provider-neutral VLM predictions from a fixed video manifest."""

import argparse
from contextlib import contextmanager
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
)

from malbut_agent_server.domain.vlm import (
    AuroraDepthEvidence,
    FallCandidateKind,
    PhysicalFallEvidence,
    RecoveryState,
    ResponseState,
    VlmAnalysisRequest,
    VlmMedia,
)
from malbut_agent_server.ports.vlm_provider import (
    VlmProvider,
    VlmProviderError,
)
from malbut_agent_server.vlm_eval_prompt import (
    PROMPT_SHA256,
    PROMPT_VERSION,
)
from malbut_agent_server.vlm_eval_schema import (
    VlmEvaluationCase,
    load_vlm_cases,
)
from malbut_agent_server.vlm_factory import VlmSettings, create_vlm_provider


OBSERVATION_FIELDS = frozenset(
    {
        'schema_version',
        'case_id',
        'candidate_kind',
        'descent_score',
        'floor_proximity_score',
        'body_visibility',
        'robot_motion',
        'recovery',
        'response',
        'pose',
        'yolo',
        'depth',
        'source',
    }
)


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a number')
    return float(value)


def _enum(enum_type: Any, value: Any, name: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name} is unsupported') from error


def _parse_depth(value: Any, case_id: str) -> Optional[AuroraDepthEvidence]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f'{case_id}: depth must be an object or null')
    required = {
        'aligned_to_rgb',
        'stale',
        'valid_torso_ratio',
        'torso_floor_distance_m',
        'person_distance_m',
        'sampled_torso_points',
    }
    if set(value) != required:
        raise ValueError(f'{case_id}: depth fields do not match the contract')
    if not isinstance(value['aligned_to_rgb'], bool):
        raise ValueError(f'{case_id}: depth.aligned_to_rgb must be boolean')
    if not isinstance(value['stale'], bool):
        raise ValueError(f'{case_id}: depth.stale must be boolean')
    sampled = value['sampled_torso_points']
    if isinstance(sampled, bool) or not isinstance(sampled, int):
        raise ValueError(
            f'{case_id}: depth.sampled_torso_points must be an integer'
        )
    return AuroraDepthEvidence(
        aligned_to_rgb=value['aligned_to_rgb'],
        stale=value['stale'],
        valid_torso_ratio=_number(
            value['valid_torso_ratio'],
            f'{case_id}: depth.valid_torso_ratio',
        ),
        torso_floor_distance_m=(
            None
            if value['torso_floor_distance_m'] is None
            else _number(
                value['torso_floor_distance_m'],
                f'{case_id}: depth.torso_floor_distance_m',
            )
        ),
        person_distance_m=(
            None
            if value['person_distance_m'] is None
            else _number(
                value['person_distance_m'],
                f'{case_id}: depth.person_distance_m',
            )
        ),
        sampled_torso_points=sampled,
    )


def parse_observation(value: Mapping[str, Any]) -> PhysicalFallEvidence:
    """Parse one label-free local observation used by every candidate."""
    if not isinstance(value, dict):
        raise ValueError('observation must be an object')
    case_id = value.get('case_id')
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError('observation.case_id must be a non-empty string')
    unknown = set(value) - OBSERVATION_FIELDS
    if unknown:
        raise ValueError(
            f'{case_id}: observation has unknown fields: '
            + ', '.join(sorted(unknown))
        )
    if value.get('schema_version') != 2:
        raise ValueError(f'{case_id}: observation schema_version must be 2')
    pose = value.get('pose', {})
    yolo = value.get('yolo', {})
    if not isinstance(pose, dict) or not isinstance(yolo, dict):
        raise ValueError(f'{case_id}: pose and yolo must be objects')
    source = value.get('source')
    source_fields = {
        'producer',
        'version',
        'config_sha256',
        'artifact_sha256',
    }
    if not isinstance(source, dict) or set(source) != source_fields:
        raise ValueError(f'{case_id}: source fields do not match contract')
    for name in ('producer', 'version'):
        if not isinstance(source[name], str) or not source[name].strip():
            raise ValueError(f'{case_id}: source.{name} must be non-empty')
    for name in ('config_sha256', 'artifact_sha256'):
        digest = source[name]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in '0123456789abcdef' for character in digest)
        ):
            raise ValueError(f'{case_id}: source.{name} is invalid')
    return PhysicalFallEvidence(
        candidate_kind=_enum(
            FallCandidateKind,
            value.get('candidate_kind'),
            f'{case_id}: candidate_kind',
        ),
        descent_score=_number(
            value.get('descent_score'),
            f'{case_id}: descent_score',
        ),
        floor_proximity_score=_number(
            value.get('floor_proximity_score'),
            f'{case_id}: floor_proximity_score',
        ),
        body_visibility=str(value.get('body_visibility', '')),
        robot_motion=str(value.get('robot_motion', '')),
        recovery=_enum(
            RecoveryState,
            value.get('recovery', 'unknown'),
            f'{case_id}: recovery',
        ),
        response=_enum(
            ResponseState,
            value.get('response', 'not_asked'),
            f'{case_id}: response',
        ),
        depth=_parse_depth(value.get('depth'), case_id),
        pose_summary=pose,
        yolo_summary=yolo,
        source_provenance=source,
    )


def load_observations(
    path: Path,
    cases: Sequence[VlmEvaluationCase],
) -> Dict[str, PhysicalFallEvidence]:
    """Load exactly one observation for each case without reading GT labels."""
    by_case: Dict[str, PhysicalFallEvidence] = {}
    expected = {case.case_id for case in cases}
    for line_number, raw in enumerate(
        path.expanduser().read_text(encoding='utf-8').splitlines(),
        start=1,
    ):
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(
                f'{path}: invalid JSONL at line {line_number}'
            ) from error
        if not isinstance(value, dict):
            raise ValueError(f'{path}: line {line_number} must be an object')
        case_id = value.get('case_id')
        if case_id not in expected:
            raise ValueError(
                f'observation references unknown case: {case_id}'
            )
        if case_id in by_case:
            raise ValueError(f'duplicate observation for case: {case_id}')
        by_case[str(case_id)] = parse_observation(value)
    missing = sorted(expected - set(by_case))
    if missing:
        raise ValueError(
            'missing observations for cases: ' + ', '.join(missing)
        )
    return by_case


def _media_for_case(case: VlmEvaluationCase, manifest: Path) -> VlmMedia:
    if not case.media_path:
        raise ValueError(f'{case.case_id}: clip.path is missing')
    path = Path(case.media_path).expanduser()
    if not path.is_absolute():
        path = manifest.resolve().parent / path
    suffix = path.suffix.lower().lstrip('.')
    return VlmMedia(
        video_format=suffix,
        local_path=str(path),
        sha256=case.media_sha256,
    )


def _input_spec(
    settings: VlmSettings,
    *,
    sampling: str,
    effective_fps: Optional[float],
    resolution: str,
    audio_included: bool,
    preprocessing_sha256: Optional[str],
) -> Dict[str, Any]:
    modes = {
        'nova': 'tool_use',
        'qwen': 'json_object_with_local_schema_validation',
    }
    mode = modes.get(settings.provider, 'json_schema')
    return {
        'sampling': sampling,
        'effective_fps': effective_fps,
        'frame_count': None,
        'resolution': resolution,
        'audio_included': audio_included,
        'preprocessing_sha256': preprocessing_sha256,
        'structured_output_mode': mode,
        'decoding': {
            'temperature': 0.00001 if settings.provider == 'nova' else 0,
            'max_output_tokens': settings.max_output_tokens,
            'reasoning_mode': (
                'low'
                if settings.provider == 'gemini'
                else (
                    'off'
                    if settings.provider == 'qwen'
                    else 'provider_default'
                )
            ),
        },
    }


def generate_prediction_records(
    *,
    manifest: Path,
    cases: Sequence[VlmEvaluationCase],
    observations: Mapping[str, PhysicalFallEvidence],
    provider: VlmProvider,
    settings: VlmSettings,
    repetitions: int,
    input_spec: Mapping[str, Any],
    context_variant: str,
    on_record: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """Call one interchangeable adapter without exposing ground truth."""
    records: List[Dict[str, Any]] = []
    provider_name = str(
        getattr(provider, 'provider_name', getattr(provider, 'name', 'vlm'))
    )
    model_id = str(getattr(provider, 'model_id', settings.model_id))
    region = str(getattr(provider, 'region', settings.region))
    runtime = (
        'self-hosted'
        if settings.provider == 'openai_compatible'
        else 'remote-api'
    )
    for repetition in range(1, repetitions + 1):
        for case in cases:
            request_id = str(uuid.uuid4())
            request = VlmAnalysisRequest(
                request_id=request_id,
                incident_id=f'eval-{case.case_id}',
                duration_s=case.duration_s,
                media=_media_for_case(case, manifest),
                evidence=observations[case.case_id],
                context_variant=context_variant,
            )
            prediction = None
            error_type = None
            result = None
            try:
                result = provider.analyze(request)
                prediction = dict(result.prediction)
            except VlmProviderError as error:
                error_type = str(error) or 'provider_error'
            except Exception as error:
                error_type = f'unexpected_{type(error).__name__}'
            record = {
                    'schema_version': 1,
                    'request_id': request_id,
                    'case_id': case.case_id,
                    'repetition': repetition,
                    'model': {
                        'provider': (
                            result.provider if result else provider_name
                        ),
                        'id': result.model_id if result else model_id,
                        'version': (
                            result.model_version
                            if result
                            else str(
                                getattr(provider, 'model_version', model_id)
                            )
                        ),
                        'region': result.region if result else region,
                        'runtime': runtime,
                    },
                    'input': {
                        'track': 'V',
                        'context_variant': context_variant,
                        'prompt_version': PROMPT_VERSION,
                        'prompt_sha256': PROMPT_SHA256,
                        'observation_sha256': (
                            observations[case.case_id].contract_sha256
                        ),
                        'input_spec': dict(input_spec),
                    },
                    'prediction': prediction,
                    'request_succeeded': result is not None,
                    'error_type': error_type,
                    'telemetry': {
                        'total_latency_ms': (
                            result.latency_ms if result else None
                        ),
                        'first_token_latency_ms': None,
                        'input_tokens': (
                            result.input_tokens if result else None
                        ),
                        'output_tokens': (
                            result.output_tokens if result else None
                        ),
                        'cached_input_tokens': 0 if result else None,
                        'retry_count': 0,
                    },
                }
            records.append(record)
            if on_record is not None:
                on_record(record)
    return records


@contextmanager
def private_jsonl_journal(
    path: Path,
) -> Iterator[Callable[[Mapping[str, Any]], None]]:
    """Create a durable owner-only journal for billable model results.

    Every completed provider attempt is flushed before the next call starts.
    If the process is interrupted, the destination remains valid JSONL with
    all previously completed attempts instead of losing the whole batch.
    """
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    stream = os.fdopen(descriptor, 'w', encoding='utf-8')

    def append(row: Mapping[str, Any]) -> None:
        stream.write(
            json.dumps(
                dict(row),
                ensure_ascii=False,
                separators=(',', ':'),
            )
            + '\n'
        )
        stream.flush()
        os.fsync(stream.fileno())

    try:
        yield append
    finally:
        stream.close()


def write_private_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Atomically write owner-only prediction records."""
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f'.{destination.name}.',
        suffix='.tmp',
        dir=str(destination.parent),
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            for row in rows:
                stream.write(
                    json.dumps(
                        dict(row),
                        ensure_ascii=False,
                        separators=(',', ':'),
                    )
                    + '\n'
                )
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Invoke one replaceable VLM over a fixed private dataset.',
    )
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--observations', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument(
        '--context-variant',
        choices=('C0', 'C1', 'C2'),
        default='C2',
    )
    parser.add_argument('--sampling', default='provider-native-video')
    parser.add_argument('--effective-fps', type=float)
    parser.add_argument('--resolution', default='source')
    parser.add_argument('--audio-included', action='store_true')
    parser.add_argument('--preprocessing-sha256')
    parser.add_argument(
        '--execute',
        action='store_true',
        help=(
            'Perform billable/network model calls. Without this, '
            'validate only.'
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Validate inputs by default; call the configured provider explicitly."""
    args = _parser().parse_args(argv)
    if not 1 <= args.repetitions <= 20:
        raise ValueError('repetitions must be between 1 and 20')
    if args.preprocessing_sha256 is not None and (
        len(args.preprocessing_sha256) != 64
        or any(
            character not in '0123456789abcdef'
            for character in args.preprocessing_sha256
        )
    ):
        raise ValueError('preprocessing_sha256 must be lowercase SHA-256')
    cases = load_vlm_cases(
        args.manifest.expanduser(),
        require_media=args.execute,
    )
    observations = load_observations(args.observations, cases)
    settings = VlmSettings.from_env()
    spec = _input_spec(
        settings,
        sampling=args.sampling,
        effective_fps=args.effective_fps,
        resolution=args.resolution,
        audio_included=args.audio_included,
        preprocessing_sha256=args.preprocessing_sha256,
    )
    if not args.execute:
        print(
            f'VALID cases={len(cases)} provider={settings.provider} '
            f'model={settings.model_id} prompt={PROMPT_VERSION}'
        )
        return 0
    if args.output is None:
        raise ValueError('--output is required with --execute')
    provider = create_vlm_provider(settings)
    with private_jsonl_journal(args.output) as checkpoint:
        records = generate_prediction_records(
            manifest=args.manifest.expanduser(),
            cases=cases,
            observations=observations,
            provider=provider,
            settings=settings,
            repetitions=args.repetitions,
            input_spec=spec,
            context_variant=args.context_variant,
            on_record=checkpoint,
        )
    succeeded = sum(row['request_succeeded'] for row in records)
    print(
        f'completed={len(records)} succeeded={succeeded} '
        f'failed={len(records) - succeeded} output={args.output.expanduser()}'
    )
    return 0 if succeeded == len(records) else 2


if __name__ == '__main__':
    raise SystemExit(main())
