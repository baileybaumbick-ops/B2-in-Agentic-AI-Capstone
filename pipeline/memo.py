"""Stage 6 - Recommend (MEMO role; code builds the evidence pack and checks every claim).

The model receives only saved aggregates, a claims table with IDs, and a bounded evidence
pack. Code rejects memos that cite unknown claim/review IDs or contain numbers that are not
in the claims table; it retries once with the violations listed, then saves with a warning.
"""
import csv
import hashlib
import json
import re
from pathlib import Path

from .common import ROOT, read_json, write_json, atomic_write_text
from .llm import BudgetExceeded, approx_tokens

AREA_ENUM = ["access", "usability", "playback", "billing_support"]
# Distinct, unambiguous claim-ID suffixes for area metrics (first-3-letter IDs collided: severity_sum vs sev4plus).
AREA_ABBR = {"complaints": "COMPL", "cancellations": "CANCEL", "severity_sum": "SEVSUM", "sev4plus": "SEV4P",
             "mean_severity": "MEANSEV"}


def _read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def fmt_int(v):
    return f"{int(v):,}"


def build_pack(run_dir, cfg, coverage, verify_report):
    mc = cfg["memo"]
    run_dir = Path(run_dir)
    ranking = _read_csv(run_dir / "rank" / "ranking.csv")
    areas = _read_csv(run_dir / "rank" / "aggregates_by_area.csv")
    aggs = {r["issue_id"]: r for r in _read_csv(run_dir / "rank" / "aggregates.csv")}
    issues = {i["issue_id"]: i for i in read_json(run_dir / "group" / "issues.json")["issues"]}
    texts_for = {}
    claims, issue_claims = [], []
    n = 0
    top = ranking[:mc["top_issues"]]
    for row in top:
        for metric in ("complaint_count", "severity_sum", "mean_severity", "priority_score"):
            n += 1
            cid = f"C{n:02d}"
            shown = row[metric] if metric == "mean_severity" else fmt_int(row[metric])
            claims.append({"claim_id": cid, "scope": "issue", "issue_id": row["issue_id"], "metric": metric,
                           "value": row[metric], "display": shown})
            issue_claims.append({"claim_id": cid, "issue_id": row["issue_id"], "metric": metric, "value": row[metric]})
        n_c = int(aggs[row["issue_id"]]["cancellations"])
        claims.append({"claim_id": f"C{n + 1:02d}", "scope": "issue_context", "issue_id": row["issue_id"],
                       "metric": "cancellation_intent_members", "value": str(n_c), "display": fmt_int(n_c)})
        n += 1
    for i, a in enumerate(areas, 1):
        for metric in ("complaints", "cancellations", "severity_sum", "sev4plus", "mean_severity"):
            claims.append({"claim_id": f"A{i}{AREA_ABBR[metric]}", "scope": "area", "issue_id": "", "area": a["group"],
                           "metric": metric, "value": a[metric],
                           "display": a[metric] if metric == "mean_severity" else fmt_int(a[metric] or 0)})
    # Trend context (code-computed): two comparable 3-month windows of full months, with denominators.
    trend_path = run_dir / "rank" / "trend_by_month.csv"
    if trend_path.exists():
        rows = {r["month"]: r for r in _read_csv(trend_path)}
        windows = {"early": ["2022-06", "2022-07", "2022-08"], "late": ["2023-08", "2023-09", "2023-10"]}
        t = 0
        for wname, months in windows.items():
            if not all(m in rows for m in months):
                continue
            denom = sum(int(rows[m]["completed_records"]) for m in months)
            t += 1
            claims.append({"claim_id": f"T{t:02d}", "scope": "trend", "issue_id": "", "metric": f"{wname}_window_completed_records",
                           "window": "+".join(months), "value": str(denom), "display": fmt_int(denom)})
            for area in ("access", "usability", "playback", "billing_support"):
                n_area = sum(int(rows[m][f"{area}_complaints"]) for m in months)
                t += 1
                claims.append({"claim_id": f"T{t:02d}", "scope": "trend", "issue_id": "", "area": area,
                               "metric": f"{wname}_window_complaints", "window": "+".join(months),
                               "value": str(n_area), "display": fmt_int(n_area)})
                t += 1
                pct = f"{100 * n_area / denom:.1f}%" if denom else "n/a"
                claims.append({"claim_id": f"T{t:02d}", "scope": "trend", "issue_id": "", "area": area,
                               "metric": f"{wname}_window_complaint_share_of_completed", "window": "+".join(months),
                               "value": pct, "display": pct})
    for k, (metric, value) in enumerate(coverage.items(), 1):
        claims.append({"claim_id": f"K{k:02d}", "scope": "coverage", "issue_id": "", "metric": metric, "value": str(value),
                       "display": fmt_int(value) if isinstance(value, int) else str(value)})
    evidence = []
    for row in top[:8]:
        iss = issues.get(row["issue_id"], {})
        misfit = set(iss.get("misfit_review_ids", []))
        picks = [rid for rid in iss.get("sample_review_ids", []) if rid not in misfit][:mc["quotes_per_issue"]]
        evidence.append({"issue_id": row["issue_id"], "name": iss.get("name"), "description": iss.get("description"),
                         "coherence": iss.get("coherence"), "examples": [{"review_id": r} for r in picks]})
        for r in picks:
            texts_for[r] = None
    return {"question": "Where should the next quarter of product effort go: access, usability, playback, or billing/support?",
            "claims": [{k: c[k] for k in ("claim_id", "scope", "issue_id", "area", "window", "metric", "display") if k in c and c[k] != ""} for c in claims],
            "issues": evidence,
            "method": {"baseline": "priority = complaint_count x mean_severity = severity_sum over complaint+cancellation records; one issue per record (topic.subtopic)",
                       "area_mapping": "access=access; usability=usability; playback=playback; billing_support=billing+support; downloads/catalog/other are outside the four areas",
                       "verifier_agreement": (verify_report or {}).get("agreement"),
                       "verifier_sample": (verify_report or {}).get("verified")},
            "limitations": ["Self-selected Google Play reviews, May 2022-Nov 2023; not a customer population sample.",
                            "No revenue, plan tier or observed churn; cancellation language is stated intent.",
                            "Labels come from a small model; see golden-set and verifier agreement.",
                            "Unresolved or quarantined records are excluded from aggregates (see coverage claims).",
                            "Area assignment of free-tier limits depends on the shared definitions: premium-only controls (skip "
                            "limits, forced shuffle, can't pick songs) are filed under billing. A reviewer who files them under "
                            "usability would see usability, not billing/support, as the largest area; the top issue and the "
                            "recommended work are the same either way. On the 50-review human golden set, topic agreement was "
                            "0.80 and most disagreements were on the usability/billing/catalog/other boundaries."]}, claims, issue_claims


