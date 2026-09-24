"""``clm-decisions``: measure logged routing decisions and export them for fine-tuning.

    clm-decisions report decisions.jsonl                 # local JsonlSink files
    clm-decisions report https://clm.example.com         # a collector (CLM_API_KEY)
    clm-decisions export decisions.jsonl --out data/routing
    python train/finetune.py --task choice --data data/routing --workflow routing/<name> ...

``report`` answers the questions that decide whether CLM can take over a router:
how often it agrees with the current router, how accurate each is where the right
answer is known (``gold``, from outcomes), whether CLM's probability tracks that, and,
for each threshold, how many decisions CLM would take and how good those are.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

from .decisions import QID, merge, read_jsonl

THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)
BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))


# ── loading ──────────────────────────────────────────────────────────────────

def fetch(base_url: str, api_key: str | None, workflow: str | None = None) -> list[dict]:
    """Every event the caller may read from a collector (``GET /v1/decisions``, paged)."""
    import requests
    s = requests.Session()
    if api_key:
        s.headers["Authorization"] = f"Bearer {api_key}"
    events, cursor = [], None
    while True:
        params = {k: v for k, v in {"cursor": cursor, "workflow": workflow}.items() if v}
        r = s.get(base_url.rstrip("/") + "/v1/decisions", params=params, timeout=60)
        if r.status_code != 200:
            raise SystemExit(f"collector returned {r.status_code}: {r.text[:300]}")
        j = r.json()
        events += j["events"]
        cursor = j.get("cursor")
        if not cursor:
            return events


def load(sources: list[str], workflow: str | None = None) -> list[dict]:
    events: list[dict] = []
    for src in sources:
        if src.startswith(("http://", "https://")):
            events += fetch(src, os.environ.get("CLM_API_KEY"), workflow)
        else:
            events += read_jsonl(src)
    recs = merge(events)
    return [r for r in recs if not workflow or r.get("workflow") == workflow]


def _label(r: dict, key: str) -> str | None:
    return ((r.get(key) or {}).get(QID) or {}).get("label")


# ── report ───────────────────────────────────────────────────────────────────

def _rate(hits: int, n: int) -> str:
    return f"{hits / n:6.1%} ({hits}/{n})" if n else "     -"


def summarize(recs: list[dict]) -> dict:
    answered = [r for r in recs if (r.get("clm") or {}).get("choice")]
    errors = [r for r in recs if (r.get("clm") or {}).get("error")]
    lat = sorted(r["clm"]["latency_ms"] for r in answered if r["clm"].get("latency_ms") is not None)
    with_base = [r for r in answered if _label(r, "baseline")]
    gold = [r for r in answered if _label(r, "gold")]
    agree = lambda r: r["clm"]["choice"] == _label(r, "baseline")          # noqa: E731
    clm_ok = lambda r: r["clm"]["choice"] == _label(r, "gold")             # noqa: E731
    base_ok = lambda r: _label(r, "baseline") == _label(r, "gold")         # noqa: E731

    out = {"records": len(recs), "clm_answered": len(answered), "clm_errors": len(errors),
           "latency_ms": {"p50": statistics.median(lat) if lat else None,
                          "p95": lat[int(0.95 * (len(lat) - 1))] if lat else None},
           "agreement": {"n": len(with_base), "hits": sum(map(agree, with_base))},
           "gold": {"n": len(gold), "clm": sum(map(clm_ok, gold)),
                    "baseline_n": sum(1 for r in gold if _label(r, "baseline")),
                    "baseline": sum(1 for r in gold if _label(r, "baseline") and base_ok(r))}}

    out["calibration"] = []
    for lo, hi in BUCKETS:
        b = [r for r in answered if lo <= r["clm"]["probability"] < hi]
        bb, bg = [r for r in b if _label(r, "baseline")], [r for r in b if _label(r, "gold")]
        out["calibration"].append({"bucket": [lo, min(hi, 1.0)], "n": len(b),
                                   "mean_probability": statistics.fmean(r["clm"]["probability"] for r in b) if b else None,
                                   "agreement": [sum(map(agree, bb)), len(bb)], "gold": [sum(map(clm_ok, bg)), len(bg)]})

    out["thresholds"] = []
    for t in THRESHOLDS:
        take = [r for r in answered if r["clm"]["probability"] >= t]
        tb, tg = [r for r in take if _label(r, "baseline")], [r for r in take if _label(r, "gold")]
        # what the agent would have got: CLM above the threshold, the baseline below it
        hybrid = [r for r in gold if _label(r, "baseline")]
        hyb_ok = sum(1 for r in hybrid if (r["clm"]["choice"] if r["clm"]["probability"] >= t
                                           else _label(r, "baseline")) == _label(r, "gold"))
        out["thresholds"].append({"threshold": t, "coverage": [len(take), len(answered)],
                                  "agreement": [sum(map(agree, tb)), len(tb)], "gold": [sum(map(clm_ok, tg)), len(tg)],
                                  "hybrid_gold": [hyb_ok, len(hybrid)]})

    pairs = Counter((_label(r, "baseline"), r["clm"]["choice"]) for r in with_base if not agree(r))
    out["disagreements"] = [{"baseline": b, "clm": c, "n": n} for (b, c), n in pairs.most_common(10)]
    return out


def print_report(name: str, s: dict) -> None:
    p = lambda *a: print(*a)                                                 # noqa: E731
    p(f"\n== {name}: {s['records']} decisions, CLM answered {s['clm_answered']}, errors {s['clm_errors']}")
    if s["latency_ms"]["p50"] is not None:
        p(f"   CLM latency p50 {s['latency_ms']['p50']:.0f} ms, p95 {s['latency_ms']['p95']:.0f} ms (as seen by the agent)")
    a, g = s["agreement"], s["gold"]
    p(f"   agrees with current router   {_rate(a['hits'], a['n'])}")
    p(f"   accuracy on gold    CLM      {_rate(g['clm'], g['n'])}")
    p(f"                       router   {_rate(g['baseline'], g['baseline_n'])}")
    p("\n   CLM probability   n     mean   agrees w/ router     correct (gold)")
    for c in s["calibration"]:
        lo, hi = c["bucket"]
        mean = f"{c['mean_probability']:.2f}" if c["mean_probability"] is not None else "   -"
        p(f"   {lo:.2f}-{hi:.2f}   {c['n']:6d}   {mean}   {_rate(*c['agreement']):>18}   {_rate(*c['gold']):>18}")
    p("\n   threshold   CLM decides        agrees w/ router     correct (gold)       CLM+router on gold")
    for t in s["thresholds"]:
        p(f"   {t['threshold']:.2f}       {_rate(*t['coverage']):>16}   {_rate(*t['agreement']):>18}   "
          f"{_rate(*t['gold']):>18}   {_rate(*t['hybrid_gold']):>18}")
    if s["disagreements"]:
        p("\n   most common disagreements (router -> CLM):")
        for d in s["disagreements"]:
            p(f"     {d['baseline']} -> {d['clm']}: {d['n']}")


def report(args) -> None:
    recs = load(args.sources, args.workflow)
    by = defaultdict(list)
    for r in recs:
        by[r.get("workflow") or "?"].append(r)
    result = {w: summarize(rs) for w, rs in sorted(by.items())}
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return
    if not result:
        print("no decisions found")
    for w, s in result.items():
        print_report(w, s)


# ── export ───────────────────────────────────────────────────────────────────

def to_row(r: dict, labels: str) -> dict | None:
    label = _label(r, "gold") or (_label(r, "baseline") if labels == "baseline" else None)
    if label is None:
        return None
    enc = lambda x: x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)     # noqa: E731
    return {"id": r["id"], "workflow": r["workflow"], "state": enc(r["state"]),
            "questions": json.dumps(r["questions"], ensure_ascii=False),
            "gold": json.dumps({QID: {"label": label}})}


def split_of(rid: str, test_frac: float) -> str:
    h = int(hashlib.sha1(rid.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "test" if h < test_frac else "train"


def export(args) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    rows = [row for r in load(args.sources, args.workflow) if (row := to_row(r, args.labels))]
    if not rows:
        raise SystemExit("no labelled decisions to export (label them with outcomes, or use --labels baseline)")
    groups = defaultdict(list)
    for row in rows:
        groups[(row["workflow"], split_of(row["id"], args.test_frac))].append(row)
    for (workflow, split), rs in sorted(groups.items()):
        d = os.path.join(args.out, workflow)
        os.makedirs(d, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rs), os.path.join(d, f"{split}-00000.parquet"))
        print(f"{workflow} {split}: {len(rs)} rows -> {d}")
    missing = {w for w, _ in groups} - {w for w, s in groups if s == "test"}
    for w in sorted(missing):
        print(f"warning: {w} has no test rows; finetune.py needs both splits", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="clm-decisions", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("report", report), ("export", export)):
        sp = sub.add_parser(name)
        sp.add_argument("sources", nargs="+", help="JsonlSink files and/or collector base URLs")
        sp.add_argument("--workflow", help="only this workflow (routing/<router name>)")
        sp.set_defaults(fn=fn)
        if name == "report":
            sp.add_argument("--json", action="store_true")
        else:
            sp.add_argument("--out", required=True, help="output directory (finetune.py --data)")
            sp.add_argument("--labels", choices=["gold", "baseline"], default="gold",
                            help="gold: only decisions with a known right answer; baseline: fall back to "
                                 "the current router's pick (distils the router)")
            sp.add_argument("--test-frac", type=float, default=0.2)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
