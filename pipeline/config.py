"""Versioned run configuration. Changing any value that affects labels changes label_config,
which invalidates exact-text cache reuse (results are reused only under identical settings)."""
import copy

ENRICHERS = {
    # Jev: fixed-choice questions, one review per request; code maps probabilities to the schema.
    "jev": {"provider": "jev", "model": "jev-1.13.0", "effort": None, "prompt": "jev_q2", "schema": "s1", "taxonomy": "t1",
            "batch_size": 1, "max_output_tokens": 0,
            "thresholds": {"topic": 0.5, "intent": 0.5, "severity": 0.4, "subtopic_mass": 0.3, "injection": 0.5}},
    # Final enricher: compact batched LLM. 50 reviews/request (instructions amortized), stable instruction prefix
    # for provider prompt caching, Flex tier (50% price, queued), exact-text result cache, parallel workers.
    "luna2": {"provider": "openai", "model": "gpt-6-luna", "effort": "none", "prompt": "enrich_v2", "schema": "s2",
              "taxonomy": "t1", "batch_size": 50, "max_output_tokens": 4000, "service_tier": "flex"},
    # Small LLM alternative v1 (kept for comparison): up to 50 reviews per request with a JSON schema.
    "luna": {"provider": "openai", "model": "gpt-6-luna", "effort": "none", "prompt": "enrich_v1", "schema": "s1",
             "taxonomy": "t1", "batch_size": 50, "max_output_tokens": 6000},
}

DEFAULTS = {
    "enrich": dict(ENRICHERS["luna2"]),
    "fallback": {"model": "gpt-5-mini", "effort": "low", "max_fraction": 0.002, "max_output_tokens": 1500},
    # Verifier = a different provider (Jev choice probabilities) re-labeling a seeded sample from text + rubric only.
    "verify": {"provider": "jev", "model": "jev-1.13.0", "effort": None, "prompt": "jev_verify_q1", "seed": "verify-2026-10",
               "sample_fraction": 0.004, "min_sample": 20, "max_sample": 2000, "batch_size": 1, "workers": 4,
               "max_output_tokens": 0},
    "group": {"model": "gpt-5-mini", "effort": "low", "prompt": "group_v1", "examples_per_issue": 12,
              "issues_per_call": 8, "max_output_tokens": 6000},
    "memo": {"model": "gpt-5-mini", "effort": "medium", "prompt": "memo_v3", "top_issues": 40,
             "quotes_per_issue": 3, "max_output_tokens": 12000},
    "limits": {"spend_cap_usd": 10.0, "workers": 1, "max_attempts": 6, "consecutive_failure_stop": 5},
}


def build(overrides=None, enricher="luna2"):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["enrich"] = copy.deepcopy(ENRICHERS[enricher])
    for path, value in (overrides or {}).items():
        section, key = path.split(".")
        cfg[section][key] = value
    return cfg


def label_config(cfg, role="enrich"):
    e = cfg["enrich"]
    if role == "fallback":  # fallback always uses the LLM prompt with the fallback model
        c = cfg["fallback"]
        return f"{c['model']}|effort={c['effort']}|prompt=enrich_v1+issue|schema=s1i|taxonomy={e['taxonomy']}|fallback"
    extra = ""
    if e.get("thresholds"):
        extra = "|thr=" + ",".join(f"{k}:{v}" for k, v in sorted(e["thresholds"].items()))
    return f"{e['model']}|effort={e['effort']}|prompt={e['prompt']}|schema={e['schema']}|taxonomy={e['taxonomy']}{extra}"