def schema():
    return {"type": "object", "additionalProperties": False,
            "required": ["recommended_area", "title", "body_markdown", "cited_claim_ids", "cited_review_ids"],
            "properties": {"recommended_area": {"type": "string", "enum": AREA_ENUM}, "title": {"type": "string"},
                           "body_markdown": {"type": "string"},
                           "cited_claim_ids": {"type": "array", "items": {"type": "string"}},
                           "cited_review_ids": {"type": "array", "items": {"type": "string"}}}}


NUM_RE = re.compile(r"(?<![\w-])\d[\d,]*(?:\.\d+)?(?![\w-])")


def check_memo(memo, claims, evidence_ids):
    """Code-side claim check. Returns list of violations (empty = pass)."""
    body = memo["body_markdown"]
    by_id = {c["claim_id"]: c for c in claims}
    allowed = ({c["display"].replace(",", "").rstrip("%") for c in claims}
               | {str(c["value"]).replace(",", "").rstrip("%") for c in claims})
    violations = []
    cited = set(re.findall(r"\[([CAKT][0-9A-Z]+)\]", body))
    for cid in cited | set(memo["cited_claim_ids"]):
        if cid not in by_id:
            violations.append(f"unknown claim id {cid}")
    for m in NUM_RE.finditer(body):
        tok = m.group(0).rstrip(".,").replace(",", "")
        context = body[max(0, m.start() - 2):m.end() + 2]
        if tok in allowed or (tok.isdigit() and int(tok) <= 10) or re.match(r"20\d\d", tok):
            continue  # claim values, small ordinals ("top 3"), years
        violations.append(f"number not in claims table: {m.group(0)} (near '{context}')")
    # Each claim ID must be attached to the number it cites: the nearest number before a run of [IDs]
    # (within 80 characters) must equal every cited claim's value.
    for m in re.finditer(r"((?:\[[CAKT][0-9A-Z]+\])+)", body):
        ids = re.findall(r"\[([CAKT][0-9A-Z]+)\]", m.group(1))
        window = body[max(0, m.start() - 80):m.start()]
        nums = list(NUM_RE.finditer(window))
        if not nums:
            violations.append(f"claim id(s) {ids} not attached to a number")
            continue
        n = nums[-1].group(0).rstrip(".,").replace(",", "")
        for cid in ids:
            c = by_id.get(cid)
            if c and n not in {c["display"].replace(",", "").rstrip("%"), str(c["value"]).replace(",", "").rstrip("%")}:
                violations.append(f"number {nums[-1].group(0)} is cited as [{cid}] but {cid} = {c['display']} ({c['metric']})")
    uuid_like = set(re.findall(r"`([0-9a-f]{8}-[0-9a-f-]{27,})`", body)) | set(memo["cited_review_ids"])
    for rid in uuid_like:
        if rid not in evidence_ids:
            violations.append(f"review id not in evidence pack: {rid}")
    if not cited:
        violations.append("memo cites no claim IDs")
    return violations


