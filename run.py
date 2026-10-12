"""Orchestrator: runs the six stages on any input CSV without manual chat pasting.

  python run.py run --input data/cost_100.csv --run-dir runs/pilot100        # PAID unless --mock
  python run.py rank --run-dir runs/pilot100                                  # offline, no model calls
  python run.py export --run-dir runs/full --out grading                      # standardized grading/ export
  python run.py checkpoint --run-dir runs/full --name before                  # resume-evidence snapshot
  python run.py status --run-dir runs/full

Code decides what runs next (stage order, cache hits, retries, budget stops); models only
answer bounded language questions inside the enrich/verify/group/memo roles.
"""
import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from pipeline import config as config_mod
from pipeline.common import ROOT, append_jsonl, file_sha, now_iso, read_json, read_jsonl, write_json
from pipeline.state import State

STAGES = ("ingest", "enrich", "verify", "group", "rank", "memo")


class RunLog:
    def __init__(self, run_dir, run_id):
        self.path = Path(run_dir) / "run_log.jsonl"
        self.run_id = run_id

    def __call__(self, stage, event, **fields):
        row = {"ts": now_iso(), "run_id": self.run_id, "stage": stage, "event": event, **fields}
        append_jsonl(self.path, row)
        extras = " ".join(f"{k}={v}" for k, v in fields.items() if k not in ("counts", "agreement"))
        print(f"[{row['ts']}] {stage:7s} {event} {extras}"[:400], flush=True)


def code_version():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or "uncommitted"
    except OSError:
        return "unknown"


def prior_spend(run_dir):
    p = Path(run_dir) / "calls.jsonl"
    if not p.exists():
        return 0.0, 0
    calls = read_jsonl(p)
    return sum(c["cost_usd"] or 0 for c in calls), sum(1 for c in calls if c.get("cost_usd") is None and not c.get("injected_fault"))


def parse_faults(s):
    out = {}
    for part in (s or "").split(","):
        if part.strip():
            k, v = part.split("=")
            out[k.strip()] = int(v)
    return out


