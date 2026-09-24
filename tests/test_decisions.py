"""clm.decisions: shadow / active routing, outcomes, the report and the fine-tuning export."""
import http.server
import json
import os
import sys
import threading
import time

import pytest

from clm import decisions as D
from clm.decisions_cli import main as cli, summarize, to_row

WORKERS = {"researcher": "Collects the sources the task still needs.",
           "writer": "Drafts the deliverable from sources already collected.",
           "reviewer": "Checks a finished draft before it is saved."}


class FakeResponse:
    def __init__(self, ms="3.0"):
        self.headers = {"X-CLM-Latency-Ms": ms}


class FakeClient:
    """Answers with a fixed distribution after ``delay`` seconds, or raises ``error``."""
    model = "clm-latest"

    def __init__(self, probs=None, delay=0.0, error=None):
        self.probs = probs or {"researcher": 0.9, "writer": 0.07, "reviewer": 0.03}
        self.delay, self.error, self.calls = delay, error, []

    def _post(self, path, body):
        self.calls.append((path, body))
        time.sleep(self.delay)
        if self.error:
            raise self.error
        choice = max(self.probs, key=self.probs.get)
        return {"model": body["model"], "answers": {"route": {"type": "choice", "choice": choice,
                "confidence": 0.5, "probabilities": self.probs}}}, FakeResponse()


class ListSink:
    def __init__(self):
        self.events = []

    def write(self, e):
        self.events.append(json.loads(json.dumps(e)))

    def flush(self, timeout=None):
        pass


def router(**kw):
    sink = kw.pop("sink", None) or ListSink()
    return D.Router("chief", "Choose which worker should act next.", sink=sink, client=kw.pop("client", FakeClient()),
                    **kw), sink


# ── shadow ───────────────────────────────────────────────────────────────────

def test_shadow_acts_on_the_baseline_without_waiting_for_clm():
    r, sink = router(client=FakeClient(delay=0.5))
    t0 = time.perf_counter()
    d = r.route({"task": "brief"}, WORKERS, baseline="writer")
    assert time.perf_counter() - t0 < 0.1
    assert (d.worker, d.acted) == ("writer", "baseline")
    assert sink.events == []                          # the CLM answer is still in flight
    r.flush()
    [rec] = sink.events
    assert rec["baseline"] == {"route": {"label": "writer"}} and rec["clm"]["choice"] == "researcher"
    assert rec["workflow"] == "routing/chief" and rec["acted"] == "baseline" and rec["worker"] == "writer"
    assert rec["questions"]["route"]["criteria"] == WORKERS


def test_shadow_survives_clm_errors():
    r, sink = router(client=FakeClient(error=ConnectionError("down")))
    assert r.route("state", WORKERS, baseline="reviewer").worker == "reviewer"
    r.flush()
    assert "ConnectionError: down" in sink.events[0]["clm"]["error"]


def test_shadow_needs_a_valid_baseline():
    r, _ = router()
    with pytest.raises(ValueError, match="no baseline"):
        r.route("s", WORKERS)
    with pytest.raises(ValueError, match="not one of the workers"):
        r.route("s", WORKERS, baseline="publisher")


# ── active ───────────────────────────────────────────────────────────────────

def test_active_uses_clm_when_confident_and_never_calls_the_baseline():
    calls = []
    r, sink = router(mode="active", threshold=0.8)
    d = r.route("s", WORKERS, baseline=lambda: calls.append(1) or "writer")
    assert (d.worker, d.acted, d.probability) == ("researcher", "clm", 0.9) and calls == []
    assert sink.events[0]["acted"] == "clm" and "baseline" not in sink.events[0]


def test_active_falls_back_below_the_threshold():
    r, sink = router(mode="active", threshold=0.95)
    d = r.route("s", WORKERS, baseline=lambda: "writer")
    assert (d.worker, d.acted, d.clm) == ("writer", "baseline", "researcher")
    assert sink.events[0]["baseline"] == {"route": {"label": "writer"}}


def test_active_falls_back_on_errors():
    r, _ = router(mode="active", client=FakeClient(error=TimeoutError("slow")))
    assert r.route("s", WORKERS, baseline="reviewer").worker == "reviewer"
    r2, _ = router(mode="active", client=FakeClient(error=TimeoutError("slow")))
    with pytest.raises(RuntimeError, match="no baseline"):
        r2.route("s", WORKERS)


def test_active_records_a_plain_baseline_for_comparison():
    r, sink = router(mode="active")
    r.route("s", WORKERS, baseline="writer")
    assert sink.events[0]["baseline"] == {"route": {"label": "writer"}} and sink.events[0]["acted"] == "clm"


def test_router_validates_its_inputs():
    with pytest.raises(ValueError):
        router(mode="yolo")
    with pytest.raises(ValueError):
        router(threshold=0)
    r, _ = router()
    with pytest.raises(ValueError, match="at least two"):
        r.route("s", {"only": "one"}, baseline="only")


# ── outcomes and gold ────────────────────────────────────────────────────────

