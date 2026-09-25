"""Routing decisions with CLM: shadow them first, measure, then let CLM decide.

An agent that hands work to one of several workers asks CLM the same question
its current router answers, and every decision is logged as a record that is
also a typed-decisions training row (``train/finetune.py --task choice``):

    from clm.decisions import Router, HttpSink

    router = Router("chief-of-staff", "Choose which worker should act next.",
                    sink=HttpSink.from_env())         # or JsonlSink("decisions.jsonl")
    d = router.route(state, workers={"researcher": "Collects sources the task still needs.",
                                     "writer": "Drafts the deliverable from collected sources.",
                                     "reviewer": "Checks a finished draft before it is saved."},
                     baseline="researcher")         # what the current router chose
    run(d.worker)
    ...
    router.outcome(d, ok=False, label="writer")        # later: it should have gone to the writer

Modes:

* ``shadow`` (default): the baseline decides; CLM is asked in the background and only
  logged, so the agent's behaviour and latency do not change.
* ``active``: CLM decides when its top probability reaches ``threshold``; otherwise, or
  on any error or timeout, the baseline does. ``baseline`` may be a callable, which then
  runs only on those fallbacks (the point: no LLM call when CLM is confident).

A record is ``{id, workflow, state, questions, baseline, clm, acted, worker}`` plus
``outcome`` / ``gold`` once known; ``clm-decisions report`` measures them and
``clm-decisions export`` writes the labelled ones as fine-tuning data.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

QID = "route"
MODES = ("shadow", "active")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def _warn(msg: str) -> None:
    print(f"[clm.decisions] {msg}", file=sys.stderr, flush=True)


# ── sinks ────────────────────────────────────────────────────────────────────

class JsonlSink:
    """Append records and outcome events to a local ``.jsonl`` file (one object per line)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def write(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def flush(self, timeout: float | None = None) -> None:
        pass


class HttpSink:
    """Send events to a collector (``POST {base}/v1/decisions``) from a background thread.

    Never raises into the agent: failures are logged and the event is dropped.
    """

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 5.0, max_queue: int = 10_000):
        import requests
        self.url = base_url.rstrip("/") + "/v1/decisions"
        self.timeout = timeout
        self._s = requests.Session()
        if api_key:
            self._s.headers["Authorization"] = f"Bearer {api_key}"
        self._q: queue.Queue = queue.Queue(max_queue)
        threading.Thread(target=self._run, name="clm-decisions-sink", daemon=True).start()

    @classmethod
    def from_env(cls, **kw) -> "HttpSink":
        """``CLM_BASE_URL`` and ``CLM_API_KEY``, the same variables ``CLMClient`` reads."""
        base = os.environ.get("CLM_BASE_URL")
        if not base:
            raise RuntimeError("CLM_BASE_URL is not set")
        return cls(base, os.environ.get("CLM_API_KEY"), **kw)

    def write(self, event: dict) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            _warn("sink queue full; dropping a decision event")

    def _run(self) -> None:
        while True:
            event = self._q.get()
            try:
                r = self._s.post(self.url, json=event, timeout=self.timeout)
                if r.status_code >= 300:
                    _warn(f"collector returned {r.status_code}: {r.text[:200]}")
            except Exception as e:  # noqa: BLE001
                _warn(f"collector unreachable: {e}")
            finally:
                self._q.task_done()

    def flush(self, timeout: float | None = 10.0) -> None:
        """Wait (up to ``timeout`` seconds) until queued events are sent, e.g. before exit."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._q.unfinished_tasks:
            if deadline is not None and time.monotonic() > deadline:
                _warn(f"{self._q.unfinished_tasks} decision events still unsent")
                return
            time.sleep(0.02)


# ── the router ───────────────────────────────────────────────────────────────

@dataclass
class Decision:
    id: str
    worker: str                         # what the agent should act on
    acted: str                          # "baseline" or "clm"
    clm: str | None = None              # CLM's pick, when it answered in time
    probability: float | None = None    # CLM's probability for its pick
    record: dict = field(default_factory=dict, repr=False)


class Router:
    """Route a task to one of ``workers`` with CLM, logging every decision to ``sink``."""

    def __init__(self, name: str, instructions: str, sink=None, mode: str = "shadow",
                 threshold: float = 0.8, client=None, model: str | None = None,
                 timeout: float = 2.0, calibrate: str | None = "content-free"):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be in (0, 1]")
        from .client import CLMClient
        self.name, self.instructions, self.sink, self.mode, self.threshold = name, instructions, sink, mode, threshold
        self.client = client or CLMClient(timeout=timeout)
        self.model = model or os.environ.get("CLM_MODEL") or self.client.model
        self.calibrate = calibrate            # "content-free": remove each option's lean (None: off)
        self._pending: set[threading.Thread] = set()
        self._pending_lock = threading.Lock()

    # the System One request for one decision
    def question(self, workers: dict[str, str]) -> dict:
        if not workers or len(workers) < 2:
            raise ValueError("workers must name at least two options")
        return {QID: {"type": "choice", "instructions": self.instructions,
                      "criteria": {str(k): str(v) for k, v in workers.items()}}}

    def _ask(self, state: Any, questions: dict) -> dict:
        t0 = time.perf_counter()
        try:
            body = {"state": state, "questions": questions, "model": self.model}
            if self.calibrate:
                body["calibrate"] = self.calibrate
            j, r = self.client._post("/v1/systemone", body)
            a = j["answers"][QID]
            return {"model": j.get("model", self.model), "calibrate": j.get("calibrate", "none"),
                    "choice": a["choice"],
                    "probability": float(a["probabilities"][a["choice"]]), "confidence": float(a["confidence"]),
                    "probabilities": a["probabilities"], "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "server_ms": float(r.headers.get("X-CLM-Latency-Ms") or 0) or None}
        except Exception as e:  # noqa: BLE001
            return {"model": self.model, "error": f"{type(e).__name__}: {e}"[:300],
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def route(self, state: Any, workers: dict[str, str], baseline: str | Callable[[], str] | None = None,
              meta: dict | None = None) -> Decision:
        """-> the ``Decision``; ``baseline`` is the current router's pick (or a callable making it).

        ``state`` is what the decision depends on (the task, progress, evidence), in the System One
        ``state`` format; ``workers`` maps each available worker to one clear description.
        """
        questions = self.question(workers)
        rec = {"id": uuid.uuid4().hex, "workflow": f"routing/{self.name}", "created_at": _now(),
               "mode": self.mode, "threshold": self.threshold, "state": state, "questions": questions,
               "meta": meta or {}}

        def base() -> str:
            b = baseline() if callable(baseline) else baseline
            if b is None:
                raise ValueError("no baseline: pass the current router's choice (or mode='active')")
            if str(b) not in questions[QID]["criteria"]:
                raise ValueError(f"baseline {b!r} is not one of the workers {list(questions[QID]['criteria'])}")
            return str(b)

        if self.mode == "shadow":
            worker = base()
            rec.update(baseline={QID: {"label": worker}}, acted="baseline", worker=worker)
            t = threading.Thread(target=self._finish_shadow, args=(rec,), name="clm-decisions-shadow", daemon=True)
            with self._pending_lock:
                self._pending.add(t)
            t.start()
            return Decision(rec["id"], worker, "baseline", record=rec)

        clm = self._ask(state, questions)
        rec["clm"] = clm
        if "error" not in clm and clm["probability"] >= self.threshold:
            worker, acted = clm["choice"], "clm"
            if baseline is not None and not callable(baseline):
                rec["baseline"] = {QID: {"label": base()}}
        else:
            worker, acted = (base() if baseline is not None else clm.get("choice")), "baseline"
            if worker is None:
                raise RuntimeError(f"CLM failed and there is no baseline: {clm.get('error')}")
            if baseline is not None:
                rec["baseline"] = {QID: {"label": worker}}
            else:
                acted = "clm"                # below threshold but nothing to fall back to
        rec.update(acted=acted, worker=worker)
        self._emit(rec)
        return Decision(rec["id"], worker, acted, clm.get("choice"), clm.get("probability"), rec)

    def _finish_shadow(self, rec: dict) -> None:
        try:
            rec["clm"] = self._ask(rec["state"], rec["questions"])
            self._emit(rec)
        finally:
            with self._pending_lock:
                self._pending.discard(threading.current_thread())

    def _emit(self, event: dict) -> None:
        if self.sink is None:
            return
        try:
            self.sink.write(event)
        except Exception as e:  # noqa: BLE001
            _warn(f"sink failed: {e}")

    def outcome(self, decision: Decision | str, ok: bool | None = None, label: str | None = None,
                note: str | None = None) -> None:
        """Record what happened: ``ok`` = the chosen worker was right; ``label`` = the right worker.

        ``ok=False`` with no ``label`` says only that the pick was wrong. A ``label`` becomes the
        record's gold label for evaluation and fine-tuning.
        """
        did = decision.id if isinstance(decision, Decision) else str(decision)
        if isinstance(decision, Decision) and label is not None:
            options = decision.record["questions"][QID]["criteria"]
            if label not in options:
                raise ValueError(f"label {label!r} is not one of the workers {list(options)}")
        self._emit({"event": "outcome", "id": did, "created_at": _now(), "ok": ok, "label": label, "note": note})

    def flush(self, timeout: float | None = 10.0) -> None:
        """Wait for background CLM calls and queued events (call before a short-lived process exits)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._pending_lock:
            pending = list(self._pending)
        for t in pending:
            t.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        if self.sink is not None:
            self.sink.flush(timeout if deadline is None else max(0.0, deadline - time.monotonic()))


