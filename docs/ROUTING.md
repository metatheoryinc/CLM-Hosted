# Routing with CLM

Many agent decisions are "which of these workers should take this next?". An LLM
call answers that today; CLM can answer it in milliseconds without generating
anything. `clm.decisions` lets you find out whether it answers *well enough* for
your router before it changes what your agent does:

1. **Shadow**: your router keeps deciding. CLM is asked the same question in the
   background and both answers are logged. Nothing about the agent changes.
2. **Label**: report outcomes (the hand-off was right, wrong, or should have gone
   to X). Those become the gold labels.
3. **Measure**: `clm-decisions report` shows agreement, accuracy on gold, whether
   CLM's probability can be trusted, and what each threshold would do.
4. **Go active**: CLM decides above the threshold the report supports; below it,
   or on any error, your router does.
5. **Fine-tune** (optional): export the labelled decisions and train a choice head
   on them (`train/finetune.py --task choice`).

## Install

The client needs only `requests` and `numpy` (no torch, no GPU):

```bash
pip install "clm @ git+https://github.com/metatheoryinc/CLM-Hosted"
```

```bash
export CLM_BASE_URL=https://clm.metatheory.dev
export CLM_API_KEY=<your agent key>
```

Each agent (or service) should have its own key; ask whoever runs the
[infra](../infra/README.md) stack to add one to `agentKeys`.

## 1. Shadow your router

```python
from clm.decisions import HttpSink, Router

router = Router(
    "chief-of-staff",                                   # one name per decision point
    "Choose which worker should act next on this task.",
    sink=HttpSink.from_env(),                           # logs to the collector
)

WORKERS = {
    "researcher": "Finds and collects sources the task still needs.",
    "writer": "Writes the draft from sources that are already collected.",
    "reviewer": "Checks a finished, saved draft before it goes to a person.",
}

state = {
    "task": task.description,
    "progress": task.progress_summary(),               # what has been done so far
    "evidence": [s.title for s in task.sources],        # what the decision depends on
}
choice = current_llm_router(task)                        # your existing router, unchanged
d = router.route(state, WORKERS, baseline=choice)
dispatch(d.worker, task)                                 # == choice in shadow mode
```

`route` returns immediately in shadow mode; the CLM call and the upload happen
on background threads and never raise into your agent. Call `router.flush()`
before a short-lived process (a script, a Lambda) exits so they finish.

The options can change on every call (a "dynamic menu"): pass only the workers
that are actually available for this task, right now.

## 2. Report outcomes

Whenever your system learns whether a hand-off was right, say so:

```python
router.outcome(d, ok=True)                     # the chosen worker was right
router.outcome(d, ok=False, label="writer")    # wrong; it should have gone to the writer
router.outcome(d, ok=False)                    # wrong; the right one is unknown
```

Good sources of outcomes: a worker handing a task back, a task being reassigned,
a reviewer correcting a route, a run failing at the first step. Keep `d.id` with
the task if the outcome arrives later (`router.outcome(task.decision_id, ...)`
works from any process with the same key).

A labelled outcome is what makes the measurement trustworthy: without them the
report can only show how often CLM agrees with your current router, not which of
the two was right when they disagree.

## 3. Measure

```bash
clm-decisions report https://clm.metatheory.dev
clm-decisions report https://clm.metatheory.dev --workflow routing/chief-of-staff
```

Per router (`workflow`), it shows:

* **agreement** with your current router, and the most common disagreements;
* **accuracy on gold** for CLM and for your router, on the decisions with a known
  right answer;
* **calibration**: for each band of CLM probability, how often it agreed / was
  right. If 0.9+ answers are right far less than 90% of the time, don't trust the
  probability yet;
* **thresholds**: at each threshold, the share of decisions CLM would take, how good
  those are, and the accuracy of "CLM above the threshold, your router below".

Before going active, look for: a few hundred labelled decisions, the
"CLM + router" column at or above your router alone at the threshold you pick, and
a threshold that still lets CLM take a useful share.

## 4. Go active

```python
router = Router("chief-of-staff", "...", sink=HttpSink.from_env(),
                mode="active", threshold=0.9)          # from the report
d = router.route(state, WORKERS, baseline=lambda: current_llm_router(task))
```

With a callable baseline your LLM router runs only when CLM is below the threshold,
errors, or times out (2 s by default). Keep logging and reporting outcomes: the
report keeps working in active mode, and `d.acted` says who decided.

## Writing good options and state

CLM scores the state against each option's description; it does not reason about
them. What it responds to:

* **One idea per option.** "Goal unclear, outside scope, or work complete" packs
  three conditions into one option, and CLM almost never picks such options. Split
  them (`clarify`, `decline`, `done`) and map them back in code.
* **Describe the worker's job**, not the condition for choosing it, in the same
  terms the state uses ("writes the draft from collected sources").
* **Put the evidence in the state**: the sources found, the gaps left, the last
  worker's result. "The researcher finished" says less than what it found.
* **Keep the state's fields in a fixed order.** Reordering the same fields can
  change the answer; build the state the same way every time.
* **Keep hard rules in code**: action and spending limits, and anything needing
  approval (publishing, sending, deleting), should not depend on any classifier.

## 5. Fine-tune on your decisions

Labelled decisions are typed-decision training rows. Export them and train a head
(GPU; see [FINETUNING.md](FINETUNING.md)):

```bash
clm-decisions export https://clm.metatheory.dev --out data/routing
python train/finetune.py --task choice --data data/routing --workflow routing/chief-of-staff \
    --init-ckpt ckpts/CLM_v0.1-8B.pt --out-dir runs/routing
```

`--labels baseline` also exports unlabelled decisions with your current router's
choice as the label, which trains CLM to imitate that router.

## In Claude Code

[integrations/claude_code](../integrations/claude_code/README.md) applies the same shadow /
measure / active loop to Claude Code's tool calls (allow / review / block) with a hook.

## Without the hosted collector

`JsonlSink("decisions.jsonl")` logs to a local file instead, against any
`clm-serve`; `clm-decisions report decisions.jsonl` and `export` read those files.

## What is stored

Every decision's `state`, options, both answers and outcomes are stored in the
stack's Cloudflare D1 database, tagged with the agent that sent them. Don't put
secrets or personal data you would not store there into `state`.
