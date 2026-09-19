"""CLI for scoring provider-neutral, precomputed VLM predictions."""

import argparse
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from malbut_agent_server import __version__
from malbut_agent_server.vlm_eval_metrics import build_evaluation_report
from malbut_agent_server.vlm_eval_prompt import (
    PROMPT_SHA256,
    PROMPT_VERSION,
)
from malbut_agent_server.vlm_eval_schema import (
    TRAFFIC_CLASSES,
    load_prediction_records,
    load_vlm_cases,
)


SECRET_PATTERNS = (
    re.compile(r'sk-[A-Za-z0-9_-]{8,}'),
    re.compile(
        r'(?i)((?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|PASSWORD)'
        r'\s*[=:]\s*)[^\s,;]+'
    ),
    re.compile(r'(?i)(Bearer\s+)[A-Za-z0-9._-]{8,}'),
)


def _redact_text(value: str) -> str:
    result = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            result = pattern.sub(r'\1<redacted>', result)
        else:
            result = pattern.sub('<redacted>', result)
    return result


def redact(value: Any) -> Any:
    """Recursively redact credentials before writing a report."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                '<redacted>'
                if key.lower()
                in {
                    'api_key',
                    'access_token',
                    'auth_token',
                    'password',
                    'secret',
                }
                else redact(item)
            )
            for key, item in value.items()
        }
    return value


def write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically create an owner-only JSON report."""
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{destination.name}.',
        suffix='.tmp',
        dir=str(destination.parent),
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(
                redact(dict(value)),
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write('\n')
        os.replace(temporary_name, destination)
        os.chmod(destination, 0o600)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _load_json_object(path: Optional[Path], label: str) -> Dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.expanduser().read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise ValueError(f'{label} must contain valid JSON') from error
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be a JSON object')
    return value


def _load_prices(path: Optional[Path]) -> Dict[str, Any]:
    value = _load_json_object(path, 'price catalog')
    if not value:
        return {}
    if value.get('schema_version') != 1:
        raise ValueError('price catalog schema_version must be 1')
    models = value.get('models')
    if not isinstance(models, dict):
        raise ValueError('price catalog models must be an object')
    for model_id, price in models.items():
        if not isinstance(model_id, str) or not isinstance(price, dict):
            raise ValueError('price catalog model entry is invalid')
        required = {
            'currency',
            'input_per_million',
            'output_per_million',
            'as_of',
            'source',
        }
        if not required.issubset(price):
            raise ValueError(
                f'price catalog {model_id} is missing required fields'
            )
        for field in (
            'input_per_million',
            'output_per_million',
            'cached_input_per_million',
            'per_request',
            'usd_per_currency_unit',
        ):
            amount = price.get(field)
            if amount is not None and (
                isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or amount < 0
            ):
                raise ValueError(
                    f'price catalog {model_id}.{field} is invalid'
                )
    return dict(models)


def _load_gates(path: Optional[Path]) -> List[Mapping[str, Any]]:
    value = _load_json_object(path, 'gate configuration')
    if not value:
        return []
    if value.get('schema_version') != 1:
        raise ValueError('gate configuration schema_version must be 1')
    gates = value.get('gates')
    if not isinstance(gates, list) or any(
        not isinstance(gate, dict) for gate in gates
    ):
        raise ValueError('gate configuration gates must be a list')
    return list(gates)


def _load_traffic_profile(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    value = _load_json_object(path, 'traffic profile')
    if not value:
        return None
    if value.get('schema_version') != 1:
        raise ValueError('traffic profile schema_version must be 1')
    profile = value.get('profile')
    if not isinstance(profile, dict):
        raise ValueError('traffic profile.profile must be an object')
    volumes = profile.get('clips_per_camera_day')
    if not isinstance(volumes, dict) or not volumes:
        raise ValueError(
            'traffic profile clips_per_camera_day must be an object'
        )
    if {'fall', 'found_down'} & set(volumes):
        raise ValueError(
            'traffic profile must contain only normal non-incident clips'
        )
    for traffic_class, clips_per_day in volumes.items():
        if traffic_class not in TRAFFIC_CLASSES:
            raise ValueError('traffic profile contains an unknown class')
        if (
            isinstance(clips_per_day, bool)
            or not isinstance(clips_per_day, (int, float))
            or clips_per_day < 0
        ):
            raise ValueError('traffic profile clip volume is invalid')
    budget = profile.get('false_alert_budget_per_camera_day')
    if budget is not None and (
        isinstance(budget, bool)
        or not isinstance(budget, (int, float))
        or budget < 0
    ):
        raise ValueError('traffic profile false-alert budget is invalid')
    return dict(profile)


def evaluation_exit_code(report: Mapping[str, Any]) -> int:
    """Fail incomplete harness data and configured product gates.

    Provider, schema, and semantic validity are measured model outcomes. They
    must remain available to gates and reports instead of being conflated with
    a broken evaluation harness.
    """
    runs = report.get('runs')
    if not isinstance(runs, list) or not runs:
        return 2
    for run in runs:
        if not isinstance(run, dict):
            return 2
        if run.get('received') != run.get('attempted'):
            return 2
        gate_status = run.get('gates', {}).get('overall')
        if gate_status == 'fail':
            return 3
        if gate_status == 'hold':
            return 4
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Score fixed Malbut home-camera clips from provider-neutral '
            'VLM prediction JSONL.'
        ),
    )
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--predictions', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prices', type=Path)
    parser.add_argument('--gates', type=Path)
    parser.add_argument('--traffic-profile', type=Path)
    parser.add_argument(
        '--expected-prompt-version',
        default=PROMPT_VERSION,
    )
    parser.add_argument(
        '--expected-prompt-sha256',
        default=PROMPT_SHA256,
    )
    parser.add_argument(
        '--require-media',
        action='store_true',
        help='Require every media file and verify declared SHA-256.',
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Load fixed inputs, calculate metrics, and write a private report."""
    args = _parser().parse_args(argv)
    cases = load_vlm_cases(
        args.manifest.expanduser(),
        require_media=args.require_media,
    )
    records = load_prediction_records(
        args.predictions.expanduser(),
        cases,
    )
    for record in records:
        if record.prompt_version != args.expected_prompt_version:
            raise ValueError('prediction prompt version does not match')
        if record.prompt_sha256 != args.expected_prompt_sha256:
            raise ValueError('prediction prompt SHA-256 does not match')
    report = build_evaluation_report(
        cases,
        records,
        prices=_load_prices(args.prices),
        gates=_load_gates(args.gates),
        traffic_profile=_load_traffic_profile(args.traffic_profile),
    )
    report['generated_at'] = datetime.now(timezone.utc).isoformat()
    report['package_version'] = __version__
    report['prompt_contract'] = {
        'version': args.expected_prompt_version,
        'sha256': args.expected_prompt_sha256,
    }
    write_private_json(args.output, report)

    for run in report['runs']:
        fall = run['metrics']['fall']['all_recall']
        latency = run['metrics']['telemetry']['total_latency_ms']['p95']
        print(
            f"{run['model']['id']} "
            f"track={run['input']['track']} "
            f"fall_recall={fall['value']} "
            f"ci95=[{fall['ci95']['lower']},"
            f"{fall['ci95']['upper']}] "
            f"p95_ms={latency} "
            f"gates={run['gates']['overall']}"
        )
    print(f'report: {args.output.expanduser()}')
    return evaluation_exit_code(report)


if __name__ == '__main__':
    raise SystemExit(main())
