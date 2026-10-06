"""Shared capability and budget policy for per-turn model selection."""
import importlib.util
import math
import os
import time

import pytest

from clm.turn_picker import (
    AuditQueue,
    SUBAGENT_INSTRUCTIONS,
    SUBAGENT_OPTIONS,
    build_state,
    pick_turn,
)


NOW = 1_791_300_000.0
LONG_TASK = "Review the migration, trace its callers, and identify correctness risks. " * 20


def candidate(provider, tier="sonnet", used=None, *, reset=NOW + 3600, observed=NOW,
              model=None, effort="medium", windows=None):
    out = {"provider": provider, "model": model or f"{provider}-{tier}", "effort": effort, "tier": tier}
    if windows is not None:
        out["budget"] = {"observed_at": observed, "windows": windows}
    elif used is not ...:
        out["budget"] = {"used_percent": used, "window_minutes": 300, "resets_at": reset,
                         "observed_at": observed}
    return out


def request(*candidates, task=LONG_TASK, **extra):
    return {"task": task, "caller": "test", "candidates": list(candidates), **extra}


def result(choice, probability=0.9, probabilities=None):
    probs = probabilities or {"haiku": 0.05, "sonnet": 0.05, "opus": 0.9}
    return {"choice": choice, "probability": probability, "probabilities": probs,
            "model": "subagent-tier-v2", "latency_ms": 2.0}


def classifier(answer):
    def classify(state, timeout):
        classify.calls.append((state, timeout))
        return answer
    classify.calls = []
    return classify


def test_rubric_and_state_are_exactly_the_trained_hook_values():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "integrations", "claude_code", "clm_hook.py")
    spec = importlib.util.spec_from_file_location("turn_picker_hook", path)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    task = "token=abcdefghijklmnop " + "x" * 1600
    context = "prior API key: abcdefghijklmnop"
    expected = hook.subagent_state({"tool_input": {"description": "", "prompt": task + "\n\nContext:\n" + context,
                                                     "subagent_type": "general-purpose"}})
    assert (SUBAGENT_INSTRUCTIONS, SUBAGENT_OPTIONS) == (hook.SUBAGENT_INSTRUCTIONS, hook.SUBAGENT_OPTIONS)
    assert build_state(task, context) == expected


@pytest.mark.parametrize("choice, probability, expected", [
    ("opus", 0.8, "opus"),
    ("opus", 0.799999, "sonnet"),
    ("haiku", 0.8, "haiku"),
    ("haiku", 0.799999, "sonnet"),
    ("sonnet", 0.4, "sonnet"),
])
def test_capability_threshold_boundaries(choice, probability, expected):
    probs = {"haiku": 0.1, "sonnet": 0.1, "opus": 0.8}
    probs[choice] = probability
    remaining = 1.0 - probability
    for tier in probs:
        if tier != choice:
            probs[tier] = remaining / 2
    c = classifier(result(choice, probability, probs))
    candidates = [candidate("codex", tier, 10) for tier in ("haiku", "sonnet", "opus")]
    got = pick_turn(request(*candidates), c, now=lambda: NOW)
    assert got["tier"] == expected and got["fallback"] is False
    assert got["probabilities"] == probs


def test_short_task_guard_uses_task_length_not_context_length():
    c = classifier(result("haiku", 0.95, {"haiku": 0.95, "sonnet": 0.04, "opus": 0.01}))
    got = pick_turn(request(candidate("codex", "haiku", 10), candidate("claude", "sonnet", 10),
                            task="list files", context="x" * 3000), c, now=lambda: NOW)
    assert got["tier"] == "sonnet"
    assert "too short" in got["why"]


def test_only_task_and_context_enter_the_classifier_state():
    c = classifier(result("sonnet", 0.9, {"haiku": 0.05, "sonnet": 0.9, "opus": 0.05}))
    req = request(candidate("codex", "sonnet", 10, model="SECRET-MODEL"), context="prior result",
                  prefer="claude")
    pick_turn(req, c, now=lambda: NOW)
    state = c.calls[0][0]
    assert state == build_state(LONG_TASK, "prior result")
    assert "SECRET-MODEL" not in repr(state) and "claude" not in repr(state)


def test_manual_tier_is_strict_skips_classifier_and_has_no_probabilities():
    c = classifier(AssertionError("must not run"))
    got = pick_turn(request(candidate("codex", "haiku", 95), candidate("claude", "haiku", 10),
                            requested_tier="haiku"), c, now=lambda: NOW)
    assert got["provider"] == "claude" and got["tier"] == "haiku"
    assert "probabilities" not in got and got["fallback"] is False
    assert not c.calls and "manual" in got["why"].lower()


