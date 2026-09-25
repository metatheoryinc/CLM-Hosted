"""CLM on the Respan Behavior Benchmark (respanai/behavior-benchmark): zero-shot vs fine-tuned.

    BEHAVIOR_DIR=<dir with core.parquet, multilingual.parquet>  CLM_API_KEY=...
    python evaluation/behavior_eval.py build        render traces, 75/25 split by trace, embed via /v1/encoder
    python train/finetune.py --task choice --data $BEHAVIOR_DIR/split --workflow behavior \
        --embed-cache $BEHAVIOR_DIR/cache --init-ckpt <clm-latest .pt> --balance --balance-power 0.5 \
        --select-metric balanced_acc --patience 15 --epochs 40 --out-dir runs/behavior
    python evaluation/behavior_eval.py eval <clm-latest .pt> runs/behavior/best_head.pt ...

``eval`` reports the benchmark card's metric, F1 on ``present``, for core, multilingual and pooled
(95% bootstrap over traces), at the head's argmax and at a p(present) threshold cross-fitted on
the other half of the test traces. Traces are rendered by the Claude Code hook's own renderer
(integrations/claude_code/clm_behaviors.py), so a head trained here reads live traces the same way.

Result (2026-09-25, behavior-v1 = the bp 0.5 warm start): pooled F1 0.261 zero-shot -> 0.664
(Jev 0.715, Span-01 0.843 on the published card, zero-shot on the full benchmark).
"""
import base64
import collections
import hashlib
import json
import os
import random
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B = os.environ.get("BEHAVIOR_DIR", "behavior")
URL = os.environ.get("CLM_BASE_URL", "https://clm.metatheory.dev")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "train"))
sys.path.insert(0, os.path.join(ROOT, "integrations", "claude_code"))
import clm_behaviors as CB  # noqa: E402

PAPER = {"Span-01": 0.843, "GPT-5.6 Terra": 0.837, "Span-01 Lite": 0.761, "Sonnet 5": 0.717, "Jev": 0.715}


def render(r):
    return CB.render(r.get("metadata"), r.get("input"), r.get("output"))


def question(r):
    return {"route": CB.question(r["behavior_definition"])}


