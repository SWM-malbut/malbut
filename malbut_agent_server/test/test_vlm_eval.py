"""Offline tests for the provider-neutral home-camera VLM evaluator."""

import json
import stat
from pathlib import Path

import pytest

from malbut_agent_server.vlm_eval_metrics import (
    build_evaluation_report,
    match_events,
    pareto_frontier,
    temporal_iou,
    wilson_interval,
)
from malbut_agent_server.vlm_eval_prompt import (
    PREDICTION_JSON_SCHEMA,
    PROMPT_SHA256,
    PROMPT_VERSION,
    build_user_prompt,
)
from malbut_agent_server.vlm_eval_runner import (
    evaluation_exit_code,
    main,
    write_private_json,
)
from malbut_agent_server.vlm_eval_schema import (
    GroundTruthEvent,
    PredictedEvent,
    VlmEvaluationCase,
    load_prediction_records,
    load_vlm_cases,
    validate_prediction,
)


DATA = (
    Path(__file__).resolve().parents[1]
    / 'malbut_agent_server'
    / 'data'
)
MANIFEST = DATA / 'vlm_eval_pilot_sample.jsonl'
PREDICTIONS = DATA / 'vlm_eval_predictions_sample.jsonl'
PRICES = DATA / 'vlm_eval_prices_sample.json'
GATES = DATA / 'vlm_eval_gates_sample.json'
TRAFFIC = DATA / 'vlm_eval_traffic_sample.json'


def _prediction() -> dict:
    return {
        'subjects': {'person': 1, 'pet': 0, 'other': 0},
        'events': [
            {
                'type': 'fall',
                'subject': 'person',
                'start_s': 3.0,
                'end_s': 4.0,
                'confidence': 0.9,
            }
        ],
        'fall': {
            'assessment': 'confirmed_fall',
            'confidence': 0.9,
            'recovery': 'not_recovered',
        },
        'posture_end': 'lying_floor',
        'risk': 'urgent',
        'risk_confidence': 0.9,
        'camera_motion_observed': 'none',
        'explanation_ko': '사람이 넘어져 바닥에 누워 있습니다.',
        'evidence_ko': ['급격한 하강이 관찰됩니다.'],
        'uncertainty_flags': [],
    }


def test_sample_manifest_is_versioned_and_infers_scoring_contract() -> None:
    cases = load_vlm_cases(MANIFEST)
    assert [case.case_id for case in cases] == ['P001', 'P002', 'P003']
    assert cases[0].has_clear_fall is True
    assert cases[1].events[0].event_type == 'lie_down_floor'
    assert cases[2].robot_motion == 'whole'


def test_prompt_contract_and_context_serialization_are_stable() -> None:
    assert PROMPT_VERSION == 'malbut-homecam-vlm-v3'
    assert len(PROMPT_SHA256) == 64
    assert PREDICTION_JSON_SCHEMA['additionalProperties'] is False
    first = build_user_prompt(10, yolo_context={'b': 2, 'a': 1})
    second = build_user_prompt(10, yolo_context={'a': 1, 'b': 2})
    assert first == second


def test_manifest_can_require_real_media_and_hash(tmp_path: Path) -> None:
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'fixed-test-media')
    import hashlib

    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    row = json.loads(MANIFEST.read_text(encoding='utf-8').splitlines()[0])
    row['clip']['path'] = media.name
    row['clip']['sha256'] = digest
    manifest = tmp_path / 'manifest.jsonl'
    manifest.write_text(json.dumps(row) + '\n', encoding='utf-8')
    loaded = load_vlm_cases(manifest, require_media=True)
    assert loaded[0].media_sha256 == digest

    media.write_bytes(b'changed')
    with pytest.raises(ValueError, match='SHA-256'):
        load_vlm_cases(manifest, require_media=True)


def test_require_media_rejects_manifest_without_hash(tmp_path: Path) -> None:
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'unsealed-media')
    row = json.loads(MANIFEST.read_text(encoding='utf-8').splitlines()[0])
    row['clip']['path'] = media.name
    row['clip'].pop('sha256', None)
    manifest = tmp_path / 'manifest.jsonl'
    manifest.write_text(json.dumps(row) + '\n', encoding='utf-8')

    with pytest.raises(ValueError, match='clip.sha256 is required'):
        load_vlm_cases(manifest, require_media=True)