def test_manual_tier_rejects_a_mixed_menu():
    with pytest.raises(ValueError, match="requested_tier"):
        pick_turn(request(candidate("codex", "haiku", 10), candidate("claude", "sonnet", 10),
                          requested_tier="haiku"), classifier({}), now=lambda: NOW)


def test_every_window_must_fit_and_minimum_headroom_wins():
    codex = candidate("codex", windows=[
        {"name": "primary", "used_percent": 20, "window_minutes": 300, "resets_at": NOW + 4000},
        {"name": "weekly", "used_percent": 91, "window_minutes": 10080, "resets_at": NOW + 9000},
    ])
    claude = candidate("claude", windows=[
        {"name": "five_hour", "used_percent": 40, "window_minutes": 300, "resets_at": NOW + 4000},
        {"name": "weekly", "used_percent": 50, "window_minutes": 10080, "resets_at": NOW + 9000},
    ])
    c = classifier(result("sonnet", 0.9, {"haiku": 0.05, "sonnet": 0.9, "opus": 0.05}))
    got = pick_turn(request(codex, claude), c, now=lambda: NOW)
    assert got["provider"] == "claude"
    assert "excluded" in got["why"]
    assert "91" not in got["why"] and "50" not in got["why"]


@pytest.mark.parametrize("used, reset, competing, selected, fallback", [
    (89.999, NOW + 7200, 100, "codex", False),
    (90.0, NOW + 7200, 100, "codex", True),
    (90.0, NOW + 1800, 100, "codex", False),
    (99.999, NOW + 1799, 100, "codex", False),
    (100.0, NOW + 1, 50, "claude", False),
])
def test_guard_grace_and_full_exhaustion_boundaries(used, reset, competing, selected, fallback):
    c = classifier(result("sonnet", 0.9, {"haiku": 0.05, "sonnet": 0.9, "opus": 0.05}))
    got = pick_turn(request(candidate("codex", used=used, reset=reset), candidate("claude", used=competing)),
                    c, now=lambda: NOW)
    assert got["provider"] == selected and got["fallback"] is fallback


def test_reserve_is_added_before_the_strict_guard():
    cfg = {"turn_pick_budget_reserve_percent": 5}
    c = classifier(result("sonnet", 0.9, {"haiku": 0.05, "sonnet": 0.9, "opus": 0.05}))
    got = pick_turn(request(candidate("codex", used=85), candidate("claude", used=84.999)), c,
                    config=cfg, now=lambda: NOW)
    assert got["provider"] == "claude"


@pytest.mark.parametrize("budget", [
    None,
    {"windows": []},
    {"used_percent": None, "window_minutes": None, "resets_at": None},
    {"used_percent": -1, "window_minutes": 300, "resets_at": NOW + 1000},
    {"used_percent": 101, "window_minutes": 300, "resets_at": NOW + 1000},
    {"used_percent": 10, "window_minutes": 300, "resets_at": NOW - 1},
    {"used_percent": 10, "window_minutes": 300, "resets_at": NOW + 1000, "observed_at": NOW - 601},
    {"used_percent": 10, "window_minutes": 300, "resets_at": NOW + 1000, "observed_at": NOW + 1},
])
def test_null_out_of_range_stale_future_and_expired_budgets_are_unknown(budget):
    unknown = candidate("codex", used=...)
    if budget is not None:
        unknown["budget"] = budget
    known = candidate("claude", used=89)
    c = classifier(result("sonnet", 0.9, {"haiku": 0.05, "sonnet": 0.9, "opus": 0.05}))
    got = pick_turn(request(unknown, known), c, now=lambda: NOW)
    assert got["provider"] == "claude"  # known room outranks unknown, regardless of numeric appearance


def test_unknown_outranks_grace_and_known_blocking_is_not_erased_by_unknown():
    grace = candidate("codex", used=95, reset=NOW + 60)
    unknown = candidate("claude", used=...)
    c = classifier(result("sonnet", 0.9, {"haiku": 0.05, "sonnet": 0.9, "opus": 0.05}))
    assert pick_turn(request(grace, unknown), c, now=lambda: NOW)["provider"] == "claude"
    partial = candidate("codex", windows=[
        {"used_percent": None, "window_minutes": None, "resets_at": None},
        {"used_percent": 95, "window_minutes": 300, "resets_at": NOW + 4000},
    ])
    assert pick_turn(request(partial, unknown), c, now=lambda: NOW)["provider"] == "claude"


