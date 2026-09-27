"""Request-denominator metrics and auditable, optional USD cost estimates."""

from collections import Counter
from decimal import Decimal, InvalidOperation
import math
import statistics

from .inputs import LABELS, require

COMPONENTS = ('input', 'cached_input', 'cache_write_5m', 'cache_write_1h', 'output')


def money(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError('invalid monetary amount')
    try:
        amount = Decimal(str(value))
        require(amount.is_finite() and amount >= 0, 'invalid monetary amount')
        return amount
    except InvalidOperation:
        raise ValueError('invalid monetary amount') from None


def validate_rate(rate):
    require(isinstance(rate, dict) and rate.get('currency') == 'USD'
            and isinstance(rate.get('source'), str) and rate['source'].startswith('https://')
            and isinstance(rate.get('checked_on'), str) and bool(rate['checked_on']), 'unverified rate')
    prices = rate.get('per_million_tokens')
    require(isinstance(prices, dict) and set(COMPONENTS) <= set(prices)
            <= set(COMPONENTS) | {'cache_write_30m'}, 'incomplete price table')
    return {key: money(value) for key, value in prices.items()}


def estimate_cost(usage, rate):
    if usage is None or rate is None:
        return None
    try:
        prices = validate_rate(rate)
        # reasoning_output is already in output; never add it a second time.
        require(not usage.get('cache_write_30m') or 'cache_write_30m' in prices,
                'missing cache-write price')
        return str(sum(Decimal(usage.get(k, 0)) * value for k, value in prices.items()) / Decimal(1000000))
    except (ValueError, KeyError, TypeError):
        return None


def ratio(n, d):
    return dict(correct=n, total=d, rate=n/d if d else None)


def latency(values):
    if not values:
        return dict(count=0, median_s=None, p95_s=None)
    ordered = sorted(values)
    return dict(count=len(values), median_s=statistics.median(values),
                p95_s=ordered[math.ceil(.95 * len(values))-1])


def summarize(rows, labels):
    require(len({r['case_id'] for r in rows}) == len(rows), 'duplicate results')
    require(all(r['case_id'] in labels for r in rows), 'unexpected case')
    complete = {r['case_id'] for r in rows} == set(labels)
    correct = sum(r['outcome'] == 'classified' and r['label'] == labels[r['case_id']] for r in rows)
    counts = Counter(r['outcome'] for r in rows)
    def reported(row):
        if row['outcome'] not in ('classified', 'invalid_response', 'unobservable'):
            return None
        return row.get('reported_assessment', row.get('label'))
    video_correct = sum(reported(r) == labels[r['case_id']] for r in rows)
    raw_missed = {label: sum(labels[r['case_id']] == label and reported(r) == 'normal_activity'
                            for r in rows) for label in LABELS[:2]}
    issue_counts = Counter(code for r in rows for code in r.get('response_issue_codes', []))
    location_rows = [r for r in rows if r.get('localization_counts') is not None]
    # Empty regions can be honest abstention. Report them separately so a prompt
    # that suppresses all boxes is not mistaken for better localization.
    localization = dict(
        responses_with_counts=len(location_rows), responses_without_counts=len(rows)-len(location_rows),
        responses_with_empty_regions=sum(r['localization_counts']['findings_with_empty_regions'] > 0
                                         for r in location_rows),
        positive_responses_without_regions=sum(reported(r) in LABELS[:2]
                                                and r['localization_counts']['regions'] == 0
                                                for r in location_rows),
        **{key: sum(r['localization_counts'][key] for r in location_rows)
           for key in ('findings', 'findings_with_empty_regions', 'findings_with_regions', 'regions')})
    missed = {label: sum(labels[r['case_id']] == label and r.get('label') == 'normal_activity'
                         and r['outcome'] == 'classified' for r in rows)
              for label in LABELS[:2]}
    per_label = {}
    for label in LABELS:
        group = [r for r in rows if labels[r['case_id']] == label]
        per_label[label] = dict(planned=sum(v == label for v in labels.values()),
            attempted=len(group), outcomes=dict(Counter(r['outcome'] for r in group)),
            correct=sum(r['outcome'] == 'classified' and r['label'] == label for r in group))
    known = [money(r['cost_estimate_usd']) for r in rows if r.get('cost_estimate_usd') is not None]
    return dict(complete=complete, planned=len(labels), attempted=len(rows),
                not_called=len(labels)-len(rows), accuracy=ratio(correct, len(labels)) if complete else None,
                attempted_accuracy=ratio(correct, len(rows)), outcomes=dict(counts), per_label=per_label,
                video_label_accuracy=ratio(video_correct, len(labels)) if complete else None,
                video_label_attempted_accuracy=ratio(video_correct, len(rows)),
                video_label_per_label={label:dict(
                    planned=sum(v == label for v in labels.values()),
                    attempted=sum(labels[r['case_id']] == label for r in rows),
                    correct=sum(labels[r['case_id']] == label and reported(r) == label for r in rows))
                    for label in LABELS},
                video_label_missed_as_normal=raw_missed, response_issue_counts=dict(issue_counts),
                localization_counts=localization,
                missed_as_normal=missed,
                refusal_or_block_rate=dict(count=counts['refused']+counts['safety_blocked'],
                    total=len(rows), rate=(counts['refused']+counts['safety_blocked'])/len(rows) if rows else None),
                classification_latency=latency([r['elapsed_s'] for r in rows if r['outcome'] == 'classified']),
                all_request_termination_latency=latency([r['elapsed_s'] for r in rows]),
                timeout_count=counts['timeout'], manual_review_count=sum(r.get('manual_review', False) for r in rows),
                cost=dict(known_estimate_subtotal_usd=str(sum(known, Decimal(0))),
                          unknown_requests=len(rows)-len(known),
                          total_estimate_usd=str(sum(known, Decimal(0))) if len(known) == len(rows) else None))
