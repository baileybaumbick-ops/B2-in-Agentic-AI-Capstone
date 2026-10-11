"""Model gateway shared by every role.

Responsibilities (code, not the model): model allowlist, spend ledger with reservations,
bounded retries with backoff + jitter, output-token caps, and a per-attempt call log.
A deterministic offline "mock" provider exists only for development without an API key;
its label_config contains "mock" and its outputs are never used as evidence.
"""
import csv
import json
import random
import re
import threading
import time
import uuid
from pathlib import Path

from .common import ROOT, append_jsonl, now_iso

RATES_PATH = ROOT / "cost" / "rates.csv"
ALLOWED_MODELS = {"gpt-6-luna", "gpt-5-mini", "jev-1.13.0", "mock-keyword-v0"}
RETRYABLE = ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError",
             "TypeSafeRateLimitError", "TypeSafeAPITimeoutError", "TypeSafeAPIConnectionError", "TypeSafeInternalServerError")


class BudgetExceeded(Exception):
    pass


class ModelOutputError(Exception):
    """Response arrived but was unusable (incomplete, refused, invalid JSON)."""


def load_rates(path=RATES_PATH, tier="standard"):
    rates = {}
    with Path(path).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["tier"] == tier:
                rates.setdefault(row["model"], {})[row["item"]] = float(row["price_usd"]) / float(row["per_units"])
    return rates


def usage_cost(model, usage, rates):
    """Mutually exclusive items: uncached input, cached input, cache writes, output (reasoning is inside output)."""
    r = rates.get(model)
    if r is None or usage is None:
        return None
    cached = usage.get("cached_tokens", 0)
    writes = usage.get("cache_write_tokens", 0)
    uncached = usage["input_tokens"] - cached - writes
    return (uncached * r["input_uncached"] + cached * r["input_cached"]
            + writes * r.get("cache_write", r["input_uncached"]) + usage["output_tokens"] * r["output"])


class Ledger:
    """One spend ledger shared by all workers. Reserve worst case before dispatch."""

    def __init__(self, cap_usd, spent_usd=0.0):
        self.cap = cap_usd
        self.spent = spent_usd
        self.reserved = 0.0
        self.lock = threading.Lock()

    def reserve(self, amount):
        with self.lock:
            if self.spent + self.reserved + amount > self.cap:
                raise BudgetExceeded(f"spent {self.spent:.4f} + reserved {self.reserved:.4f} + next {amount:.4f} > cap {self.cap:.2f}")
            self.reserved += amount
            return amount

    def settle(self, reserved, actual):
        with self.lock:
            self.reserved -= reserved
            self.spent += actual if actual is not None else reserved  # unknown usage: keep worst case


