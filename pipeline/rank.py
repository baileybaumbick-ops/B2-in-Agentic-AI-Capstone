"""Stage 5 - Rank (code only; no model). Reproducible from saved files alone:

    python run.py rank --run-dir <dir>

priority(issue) = complaint_count x mean_severity = severity_sum (integer arithmetic, no float loss).
Sort: descending priority, then ascending issue_id. Means: 6 decimals, decimal half-up.
"""
import csv
import gzip
import json
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .common import read_json, write_json

AREAS = {"access": "access", "usability": "usability", "playback": "playback", "billing": "billing_support",
         "support": "billing_support", "downloads": "outside_four_areas", "catalog": "outside_four_areas",
         "other": "outside_four_areas"}


def mean6(total, n):
    return str((Decimal(total) / Decimal(n)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def load_records(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return {r["review_id"]: r for r in (json.loads(line) for line in f if line.strip())}


def rank(records_path, membership_path, out_dir):
    records = load_records(records_path)
    sev = defaultdict(list)
    cancels = defaultdict(int)
    seen_pairs = set()
    with open(membership_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            pair = (row["issue_id"], row["review_id"])
            r = records.get(row["review_id"])
            if pair in seen_pairs or r is None or r.get("status") != "completed" or r["intent"] not in ("complaint", "cancellation"):
                raise ValueError(f"invalid membership row {row}")
            seen_pairs.add(pair)
            sev[row["issue_id"]].append(r["severity"])
            cancels[row["issue_id"]] += r["intent"] == "cancellation"
    ranking = [{"issue_id": iid, "complaint_count": len(v), "severity_sum": sum(v), "mean_severity": mean6(sum(v), len(v)),
                "priority_score": sum(v)} for iid, v in sev.items()]
    ranking.sort(key=lambda x: (-x["priority_score"], x["issue_id"]))
    for i, row in enumerate(ranking, 1):
        row["rank"] = i
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "ranking.csv", ["rank", "issue_id", "complaint_count", "severity_sum", "mean_severity", "priority_score"], ranking)

    # Additional context tables (not the baseline): per-topic and per-product-area aggregates over ALL completed records.
    completed = [r for r in records.values() if r.get("status") == "completed"]
    total_completed = len(completed)
    by_topic = defaultdict(lambda: {"records": 0, "complaints": 0, "cancellations": 0, "severity_sum": 0, "sev4plus": 0})
    by_area = defaultdict(lambda: {"records": 0, "complaints": 0, "cancellations": 0, "severity_sum": 0, "sev4plus": 0})
    for r in completed:
        for key, table in ((r["topic"], by_topic), (AREAS[r["topic"]], by_area)):
            t = table[key]
            t["records"] += 1
            if r["intent"] in ("complaint", "cancellation"):
                t["complaints"] += 1
                t["cancellations"] += r["intent"] == "cancellation"
                t["severity_sum"] += r["severity"]
                t["sev4plus"] += r["severity"] >= 4
    for name, table in (("aggregates_by_topic.csv", by_topic), ("aggregates_by_area.csv", by_area)):
        rows = []
        for key, t in sorted(table.items(), key=lambda kv: -kv[1]["severity_sum"]):
            rows.append({"group": key, **t, "mean_severity": mean6(t["severity_sum"], t["complaints"]) if t["complaints"] else "",
                         "share_of_completed_records": mean6(t["records"], total_completed)})
        _write_csv(out_dir / name, ["group", "records", "complaints", "cancellations", "severity_sum", "sev4plus",
                                    "mean_severity", "share_of_completed_records"], rows)
    # aggregates.csv = baseline ranking joined with issue names (if grouping saved them)
    names = {}
    issues_path = Path(membership_path).parent / "issues.json"
    if issues_path.exists():
        names = {i["issue_id"]: i for i in read_json(issues_path)["issues"]}
    agg = [{**row, "area": AREAS[row["issue_id"].split(".")[0]], "name": names.get(row["issue_id"], {}).get("name", ""),
            "cancellations": cancels[row["issue_id"]]}
           for row in ranking]
    _write_csv(out_dir / "aggregates.csv", ["rank", "issue_id", "name", "area", "complaint_count", "cancellations",
                                           "severity_sum", "mean_severity", "priority_score"], agg)
    summary = {"issues": len(ranking), "members": len(seen_pairs), "completed_records": total_completed,
               "top5": [r["issue_id"] for r in ranking[:5]]}
    write_json(out_dir / "rank_summary.json", summary)
    return ranking


def _write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in fields})
