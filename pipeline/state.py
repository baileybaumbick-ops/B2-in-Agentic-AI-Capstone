"""Run state in SQLite: one row per source review, written in a transaction after every batch.

SQLite gives atomic upserts, so a crash or Ctrl+C can never leave a half-written batch,
and only the main thread writes, so workers cannot overwrite or double-count results.
"""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
  row_idx INTEGER PRIMARY KEY, review_id TEXT UNIQUE NOT NULL, row_sha TEXT NOT NULL, text_sha TEXT NOT NULL,
  review_text TEXT NOT NULL, review_rating TEXT, review_likes TEXT, app_version TEXT, review_timestamp TEXT,
  is_empty INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS source_text ON source(text_sha);
CREATE TABLE IF NOT EXISTS records (
  review_id TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  topic TEXT, subtopic TEXT, intent TEXT, severity INTEGER, sentiment REAL, entities TEXT, evidence_quote TEXT,
  needs_review INTEGER, review_note TEXT, label_config TEXT, cache_source_id TEXT, enrich_request_id TEXT,
  run_phase TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS records_status ON records(status);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

LABEL_COLS = ("topic", "subtopic", "intent", "severity", "sentiment", "entities", "evidence_quote",
              "needs_review", "review_note", "label_config", "cache_source_id", "enrich_request_id", "run_phase")


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)  # explicit transactions
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def counts(self):
        return dict(self.db.execute("SELECT status, COUNT(*) FROM records GROUP BY status").fetchall())

    def completed_ids(self):
        return [r[0] for r in self.db.execute("SELECT review_id FROM records WHERE status='completed' ORDER BY review_id")]

    def save_batch(self, completed, quarantined):
        """completed: list of dicts (review_id + LABEL_COLS + attempts); quarantined: list of (id, reason, attempts)."""
        from .common import now_iso
        ts = now_iso()
        cur = self.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            for r in completed:
                cur.execute(
                    f"UPDATE records SET status='completed', reason=NULL, attempts=?, updated_at=?, "
                    + ", ".join(f"{c}=?" for c in LABEL_COLS) + " WHERE review_id=? AND status!='completed'",
                    [r["attempts"], ts] + [_db(r.get(c)) for c in LABEL_COLS] + [r["review_id"]])
            for rid, reason, attempts in quarantined:
                cur.execute("UPDATE records SET status='quarantined', reason=?, attempts=?, updated_at=? "
                            "WHERE review_id=? AND status!='completed'", (reason, attempts, ts, rid))
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise


def _db(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v