class Gateway:
    def __init__(self, run_dir, ledger, rates, phase="initial", mock=False, faults=None, max_attempts=4):
        self.run_dir = Path(run_dir)
        self.calls_path = self.run_dir / "calls.jsonl"
        self.ledger = ledger
        self.rates = rates  # standard tier: used for worst-case reservations
        self.flex_rates = load_rates(tier="flex")
        self.phase = phase
        self.mock = mock
        self.max_attempts = max_attempts
        self.faults = dict(faults or {})  # e.g. {"rate_limit": 1, "malformed": 1}; injected, logged as such
        self.fault_lock = threading.Lock()
        self.log_lock = threading.Lock()  # one writer at a time: concurrent appends can lose lines
        self.client = None
        self._local = threading.local()
        if not mock:
            from openai import OpenAI
            from .common import get_secret
            # retries are ours; Flex requests can queue for minutes, so allow a long timeout
            self.client = OpenAI(api_key=get_secret("OPENAI_API_KEY"), max_retries=0, timeout=900)
            get_secret("TYPESAFE_API_KEY")

    def _cost_rates(self, record):
        """Bill each attempt at the tier the provider reports it actually used."""
        return self.flex_rates if record.get("service_tier") == "flex" else self.rates

    def _jev(self):
        """One TypeSafe client per worker thread; SDK retries off so every attempt is logged here."""
        if getattr(self._local, "jev", None) is None:
            from typesafe_sdk import RetryPolicy, TypeSafeClient
            self._local.jev = TypeSafeClient(retry=RetryPolicy(max_retries=0), timeout=60)
        return self._local.jev

    def _take_fault(self, kind):
        with self.fault_lock:
            if self.faults.get(kind, 0) > 0:
                self.faults[kind] -= 1
                return True
        return False

    def call(self, *, role, model, instructions, user_input, schema, review_ids, label_config,
             max_output_tokens, effort="none", est_input_tokens=None, artifacts=None, mock_fn=None, questions=None,
             service_tier=None, cache_key=None):
        """Returns (parsed_json, call_record). Raises BudgetExceeded / ModelOutputError / last API error."""
        if model not in ALLOWED_MODELS:
            raise ValueError(f"model {model} not in allowlist")
        if self.mock:
            model = "mock-keyword-v0"
        est_in = est_input_tokens or (len(instructions) + len(user_input)) // 3
        r = self.rates.get(model, {"input_uncached": 0, "output": 0})
        worst = est_in * r["input_uncached"] + max_output_tokens * r["output"]
        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            reserved = self.ledger.reserve(worst)
            record = {"request_id": f"local-{uuid.uuid4().hex[:16]}", "role": role, "review_ids": list(review_ids),
                      "model": model, "effort": effort, "phase": self.phase, "label_config": label_config,
                      "attempt": attempt, "started_at": now_iso(), "max_output_tokens": max_output_tokens,
                      "artifacts": artifacts or [], "injected_fault": None, "service_tier": None}
            t0 = time.perf_counter()
            usage, actual = None, None
            try:
                if len(review_ids) == 1 and self._take_fault("policy_single"):
                    record["injected_fault"] = "policy_refusal"
                    raise InjectedBadRequestError("injected 400: prompt flagged as potentially violating usage policy")
                if len(review_ids) > 1 and self._take_fault("policy"):
                    record["injected_fault"] = "policy_refusal"
                    raise InjectedBadRequestError("injected 400: prompt flagged as potentially violating usage policy")
                if self._take_fault("rate_limit"):
                    record["injected_fault"] = "rate_limit"
                    raise _InjectedRateLimit("injected 429 for retry-control test")
                if self.mock:
                    text = json.dumps(mock_fn(user_input)) if mock_fn else "{}"
                    usage = {"input_tokens": est_in, "output_tokens": len(text) // 3, "cached_tokens": 0,
                             "cache_write_tokens": 0, "reasoning_tokens": 0}
                    record["provider_response_id"] = None
                elif model.startswith("jev"):
                    resp = self._jev().system_one(state=user_input, questions=questions, model=model)
                    record["provider_response_id"] = None  # System One responses carry no request id
                    record["provider_model_version"] = resp.model
                    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
                             "cached_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}
                    text = json.dumps({k: a.model_dump(mode="json") for k, a in resp.answers.items()})
                else:
                    kwargs = dict(model=model, instructions=instructions, input=user_input,
                                  max_output_tokens=max_output_tokens, store=False,
                                  text={"format": {"type": "json_schema", "name": f"{role}_result", "schema": schema, "strict": True}})
                    if effort:
                        kwargs["reasoning"] = {"effort": effort}
                    if service_tier:
                        kwargs["service_tier"] = service_tier
                    if cache_key:
                        kwargs["prompt_cache_key"] = cache_key
                    resp = self.client.responses.create(**kwargs)
                    record["provider_response_id"] = resp.id
                    record["service_tier"] = resp.service_tier
                    u = resp.usage
                    usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                             "cached_tokens": getattr(u.input_tokens_details, "cached_tokens", 0) or 0,
                             "cache_write_tokens": getattr(u.input_tokens_details, "cache_write_tokens", 0) or 0,
                             "reasoning_tokens": getattr(u.output_tokens_details, "reasoning_tokens", 0) or 0}
                    if resp.status != "completed":
                        raise ModelOutputError(f"status={resp.status} details={resp.incomplete_details}")
                    text = resp.output_text
                if self._take_fault("malformed"):
                    record["injected_fault"] = "malformed"
                    text = text[: len(text) // 2]
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as e:
                    raise ModelOutputError(f"invalid JSON: {e}") from e
                actual = usage_cost(model, usage, self._cost_rates(record))
                record.update(outcome="succeeded", error=None)
                return parsed, record
            except ModelOutputError as e:
                actual = usage_cost(model, usage, self._cost_rates(record)) if usage else None
                record.update(outcome="failed", error=f"ModelOutputError: {e}")
                raise
            except Exception as e:  # API/transport errors
                name = type(e).__name__
                record.update(outcome="failed", error=f"{name}: {str(e)[:300]}")
                last_error = e
                if not (name in RETRYABLE or isinstance(e, _InjectedRateLimit)) or attempt == self.max_attempts:
                    raise
            finally:
                record["latency_s"] = round(time.perf_counter() - t0, 3)
                record["usage_available"] = usage is not None
                for k in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens"):
                    record[k] = (usage or {}).get(k, 0)
                record["cost_usd"] = round(actual, 8) if actual is not None else None
                # No usage on a transport failure: keep the reservation as spent (unknown charge). Exceptions: injected
                # test faults and 429 rejections (the provider refuses the request before processing; not billed).
                rejected = (record.get("error") or "").split(":")[0] in ("RateLimitError", "TypeSafeRateLimitError")
                if not usage and rejected:
                    record["cost_usd"] = 0.0
                    record["cost_note"] = "429 rejected before processing; not billed"
                self.ledger.settle(reserved, actual if usage else (0.0 if record.get("injected_fault") or rejected else None))
                with self.log_lock:
                    append_jsonl(self.calls_path, record)
            time.sleep(min(30, 2 ** attempt) * (0.5 + random.random()))  # backoff with jitter before next attempt
        raise last_error


class _InjectedRateLimit(Exception):
    pass


class InjectedBadRequestError(Exception):
    """Test-only stand-in for a provider content-policy refusal (HTTP 400 'flagged')."""


def approx_tokens(text):
    return max(1, len(re.findall(r"\w+|[^\w\s]", text)))
