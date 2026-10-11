"""Saved handoffs and the standardized grading/ export (GRADING_CONTRACT.md)."""
import gzip
import json
import shutil
from pathlib import Path

from .common import read_json, read_jsonl, write_json

CONTRACT_KEYS = ("review_id", "source_sha256", "status", "topic", "intent", "sentiment", "severity", "entities",
                 "evidence_quote", "needs_review", "label_config")


def iter_records(state):
    cur = state.db.execute(
        """SELECT s.review_id, s.row_sha, r.status, r.reason, r.attempts, r.topic, r.subtopic, r.intent, r.sentiment,
                  r.severity, r.entities, r.evidence_quote, r.needs_review, r.review_note, r.label_config,
                  r.cache_source_id, r.enrich_request_id, r.run_phase
           FROM source s JOIN records r USING(review_id) ORDER BY s.row_idx""")
    for (rid, sha, status, reason, attempts, topic, sub, intent, sent, sev, ents, quote, nr, note, lc, csid, req,
         phase) in cur:
        if status == "completed":
            rec = {"review_id": rid, "source_sha256": sha, "status": status, "topic": topic, "subtopic": sub,
                   "intent": intent, "sentiment": sent, "severity": sev, "entities": json.loads(ents),
                   "evidence_quote": quote, "needs_review": bool(nr), "label_config": lc, "attempts": attempts,
                   "enrich_request_id": req, "run_phase": phase}
            if note:
                rec["review_note"] = note
            if csid:
                rec["cache_source_id"] = csid
        else:
            rec = {"review_id": rid, "source_sha256": sha, "status": status, "attempts": attempts}
            if reason:
                rec["reason"] = reason
        yield rec


def write_enriched(state, run_dir):
    run_dir = Path(run_dir)
    n = {"completed": 0, "quarantined": 0, "pending": 0}
    quarantine = []
    with gzip.open(run_dir / "enriched.jsonl.gz", "wt", encoding="utf-8") as f:
        for rec in iter_records(state):
            n[rec["status"]] = n.get(rec["status"], 0) + 1
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            if rec["status"] == "quarantined":
                quarantine.append(rec)
    with open(run_dir / "quarantine.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for q in quarantine:
            f.write(json.dumps(q, ensure_ascii=False, sort_keys=True) + "\n")
    return n


def snapshot(state, run_dir, name):
    ids = state.completed_ids()
    from .common import now_iso
    write_json(Path(run_dir) / "checkpoints" / f"{name}.json",
               {"completed_ids": ids, "completed_count": len(ids), "counts": state.counts(), "taken_at": now_iso()})
    return len(ids)


def export_grading(state, run_dir, out_dir, run_meta):
    run_dir, out_dir = Path(run_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "run.json", run_meta)
    shutil.copyfile(run_dir / "ingestion.json", out_dir / "ingestion.json")
    with gzip.open(out_dir / "records.jsonl.gz", "wt", encoding="utf-8") as f:
        for rec in iter_records(state):
            keep = {k: rec[k] for k in rec if k in CONTRACT_KEYS or k in ("reason", "cache_source_id", "subtopic")}
            f.write(json.dumps(keep, ensure_ascii=False, sort_keys=True) + "\n")
    for src, dst in (("group/membership.csv", "membership.csv"), ("rank/ranking.csv", "ranking.csv"),
                     ("memo/claims.csv", "claims.csv")):
        if (run_dir / src).exists():
            shutil.copyfile(run_dir / src, out_dir / dst)
    calls = read_jsonl(run_dir / "calls.jsonl") if (run_dir / "calls.jsonl").exists() else []
    keys = ("request_id", "role", "review_ids", "model", "phase", "outcome", "label_config", "input_tokens", "output_tokens",
            "cached_tokens", "reasoning_tokens", "cost_usd", "attempt", "error", "started_at", "latency_s", "artifacts",
            "injected_fault", "provider_response_id", "usage_available", "effort")
    with gzip.open(out_dir / "calls.jsonl.gz", "wt", encoding="utf-8") as f:
        for c in calls:
            f.write(json.dumps({k: c.get(k) for k in keys}, ensure_ascii=False, sort_keys=True) + "\n")
    for name in ("before", "after"):
        p = run_dir / "checkpoints" / f"{name}.json"
        if p.exists():
            snap = read_json(p)
            write_json(out_dir / f"checkpoint_{name}.json", {"completed_ids": snap["completed_ids"], "taken_at": snap["taken_at"]})
    return out_dir
