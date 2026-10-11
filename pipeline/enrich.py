"""Stage 2 - Classify (ENRICHER role, model judgment; code owns accounting and validation).

Code: exact-text cache lookup, batching (<=50 reviews/request), ID validation, schema and
quote checks, one invalid-output retry, capped stronger-model fallback, quarantine, and an
atomic save after every batch. Model: topic/subtopic/intent/severity/sentiment/quote/entities.
"""
import json
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import taxonomy as tx
from .common import ROOT, TOPICS, INTENTS
from .config import label_config
from .llm import BudgetExceeded, ModelOutputError, approx_tokens

ALL_SUBTOPICS = [s for subs in tx.SUBTOPICS.values() for s in subs]
SUB_TO_TOPIC = {s: t for t, subs in tx.SUBTOPICS.items() for s in subs}
MAX_QUOTE_FALLBACK_CHARS = 300


def schema():
    item = {
        "type": "object", "additionalProperties": False,
        "required": ["key", "quote", "topic", "subtopic", "intent", "severity", "sentiment", "entities", "needs_review"],
        "properties": {
            "key": {"type": "string"},
            "quote": {"type": "string"},
            "topic": {"type": "string", "enum": list(TOPICS)},
            "subtopic": {"type": "string", "enum": ALL_SUBTOPICS},
            "intent": {"type": "string", "enum": list(INTENTS)},
            "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "sentiment": {"type": "number", "enum": list(tx.SENTIMENT_LEVELS)},
            "entities": {"type": "array", "items": {"type": "string"}},
            "needs_review": {"type": "boolean"},
        },
    }
    return {"type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "array", "items": item}}}


def fallback_schema():
    """Like schema() but topic+subtopic are one enum value 'topic.subtopic', so they cannot contradict."""
    sch = schema()
    item = sch["properties"]["results"]["items"]
    props = item["properties"]
    props.pop("topic")
    props.pop("subtopic")
    props["issue"] = {"type": "string", "enum": tx.issue_ids()}
    item["required"] = ["key", "quote", "issue", "intent", "severity", "sentiment", "entities", "needs_review"]
    return sch


FALLBACK_NOTE = ("\n\nOUTPUT NOTE (fallback): instead of separate topic and subtopic fields, return issue = "
                 "'<topic>.<subtopic>' using exactly one allowed value; the topic is the part before the dot.")


def instructions(prompt_name):
    text = (ROOT / "prompts" / f"{prompt_name}.md").read_text(encoding="utf-8")
    return text.replace("{definitions}", tx.definitions_text())


def format_batch(items):
    """items: list of (key, text). JSON keeps review text as data and preserves it exactly."""
    return json.dumps([{"key": k, "text": t} for k, t in items], ensure_ascii=False)


# ---------- validation ----------

def locate_quote(quote, text):
    """Return (exact_substring, note) or (None, reason). Repairs only trivial copy differences."""
    if quote and quote.strip() and quote in text:
        return quote, None
    q = (quote or "").strip().strip("\"'“”‘’").strip()
    if q and q in text:
        return q, "quote_trimmed"
    if q:
        i = text.lower().find(q.lower())
        if i >= 0 and text[i:i + len(q)].lower() == q.lower():
            return text[i:i + len(q)], "quote_case_repaired"
        tokens = [re.escape(t) for t in q.split()]
        if tokens:
            m = re.search(r"\s+".join(tokens), text, flags=re.IGNORECASE)
            if m and m.group(0).strip():
                return m.group(0), "quote_whitespace_repaired"
    return None, "quote_not_in_source"


def validate_item(item, text):
    """Returns (clean_labels, None) or (None, reason)."""
    if item.get("topic") not in TOPICS or item.get("intent") not in INTENTS:
        return None, "invalid_label"
    if SUB_TO_TOPIC.get(item.get("subtopic")) != item["topic"]:
        return None, "subtopic_topic_mismatch"
    sev = item.get("severity")
    if type(sev) is not int or not 1 <= sev <= 5:
        return None, "invalid_severity"
    sent = item.get("sentiment")
    if type(sent) not in (int, float) or not -1 <= sent <= 1:
        return None, "invalid_sentiment"
    quote, note = locate_quote(item.get("quote", ""), text)
    if quote is None:
        return None, note
    entities = [e.strip() for e in item.get("entities", []) if isinstance(e, str) and e.strip()]
    return {"topic": item["topic"], "subtopic": item["subtopic"], "intent": item["intent"], "severity": sev,
            "sentiment": float(sent), "entities": list(dict.fromkeys(entities)), "evidence_quote": quote,
            "needs_review": bool(item.get("needs_review")), "review_note": note}, None


def force_quote(labels_item, text):
    """Second quote failure: keep the model's labels, use the source text itself, flag for review."""
    quote = text if len(text) <= MAX_QUOTE_FALLBACK_CHARS else text[:MAX_QUOTE_FALLBACK_CHARS]
    if not quote.strip():
        return None
    item = dict(labels_item, quote=quote)
    clean, err = validate_item(item, text)
    if clean:
        clean.update(needs_review=True, review_note="quote_fallback_source_text")
    return clean


# ---------- offline mock (development only) ----------

def _mock_fallback(user_input):
    out = mock_fn(user_input)
    for r in out["results"]:
        r["issue"] = f"{r.pop('topic')}.{r.pop('subtopic')}"
    return out


MOCK_RULES = [
    (r"log ?in|sign ?in|password|account", "access", "login_failure", 4),
    (r"\bads?\b|advert", "usability", "ads", 2),
    (r"skip|shuffle|premium|pay", "billing", "free_tier_restrictions", 3),
    (r"crash|won'?t open|freez", "playback", "crash_or_freeze", 4),
    (r"stop|pause|won'?t play", "playback", "stops_or_skips", 3),
    (r"download|offline", "downloads", "download_failure", 3),
    (r"lyric", "catalog", "lyrics", 2),
]


def mock_fn(user_input):
    out = []
    for r in json.loads(user_input):
        t = r["text"]
        topic, sub, sev, intent = "other", "general_praise", 1, "praise"
        for pat, tp, sb, sv in MOCK_RULES:
            if re.search(pat, t, re.I):
                topic, sub, sev, intent = tp, sb, sv, "complaint"
                break
        if intent == "praise" and re.search(r"bad|worst|hate|terrible|annoy", t, re.I):
            sub, sev, intent = "general_negative", 2, "complaint"
        if re.search(r"uninstall|cancel", t, re.I):
            intent = "cancellation"
        out.append({"key": r["key"], "quote": t[:60], "topic": topic, "subtopic": sub, "intent": intent,
                    "severity": sev, "sentiment": -0.5 if intent != "praise" else 0.5, "entities": [],
                    "needs_review": False})
    return {"results": out}


# ---------- stage driver ----------

def _policy_refusal(err):
    text = str(err)
    return type(err).__name__.endswith("BadRequestError") and ("flagged" in text or "usage policy" in text)


class Enricher:
    def __init__(self, state, gateway, cfg, log, run_dir):
        self.state, self.gw, self.cfg, self.log = state, gateway, cfg, log
        self.run_dir = Path(run_dir)
        self.lc = label_config(cfg, "enrich")
        self.ec = cfg["enrich"]
        self.instr = instructions(self.ec["prompt"])
        self.instr_tokens = approx_tokens(self.instr)
        self.schema = schema()
        self._splits = None
        self.stats = {"requests": 0, "failed_requests": 0, "cache_hits": 0, "completed": 0, "quarantined": 0,
                      "retried_items": 0, "quote_fallbacks": 0, "fallback_items": 0, "stopped_reason": None}

    # --- cache: complete pending rows whose exact text already has a valid original under this config
    def apply_cache(self):
        rows = self.state.db.execute(
            """SELECT p.review_id, o.review_id FROM records p JOIN source ps ON ps.review_id=p.review_id
               JOIN source os ON os.text_sha=ps.text_sha AND os.review_text=ps.review_text
               JOIN records o ON o.review_id=os.review_id
               WHERE p.status='pending' AND o.status='completed' AND o.cache_source_id IS NULL AND o.label_config=?""",
            (self.lc,)).fetchall()
        seen, completed = set(), []
        for rid, origin in rows:
            if rid in seen:
                continue
            seen.add(rid)
            completed.append(self._copy_from(origin, rid))
        if completed:
            self.state.save_batch(completed, [])
            self.stats["cache_hits"] += len(completed)
        self.log("enrich", "cache lookup", cache_hits=len(completed), label_config=self.lc)

    def _copy_from(self, origin, rid):
        o = self.state.db.execute(
            "SELECT topic,subtopic,intent,severity,sentiment,entities,evidence_quote,needs_review,review_note,label_config "
            "FROM records WHERE review_id=?", (origin,)).fetchone()
        keys = ("topic", "subtopic", "intent", "severity", "sentiment", "entities", "evidence_quote", "needs_review",
                "review_note", "label_config")
        rec = dict(zip(keys, o))
        rec["entities"] = json.loads(rec["entities"])
        rec["needs_review"] = bool(rec["needs_review"])
        rec.update(review_id=rid, cache_source_id=origin, enrich_request_id=None, attempts=0, run_phase=self.gw.phase)
        return rec

    def pending_units(self):
        """One unit per distinct pending text: (representative_id, text, [duplicate ids])."""
        rows = self.state.db.execute(
            """SELECT s.review_id, s.text_sha, s.review_text, r.attempts FROM records r JOIN source s USING(review_id)
               WHERE r.status='pending' ORDER BY s.row_idx""").fetchall()
        units = {}
        for rid, tsha, text, attempts in rows:
            key = (tsha, text)
            if key in units:
                units[key][2].append(rid)
            else:
                units[key] = [rid, text, [], attempts]
        return list(units.values())

    def _send(self, batch, attempt):
        """batch: list of units. Runs in a worker thread. Returns (batch, attempt, parsed|exception, call)."""
        items = [(f"r{i + 1}", u[1]) for i, u in enumerate(batch)]
        user = format_batch(items)
        est = self.instr_tokens + approx_tokens(user) + 20
        try:
            parsed, call = self.gw.call(
                role="enrich", model=self.ec["model"], effort=self.ec["effort"], instructions=self.instr,
                user_input=user, schema=self.schema, review_ids=[u[0] for u in batch], label_config=self.lc,
                max_output_tokens=self.ec["max_output_tokens"], est_input_tokens=est, mock_fn=mock_fn)
            return batch, attempt, parsed, call
        except (ModelOutputError, BudgetExceeded) as e:
            return batch, attempt, e, None
        except Exception as e:  # transport failure after bounded retries
            return batch, attempt, e, None

    def _process(self, batch, attempt, parsed, call):
        """Validate, save, and return units needing one retry."""
        completed, quarantined, retry = [], [], []
        if isinstance(parsed, Exception):
            if isinstance(parsed, ModelOutputError):
                if attempt == 1:
                    self.state.db.executemany("UPDATE records SET attempts=attempts+1 WHERE review_id=?", [(u[0],) for u in batch])
                    for u in batch:
                        u[3] += 1
                    return batch  # retry whole batch once
                if len(batch) > 1:
                    # Failed twice as a whole (truncated/invalid JSON): split so one bad review cannot sink the rest.
                    self.state.db.executemany("UPDATE records SET attempts=attempts+1 WHERE review_id=?", [(u[0],) for u in batch])
                    for u in batch:
                        u[3] += 1
                    mid = len(batch) // 2
                    self._splits = [batch[:mid], batch[mid:]]
                    self.stats["batch_splits"] = self.stats.get("batch_splits", 0) + 1
                    self.log("enrich", "batch failed twice; splitting", size=len(batch))
                    return []
                quarantined = [(u[0], "model_output_error", u[3] + 1) for u in batch]
                quarantined += [(d, "duplicate_of_quarantined:" + u[0], 0) for u in batch for d in u[2]]
                self.state.save_batch([], quarantined)
                self.stats["quarantined"] += len(quarantined)
            return []
        results = parsed.get("results", []) if isinstance(parsed, dict) else []
        by_key = {}
        for r in results:
            if isinstance(r, dict) and isinstance(r.get("key"), str) and r["key"] not in by_key:
                by_key[r["key"]] = r  # duplicate keys: first wins, rest ignored
        expected = {f"r{i + 1}": u for i, u in enumerate(batch)}
        for key, u in expected.items():
            rid, text, dups, prior = u
            item = by_key.get(key)
            clean, err = (validate_item(item, text) if item else (None, "missing_id_in_output"))
            if clean is None and attempt >= 2 and item and err == "quote_not_in_source":
                clean = force_quote(item, text)
                if clean:
                    self.stats["quote_fallbacks"] += 1
            if clean is None:
                if attempt == 1:
                    u[3] += 1
                    self.state.db.execute("UPDATE records SET attempts=? WHERE review_id=?", (u[3], rid))
                    retry.append(u)
                    continue
                quarantined.append((rid, f"invalid_model_output:{err}", prior + 1))
                quarantined += [(d, "duplicate_of_quarantined:" + rid, 0) for d in dups]
                continue
            rec = dict(clean, review_id=rid, label_config=call["label_config"], cache_source_id=None,
                       enrich_request_id=call["request_id"], attempts=prior + 1, run_phase=self.gw.phase)
            completed.append(rec)
            for d in dups:
                completed.append(dict(rec, review_id=d, cache_source_id=rid, enrich_request_id=None, attempts=0))
        unknown = set(by_key) - set(expected)
        if unknown:
            self.log("enrich", "ignored unknown keys in output", keys=sorted(unknown)[:10], request_id=call["request_id"])
        self.state.save_batch(completed, quarantined)  # atomic, after every batch
        self.stats["completed"] += len(completed)
        self.stats["quarantined"] += len(quarantined)
        self.stats["retried_items"] += len(retry)
        return retry

    def run(self, max_batches=None, stop_file=None):
        self.apply_cache()
        units = self.pending_units()
        bs = self.ec["batch_size"]
        assert 1 <= bs <= 50, "enrichment batches must hold at most 50 reviews"
        queue = [(units[i:i + bs], 1) for i in range(0, len(units), bs)]
        self.log("enrich", "queued", distinct_texts=len(units), batches=len(queue), workers=self.cfg["limits"]["workers"])
        workers = self.cfg["limits"]["workers"]
        in_flight, dispatched, consecutive_fail = set(), 0, 0
        stop = None
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                while queue or in_flight:
                    if stop is None:
                        if stop_file and Path(stop_file).exists():
                            stop = "stop_file"
                        elif max_batches is not None and dispatched >= max_batches:
                            stop = "max_batches"
                    while stop is None and queue and len(in_flight) < workers:
                        batch, attempt = queue.pop(0)
                        in_flight.add(pool.submit(self._send, batch, attempt))
                        dispatched += 1
                    if not in_flight:
                        break
                    done, in_flight = wait(in_flight, timeout=0.5, return_when=FIRST_COMPLETED)
                    for fut in done:
                        batch, attempt, parsed, call = fut.result()
                        self.stats["requests"] += 1
                        if isinstance(parsed, BudgetExceeded):
                            stop = stop or "spend_cap"
                            self.log("enrich", "budget stop", detail=str(parsed))
                            queue.insert(0, (batch, attempt))
                            continue
                        if isinstance(parsed, Exception) and _policy_refusal(parsed):
                            # Provider refused the whole request because of some review's content. Bisect to isolate
                            # it; a single refused review is quarantined for the cross-provider (Jev) fallback.
                            self.stats["policy_refusals"] = self.stats.get("policy_refusals", 0) + 1
                            if len(batch) > 1:
                                mid = len(batch) // 2
                                queue[0:0] = [(batch[:mid], attempt), (batch[mid:], attempt)]
                                self.log("enrich", "policy refusal; bisecting batch", size=len(batch))
                            else:
                                u = batch[0]
                                self.state.save_batch([], [(u[0], "provider_policy_refusal", u[3] + 1)]
                                                      + [(d, "duplicate_of_quarantined:" + u[0], 0) for d in u[2]])
                                self.stats["quarantined"] += 1 + len(u[2])
                                self.log("enrich", "policy refusal isolated; quarantined for fallback", review_id=u[0])
                            continue
                        if isinstance(parsed, Exception) and not isinstance(parsed, ModelOutputError):
                            self.stats["failed_requests"] += 1
                            consecutive_fail += 1
                            self.log("enrich", "batch left pending after transport failures", error=str(parsed)[:200])
                            if consecutive_fail >= self.cfg["limits"]["consecutive_failure_stop"]:
                                stop = stop or "consecutive_failures"
                            continue
                        consecutive_fail = 0
                        self._splits = None
                        retry = self._process(batch, attempt, parsed, call)
                        if retry:
                            queue.insert(0, (retry, attempt + 1))
                        if self._splits:
                            queue[0:0] = [(half, 1) for half in self._splits]
                    if stop:
                        queue.clear()  # undispatched work simply stays 'pending' in the state DB
            except KeyboardInterrupt:
                stop = "keyboard_interrupt"
                self.log("enrich", "interrupt received; finishing in-flight batches then saving")
                for fut in in_flight:
                    batch, attempt, parsed, call = fut.result()
                    if not isinstance(parsed, Exception):
                        self._process(batch, attempt, parsed, call)
        self.stats["stopped_reason"] = stop
        self.stats["seconds"] = round(time.perf_counter() - t0, 3)
        self.log("enrich", "stage end", **self.stats, counts=self.state.counts())
        return self.stats

    def _jev_fallback(self, rows):
        """Cross-provider fallback for reviews the main provider refused to process (content policy)."""
        import copy
        from .config import ENRICHERS
        cfg = copy.deepcopy(self.cfg)
        cfg["enrich"] = copy.deepcopy(ENRICHERS["jev"])
        if self.gw.mock:
            cfg["enrich"]["model"] = "mock-keyword-v0"
        jevr = JevEnricher(self.state, self.gw, cfg, self.log, self.run_dir)
        done = 0
        for rid, text, prior in rows:
            dups = [r[0] for r in self.state.db.execute("SELECT review_id FROM records WHERE reason=?",
                                                         ("duplicate_of_quarantined:" + rid,))]
            unit = [rid, text, dups, prior]
            for attempt in (1, 2):
                batch, attempt, parsed, call = jevr._send([unit], attempt)
                if isinstance(parsed, BudgetExceeded):
                    return done
                if isinstance(parsed, Exception):
                    self.log("fallback", "jev fallback failed", review_id=rid, error=str(parsed)[:200])
                    continue
                if not jevr._process(batch, attempt, parsed, call):
                    done += 1
                    break
        self.stats["jev_fallback_items"] = done
        self.log("fallback", "jev cross-provider fallback done", attempted=len(rows), completed=done)
        return done

    def run_fallback(self):
        """Capped stronger-model pass over records quarantined for invalid model output."""
        fc = self.cfg["fallback"]
        total = self.state.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        cap = max(5, int(total * fc["max_fraction"]))  # declared cap; floor of 5 for small runs
        refused = self.state.db.execute(
            """SELECT r.review_id, s.review_text, r.attempts FROM records r JOIN source s USING(review_id)
               WHERE r.status='quarantined' AND r.reason='provider_policy_refusal' ORDER BY s.row_idx""").fetchall()
        if refused and self.cfg["enrich"].get("provider") != "jev":
            self._jev_fallback(refused[:cap])
        rows = self.state.db.execute(
            """SELECT r.review_id, s.review_text, r.attempts FROM records r JOIN source s USING(review_id)
               WHERE r.status='quarantined' AND (r.reason LIKE 'invalid_model_output%' OR r.reason LIKE 'fallback_invalid%')
               ORDER BY s.row_idx""").fetchall()
        if not rows:
            return 0
        lc = label_config(self.cfg, "fallback")
        done = 0
        for rid, text, prior in rows[:cap]:
            user = format_batch([("r1", text)])
            try:
                # fallback always uses the v1 single-review prompt and schema (its label_config says prompt=enrich_v1)
                parsed, call = self.gw.call(
                    role="enrich", model=fc["model"], effort=fc["effort"], instructions=instructions("enrich_v1") + FALLBACK_NOTE,
                    user_input=user, schema=fallback_schema(), review_ids=[rid], label_config=lc,
                    max_output_tokens=fc["max_output_tokens"], mock_fn=_mock_fallback)
            except BudgetExceeded:
                break
            except Exception as e:
                self.log("fallback", "failed", review_id=rid, error=str(e)[:200])
                continue
            items = parsed.get("results", [])
            item = dict(items[0]) if items else None
            if item and "." in str(item.get("issue", "")):
                item["topic"], item["subtopic"] = item.pop("issue").split(".", 1)
            if item and item.get("intent") in INTENTS and type(item.get("severity")) is int:
                item["severity"] = tx.consistent_severity(item["intent"], item["severity"])
            clean, err = validate_item(item, text) if item else (None, "missing_id_in_output")
            if clean is None and item and err == "quote_not_in_source":
                clean = force_quote(item, text)
            if clean is None:
                self.state.db.execute("UPDATE records SET reason=?, attempts=? WHERE review_id=?",
                                      (f"fallback_invalid:{err}", prior + 1, rid))
                continue
            rec = dict(clean, review_id=rid, label_config=lc, cache_source_id=None, enrich_request_id=call["request_id"],
                       attempts=prior + 1, run_phase=self.gw.phase)
            dups = [r[0] for r in self.state.db.execute(
                "SELECT review_id FROM records WHERE reason=?", ("duplicate_of_quarantined:" + rid,))]
            batch = [rec] + [dict(rec, review_id=d, cache_source_id=rid, enrich_request_id=None, attempts=0) for d in dups]
            self.state.save_batch(batch, [])
            done += 1
        self.stats["fallback_items"] = done
        self.log("fallback", "done", attempted=min(len(rows), cap), completed=done, cap=cap, eligible=len(rows))
        return done


# ---------- Jev enricher (one review per request; code maps probabilities to the schema) ----------

def mock_jev(text):
    """Offline stand-in returning Jev-shaped answers (development only)."""
    r = mock_fn(json.dumps([{"key": "r1", "text": text}]))["results"][0]
    one = lambda choice, opts: {"choice": choice, "confidence": 0.9, "probabilities": {o: (0.9 if o == choice else 0.1 / max(1, len(opts) - 1)) for o in opts}}
    subs = [f"{t}.{s}" for t, ss in tx.SUBTOPICS.items() for s in ss]
    return {"topic": one(r["topic"], TOPICS), "subtopic": one(f"{r['topic']}.{r['subtopic']}", subs),
            "intent": one(r["intent"], INTENTS), "severity": one(str(r["severity"]), ["1", "2", "3", "4", "5"]),
            "sentiment": {"score": 1.0 if r["intent"] != "praise" else 3.0, "confidence": 0.9}, "injection": {"noul": 0.01}}


class JevEnricher(Enricher):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        from . import jev
        self.jev = jev
        self.instr = ""  # Jev questions carry their own instructions (pipeline/jev.py, version jev_q1)
        self.diag_path = self.run_dir / "enrich" / "jev_answers.jsonl"
        self.diag_path.parent.mkdir(parents=True, exist_ok=True)

    def _send(self, batch, attempt):
        (rid, text, dups, prior), = batch  # exactly one review per Jev request
        questions, sents = self.jev.build_questions(text)
        est = 1800 + approx_tokens(text) * (2 if len(sents) > 1 else 1)
        try:
            parsed, call = self.gw.call(
                role="enrich", model=self.ec["model"], effort=None, instructions="", user_input=text, schema=None,
                questions=questions, review_ids=[rid], label_config=self.lc, max_output_tokens=0,
                est_input_tokens=est, mock_fn=mock_jev)
            return batch, attempt, (parsed, sents), call
        except Exception as e:
            return batch, attempt, e, None

    def _process(self, batch, attempt, parsed, call):
        (rid, text, dups, prior), = batch
        u = batch[0]
        if isinstance(parsed, Exception):
            return super()._process(batch, attempt, parsed, call)
        answers, sents = parsed
        try:
            labels, diag = self.jev.map_answers(answers, text, sents, self.ec["thresholds"])
        except (KeyError, ValueError, TypeError, IndexError) as e:
            if attempt == 1:
                u[3] += 1
                self.state.db.execute("UPDATE records SET attempts=? WHERE review_id=?", (u[3], rid))
                self.stats["retried_items"] += 1
                return batch
            self.state.save_batch([], [(rid, f"invalid_model_output:{type(e).__name__}", prior + 1)]
                                  + [(d, "duplicate_of_quarantined:" + rid, 0) for d in dups])
            self.stats["quarantined"] += 1 + len(dups)
            return []
        rec = dict(labels, review_id=rid, label_config=call["label_config"], cache_source_id=None,
                   enrich_request_id=call["request_id"], attempts=prior + 1, run_phase=self.gw.phase)
        completed = [rec] + [dict(rec, review_id=d, cache_source_id=rid, enrich_request_id=None, attempts=0) for d in dups]
        self.state.save_batch(completed, [])
        with self.diag_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"review_id": rid, "request_id": call["request_id"], "model_version": call.get("provider_model_version"),
                                **diag}, ensure_ascii=False) + "\n")
        self.stats["completed"] += len(completed)
        return []


