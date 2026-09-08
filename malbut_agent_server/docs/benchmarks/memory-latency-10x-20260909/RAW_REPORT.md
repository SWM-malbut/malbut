# Memory latency comparison

Live API: True; model: gpt-5.6-luna; calls: 44/44.

Metric: JSON readiness, not durable HTTP delivery or audio.
B/C keep the turn pending until one deferred completion transaction.
Fixed responses are harness checks, not model latency evidence.

All 30 measured trials valid: False.
If false, use case-level results; do not rank differing subsets.

| Mode | Valid/measured | Reply median ms | Memory median ms |
|---|---:|---:|---:|
| A | 10/10 | 2105.06 | 2104.77 |
| B | 9/10 | 2818.78 | 2820.37 |
| C | 10/10 | 1699.18 | 3174.25 |

Known cost USD: 0.009197; usage complete: True.
Stop reason: completed.

Small sample: inspect case-level ranges, outputs and failures before choosing a production architecture.
