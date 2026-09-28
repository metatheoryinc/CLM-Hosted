"""CLM vs Jev (TypeSafe) on our three labelled sets: the same state and question to both, held-out CLM heads.

    jev_compare.py ask [--limit N]   ask Jev (cached in jev/cache.sqlite, so re-runs are free)
    jev_compare.py report            score CLM's held-out heads and Jev side by side

Sets: subagent tiers (tierdata_v2: 295 real calls, held out by project, + 15 hand-written),
tool calls (tooldata: Fable labels, held out by project, population-weighted), behaviors (the
Respan benchmark's held-out 25% of traces, rendered as the hook renders them).
"""
import collections
import hashlib
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor

S = os.environ.get("CLM_EVAL_DATA", ".")        # tierdata_v2/, tooldata/, behavior/, runs/ (not in the repo)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in ("src", "train", "examples", "integrations/claude_code"):
    sys.path.insert(0, os.path.join(REPO, p))
os.makedirs(f"{S}/jev", exist_ok=True)
TIERS = ("haiku", "sonnet", "opus")
TIER_FOLDS = {"A": ["kb-1-cloud"], "B": ["mafia-who-cried-wolf-benchmark"],
              "C": ["atmos-mind-gym", "mdm-copilot", "CLM-Hosted"],
              "D": ["coworld", "tofu-tech--claude-worktrees-mafia-game-mobile-friendly-565cf1", "kb-1"]}
PER_REQUEST = 16                       # behavior questions per Jev request (one trace)


# ── the three sets, as (id, state, question) plus gold ───────────────────────

def tier_items():
    rows = json.load(open(f"{S}/tierdata_v2/rows.json"))
    out = []
    for split in ("real", "ood"):
        for r in rows[split]:
            out.append({"set": "subagents", "split": split, "id": r["id"], "project": r.get("project"),
                        "state": json.loads(r["state"]), "question": json.loads(r["questions"])["route"],
                        "gold": r["tier"], "fold": next((k for k, ps in TIER_FOLDS.items() if r.get("project") in ps), None)})
    return out


def tool_items():
    d = json.load(open(f"{S}/tooldata/rows.json"))
    fold_of = {p: k for k, ps in d["folds"].items() for p in ps}
    return [{"set": "tools", "id": r["id"], "state": r["state"], "question": r["questions"]["route"],
             "gold": r["tier"], "weight": r["weight"], "fold": fold_of[r["project"]]} for r in d["recs"]]


def behavior_items():
    rows = [r for r in json.load(open(f"{S}/behavior/rows.json")) if r["split"] == "test"]
    return [{"set": "behaviors", "id": r["id"], "trace": r["trace"], "suite": r["suite"], "state": r["state"],
             "question": r["questions"]["route"], "gold": r["label"], "seen": r["seen_behavior"],
             "type": r["behavior_type"]} for r in rows]


# ── Jev ───────────────────────────────────────────────────────────────────────