def make_enricher(state, gw, cfg, log, run_dir):
    return (JevEnricher if cfg["enrich"].get("provider") == "jev" else Enricher)(state, gw, cfg, log, run_dir)


# ---------- Compact batched LLM enricher (v2): 50 reviews/request, prompt caching, Flex tier ----------

QUOTE_BY_CODE_CHARS = 200  # reviews up to this length use their full text as the evidence quote (code, not model)


def compact_schema():
    item = {
        "type": "object", "additionalProperties": False, "required": ["k", "t", "s", "i", "v", "m", "q", "n"],
        "properties": {
            "k": {"type": "string"}, "t": {"type": "string", "enum": list(TOPICS)},
            "s": {"type": "string", "enum": ALL_SUBTOPICS}, "i": {"type": "string", "enum": list(INTENTS)},
            "v": {"type": "integer", "enum": [1, 2, 3, 4, 5]}, "m": {"type": "number", "enum": list(tx.SENTIMENT_LEVELS)},
            "q": {"type": "string"}, "n": {"type": "boolean"}}}
    return {"type": "object", "additionalProperties": False, "required": ["r"],
            "properties": {"r": {"type": "array", "items": item}}}


def compact_mock(user_input):
    rows = json.loads(user_input)
    out = mock_fn(json.dumps([{"key": r["k"], "text": r["x"]} for r in rows]))["results"]
    return {"r": [{"k": o["key"], "t": o["topic"], "s": o["subtopic"], "i": o["intent"], "v": o["severity"],
                   "m": o["sentiment"], "q": o["quote"] if r["lq"] else "", "n": False} for o, r in zip(out, rows)]}


