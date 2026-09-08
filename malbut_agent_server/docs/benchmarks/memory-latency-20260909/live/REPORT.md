# Memory latency comparison

Live API: True; model: gpt-5.6-luna; calls: 1/28.

Metric: JSON readiness, not durable HTTP delivery or audio.
B/C keep the turn pending until one deferred completion transaction.
Fixed responses are harness checks, not model latency evidence.

All 18 measured trials valid: False.
If false, use case-level results; do not rank differing subsets.

| Mode | Valid/measured | Reply median ms | Memory median ms |
|---|---:|---:|---:|
| A | 0/0 | n/a | n/a |
| B | 0/0 | n/a | n/a |
| C | 0/0 | n/a | n/a |

Known cost USD: 0.000000; usage complete: False.
Stop reason: api_access_or_configuration_error.

Small sample: inspect case-level ranges, outputs and failures before choosing a production architecture.