def test_found_down_is_separate_from_observed_fall_ground_truth(
    tmp_path: Path,
) -> None:
    row = json.loads(MANIFEST.read_text(encoding='utf-8').splitlines()[0])
    row['schema_version'] = 2
    row['events'] = []
    row['fall_assessment_gt'] = 'found_down'
    row['traffic_class'] = 'found_down'
    manifest = tmp_path / 'found-down.jsonl'
    manifest.write_text(json.dumps(row) + '\n', encoding='utf-8')

    case = load_vlm_cases(manifest)[0]
    assert case.has_fall is False
    assert case.fall_assessment == 'found_down'

    row['events'] = [
        {
            'event_id': 'observed-descent',
            'subject_ref': 'person',
            'type': 'fall',
            'start_s': 1.0,
            'end_s': 2.0,
            'fall_tier': 'ambiguous',
        }
    ]
    manifest.write_text(json.dumps(row) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='cannot contain'):
        load_vlm_cases(manifest)


def test_strict_prediction_schema_separates_semantic_errors() -> None:
    case = load_vlm_cases(MANIFEST)[0]
    valid, schema_errors, semantic_errors = validate_prediction(
        _prediction(),
        case.duration_s,
    )
    assert valid is not None
    assert schema_errors == []
    assert semantic_errors == []

    structurally_invalid = _prediction()
    structurally_invalid['unexpected'] = True
    result, schema_errors, semantic_errors = validate_prediction(
        structurally_invalid,
        case.duration_s,
    )
    assert result is None
    assert schema_errors
    assert semantic_errors == []

    confidence_invalid = _prediction()
    confidence_invalid['fall']['confidence'] = 1.2
    result, schema_errors, semantic_errors = validate_prediction(
        confidence_invalid,
        case.duration_s,
    )
    assert result is None
    assert schema_errors
    assert semantic_errors == []

    semantically_invalid = _prediction()
    semantically_invalid['events'][0]['end_s'] = 99
    result, schema_errors, semantic_errors = validate_prediction(
        semantically_invalid,
        case.duration_s,
    )
    assert result is not None
    assert schema_errors == []
    assert semantic_errors == ['event_time:outside_clip']

    probability_disagreement = _prediction()
    probability_disagreement['events'] = []
    probability_disagreement['fall'].update(
        assessment='normal_activity',
        confidence=0.9,
        recovery='unknown',
    )
    probability_disagreement['posture_end'] = 'standing'
    probability_disagreement['risk'] = 'none'
    result, schema_errors, semantic_errors = validate_prediction(
        probability_disagreement,
        case.duration_s,
    )
    assert result is not None
    assert schema_errors == []
    assert semantic_errors == [
        'fall:probability_assessment_disagreement'
    ]


def test_temporal_matching_is_one_to_one_and_type_safe() -> None:
    ground_truth = [
        GroundTruthEvent('one', 'person', 'fall', 2.0, 4.0, 'clear'),
        GroundTruthEvent('two', 'person', 'fall', 7.0, 8.0, 'clear'),
    ]
    predicted = [
        PredictedEvent('fall', 'person', 2.2, 4.2, 0.9),
        PredictedEvent('other', 'person', 7.0, 8.0, 0.9),
    ]
    matches, unmatched_gt, unmatched_predicted = match_events(
        ground_truth,
        predicted,
    )
    assert len(matches) == 1
    assert matches[0][0:2] == (0, 0)
    assert unmatched_gt == [1]
    assert unmatched_predicted == [1]
    assert temporal_iou(2, 4, 2, 4) == 1.0


def test_wilson_interval_matches_known_reference_values() -> None:
    interval = wilson_interval(98, 100)
    assert interval['lower'] == pytest.approx(0.9300, abs=0.0002)
    assert interval['upper'] == pytest.approx(0.9945, abs=0.0002)
    assert wilson_interval(0, 0) == {'lower': None, 'upper': None}


