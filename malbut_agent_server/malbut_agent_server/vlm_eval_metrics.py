"""Metrics and unweighted model selection for offline VLM evaluation."""

import math
from collections import Counter, defaultdict
from dataclasses import asdict
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from malbut_agent_server.vlm_eval_schema import (
    FALL_ASSESSMENTS,
    PREDICTED_EVENT_TYPES,
    PREDICTED_POSTURES,
    RISKS,
    SUBJECTS,
    GroundTruthEvent,
    PredictedEvent,
    PredictionRecord,
    VlmEvaluationCase,
    VlmPrediction,
    canonical_sha256,
)


def ratio(numerator: int, denominator: int) -> Optional[float]:
    """Return a bounded ratio, preserving an unavailable denominator."""
    if denominator == 0:
        return None
    return round(numerator / denominator, 9)


def wilson_interval(
    successes: int,
    total: int,
    z: float = 1.959963984540054,
) -> Dict[str, Optional[float]]:
    """Return the two-sided Wilson 95% interval for a proportion."""
    if total == 0:
        return {'lower': None, 'upper': None}
    observed = successes / total
    denominator = 1 + z * z / total
    center = (observed + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            observed * (1 - observed) / total
            + z * z / (4 * total * total)
        )
        / denominator
    )
    return {
        'lower': round(max(0.0, center - margin), 9),
        'upper': round(min(1.0, center + margin), 9),
    }


def proportion(successes: int, total: int) -> Dict[str, Any]:
    """Describe a binomial metric without hiding sample size."""
    return {
        'numerator': successes,
        'denominator': total,
        'value': ratio(successes, total),
        'ci95': wilson_interval(successes, total),
    }