def ask(limit=None):
    from common import Judge
    jev = Judge("jev", cache=f"{S}/jev/cache.sqlite", timeout=180)
    rng = random.Random(0)
    tiers, tools, beh = tier_items(), tool_items(), behavior_items()
    if limit:
        tiers, tools = rng.sample(tiers, min(limit, len(tiers))), rng.sample(tools, min(limit, len(tools)))
        keep = set(rng.sample(sorted({b["trace"] for b in beh}), max(1, limit // 20)))
        beh = [b for b in beh if b["trace"] in keep]
    # one request per decision for tiers and tools; one per trace (chunks of PER_REQUEST) for behaviors
    jobs = [(x["state"], {"route": x["question"]}, [(x["id"], "route")]) for x in tiers + tools]
    by_trace = collections.defaultdict(list)
    for b in beh:
        by_trace[b["trace"]].append(b)
    for bs in by_trace.values():
        for i in range(0, len(bs), PER_REQUEST):
            chunk = bs[i:i + PER_REQUEST]
            jobs.append((chunk[0]["state"], {f"q{j}": b["question"] for j, b in enumerate(chunk)},
                         [(b["id"], f"q{j}") for j, b in enumerate(chunk)]))
    print(f"{len(jobs)} Jev requests ({len(tiers)} subagent, {len(tools)} tool, {len(beh)} behavior questions)", flush=True)
    out, done, errors = {}, 0, 0

    def one(job):
        state, qs, ids = job
        try:
            r = jev.ask(state, qs)
            return [(i, {"probabilities": r.answers[q]["probabilities"], "latency_ms": r.latency_ms,
                         "model": r.model, "cached": r.cached, "n_questions": len(qs)}) for i, q in ids]
        except Exception as e:  # noqa: BLE001
            return [(i, {"error": f"{type(e).__name__}: {e}"[:300]}) for i, _ in ids]

    with ThreadPoolExecutor(8) as ex:
        for res in ex.map(one, jobs):
            for i, a in res:
                out[i] = a
                errors += "error" in a
            done += 1
            if done % 100 == 0:
                print(f"   {done}/{len(jobs)}  errors {errors}", flush=True)
    prev = json.load(open(f"{S}/jev/answers.json")) if os.path.exists(f"{S}/jev/answers.json") else {}
    prev.update(out)
    json.dump(prev, open(f"{S}/jev/answers.json", "w"))
    print(f"done: {len(out)} answers, {errors} errors; Jev {json.dumps(jev.stats.summary())}")


# ── CLM's held-out heads ─────────────────────────────────────────────────────

def clm_probs():
    import numpy as np
    import torch
    from adapters import typed_decision_examples
    from clm.heads import HeadPair

    def run(cache_npz, rows_typed, head):
        z = np.load(cache_npz)
        cache = dict(zip(z["keys"].tolist(), z["vecs"].astype(np.float32)))
        hp = HeadPair("h", head, "cpu").ensure()
        out = {}
        for r, e in zip(rows_typed, typed_decision_examples(rows_typed)):
            v = lambda t: cache[hashlib.sha1(t.encode()).hexdigest()]              # noqa: E731
            a = hp.project_actions(np.stack([v(c) for c in e.candidates]))
            out[r["id"]] = dict(zip(e.keys, torch.softmax(hp.scale * (a @ hp.project_states(v(e.state_text)[None])[0]), -1).tolist()))
        return out

    typed = lambda x: {"id": x["id"], "workflow": "w", "state": x["state"] if isinstance(x["state"], str) else json.dumps(x["state"]),  # noqa: E731
                       "questions": json.dumps({"route": x["question"]}), "gold": json.dumps({"route": {"label": x["gold"]}})}
    probs = {}
    tiers = tier_items()
    for k in "ABCD":
        probs.update(run(f"{S}/tierdata_v2/cache/choice_Qwen_Qwen3-8B_2048.npz",
                         [typed(x) for x in tiers if x["split"] == "real" and x["fold"] == k], f"{S}/runs/v2/fold{k}/best_head.pt"))
    probs.update(run(f"{S}/tierdata_v2/cache/choice_Qwen_Qwen3-8B_2048.npz",
                     [typed(x) for x in tiers if x["split"] == "ood"], f"{S}/runs/v2final/best_head.pt"))
    tools = tool_items()
    for k in sorted({x["fold"] for x in tools}):
        probs.update(run(f"{S}/tooldata/cache/choice_Qwen_Qwen3-8B_2048.npz",
                         [typed(x) for x in tools if x["fold"] == k], f"{S}/runs/tool_small/fold{k}/best_head.pt"))
    probs.update(run(f"{S}/behavior/cache/choice_Qwen_Qwen3-8B_2048.npz",
                     [typed(x) for x in behavior_items()], f"{S}/runs/beh_warm_bp05/best_head.pt"))
    return probs


# ── scoring ───────────────────────────────────────────────────────────────────

def pick(p):
    return max(p, key=p.get)


def auroc(scores, pos):
    P = [s for s, y in zip(scores, pos) if y]
    N = [s for s, y in zip(scores, pos) if not y]
    if not P or not N:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in P for n in N) / (len(P) * len(N))


def f1(pairs):
    tp = sum(p == "present" and g == "present" for p, g in pairs)
    fp = sum(p == "present" and g != "present" for p, g in pairs)
    fn = sum(p != "present" and g == "present" for p, g in pairs)
    return 2 * tp / (2 * tp + fp + fn) if tp else 0.0


def report():
    import numpy as np
    jev = json.load(open(f"{S}/jev/answers.json"))
    clm = clm_probs()
    models = {"CLM": clm, "Jev": {k: v["probabilities"] for k, v in jev.items() if "probabilities" in v}}

    def both(items):
        return [x for x in items if x["id"] in models["Jev"] and x["id"] in clm]

    def conf(name, xs):
        m = models[name]
        return auroc([max(m[x["id"]].values()) for x in xs], [pick(m[x["id"]]) == x["gold"] for x in xs])

    lat = [v["latency_ms"] for v in jev.values() if v.get("latency_ms") and not v.get("cached")]
    errs = sum("error" in v for v in jev.values())
    print(f"Jev answers {len(models['Jev'])} ({errs} errors); Jev latency p50 "
          f"{np.median(lat) if lat else float('nan'):.0f} ms per request (no-cache requests only)")

    xs = both([x for x in tier_items() if x["split"] == "real"])
    print(f"\n== subagent tiers: {len(xs)} real calls held out by project")
    for n in ("CLM", "Jev"):
        m = models[n]
        rec = {t: np.mean([pick(m[x["id"]]) == t for x in xs if x["gold"] == t]) for t in TIERS if any(x["gold"] == t for x in xs)}
        down = [x for x in xs if pick(m[x["id"]]) != "opus" and max(m[x["id"]].values()) >= 0.95]
        print(f"   {n}: balanced {np.mean(list(rec.values())):.1%}  recall " + " ".join(f"{t} {v:.0%}" for t, v in rec.items())
              + f"  | conf AUROC {conf(n, xs) or float('nan'):.2f} | at p>=0.95: downgrades {len(down)}, of which gold opus "
              f"{sum(x['gold'] == 'opus' for x in down)}")
    ood = both([x for x in tier_items() if x["split"] == "ood"])
    print("   hand-written 15: " + ", ".join(f"{n} {sum(pick(models[n][x['id']]) == x['gold'] for x in ood)}/{len(ood)}" for n in models))
    agree = np.mean([pick(clm[x["id"]]) == pick(models["Jev"][x["id"]]) for x in xs])
    print(f"   CLM and Jev agree on {agree:.0%}")

    xs = both(tool_items())
    W = lambda ys: sum(y["weight"] for y in ys)                                   # noqa: E731
    print(f"\n== tool calls: {len(xs)} Fable-labelled, held out by project (weighted to the population)")
    for n in ("CLM", "Jev"):
        m = models[n]
        rec = {t: np.mean([pick(m[x["id"]]) == t for x in xs if x["gold"] == t]) for t in ("allow", "review")}
        line = f"   {n}: balanced (allow/review) {np.mean(list(rec.values())):.1%}  | conf AUROC {conf(n, xs) or float('nan'):.2f}"
        for t in (0.5, 0.7, 0.9):
            fl = [x for x in xs if pick(m[x["id"]]) != "allow" and max(m[x["id"]].values()) >= t]
            rv, al = [x for x in xs if x["gold"] == "review"], [x for x in xs if x["gold"] == "allow"]
            line += (f"  | t={t}: catches {W([x for x in fl if x['gold'] == 'review']) / W(rv):.0%} of review, "
                     f"{W([x for x in fl if x['gold'] == 'allow']) / W(al) * 100:.1f} false/100")
        print(line)
    print(f"   CLM and Jev agree on {np.mean([pick(clm[x['id']]) == pick(models['Jev'][x['id']]) for x in xs]):.0%}")

    xs = both(behavior_items())
    print(f"\n== behaviors: {len(xs)} held-out benchmark rows ({len({x['trace'] for x in xs})} traces); F1 on present")
    for n in ("CLM", "Jev"):
        m = models[n]
        parts = {s: f1([(pick(m[x["id"]]), x["gold"]) for x in xs if x["suite"] == s]) for s in ("core", "multilingual")}
        unseen = [x for x in xs if not x["seen"]]
        prec = [x for x in xs if m[x["id"]]["present"] >= 0.9]
        print(f"   {n}: pooled {f1([(pick(m[x['id']]), x['gold']) for x in xs]):.3f}  core {parts['core']:.3f}  "
              f"multilingual {parts['multilingual']:.3f}  unseen {f1([(pick(m[x['id']]), x['gold']) for x in unseen]):.3f}"
              f"  | precision at p>=0.9 {np.mean([x['gold'] == 'present' for x in prec]) if prec else float('nan'):.0%} "
              f"({len(prec)})  | conf AUROC {conf(n, xs) or float('nan'):.2f}")
    bt = collections.defaultdict(list)
    for x in xs:
        bt[x["type"]].append(x)
    print("   by type (CLM / Jev): " + "  ".join(
        f"{t} {f1([(pick(clm[x['id']]), x['gold']) for x in v]):.2f}/{f1([(pick(models['Jev'][x['id']]), x['gold']) for x in v]):.2f}"
        for t, v in sorted(bt.items(), key=lambda kv: -len(kv[1]))))


if __name__ == "__main__":
    if sys.argv[1] == "ask":
        ask(int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None)
    else:
        report()
