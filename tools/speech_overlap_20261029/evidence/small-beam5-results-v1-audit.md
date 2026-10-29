# Same-small decoder beam 5: reject adoption

The native comparison completed all 24 calls. Greedy reproduced all 12 prior current transcripts verbatim, CUDA0 was observed, and the fixed small model, six threads, T=0, no fallback and no prompt were retained. This is a decoder beam-search setting, unrelated to the microphone direction setting. Production remains greedy 1.

**Reject beam 5.** A one-edit reduction in original-clean and captured CER does not compensate for the captured monetary-quantity regression: **14억여원 → 14여원**. The persistent **55건 → 15건/15번** error is unchanged in all three input variants. Do not run a larger decoder grid or adopt this setting on these results.

| Input kind | Greedy edits / chars | Greedy strict CER | Beam 5 edits / chars | Beam 5 strict CER |
|---|---:|---:|---:|---:|
| original_clean | 24/156 | 15.38% | 23/156 | 14.74% |
| matched_interval_clean | 24/156 | 15.38% | 25/156 | 16.03% |
| exact_captured | 33/156 | 21.15% | 32/156 | 20.51% |

Strict CER removes spaces and punctuation, but preserves Korean number words versus digits. All four utterances per kind have nonzero strict error in both arms; this is **not** a statement that all have incorrect meaning. In particular, 2013년 remains numerically and substantively correct. The twelve inputs are three related variants of four utterances, not twelve independent test utterances.

| Utterance | Input kind | Greedy edits | Beam 5 edits | Paired outcome |
|---|---|---:|---:|---|
| 137_003_0029 | original_clean | 2 | 2 | Unchanged: 몬터균 for 몬터규 |
| 137_003_0029 | matched_interval_clean | 2 | 2 | Unchanged: 몬터균 for 몬터규 |
| 137_003_0029 | exact_captured | 4 | 4 | Unchanged: 몬터균 and 처리 for 초래 |
| 149_003_0057 | original_clean | 11 | 10 | Repairs 자정차 → 자동차; 임펠라 remains |
| 149_003_0057 | matched_interval_clean | 11 | 11 | Unchanged: 자정차 and 임펠라 |
| 149_003_0057 | exact_captured | 12 | 12 | Unchanged: 사정차 and 임펠라 |
| 112_003_0107 | original_clean | 4 | 4 | Unchanged; 2013년 is correct |
| 112_003_0107 | matched_interval_clean | 4 | 5 | Regresses 평가 → 평과 |
| 112_003_0107 | exact_captured | 4 | 4 | Unchanged; 2013년 is correct |
| 121_003_0017 | original_clean | 7 | 7 | Unchanged: 55건 → 15건 |
| 121_003_0017 | matched_interval_clean | 7 | 7 | Unchanged: 55건 → 15건 |
| 121_003_0017 | exact_captured | 13 | 12 | Loses 억, keeps wrong 15번; repairs 벌어들인 |

The six explicit numeric facts per input kind are 2일, 31일, 900대, 2013년, 55건 and 14억. Greedy preserves 5/6 in each kind; beam 5 preserves 5/6 in original/matched clean and 4/6 in captured audio. These are narrow manual content checks, not a complete semantic score. The missed 억 changes the amount by a large factor; retaining digits “14” is insufficient.

The changed captured case121 illustrates the limitation of edit count. Its reference is “한 특허법인은 전국을 돌며 쉰 다섯 건을 수임 십 사 억여원을 벌어들인 것으로 나타났다.”

- Greedy: “한 토커워비는 전국을 돌며 15번을 수임 14억여원을 벌어드린 것으로 나타났다.”
- Beam 5: “한 토커와비는 전국을 돌며 15번을 수임 14여원을 벌어들인 것으로 나타났다.”

Both malformed organization names remain wrong. Repairing 벌어드린 to 벌어들인 and altering that malformed name reduce the total edit count despite loss of the monetary scale. All 24 full transcripts, references and individual measures are retained in the linked audit JSON and immutable native results.

| Recorded per-call cost | Greedy 1 | Beam 5 |
|---|---:|---:|
| Mean wall seconds | 1.339669 | 1.188239 |
| Total wall seconds (12 calls) | 16.076033 | 14.258865 |
| Mean process cpu seconds | 0.310986 | 0.534287 |
| Total process cpu seconds (12 calls) | 3.731834 | 6.411439 |

Observed total wall time changed -11.30%; process CPU time changed +71.80%. The shorter wall times are not a CPU improvement. This is one measurement per arm/input with balanced alternating order and no additional warm-up; first-call/cold effects remain. Process CPU excludes GPU execution cost, model loading and the rest of the service. **Neither figure verifies the requested live service CPU or interruption-latency limits.**

Local audit rechecked 25 pack hashes, 21 fetched result hashes, all 12 exact WAV-to-float32 conversions, the 24-call identities/order/statuses, all 12 greedy replications and independently recomputed every Levenshtein score from native segment text. Native prepare recorded 53 asset hashes and 26 executable dependencies; current remote asset bytes were not re-read by this local audit. The existing three matched-clean source-tail coverage limitations remain explicit. No recordings, outputs, model settings, production candidates or prior labels were changed.

- Full paired audit JSON
- Verified native outputs
- Fetch manifest
- Independent local audit script