def test_report_scores_metrics_cost_and_operational_false_alerts() -> None:
    cases = load_vlm_cases(MANIFEST)
    records = load_prediction_records(PREDICTIONS, cases)
    prices = json.loads(PRICES.read_text(encoding='utf-8'))['models']
    profile = json.loads(TRAFFIC.read_text(encoding='utf-8'))['profile']
    report = build_evaluation_report(
        cases,
        records,
        prices=prices,
        traffic_profile=profile,
    )
    assert len(report['runs']) == 1
    run = report['runs'][0]
    assert run['model']['region'] == 'local'
    assert run['input']['observed_frame_count'] == {'min': 8, 'max': 12}
    assert run['metrics']['fall']['clear_recall']['value'] == 1.0
    assert run['metrics']['fall']['hard_negative_fpr']['value'] == 0.0
    assert (
        run['metrics']['fall']['robot_motion_false_event_rate']['value']
        == 0.0
    )
    assert run['metrics']['events']['per_type']['fall']['f1'] == 1.0
    assert run['metrics']['posture']['accuracy'] == 1.0
    assert run['metrics']['risk']['accuracy'] == 1.0
    assert run['metrics']['telemetry']['total_latency_ms']['p95'] == 850
    assert run['metrics']['cost']['coverage']['value'] == 1.0
    assert run['metrics']['cost']['usd']['mean_per_clip'] > 0
    expected_alerts = run['metrics']['fall']['expected_false_alerts']
    assert expected_alerts['complete'] is True
    assert expected_alerts['value'] == 0.0
    calibration = run['metrics']['fall']['calibration']
    assert calibration['auroc'] == 1.0
    assert calibration['unique_confidence_count'] == 3
    threshold_result = run['metrics']['fall']['confidence_threshold_sweep']
    assert threshold_result['budget_per_camera_day'] == 0.1
    assert threshold_result['recall_at_budget']['recall'] == 1.0
    assert report['evaluation_contract']['weighted_score'] is False
    assert report['privacy']['model_explanations_in_report'] is False
    serialized = json.dumps(report, ensure_ascii=False)
    assert '사람이 바닥으로 넘어져' not in serialized
    assert 'clips/P001.mp4' not in serialized


def test_missing_or_invalid_output_is_conservative_and_nonzero_exit(
    tmp_path: Path,
) -> None:
    cases = load_vlm_cases(MANIFEST)
    rows = PREDICTIONS.read_text(encoding='utf-8').splitlines()
    invalid = json.loads(rows[0])
    invalid['prediction']['fall']['assessment'] = 'normal_activity'
    predictions = tmp_path / 'invalid.jsonl'
    predictions.write_text(
        '\n'.join([json.dumps(invalid), rows[1]]) + '\n',
        encoding='utf-8',
    )
    records = load_prediction_records(predictions, cases)
    report = build_evaluation_report(cases, records)
    run = report['runs'][0]
    assert run['missing'] == 1
    assert run['metrics']['fall']['clear_recall']['value'] == 0.0
    assert (
        run['metrics']['telemetry']['semantic_valid_rate']['value']
        == pytest.approx(1 / 3)
    )
    assert evaluation_exit_code(report) == 2


def test_invalid_model_output_is_a_metric_not_harness_exit_code() -> None:
    report = {
        'runs': [
            {
                'attempted': 3,
                'received': 3,
                'gates': {'overall': 'pass'},
                'metrics': {
                    'telemetry': {'semantic_valid_rate': {'value': 2 / 3}}
                },
            }
        ]
    }

    assert evaluation_exit_code(report) == 0


def test_pareto_frontier_does_not_create_a_weighted_score() -> None:
    axes = (
        {'metric': 'recall', 'direction': 'max'},
        {'metric': 'cost', 'direction': 'min'},
    )
    runs = [
        {
            'configuration_id': 'accurate',
            'metrics': {'recall': 0.99, 'cost': 5},
        },
        {
            'configuration_id': 'cheap',
            'metrics': {'recall': 0.95, 'cost': 1},
        },
        {
            'configuration_id': 'dominated',
            'metrics': {'recall': 0.90, 'cost': 6},
        },
    ]
    selection = pareto_frontier(runs, axes)
    assert selection['frontier'] == ['accurate', 'cheap']
    assert 'score' not in json.dumps(selection)


def test_private_report_redacts_secrets_and_uses_mode_600(
    tmp_path: Path,
) -> None:
    output = tmp_path / 'private.json'
    secret = 'sk-private-evaluation-key'
    write_private_json(output, {'api_key': secret, 'log': f'Bearer {secret}'})
    assert secret not in output.read_text(encoding='utf-8')
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_cli_scores_sample_without_credentials(tmp_path: Path) -> None:
    output = tmp_path / 'report.json'
    exit_code = main(
        [
            '--manifest',
            str(MANIFEST),
            '--predictions',
            str(PREDICTIONS),
            '--prices',
            str(PRICES),
            '--gates',
            str(GATES),
            '--traffic-profile',
            str(TRAFFIC),
            '--output',
            str(output),
        ]
    )
    assert exit_code == 0
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['suite'] == 'malbut-homecam-vlm-v3'
    assert report['runs'][0]['gates']['overall'] == 'pass'
    assert report['selection']['method'] == 'pareto_no_weights'
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_ground_truth_rejects_subject_count_disagreement() -> None:
    raw = json.loads(MANIFEST.read_text(encoding='utf-8').splitlines()[0])
    raw['counts']['n_person'] = 0
    with pytest.raises(ValueError, match='n_person disagree'):
        VlmEvaluationCase.from_dict(raw)