def test_preference_only_breaks_ties_and_order_breaks_remaining_ties():
    c = classifier(result("sonnet", 0.9, {"haiku": 0.05, "sonnet": 0.9, "opus": 0.05}))
    codex, claude = candidate("codex", used=40), candidate("claude", used=40)
    tied = pick_turn(request(codex, claude, prefer="claude"), c, now=lambda: NOW)
    assert tied["provider"] == "claude" and "tie" in tied["why"] and "greater" not in tied["why"]
    assert pick_turn(request(codex, claude), c, now=lambda: NOW)["provider"] == "codex"
    # Preference cannot beat greater measured headroom.
    assert pick_turn(request(candidate("codex", used=40), candidate("claude", used=41), prefer="claude"),
                     c, now=lambda: NOW)["provider"] == "codex"


def test_exactly_one_automatic_tier_drop_and_never_an_upgrade():
    c = classifier(result("opus", 0.9, {"haiku": 0.05, "sonnet": 0.05, "opus": 0.9}))
    got = pick_turn(request(candidate("codex", "opus", 95), candidate("claude", "sonnet", 20),
                            candidate("codex", "haiku", 10)), c, now=lambda: NOW)
    assert got["tier"] == "sonnet" and "one tier" in got["why"]
    got = pick_turn(request(candidate("codex", "opus", 95), candidate("claude", "sonnet", 95),
                            candidate("codex", "haiku", 10)), c, now=lambda: NOW)
    assert got["tier"] == "opus" and got["fallback"] is True
    c2 = classifier(result("haiku", 0.9, {"haiku": 0.9, "sonnet": 0.05, "opus": 0.05}))
    got = pick_turn(request(candidate("codex", "haiku", 95), candidate("claude", "sonnet", 10)), c2,
                    config={"turn_pick_min_task_chars": 1}, now=lambda: NOW)
    assert got["tier"] == "haiku" and got["fallback"] is True


@pytest.mark.parametrize("bad", [
    {},
    {"task": " ", "caller": "x", "candidates": [{}]},
    {"task": "x", "caller": "", "candidates": [{}]},
    {"task": "x", "caller": "x", "candidates": []},
    request(candidate("other")),
    request({"provider": "codex", "model": "m", "effort": "e", "tier": "middle"}),
    request(candidate("codex"), candidate("codex")),
    request(candidate("codex"), prefer="other"),
    request(candidate("codex", used=...)) | {"context": 4},
])
def test_request_validation_errors(bad):
    with pytest.raises(ValueError):
        pick_turn(bad, classifier({}), now=lambda: NOW)


@pytest.mark.parametrize("budget", [
    {"used_percent": "10"},
    {"used_percent": 10, "window_minutes": 0},
    {"used_percent": 10, "resets_at": "soon"},
    {"observed_at": "now", "windows": []},
    {"windows": "all"},
])
def test_budget_wrong_types_and_invalid_durations_are_validation_errors(budget):
    c = candidate("codex", used=...)
    c["budget"] = budget
    with pytest.raises(ValueError):
        pick_turn(request(c), classifier({}), now=lambda: NOW)


@pytest.mark.parametrize("classification", [
    {"error": "secret token abcdefghijklmnop"},
    result("sonnet", math.nan, {"haiku": 0.05, "sonnet": math.nan, "opus": 0.95}),
    result("sonnet", 0.8, {"haiku": 0.1, "sonnet": 0.8, "opus": 0.8}),
    result("other", 1.0, {"haiku": 0.0, "sonnet": 0.0, "opus": 1.0}),
])
def test_classification_failure_returns_exact_first_candidate_without_leaking_errors(classification):
    first = candidate("codex", "haiku", 100, model="exact-default", effort="low")
    got = pick_turn(request(first, candidate("claude", "sonnet", 0)), classifier(classification), now=lambda: NOW)
    assert {k: got[k] for k in ("provider", "model", "effort", "tier")} == {
        "provider": "codex", "model": "exact-default", "effort": "low", "tier": "haiku"}
    assert got["fallback"] is True and "probabilities" not in got
    assert "abcdef" not in got["why"] and "classification" in got["why"].lower()


