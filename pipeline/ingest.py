"""Stage 1 - Prepare (code only, no model).

Reads every row with a multiline-safe CSV parser, preserves IDs and source values,
hashes rows per the grading contract, quarantines empty texts, profiles quality,
finds exact duplicate texts, and queues pending work.
"""
import importlib.util
from collections import Counter
from pathlib import Path

from .common import ROOT, Timer, file_sha, now_iso, read_csv, row_sha, text_sha, write_json

# The course's zero-API checker is not redistributed in this repo; it ships in the course dataset ZIP.
_CHECKER = next((p for p in (ROOT / "tools" / "check_submission.py", ROOT / "data" / "check_submission.py") if p.exists()), None)
if _CHECKER is None:
    raise SystemExit("check_submission.py not found: unzip the course dataset into data/ (see README 'Setup').")
_spec = importlib.util.spec_from_file_location("check_submission", _CHECKER)
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


def ingest(state, input_csv, run_dir, log):
    input_csv = Path(input_csv)
    sha = file_sha(input_csv)
    existing = state.get_meta("input_sha256")
    if existing:
        if existing != sha:
            raise SystemExit(f"Run dir was built from a different input ({existing[:12]}...); use a new --run-dir.")
        log("ingest", "skipped: state already holds this input", input_sha256=sha)
        return state.get_meta("ingestion_summary")

    with Timer() as t:
        ids, months, ratings, texts = Counter(), Counter(), Counter(), Counter()
        empty = missing_version = rows = 0
        first = last = None
        cur = state.db.cursor()
        cur.execute("BEGIN")
        for idx, row in enumerate(read_csv(input_csv)):
            rows += 1
            rid, text = row["review_id"], row["review_text"]
            ids[rid] += 1
            is_empty = not text.strip()
            empty += is_empty
            missing_version += not row["app_version"].strip()
            months[row["review_timestamp"][:7]] += 1
            ratings[row["review_rating"]] += 1
            ts = row["review_timestamp"]
            first = min(first, ts) if first else ts
            last = max(last, ts) if last else ts
            tsha = text_sha(text)
            if not is_empty:
                texts[tsha] += 1
            cur.execute("INSERT INTO source VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (idx, rid, row_sha(row), tsha, text, row["review_rating"], row["review_likes"],
                         row["app_version"], ts, int(is_empty)))
            if is_empty:
                cur.execute("INSERT INTO records (review_id,status,reason,attempts,updated_at) VALUES (?,?,?,0,?)",
                            (rid, "quarantined", "empty_review_text", now_iso()))
            else:
                cur.execute("INSERT INTO records (review_id,status,attempts) VALUES (?,?,0)", (rid, "pending"))
        cur.execute("COMMIT")

    dup_groups = [c for c in texts.values() if c > 1]
    summary = {
        "input_path": str(input_csv), "input_sha256": sha, "input_bytes": input_csv.stat().st_size,
        "records": rows, "duplicate_review_ids": sum(c - 1 for c in ids.values() if c > 1),
        "empty_review_text_quarantined": empty, "nonempty_to_classify": rows - empty,
        "missing_app_version": missing_version,
        "distinct_nonempty_texts": len(texts), "exact_duplicate_text_groups": len(dup_groups),
        "rows_reusing_exact_text": sum(dup_groups) - len(dup_groups),
        "first_review": first, "last_review": last,
        "reviews_by_month": dict(sorted(months.items())), "reviews_by_rating": dict(sorted(ratings.items())),
        "ingest_seconds": t.seconds, "profiled_at": now_iso(),
        "note": "No rows removed. Empty texts are quarantined with reason empty_review_text; "
                "missing app_version does not block classification.",
    }
    top_dups = state.db.execute(
        "SELECT review_text, COUNT(*) c FROM source WHERE is_empty=0 GROUP BY text_sha HAVING c>1 ORDER BY c DESC LIMIT 15"
    ).fetchall()
    summary["top_duplicate_texts"] = [{"text": t[:80], "rows": c} for t, c in top_dups]
    state.set_meta("input_sha256", sha)
    state.set_meta("ingestion_summary", summary)
    write_json(Path(run_dir) / "ingestion_report.json", summary)
    write_json(Path(run_dir) / "ingestion.json", checker.profile(input_csv))  # exact grading profile
    log("ingest", "done", records=rows, empty=empty, distinct_texts=len(texts), seconds=t.seconds)
    return summary