class CompactEnricher(Enricher):
    """Same accounting/validation/save path as Enricher; compact keys cut billed output tokens.
    Code supplies: evidence quote for short reviews, entities (explicit term match), severity consistency."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        from . import jev
        self.entities = jev.entities
        self.schema = compact_schema()

    def _send(self, batch, attempt):
        items = [{"k": f"r{i + 1}", "x": u[1], "lq": len(u[1]) > QUOTE_BY_CODE_CHARS} for i, u in enumerate(batch)]
        user = json.dumps(items, ensure_ascii=False)
        est = self.instr_tokens + approx_tokens(user) + 20
        try:
            parsed, call = self.gw.call(
                role="enrich", model=self.ec["model"], effort=self.ec["effort"], instructions=self.instr,
                user_input=user, schema=self.schema, review_ids=[u[0] for u in batch], label_config=self.lc,
                max_output_tokens=self.ec["max_output_tokens"], est_input_tokens=est, mock_fn=compact_mock,
                service_tier=self.ec.get("service_tier"), cache_key=self.ec["prompt"])
            # translate compact keys to the standard item shape so the shared validator/saver is reused
            if isinstance(parsed, dict):
                texts = {f"r{i + 1}": u[1] for i, u in enumerate(batch)}
                results = []
                for r in parsed.get("r", []):
                    if not isinstance(r, dict) or r.get("k") not in texts:
                        results.append({"key": r.get("k") if isinstance(r, dict) else None})
                        continue
                    text = texts[r["k"]]
                    quote = text if len(text) <= QUOTE_BY_CODE_CHARS else r.get("q", "")
                    results.append({"key": r["k"], "topic": r.get("t"), "subtopic": r.get("s"), "intent": r.get("i"),
                                    "severity": tx.consistent_severity(r.get("i"), r.get("v", 1)) if r.get("i") in INTENTS else r.get("v"),
                                    "raw_severity": r.get("v"), "sentiment": r.get("m"), "quote": quote,
                                    "entities": self.entities(text), "needs_review": r.get("n", False)})
                parsed = {"results": results}
            return batch, attempt, parsed, call
        except Exception as e:
            return batch, attempt, e, None


def make_enricher(state, gw, cfg, log, run_dir):  # noqa: F811 (extends the earlier factory)
    provider = cfg["enrich"].get("provider")
    if provider == "jev":
        return JevEnricher(state, gw, cfg, log, run_dir)
    if cfg["enrich"].get("schema") == "s2":
        return CompactEnricher(state, gw, cfg, log, run_dir)
    return Enricher(state, gw, cfg, log, run_dir)
