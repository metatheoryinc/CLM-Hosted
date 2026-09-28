# Evaluations

| script | what it measures |
|---|---|
| [judge_eval.py](judge_eval.py) | CLM on the *JEV-as-a-Judge* benchmarks (RewardBench, JudgeBench, HaluEval) |
| [behavior_eval.py](behavior_eval.py) | the Respan behavior benchmark: zero-shot vs fine-tuned `behavior-v1` |
| [jev_compare.py](jev_compare.py) | CLM vs Jev on our labelled subagent, tool-call and behavior sets |
| [bon_eval.py](bon_eval.py) | best-of-n verification |

## CLM vs Jev

2026-09-28, `jev-latest` zero-shot vs our fine-tuned heads, every CLM answer from a head that never
saw the item (held out by project or by trace). Same state and question to both; labels from Fable
(subagents, tool calls) and the benchmark (behaviors). Data lives outside the repo
(`CLM_EVAL_DATA`); Jev answers are cached, 1,900 requests, 2.6M input tokens.

| | CLM | Jev | average |
|---|---|---|---|
| Subagent tiers, balanced accuracy (295) | **55.2%** | 48.1% | 59.6% |
| confident (p ≥ 0.95) downgrades of tasks needing Opus | 0 / 57 | 1 / 72 | |
| Tool calls, balanced accuracy (745) | 76.9% | 81.6% | **85.3%** |
| review-worthy calls caught at t=0.9 (false flags per 100) | 14% (1.2) | **51% (1.1)** | |
| Behaviors, F1 on present (8,699) | 0.648 | 0.626 | **0.736** |
| behaviors not seen in training | 0.42 | **0.80** | 0.71 |
| confidence AUROC, subagents / tools / behaviors | 0.59 / 0.78 / 0.85 | 0.78 / 0.84 / 0.84 | |
| latency p50 per request (from a laptop) | ~190–240 ms | ~210 ms | |

Jev read the same 5600-character renderings as CLM; on the full traces its published F1 is 0.715,
and its low core score here (0.53, multilingual 0.84) is mostly truncation. What the hook does with
this: behavior checks act on the average (from 0.7: 90% precise, 46% recall), unsure tool calls go to
Jev, subagent tiers stay CLM only ([integrations/claude_code](../integrations/claude_code/README.md#with-a-jev-key)).
