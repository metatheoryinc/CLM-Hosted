"""Shared, standard-library-only policy for selecting a model for one turn.

The module is copied next to the Codex MCP server, so it deliberately does not
import the :mod:`clm` package or any third-party dependency.  Transport
adapters supply a classifier which accepts ``(state, timeout_seconds)``.
"""
from __future__ import annotations

import datetime
import json
import math
import queue
import re
import threading
import time
import uuid


WORKFLOW = "routing/turn-picks"
POLICY_VERSION = "turn-picks-v1"
QID = "route"
TIERS = ("haiku", "sonnet", "opus")
TIER_RANK = {tier: rank for rank, tier in enumerate(TIERS)}

# These strings and the state shape are the training interface.  Tests compare
# them to clm_hook.py so the standalone installed copy cannot drift silently.
SUBAGENT_INSTRUCTIONS = "Which model is capable enough for this subagent task, at the lowest cost?"
SUBAGENT_OPTIONS = {
    "haiku": "Searches, reads or lists code and files and reports what it finds.",
    "sonnet": "Makes a focused code change, writes tests, or fixes a well-described bug.",
    "opus": "Designs or plans, debugs a hard problem across many files, or makes a judgment call.",
}
MAX_FIELD = 6000
SUBAGENT_MAX_INSTRUCTIONS = 1200

DEFAULT_CONFIG = {
    "turn_pick_threshold": 0.8,
    "turn_pick_top_threshold": 0.8,
    "turn_pick_min_task_chars": 1000,
    "turn_pick_budget_ceiling_percent": 90.0,
    "turn_pick_budget_reserve_percent": 0.0,
    "turn_pick_reset_grace_minutes": 30.0,
    "turn_pick_budget_max_age_seconds": 600.0,
    "turn_pick_timeout_seconds": 1.5,
    "turn_pick_jev_enabled": False,
}

SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?key|secret|token|password|passwd|pwd)[\"']?\s*[:=]\s*[\"']?)"
                r"[^\s\"',;]{4,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
                r"xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"), "[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[REDACTED]"),
]