def cmd_run(a):
    from pipeline.enrich import make_enricher
    from pipeline.export import export_grading, snapshot, write_enriched
    from pipeline.group import run_group
    from pipeline.ingest import ingest
    from pipeline.llm import Gateway, Ledger, load_rates
    from pipeline.memo import run_memo
    from pipeline.rank import rank
    from pipeline.verify import run_verify

    run_dir = Path(a.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    overrides = {"limits.workers": a.workers, "limits.spend_cap_usd": a.spend_cap}
    if a.mock:
        for role in ("enrich", "fallback", "verify", "group", "memo"):
            overrides[f"{role}.model"] = "mock-keyword-v0"
    cfg = config_mod.build(overrides, enricher=a.enricher)
    saved_cfg = run_dir / "config.json"
    if saved_cfg.exists() and not a.allow_config_change:
        old = read_json(saved_cfg)
        if config_mod.label_config(old) != config_mod.label_config(cfg):
            sys.exit("Label settings differ from this run dir's saved config; use a new --run-dir (or --allow-config-change).")
    write_json(saved_cfg, cfg)

    state = State(run_dir / "state.sqlite")
    sessions = state.get_meta("sessions", [])
    phase = a.phase or ("resume" if any(s.get("enrich_calls_made") for s in sessions) else "initial")
    run_id = state.get_meta("run_id") or f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    state.set_meta("run_id", run_id)
    log = RunLog(run_dir, run_id)
    spent, unknown = prior_spend(run_dir)
    ledger = Ledger(cfg["limits"]["spend_cap_usd"], spent)
    rates = load_rates()
    gw = Gateway(run_dir, ledger, rates, phase=phase, mock=a.mock, faults=parse_faults(a.inject_faults),
                 max_attempts=cfg["limits"]["max_attempts"])
    calls_before = len(read_jsonl(run_dir / "calls.jsonl")) if (run_dir / "calls.jsonl").exists() else 0
    session = {"started_at": now_iso(), "phase": phase, "mock": a.mock, "workers": a.workers, "spend_cap_usd": a.spend_cap,
               "prior_spend_usd": round(spent, 6), "prior_calls_with_unknown_cost": unknown, "input": a.input,
               "code_version": code_version(), "stage_seconds": {}}
    if (run_dir / "STOP").exists():
        (run_dir / "STOP").unlink()
        log("run", "cleared STOP file left by the previous (interrupted) session")
    log("run", "session start", phase=phase, mock=a.mock, workers=a.workers, cap=a.spend_cap, prior_spend=round(spent, 4))
    wall0 = time.perf_counter()
    stages = a.stages.split(",") if a.stages != "all" else list(STAGES)

    def timed(name, fn):
        t = time.perf_counter()
        out = fn()
        session["stage_seconds"][name] = round(time.perf_counter() - t, 3)
        return out

    summary = timed("ingest", lambda: ingest(state, a.input, run_dir, log))
    session["completed_at_start"] = state.counts().get("completed", 0)
    enrich_stats = None
    if "enrich" in stages:
        enricher = make_enricher(state, gw, cfg, log, run_dir)
        enrich_stats = timed("enrich", lambda: enricher.run(max_batches=a.max_batches, stop_file=run_dir / "STOP"))
        if not enrich_stats["stopped_reason"]:
            timed("fallback", enricher.run_fallback)
    counts = state.counts()
    session["counts_after_enrich"] = counts
    calls_now = read_jsonl(run_dir / "calls.jsonl") if (run_dir / "calls.jsonl").exists() else []
    session["enrich_calls_made"] = sum(1 for c in calls_now[calls_before:] if c["role"] == "enrich")
    timed("write_enriched", lambda: write_enriched(state, run_dir))
    finished = counts.get("pending", 0) == 0
    verify_report = None
    if finished:
        if "verify" in stages:
            verify_report = timed("verify", lambda: run_verify(state, gw, cfg, log, run_dir))
        if "group" in stages:
            timed("group", lambda: run_group(state, gw, cfg, log, run_dir))
        if "rank" in stages:
            timed("rank", lambda: rank(run_dir / "enriched.jsonl.gz", run_dir / "group" / "membership.csv", run_dir / "rank"))
            from pipeline.rank import monthly_trend
            months = dict(state.db.execute("SELECT review_id, substr(review_timestamp, 1, 7) FROM source"))
            monthly_trend(run_dir / "enriched.jsonl.gz", months, run_dir / "rank")
        if "memo" in stages:
            coverage = {"source_rows": summary["records"], "completed_records": counts.get("completed", 0),
                        "quarantined_records": counts.get("quarantined", 0), "pending_records": counts.get("pending", 0),
                        "empty_text_quarantines": summary["empty_review_text_quarantined"]}
            timed("memo", lambda: run_memo(state, gw, cfg, log, run_dir, coverage, verify_report))
    else:
        log("run", "enrichment incomplete; downstream stages skipped until all rows are completed or quarantined",
            pending=counts.get("pending", 0), stopped_reason=(enrich_stats or {}).get("stopped_reason"))
    if a.snapshot:
        n = snapshot(state, run_dir, a.snapshot)
        log("run", "checkpoint saved", name=a.snapshot, completed=n)
    session["wall_clock_seconds"] = round(time.perf_counter() - wall0, 3)
    session["ended_at"] = now_iso()
    session["enrich_stats"] = enrich_stats
    session["spend_usd_after"] = round(ledger.spent, 6)
    session["calls_this_session"] = len(read_jsonl(run_dir / "calls.jsonl")) - calls_before if (run_dir / "calls.jsonl").exists() else 0
    sessions.append(session)
    state.set_meta("sessions", sessions)
    write_summary(state, run_dir, cfg, sessions)
    log("run", "session end", wall_s=session["wall_clock_seconds"], spend=session["spend_usd_after"],
        calls=session["calls_this_session"], counts=state.counts())


def write_summary(state, run_dir, cfg, sessions):
    calls = read_jsonl(Path(run_dir) / "calls.jsonl") if (Path(run_dir) / "calls.jsonl").exists() else []
    usage = defaultdict(lambda: defaultdict(float))
    for c in calls:
        u = usage[f"{c['role']}|{c['model']}"]
        u["attempts"] += 1
        u["succeeded"] += c["outcome"] == "succeeded"
        u["failed"] += c["outcome"] == "failed"
        for k in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"):
            u[k] += c.get(k) or 0
        u["cost_usd"] += c.get("cost_usd") or 0
        u["unknown_cost_attempts"] += c.get("cost_usd") is None
    write_json(Path(run_dir) / "run_summary.json", {
        "run_id": state.get_meta("run_id"), "input_sha256": state.get_meta("input_sha256"),
        "label_config": config_mod.label_config(cfg), "config": cfg, "counts": state.counts(),
        "usage_by_role_model": {k: {kk: round(vv, 6) for kk, vv in v.items()} for k, v in usage.items()},
        "total_cost_usd": round(sum(c.get("cost_usd") or 0 for c in calls), 6),
        "cost_note": "API cost recomputed from logged provider usage x cost/rates.csv; attempts with unknown usage are listed separately.",
        "sessions": sessions, "updated_at": now_iso()})


def cmd_rank(a):
    from pipeline.rank import rank
    run_dir = Path(a.run_dir)
    ranking = rank(run_dir / "enriched.jsonl.gz", run_dir / "group" / "membership.csv", run_dir / "rank")
    print(f"Re-ranked {len(ranking)} issues from saved records + membership (no model calls). Top 5:")
    for r in ranking[:5]:
        print(f"  {r['rank']:>2}. {r['issue_id']:40s} count={r['complaint_count']:>7} sum={r['priority_score']:>8} mean={r['mean_severity']}")


def cmd_export(a):
    from pipeline.export import export_grading
    run_dir = Path(a.run_dir)
    state = State(run_dir / "state.sqlite")
    summary = state.get_meta("ingestion_summary")
    meta = {"version": "a5-audit-v1", "analysis_count": summary["records"], "analysis_sha256": summary["input_sha256"],
            "classification_input_fields": ["review_text"], "allow_multi_issue": False,
            "run_id": state.get_meta("run_id"), "label_config": config_mod.label_config(read_json(run_dir / "config.json")),
            "code_version": code_version(), "exported_at": now_iso(), "source_run_dir": str(run_dir),
            "counts": state.counts()}
    out = export_grading(state, run_dir, a.out, meta)
    print("Exported grading folder to", out)


def cmd_checkpoint(a):
    from pipeline.export import snapshot
    state = State(Path(a.run_dir) / "state.sqlite")
    n = snapshot(state, a.run_dir, a.name)
    print(f"Saved checkpoint '{a.name}' with {n} completed IDs; counts={state.counts()}")


def cmd_requeue(a):
    """Return quarantined rows with the given reasons (and their duplicate rows) to 'pending' for another pass.
    Completed rows are never touched, so resume still never re-sends completed IDs."""
    state = State(Path(a.run_dir) / "state.sqlite")
    log = RunLog(a.run_dir, state.get_meta("run_id"))
    total = 0
    if a.missing_call_logs:
        # Completed rows whose enrich request has no line in calls.jsonl (log line lost): relabel with logged calls.
        logged = {c["request_id"] for c in read_jsonl(Path(a.run_dir) / "calls.jsonl")}
        reqs = [r[0] for r in state.db.execute("SELECT DISTINCT enrich_request_id FROM records WHERE enrich_request_id IS NOT NULL")]
        lost = [r for r in reqs if r not in logged]
        ids = [r[0] for q in lost for r in state.db.execute("SELECT review_id FROM records WHERE enrich_request_id=?", (q,))]
        dups = [r[0] for i in ids for r in state.db.execute("SELECT review_id FROM records WHERE cache_source_id=?", (i,))]
        state.db.executemany("UPDATE records SET status='pending', reason=NULL, enrich_request_id=NULL, cache_source_id=NULL "
                             "WHERE review_id=?", [(i,) for i in ids + dups])
        log("requeue", "relabel rows whose enrich call log line is missing", lost_request_ids=lost,
            originals=len(ids), duplicates=len(dups))
        total += len(ids) + len(dups)
        a.reasons = ""
    for reason in filter(None, a.reasons.split(",")):
        ids = [r[0] for r in state.db.execute("SELECT review_id FROM records WHERE status='quarantined' AND reason=?", (reason,))]
        dups = [r[0] for i in ids for r in state.db.execute("SELECT review_id FROM records WHERE reason=?", ("duplicate_of_quarantined:" + i,))]
        state.db.executemany("UPDATE records SET status='pending', reason=NULL WHERE review_id=?", [(i,) for i in ids + dups])
        log("requeue", "returned quarantined rows to pending", reason=reason, originals=len(ids), duplicates=len(dups))
        total += len(ids) + len(dups)
    print(f"Requeued {total} rows; counts={state.counts()}")


def cmd_status(a):
    state = State(Path(a.run_dir) / "state.sqlite")
    spent, unknown = prior_spend(a.run_dir)
    print(json.dumps({"counts": state.counts(), "spend_usd": round(spent, 4), "attempts_with_unknown_cost": unknown,
                      "sessions": len(state.get_meta("sessions", []))}, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Run the pipeline (makes PAID API calls unless --mock)")
    r.add_argument("--input", required=True)
    r.add_argument("--run-dir", required=True)
    r.add_argument("--enricher", choices=["luna2", "jev", "luna"], default="luna2", help="classifier for the enrich role")
    r.add_argument("--workers", type=int, default=config_mod.DEFAULTS["limits"]["workers"])
    r.add_argument("--spend-cap", type=float, default=config_mod.DEFAULTS["limits"]["spend_cap_usd"])
    r.add_argument("--phase", choices=["initial", "resume"], help="default: auto (resume if earlier sessions made enrich calls)")
    r.add_argument("--max-batches", type=int, help="stop dispatching after N enrichment batches (interruption demo)")
    r.add_argument("--stages", default="all", help=f"comma list from {STAGES}; ingest always runs")
    r.add_argument("--snapshot", help="save a completed-ID checkpoint with this name at the end (e.g. before/after)")
    r.add_argument("--inject-faults", help="test only, e.g. rate_limit=1,malformed=1 (logged as injected)")
    r.add_argument("--mock", action="store_true", help="offline keyword mock; no API calls, not evidence")
    r.add_argument("--allow-config-change", action="store_true")
    r.set_defaults(fn=cmd_run)
    for name, fn in (("rank", cmd_rank), ("status", cmd_status)):
        s = sub.add_parser(name)
        s.add_argument("--run-dir", required=True)
        s.set_defaults(fn=fn)
    e = sub.add_parser("export")
    e.add_argument("--run-dir", required=True)
    e.add_argument("--out", default="grading")
    e.set_defaults(fn=cmd_export)
    q = sub.add_parser("requeue", help="return quarantined rows with given reasons to pending (no model calls)")
    q.add_argument("--run-dir", required=True)
    q.add_argument("--reasons", default="model_output_error")
    q.add_argument("--missing-call-logs", action="store_true", help="requeue completed rows whose enrich call is not in calls.jsonl")
    q.set_defaults(fn=cmd_requeue)
    c = sub.add_parser("checkpoint")
    c.add_argument("--run-dir", required=True)
    c.add_argument("--name", required=True)
    c.set_defaults(fn=cmd_checkpoint)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