def percentile(values: Iterable[float], quantile: float) -> Optional[float]:
    """Use nearest-rank percentiles so small samples remain inspectable."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil(quantile * len(ordered)))
    return round(ordered[rank - 1], 6)


def _distribution(values: Sequence[float]) -> Dict[str, Optional[float]]:
    return {
        'p50': percentile(values, 0.50),
        'p90': percentile(values, 0.90),
        'p95': percentile(values, 0.95),
        'p99': percentile(values, 0.99),
        'mean': (
            round(sum(values) / len(values), 6) if values else None
        ),
    }


def temporal_iou(
    gt_start: float,
    gt_end: float,
    predicted_start: float,
    predicted_end: float,
) -> float:
    """Return intersection over union for two temporal intervals."""
    intersection = max(
        0.0,
        min(gt_end, predicted_end) - max(gt_start, predicted_start),
    )
    union = max(gt_end, predicted_end) - min(gt_start, predicted_start)
    if union == 0:
        return 1.0 if gt_start == predicted_start else 0.0
    return intersection / union


def match_events(
    ground_truth: Sequence[GroundTruthEvent],
    predicted: Sequence[PredictedEvent],
    threshold: float = 0.3,
) -> Tuple[List[Tuple[int, int, float]], List[int], List[int]]:
    """Greedily form one-to-one type/subject matches by descending tIoU."""
    candidates = []
    for gt_index, gt_event in enumerate(ground_truth):
        for predicted_index, predicted_event in enumerate(predicted):
            if (
                gt_event.event_type != predicted_event.event_type
                or gt_event.subject != predicted_event.subject
            ):
                continue
            overlap = temporal_iou(
                gt_event.start_s,
                gt_event.end_s,
                predicted_event.start_s,
                predicted_event.end_s,
            )
            if overlap >= threshold:
                candidates.append((overlap, gt_index, predicted_index))
    candidates.sort(reverse=True)
    used_gt = set()
    used_predicted = set()
    matches = []
    for overlap, gt_index, predicted_index in candidates:
        if gt_index in used_gt or predicted_index in used_predicted:
            continue
        used_gt.add(gt_index)
        used_predicted.add(predicted_index)
        matches.append((gt_index, predicted_index, overlap))
    unmatched_gt = [
        index for index in range(len(ground_truth)) if index not in used_gt
    ]
    unmatched_predicted = [
        index
        for index in range(len(predicted))
        if index not in used_predicted
    ]
    return matches, unmatched_gt, unmatched_predicted


def _classification(
    labels: Sequence[str],
    truths: Sequence[str],
    predictions: Sequence[str],
) -> Dict[str, Any]:
    per_class = {}
    f1_values = []
    for label in labels:
        true_positive = sum(
            truth == label and predicted == label
            for truth, predicted in zip(truths, predictions)
        )
        false_positive = sum(
            truth != label and predicted == label
            for truth, predicted in zip(truths, predictions)
        )
        false_negative = sum(
            truth == label and predicted != label
            for truth, predicted in zip(truths, predictions)
        )
        support = true_positive + false_negative
        precision = ratio(true_positive, true_positive + false_positive)
        recall = ratio(true_positive, support)
        f1 = None
        if support > 0:
            f1 = (
                0.0
                if true_positive == 0
                else round(
                    2
                    * true_positive
                    / (2 * true_positive + false_positive + false_negative),
                    9,
                )
            )
            f1_values.append(f1)
        per_class[label] = {
            'precision': precision,
            'recall': recall,
            'f1': f1,
            'support': support,
        }
    return {
        'per_class': per_class,
        'macro_f1': (
            round(sum(f1_values) / len(f1_values), 9)
            if f1_values
            else None
        ),
        'accuracy': ratio(
            sum(
                truth == predicted
                for truth, predicted in zip(truths, predictions)
            ),
            len(truths),
        ),
        'confusion_matrix': {
            truth: {
                predicted: sum(
                    actual == truth and output == predicted
                    for actual, output in zip(truths, predictions)
                )
                for predicted in labels
            }
            for truth in labels
        },
    }


def _quadratic_weighted_kappa(
    truths: Sequence[str],
    predictions: Sequence[str],
) -> Optional[float]:
    if not truths:
        return None
    order = ('none', 'attention', 'urgent')
    indices = {label: index for index, label in enumerate(order)}
    size = len(order)
    observed = [[0 for _ in order] for _ in order]
    truth_histogram = [0 for _ in order]
    prediction_histogram = [0 for _ in order]
    for truth, prediction in zip(truths, predictions):
        truth_index = indices[truth]
        prediction_index = indices[prediction]
        observed[truth_index][prediction_index] += 1
        truth_histogram[truth_index] += 1
        prediction_histogram[prediction_index] += 1
    weighted_observed = 0.0
    weighted_expected = 0.0
    for row in range(size):
        for column in range(size):
            weight = ((row - column) / (size - 1)) ** 2
            weighted_observed += weight * observed[row][column]
            weighted_expected += (
                weight
                * truth_histogram[row]
                * prediction_histogram[column]
                / len(truths)
            )
    if weighted_expected == 0:
        return 1.0 if weighted_observed == 0 else None
    return round(1 - weighted_observed / weighted_expected, 9)


def _binary_metrics(
    truths: Sequence[bool],
    predictions: Sequence[bool],
) -> Dict[str, Any]:
    true_positive = sum(
        truth and prediction
        for truth, prediction in zip(truths, predictions)
    )
    false_positive = sum(
        not truth and prediction
        for truth, prediction in zip(truths, predictions)
    )
    false_negative = sum(
        truth and not prediction
        for truth, prediction in zip(truths, predictions)
    )
    support = true_positive + false_negative
    precision = ratio(true_positive, true_positive + false_positive)
    recall = ratio(true_positive, support)
    f1 = None
    if support > 0:
        f1 = (
            0.0
            if true_positive == 0
            else round(
                2
                * true_positive
                / (2 * true_positive + false_positive + false_negative),
                9,
            )
        )
    return {
        'precision': precision,
        'recall': recall,
        'f1': f1,
        'support': support,
    }


def _calibration(
    truths: Sequence[bool],
    confidences: Sequence[float],
    bin_count: int = 15,
) -> Dict[str, Any]:
    if not truths:
        return {'brier': None, 'ece': None, 'bins': []}
    brier = sum(
        (confidence - float(truth)) ** 2
        for truth, confidence in zip(truths, confidences)
    ) / len(truths)
    bins = []
    weighted_gap = 0.0
    for index in range(bin_count):
        lower = index / bin_count
        upper = (index + 1) / bin_count
        selected = [
            (truth, confidence)
            for truth, confidence in zip(truths, confidences)
            if confidence >= lower
            and (confidence < upper or index == bin_count - 1)
        ]
        if not selected:
            continue
        accuracy = sum(float(truth) for truth, _ in selected) / len(selected)
        average_confidence = sum(
            confidence for _, confidence in selected
        ) / len(selected)
        gap = abs(accuracy - average_confidence)
        weighted_gap += gap * len(selected) / len(truths)
        bins.append(
            {
                'lower': lower,
                'upper': upper,
                'count': len(selected),
                'accuracy': round(accuracy, 9),
                'mean_confidence': round(average_confidence, 9),
            }
        )
    positives = [
        confidence
        for truth, confidence in zip(truths, confidences)
        if truth
    ]
    negatives = [
        confidence
        for truth, confidence in zip(truths, confidences)
        if not truth
    ]
    auroc = None
    if positives and negatives:
        wins = sum(
            1.0
            if positive > negative
            else 0.5
            if positive == negative
            else 0.0
            for positive in positives
            for negative in negatives
        )
        auroc = round(wins / (len(positives) * len(negatives)), 9)
    certainty_rows = sorted(
        zip(truths, confidences),
        key=lambda item: abs(item[1] - 0.5),
        reverse=True,
    )
    selected_count = max(1, math.ceil(0.8 * len(certainty_rows)))
    selected = certainty_rows[:selected_count]
    selective_accuracy = sum(
        truth == (confidence >= 0.5) for truth, confidence in selected
    ) / len(selected)
    histogram = Counter(round(confidence, 6) for confidence in confidences)
    entropy = -sum(
        (count / len(confidences)) * math.log2(count / len(confidences))
        for count in histogram.values()
    )
    return {
        'brier': round(brier, 9),
        'ece': round(weighted_gap, 9),
        'auroc': auroc,
        'selective_accuracy_at_80_percent_coverage': round(
            selective_accuracy,
            9,
        ),
        'unique_confidence_count': len(histogram),
        'confidence_entropy_bits': round(entropy, 9),
        'bins': bins,
    }


def _fall_threshold_curve(
    rows: Sequence[Tuple[Any, Any, VlmPrediction]],
    traffic_profile: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Sweep fall confidence and optionally apply a camera-day budget."""
    thresholds = sorted(
        {0.0, 1.0} | {row[2].fall_confidence for row in rows},
        reverse=True,
    )
    volumes = None
    budget = None
    if isinstance(traffic_profile, dict):
        candidate_volumes = traffic_profile.get('clips_per_camera_day')
        if isinstance(candidate_volumes, dict):
            volumes = candidate_volumes
        candidate_budget = traffic_profile.get(
            'false_alert_budget_per_camera_day'
        )
        if isinstance(candidate_budget, (int, float)) and not isinstance(
            candidate_budget,
            bool,
        ):
            budget = float(candidate_budget)
    curve = []
    for threshold in thresholds:
        fall_rows = [row for row in rows if row[0].has_fall]
        fall_detected = sum(
            row[2].fall_confidence >= threshold for row in fall_rows
        )
        false_alerts = None
        if volumes is not None:
            estimate = 0.0
            complete = True
            for traffic_class, volume in volumes.items():
                selected = [
                    row
                    for row in rows
                    if row[0].traffic_class == traffic_class
                ]
                if not selected or not isinstance(volume, (int, float)):
                    complete = False
                    break
                positive_rate = sum(
                    row[2].fall_confidence >= threshold
                    for row in selected
                ) / len(selected)
                estimate += positive_rate * float(volume)
            if complete:
                false_alerts = round(estimate, 9)
        curve.append(
            {
                'threshold': round(threshold, 9),
                'recall': ratio(fall_detected, len(fall_rows)),
                'false_alerts_per_camera_day': false_alerts,
            }
        )
    candidates = [
        point
        for point in curve
        if budget is not None
        and point['false_alerts_per_camera_day'] is not None
        and point['false_alerts_per_camera_day'] <= budget
        and point['recall'] is not None
    ]
    best = None
    if candidates:
        best = max(
            candidates,
            key=lambda point: (
                point['recall'],
                -point['false_alerts_per_camera_day'],
                -point['threshold'],
            ),
        )
    return {
        'budget_per_camera_day': budget,
        'recall_at_budget': best,
        'curve': curve,
    }


