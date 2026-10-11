"""Stage 4 - Group (code owns membership; GROUPER model only names/describes issues).

Every completed complaint/cancellation record belongs to exactly one issue:
issue_id = "<topic>.<subtopic>". This mapping is deterministic and saved, so ranking never
needs a model. The model sees a bounded, seeded sample of quotes per issue and returns a
name, description, coherence rating and misfit IDs for human inspection.
"""
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from . import taxonomy as tx
from .common import ROOT, read_json, write_json
from .llm import BudgetExceeded, approx_tokens

COMPLAINT_INTENTS = ("complaint", "cancellation")


def build_membership(state, out_dir):
    rows = state.db.execute(
        f"""SELECT r.review_id, r.topic, r.subtopic FROM records r JOIN source s USING(review_id)
            WHERE r.status='completed' AND r.intent IN ({','.join('?' * len(COMPLAINT_INTENTS))}) ORDER BY s.row_idx""",
        COMPLAINT_INTENTS).fetchall()
    members = defaultdict(list)
    for rid, topic, sub in rows:
        members[f"{topic}.{sub}"].append(rid)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "membership.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["issue_id", "review_id"])
        for iid in sorted(members):
            for rid in sorted(members[iid]):
                w.writerow([iid, rid])
    return members


def schema():
    item = {"type": "object", "additionalProperties": False,
            "required": ["issue_id", "name", "description", "coherence", "misfit_review_ids"],
            "properties": {"issue_id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
                           "coherence": {"type": "string", "enum": ["high", "mixed", "low"]},
                           "misfit_review_ids": {"type": "array", "items": {"type": "string"}}}}
    return {"type": "object", "additionalProperties": False, "required": ["issues"],
            "properties": {"issues": {"type": "array", "items": item}}}


def _sample(state, ids, k, seed="group-2026-10"):
    """Seeded sample of distinct-text original records."""
    ranked = sorted(ids, key=lambda i: hashlib.sha256(f"{seed}:{i}".encode()).hexdigest())
    out, seen = [], set()
    for rid in ranked:
        quote, csid = state.db.execute("SELECT evidence_quote, cache_source_id FROM records WHERE review_id=?", (rid,)).fetchone()
        if csid or quote in seen:
            continue
        seen.add(quote)
        out.append({"review_id": rid, "quote": quote[:240]})
        if len(out) == k:
            break
    return out


def mock_group(user_input):
    data = json.loads(user_input)
    return {"issues": [{"issue_id": i["issue_id"], "name": i["issue_id"].replace(".", ": ").replace("_", " "),
                        "description": i["definition"], "coherence": "mixed", "misfit_review_ids": []} for i in data]}


def run_group(state, gw, cfg, log, run_dir):
    gc = cfg["group"]
    out_dir = Path(run_dir) / "group"
    members = build_membership(state, out_dir)
    packs = []
    for iid in sorted(members):
        topic, sub = iid.split(".")
        packs.append({"issue_id": iid, "definition": f"{tx.TOPIC_DEFS[topic]} Subtopic: {tx.SUBTOPICS[topic][sub]}",
                      "samples": _sample(state, members[iid], gc["examples_per_issue"])})
    fingerprint = hashlib.sha256(json.dumps([packs, gc], sort_keys=True).encode()).hexdigest()
    issues_path = out_dir / "issues.json"
    if issues_path.exists() and read_json(issues_path).get("input_fingerprint") == fingerprint:
        log("group", "reused saved issue names (same inputs and config); no model call", fingerprint=fingerprint[:12])
        return read_json(issues_path)
    write_json(out_dir / "group_input_packs.json", packs)  # the exact bounded handoff the model saw
    instr = (ROOT / "prompts" / f"{gc['prompt']}.md").read_text(encoding="utf-8")
    named = {}
    for start in range(0, len(packs), gc["issues_per_call"]):
        chunk = packs[start:start + gc["issues_per_call"]]
        user = json.dumps(chunk, ensure_ascii=False)
        ids_sent = sorted({s["review_id"] for p in chunk for s in p["samples"]})
        try:
            parsed, call = gw.call(role="group", model=gc["model"], effort=gc["effort"], instructions=instr, user_input=user,
                                   schema=schema(), review_ids=ids_sent, label_config=f"group|{gc['model']}|{gc['prompt']}",
                                   max_output_tokens=gc["max_output_tokens"], est_input_tokens=approx_tokens(instr + user),
                                   artifacts=["group/group_input_packs.json"], mock_fn=mock_group)
        except BudgetExceeded as e:
            log("group", "budget stop", detail=str(e))
            break
        except Exception as e:
            log("group", "call failed; taxonomy names used", error=str(e)[:200])
            continue
        valid_ids = {p["issue_id"]: {s["review_id"] for s in p["samples"]} for p in chunk}
        for item in parsed.get("issues", []):
            iid = item.get("issue_id")
            if iid not in valid_ids or iid in named:
                continue  # reject issues the model invented or repeated
            rejected = [m for m in item["misfit_review_ids"] if m not in valid_ids[iid]]
            named[iid] = {"name": item["name"].strip()[:80], "description": item["description"].strip()[:300],
                          "coherence": item["coherence"],
                          "misfit_review_ids": [m for m in item["misfit_review_ids"] if m in valid_ids[iid]],
                          "rejected_unknown_ids": rejected, "name_source": call["model"], "request_id": call["request_id"]}
    issues = []
    for p in packs:
        topic, sub = p["issue_id"].split(".")
        base = {"issue_id": p["issue_id"], "topic": topic, "subtopic": sub, "definition": p["definition"],
                "member_count": len(members[p["issue_id"]]), "sample_review_ids": [s["review_id"] for s in p["samples"]]}
        base.update(named.get(p["issue_id"]) or {"name": f"{topic}: {sub.replace('_', ' ')}", "description": p["definition"],
                                                 "coherence": None, "misfit_review_ids": [], "name_source": "taxonomy_fallback"})
        issues.append(base)
    result = {"input_fingerprint": fingerprint, "config": gc, "issues": issues,
              "membership_rule": "issue_id = topic.subtopic for every completed complaint/cancellation record; one issue per record"}
    write_json(issues_path, result)
    log("group", "done", issues=len(issues), named_by_model=len(named), members=sum(len(v) for v in members.values()))
    return result
