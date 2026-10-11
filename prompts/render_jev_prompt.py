"""Render the exact Jev question set (pipeline/jev.py) to prompts/jev_q1.md for inspection."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline import jev  # noqa: E402

sample = "Example long review. The app keeps stopping when the screen is off! Also too many ads. Please fix it."
q, sents = jev.build_questions(sample * 3)
lines = [f"# Jev question set `{jev.PROMPT_VERSION}` (generated from pipeline/jev.py)", "",
         "Model: `jev-1.13.0` (TypeSafe System One). One review per request; `state` = the exact review text.",
         "All questions are evaluated in parallel on the same state. Code maps the answers to the schema (see pipeline/jev.py `map_answers`).",
         "The `evidence` question is only added for reviews longer than 200 characters with 2+ sentences; its options are exact sentences of that review.", ""]
for qid, obj in q.items():
    d = obj.model_dump(mode="json")
    lines += [f"## `{qid}` ({d['type']})", "", "**Instructions:** " + str(d["instructions"]), "", "**Criteria:**", "", "```json",
              json.dumps(d.get("criteria"), indent=2, ensure_ascii=False), "```", ""]
(ROOT / "prompts" / f"{jev.PROMPT_VERSION}.md").write_text("\n".join(lines), encoding="utf-8")
print("wrote prompts/" + jev.PROMPT_VERSION + ".md")

# Verifier question set (pipeline/verify.py jev_verify_questions): same definitions, no access to enricher output.
from pipeline.verify import jev_verify_questions  # noqa: E402

vq = jev_verify_questions()
lines = ["# Jev verifier question set `jev_verify_q1` (generated from pipeline/verify.py)", "",
         "Model: `jev-1.13.0`. One sampled review per request; `state` = the exact review text only.",
         "The verifier never sees the enricher's labels; code compares the two afterwards (pipeline/verify.py `compare`).", ""]
for qid, obj in vq.items():
    d = obj.model_dump(mode="json")
    lines += [f"## `{qid}` ({d['type']})", "", "**Instructions:** " + str(d["instructions"]), "", "**Criteria:**", "", "```json",
              json.dumps(d.get("criteria"), indent=2, ensure_ascii=False), "```", ""]
(ROOT / "prompts" / "jev_verify_q1.md").write_text("\n".join(lines), encoding="utf-8")
print("wrote prompts/jev_verify_q1.md")
