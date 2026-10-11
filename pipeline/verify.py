"""Stage 3 - Verify (VERIFIER role).

A different model re-labels a declared, seeded random sample of completed *original* records
(not cache copies) from the text and rubric alone; it never sees the first prediction.
Code then compares the two and writes an agreement/disagreement report.
"""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from . import taxonomy as tx
from .common import INTENTS, TOPICS, ROOT, write_json
from .enrich import ALL_SUBTOPICS, format_batch
from .llm import BudgetExceeded, approx_tokens


def schema():
    item = {"type": "object", "additionalProperties": False,
            "required": ["key", "topic", "subtopic", "intent", "severity", "confidence"],
            "properties": {"key": {"type": "string"}, "topic": {"type": "string", "enum": list(TOPICS)},
                           "subtopic": {"type": "string", "enum": ALL_SUBTOPICS},
                           "intent": {"type": "string", "enum": list(INTENTS)},
                           "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                           "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}
    return {"type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "array", "items": item}}}


def sample_ids(state, vc):
    """Deterministic sample: lowest SHA-256(seed:id) among completed original records."""
    rows = state.db.execute("SELECT review_id FROM records WHERE status='completed' AND cache_source_id IS NULL").fetchall()
    ids = [r[0] for r in rows]
    n = max(vc["min_sample"], min(vc["max_sample"], round(len(ids) * vc["sample_fraction"])))
    ids.sort(key=lambda i: hashlib.sha256(f"{vc['seed']}:{i}".encode()).hexdigest())
    return ids[:min(n, len(ids))]


def mock_verify(user_input):
    from .enrich import mock_fn
    out = mock_fn(user_input)["results"]
    return {"results": [{k: r[k] for k in ("key", "topic", "subtopic", "intent", "severity")} | {"confidence": "low"}
                        for r in out]}


def run_verify(state, gw, cfg, log, run_dir):
    vc = cfg["verify"]
    run_dir = Path(run_dir)
    out_path = run_dir / "verify" / "verifier_predictions.jsonl"
    done = {}
    if out_path.exists():  # resume: never re-pay for a sampled ID already verified under this config
        for line in out_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["verify_config"] == _vconfig(vc):
                done[r["review_id"]] = r
    ids = sample_ids(state, vc)
    todo = [i for i in ids if i not in done]
    instr = (ROOT / "prompts" / f"{vc['prompt']}.md").read_text(encoding="utf-8").replace("{definitions}", tx.definitions_text())
    texts = dict(state.db.execute(
        f"SELECT review_id, review_text FROM source WHERE review_id IN ({','.join('?' * len(todo))})", todo).fetchall()) if todo else {}
    log("verify", "sample", sample_size=len(ids), already_verified=len(done), to_send=len(todo), provider=vc.get("provider", "openai"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if vc.get("provider") == "jev":
        _verify_jev(todo, texts, gw, vc, log, out_path, done)
        return compare(state, [done[i] for i in ids if i in done], run_dir / "verify", log, len(ids))
    for start in range(0, len(todo), vc["batch_size"]):
        chunk = todo[start:start + vc["batch_size"]]
        user = format_batch([(f"v{i + 1}", texts[rid]) for i, rid in enumerate(chunk)])
        try:
            parsed, call = gw.call(role="verify", model=vc["model"], effort=vc["effort"], instructions=instr,
                                   user_input=user, schema=schema(), review_ids=chunk, label_config=_vconfig(vc),
                                   max_output_tokens=vc["max_output_tokens"],
                                   est_input_tokens=approx_tokens(instr) + approx_tokens(user), mock_fn=mock_verify)
        except BudgetExceeded as e:
            log("verify", "budget stop", detail=str(e))
            break
        except Exception as e:
            log("verify", "batch failed; left unverified", error=str(e)[:200])
            continue
        by_key = {r["key"]: r for r in parsed.get("results", [])}
        with out_path.open("a", encoding="utf-8") as f:
            for i, rid in enumerate(chunk):
                r = by_key.get(f"v{i + 1}")
                if r:
                    row = {"review_id": rid, "verify_config": _vconfig(vc), "request_id": call["request_id"],
                           **{k: r[k] for k in ("topic", "subtopic", "intent", "severity", "confidence")}}
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    done[rid] = row
    return compare(state, [done[i] for i in ids if i in done], run_dir / "verify", log, len(ids))


def _vconfig(vc):
    return f"{vc['model']}|effort={vc.get('effort')}|prompt={vc['prompt']}|seed={vc['seed']}"


def jev_verify_questions():
    """Independent re-label with Jev: same shared definitions, different provider and method (choice probabilities)."""
    from typesafe_sdk import Choice
    from . import jev
    return {"topic": jev.topic_question(Choice), "subtopic": jev.subtopic_question(Choice),
            "intent": Choice(instructions=jev.INTENT_INSTR, criteria=dict(tx.INTENT_DEFS)),
            "severity": Choice(instructions=jev.SEVERITY_INSTR, criteria={str(k): v for k, v in tx.SEVERITY_DEFS.items()})}


def mock_jev_verify(text):
    from .enrich import mock_jev
    return mock_jev(text)


def _verify_jev(todo, texts, gw, vc, log, out_path, done):
    from concurrent.futures import ThreadPoolExecutor
    questions = jev_verify_questions()

    def one(rid):
        try:
            parsed, call = gw.call(role="verify", model=vc["model"], effort=None, instructions="", user_input=texts[rid],
                                   schema=None, questions=questions, review_ids=[rid], label_config=_vconfig(vc),
                                   max_output_tokens=0, est_input_tokens=1700 + approx_tokens(texts[rid]), mock_fn=mock_jev_verify)
            return rid, parsed, call
        except Exception as e:
            return rid, e, None

    with ThreadPoolExecutor(max_workers=vc.get("workers", 4)) as pool, out_path.open("a", encoding="utf-8") as f:
        for rid, parsed, call in pool.map(one, todo):
            if isinstance(parsed, BudgetExceeded):
                log("verify", "budget stop", detail=str(parsed))
                break
            if isinstance(parsed, Exception):
                log("verify", "review left unverified", review_id=rid, error=str(parsed)[:200])
                continue
            topic = parsed["topic"]["choice"]
            sub_probs = {k: v for k, v in parsed["subtopic"]["probabilities"].items() if k.startswith(topic + ".")}
            sub = max(sub_probs, key=sub_probs.get).split(".", 1)[1] if sub_probs else next(iter(tx.SUBTOPICS[topic]))
            intent = parsed["intent"]["choice"]
            conf = min(parsed[q]["confidence"] for q in ("topic", "intent", "severity"))
            row = {"review_id": rid, "verify_config": _vconfig(vc), "request_id": call["request_id"], "topic": topic,
                   "subtopic": sub, "intent": intent,
                   "severity": tx.consistent_severity(intent, int(parsed["severity"]["choice"])),
                   "confidence": "high" if conf >= 0.8 else "medium" if conf >= 0.5 else "low",
                   "min_confidence": round(conf, 4)}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            done[rid] = row


def compare(state, verifier_rows, out_dir, log, sample_size, enriched_override=None):
    """Code-only comparison. enriched_override lets the planted-error test swap in a corrupted copy."""
    out_dir = Path(out_dir)
    cols = ("topic", "subtopic", "intent", "severity", "needs_review")
    agree = Counter()
    disagreements = []
    for v in verifier_rows:
        rid = v["review_id"]
        if enriched_override is not None:
            e = enriched_override[rid]
        else:
            row = state.db.execute(f"SELECT {','.join(cols)}, evidence_quote FROM records WHERE review_id=?", (rid,)).fetchone()
            e = dict(zip(cols + ("evidence_quote",), row))
        flags = {f: e[f] == v[f] for f in ("topic", "subtopic", "intent", "severity")}
        for f, ok in flags.items():
            agree[f] += ok
        agree["severity_within_1"] += abs(e["severity"] - v["severity"]) <= 1
        agree["sev_abs_err"] += abs(e["severity"] - v["severity"])
        agree["all3"] += flags["topic"] and flags["intent"] and flags["severity"]
        if not (flags["topic"] and flags["intent"] and flags["severity"]):
            disagreements.append({"review_id": rid, **{f"enrich_{f}": e[f] for f in ("topic", "subtopic", "intent", "severity")},
                                  **{f"verify_{f}": v[f] for f in ("topic", "subtopic", "intent", "severity")},
                                  "verify_confidence": v.get("confidence"), "enrich_needs_review": e.get("needs_review"),
                                  "fields_disagreeing": ",".join(f for f in ("topic", "intent", "severity") if not flags[f])})
    n = len(verifier_rows)
    report = {"sample_size_declared": sample_size, "verified": n,
              "agreement": {f: round(agree[f] / n, 4) if n else None for f in ("topic", "subtopic", "intent", "severity", "severity_within_1", "all3")},
              "severity_mae": round(agree["sev_abs_err"] / n, 4) if n else None,
              "disagreement_count": len(disagreements),
              "needs_review_rate_among_disagreements": round(sum(bool(d["enrich_needs_review"]) for d in disagreements) / len(disagreements), 4) if disagreements else None,
              "topic_confusion": dict(Counter(f"{d['enrich_topic']}->{d['verify_topic']}" for d in disagreements if d["enrich_topic"] != d["verify_topic"]).most_common(20))}
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "verify_report.json", report)
    if disagreements or n:
        with (out_dir / "disagreements.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(disagreements[0].keys()) if disagreements else ["review_id"])
            w.writeheader()
            w.writerows(disagreements)
    log("verify", "compared", **{k: report[k] for k in ("verified", "disagreement_count", "severity_mae")}, agreement=report["agreement"])
    return report
