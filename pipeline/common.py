"""Shared helpers: hashing, CSV reading, atomic JSON/JSONL writes, timestamps.

Hashing matches data/check_submission.py exactly so grading hashes line up.
"""
import csv
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp")
TOPICS = ("access", "usability", "playback", "downloads", "catalog", "billing", "support", "other")
INTENTS = ("cancellation", "complaint", "request", "praise", "unclear")  # precedence order
ROOT = Path(__file__).resolve().parent.parent

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def row_sha(row):
    """SHA-256 of compact JSON of the six original field strings (grading contract)."""
    return hashlib.sha256(canonical([row[k] for k in FIELDS]).encode("utf-8")).hexdigest()


def text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_csv(path):
    """Yield rows with a parser that handles quoted multiline text. Values are untouched."""
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, strict=True)
        missing = [k for k in FIELDS if k not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing required columns {missing}")
        for row in reader:
            yield {k: row[k] for k in FIELDS}


def get_secret(name):
    """API key from the environment, falling back to the Windows per-user environment (setx)
    when the current process was started before the variable was set. Never logged."""
    value = os.environ.get(name)
    if not value and sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = winreg.QueryValueEx(key, name)[0]
        except OSError:
            value = None
    if value:
        os.environ[name] = value  # visible to SDKs and child processes of this run only
    return value


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json(path, value):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_jsonl(path, rows):
    atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_jsonl(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


class Timer:
    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.seconds = round(time.perf_counter() - self.start, 3)