def _price_for_record(
    record: PredictionRecord,
    prices: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    price = prices.get(record.model_id)
    if not isinstance(price, dict):
        return None
    currency = price.get('currency')
    if not isinstance(currency, str) or not currency:
        return None
    if record.input_tokens is None or record.output_tokens is None:
        return None
    input_price = price.get('input_per_million')
    output_price = price.get('output_per_million')
    if not isinstance(input_price, (int, float)) or not isinstance(
        output_price,
        (int, float),
    ):
        return None
    cached_tokens = record.cached_input_tokens or 0
    if cached_tokens > record.input_tokens:
        return None
    cached_price = price.get('cached_input_per_million', input_price)
    if not isinstance(cached_price, (int, float)):
        return None
    per_request = price.get('per_request', 0.0)
    if not isinstance(per_request, (int, float)):
        return None
    uncached_tokens = record.input_tokens - cached_tokens
    amount = (
        uncached_tokens * float(input_price)
        + cached_tokens * float(cached_price)
        + record.output_tokens * float(output_price)
    ) / 1_000_000 + float(per_request)
    result = {'currency': currency, 'amount': amount}
    usd_per_currency = price.get('usd_per_currency_unit')
    if isinstance(usd_per_currency, (int, float)):
        result['amount_usd'] = amount * float(usd_per_currency)
    elif currency == 'USD':
        result['amount_usd'] = amount
    return result


def _case_contract(case: VlmEvaluationCase) -> Dict[str, Any]:
    """Exclude local paths while binding results to all scoring labels."""
    return {
        'case_id': case.case_id,
        'duration_s': case.duration_s,
        'media_sha256': case.media_sha256,
        'has_audio': case.has_audio,
        'source': case.source,
        'robot_motion': case.robot_motion,
        'subjects': case.subjects,
        'events': [asdict(event) for event in case.events],
        'fall_assessment': case.fall_assessment,
        'posture_end': case.posture_end,
        'risk': case.risk,
        'traffic_class': case.traffic_class,
        'conditions': case.conditions,
    }


def _dataset_summary(
    cases: Sequence[VlmEvaluationCase],
) -> Dict[str, Any]:
    condition_counts: Dict[str, Counter] = defaultdict(Counter)
    for case in cases:
        for name, value in case.conditions.items():
            condition_counts[name][value] += 1
    return {
        'traffic_classes': dict(
            sorted(Counter(case.traffic_class for case in cases).items())
        ),
        'sources': dict(
            sorted(Counter(case.source for case in cases).items())
        ),
        'robot_motion': dict(
            sorted(Counter(case.robot_motion for case in cases).items())
        ),
        'fall_tiers': dict(
            sorted(
                Counter(
                    event.fall_tier
                    for case in cases
                    for event in case.events
                    if event.event_type == 'fall'
                ).items()
            )
        ),
        'fall_assessments': dict(
            sorted(Counter(case.fall_assessment for case in cases).items())
        ),
        'conditions': {
            name: dict(sorted(counts.items()))
            for name, counts in sorted(condition_counts.items())
        },
        'duration_s': _distribution([case.duration_s for case in cases]),
        'audio_clip_count': sum(case.has_audio for case in cases),
        'media_sha256_coverage': proportion(
            sum(case.media_sha256 is not None for case in cases),
            len(cases),
        ),
    }


def _fallback_prediction() -> VlmPrediction:
    """Treat missing or invalid output as a conservative failed attempt."""
    return VlmPrediction(
        subjects={subject: 0 for subject in SUBJECTS},
        events=(),
        fall_assessment='unobservable',
        fall_confidence=0.0,
        fall_recovery='unknown',
        posture_end='unknown',
        risk='none',
        risk_confidence=0.0,
        camera_motion='none',
        explanation_ko='',
        evidence_ko=(),
        uncertainty_flags=(),
    )


def _summarize_configuration(
    cases: Sequence[VlmEvaluationCase],
    records: Sequence[PredictionRecord],
    prices: Mapping[str, Any],
    traffic_profile: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    first = records[0]
    repetitions = sorted({record.repetition for record in records})
    if repetitions != list(range(1, max(repetitions) + 1)):
        raise ValueError('prediction repetitions must be contiguous from 1')
    expected = len(cases) * len(repetitions)
    by_identity = {
        (record.case_id, record.repetition): record for record in records
    }
    rows = []
    missing = 0
    for repetition in repetitions:
        for case in cases:
            record = by_identity.get((case.case_id, repetition))
            if record is None:
                missing += 1
                rows.append((case, None, _fallback_prediction()))
                continue
            prediction = (
                record.prediction
                if record.semantic_valid and record.prediction is not None
                else _fallback_prediction()
            )
            rows.append((case, record, prediction))

    clear_fall_rows = [row for row in rows if row[0].has_clear_fall]
    all_fall_rows = [row for row in rows if row[0].has_fall]
    hard_negative_rows = [
        row for row in rows if row[0].traffic_class == 'hard_negative'
    ]
    no_event_rows = [
        row for row in rows if row[0].traffic_class == 'no_event'
    ]
    motion_only_rows = [
        row for row in rows if row[0].traffic_class == 'robot_motion_only'
    ]
    screen_rows = [
        row
        for row in rows
        if row[0].traffic_class == 'screen_or_reflection'
    ]

    def detected_count(selected: Sequence[Tuple[Any, Any, Any]]) -> int:
        return sum(row[2].fall_detected for row in selected)

    valid_rows = [
        row
        for row in rows
        if row[1] is not None and row[1].semantic_valid
    ]
    fall_metrics = {
        'clear_recall': proportion(
            detected_count(clear_fall_rows),
            len(clear_fall_rows),
        ),
        'all_recall': proportion(
            detected_count(all_fall_rows),
            len(all_fall_rows),
        ),
        'hard_negative_fpr': proportion(
            detected_count(hard_negative_rows),
            len(hard_negative_rows),
        ),
        'no_event_fpr': proportion(
            detected_count(no_event_rows),
            len(no_event_rows),
        ),
        'screen_reflection_fpr': proportion(
            sum(
                row[2].fall_detected
                or row[2].subjects['person'] > 0
                or any(
                    event.subject == 'person' for event in row[2].events
                )
                for row in screen_rows
            ),
            len(screen_rows),
        ),
        'robot_motion_false_event_rate': proportion(
            sum(
                row[2].fall_detected
                or row[2].subjects['person'] > 0
                or any(
                    event.subject == 'person' for event in row[2].events
                )
                for row in motion_only_rows
            ),
            len(motion_only_rows),
        ),
    }
    assessment_truth = [row[0].fall_assessment for row in valid_rows]
    assessment_predictions = [
        row[2].fall_assessment for row in valid_rows
    ]
    fall_metrics['assessment'] = _classification(
        FALL_ASSESSMENTS,
        assessment_truth,
        assessment_predictions,
    )
    found_down_rows = [
        row for row in rows if row[0].fall_assessment == 'found_down'
    ]
    fall_metrics['found_down_recall'] = proportion(
        sum(
            row[2].fall_assessment == 'found_down'
            for row in found_down_rows
        ),
        len(found_down_rows),
    )
    unobservable_rows = [
        row for row in rows if row[0].fall_assessment == 'unobservable'
    ]
    fall_metrics['unobservable_to_normal_rate'] = proportion(
        sum(
            row[2].fall_assessment == 'normal_activity'
            for row in unobservable_rows
        ),
        len(unobservable_rows),
    )
    normal_rows = [
        row
        for row in rows
        if row[0].fall_assessment == 'normal_activity'
    ]
    fall_metrics['precision_reference_only'] = _binary_metrics(
        [row[0].has_fall for row in rows],
        [row[2].fall_detected for row in rows],
    )['precision']
    fall_metrics['all_non_fall_fpr'] = proportion(
        detected_count(normal_rows),
        len(normal_rows),
    )
    fall_metrics['calibration'] = _calibration(
        [row[0].has_fall for row in rows],
        [row[2].fall_confidence for row in rows],
    )
    recovery_rows = [
        row
        for row in all_fall_rows
        if row[2].fall_detected
        and row[2].fall_recovered is not None
        and any(
            event.event_type == 'fall' and event.recovered is not None
            for event in row[0].events
        )
    ]
    fall_metrics['recovery_accuracy'] = proportion(
        sum(
            row[2].fall_recovered
            == next(
                event.recovered
                for event in row[0].events
                if event.event_type == 'fall'
                and event.recovered is not None
            )
            for row in recovery_rows
        ),
        len(recovery_rows),
    )

    strata = defaultdict(list)
    for row in all_fall_rows:
        for name, value in row[0].conditions.items():
            strata[(name, value)].append(row)
        strata[('robot_motion', row[0].robot_motion)].append(row)
    fall_metrics['stratified_recall'] = {
        f'{name}={value}': proportion(
            detected_count(selected),
            len(selected),
        )
        for (name, value), selected in sorted(strata.items())
    }

    subject_metrics = {
        'coverage': proportion(len(valid_rows), expected),
    }
    for subject in SUBJECTS:
        subject_metrics[subject] = _binary_metrics(
            [row[0].subjects[subject] > 0 for row in valid_rows],
            [row[2].subjects[subject] > 0 for row in valid_rows],
        )
    subject_metrics['person_pet_count_exact_accuracy'] = proportion(
        sum(
            row[0].subjects['person'] == row[2].subjects['person']
            and row[0].subjects['pet'] == row[2].subjects['pet']
            for row in valid_rows
        ),
        len(valid_rows),
    )

    event_counts = {
        event_type: {'tp': 0, 'fp': 0, 'fn': 0}
        for event_type in PREDICTED_EVENT_TYPES
    }
    overlaps = []
    start_errors = []
    end_errors = []
    within_one = 0
    within_two = 0
    match_count = 0
    for case, _record, prediction in rows:
        matches, unmatched_gt, unmatched_predicted = match_events(
            case.events,
            prediction.events,
        )
        for gt_index, predicted_index, overlap in matches:
            event_type = case.events[gt_index].event_type
            event_counts[event_type]['tp'] += 1
            overlaps.append(overlap)
            start_error = abs(
                case.events[gt_index].start_s
                - prediction.events[predicted_index].start_s
            )
            end_error = abs(
                case.events[gt_index].end_s
                - prediction.events[predicted_index].end_s
            )
            start_errors.append(start_error)
            end_errors.append(end_error)
            within_one += start_error <= 1
            within_two += start_error <= 2
            match_count += 1
        for index in unmatched_gt:
            event_counts[case.events[index].event_type]['fn'] += 1
        for index in unmatched_predicted:
            event_counts[prediction.events[index].event_type]['fp'] += 1
    event_metrics = {}
    for event_type, counts in event_counts.items():
        precision_value = ratio(
            counts['tp'],
            counts['tp'] + counts['fp'],
        )
        recall_value = ratio(
            counts['tp'],
            counts['tp'] + counts['fn'],
        )
        f1 = None
        if counts['tp'] + counts['fn'] > 0:
            f1 = (
                0.0
                if counts['tp'] == 0
                else round(
                    2 * counts['tp']
                    / (2 * counts['tp'] + counts['fp'] + counts['fn']),
                    9,
                )
            )
        event_metrics[event_type] = {
            **counts,
            'precision': precision_value,
            'recall': recall_value,
            'f1': f1,
        }

    posture_truth = [row[0].posture_end for row in valid_rows]
    posture_predictions = [row[2].posture_end for row in valid_rows]
    risk_truth = [row[0].risk for row in valid_rows]
    risk_predictions = [row[2].risk for row in valid_rows]
    risk_metrics = _classification(RISKS, risk_truth, risk_predictions)
    risk_metrics['quadratic_weighted_kappa'] = _quadratic_weighted_kappa(
        risk_truth,
        risk_predictions,
    )
    risk_metrics['coverage'] = proportion(len(valid_rows), expected)
    urgent_rows = [row for row in valid_rows if row[0].risk == 'urgent']
    none_rows = [row for row in valid_rows if row[0].risk == 'none']
    risk_metrics['urgent_miss_rate'] = proportion(
        sum(row[2].risk == 'none' for row in urgent_rows),
        len(urgent_rows),
    )
    risk_metrics['urgent_overcall_rate'] = proportion(
        sum(row[2].risk == 'urgent' for row in none_rows),
        len(none_rows),
    )

    records_present = [record for _case, record, _prediction in rows if record]
    total_latencies = [
        record.total_latency_ms
        for record in records_present
        if record.total_latency_ms is not None
    ]
    first_token_latencies = [
        record.first_token_latency_ms
        for record in records_present
        if record.first_token_latency_ms is not None
    ]
    telemetry = {
        'request_success_rate': proportion(
            sum(record.request_succeeded for record in records_present),
            expected,
        ),
        'first_attempt_success_rate': proportion(
            sum(
                record.request_succeeded and record.retry_count == 0
                for record in records_present
            ),
            expected,
        ),
        'timeout_rate': proportion(
            sum(
                not record.request_succeeded
                and record.error_type is not None
                and 'timeout' in record.error_type.lower()
                for record in records_present
            ),
            expected,
        ),
        'schema_valid_rate': proportion(
            sum(record.schema_valid for record in records_present),
            expected,
        ),
        'semantic_valid_rate': proportion(
            sum(record.semantic_valid for record in records_present),
            expected,
        ),
        'total_latency_ms': _distribution(total_latencies),
        'first_token_latency_ms': _distribution(first_token_latencies),
        'retry_count_total': sum(
            record.retry_count for record in records_present
        ),
        'error_types': dict(
            sorted(
                Counter(
                    record.error_type or 'none'
                    for record in records_present
                    if not record.request_succeeded
                ).items()
            )
        ),
        'schema_error_types': dict(
            sorted(
                Counter(
                    error.split(':', 1)[0]
                    for record in records_present
                    for error in record.schema_errors
                ).items()
            )
        ),
        'semantic_error_types': dict(
            sorted(
                Counter(
                    error.split(':', 1)[0]
                    for record in records_present
                    for error in record.semantic_errors
                ).items()
            )
        ),
    }

    costs = [
        cost
        for record in records_present
        for cost in [_price_for_record(record, prices)]
        if cost is not None
    ]
    currencies = sorted({cost['currency'] for cost in costs})
    complete_cost = len(costs) == expected and len(currencies) == 1
    cost_metrics: Dict[str, Any] = {
        'coverage': proportion(len(costs), expected),
        'currency': currencies[0] if len(currencies) == 1 else None,
        'total': None,
        'mean_per_clip': None,
        'normalized_30s_mean': None,
        'usd': None,
    }
    if complete_cost:
        total = sum(cost['amount'] for cost in costs)
        normalized = [
            cost['amount'] * 30 / case.duration_s
            for (case, record, _prediction), cost in zip(rows, costs)
            if record is not None
        ]
        cost_metrics.update(
            {
                'total': round(total, 9),
                'mean_per_clip': round(total / expected, 9),
                'normalized_30s_mean': round(
                    sum(normalized) / len(normalized),
                    9,
                ),
            }
        )
    usd_costs = [cost.get('amount_usd') for cost in costs]
    if len(usd_costs) == expected and all(
        isinstance(cost, (int, float)) for cost in usd_costs
    ):
        total_usd = sum(float(cost) for cost in usd_costs)
        mean_usd = total_usd / expected
        cost_metrics['usd'] = {
            'total': round(total_usd, 9),
            'mean_per_clip': round(mean_usd, 9),
            'per_camera_month': {
                str(clips_per_day): round(
                    mean_usd * clips_per_day * 30,
                    6,
                )
                for clips_per_day in (10, 50, 200)
            },
        }

    expected_false_alerts = None
    if isinstance(traffic_profile, dict):
        volumes = traffic_profile.get('clips_per_camera_day')
        if isinstance(volumes, dict):
            rates = {}
            complete = True
            total_false_alerts = 0.0
            for traffic_class, volume in volumes.items():
                selected = [
                    row
                    for row in rows
                    if row[0].traffic_class == traffic_class
                ]
                if (
                    not isinstance(volume, (int, float))
                    or isinstance(volume, bool)
                    or volume < 0
                    or not selected
                ):
                    complete = False
                    continue
                rate = detected_count(selected) / len(selected)
                rates[str(traffic_class)] = round(rate, 9)
                total_false_alerts += rate * float(volume)
            expected_false_alerts = {
                'profile': traffic_profile.get('name', 'unnamed'),
                'complete': complete,
                'value': (
                    round(total_false_alerts, 9) if complete else None
                ),
                'unit': 'alerts_per_camera_day',
                'class_fall_positive_rates': rates,
            }
    fall_metrics['expected_false_alerts'] = expected_false_alerts
    fall_metrics['confidence_threshold_sweep'] = _fall_threshold_curve(
        rows,
        traffic_profile,
    )

    stability_cases = []
    if len(repetitions) > 1:
        for case in cases:
            outcomes = [
                row[2]
                for row in rows
                if row[0].case_id == case.case_id
            ]
            stability_cases.append(
                {
                    'fall_flip': len(
                        {prediction.fall_detected for prediction in outcomes}
                    )
                    > 1,
                    'risk_flip': len(
                        {prediction.risk for prediction in outcomes}
                    )
                    > 1,
                    'posture_flip': len(
                        {prediction.posture_end for prediction in outcomes}
                    )
                    > 1,
                }
            )
    stability = {
        'repetitions': len(repetitions),
        'fall_flip_rate': proportion(
            sum(item['fall_flip'] for item in stability_cases),
            len(stability_cases),
        ),
        'risk_flip_rate': proportion(
            sum(item['risk_flip'] for item in stability_cases),
            len(stability_cases),
        ),
        'posture_flip_rate': proportion(
            sum(item['posture_flip'] for item in stability_cases),
            len(stability_cases),
        ),
    }

    case_outcomes = []
    for row_index, (case, record, prediction) in enumerate(rows):
        case_outcomes.append(
            {
                'case_id': case.case_id,
                'repetition': (
                    record.repetition
                    if record
                    else repetitions[row_index // len(cases)]
                ),
                'traffic_class': case.traffic_class,
                'fall_gt': case.has_fall,
                'fall_predicted': prediction.fall_detected,
                'schema_valid': record.schema_valid if record else False,
                'semantic_valid': record.semantic_valid if record else False,
                'request_succeeded': (
                    record.request_succeeded if record else False
                ),
            }
        )

    return {
        'configuration_id': canonical_sha256(first.config_key)[:16],
        'model': {
            'provider': first.provider,
            'id': first.model_id,
            'version': first.model_version,
            'region': first.model_region,
            'runtime': first.model_runtime,
            'contract_sha256': first.model_contract_sha256,
        },
        'input': {
            'track': first.track,
            'context_variant': first.context_variant,
            'prompt_version': first.prompt_version,
            'prompt_sha256': first.prompt_sha256,
            'configuration_sha256': first.input_spec_sha256,
            'configuration': {
                key: item
                for key, item in first.input_spec.items()
                if key not in {'effective_fps', 'frame_count'}
            },
            'observed_effective_fps': sorted(
                {
                    record.input_spec.get('effective_fps')
                    for record in records
                    if record.input_spec.get('effective_fps') is not None
                }
            ),
            'observed_frame_count': {
                'min': min(
                    (
                        record.input_spec['frame_count']
                        for record in records
                        if record.input_spec.get('frame_count') is not None
                    ),
                    default=None,
                ),
                'max': max(
                    (
                        record.input_spec['frame_count']
                        for record in records
                        if record.input_spec.get('frame_count') is not None
                    ),
                    default=None,
                ),
            },
        },
        'attempted': expected,
        'received': len(records),
        'missing': missing,
        'metrics': {
            'fall': fall_metrics,
            'subjects': subject_metrics,
            'events': {
                'tiou_threshold': 0.3,
                'per_type': event_metrics,
            },
            'temporal': {
                'tiou': _distribution(overlaps),
                'start_error_s': _distribution(start_errors),
                'end_error_s': _distribution(end_errors),
                'start_within_1s': proportion(within_one, match_count),
                'start_within_2s': proportion(within_two, match_count),
            },
            'posture': _classification(
                PREDICTED_POSTURES,
                posture_truth,
                posture_predictions,
            ),
            'risk': risk_metrics,
            'camera_motion_accuracy': proportion(
                sum(
                    row[0].robot_motion == row[2].camera_motion
                    for row in valid_rows
                ),
                len(valid_rows),
            ),
            'telemetry': telemetry,
            'stability': stability,
            'cost': cost_metrics,
        },
        'case_outcomes': case_outcomes,
    }


def _metric_path(value: Mapping[str, Any], path: str) -> Any:
    current: Any = value
    for key in path.split('.'):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def apply_gates(
    run: Mapping[str, Any],
    gates: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Apply declarative gates without inventing project thresholds."""
    results = []
    operators = {
        '>=': lambda actual, target: actual >= target,
        '<=': lambda actual, target: actual <= target,
        '>': lambda actual, target: actual > target,
        '<': lambda actual, target: actual < target,
        '==': lambda actual, target: actual == target,
    }
    for gate in gates:
        gate_id = gate.get('id')
        metric = gate.get('metric')
        operator = gate.get('operator')
        target = gate.get('value')
        if (
            not isinstance(gate_id, str)
            or not isinstance(metric, str)
            or operator not in operators
            or not isinstance(target, (int, float, bool))
        ):
            raise ValueError('gate configuration is invalid')
        actual = _metric_path(run.get('metrics', {}), metric)
        if not isinstance(actual, (int, float, bool)):
            status = 'hold'
        else:
            status = (
                'pass'
                if operators[str(operator)](actual, target)
                else 'fail'
            )
        results.append(
            {
                'id': gate_id,
                'metric': metric,
                'operator': operator,
                'target': target,
                'actual': actual,
                'status': status,
            }
        )
    overall = 'pass'
    if any(result['status'] == 'fail' for result in results):
        overall = 'fail'
    elif any(result['status'] == 'hold' for result in results):
        overall = 'hold'
    return {'overall': overall, 'results': results}


def pareto_frontier(
    runs: Sequence[Mapping[str, Any]],
    axes: Sequence[Mapping[str, str]],
) -> Dict[str, Any]:
    """Find non-dominated runs; never collapse axes into a score."""
    eligible = []
    excluded = []
    for run in runs:
        values = []
        for axis in axes:
            value = _metric_path(run.get('metrics', {}), axis['metric'])
            if not isinstance(value, (int, float)):
                values = []
                break
            values.append(float(value))
        if values:
            eligible.append((run['configuration_id'], values))
        else:
            excluded.append(run['configuration_id'])
    frontier = []
    for candidate_id, candidate_values in eligible:
        dominated = False
        for competitor_id, competitor_values in eligible:
            if competitor_id == candidate_id:
                continue
            no_worse = True
            strictly_better = False
            for index, axis in enumerate(axes):
                if axis['direction'] == 'max':
                    no_worse &= (
                        competitor_values[index]
                        >= candidate_values[index]
                    )
                    strictly_better |= (
                        competitor_values[index] > candidate_values[index]
                    )
                elif axis['direction'] == 'min':
                    no_worse &= (
                        competitor_values[index]
                        <= candidate_values[index]
                    )
                    strictly_better |= (
                        competitor_values[index] < candidate_values[index]
                    )
                else:
                    raise ValueError('Pareto direction must be min or max')
            if no_worse and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(candidate_id)
    return {
        'method': 'pareto_no_weights',
        'axes': list(axes),
        'frontier': sorted(frontier),
        'excluded_missing_axes': sorted(excluded),
    }


DEFAULT_PARETO_AXES = (
    {
        'metric': 'fall.all_recall.ci95.lower',
        'direction': 'max',
    },
    {
        'metric': 'fall.expected_false_alerts.value',
        'direction': 'min',
    },
    {
        'metric': 'telemetry.total_latency_ms.p95',
        'direction': 'min',
    },
    {
        'metric': 'cost.usd.per_camera_month.50',
        'direction': 'min',
    },
)


def build_evaluation_report(
    cases: Sequence[VlmEvaluationCase],
    records: Sequence[PredictionRecord],
    *,
    prices: Optional[Mapping[str, Any]] = None,
    gates: Optional[Sequence[Mapping[str, Any]]] = None,
    traffic_profile: Optional[Mapping[str, Any]] = None,
    pareto_axes: Sequence[Mapping[str, str]] = DEFAULT_PARETO_AXES,
) -> Dict[str, Any]:
    """Aggregate fixed cases without persisting media or model prose."""
    if not cases:
        raise ValueError('VLM evaluation cases are empty')
    if not records:
        raise ValueError('VLM prediction records are empty')
    grouped: Dict[Tuple[str, ...], List[PredictionRecord]] = defaultdict(list)
    for record in records:
        grouped[record.config_key].append(record)
    price_map = prices or {}
    runs = [
        _summarize_configuration(
            cases,
            sorted(
                configuration_records,
                key=lambda item: (item.repetition, item.case_id),
            ),
            price_map,
            traffic_profile,
        )
        for _key, configuration_records in sorted(grouped.items())
    ]
    for run in runs:
        run['gates'] = apply_gates(run, gates or ())
    eligible_runs = [run for run in runs if run['gates']['overall'] == 'pass']
    protocols = {
        (
            run['input']['track'],
            run['input']['context_variant'],
            run['input']['prompt_version'],
            run['input']['prompt_sha256'],
        )
        for run in eligible_runs
    }
    selection_by_protocol = []
    for protocol in sorted(
        protocols,
        key=lambda item: tuple(value or '' for value in item),
    ):
        comparable = [
            run
            for run in eligible_runs
            if (
                run['input']['track'],
                run['input']['context_variant'],
                run['input']['prompt_version'],
                run['input']['prompt_sha256'],
            )
            == protocol
        ]
        selection_by_protocol.append(
            {
                'protocol': {
                    'track': protocol[0],
                    'context_variant': protocol[1],
                    'prompt_version': protocol[2],
                    'prompt_sha256': protocol[3],
                },
                'pareto': pareto_frontier(comparable, pareto_axes),
            }
        )
    return {
        'schema_version': 1,
        'suite': 'malbut-homecam-vlm-v3',
        'case_count': len(cases),
        'dataset_summary': _dataset_summary(cases),
        'dataset_contract_sha256': canonical_sha256(
            [_case_contract(case) for case in cases]
        ),
        'evaluation_contract': {
            'invalid_or_missing_output_scored_as_negative': True,
            'event_matching_tiou_threshold': 0.3,
            'confidence_bins': 15,
            'weighted_score': False,
            'tracks_compared_only_within_track': True,
        },
        'privacy': {
            'media_paths_in_report': False,
            'model_explanations_in_report': False,
            'raw_requests_in_report': False,
            'raw_responses_in_report': False,
        },
        'price_catalog': price_map,
        'traffic_profile': traffic_profile,
        'runs': runs,
        'selection': {
            'method': 'pareto_no_weights',
            'cross_protocol_comparison': False,
            'by_protocol': selection_by_protocol,
        },
    }
