# Spotify review pipeline: a multi-agent recommendation from 660,622 reviews

**Question:** at the end of the review window (May 2022 to Nov 2023), where should Spotify's next quarter of product effort go: access, usability, playback, or billing/support?

**Answer (full-corpus run):** _RESULT_PENDING_. See the [decision memo](runs/full/memo/memo.md).

This repository is the single deliverable. Everything below links to saved evidence, so a reviewer can inspect the results **without an API key and without paid calls**, and can rerun the deterministic ranking and the cost calculator offline.

---

## Results summary (final full-corpus run `runs/full`)

_RESULTS_TABLE_PENDING_

---

## Rubric map: where the evidence is

| Rubric item | Evidence |
|---|---|
| **D1** Accessible code, setup, artifacts | [Setup](#setup) and [Commands](#commands) below; [`requirements.txt`](requirements.txt); [`.env.example`](.env.example) (names only); all outputs under [`runs/full/`](runs/full), [`grading/`](grading), [`cost/`](cost), [`evals/`](evals) |
| **D2** Architecture, shared schema, provenance | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (diagram, roles, code/model boundary, stop/retry paths); [`docs/LABELS.md`](docs/LABELS.md) (topics, subtopics, intent, severity, examples); prompts in [`prompts/`](prompts); `label_config` on every record and call; row SHA-256 per contract; `cache_source_id` provenance; [`runs/full/run_summary.json`](runs/full/run_summary.json) |
| **D3** Memo numbers traced to calculations and sources | [`runs/full/memo/memo.md`](runs/full/memo/memo.md) cites claim IDs; [`grading/claims.csv`](grading/claims.csv); code claim check in [`pipeline/memo.py`](pipeline/memo.py); [`docs/TRACE.md`](docs/TRACE.md) traces one real review from source row to memo claim |
| **D4** Recommendation, alternatives, limitations | memo sections 1-4; [Limitations](#limitations) |
| **T1** 50 human labels, per-field comparison, error analysis | [`evals/golden/`](evals/golden) (human labels), [`evals/results/golden_eval.json`](evals/results/golden_eval.json), [`evals/results/golden_per_case.csv`](evals/results/golden_per_case.csv), [Golden-set results](#golden-set-evaluation) |
| **T2** Independent verification, planted-error and injection tests | [`runs/full/verify/`](runs/full/verify) (Jev re-labels a seeded sample; [`disagreements.csv`](runs/full/verify/disagreements.csv)); [`evals/results/planted_errors/`](evals/results/planted_errors); [`evals/results/injection_results.json`](evals/results/injection_results.json) |
| **T3** Real 100-review cold/warm pilot, offline calculator, retry/spend/recovery controls | [`cost/report.md`](cost/report.md), [`cost/calculator.py`](cost/calculator.py), [`cost/pilot_calls.jsonl`](cost/pilot_calls.jsonl), [`cost/pilot_records.jsonl`](cost/pilot_records.jsonl), [`cost/rates.csv`](cost/rates.csv), [`cost/usage.csv`](cost/usage.csv); controls demo [`runs/controls_demo/`](runs/controls_demo) and [System checks](#system-checks) |
| **W1** Full ingestion, coverage, classification | [`grading/ingestion.json`](grading/ingestion.json), [`runs/full/ingestion_report.json`](runs/full/ingestion_report.json), [`grading/records.jsonl.gz`](grading/records.jsonl.gz), [`runs/full/quarantine.jsonl`](runs/full/quarantine.jsonl), checker output in [Self-check](#zero-api-self-check) |
| **W2** Runnable staged program, bounded calls, saved handoffs, resume | [`run.py`](run.py); per-stage outputs under [`runs/full/`](runs/full); [`grading/calls.jsonl.gz`](grading/calls.jsonl.gz) (every attempt, at most 50 reviews per enrichment request); [`grading/checkpoint_before.json`](grading/checkpoint_before.json), [`grading/checkpoint_after.json`](grading/checkpoint_after.json); recording [`evidence/interruption_resume_recording.mp4`](evidence/interruption_resume_recording.mp4) |
| **W3** Reproducible baseline ranking, grounded output | `python run.py rank --run-dir runs/full` regenerates [`runs/full/rank/ranking.csv`](runs/full/rank/ranking.csv) from saved records and membership with no model calls; [`grading/ranking.csv`](grading/ranking.csv) |

---

## Setup

Python 3.12 (3.10+ works). No API key is needed to inspect the outputs, replay the calculator, or rerun the ranking.

```bash
pip install -r requirements.txt
```

The course dataset and the instructor's materials are **not redistributed** here. Unzip the course ZIP into `data/`; see [`data/WHERE_TO_PUT_THE_DATA.md`](data/WHERE_TO_PUT_THE_DATA.md). Raw data: `spotify_reviews_18months.csv`, 97,400,616 bytes, SHA-256 `1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6` (BwandoWando, *3.4 Million Spotify Google Store Reviews*, v2, Kaggle, CC0).

Paid runs only: set `OPENAI_API_KEY` and `TYPESAFE_API_KEY` in the environment (see `.env.example`). On Windows, `setx` values are also read from the user environment.

## Commands

| Purpose | Command | Paid? |
|---|---|---|
| Rerun the baseline ranking from saved outputs | `python run.py rank --run-dir runs/full` | no |
| Offline cost replay (default calculator mode) | `python cost/calculator.py` | no |
| Recompute with edited prices | edit `cost/rates.csv`, then `python cost/calculator.py` | no |
| Export the grading folder | `python run.py export --run-dir runs/full --out grading` | no |
| Zero-API self-check | see [Self-check](#zero-api-self-check) | no |
| Golden-set scoring | `python evals/golden_eval.py score --run-dir runs/full` | no |
| Planted-error test | `python evals/planted_errors.py --run-dir runs/full` | no |
| Trace one review end to end | `python evals/trace_review.py --run-dir runs/full` | no |
| Run status | `python run.py status --run-dir runs/full` | no |
| Full pipeline on any CSV | `python run.py run --input <csv> --run-dir runs/<name> --workers 24 --spend-cap 10` | **yes** |
| Offline plumbing test (keyword mock, not evidence) | add `--mock` to the run command | no |
| Explicit paid 100-review pilot (cold + warm) | `python cost/calculator.py run-pilot --yes-spend` | **yes** |
| Injection test | `python evals/injection_test.py --yes-spend` (re-check offline with `--check-only`) | **yes** (under $0.01) |

Interrupt a run at any time with Ctrl+C or by creating `runs/<name>/STOP`. Rerunning the same command resumes; completed IDs are never re-sent under unchanged settings.

---

_SECTIONS_PENDING_
