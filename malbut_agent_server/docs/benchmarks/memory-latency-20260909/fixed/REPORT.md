# Memory latency comparison

Live API: False; model: gpt-5.6-luna; calls: 28/28.

Metric: JSON readiness, not durable HTTP delivery or audio.
B/C keep the turn pending until one deferred completion transaction.
Fixed responses are harness checks, not model latency evidence.

All 18 measured trials valid: True.
If false, use case-level results; do not rank differing subsets.

| Mode | Valid/measured | Reply median ms | Memory median ms |
|---|---:|---:|---:|
| A | 6/6 | 1.09 | 1.03 |
| B | 6/6 | 0.76 | 1.40 |
| C | 6/6 | 0.67 | 1.40 |

Known cost USD: 0.000000; usage complete: False.
Stop reason: completed.

Small sample: inspect case-level ranges, outputs and failures before choosing a production architecture.
