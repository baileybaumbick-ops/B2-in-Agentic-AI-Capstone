"""Planted-error test (SYNTHETIC; no model calls; never touches business outputs).

Takes the saved verifier predictions from a run, builds a separate TEST COPY of the enricher's
labels for the verified sample, deliberately corrupts a seeded subset (wrong topic, or severity
shifted by 2), and checks whether the code comparator flags each planted error.

    python evals/planted_errors.py --run-dir runs/dev500
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.common import TOPICS, write_json  # noqa: E402
from pipeline.state import State  # noqa: E402
from pipeline.verify import compare  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--fraction", type=float, default=0.25)
    a = p.parse_args()
    run_dir = Path(a.run_dir)
    state = State(run_dir / "state.sqlite")
    vrows = [json.loads(l) for l in (run_dir / "verify" / "verifier_predictions.jsonl").read_text(encoding="utf-8").splitlines()]
    cols = ("topic", "subtopic", "intent", "severity", "needs_review")
    copy = {}
    for v in vrows:
        row = state.db.execute(f"SELECT {','.join(cols)} FROM records WHERE review_id=?", (v["review_id"],)).fetchone()
        copy[v["review_id"]] = dict(zip(cols, row))
    ids = sorted(copy, key=lambda i: hashlib.sha256(f"planted:{i}".encode()).hexdigest())
    planted = []
    for k, rid in enumerate(ids[:max(1, int(len(ids) * a.fraction))]):
        e = copy[rid]
        if k % 2 == 0:  # wrong topic: seeded choice among topics != enricher's, chosen WITHOUT looking at the verifier
            others = [t for t in TOPICS if t != e["topic"]]
            wrong = others[int(hashlib.sha256(f"wrong:{rid}".encode()).hexdigest(), 16) % len(others)]
            planted.append({"review_id": rid, "field": "topic", "original": e["topic"], "planted": wrong})
            e["topic"] = wrong
        else:  # severity off by 2 in the safe direction
            new = e["severity"] + 2 if e["severity"] <= 3 else e["severity"] - 2
            planted.append({"review_id": rid, "field": "severity", "original": e["severity"], "planted": new})
            e["severity"] = new
    out = ROOT / "evals" / "results" / "planted_errors"
    report = compare(None, vrows, out, lambda *x, **y: None, len(vrows), enriched_override=copy)
    flagged = {r.strip() for r in (out / "disagreements.csv").read_text(encoding="utf-8").splitlines()[1:] for r in [r.split(",")[0]]}
    for pl in planted:
        pl["flagged_by_comparator"] = pl["review_id"] in flagged
    result = {"synthetic_test": True, "source_run": str(run_dir), "verified_sample": len(vrows), "planted": len(planted),
              "detected": sum(p["flagged_by_comparator"] for p in planted),
              "detection_rate": round(sum(p["flagged_by_comparator"] for p in planted) / len(planted), 4),
              "comparator_report_on_test_copy": report, "cases": planted,
              "note": "Test copy only; business records and aggregates are untouched. A planted error is detectable when the "
                      "independent verifier disagrees with the corrupted label."}
    write_json(out / "planted_errors_result.json", result)
    print(json.dumps({k: result[k] for k in ("verified_sample", "planted", "detected", "detection_rate")}, indent=2))


if __name__ == "__main__":
    main()