def build():
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    from adapters import typed_decision_examples
    from finetune import _slug
    rng = random.Random(0)
    rows = []
    for suite in ("core", "multilingual"):
        t = pq.read_table(f"{B}/{suite}.parquet").to_pylist()
        traces = sorted({r["task_id"] for r in t})
        rng.shuffle(traces)
        test = set(traces[:len(traces) // 4])
        for r in t:
            rows.append({"id": r["cell_id"], "trace": r["task_id"], "suite": suite, "source": r["source"],
                         "behavior_type": r["behavior_type"], "def_id": r["def_id"], "label": r["label"],
                         "split": "test" if r["task_id"] in test else "train",
                         "state": render(r), "questions": question(r)})
    train_defs = {r["def_id"] for r in rows if r["split"] == "train"}
    for r in rows:
        r["seen_behavior"] = r["def_id"] in train_defs
    typed = lambda r: {"id": r["id"], "workflow": "behavior", "state": r["state"],                     # noqa: E731
                       "questions": json.dumps(r["questions"]), "gold": json.dumps({"route": {"label": r["label"]}})}
    # content-free states for calibration, per distinct question
    from clm.schema import state_text
    cf_texts = {state_text(cf, r["questions"]["route"]["instructions"]) or "N/A"
                for r in rows for cf in ("N/A", "")}
    texts = sorted({t for e in typed_decision_examples([typed(r) for r in rows]) for t in (e.state_text, *e.candidates)})
    texts = sorted(set(texts) | cf_texts)          # a trace's questions sort together: prefix cache hits
    key = os.environ["CLM_API_KEY"]
    vecs = []
    for i in range(0, len(texts), 64):
        req = urllib.request.Request(URL + "/v1/encoder", method="POST",
                                     data=json.dumps({"texts": texts[i:i + 64]}).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                              "User-Agent": "behavior/1"})
        for attempt in range(5):
            try:
                vecs += json.load(urllib.request.urlopen(req, timeout=600))["embeddings"]
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 4:
                    raise
                print("retry", i, e, flush=True)
        if i % 3200 == 0:
            print(f"embedded {i + 64}/{len(texts)}", flush=True)
    V = np.stack([np.frombuffer(base64.b64decode(v), dtype=np.float32) for v in vecs])
    os.makedirs(f"{B}/cache", exist_ok=True)
    np.savez(f"{B}/cache/choice_{_slug('Qwen/Qwen3-8B')}_2048.npz",
             keys=np.array([hashlib.sha1(t.encode()).hexdigest() for t in texts]), vecs=V.astype(np.float16))
    d = f"{B}/split/behavior"
    os.makedirs(d, exist_ok=True)
    for split in ("train", "test"):
        pq.write_table(pa.Table.from_pylist([typed(r) for r in rows if r["split"] == split]), f"{d}/{split}-00000.parquet")
    json.dump(rows, open(f"{B}/rows.json", "w"))
    c = collections.Counter((r["suite"], r["split"]) for r in rows)
    print(f"{len(texts)} texts embedded; rows {dict(c)}; unseen-behavior test rows "
          f"{sum(r['split'] == 'test' and not r['seen_behavior'] for r in rows)}")


def f1(pairs):
    tp = sum(p == "present" and g == "present" for p, g in pairs)
    fp = sum(p == "present" and g != "present" for p, g in pairs)
    fn = sum(p != "present" and g == "present" for p, g in pairs)
    return 2 * tp / (2 * tp + fp + fn) if tp else 0.0


def boot(rows, pred, n=1000, seed=0):
    by = collections.defaultdict(list)
    for r in rows:
        by[r["trace"]].append((pred[r["id"]], r["label"]))
    keys, rng, vals = list(by), random.Random(seed), []
    for _ in range(n):
        vals.append(f1([x for k in (rng.choice(keys) for _ in keys) for x in by[k]]))
    vals.sort()
    return vals[int(0.025 * n)], vals[int(0.975 * n)]


def evaluate(ref, heads):
    import numpy as np
    import torch
    from adapters import typed_decision_examples
    from clm.heads import HeadPair
    from clm.schema import state_text
    rows = [r for r in json.load(open(f"{B}/rows.json")) if r["split"] == "test"]
    z = np.load(f"{B}/cache/choice_Qwen_Qwen3-8B_2048.npz")
    cache = dict(zip(z["keys"].tolist(), z["vecs"].astype(np.float32)))
    vec = lambda t: cache[hashlib.sha1(t.encode()).hexdigest()]                          # noqa: E731
    typed = lambda r: {"id": r["id"], "workflow": "behavior", "state": r["state"],        # noqa: E731
                       "questions": json.dumps(r["questions"]), "gold": json.dumps({"route": {"label": r["label"]}})}
    exs = list(typed_decision_examples([typed(r) for r in rows]))

    def predict(path, calibrate=False):
        hp = HeadPair("h", path, "cpu").ensure()
        pred = {}
        for r, e in zip(rows, exs):
            a = hp.project_actions(np.stack([vec(c) for c in e.candidates]))
            lg = hp.scale * (a @ hp.project_states(vec(e.state_text)[None])[0])
            if calibrate:
                ins = r["questions"]["route"]["instructions"]
                cf = [state_text(s, ins) or "N/A" for s in ("N/A", "")]
                lg = lg - sum(hp.scale * (a @ hp.project_states(vec(t)[None])[0]) for t in cf) / 2
            pred[r["id"]] = dict(zip(e.keys, torch.softmax(lg, -1).tolist()))
        return pred

    def argmax(probs):
        return {k: max(p, key=p.get) for k, p in probs.items()}

    def crossfit(probs):
        """present when p(present) >= t, t tuned for F1 on the other half of the test traces."""
        half = lambda r: int(hashlib.sha1(r["trace"].encode()).hexdigest(), 16) % 2           # noqa: E731
        grid = [i / 100 for i in range(5, 96)]
        out, ts = {}, []
        for h in (0, 1):
            tune = [r for r in rows if half(r) != h]
            lab = lambda r, t: "present" if probs[r["id"]]["present"] >= t else "other"         # noqa: E731
            t = max(grid, key=lambda t: f1([(lab(r, t), r["label"]) for r in tune]))
            ts.append(t)
            out.update({r["id"]: "present" if probs[r["id"]]["present"] >= t else
                        max((k for k in probs[r["id"]] if k != "present"), key=probs[r["id"]].get)
                        for r in rows if half(r) == h})
        return out, ts

    def show(name, pred):
        out = {}
        for label, rs in (("core", [r for r in rows if r["suite"] == "core"]),
                          ("multilingual", [r for r in rows if r["suite"] == "multilingual"]), ("pooled", rows)):
            lo, hi = boot(rs, pred)
            out[label] = f"{f1([(pred[r['id']], r['label']) for r in rs]):.3f} [{lo:.3f}, {hi:.3f}]"
        seen = [r for r in rows if r["seen_behavior"]]
        unseen = [r for r in rows if not r["seen_behavior"]]
        no = [r for r in rows if r["label"] == "not_observable"]
        print(f"== {name}\n   F1(present)  core {out['core']}   multilingual {out['multilingual']}   pooled {out['pooled']}")
        print(f"   seen behaviors {f1([(pred[r['id']], r['label']) for r in seen]):.3f} ({len(seen)})   "
              f"unseen {f1([(pred[r['id']], r['label']) for r in unseen]):.3f} ({len(unseen)})   "
              f"not_observable recall {np.mean([pred[r['id']] == 'not_observable' for r in no]):.0%} ({len(no)})   "
              f"picks {dict(collections.Counter(pred.values()))}")
        bt = collections.defaultdict(list)
        for r in rows:
            bt[r["behavior_type"]].append((pred[r["id"]], r["label"]))
        print("   by type: " + "  ".join(f"{k} {f1(v):.2f}" for k, v in sorted(bt.items(), key=lambda kv: -len(kv[1]))))

    print("published pooled F1(present): " + ", ".join(f"{k} {v}" for k, v in PAPER.items()))
    for name, probs in [("zero-shot clm-latest", predict(ref)),
                        ("zero-shot clm-latest, content-free", predict(ref, calibrate=True))] + \
            [(f"trained {h}", predict(h)) for h in heads]:
        show(f"{name} [argmax]", argmax(probs))
        pred, ts = crossfit(probs)
        show(f"{name} [p(present) >= t, t cross-fitted: {ts}]", pred)


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build()
    else:
        evaluate(sys.argv[2], sys.argv[3:])
