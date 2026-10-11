# 100-review cost and runtime calculator - report

Generated offline from saved evidence. Rates file: `rates.csv`; assumptions: `assumptions.json`. No API key is used and no model is called by this report.

## Measured pilot (cost_100.csv, unchanged)

- Input checksum: `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e` (expected `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e`; match: True)
- Pilot IDs: 100 records - completed 100, quarantined 0, pending 0
- Unique texts sent to the enricher: 100; result-cache hits (cold): 0; (warm): 100 records served from saved results
- Workers: 1; enrichment batch size: 50; verification sample: 20.0 records
- **Cold run: wall-clock 124.99 s, API cost $0.015752, 27 attempts**
- **Warm run (same run dir, saved results): wall-clock 0.12 s, new enrichment calls 0, downstream calls 0, incremental API cost $0**
- Cost per 1,000 input rows: $0.1575; per completed record: $0.000158
- Throughput (cold, end to end): 0.80 records/s
- Attempts with unknown usage (left unresolved, not zeroed): 0

### By stage (cold run)

| Stage | Provider | Model | Effort | Requests | Attempts | Retries | Failed | Reviews sent | Uncached in | Cached in | Output (incl. reasoning) | Reasoning | Cost USD | Stage s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| enrich | openai | gpt-6-luna | none | 3 | 3 | 0 | 0 | 101 | 9 | 10,088 | 3,708 | 0 | 0.000986 | 88.4 |
| verify | typesafe | jev-1.13.0 | None | 20 | 20 | 0 | 0 | 20 | 38,191 | 0 | 11,381 | 0 | 0.001604 | 0.622 |
| group | openai | gpt-5-mini | low | 3 | 3 | 0 | 0 | 51 | 4,509 | 0 | 1,801 | 768 | 0.004729 | 15.833 |
| memo | openai | gpt-5-mini | medium | 1 | 1 | 0 | 0 | 20 | 5,681 | 0 | 3,506 | 2,240 | 0.008432 | 20.073 |

Prompt/schema versions: enrich enrich_v2/s2/t1 (gpt-6-luna, effort none, Flex tier, prompt_cache_key), verify jev_verify_q1 (jev-1.13.0), group_v1, memo_v1 (gpt-5-mini). Summed call latency is not wall-clock time when calls overlap; wall-clock values above come from the orchestrator clock.

### Rates used (editable `rates.csv`; each attempt is billed at the tier the provider reported)

| Model | Tier | Item | USD per unit | Source | Checked |
|---|---|---|---|---|---|
| gpt-6-luna | standard | input_uncached | 0.0000001000 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| gpt-6-luna | standard | input_cached | 0.0000000100 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| gpt-6-luna | standard | cache_write | 0.0000001250 | https://developers.openai.com/api/docs/models/gpt-6-luna | 2026-10-01 |
| gpt-6-luna | standard | output | 0.0000005000 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| gpt-5-mini | standard | input_uncached | 0.0000002500 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| gpt-5-mini | standard | input_cached | 0.0000000250 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| gpt-5-mini | standard | output | 0.0000020000 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| jev-1.13.0 | standard | input_uncached | 0.0000000420 | UNVERIFIED third-party figure (https://www.eesel.ai/blog/typesafe-jev-pricing) - confirm in console.typesafe.ai | 2026-10-11 |
| jev-1.13.0 | standard | input_cached | 0.0000000420 | UNVERIFIED (no cached-input discount known) | 2026-10-11 |
| jev-1.13.0 | standard | output | 0.0000000000 | UNVERIFIED third-party figure: output not billed - confirm in console | 2026-10-11 |
| gpt-6-luna | flex | input_uncached | 0.0000000500 | https://developers.openai.com/api/docs/models/gpt-6-luna (Batch and Flex are 50% of Standard) | 2026-10-11 |
| gpt-6-luna | flex | input_cached | 0.0000000050 | https://developers.openai.com/api/docs/models/gpt-6-luna (Batch and Flex are 50% of Standard) | 2026-10-11 |
| gpt-6-luna | flex | output | 0.0000002500 | https://developers.openai.com/api/docs/models/gpt-6-luna (Batch and Flex are 50% of Standard) | 2026-10-11 |

Formula: item_cost = billed_units x price_per_unit; total = sum of items. Uncached input = input - cached - cache_write. Every row of `usage.csv` maps to a `request_id` in `pilot_calls.jsonl`.

### Local compute (separate from API spend)

- Local work (CSV parsing, SQLite, hashing, ranking) runs on a laptop CPU; measured inside the wall-clock times. Electricity/hardware cost is not metered and is listed as unknown, not zero.

## Full-run projection

Scope: 660,622 rows; 660,609 nonempty classifications; 13 empty-text quarantines. Exact-text reuse reduces first-pass work to 484,189 distinct texts.

Per-unit inputs from the measurement: enrichment $0.00000986 and 0.8840 s per distinct text; verification $0.00008020 per record x 1,937 records; group+memo fixed $0.0297 (added once); fallback $0.001425 per routed review.

| Scenario | Texts | Enrich $ | Fallback $ | Verify $ | Group+memo $ | **Total API $** | Workers | Hours | Over budget? |
|---|---|---|---|---|---|---|---|---|---|
| base|reuse | 484,189 | 5.01 | 0.34 | 0.16 | 0.03 | **5.54** | 16 | 10.00 | no |
| base|no_reuse | 660,609 | 6.84 | 0.47 | 0.16 | 0.03 | **7.50** | 16 | 13.56 | no |
| conservative|reuse | 484,189 | 5.49 | 1.38 | 0.16 | 0.03 | **7.06** | 8 | 28.99 | no |
| conservative|no_reuse | 660,609 | 7.49 | 1.88 | 0.16 | 0.03 | **9.56** | 8 | 39.36 | no |

## Controls

- Spending limit: $10.00 (enforced in code by a shared ledger that reserves worst-case cost before each request)
- Output-token cap per enrichment request: 4,000
- Maximum workers: 16
- Maximum fallback fraction: 0.002
- Scenario assumptions: {"base": {"retry_rate": 0.05, "fallback_fraction": 0.0005, "workers": 16, "parallel_efficiency": 0.8, "fixed_overhead_s": 900}, "conservative": {"retry_rate": 0.15, "fallback_fraction": 0.002, "workers": 8, "parallel_efficiency": 0.6, "fixed_overhead_s": 1800}}

Edit `rates.csv` or `assumptions.json` and rerun `python cost/calculator.py` to recompute. Doubling all API rates doubles every API dollar figure while measured times and local-compute notes stay unchanged; changing projected volume never changes the measured pilot.