# ── reading records back ─────────────────────────────────────────────────────

def merge(events: Iterable[dict]) -> list[dict]:
    """Decision records with their outcome events folded in (``outcome`` list and ``gold``).

    Gold, per record: the latest outcome ``label``; else, when an outcome says the acted-on
    worker was right (``ok=True``), that worker.
    """
    recs: dict[str, dict] = {}
    outcomes: dict[str, list] = {}
    baselines: dict[str, list] = {}
    for e in events:
        if e.get("event") == "outcome":
            outcomes.setdefault(e["id"], []).append(e)
        elif e.get("event") == "baseline":
            baselines.setdefault(e["id"], []).append(e)
        elif "questions" in e:
            recs[e["id"]] = dict(e)
    same_agent = lambda r, e: not (r.get("agent") and e.get("agent") and e["agent"] != r["agent"])  # noqa: E731
    for did, evs in baselines.items():
        # the existing decision-maker's pick, reported after the record (e.g. by a hook), maybe
        # in several steps: the highest ``rank`` wins, then the latest (a permission prompt, rank 2,
        # outranks the "it ran" event that follows an approval, rank 1)
        r = recs.get(did)
        evs = [e for e in evs if r is not None and same_agent(r, e)]
        if evs:
            best = max(evs, key=lambda e: (e.get("rank", 0), e.get("created_at") or ""))
            r["baseline"] = {QID: {"label": best["label"]}}
    for did, evs in outcomes.items():
        r = recs.get(did)
        if r is None:
            continue
        # a collector stamps events with the posting agent: only the record's own agent labels it
        evs = [o for o in evs if same_agent(r, o)]
        if not evs:
            continue
        evs = sorted(evs, key=lambda o: o.get("created_at") or "")
        r["outcome"] = evs
        # the latest label wins; a later "retract" event withdraws the labels before it
        last = next((o for o in reversed(evs) if o.get("label") or o.get("retract")), None)
        label = last["label"] if last and not last.get("retract") else None
        if last and last.get("retract"):
            r["outcome"] = [o for o in evs if not o.get("label") or o["created_at"] > last["created_at"]]
            continue
        if label is None and any(o.get("ok") is True for o in evs) and not any(o.get("ok") is False for o in evs):
            label = r.get("worker")
        if label is not None:
            r["gold"] = {QID: {"label": label}}
    return sorted(recs.values(), key=lambda r: r.get("created_at") or "")


def read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
