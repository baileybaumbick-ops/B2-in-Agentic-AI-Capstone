"""100-review cost and runtime calculator (COST_CALCULATOR.md).

DEFAULT = OFFLINE REPLAY (no API key, no model calls):
    python cost/calculator.py
    python cost/calculator.py --rates cost/rates.csv --assumptions cost/assumptions.json

EXPLICIT PAID PILOT (cold run with empty cache, then warm rerun of the same run dir, 1 worker):
    python cost/calculator.py run-pilot --yes-spend

Importing this module never makes a paid call. All money = billed units x price per unit,
with mutually exclusive input / cached-input / cache-write / output items; reasoning tokens are
already inside output tokens and are not added again.
"""
import argparse
import csv
import gzip
import json
import math
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PILOT_INPUT = ROOT / "data" / "cost_100.csv"
PILOT_SHA = "c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e"
ITEMS = ("input_uncached", "input_cached", "cache_write", "output")


# ---------------- arithmetic ----------------

def load_rates(path):
    rates = defaultdict(dict)
    meta = []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rates[(r["model"], r["tier"])][r["item"]] = float(r["price_usd"]) / float(r["per_units"])
            meta.append(r)
    return rates, meta


def billed_units(call):
    """Split provider usage into mutually exclusive billing items."""
    cached = call.get("cached_tokens") or 0
    writes = call.get("cache_write_tokens") or 0
    return {"input_uncached": (call.get("input_tokens") or 0) - cached - writes, "input_cached": cached,
            "cache_write": writes, "output": call.get("output_tokens") or 0}


def call_tier(call):
    """Bill at the processing tier the provider reported for this attempt."""
    return "flex" if call.get("service_tier") == "flex" else "standard"


def call_cost(call, rates, tier=None):
    if not call.get("usage_available", True):
        # 429 rejections are refused before processing and not billed; other missing usage stays unknown
        return 0.0 if "not billed" in (call.get("cost_note") or "") else None
    r = rates.get((call["model"], tier or call_tier(call)))
    if r is None:
        return None
    units = billed_units(call)
    return sum(units[i] * r.get(i, r["input_uncached"] if i == "cache_write" else 0) for i in ITEMS)


# ---------------- report ----------------

def load_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def summarize_calls(calls, rates):
    by_stage = defaultdict(lambda: defaultdict(float))
    unknown = []
    for c in calls:
        s = by_stage[(c["role"], c["model"], c.get("effort"))]
        s["attempts"] += 1
        s["requests"] += c.get("attempt", 1) == 1
        s["retries"] += c.get("attempt", 1) > 1
        s["failed"] += c["outcome"] == "failed"
        s["succeeded"] += c["outcome"] == "succeeded"
        s["reviews_sent"] += len(c.get("review_ids") or [])
        for k, v in billed_units(c).items():
            s[k] += v
        s["reasoning_tokens"] += c.get("reasoning_tokens") or 0
        s["latency_s_sum"] += c.get("latency_s") or 0
        cost = call_cost(c, rates)
        if cost is None:
            unknown.append(c["request_id"])
        else:
            s["cost_usd"] += cost
    return by_stage, unknown


def fmt(x, nd=4):
    return f"{x:,.{nd}f}" if isinstance(x, float) else f"{x:,}" if isinstance(x, int) else str(x)


