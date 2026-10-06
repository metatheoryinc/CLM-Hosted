"""POST /v1/pick-turn uses the shared policy and the server's local engine."""
import math
import time

import pytest
from fastapi.testclient import TestClient

from clm.server import create_app
from clm.turn_picker import SUBAGENT_INSTRUCTIONS, SUBAGENT_OPTIONS, build_state, pick_turn


NOW = 1_791_300_000.0
LONG_TASK = "Review the migration, trace its callers, and identify correctness risks. " * 20
PROBS = {"haiku": 0.05, "sonnet": 0.9, "opus": 0.05}


class Embedder:
    def healthy(self):
        return True


class FakeEngine:
    RESERVED = ()
    heads = {}
    arena = None

    def __init__(self, probs=None, delay=0):
        self.probs = probs or PROBS
        self.delay = delay
        self.calls = []
        self.embedder = Embedder()

    def models(self):
        return [{"name": "subagent-tier-v2"}]

    def answer(self, state, questions, model, temperature=1.0, calibrate=None):
        self.calls.append((state, questions, model, temperature, calibrate))
        time.sleep(self.delay)
        choice = max(self.probs, key=self.probs.get)
        return {"model": model, "calibrate": calibrate or "none", "answers": {"route": {
            "type": "choice", "choice": choice, "confidence": 0.8, "probabilities": self.probs}}}


def candidate(provider, used, tier="sonnet"):
    return {"provider": provider, "model": provider + "-" + tier, "effort": "medium", "tier": tier,
            "budget": {"used_percent": used, "window_minutes": 300, "resets_at": NOW + 3600,
                       "observed_at": NOW}}


def request():
    return {"task": LONG_TASK, "context": "Check rollback behavior.", "caller": "http-test",
            "candidates": [candidate("codex", 91), candidate("claude", 20)]}


def client(engine=None, **kwargs):
    return TestClient(create_app(engine or FakeEngine(), api_key="k", ui=False,
                                 turn_pick_config=kwargs.pop("config", {}), turn_pick_clock=lambda: NOW,
                                 turn_pick_environ=kwargs.pop("environ", {}), **kwargs))


def test_pick_turn_requires_auth_and_validates_requests():
    with client() as c:
        assert c.post("/v1/pick-turn", json=request()).status_code == 401
        assert c.post("/v1/pick-turn", content=b"not json", headers={"Authorization": "Bearer k"}).status_code == 422
        bad = request()
        bad["candidates"] = []
        assert c.post("/v1/pick-turn", json=bad, headers={"Authorization": "Bearer k"}).status_code == 422


def test_pick_turn_classifies_with_the_local_engine_and_trained_rubric():
    engine = FakeEngine()
    with client(engine) as c:
        response = c.post("/v1/pick-turn", json=request(), headers={"Authorization": "Bearer k"})
    assert response.status_code == 200, response.text
    got = response.json()
    assert (got["provider"], got["tier"], got["fallback"]) == ("claude", "sonnet", False)
    [(state, questions, model, temperature, calibrate)] = engine.calls
    assert state == build_state(LONG_TASK, "Check rollback behavior.")
    assert questions["route"]["instructions"] == SUBAGENT_INSTRUCTIONS
    assert questions["route"]["criteria"] == SUBAGENT_OPTIONS
    assert (model, temperature, calibrate) == ("subagent-tier-v2", 1.0, "none")


def test_http_and_library_adapters_have_policy_parity():
    engine = FakeEngine()
    with client(engine) as c:
        http = c.post("/v1/pick-turn", json=request(), headers={"Authorization": "Bearer k"}).json()
    classification = {"choice": "sonnet", "probability": 0.9, "probabilities": PROBS,
                      "model": "subagent-tier-v2", "calibrate": "none", "confidence": 0.8}
    direct = pick_turn(request(), lambda _state, _timeout: classification, now=lambda: NOW)
    assert http == direct


def test_malformed_local_probabilities_fall_back_to_the_exact_default():
    engine = FakeEngine({"haiku": 0.05, "sonnet": math.nan, "opus": 0.95})
    with client(engine) as c:
        got = c.post("/v1/pick-turn", json=request(), headers={"Authorization": "Bearer k"}).json()
    assert got["provider"] == "codex" and got["model"] == "codex-sonnet"
    assert got["fallback"] is True and "classification failed" in got["why"]


def test_engine_deadline_does_not_block_the_server_event_loop_indefinitely():
    engine = FakeEngine(delay=0.5)
    started = time.monotonic()
    with client(engine, config={"turn_pick_timeout_seconds": 0.02}) as c:
        got = c.post("/v1/pick-turn", json=request(), headers={"Authorization": "Bearer k"}).json()
    assert time.monotonic() - started < 0.3
    assert got["fallback"] is True


def test_server_audit_is_bounded_asynchronous_and_sanitized():
    records = []
    entered = []
    def slow_sink(record):
        entered.append(True)
        time.sleep(0.5)
        records.append(record)
    req = request()
    req["task"] = "api_key=hunter2hunter2 " + LONG_TASK
    started = time.monotonic()
    with client(turn_pick_sink=slow_sink) as c:
        got = c.post("/v1/pick-turn", json=req, headers={"Authorization": "Bearer k"}).json()
        elapsed = time.monotonic() - started
        deadline = time.time() + 1
        while not entered and time.time() < deadline:
            time.sleep(0.01)
    assert got["fallback"] is False and elapsed < 0.2
    assert entered
    deadline = time.time() + 1
    while not records and time.time() < deadline:
        time.sleep(0.01)
    assert records[0]["workflow"] == "routing/turn-picks" and "hunter2" not in repr(records[0])


def test_service_owned_collector_configuration_uploads_with_a_short_timeout():
    uploads = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, _limit):
            return b"{}"
    def opener(req, timeout):
        uploads.append((req, timeout))
        return Response()
    env = {"CLM_TURN_PICK_AUDIT_URL": "https://collector.invalid/v1/decisions",
           "CLM_TURN_PICK_AUDIT_KEY": "collector-key"}
    with client(environ=env, turn_pick_audit_opener=opener) as c:
        got = c.post("/v1/pick-turn", json=request(), headers={"Authorization": "Bearer k"}).json()
        deadline = time.time() + 1
        while not uploads and time.time() < deadline:
            time.sleep(0.01)
    assert got["fallback"] is False and len(uploads) == 1
    req, timeout = uploads[0]
    assert req.full_url == env["CLM_TURN_PICK_AUDIT_URL"] and timeout == 0.25
    assert req.get_header("Authorization") == "Bearer collector-key"
    assert b'"workflow": "routing/turn-picks"' in req.data
    with pytest.raises(ValueError, match="both be set"):
        create_app(FakeEngine(), ui=False, turn_pick_environ={"CLM_TURN_PICK_AUDIT_URL": "https://x"})


def test_manual_request_never_calls_the_engine():
    engine = FakeEngine()
    req = {"task": "manual", "caller": "http-test", "requested_tier": "sonnet",
           "candidates": [candidate("codex", 10)]}
    with client(engine) as c:
        got = c.post("/v1/pick-turn", json=req, headers={"Authorization": "Bearer k"}).json()
    assert got["tier"] == "sonnet" and "probabilities" not in got and engine.calls == []