def test_outcomes_become_gold():
    r, sink = router()
    a = r.route("a", WORKERS, baseline="writer")
    b = r.route("b", WORKERS, baseline="writer")
    c = r.route("c", WORKERS, baseline="writer")
    r.flush()
    r.outcome(a, ok=False, label="researcher")       # wrong, and we know the right one
    r.outcome(b, ok=True)                             # right: the acted-on worker is gold
    r.outcome(c, ok=False)                            # wrong, right answer unknown
    with pytest.raises(ValueError):
        r.outcome(a, label="publisher")
    recs = {x["id"]: x for x in D.merge(sink.events)}
    assert recs[a.id]["gold"] == {"route": {"label": "researcher"}}
    assert recs[b.id]["gold"] == {"route": {"label": "writer"}}
    assert "gold" not in recs[c.id] and len(recs[c.id]["outcome"]) == 1


def test_jsonl_sink_round_trip(tmp_path):
    path = str(tmp_path / "d.jsonl")
    r, _ = router(sink=D.JsonlSink(path))
    d = r.route({"task": "t"}, WORKERS, baseline="writer")
    r.flush()
    r.outcome(d, label="researcher")
    [rec] = D.merge(D.read_jsonl(path))
    assert rec["gold"]["route"]["label"] == "researcher" and rec["clm"]["choice"] == "researcher"


def test_http_sink_posts_events_and_never_raises():
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.headers["Authorization"],
                        json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    sink = D.HttpSink(f"http://127.0.0.1:{srv.server_port}", "k")
    sink.write({"event": "outcome", "id": "x"})
    sink.flush(5)
    srv.shutdown()
    assert got == [("/v1/decisions", "Bearer k", {"event": "outcome", "id": "x"})]
    dead = D.HttpSink("http://127.0.0.1:9", timeout=0.2)
    dead.write({"id": "y"})
    dead.flush(5)                                     # logged and dropped, not raised


# ── report and export ────────────────────────────────────────────────────────

def rec(i, clm, p, base, gold=None):
    r = {"id": f"r{i}", "workflow": "routing/chief", "created_at": f"2026-01-01T00:00:{i:02d}",
         "state": {"task": f"t{i}"}, "questions": {"route": {"type": "choice", "instructions": "x", "criteria": WORKERS}},
         "baseline": {"route": {"label": base}}, "acted": "baseline", "worker": base,
         "clm": {"choice": clm, "probability": p, "latency_ms": 10.0 + i}}
    if gold:
        r["gold"] = {"route": {"label": gold}}
    return r


def test_summary_numbers():
    recs = [rec(0, "writer", 0.95, "writer", "writer"),        # agree, both right
            rec(1, "writer", 0.9, "researcher", "writer"),     # CLM right, router wrong
            rec(2, "reviewer", 0.6, "writer", "writer"),       # CLM wrong (low p), router right
            rec(3, "writer", 0.85, "writer")]                  # agree, no gold
    s = summarize(recs)
    assert s["agreement"] == {"n": 4, "hits": 2}
    assert (s["gold"]["n"], s["gold"]["clm"], s["gold"]["baseline"]) == (3, 2, 2)
    t80 = next(t for t in s["thresholds"] if t["threshold"] == 0.8)
    assert t80["coverage"] == [3, 4] and t80["gold"] == [2, 2]
    assert t80["hybrid_gold"] == [3, 3]            # CLM above 0.8, router below: all right
    assert s["disagreements"][0]["n"] == 1


def test_export_is_readable_by_the_trainer(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "train"))
    from adapters import typed_decision_examples
    pq = pytest.importorskip("pyarrow.parquet")
    events = [rec(i, "writer", 0.9, "writer", gold="researcher" if i % 2 else None) for i in range(40)]
    events.append(dict(rec(99, "writer", 0.9, "writer", gold="reviewer"), state="plain text state"))
    src = tmp_path / "d.jsonl"
    src.write_text("\n".join(json.dumps(e) for e in events))
    out = tmp_path / "data"
    cli(["export", str(src), "--out", str(out), "--test-frac", "0.3"])
    rows = [row for split in ("train", "test")
            for row in pq.read_table(out / "routing/chief" / f"{split}-00000.parquet").to_pylist()]
    assert len(rows) == 21                         # only the labelled decisions
    exs = list(typed_decision_examples(rows))
    assert {e.keys[e.label] for e in exs} == {"researcher", "reviewer"}
    assert any(e.state_text.startswith("plain text state") for e in exs)
    assert all(e.keys == list(WORKERS) for e in exs)
    assert to_row(rec(5, "writer", 0.9, "writer"), "baseline")["gold"] == json.dumps({"route": {"label": "writer"}})


def test_outcomes_from_another_agent_are_ignored():
    r = dict(rec(1, "writer", 0.9, "writer"), agent="alpha")
    ours = {"event": "outcome", "id": "r1", "label": "reviewer", "agent": "alpha", "created_at": "t1"}
    theirs = {"event": "outcome", "id": "r1", "label": "researcher", "agent": "beta", "created_at": "t2"}
    [m] = D.merge([r, ours, theirs])
    assert m["gold"]["route"]["label"] == "reviewer" and len(m["outcome"]) == 1


def test_baseline_events_fill_in_the_baseline_by_rank():
    r = {k: v for k, v in rec(1, "review", 0.9, "writer").items() if k != "baseline"}
    events = [r, {"event": "baseline", "id": "r1", "label": "review", "rank": 2, "created_at": "t1"},
              {"event": "baseline", "id": "r1", "label": "allow", "rank": 1, "created_at": "t2"},
              {"event": "baseline", "id": "other", "label": "block", "rank": 3}]
    [m] = D.merge(events)
    assert m["baseline"] == {"route": {"label": "review"}}     # the prompt outranks the later "it ran"