def mock_memo(user_input):
    pack = json.loads(user_input)
    c = pack["claims"][0]
    ex = pack["issues"][0]["examples"][0]["review_id"] if pack["issues"] and pack["issues"][0]["examples"] else None
    body = (f"## Recommendation\nMOCK MEMO (offline development, not a real recommendation). Top issue {c['issue_id']} "
            f"has {c['display']} complaints [{c['claim_id']}]." + (f" Example: `{ex}`." if ex else ""))
    return {"recommended_area": "playback", "title": "MOCK", "body_markdown": body, "cited_claim_ids": [c["claim_id"]],
            "cited_review_ids": [ex] if ex else []}


def run_memo(state, gw, cfg, log, run_dir, coverage, verify_report):
    mc = cfg["memo"]
    run_dir = Path(run_dir)
    out = run_dir / "memo"
    pack, claims, issue_claims = build_pack(run_dir, cfg, coverage, verify_report)
    evidence_ids = {e["review_id"] for i in pack["issues"] for e in i["examples"]}
    # attach exact quotes for the evidence reviews (bounded)
    for iss in pack["issues"]:
        for e in iss["examples"]:
            e["quote"] = state.db.execute("SELECT evidence_quote FROM records WHERE review_id=?", (e["review_id"],)).fetchone()[0][:240]
    fingerprint = hashlib.sha256(json.dumps([pack, mc], sort_keys=True).encode()).hexdigest()
    saved = out / "memo_output.json"
    if saved.exists() and read_json(saved).get("input_fingerprint") == fingerprint:
        log("memo", "reused saved memo (same evidence pack and config); no model call")
        return read_json(saved)
    write_json(out / "memo_input_pack.json", pack)
    instr = (ROOT / "prompts" / f"{mc['prompt']}.md").read_text(encoding="utf-8")
    user = json.dumps(pack, ensure_ascii=False)
    memo, violations = None, []
    for attempt in (1, 2):
        prompt = user if attempt == 1 else user + "\n\nYOUR PREVIOUS DRAFT FAILED THESE CHECKS; fix them:\n" + "\n".join(violations)
        try:
            memo, call = gw.call(role="memo", model=mc["model"], effort=mc["effort"], instructions=instr, user_input=prompt,
                                 schema=schema(), review_ids=sorted(evidence_ids), label_config=f"memo|{mc['model']}|{mc['prompt']}",
                                 max_output_tokens=mc["max_output_tokens"], est_input_tokens=approx_tokens(instr + prompt),
                                 artifacts=["memo/memo_input_pack.json", "rank/ranking.csv", "rank/aggregates_by_area.csv"],
                                 mock_fn=mock_memo)
        except BudgetExceeded as e:
            log("memo", "budget stop", detail=str(e))
            return None
        violations = check_memo(memo, claims, evidence_ids)
        log("memo", "draft checked", attempt=attempt, violations=len(violations))
        if not violations:
            break
    cited_c = sorted(set(re.findall(r"\[(C\d+)\]", memo["body_markdown"])))
    used_issue_claims = [c for c in issue_claims if c["claim_id"] in cited_c]
    with (out / "claims.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["claim_id", "issue_id", "metric", "value"], lineterminator="\n")
        w.writeheader()
        w.writerows(used_issue_claims)
    write_json(out / "claims_all.json", claims)
    result = {"input_fingerprint": fingerprint, "memo": memo, "violations": violations, "validation": "pass" if not violations else "failed",
              "cited_issue_claims": used_issue_claims}
    write_json(saved, result)
    render(out / "memo.md", memo, claims, violations, coverage)
    log("memo", "done", recommended_area=memo["recommended_area"], validation=result["validation"])
    return result


def render(path, memo, claims, violations, coverage):
    by_id = {c["claim_id"]: c for c in claims}
    cited = sorted(set(re.findall(r"\[([CAKT][0-9A-Z]+)\]", memo["body_markdown"])), key=lambda x: (x[0], x))
    lines = [f"# {memo['title']}", "", f"**Recommended area:** `{memo['recommended_area']}`", ""]
    if violations:
        lines += ["> **Automated claim check FAILED** - the items below were not resolved; treat this draft with caution:", ""]
        lines += [f"> - {v}" for v in violations] + [""]
    lines += [memo["body_markdown"], "", "---", "", "## Claim table (generated by code from saved aggregates)", "",
              "| Claim | Scope | Issue / area | Metric | Value |", "|---|---|---|---|---|"]
    for cid in cited:
        c = by_id.get(cid)
        if c:
            lines.append(f"| {cid} | {c['scope']} | {c.get('issue_id') or c.get('area', '')} | {c['metric']} | {c['display']} |")
    lines += ["", "## Coverage (generated by code)", ""] + [f"- {k}: {v:,}" if isinstance(v, int) else f"- {k}: {v}" for k, v in coverage.items()]
    atomic_write_text(path, "\n".join(lines) + "\n")
