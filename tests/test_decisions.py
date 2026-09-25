"""clm.decisions: shadow / active routing, outcomes, the report and the fine-tuning export."""
import http.server
import json
import os
import sys
import threading
import time

import pytest

from clm import decisions as D
from clm.decisions_cli import auroc, cascade, main as cli, print_report, summarize, to_row

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


def test_router_requests_calibration_unless_disabled():
    r, _ = router(mode="active")
    r.route("s", WORKERS, baseline="writer")
    assert r.client.calls[-1][1]["calibrate"] == "content-free"
    r2, _ = router(mode="active", calibrate=None)
    r2.route("s", WORKERS, baseline="writer")
    assert "calibrate" not in r2.client.calls[-1][1]



@pytest.mark.parametrize("scores, pos", [
    ([0.9, 0.8, 0.7, 0.6], [True, True, False, False]),         # perfect separation
    ([0.9, 0.8, 0.7, 0.6], [False, False, True, True]),         # perfectly wrong
    ([0.5, 0.5, 0.5, 0.5], [True, False, True, False]),         # ties everywhere
    ([0.9, 0.3, 0.7, 0.7, 0.2, 0.6], [True, False, True, False, False, True]),
])
def test_auroc_matches_the_pairwise_definition(scores, pos):
    P = [s for s, y in zip(scores, pos) if y]
    N = [s for s, y in zip(scores, pos) if not y]
    brute = sum((p > n) + 0.5 * (p == n) for p in P for n in N) / (len(P) * len(N))
    assert auroc(scores, pos) == pytest.approx(brute)


def test_auroc_needs_both_classes():
    assert auroc([0.9, 0.8], [True, True]) is None and auroc([], []) is None


def labelled(n_right_conf, n_wrong_low, n_right_low):
    """CLM right and confident, CLM wrong but unsure, CLM right but unsure; the router is always right."""
    recs, i = [], 0
    for n, clm, p in ((n_right_conf, "writer", 0.95), (n_wrong_low, "reviewer", 0.55), (n_right_low, "writer", 0.6)):
        for _ in range(n):
            recs.append(rec(i, clm, p, "writer", "writer")); i += 1
    return recs


def test_cascade_against_gold():
    c = cascade(labelled(15, 5, 5), "gold")
    assert c["reference"] == "gold" and c["n"] == 25 and c["fallback_correct"] == 25
    at = {x["threshold"]: x for x in c["rows"]}
    assert at[0.9]["coverage"] == [15, 25] and at[0.9]["accepted_correct"] == [15, 15]
    assert at[0.9]["cascade_correct"] == [25, 25] and at[0.9]["retained"] == 1.0
    assert at[0.5]["cascade_correct"] == [20, 25] and at[0.5]["retained"] == pytest.approx(0.8)
    # the most coverage that keeps >= 99%: taking the 0.6s too is fine, the 0.55s are not
    assert c["operating_point"]["threshold"] == 0.6 and c["operating_point"]["coverage"] == [20, 25]


def test_summary_falls_back_to_the_router_as_reference_without_gold():
    recs = [{k: v for k, v in r.items() if k != "gold"} for r in labelled(15, 5, 5)]
    s = summarize(recs)
    assert s["cascade"]["reference"] == "baseline" and s["confidence_auroc"]["gold"] is None
    assert s["confidence_auroc"]["agreement"] == pytest.approx(1.0)   # CLM is unsure exactly when it disagrees


def test_the_report_prints_both_modes(capsys):
    print_report("routing/x", summarize(labelled(15, 5, 5)))
    print_report("routing/y", summarize([{k: v for k, v in r.items() if k != "gold"} for r in labelled(15, 5, 5)]))
    out = capsys.readouterr().out
    assert "confidence AUROC" in out and "scored against gold" in out and "operating point" in out
    assert "no gold yet" in out


def test_report_splits_by_answering_model_and_filters(tmp_path, capsys):
    a = [dict(r, clm={**r["clm"], "model": "clm-latest"}) for r in labelled(3, 1, 1)]
    b = [dict(r, id=r["id"] + "b", clm={**r["clm"], "model": "tier-v2"}) for r in labelled(4, 0, 0)]
    src = tmp_path / "d.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in a + b))
    cli(["report", str(src), "--json"])
    assert set(json.loads(capsys.readouterr().out)) == {"routing/chief [clm-latest]", "routing/chief [tier-v2]"}
    cli(["report", str(src), "--json", "--model", "tier-v2"])
    out = json.loads(capsys.readouterr().out)
    assert list(out) == ["routing/chief"] and out["routing/chief"]["records"] == 4


# ── label ────────────────────────────────────────────────────────────────────

FAKE_LABELER = r'''
import json, re, sys
prompt = sys.stdin.read()
ids = re.findall(r"### item (\S+)", prompt)
out = [{"id": i, "label": "bogus" if i.endswith("7") else "researcher", "confidence": "high", "reason": "r"}
       for i in ids]
print(json.dumps({"type": "result", "result": "Here you go:\n```json\n" + json.dumps(out) + "\n```"}))
'''