def project(assume, measured, rates):
    """Full-run scenarios from per-unit measured costs; one-time stages added once."""
    full = assume["full_run"]
    distinct = full["distinct_nonempty_texts"]
    nonempty = full["nonempty_rows"]
    enrich = measured["enrich"]
    per_text = enrich["cost"] / enrich["texts_sent"] if enrich["texts_sent"] else 0
    per_text_time = enrich["seconds"] / enrich["texts_sent"] if enrich["texts_sent"] else 0
    verify_n = min(assume["verify"]["max_sample"], max(assume["verify"]["min_sample"], round(distinct * assume["verify"]["sample_fraction"])))
    per_verify = measured["verify"]["cost"] / measured["verify"]["n"] if measured["verify"]["n"] else 0
    fixed = measured["group"]["cost"] * assume["fixed"]["group_cost_multiplier"] + measured["memo"]["cost"]
    fb_rate = rates.get((assume["fallback"]["model"], "standard"), {})
    fb_per = (assume["fallback"]["input_tokens_per_review"] * fb_rate.get("input_uncached", 0)
              + assume["fallback"]["output_tokens_per_review"] * fb_rate.get("output", 0))
    out = {}
    for name, sc in assume["scenarios"].items():
        for reuse in (True, False):
            texts = distinct if reuse else nonempty
            enrich_cost = texts * per_text * (1 + sc["retry_rate"])
            fb_cost = texts * sc["fallback_fraction"] * fb_per
            verify_cost = verify_n * per_verify
            total = enrich_cost + fb_cost + verify_cost + fixed
            workers = sc["workers"]
            hours = (texts * per_text_time * (1 + sc["retry_rate"]) / workers / sc["parallel_efficiency"] + sc["fixed_overhead_s"]) / 3600
            out[f"{name}|{'reuse' if reuse else 'no_reuse'}"] = {
                "texts_classified": texts, "enrich_usd": enrich_cost, "fallback_usd": fb_cost, "verify_usd": verify_cost,
                "fixed_group_memo_usd": fixed, "total_api_usd": total, "hours": hours, "workers": workers,
                "over_budget": total > assume["controls"]["spending_limit_usd"]}
    return out, {"per_text_usd": per_text, "per_text_s": per_text_time, "verify_n": verify_n, "per_verify_usd": per_verify,
                 "fixed_usd": fixed, "fallback_per_review_usd": fb_per}


