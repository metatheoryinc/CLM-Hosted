#!/usr/bin/env python3
"""CLM as a judge, on the benchmarks of "JEV-as-a-Judge: Accept When Confident, Escalate
When Unsure" (Li et al., CMU, 2026): RewardBench, JudgeBench and HaluEval.

    python evaluation/judge_eval.py --base-url https://clm.example.com --out results/judge.json

Samples like the paper (fixed seed; the paper's own item lists are not public, so numbers
are comparable, not identical): RewardBench 100 pairs from each of chat / chat-hard /
safety / reasoning (duplicate triples removed), JudgeBench's 350-pair GPT-4o split, and
120 HaluEval QA questions judged with their right and their hallucinated answer (240).

Two framings:

* ``paper``: the paper's requests. Pairs are a Choice between A and B, judged in both
  orders with the aligned probability averaged, p(A) = (p1(A|A,B) + p2(A|B,A)) / 2;
  HaluEval is a Choice between supported and hallucinated given question, evidence and
  answer. The whole state must fit CLM's 2048-token budget (the question comes last), so
  long texts are clipped (start and end kept); Jev and GPT-6 read everything.
* ``rank``: CLM's own primitive for pairs: the question is the state and each response a
  candidate (``/v1/rank``), so each response gets its own 2048-token budget.

Each is run uncalibrated and with ``calibrate="content-free"``. Reported per benchmark and
category: accuracy (a failed request counts as an error), confidence AUROC (max label
probability against correctness), Brier score, and accuracy of the items CLM would accept
at each threshold (the first stage of the paper's accept/escalate cascade). Per-item
results are saved so a fallback judge can be added for the full cascade.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

ROWS = "https://datasets-server.huggingface.co/rows?dataset={ds}&config={cfg}&split={split}&offset={o}&length={n}"
RB_CATEGORIES = {
    "chat": ["alpacaeval-easy", "alpacaeval-length", "alpacaeval-hard", "mt-bench-easy", "mt-bench-med"],
    "chat-hard": ["mt-bench-hard", "llmbar-natural", "llmbar-adver-neighbor", "llmbar-adver-GPTInst",
                  "llmbar-adver-GPTOut", "llmbar-adver-manual"],
    "safety": ["refusals-dangerous", "refusals-offensive", "xstest-should-refuse", "xstest-should-respond",
               "donotanswer"],
    "reasoning": ["math-prm", "hep-cpp", "hep-go", "hep-java", "hep-js", "hep-python", "hep-rust"],
}
JB_CATEGORY = lambda src: ("knowledge" if src.startswith("mmlu-pro") else "reasoning" if "reasoning" in src  # noqa: E731
                           else "math" if "math" in src else "coding")
PAIR_Q = ("Evaluate the two candidate responses to the question. Choose the better response, prioritizing "
          "factual correctness, valid reasoning, instruction following, relevance, and appropriate safety.")
PAIR_OPTS = {"A": "Response A is the better response.", "B": "Response B is the better response."}
HALL_Q = "Does the candidate answer answer the question faithfully according to the supplied evidence?"
HALL_OPTS = {"supported": "The answer is supported by the evidence.",
             "hallucinated": "The answer is not supported by the evidence."}
# the paper's numbers (accuracy %, confidence AUROC) for reference
PAPER = {"RewardBench": {"jev": (92.2, 0.869), "gpt-6": (93.5, 0.891)},
         "JudgeBench": {"jev": (78.6, 0.745), "gpt-6": (93.1, 0.907)},
         "HaluEval": {"jev": (87.5, 0.863), "gpt-6": (86.7, 0.899)}}
THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99)


# ── data ─────────────────────────────────────────────────────────────────────

def fetch(ds, cfg, split, cache, offsets=None, page=100):
    path = os.path.join(cache, f"{ds.replace('/', '__')}.{cfg}.{split}.json")
    if os.path.exists(path):
        return json.load(open(path))
    rows, o = [], 0
    while True:
        url = ROWS.format(ds=ds, cfg=cfg, split=split, o=o, n=page)
        for attempt in range(5):
            try:
                j = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "clm-eval/1"}),
                                                     timeout=60))
                break
            except Exception:  # noqa: BLE001
                time.sleep(2 * (attempt + 1))
        else:
            raise SystemExit(f"could not fetch {url}")
        rows += [r["row"] for r in j["rows"]]
        o += page
        if o >= j["num_rows_total"] or (offsets and o >= offsets):
            break
    os.makedirs(cache, exist_ok=True)
    json.dump(rows, open(path, "w"))
    return rows


def items(cache, seed):
    rng = random.Random(seed)
    out = []
    rb, seen = fetch("allenai/reward-bench", "default", "filtered", cache), set()
    for cat, subsets in RB_CATEGORIES.items():
        pool = []
        for r in rb:
            key = (r["prompt"], r["chosen"], r["rejected"])
            if r["subset"] in subsets and key not in seen:
                seen.add(key)
                pool.append(r)
        for r in rng.sample(pool, 100):
            out.append({"bench": "RewardBench", "category": cat, "kind": "pair", "question": r["prompt"],
                        "a": r["chosen"], "b": r["rejected"], "gold": "A", "id": f"rb-{r['id']}"})
    for r in fetch("ScalerLab/JudgeBench", "default", "gpt", cache):
        out.append({"bench": "JudgeBench", "category": JB_CATEGORY(r["source"]), "kind": "pair",
                    "question": r["question"], "a": r["response_A"], "b": r["response_B"],
                    "gold": "A" if r["label"].startswith("A>") else "B", "id": f"jb-{r['pair_id']}"})
    he = fetch("pminervini/HaluEval", "qa", "data", cache, offsets=2000)
    for i, r in enumerate(rng.sample(he, 120)):
        for ans, gold in ((r["right_answer"], "supported"), (r["hallucinated_answer"], "hallucinated")):
            out.append({"bench": "HaluEval", "category": "qa", "kind": "hall", "question": r["question"],
                        "evidence": r["knowledge"], "answer": ans, "gold": gold, "id": f"he-{i}-{gold}"})
    return out


# ── CLM ──────────────────────────────────────────────────────────────────────

def clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[:int(n * 0.6)] + " … " + text[len(text) - (n - int(n * 0.6)):]


def post(base, key, path, body, retries=4):
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "clm-eval/1"})
    for attempt in range(retries):
        try:
            return json.load(urllib.request.urlopen(req, timeout=300))
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 * (attempt + 1))
    raise err


def judge(it, framing, cal, base, key, model):
    """-> {"pred", "q", "p_gold"} (pred None on failure)."""
    try:
        extra = {"calibrate": cal} if cal else {}
        if it["kind"] == "hall":
            st = {"question": clip(it["question"], 800), "evidence": clip(it["evidence"], 3500),
                  "candidate answer": clip(it["answer"], 1200)}
            p = post(base, key, "/v1/systemone", {"state": st, "model": model, **extra, "questions": {
                "r": {"type": "choice", "instructions": HALL_Q, "criteria": HALL_OPTS}}})["answers"]["r"]["probabilities"]
        elif framing == "paper":
            def order(x, y):
                st = {"question": clip(it["question"], 1200), "response A": clip(x, 2300), "response B": clip(y, 2300)}
                return post(base, key, "/v1/systemone", {"state": st, "model": model, **extra, "questions": {
                    "r": {"type": "choice", "instructions": PAIR_Q, "criteria": PAIR_OPTS}}})["answers"]["r"]["probabilities"]
            p1, p2 = order(it["a"], it["b"]), order(it["b"], it["a"])
            pa = (p1["A"] + p2["B"]) / 2                       # the aligned probability of the original A
            p = {"A": pa, "B": 1 - pa}
        else:
            ranked = post(base, key, "/v1/rank", {"context": "", "question": clip(it["question"], 6000), "model": model,
                                                  "answers": [clip(it["a"], 7000), clip(it["b"], 7000)], **extra})["ranked"]
            probs = {r["candidate"]: r["prob"] for r in ranked}
            pa = probs[clip(it["a"], 7000)]
            p = {"A": pa, "B": 1 - pa}
        pred = max(p, key=p.get)
        return {"pred": pred, "q": p[pred], "p_gold": p[it["gold"]]}
    except Exception as e:  # noqa: BLE001
        return {"pred": None, "q": None, "p_gold": None, "error": f"{type(e).__name__}: {e}"[:200]}


# ── metrics ──────────────────────────────────────────────────────────────────

def auroc(scores, pos):
    P = [s for s, y in zip(scores, pos) if y]
    N = [s for s, y in zip(scores, pos) if not y]
    if not P or not N:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in P for n in N) / (len(P) * len(N))


def metrics(rs):
    ok = [r["pred"] == r["gold"] for r in rs]
    valid = [r for r in rs if r["pred"] is not None]
    sel = []
    for t in THRESHOLDS:
        take = [r for r in valid if r["q"] >= t]
        sel.append({"threshold": t, "coverage": len(take) / len(rs),
                    "accuracy": sum(r["pred"] == r["gold"] for r in take) / len(take) if take else None})
    return {"n": len(rs), "accuracy": sum(ok) / len(rs), "failed": len(rs) - len(valid),
            "auroc": auroc([r["q"] for r in valid], [r["pred"] == r["gold"] for r in valid]),
            "brier": sum((1 - r["p_gold"]) ** 2 for r in valid) / len(valid) if valid else None,
            "selective": sel}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("CLM_BASE_URL", "http://127.0.0.1:8700"))
    ap.add_argument("--model", default="clm-latest")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/clm-judge-eval"))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="only the first N items per benchmark (smoke test)")
    args = ap.parse_args()
    key = os.environ.get("CLM_API_KEY", "")
    data = items(args.cache, args.seed)
    if args.limit:
        by = defaultdict(list)
        for it in data:
            by[it["bench"]].append(it)
        data = [it for v in by.values() for it in v[:args.limit]]
    runs = [(f, c) for f in ("paper", "rank") for c in (None, "content-free")]
    results, summary = {}, {}
    for framing, cal in runs:
        name = f"{framing}/{cal or 'uncalibrated'}"
        todo = [it for it in data if not (framing == "rank" and it["kind"] == "hall")]
        t0 = time.time()
        with ThreadPoolExecutor(args.workers) as ex:
            out = list(ex.map(lambda it: {**it, **judge(it, framing, cal, args.base_url, key, args.model)}, todo))
        results[name] = [{k: v for k, v in r.items() if k not in ("question", "a", "b", "evidence", "answer")}
                         for r in out]
        summary[name] = {}
        for bench in ("RewardBench", "JudgeBench", "HaluEval"):
            rs = [r for r in out if r["bench"] == bench]
            if not rs:
                continue
            cats = {c: metrics([r for r in rs if r["category"] == c]) for c in sorted({r["category"] for r in rs})}
            summary[name][bench] = {**metrics(rs), "categories": cats}
        print(f"\n== {name} ({len(todo)} items, {time.time() - t0:.0f}s)", flush=True)
        for bench, m in summary[name].items():
            au = f"{m['auroc']:.3f}" if m["auroc"] is not None else "  -  "
            jev, gpt = PAPER[bench]["jev"], PAPER[bench]["gpt-6"]
            print(f"   {bench:12} acc {m['accuracy']:6.1%}  AUROC {au}  Brier {m['brier']:.3f}  failed {m['failed']}"
                  f"   | paper: Jev {jev[0]}% / {jev[1]}, GPT-6 {gpt[0]}% / {gpt[1]}")
            print("      " + "  ".join(f"{c} {cm['accuracy']:.0%}" for c, cm in m["categories"].items()))
            print("      accept q>=t: " + "  ".join(
                f"{s['threshold']}: {s['coverage']:.0%} @ {s['accuracy']:.0%}" if s["accuracy"] is not None else
                f"{s['threshold']}: 0%" for s in m["selective"]))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump({"model": args.model, "seed": args.seed, "summary": summary, "items": results}, open(args.out, "w"))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