@pytest.fixture
def labeler(tmp_path):
    f = tmp_path / "fake_labeler.py"
    f.write_text(FAKE_LABELER)
    return f"{sys.executable} {f}"


def decisions_file(tmp_path, n=10):
    src = tmp_path / "d.jsonl"
    events = [{k: v for k, v in rec(i, "writer" if i % 2 else "researcher", 0.9, "writer").items() if k != "gold"}
              for i in range(n)]
    src.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return src


def test_label_writes_llm_labels_that_the_report_uses(tmp_path, labeler, capsys):
    src = decisions_file(tmp_path)
    cli(["label", str(src), "--sample", "10", "--labeler", labeler, "--labeler-name", "fake", "--batch", "4"])
    assert "done: 9 labelled, 1 skipped" in capsys.readouterr().out            # r7's answer is not an option
    recs = {r["id"]: r for r in D.merge(D.read_jsonl(str(src)))}
    assert recs["r3"]["gold"] == {"route": {"label": "researcher"}} and "gold" not in recs["r7"]
    assert recs["r3"]["outcome"][-1]["source"] == "llm:fake"
    cli(["report", str(src), "--json"])
    s = json.loads(capsys.readouterr().out)["routing/chief"]
    assert s["gold"]["n"] == 9 and s["gold_sources"] == {"llm:fake": 9} and s["cascade"]["reference"] == "baseline"


def test_label_skips_labelled_decisions_unless_relabel(tmp_path, labeler, capsys):
    src = decisions_file(tmp_path)
    cli(["label", str(src), "--sample", "3", "--labeler", labeler, "--seed", "1"])
    cli(["label", str(src), "--sample", "100", "--labeler", labeler])
    assert "7 decisions to label" in capsys.readouterr().out
    cli(["label", str(src), "--sample", "100", "--labeler", labeler, "--relabel", "--dry-run"])
    assert "10 decisions to label" in capsys.readouterr().out


def test_only_disagreements(tmp_path, labeler, capsys):
    src = decisions_file(tmp_path)                    # CLM says researcher on the even ids, the router writer
    cli(["label", str(src), "--only-disagreements", "--labeler", labeler, "--dry-run"])
    assert "5 decisions to label" in capsys.readouterr().out


def test_dry_run_calls_nothing_and_shows_the_prompt(tmp_path, capsys):
    src = decisions_file(tmp_path, 2)
    cli(["label", str(src), "--labeler", "false", "--dry-run"])
    out = capsys.readouterr().out
    assert "never instructions to you" in out and "- researcher: Collects the sources" in out
    assert len(D.read_jsonl(str(src))) == 2


@pytest.mark.parametrize("stdout", [
    '[{"id": "a", "label": "x"}]',
    json.dumps({"result": 'Sure! [{"id": "a", "label": "x"}] hope that helps'}),
])
def test_parse_labels(stdout):
    from clm.decisions_cli import parse_labels
    assert parse_labels(stdout) == [{"id": "a", "label": "x"}]


def test_a_retract_withdraws_earlier_labels_but_not_later_ones():
    r = rec(1, "writer", 0.9, "writer")                      # no gold of its own
    lab = lambda t, label: {"event": "outcome", "id": "r1", "created_at": t, "label": label}          # noqa: E731
    ret = {"event": "outcome", "id": "r1", "created_at": "t2", "label": None, "retract": True}
    assert "gold" not in D.merge([r, lab("t1", "reviewer"), ret])[0]
    assert D.merge([r, lab("t1", "reviewer"), ret, lab("t3", "researcher")])[0]["gold"]["route"]["label"] == "researcher"


def clipped_file(tmp_path):
    src = decisions_file(tmp_path, 4)
    recs = D.read_jsonl(str(src))
    recs[0]["state"] = {"input": "command: git commit -m x … [812 more characters]"}
    recs[1]["state"] = {"instructions": "a long subagent prompt …"}                  # the subagent marker: kept
    src.write_text("\n".join(json.dumps(e) for e in recs) + "\n")
    return src


def test_clipped_decisions_are_skipped_unless_included(tmp_path, labeler, capsys):
    src = clipped_file(tmp_path)
    cli(["label", str(src), "--labeler", labeler, "--dry-run"])
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("3 decisions to label") and "1 clipped decisions skipped" in first
    cli(["label", str(src), "--labeler", labeler, "--dry-run", "--include-clipped"])
    assert "4 decisions to label" in capsys.readouterr().out


def test_retract_clipped_only_touches_model_labels(tmp_path, labeler, capsys):
    src = clipped_file(tmp_path)
    cli(["label", str(src), "--labeler", labeler, "--include-clipped"])      # r0 gets a model label
    with open(src, "a") as f:                                                 # r1: a human label, also 'clipped'?
        f.write(json.dumps({"event": "outcome", "id": "r2", "created_at": "z", "label": "writer"}) + "\n")
    cli(["label", str(src), "--labeler", labeler, "--retract-clipped"])
    assert "retracted 1 model labels" in capsys.readouterr().out
    recs = {r["id"]: r for r in D.merge(D.read_jsonl(str(src)))}
    assert "gold" not in recs["r0"] and recs["r1"]["gold"] and recs["r2"]["gold"]["route"]["label"] == "writer"