def report(args):
    rates, rate_meta = load_rates(args.rates)
    assume = json.loads(Path(args.assumptions).read_text(encoding="utf-8"))
    ev = Path(args.evidence_dir)
    calls = load_jsonl(ev / "pilot_calls.jsonl")
    records = load_jsonl(ev / "pilot_records.jsonl")
    sessions = json.loads((ev / "pilot_sessions.json").read_text(encoding="utf-8")) if (ev / "pilot_sessions.json").exists() else {}
    if not calls:
        sys.exit("No pilot evidence yet (cost/pilot_calls.jsonl). Run the explicit paid pilot first: "
                 "python cost/calculator.py run-pilot --yes-spend")
    cold_calls = [c for c in calls if c["pilot_run"] == "cold"]
    warm_calls = [c for c in calls if c["pilot_run"] == "warm"]
    by_stage, unknown = summarize_calls(cold_calls, rates)
    warm_stage, _ = summarize_calls(warm_calls, rates)

    # usage.csv: every charged item maps back to a request_id in pilot_calls.jsonl
    with open(ev / "usage.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["pilot_run", "request_id", "role", "model", "tier", "item", "billed_units", "price_per_unit_usd", "item_cost_usd"])
        for c in calls:
            tier = call_tier(c)
            r = rates.get((c["model"], tier), {})
            for item, units in billed_units(c).items():
                if units:
                    p = r.get(item, r.get("input_uncached", 0) if item == "cache_write" else 0)
                    w.writerow([c["pilot_run"], c["request_id"], c["role"], c["model"], tier, item, units, f"{p:.12f}", f"{units * p:.10f}"])

    cold = sessions.get("cold", {})
    warm = sessions.get("warm", {})
    status = defaultdict(int)
    for r in records:
        status[r["status"]] += 1
    total_cold = sum(s["cost_usd"] for s in by_stage.values())
    total_warm = sum(s["cost_usd"] for s in warm_stage.values())
    enrich_key = next((k for k in by_stage if k[0] == "enrich"), None)
    enrich = by_stage.get(enrich_key, {})
    texts_sent = len({rid for c in cold_calls if c["role"] == "enrich" and c["outcome"] == "succeeded" for rid in c["review_ids"]})
    stage_secs = cold.get("stage_seconds", {})
    measured = {"enrich": {"cost": enrich.get("cost_usd", 0.0), "texts_sent": texts_sent, "seconds": stage_secs.get("enrich", 0)},
                "verify": {"cost": sum(s["cost_usd"] for k, s in by_stage.items() if k[0] == "verify"),
                           "n": sum(s["reviews_sent"] for k, s in by_stage.items() if k[0] == "verify" and s["succeeded"])},
                "group": {"cost": sum(s["cost_usd"] for k, s in by_stage.items() if k[0] == "group")},
                "memo": {"cost": sum(s["cost_usd"] for k, s in by_stage.items() if k[0] == "memo")}}
    proj, unit = project(assume, measured, rates)
    ctrl = assume["controls"]

    L = ["# 100-review cost and runtime calculator - report", "",
         f"Generated offline from saved evidence. Rates file: `{Path(args.rates).name}`; assumptions: `{Path(args.assumptions).name}`. "
         "No API key is used and no model is called by this report.", "",
         "## Measured pilot (cost_100.csv, unchanged)", "",
         f"- Input checksum: `{cold.get('input_sha256', 'n/a')}` (expected `{PILOT_SHA}`; match: {cold.get('input_sha256') == PILOT_SHA})",
         f"- Pilot IDs: {len(records)} records - completed {status['completed']}, quarantined {status['quarantined']}, pending {status['pending']}",
         f"- Unique texts sent to the enricher: {texts_sent}; result-cache hits (cold): {cold.get('cache_hits', 0)}; "
         f"(warm): {warm.get('cache_hits', 0) + warm.get('completed_at_start', 0)} records served from saved results",
         f"- Workers: {cold.get('workers')}; enrichment batch size: {assume['measured_settings']['enrich_batch_size']}; verification sample: {int(measured['verify']['n'])} records",
         f"- **Cold run: wall-clock {fmt(cold.get('wall_clock_seconds', 0.0), 2)} s, API cost ${fmt(total_cold, 6)}, {len(cold_calls)} attempts**",
         f"- **Warm run (same run dir, saved results): wall-clock {fmt(warm.get('wall_clock_seconds', 0.0), 2)} s, "
         f"new enrichment calls {sum(1 for c in warm_calls if c['role'] == 'enrich')}, downstream calls {sum(1 for c in warm_calls if c['role'] != 'enrich')}, "
         f"incremental API cost ${fmt(total_warm, 6)}**",
         f"- Cost per 1,000 input rows: ${fmt(total_cold / len(records) * 1000 if records else 0.0, 4)}; per completed record: ${fmt(total_cold / status['completed'] if status['completed'] else 0.0, 6)}",
         f"- Throughput (cold, end to end): {fmt(len(records) / cold['wall_clock_seconds'] if cold.get('wall_clock_seconds') else 0.0, 2)} records/s",
         f"- Attempts with unknown usage (left unresolved, not zeroed): {len(unknown)}", "",
         "### By stage (cold run)", "",
         "| Stage | Provider | Model | Effort | Requests | Attempts | Retries | Failed | Reviews sent | Uncached in | Cached in | Output (incl. reasoning) | Reasoning | Cost USD | Stage s |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    provider = {r["model"]: r["provider"] for r in rate_meta}
    for (role, model, effort), s in by_stage.items():
        L.append(f"| {role} | {provider.get(model, 'unknown')} | {model} | {effort} | {int(s['requests'])} | {int(s['attempts'])} | {int(s['retries'])} | {int(s['failed'])} | "
                 f"{int(s['reviews_sent'])} | {int(s['input_uncached']):,} | {int(s['input_cached']):,} | {int(s['output']):,} | {int(s['reasoning_tokens']):,} | "
                 f"{s['cost_usd']:.6f} | {stage_secs.get(role, '')} |")
    L += ["", f"Prompt/schema versions: {assume['measured_settings']['versions']}. Summed call latency is not wall-clock time when calls overlap; "
          "wall-clock values above come from the orchestrator clock.", "",
          "### Rates used (editable `rates.csv`; each attempt is billed at the tier the provider reported)", "",
          "| Model | Tier | Item | USD per unit | Source | Checked |", "|---|---|---|---|---|---|"]
    for r in rate_meta:
        if r["tier"] in ("standard", "flex") and r["provider"] != "local":
            L.append(f"| {r['model']} | {r['tier']} | {r['item']} | {float(r['price_usd']) / float(r['per_units']):.10f} | {r['source_url']} | {r['checked_date']} |")
    L += ["", "Formula: item_cost = billed_units x price_per_unit; total = sum of items. Uncached input = input - cached - cache_write. "
          "Every row of `usage.csv` maps to a `request_id` in `pilot_calls.jsonl`.", "",
          "### Local compute (separate from API spend)", "", f"- {assume['local_compute']['note']}", "",
          "## Full-run projection", "",
          f"Scope: {assume['full_run']['rows']:,} rows; {assume['full_run']['nonempty_rows']:,} nonempty classifications; "
          f"{assume['full_run']['empty_quarantines']} empty-text quarantines. Exact-text reuse reduces first-pass work to "
          f"{assume['full_run']['distinct_nonempty_texts']:,} distinct texts.", "",
          f"Per-unit inputs from the measurement: enrichment ${unit['per_text_usd']:.8f} and {unit['per_text_s']:.4f} s per distinct text; "
          f"verification ${unit['per_verify_usd']:.8f} per record x {unit['verify_n']:,} records; group+memo fixed ${unit['fixed_usd']:.4f} (added once); "
          f"fallback ${unit['fallback_per_review_usd']:.6f} per routed review.", "",
          "| Scenario | Texts | Enrich $ | Fallback $ | Verify $ | Group+memo $ | **Total API $** | Workers | Hours | Over budget? |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for name, p in proj.items():
        flag = "**YES - exceeds limit**" if p["over_budget"] else "no"
        L.append(f"| {name} | {p['texts_classified']:,} | {p['enrich_usd']:.2f} | {p['fallback_usd']:.2f} | {p['verify_usd']:.2f} | "
                 f"{p['fixed_group_memo_usd']:.2f} | **{p['total_api_usd']:.2f}** | {p['workers']} | {p['hours']:.2f} | {flag} |")
    # Estimate refreshes from later, larger measurements (same arithmetic, saved call logs only).
    L += ["", "## Estimate refreshes (100 -> 500 -> 10,000) and the rejected alternative", "",
          "Enrichment cost per distinct text from each saved run's call log, projected to 484,189 distinct texts "
          "(enrichment stage only; verification/group/memo are small fixed or sampled costs shown above).", "",
          "| Measurement | Enricher | Distinct texts sent | Enrich attempts (failed) | Enrich API $ | $ per distinct text | Projected full enrich $ | Enrich wall s | Texts/s | Workers |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for name, label in assume.get("refresh_runs", []):
        rdir = ROOT / name
        rc = load_jsonl(rdir / "calls.jsonl")
        if not rc:
            continue
        ec = [c for c in rc if c["role"] == "enrich"]
        texts = len({rid for c in ec if c["outcome"] == "succeeded" for rid in c["review_ids"]})
        cost = sum(call_cost(c, rates) or 0 for c in ec)
        summ = json.loads((rdir / "run_summary.json").read_text(encoding="utf-8")) if (rdir / "run_summary.json").exists() else {}
        secs = sum((sx.get("stage_seconds") or {}).get("enrich", 0) for sx in summ.get("sessions", []))
        workers = max((sx.get("workers") or 0 for sx in summ.get("sessions", [])), default=0)
        per = cost / texts if texts else 0
        L.append(f"| `{name}` | {label} | {texts:,} | {len(ec)} ({sum(c['outcome'] == 'failed' for c in ec)}) | {cost:.4f} | "
                 f"{per:.8f} | {per * assume['full_run']['distinct_nonempty_texts']:.2f} | {secs:.0f} | "
                 f"{texts / secs if secs else 0:.1f} | {workers} |")
    L += ["", "Decision: the Jev enricher (one review per request, so the ~2,000-token question set is billed for every review) "
          "projected about $41 for full-run enrichment alone, over the $10 limit; the batched GPT-6 Luna enricher (50 reviews/request, "
          "provider prompt caching, Flex tier) projected about $5-6 and was chosen. Jev was kept as the independent verifier "
          "and the cross-provider fallback. Rates for Jev are third-party figures and are marked UNVERIFIED in rates.csv."]
    L += ["", "## Controls", "",
          f"- Spending limit: ${ctrl['spending_limit_usd']:.2f} (enforced in code by a shared ledger that reserves worst-case cost before each request)",
          f"- Output-token cap per enrichment request: {ctrl['output_token_cap']:,}",
          f"- Maximum workers: {ctrl['max_workers']}", f"- Maximum fallback fraction: {ctrl['max_fallback_fraction']}",
          f"- Scenario assumptions: {json.dumps(assume['scenarios'])}", "",
          "Edit `rates.csv` or `assumptions.json` and rerun `python cost/calculator.py` to recompute. Doubling all API rates doubles every API "
          "dollar figure while measured times and local-compute notes stay unchanged; changing projected volume never changes the measured pilot."]
    out = ev / ("report.md" if args.rates == str(HERE / "rates.csv") and args.assumptions == str(HERE / "assumptions.json") else "report_custom.md")
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:20]))
    print(f"\nWrote {out}")


# ---------------- explicit paid pilot ----------------

def run_pilot(args):
    if not args.yes_spend and not args.mock:
        sys.exit("Refusing to make paid calls without --yes-spend.")
    ev = Path(args.evidence_dir)
    if args.mock and ev.resolve() == HERE:
        sys.exit("Mock pilots must not write into cost/; pass --evidence-dir elsewhere.")
    run_dir = Path(args.run_dir)
    if run_dir.exists():
        sys.exit(f"{run_dir} already exists; the cold run needs an empty result cache. Move it aside first.")
    cmd = [sys.executable, str(ROOT / "run.py"), "run", "--input", str(PILOT_INPUT), "--run-dir", str(run_dir),
           "--workers", "1", "--spend-cap", str(args.spend_cap)] + (["--mock"] if args.mock else [])
    for label in ("cold", "warm"):
        print(f"== {label} run ==", flush=True)
        n_before = len(load_jsonl(run_dir / "calls.jsonl"))
        subprocess.run(cmd, check=True)
        calls = load_jsonl(run_dir / "calls.jsonl")[n_before:]
        summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
        sess = summary["sessions"][-1]
        info = {"run_id": summary["run_id"], "input_sha256": summary["input_sha256"], "wall_clock_seconds": sess["wall_clock_seconds"],
                "stage_seconds": sess["stage_seconds"], "workers": sess["workers"], "phase": sess["phase"],
                "cache_hits": (sess.get("enrich_stats") or {}).get("cache_hits", 0), "calls": len(calls),
                "completed_at_start": sess.get("completed_at_start", 0), "mock": sess.get("mock"),
                "started_at": sess["started_at"], "ended_at": sess["ended_at"]}
        sessions = json.loads((ev / "pilot_sessions.json").read_text(encoding="utf-8")) if (ev / "pilot_sessions.json").exists() else {}
        sessions[label] = info
        (ev / "pilot_sessions.json").write_text(json.dumps(sessions, indent=2), encoding="utf-8")
        mode = "w" if label == "cold" else "a"
        with open(ev / "pilot_calls.jsonl", mode, encoding="utf-8") as f:
            for c in calls:
                f.write(json.dumps({**c, "pilot_run": label, "run_id": summary["run_id"]}, ensure_ascii=False) + "\n")
    with gzip.open(run_dir / "enriched.jsonl.gz", "rt", encoding="utf-8") as src, open(ev / "pilot_records.jsonl", "w", encoding="utf-8") as dst:
        shutil.copyfileobj(src, dst)
    print(f"Pilot evidence saved under {ev}. Now run the offline report: python cost/calculator.py")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rates", default=str(HERE / "rates.csv"))
    p.add_argument("--assumptions", default=str(HERE / "assumptions.json"))
    p.add_argument("--evidence-dir", default=str(HERE))
    sub = p.add_subparsers(dest="cmd")
    rp = sub.add_parser("run-pilot", help="PAID: real cold+warm pilot on cost_100.csv")
    rp.add_argument("--yes-spend", action="store_true")
    rp.add_argument("--spend-cap", type=float, default=2.0)
    rp.add_argument("--run-dir", default=str(ROOT / "runs" / "pilot100"))
    rp.add_argument("--mock", action="store_true", help="offline plumbing test only; never evidence")
    a = p.parse_args()
    if a.cmd == "run-pilot":
        run_pilot(a)
    else:
        report(a)


if __name__ == "__main__":
    main()