def redact(text):
    """Remove the same likely-secret forms as the installed hook."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def build_state(task, context=""):
    """Build exactly the state used by the trained subagent tier head."""
    prompt = task + (("\n\nContext:\n" + context) if context else "")
    prompt = redact(str(prompt))
    if len(prompt) > SUBAGENT_MAX_INSTRUCTIONS:
        prompt = prompt[:SUBAGENT_MAX_INSTRUCTIONS] + " …"
    return {"task": "", "instructions": prompt, "subagent type": "general-purpose"}


def question():
    return {QID: {"type": "choice", "instructions": SUBAGENT_INSTRUCTIONS,
                  "criteria": dict(SUBAGENT_OPTIONS)}}


class AuditQueue:
    """A bounded best-effort daemon queue for decision uploads."""

    def __init__(self, sink, maxsize=64):
        if not callable(sink):
            raise TypeError("sink must be callable")
        self._sink = sink
        self._queue = queue.Queue(maxsize=max(1, int(maxsize)))
        self._closed = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="clm-turn-pick-audit", daemon=True)
        self._thread.start()

    def submit(self, record):
        if self._closed:
            self.dropped += 1
            return False
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                self._sink(item)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    def close(self, timeout=0.2):
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(max(0.0, float(timeout)))


def _number(value, name, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("%s must be a finite number%s" % (name, " or null" if allow_none else ""))
    return float(value)


def _config(values):
    cfg = dict(DEFAULT_CONFIG)
    if values:
        cfg.update(values)
    for key in ("turn_pick_threshold", "turn_pick_top_threshold"):
        cfg[key] = _number(cfg[key], key)
        if not 0 < cfg[key] <= 1:
            raise ValueError("%s must be in (0, 1]" % key)
    cfg["turn_pick_min_task_chars"] = _number(cfg["turn_pick_min_task_chars"], "turn_pick_min_task_chars")
    if cfg["turn_pick_min_task_chars"] < 0 or not cfg["turn_pick_min_task_chars"].is_integer():
        raise ValueError("turn_pick_min_task_chars must be a nonnegative integer")
    cfg["turn_pick_min_task_chars"] = int(cfg["turn_pick_min_task_chars"])
    for key in ("turn_pick_budget_ceiling_percent", "turn_pick_budget_reserve_percent"):
        cfg[key] = _number(cfg[key], key)
    if not 0 < cfg["turn_pick_budget_ceiling_percent"] <= 100:
        raise ValueError("turn_pick_budget_ceiling_percent must be in (0, 100]")
    if not 0 <= cfg["turn_pick_budget_reserve_percent"] <= 100:
        raise ValueError("turn_pick_budget_reserve_percent must be in [0, 100]")
    for key in ("turn_pick_reset_grace_minutes", "turn_pick_budget_max_age_seconds",
                "turn_pick_timeout_seconds"):
        cfg[key] = _number(cfg[key], key)
    if cfg["turn_pick_reset_grace_minutes"] < 0:
        raise ValueError("turn_pick_reset_grace_minutes must be nonnegative")
    if cfg["turn_pick_budget_max_age_seconds"] <= 0 or cfg["turn_pick_timeout_seconds"] <= 0:
        raise ValueError("budget max age and timeout must be positive")
    if not isinstance(cfg["turn_pick_jev_enabled"], bool):
        raise ValueError("turn_pick_jev_enabled must be a boolean")
    return cfg


def _window(raw, index):
    if not isinstance(raw, dict):
        raise ValueError("budget window %d must be an object" % index)
    used = raw.get("used_percent")
    if used is not None:
        used = _number(used, "used_percent")
    duration = raw.get("window_minutes")
    if duration is not None:
        duration = _number(duration, "window_minutes")
        if duration <= 0:
            raise ValueError("window_minutes must be positive or null")
    reset = raw.get("resets_at")
    if reset is not None:
        reset = _number(reset, "resets_at")
    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError("budget window name must be a string")
    return {"name": name, "used_percent": used, "window_minutes": duration, "resets_at": reset}


def _budget(raw, received_at):
    if raw is None:
        return {"observed_at": None, "windows": []}
    if not isinstance(raw, dict):
        raise ValueError("budget must be an object or null")
    observed = raw.get("observed_at", received_at)
    if observed is not None:
        observed = _number(observed, "observed_at")
    if "windows" in raw:
        windows = raw["windows"]
        if not isinstance(windows, list):
            raise ValueError("budget windows must be a list")
        normalized = [_window(w, i) for i, w in enumerate(windows)]
    elif any(k in raw for k in ("used_percent", "window_minutes", "resets_at", "name")):
        normalized = [_window(raw, 0)]
    else:
        normalized = []
    return {"observed_at": observed, "windows": normalized}


def _validate_request(request, received_at):
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    task = request.get("task")
    caller = request.get("caller")
    context = request.get("context", "")
    candidates = request.get("candidates")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string")
    if not isinstance(context, str):
        raise ValueError("context must be a string")
    if not isinstance(caller, str) or not caller.strip():
        raise ValueError("caller must be a non-empty string")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidates must be a non-empty list")
    prefer = request.get("prefer")
    if prefer is not None and prefer not in ("codex", "claude"):
        raise ValueError("prefer must be codex or claude")
    requested = request.get("requested_tier")
    if requested is not None and requested not in TIERS:
        raise ValueError("requested_tier must be haiku, sonnet, or opus")
    normalized = []
    identities = set()
    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            raise ValueError("candidate %d must be an object" % index)
        provider, model, effort, tier = (raw.get(k) for k in ("provider", "model", "effort", "tier"))
        if provider not in ("codex", "claude"):
            raise ValueError("candidate provider must be codex or claude")
        if not isinstance(model, str) or not model.strip() or not isinstance(effort, str) or not effort.strip():
            raise ValueError("candidate model and effort must be non-empty strings")
        if tier not in TIERS:
            raise ValueError("candidate tier must be haiku, sonnet, or opus")
        identity = (provider, model, effort, tier)
        if identity in identities:
            raise ValueError("duplicate candidate configuration")
        identities.add(identity)
        normalized.append({"provider": provider, "model": model, "effort": effort, "tier": tier,
                           "budget": _budget(raw.get("budget"), received_at), "order": index})
    if requested is not None and any(c["tier"] != requested for c in normalized):
        raise ValueError("all candidates must match requested_tier")
    return {"task": task, "context": context, "caller": caller, "candidates": normalized,
            "prefer": prefer, "requested_tier": requested}


def _validate_classification(raw):
    if not isinstance(raw, dict) or raw.get("error"):
        raise ValueError("classification failed")
    choice = raw.get("choice")
    probs = raw.get("probabilities")
    if choice not in TIERS or not isinstance(probs, dict) or set(probs) != set(TIERS):
        raise ValueError("classification has an invalid choice or probability map")
    clean = {}
    for tier in TIERS:
        value = _number(probs[tier], "classification probability")
        if not 0 <= value <= 1:
            raise ValueError("classification probability must be in [0, 1]")
        clean[tier] = value
    if abs(sum(clean.values()) - 1.0) > 1e-6:
        raise ValueError("classification probabilities must sum to one")
    maximum = max(clean.values())
    if abs(clean[choice] - maximum) > 1e-9:
        raise ValueError("classification choice must be an argmax")
    probability = _number(raw.get("probability"), "classification probability")
    if abs(probability - clean[choice]) > 1e-6:
        raise ValueError("classification probability does not match the chosen probability")
    # Only retain the documented, non-exception fields in an audit record.
    out = {"choice": choice, "probability": probability, "probabilities": clean}
    for key in ("model", "calibrate"):
        value = raw.get(key)
        if isinstance(value, str):
            out[key] = redact(value)[:200]
        elif value is None:
            out[key] = None
    for key in ("confidence", "latency_ms"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            out[key] = float(value)
    return out


def _tier(classification, cfg, task_length):
    choice, probability = classification["choice"], classification["probability"]
    if choice == "opus" and probability < cfg["turn_pick_top_threshold"]:
        return "sonnet", "CLM leaned opus below the top-tier confidence bar, so sonnet"
    if choice == "haiku" and probability < cfg["turn_pick_threshold"]:
        return "sonnet", "CLM leaned haiku but was unsure, so sonnet"
    if choice == "haiku" and task_length < cfg["turn_pick_min_task_chars"]:
        return "sonnet", "CLM selected haiku; the task is too short for an automatic lookup tier, so sonnet"
    return choice, "CLM: %s (p=%.2f)" % (choice, probability)


def _classify(classifier, jev_classifier, state, cfg):
    enabled_jev = cfg["turn_pick_jev_enabled"] and jev_classifier is not None
    jobs = [("clm", classifier)] + ([("jev", jev_classifier)] if enabled_jev else [])
    results = queue.Queue()
    deadline = time.monotonic() + cfg["turn_pick_timeout_seconds"]

    def run(name, function):
        try:
            value = function(state, max(0.001, deadline - time.monotonic()))
        except Exception:
            value = {"error": "classification failed"}
        try:
            results.put_nowait((name, value))
        except queue.Full:
            pass

    for name, function in jobs:
        threading.Thread(target=run, args=(name, function), name="clm-turn-pick-%s" % name, daemon=True).start()
    found = {}
    while len(found) < len(jobs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            name, value = results.get(timeout=remaining)
        except queue.Empty:
            break
        found[name] = value
    return found


def _evaluate(candidate, cfg, evaluated_at):
    budget = candidate["budget"]
    observed, windows = budget["observed_at"], budget["windows"]
    if not windows:
        return {"eligible": True, "group": "unknown", "headroom": None,
                "unknown": True, "grace": False, "exclusion": None}
    stale = observed is None or observed > evaluated_at or evaluated_at - observed > cfg["turn_pick_budget_max_age_seconds"]
    unknown = bool(stale)
    grace = False
    headrooms = []
    exclusion = None
    for window in windows:
        used, reset = window["used_percent"], window["resets_at"]
        known = used is not None and 0 <= used <= 100 and not stale and not (reset is not None and reset <= evaluated_at)
        if not known:
            unknown = True
            continue
        headrooms.append(100.0 - used)
        if used >= 100:
            exclusion = "fully exhausted budget window"
            break
        fits = used + cfg["turn_pick_budget_reserve_percent"] < cfg["turn_pick_budget_ceiling_percent"]
        within_grace = reset is not None and evaluated_at < reset <= (
            evaluated_at + cfg["turn_pick_reset_grace_minutes"] * 60.0)
        if not fits and within_grace:
            grace = True
        elif not fits:
            exclusion = "budget guard"
            break
    if exclusion:
        return {"eligible": False, "group": "excluded", "headroom": min(headrooms) if headrooms else None,
                "unknown": unknown, "grace": grace, "exclusion": exclusion}
    group = "unknown" if unknown else ("grace" if grace else "known")
    return {"eligible": True, "group": group, "headroom": min(headrooms) if headrooms else None,
            "unknown": unknown, "grace": grace, "exclusion": None}


def _choose(candidates, tier, prefer, cfg, evaluated_at):
    considered = []
    for candidate in candidates:
        if candidate["tier"] != tier:
            continue
        assessment = _evaluate(candidate, cfg, evaluated_at)
        considered.append((candidate, assessment))
    eligible = [(candidate, assessment) for candidate, assessment in considered if assessment["eligible"]]
    if not eligible:
        return None, None, considered
    group_rank = {"known": 0, "unknown": 1, "grace": 2}

    def rank(item):
        candidate, assessment = item
        score = assessment["headroom"]
        return (group_rank[assessment["group"]], -(score if score is not None else -1.0),
                0 if prefer and candidate["provider"] == prefer else 1, candidate["order"])

    candidate, assessment = min(eligible, key=rank)
    return candidate, assessment, considered


def _identity(candidate):
    return {key: candidate[key] for key in ("provider", "model", "effort", "tier")}


def _audit_identity(candidate):
    return {"provider": candidate["provider"], "model": redact(candidate["model"])[:500],
            "effort": redact(candidate["effort"])[:500], "tier": candidate["tier"]}


def _audit_budget(budget):
    return {"observed_at": budget["observed_at"], "windows": [
        {**window, "name": redact(window["name"])[:500] if window["name"] is not None else None}
        for window in budget["windows"]]}


def _iso(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat(timespec="milliseconds")


def _audit(validated, cfg, state, clm, jev, capability_tier, selected, why, fallback,
           evaluated_at, assessments, acted):
    candidates = []
    for candidate in validated["candidates"]:
        candidates.append({**_audit_identity(candidate), "budget": _audit_budget(candidate["budget"]),
                           "order": candidate["order"]})
    safe_assessments = []
    for candidate, assessment in assessments:
        safe_assessments.append({"candidate": _audit_identity(candidate), **assessment})
    meta = {
        "policy_version": POLICY_VERSION,
        "caller": redact(validated["caller"])[:500],
        "candidates": candidates,
        "default": _audit_identity(validated["candidates"][0]),
        "prefer": validated["prefer"],
        "requested_tier": validated["requested_tier"],
        "capability_tier": capability_tier,
        "selected": _audit_identity(selected),
        "why": why,
        "fallback": fallback,
        "evaluated_at": evaluated_at,
        "policy": {key: cfg[key] for key in DEFAULT_CONFIG},
        "assessments": safe_assessments,
    }
    if jev is not None:
        meta["jev"] = jev
    return {"id": uuid.uuid4().hex, "workflow": WORKFLOW, "created_at": _iso(evaluated_at),
            "mode": "active", "state": state, "questions": question(), "clm": clm or {"error": "unavailable"},
            "acted": acted, "worker": selected["tier"], "meta": meta}


def pick_turn(request, classifier, *, config=None, now=None, jev_classifier=None, sink=None):
    """Select one supplied candidate using capability first, then budget.

    ``classifier`` and ``jev_classifier`` receive ``(state, timeout_seconds)``.
    ``sink`` receives a sanitized replay record; adapters should pass
    :meth:`AuditQueue.submit` so upload latency stays off the selection path.
    Invalid requests raise :class:`ValueError`; policy, configuration, and
    classifier failures return the exact first candidate with ``fallback``.
    """
    evaluated_at = float(now() if callable(now) else (time.time() if now is None else now))
    if not math.isfinite(evaluated_at):
        raise ValueError("now must be a finite timestamp")
    validated = _validate_request(request, evaluated_at)
    default = validated["candidates"][0]
    state = build_state(validated["task"], validated["context"])
    cfg = None
    clm = jev = None
    assessments = []
    capability_tier = validated["requested_tier"]
    acted = "manual" if capability_tier else "clm"
    probabilities = None
    fallback = False
    try:
        cfg = _config(config)
    except Exception:
        why = "Policy configuration is invalid; using the caller default"
        fallback, selected, acted = True, default, "baseline"
    else:
        if capability_tier:
            why = "Manual %s tier request" % capability_tier
        else:
            raw = _classify(classifier, jev_classifier, state, cfg)
            try:
                clm = _validate_classification(raw.get("clm"))
            except Exception:
                why = "CLM classification failed; using the caller default"
                fallback, selected, acted = True, default, "baseline"
            else:
                probabilities = dict(clm["probabilities"])
                capability_tier, why = _tier(clm, cfg, len(validated["task"]))
                if cfg["turn_pick_jev_enabled"] and jev_classifier is not None:
                    try:
                        jev = _validate_classification(raw.get("jev"))
                    except Exception:
                        jev = None
                    if jev is not None:
                        jev_tier, _ = _tier(jev, cfg, len(validated["task"]))
                        if TIER_RANK[jev_tier] > TIER_RANK[capability_tier]:
                            capability_tier = jev_tier
                            why += "; Jev conservatively raised the capability tier to %s" % jev_tier
        if not fallback:
            selected, assessment, considered = _choose(validated["candidates"], capability_tier,
                                                        validated["prefer"], cfg, evaluated_at)
            assessments.extend(considered)
            dropped = False
            if selected is None and not validated["requested_tier"] and TIER_RANK[capability_tier] > 0:
                lower = TIERS[TIER_RANK[capability_tier] - 1]
                selected, assessment, considered = _choose(validated["candidates"], lower,
                                                            validated["prefer"], cfg, evaluated_at)
                assessments.extend(considered)
                if selected is not None:
                    dropped = True
                    why += "; no eligible %s capacity, so dropped exactly one tier to %s" % (capability_tier, lower)
            if selected is None:
                selected, fallback, acted = default, True, "baseline"
                why += "; no eligible capacity, using the caller default"
            elif not dropped:
                excluded = any(not a["eligible"] for _, a in considered)
                if assessment["group"] == "unknown":
                    why += "; selected capacity is unknown"
                elif assessment["group"] == "grace":
                    why += "; selected by the reset grace rule"
                elif excluded:
                    why += "; other candidates were excluded by the budget guard"
                elif len([a for _, a in considered if a["eligible"]]) > 1:
                    why += "; selected the greater measured budget headroom, with preference and order breaking ties"
    if cfg is not None:
        assessments = [(candidate, _evaluate(candidate, cfg, evaluated_at))
                       for candidate in validated["candidates"]]
    response = {**_identity(selected), "why": why, "fallback": fallback}
    if probabilities is not None:
        response["probabilities"] = probabilities
    if sink is not None:
        try:
            record_cfg = cfg if cfg is not None else dict(DEFAULT_CONFIG)
            sink(_audit(validated, record_cfg, state, clm, jev, capability_tier, selected, why, fallback,
                        evaluated_at, assessments, acted))
        except Exception:
            pass
    return response
