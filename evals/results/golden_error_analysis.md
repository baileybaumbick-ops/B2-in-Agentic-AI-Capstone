# Golden-50 evaluation and error analysis

**Reference:** 50 reviews hand-labeled by Bailey Baumbick ([`evals/golden/golden_50_label_sheet.xlsx`](../golden/golden_50_label_sheet.xlsx), imported to [`golden_50_human.csv`](../golden/golden_50_human.csv) and [`golden_50_benchmark.json`](../golden/golden_50_benchmark.json)). The labels were completed and corrected (typos and topic/subtopic consistency only) **before** any model prediction for these reviews was viewed, and were not changed after scoring.

**Predictions:** the final full-corpus run (`runs/full`). The golden texts went through the same pipeline as every other review; their human labels were never model inputs, examples or thresholds. Prompts were tuned only on the development files (`cost_100`, `checkpoint_500`, `analysis_10000`).

**Scoring:** [`golden_eval.py score`](../golden_eval.py) (code only). Results: [`golden_eval.json`](golden_eval.json); per case: [`golden_per_case.csv`](golden_per_case.csv). Fifty cases are a small diagnostic sample, not a precise population accuracy estimate.

## Results

| Metric | Result |
|---|---|
| Valid predictions | 50 / 50 |
| Topic agreement | **0.80** |
| Intent agreement | **0.88** |
| Severity exact agreement | **0.80**; MAE **0.26** |
| Topic + intent + severity all correct | 0.62 |
| Sentiment: within the predeclared tolerance of 0.5 | 0.98; MAE 0.12 |
| evidence_quote is an exact source substring | 1.00 (checked in code) |
| Entities not present in the text | 0 of 23 |
| needs_review vs. human "ambiguous" (10 cases) | precision 0.71, recall 0.50 |

Topic confusion (human -> model): `other` 27/32 correct (3 -> usability, 2 -> catalog); `usability` 4/9 correct (2 -> billing, 2 -> catalog, 1 -> other); billing 3/3, playback 4/4, catalog 1/1, downloads 1/1.
Intent confusion: complaint 17/17, praise 19/19, request 1/1, cancellation 1/3 (2 -> complaint), unclear 5/10 (2 -> complaint, 1 -> cancellation, 1 -> praise, 1 -> request).

## Error analysis (19 cases with at least one topic/intent/severity disagreement)

| Category | Cases | Examples (human -> model) | Interpretation |
|---|---|---|---|
| Model applied the written definitions; human label departs from them | about 8 | free-tier song limits: usability -> **billing** (definitions place premium-only controls in billing); recommendation praise: usability -> **catalog** (recommendations are catalog); severity 5 -> **3** without explicit financial/privacy/data harm; "Worst app ever too much ad": cancellation -> **complaint** (no stated departure) | Disagreement reflects rule application rather than reading errors; the human labels were kept as-is to avoid post-hoc tuning of the reference |
| Non-English text | 2 | Indonesian "lirik nya kdang gaada" (lyrics missing): unclear -> catalog complaint; Hindi "ads ko thoda kumm Karo" (fewer ads): unclear -> usability request | The model read the language; the human marked these unclear. A multilingual reviewer would likely side with the model |
| Severity boundary 3 vs 4 | 3 | "cannot playback the songs" and "can't even o[pen]": 3 -> 4; lyrics not loading: 2 -> 3 | "Core task clearly blocked" vs. "degraded" is the hardest boundary; within-1 agreement is 100% on these |
| Genuine model errors / judgment | about 6 | political rant about banned songs: other/s2 -> catalog/s3; "Ads were okay, but limited functionality": usability -> billing; boycott slogan: unclear -> cancellation; "Duo Premium .... No ads at all!": unclear -> usability praise; "This update very bad for everyone": other -> usability | Mostly the documented `other` vs specific-topic boundary and boycott wording |

**What this means for the result:** topic errors concentrate on the `usability` / `billing` / `catalog` / `other` boundaries. The leading issue (`billing.free_tier_restrictions`, 61,438 complaints, priority 177,926) sits exactly on the usability/billing boundary. The *issue* is robust: it ranks first by priority score whichever topic it is filed under. The *area label* is not: the course definitions put explicitly premium-only controls in billing, which is what the pipeline applied; under the alternative reading (free-tier limits as usability), usability's area severity_sum would exceed billing/support's. The recommended work (free-tier limits and their messaging) is the same either way; the memo's limitations section notes the billing/usability boundary dependency, and the README states its area-level consequence. The independent verifier (Jev, 1,937 sampled reviews) shows the same pattern: its most common topic disagreements are usability -> other (50) and catalog -> other (35) ([`runs/full/verify/verify_report.json`](../../runs/full/verify/verify_report.json)).
