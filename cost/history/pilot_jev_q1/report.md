# 100-review cost and runtime calculator - report

Generated offline from saved evidence. Rates file: `rates.csv`; assumptions: `assumptions.json`. No API key is used and no model is called by this report.

## Measured pilot (cost_100.csv, unchanged)

- Input checksum: `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e` (expected `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e`; match: True)
- Pilot IDs: 100 records - completed 100, quarantined 0, pending 0
- Unique texts sent to the enricher: 100; result-cache hits (cold): 0; (warm): 100 records served from saved results
- Workers: 1; enrichment batch size: 1; verification sample: 20.0 records
- **Cold run: wall-clock 53.04 s, API cost $0.026640, 105 attempts**
- **Warm run (same run dir, saved results): wall-clock 0.10 s, new enrichment calls 0, downstream calls 0, incremental API cost $0**
- Cost per 1,000 input rows: $0.2664; per completed record: $0.000266
- Throughput (cold, end to end): 1.89 records/s
- Attempts with unknown usage (left unresolved, not zeroed): 0

### By stage (cold run)

| Stage | Provider | Model | Effort | Requests | Attempts | Retries | Failed | Reviews sent | Uncached in | Cached in | Output (incl. reasoning) | Reasoning | Cost USD | Stage s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| enrich | typesafe | jev-1.13.0 | None | 100 | 100 | 0 | 0 | 100 | 247,137 | 0 | 60,781 | 0 | 0.010380 | 10.789 |
| verify | openai | gpt-5-mini | low | 1 | 1 | 0 | 0 | 20 | 2,100 | 0 | 1,392 | 768 | 0.003309 | 10.237 |
| group | openai | gpt-5-mini | low | 3 | 3 | 0 | 0 | 52 | 4,750 | 0 | 1,871 | 832 | 0.004929 | 14.524 |
| memo | openai | gpt-5-mini | medium | 1 | 1 | 0 | 0 | 21 | 5,957 | 0 | 3,266 | 1,984 | 0.008021 | 17.366 |

Prompt/schema versions: enrich jev_q1 (jev-1.13.0) s1/t1, verify_v1, group_v1, memo_v1. Summed call latency is not wall-clock time when calls overlap; wall-clock values above come from the orchestrator clock.

### Rates used (editable `rates.csv`, standard tier = the tier actually used)

| Model | Item | USD per unit | Source | Checked |
|---|---|---|---|---|
| gpt-6-luna | input_uncached | 0.0000001000 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| gpt-6-luna | input_cached | 0.0000000100 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| gpt-6-luna | cache_write | 0.0000001250 | https://developers.openai.com/api/docs/models/gpt-6-luna | 2026-10-01 |
| gpt-6-luna | output | 0.0000005000 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| gpt-5-mini | input_uncached | 0.0000002500 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| gpt-5-mini | input_cached | 0.0000000250 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| gpt-5-mini | output | 0.0000020000 | https://developers.openai.com/api/docs/pricing | 2026-10-01 |
| jev-1.13.0 | input_uncached | 0.0000000420 | UNVERIFIED third-party figure (https://www.eesel.ai/blog/typesafe-jev-pricing) - confirm in console.typesafe.ai | 2026-10-11 |
| jev-1.13.0 | input_cached | 0.0000000420 | UNVERIFIED (no cached-input discount known) | 2026-10-11 |
| jev-1.13.0 | output | 0.0000000000 | UNVERIFIED third-party figure: output not billed - confirm in console | 2026-10-11 |

Formula: item_cost = billed_units x price_per_unit; total = sum of items. Uncached input = input - cached - cache_write. Every row of `usage.csv` maps to a `request_id` in `pilot_calls.jsonl`.

### Local compute (separate from API spend)

- Local work (CSV parsing, SQLite, hashing, ranking) runs on a laptop CPU; measured inside the wall-clock times. Electricity/hardware cost is not metered and is listed as unknown, not zero.

## Full-run projection

Scope: 660,622 rows; 660,609 nonempty classifications; 13 empty-text quarantines. Exact-text reuse reduces first-pass work to 484,189 distinct texts.

Per-unit inputs from the measurement: enrichment $0.00010380 and 0.1079 s per distinct text; verification $0.00016545 per record x 1,937 records; group+memo fixed $0.0302 (added once); fallback $0.001425 per routed review.

| Scenario | Texts | Enrich $ | Fallback $ | Verify $ | Group+memo $ | **Total API $** | Workers | Hours | Over budget? |
|---|---|---|---|---|---|---|---|---|---|
| base|reuse | 484,189 | 51.26 | 0.34 | 0.32 | 0.03 | **51.96** | 8 | 2.34 | no |
| base|no_reuse | 660,609 | 69.94 | 0.47 | 0.32 | 0.03 | **70.76** | 8 | 3.14 | no |
| conservative|reuse | 484,189 | 55.28 | 1.38 | 0.32 | 0.03 | **57.01** | 4 | 6.20 | no |
| conservative|no_reuse | 660,609 | 75.43 | 1.88 | 0.32 | 0.03 | **77.66** | 4 | 8.28 | **YES - exceeds limit** |

## Controls

- Spending limit: $75.00 (enforced in code by a shared ledger that reserves worst-case cost before each request)
- Output-token cap per enrichment request: 6,000
- Maximum workers: 8
- Maximum fallback fraction: 0.002
- Scenario assumptions: {"base": {"retry_rate": 0.02, "fallback_fraction": 0.0005, "workers": 8, "parallel_efficiency": 0.85, "fixed_overhead_s": 600}, "conservative": {"retry_rate": 0.1, "fallback_fraction": 0.002, "workers": 4, "parallel_efficiency": 0.7, "fixed_overhead_s": 1800}}

Edit `rates.csv` or `assumptions.json` and rerun `python cost/calculator.py` to recompute. Doubling all API rates doubles every API dollar figure while measured times and local-compute notes stay unchanged; changing projected volume never changes the measured pilot.
