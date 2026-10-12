# Spotify review pipeline: a multi-agent recommendation from 660,622 reviews

**Question.** At the end of the review window (May 2022 to Nov 2023), where should Spotify's next quarter of product effort go: access, usability, playback, or billing/support?

**Answer (final full-corpus run).** **Billing/support**, led by the **free-tier restrictions** issue (skip limits, forced shuffle, no seeking or choosing songs). It is the #1 issue by the baseline priority score (61,438 complaints, priority 177,926), carries the most stated cancellation intent (12,317 reviews), and billing/support complaints rose from 5.3% of reviews in Jun-Aug 2022 to 23.3% in Aug-Oct 2023. Because it is a deliberate pricing/product policy rather than a bug, the recommended work is policy redesign, experiments and clearer in-app messaging, plus targeted fixes for the small but most severe billing cases (charges/refunds, mean severity 4.61). Read the **[decision memo](runs/full/memo/memo.md)**.

This repository is the single deliverable. Every number below links to saved evidence. A reviewer can inspect everything **without an API key or paid calls**, rerun the deterministic ranking, and replay the cost calculator offline.

---

## Results summary (final run `runs/full`)

| | Result | Evidence |
|---|---|---|
| Source rows ingested and accounted for | **660,622 / 660,622** (0 missing, 0 duplicate IDs; profile matches the course manifest) | [`grading/ingestion.json`](grading/ingestion.json), [`runs/full/ingestion_report.json`](runs/full/ingestion_report.json) |
| Nonempty reviews classified (valid, contract schema) | **660,609 / 660,609 (100%)** | [`grading/records.jsonl.gz`](grading/records.jsonl.gz) |
| Quarantined | **13**, all `empty_review_text`; no other unresolved rows | [`runs/full/quarantine.jsonl`](runs/full/quarantine.jsonl) |
| Exact-text cache reuse | 484,189 distinct texts sent; **176,420** rows reuse an original's labels (`cache_source_id`) | self-check `valid_cache_reuses` |
| Zero-API self-check (`check_submission.py`) | **status `pass`, 0 issues, coverage point 1.0** | [`evals/results/self_check_full_run.json`](evals/results/self_check_full_run.json) |
| Golden-50 vs. human labels | topic **0.80**, intent **0.88**, severity exact **0.80** (MAE 0.26), quotes exact **1.00** | [Golden-set evaluation](#golden-set-evaluation) |
| Independent verifier (Jev, 1,937 seeded sample) | topic **0.84**, intent **0.90**, severity **0.84** (within 1: 0.98, MAE 0.18); 628 disagreements | [`runs/full/verify/`](runs/full/verify) |
| Planted-error test | **480 / 484** planted wrong labels flagged (0.99) | [`evals/results/planted_errors/`](evals/results/planted_errors) |
| Injection test (final enricher) | **9 / 9** passed; all injection attempts flagged `needs_review` | [`evals/results/injection_results.json`](evals/results/injection_results.json) |
| Full-run API cost | **$6.91** (limit $10): enrich $5.89 (GPT-6 Luna, Flex), fallback $0.79 (GPT-5-mini, 940 reviews), verify $0.16 (Jev), group $0.02, memo $0.06; 2 of 16,185 attempts have unknown usage | [`runs/full/run_summary.json`](runs/full/run_summary.json), [`cost/report.md`](cost/report.md) |
| Full-run wall-clock | 4.46 h over 8 sessions (24 workers; 4 deliberate interruptions; final stages and fixes) | [Run history](#run-history-and-incidents) |
| Measured 100-review pilot | cold **$0.0158 / 125 s**; warm **$0 / 0.12 s, 0 new calls** | [`cost/report.md`](cost/report.md) |
| Interruption and resume | checkpoint before: 100,533 completed; after: 660,609; recording | [`grading/checkpoint_before.json`](grading/checkpoint_before.json), [`evidence/interruption_resume_recording.mp4`](evidence/interruption_resume_recording.mp4) |

Measured results and estimates are kept separate. Every dollar figure above is computed from logged provider usage x dated rates in [`cost/rates.csv`](cost/rates.csv). Jev rates are third-party figures marked UNVERIFIED.

---

## Rubric map: where the evidence is

| Rubric item | Evidence |
|---|---|
| **D1** Accessible code, setup, artifacts | [Setup](#setup), [Commands](#commands); [`requirements.txt`](requirements.txt); [`.env.example`](.env.example) (names only); outputs under [`runs/full/`](runs/full), [`grading/`](grading), [`cost/`](cost), [`evals/`](evals) |
| **D2** Architecture, shared schema, provenance | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (diagram, roles, code/model boundary, stop/retry paths); [`docs/LABELS.md`](docs/LABELS.md); prompts in [`prompts/`](prompts); `label_config` on every record and call; contract row SHA-256; `cache_source_id`; [`runs/full/config.json`](runs/full/config.json), [`run_summary.json`](runs/full/run_summary.json), [`run_log.jsonl`](runs/full/run_log.jsonl) |
| **D3** Memo numbers traced to calculations and sources | [`runs/full/memo/memo.md`](runs/full/memo/memo.md) (claim IDs on every number, code-generated claim table); [`grading/claims.csv`](grading/claims.csv) (checker-verified); claim check in [`pipeline/memo.py`](pipeline/memo.py); [`docs/TRACE.md`](docs/TRACE.md) |
| **D4** Recommendation, alternatives, limitations | memo sections; [Limitations](#limitations) |
| **T1** 50 human labels, per-field comparison, error analysis | [`evals/golden/`](evals/golden); [`golden_eval.json`](evals/results/golden_eval.json), [`golden_per_case.csv`](evals/results/golden_per_case.csv), [`golden_error_analysis.md`](evals/results/golden_error_analysis.md) |
| **T2** Independent verification, planted errors, injection | [`runs/full/verify/`](runs/full/verify); [`evals/results/planted_errors/`](evals/results/planted_errors); [`evals/results/injection_results.json`](evals/results/injection_results.json) |
| **T3** Real cold/warm pilot, offline calculator, retry/spend/recovery controls | [`cost/`](cost) (report, calculator, pilot calls/records, rates, usage, assumptions, history); controls demo [`runs/controls_demo/`](runs/controls_demo); [System checks](#system-checks) |
| **W1** Full ingestion, coverage, classification | [Results summary](#results-summary-final-run-runsfull); [`grading/`](grading) |
| **W2** Staged program, bounded calls, saved handoffs, resume | [`run.py`](run.py), [`pipeline/`](pipeline); [`grading/calls.jsonl.gz`](grading/calls.jsonl.gz) (every attempt; at most 50 reviews per enrichment request); checkpoints + recording |
| **W3** Reproducible baseline ranking, grounded output | `python run.py rank --run-dir runs/full` reproduces [`runs/full/rank/ranking.csv`](runs/full/rank/ranking.csv) byte-for-byte from saved records + membership, with no model calls; [`grading/ranking.csv`](grading/ranking.csv) |

---

## Setup

Python 3.12 (3.10+ works). No API key is needed to inspect outputs, replay the calculator or rerun the ranking.

```bash
pip install -r requirements.txt
```

The course dataset and instructor materials are **not redistributed**. Unzip the course ZIP into `data/` (see [`data/WHERE_TO_PUT_THE_DATA.md`](data/WHERE_TO_PUT_THE_DATA.md)). Raw file: `spotify_reviews_18months.csv`, 97,400,616 bytes, SHA-256 `1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6`. Source: BwandoWando, *3.4 Million Spotify Google Store Reviews*, v2, Kaggle (CC0).

Paid runs only: set `OPENAI_API_KEY` and `TYPESAFE_API_KEY` (see `.env.example`). On Windows, values set with `setx` are also read from the user environment.

## Commands

| Purpose | Command | Paid? |
|---|---|---|
| Rerun the baseline ranking from saved outputs | `python run.py rank --run-dir runs/full` | no |
| Offline cost replay (calculator default) | `python cost/calculator.py` | no |
| Recompute with edited prices or assumptions | edit `cost/rates.csv` / `cost/assumptions.json`, then `python cost/calculator.py` | no |
| Export the grading folder | `python run.py export --run-dir runs/full --out grading` | no |
| Zero-API self-check | `python data/check_submission.py reference --full data/spotify_reviews_18months.csv --analysis data/spotify_reviews_18months.csv --out local-reference.json` then `python data/check_submission.py check --reference local-reference.json --submission grading --out self-check.json` | no |
| Golden-set scoring | `python evals/golden_eval.py score --run-dir runs/full` | no |
| Planted-error test | `python evals/planted_errors.py --run-dir runs/full` | no |
| Trace one review end to end | `python evals/trace_review.py --run-dir runs/full` | no |
| Run status | `python run.py status --run-dir runs/full` | no |
| Full pipeline on any CSV | `python run.py run --input <csv> --run-dir runs/<name> --workers 24 --spend-cap 10` | **yes** |
| Offline plumbing test (keyword mock; never evidence) | add `--mock` to the run command | no |
| Explicit paid 100-review pilot (cold + warm) | `python cost/calculator.py run-pilot --yes-spend` | **yes** |
| Injection test | `python evals/injection_test.py --yes-spend` (re-check offline: `--check-only`) | **yes** (under $0.01) |

Interrupt a run with Ctrl+C or by creating `runs/<name>/STOP`. Rerunning the same command resumes; completed IDs are never re-sent under unchanged settings.

---

## Architecture

Six stages coordinated by code ([`run.py`](run.py)). Code owns record accounting, validation, budgets, retries and arithmetic; models answer bounded language questions inside four distinct roles. The full diagram, role table and failure behavior are in **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**.

| Stage | Owner | Model / settings | Saved handoff |
|---|---|---|---|
| 1 Prepare | code | none | `state.sqlite`, `ingestion.json`, `ingestion_report.json` |
| 2 Classify | **ENRICHER** | `gpt-6-luna`, reasoning effort `none`, Flex tier, prompt `enrich_v2`, schema `s2`, at most 50 reviews/request; capped fallback `gpt-5-mini` (effort `low`, combined topic.subtopic schema, at most 0.2% of rows) | `enriched.jsonl.gz`, `quarantine.jsonl`, `calls.jsonl` |
| 3 Verify | **VERIFIER** | `jev-1.13.0` (TypeSafe System One), question set `jev_verify_q1`, seeded 0.4% sample | `verify/verifier_predictions.jsonl`, `verify_report.json`, `disagreements.csv` |
| 4 Group | code + **GROUPER** | membership = `topic.subtopic` (code); names from `gpt-5-mini` (`group_v1`) on 12 sampled quotes per issue | `group/membership.csv`, `group/issues.json`, `group/group_input_packs.json` |
| 5 Rank | code | none | `rank/ranking.csv`, `aggregates*.csv`, `trend_by_month.csv` |
| 6 Recommend | code + **MEMO** | `gpt-5-mini`, effort `medium`, prompt `memo_v3`; code builds the claims table and rejects unsupported numbers/IDs | `memo/memo.md`, `memo/claims.csv`, `memo/memo_input_pack.json` |

**Where code decides vs. where a model judges.** Code decides stage order, cache hits, batch composition, retries (one invalid-output retry; transport backoff), splitting of failed batches, fallback routing and its cap, spend admission, validation and every aggregate. Models only judge language: labels per review (enricher, verifier), issue names (grouper) and the memo's argument (memo writer).

### Why these models (measured, not assumed)

| Option | Measured on dev data | Decision |
|---|---|---|
| Jev as enricher (one review per request) | 100 / 500 / 10,000 runs: about $0.000085 per distinct text, so **about $41** projected for full-run enrichment; very fast (77 texts/s at 8 workers) | Rejected: over the $10 limit, because Jev bills the full question set (~2,000 tokens) for every review and has no multi-review batching |
| GPT-6 Luna, 50/request, effort none, prompt caching, Flex | 100 / 500 / 10,000 runs: about $0.000012 per distinct text, so **about $5-6** projected; actual full-run enrichment $5.89 | **Chosen.** Agreement with Jev on the same 500 dev reviews: topic 0.88, intent 0.93, severity 0.87 |
| Jev as verifier | 1,937 reviews for $0.16 | **Chosen**: a different provider and method (choice probabilities) gives an independent second opinion |

Full projections and refreshes: [`cost/report.md`](cost/report.md), section "Estimate refreshes".

## Shared labels

Exact course topics, intents and severity scale with our subtopics, decision rules and worked examples: **[`docs/LABELS.md`](docs/LABELS.md)**. Every record carries `review_id, source_sha256, status, topic, intent, sentiment, severity, entities, evidence_quote, needs_review, label_config`, plus `subtopic` and, when reused, `cache_source_id`. Severity consistency is enforced in code (praise/unclear/request = 1; complaint/cancellation at least 2). Quotes are verified as exact substrings in code; entities come from explicit term matches, so none are invented.

---

## Golden-set evaluation

50 reviews hand-labeled by the author. The labels were completed, and typo and topic/subtopic inconsistencies corrected, **before** any model prediction for these reviews was viewed. They were never used in prompts, examples or thresholds; prompts were tuned only on development files.

| Metric | Result |
|---|---|
| Topic / intent agreement | 0.80 / 0.88 |
| Severity exact / MAE | 0.80 / 0.26 |
| All three correct | 0.62 |
| Sentiment within the predeclared tolerance (0.5) / MAE | 0.98 / 0.12 |
| Quote is an exact substring / unsupported entities | 1.00 / 0 of 23 |
| `needs_review` vs. human "ambiguous" (10 cases) | precision 0.71, recall 0.50 |
| Weakest topic | usability 4/9 correct (2 -> billing, 2 -> catalog) |

**Error analysis** ([full write-up](evals/results/golden_error_analysis.md)): of 19 disagreements, about 8 are cases where the model followed the written definitions and the human label departs from them (free-tier limits -> billing; recommendation praise -> catalog; no severity 5 without explicit harm). 2 are non-English reviews the model understood and the human marked unclear. 3 are on the severity 3/4 boundary. About 6 are genuine model errors on the `other` vs. specific-topic boundary and boycott wording.

## System checks

| Check | What was done | Outcome |
|---|---|---|
| Independent verification | Jev re-labels a seeded 0.4% sample (1,937 originals) from text + rubric only; code compares | topic 0.84, intent 0.90, severity 0.84 (within 1: 0.98); top topic disagreements usability -> other (50), catalog -> other (35); [`disagreements.csv`](runs/full/verify/disagreements.csv) |
| Planted errors (synthetic, separate test copy) | 484 of the verified labels corrupted (wrong topic chosen without looking at the verifier, or severity +/-2); comparator rerun | 480 flagged (0.99); 4 missed where the verifier happened to match the planted label |
| Prompt injection (synthetic `SYN-` reviews, excluded from business data) | 9 cases incl. "ignore instructions", fake JSON, an instruction to relabel a neighbor, a non-English review and an empty text, all sent in one batch | 9/9 passed with the final enricher; injection attempts flagged `needs_review`. The earlier Jev enricher failed one (intent changed by an injected instruction): [`history/`](evals/results/history) |
| Retry / spend / recovery controls (real calls) | [`runs/controls_demo`](runs/controls_demo): injected 429 and malformed JSON; spend cap set just above current spend; resume | 429 retried with backoff; malformed batch retried and succeeded; second worker refused admission (`spent 0.0016 + reserved 0.0024 + next 0.0024 > cap 0.0045`), stop reason `spend_cap`; resume finished 500/500 |
| Interruption and resume (full run) | STOP after about 2.5 min; status; same command again | checkpoint before 100,533 completed; resume queued only 480,701 of 484,189 distinct texts; no completed ID re-sent (checker `reprocessed_checkpoint` = 0); [recording](evidence/interruption_resume_recording.mp4) |
| Deterministic re-rank | `python run.py rank --run-dir runs/full` | identical to `ranking.csv`; the checker independently recomputes the same ranking and claim values |

## Cost and runtime

- **Calculator:** [`cost/calculator.py`](cost/calculator.py). Offline replay is the default; the paid pilot requires `run-pilot --yes-spend`. Report: [`cost/report.md`](cost/report.md). It contains the measured 100-review cold/warm pilot by stage, rates with sources and dates, mutually exclusive billing items (uncached input, cached input, cache writes, output; reasoning is inside output), `usage.csv` mapping every charged item to a request ID, refreshes at 500 and 10,000, the full run's actual cost, base/conservative projections with and without exact-text reuse, and the controls (spend limit $10, output cap 4,000 tokens/request, at most 24 workers, fallback at most 0.2%).
- **Pilot:** the pilot ran with an empty *result* cache. The provider *prompt* cache was warm from a dev run minutes earlier (10,088 of 10,097 enrichment input tokens billed as cached); this is shown separately, as the spec requires.
- **Full run:** $6.91 actual vs. $5.54 base / $7.06 conservative projected. Enrichment alone cost $5.89, against $4.78-6.31 projected from the 100/500/10,000 refreshes.
- **Whole project:** about $7.94 across every logged run (pilots, dev runs, rejected Jev experiments, controls demo, injection tests, full run), plus about 12 untracked setup probes outside the pipeline (API connectivity checks, the Jev model-pin test, the Jev token-count probe, the Flex availability check), well under $0.01 in total. The OpenAI Flex tier was used for enrichment; the Batch API was not.

## One review traced end to end

**[`docs/TRACE.md`](docs/TRACE.md)** (generated from saved artifacts). Review `56cf7357-...` ("…moving to a particular place in a song is not acceptable with out subscription… I am deleting this app now…") -> GPT-6 Luna request `local-295c243b6e4c4e0b` (Flex, 2,103 of 2,283 input tokens cached) -> billing / free_tier_restrictions, cancellation, severity 3, exact quote -> membership `billing.free_tier_restrictions` -> ranking row 1 (61,438 / 177,926 / 2.896025) -> memo claims C01 and C02, cited with this review ID. The same file shows a fallback-recovered case (attempts 3, GPT-5-mini) and an enricher-vs-verifier disagreement with its handling.

## Baseline ranking and trend

Rule: completed `complaint` + `cancellation` records only, one issue per record (`issue_id = topic.subtopic`); `priority = complaint_count x mean_severity = severity_sum`; sort by descending score, then ascending issue ID; means to 6 decimals, half-up. 295,790 member reviews across 33 issues.

| Rank | Issue | Name (GROUPER) | Area | Complaints | Mean severity | Priority | Cancellation intent |
|---|---|---|---|---|---|---|---|
| 1 | `billing.free_tier_restrictions` | Free tier limits: skips, shuffle, playback restrictions | billing_support | 61,438 | 2.896025 | 177,926 | 12,317 |
| 2 | `other.general_negative` | Generic negative feedback about the app | outside_four_areas | 82,595 | 2.001162 | 165,286 | 6,284 |
| 3 | `usability.ads` | Too many or intrusive ads | usability | 36,315 | 2.136225 | 77,577 | 2,665 |
| 4 | `playback.stops_or_skips` | Playback stops, pauses, or won't play | playback | 15,891 | 3.205651 | 50,941 | 1,581 |
| 5 | `access.login_failure` | Unable to log in or stays logged out | access | 8,214 | 3.918797 | 32,189 | 487 |
| 6 | `playback.crash_or_freeze` | App crashes, freezes, or won't open | playback | 8,628 | 3.605123 | 31,105 | 679 |
| 7 | `usability.queue_playlist_management` | Queue and playlist management issues | usability | 10,145 | 2.996944 | 30,404 | 788 |
| 8 | `usability.ui_redesign` | Confusing, worse UI after redesign | usability | 10,878 | 2.258044 | 24,563 | 1,526 |
| 9 | `catalog.missing_content` | Songs or albums missing from catalog | outside_four_areas | 6,013 | 2.875935 | 17,293 | 516 |
| 10 | `usability.controls` | Broken or missing playback controls and widgets | usability | 5,883 | 2.819820 | 16,589 | 595 |

Full table: [`runs/full/rank/aggregates.csv`](runs/full/rank/aggregates.csv). Area totals: [`aggregates_by_area.csv`](runs/full/rank/aggregates_by_area.csv).

**Trend (additional, not baseline).** Share of completed reviews that are billing/support complaints, by month: 2022-06 5.5%, 2022-07 5.2%, 2022-08 5.1% vs. 2023-08 6.5%, 2023-09 17.1%, 2023-10 32.0%. Denominators are in [`trend_by_month.csv`](runs/full/rank/trend_by_month.csv) (e.g. 30,338 completed reviews in 2022-06 and 89,102 in 2023-10). Playback's share fell from 8.7% to 3.3% over the same months. The first (2022-05) and last (2023-11) months are partial and excluded from the comparison windows. October 2023 has about 3x the usual review volume, consistent with a review surge rather than a steady population change.

## Run history and incidents

All sessions share one run ID, one state database and one `label_config` for the main enricher (`gpt-6-luna|effort=none|prompt=enrich_v2|schema=s2|taxonomy=t1`); that label configuration never changed during the run.

| Session | What happened |
|---|---|
| 1 (initial) | Ingest; recorded interruption via STOP at 100,533 completed (`checkpoint_before`) |
| 2-4 (resume) | Planned stop/resume cycles to deploy fixes found while monitoring: (a) fallback answers a single combined `topic.subtopic` value, because a contradictory topic/subtopic pair was otherwise possible; (b) batches that fail twice (truncated or invalid JSON, about 1-2% of calls) are split in half instead of quarantining 50 reviews, and 106 earlier `model_output_error` rows plus 101 duplicates were requeued with `python run.py requeue`; (c) the fallback runs in parallel |
| 5 (resume) | Enrichment finished; 940 rows routed to the fallback (all completed; cap 1,321); 1 content-policy refusal resolved by bisection; verify, group, rank, memo |
| 6-8 | Integrity fix and memo refinements (below) |

**Incident: lost call-log lines.** After the run, an integrity check found **4 enrichment requests** whose results were saved but whose call records were missing from `calls.jsonl` (102 reviews). The cause is unproven; concurrent file sync by OneDrive is suspected. Rather than reconstruct log entries, those 102 reviews (plus 4 duplicates) were **relabeled with new, logged calls** (`requeue --missing-call-logs`), and the export now refuses to run if any saved label points to an unlogged call. None of these reviews were in the "before" checkpoint.

**Item-level call outcomes in `grading/calls.jsonl.gz`.** When a request returned successfully but some reviews' items were rejected and later relabeled (retry or fallback), the grading view lists those reviews under a companion `<request_id>:rejected-items` entry with `outcome: failed` and zero usage; usage stays on the parent request. Raw transport-level outcomes remain in [`runs/full/calls.jsonl`](runs/full/calls.jsonl).

**Memo iterations.** `memo_v2` added trend claims. Review of its output found colliding area claim IDs (`severity_sum` and `sev4plus` both became `A2SEV`) and an unsupported support recommendation. `memo_v3` fixed the IDs, added a check that each number matches the claim ID it cites, and required policy-vs-defect framing, an early-vs-late trend comparison and evidence for any sub-area recommended. The final memo passed the code check.

## Limitations

- **Self-selected, historical reviews.** Google Play reviews from May 2022 to Nov 2023 are not a representative customer sample. There is no revenue, plan tier or observed churn; cancellation language is stated intent. No revenue-at-risk or causal retention claims are made.
- **The area label of the top issue depends on the shared definitions.** Free-tier limits are filed under billing because the course definitions put premium-only controls there. A reviewer who files them under usability would see usability as the largest area. The top issue and the recommended work (free-tier policy and messaging) are the same either way.
- **Label quality.** A small model labels every review. On the human golden set, topic agreement is 0.80, with errors concentrated on the usability/billing/catalog/other boundaries; the verifier shows the same pattern. 50 cases is a small diagnostic sample. `needs_review` catches only half of the human-ambiguous cases.
- **Taxonomy gap.** No subtopic exists for billing requests such as payment methods or plan options. These caused most topic/subtopic mismatches and were resolved by the fallback model.
- **Generic negativity.** The #2 issue, `other.general_negative` (82,595 complaints), is not actionable by area; it is reported, not redistributed.
- **Volume spikes.** October 2023 contains about 3x normal review volume. Trend shares use completed-review denominators but cannot separate genuine experience changes from review campaigns.
- **Costs.** Jev prices are third-party figures, not confirmed on TypeSafe's site. 2 of 16,185 full-run attempts have unknown usage and are counted at worst case in the ledger, but left unknown in reports.
- **Non-English text** is labeled when understood. Two golden cases show human/model divergence on such text.

## Repository layout

```
run.py                 orchestrator CLI (run / rank / export / checkpoint / requeue / status)
pipeline/              ingest, enrich (+Jev, compact, fallback), verify, group, rank, memo, export, llm gateway, state, taxonomy
prompts/               enrich_v1/v2, jev_q1/q2 + jev_verify_q1 (generated), verify_v1, group_v1, memo_v1-v3
docs/                  ARCHITECTURE.md, LABELS.md, TRACE.md
cost/                  calculator.py, report.md, rates.csv, usage.csv, assumptions.json, pilot_*.jsonl, history/
evals/                 golden/ (human labels), golden_eval.py, planted_errors.py, injection_test.py, trace_review.py, results/, synthetic/
runs/full/             final run: enriched.jsonl.gz, calls.jsonl, verify/, group/, rank/, memo/, logs, summary
runs/<dev runs>/       pilot100, dev500(_v2), dev10k(_v2), controls_demo, synthetic_injection(_v2), earlier Jev pilots
grading/               standardized export (contract format) checked by check_submission.py
evidence/              interruption/resume screen recording
data/                  (not tracked) put the course dataset here
```