@pytest.mark.parametrize("config", [
    {"turn_pick_threshold": 0}, {"turn_pick_top_threshold": 1.1},
    {"turn_pick_min_task_chars": -1}, {"turn_pick_budget_ceiling_percent": math.inf},
    {"turn_pick_budget_reserve_percent": -1}, {"turn_pick_reset_grace_minutes": -1},
    {"turn_pick_budget_max_age_seconds": 0}, {"turn_pick_timeout_seconds": 0},
])
def test_bad_policy_config_falls_back_to_exact_first_candidate(config):
    first = candidate("codex", model="default")
    got = pick_turn(request(first, candidate("claude")), classifier({}), config=config, now=lambda: NOW)
    assert got["model"] == "default" and got["fallback"] is True and "configuration" in got["why"]


def test_jev_can_only_raise_tier_and_failure_keeps_clm_success():
    clm = classifier(result("haiku", 0.9, {"haiku": 0.9, "sonnet": 0.08, "opus": 0.02}))
    jev = classifier(result("opus", 0.9, {"haiku": 0.02, "sonnet": 0.08, "opus": 0.9}))
    menu = [candidate("codex", tier, 10) for tier in ("haiku", "sonnet", "opus")]
    cfg = {"turn_pick_jev_enabled": True, "turn_pick_min_task_chars": 1}
    got = pick_turn(request(*menu), clm, config=cfg, jev_classifier=jev, now=lambda: NOW)
    assert got["tier"] == "opus" and got["probabilities"]["haiku"] == 0.9
    broken = classifier({"error": "offline"})
    got = pick_turn(request(*menu), clm, config=cfg, jev_classifier=broken, now=lambda: NOW)
    assert got["tier"] == "haiku" and got["fallback"] is False


def test_classifiers_share_one_deadline_and_late_results_are_abandoned():
    def slow(_state, _timeout):
        time.sleep(0.2)
        return result("opus", 0.9)
    cfg = {"turn_pick_timeout_seconds": 0.02, "turn_pick_jev_enabled": True}
    started = time.monotonic()
    got = pick_turn(request(candidate("codex")), slow, config=cfg, jev_classifier=slow, now=lambda: NOW)
    assert time.monotonic() - started < 0.15
    assert got["fallback"] is True


def test_audit_contains_replay_data_is_redacted_and_sink_failure_does_not_change_pick():
    records = []
    c = classifier(result("sonnet", 0.9, {"haiku": 0.05, "sonnet": 0.9, "opus": 0.05}))
    req = request(candidate("codex", used=12), candidate("claude", "opus", 30),
                  task="password=hunter2hunter2 " + LONG_TASK)
    got = pick_turn(req, c, now=lambda: NOW, sink=records.append)
    rec = records[0]
    assert rec["workflow"] == "routing/turn-picks" and rec["meta"]["policy_version"] == "turn-picks-v1"
    assert rec["meta"]["candidates"][0]["budget"]["windows"][0]["used_percent"] == 12
    assert len(rec["meta"]["assessments"]) == len(req["candidates"])
    assert "hunter2" not in repr(rec) and rec["worker"] == got["tier"]
    got2 = pick_turn(req, c, now=lambda: NOW, sink=lambda _record: (_ for _ in ()).throw(RuntimeError("secret")))
    assert got2 == got


def test_audit_sanitizes_candidate_and_classifier_metadata():
    records = []
    answer = result("sonnet", 0.9, {"haiku": 0.05, "sonnet": 0.9, "opus": 0.05})
    answer["model"] = "api_key=hunter2hunter2"
    answer["calibrate"] = "token=abcdefghijklmnop"
    cand = candidate("codex", windows=[{"name": "token=abcdefghijklmnop", "used_percent": 10,
                                            "window_minutes": 300, "resets_at": NOW + 1000}],
                     model="api_key=hunter2hunter2")
    req = request(cand)
    req["caller"] = "token=abcdefghijklmnop"
    got = pick_turn(req, classifier(answer), now=lambda: NOW, sink=records.append)
    assert got["model"] == "api_key=hunter2hunter2"  # executable identity remains exact for the caller
    assert "hunter2" not in repr(records[0]) and "abcdefghijklmnop" not in repr(records[0])


def test_bounded_audit_queue_never_blocks_and_flush_is_bounded():
    entered = []
    def slow_sink(_record):
        entered.append(True)
        time.sleep(1)
    queue = AuditQueue(slow_sink, maxsize=1)
    started = time.monotonic()
    for i in range(100):
        queue.submit({"i": i})
    queue.close(timeout=0.02)
    assert time.monotonic() - started < 0.15
    assert queue.dropped > 0 and queue._thread.daemon is True
