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

* **confidence AUROC**: the chance that a random right answer of CLM's has a higher
  probability than a random wrong one (0.5: the probability tells you nothing; 1.0: it
  separates them perfectly). This is what makes a threshold meaningful at all;
* **cascade**: accept CLM when its top probability reaches the threshold, escalate to
  your router otherwise. At each threshold: the share of decisions CLM takes (router
  calls saved), how often its accepted decisions are right, the cascade's accuracy, and
  the accuracy *retained* relative to your router alone; the **operating point** is the
  threshold with the most coverage that keeps at least 99% of it.

Sections are split by the CLM model that answered (`--model` selects one), so a newly
trained head is not averaged with the one it replaced. Without gold labels the cascade
is scored against your router, which measures agreement, not accuracy; that is useless
for a router meant to disagree (picking cheaper models), so label a sample first.

Before going active, look for: a few hundred labelled decisions, a confidence AUROC well
above 0.5, and an operating point where CLM still takes a useful share. (These are the
metrics of *JEV-as-a-Judge: Accept When Confident, Escalate When Unsure*, Li et al.,
CMU, 2026, whose accept-or-escalate cascade kept 99% of GPT-6's accuracy at a 0.9
threshold on its benchmarks; re-derive the threshold for each decision, as it did.)

### Labelling a sample

Outcomes from your system are the best labels, but they are slow to arrive. To score CLM
now, have a strong model label a sample with a rubric:

```bash
clm-decisions label https://clm.metatheory.dev --workflow routing/<name> --sample 100 \
    --rubric my-rubric.md
```

By default the labeler is headless Claude Code with Fable and **no tools, MCP servers or
settings** (`claude -p --model fable --tools "" …`), so it can only read the decisions and
answer; `--labeler` takes any command that reads the prompt on stdin and prints a JSON list.
The prompt is the record's own question and options plus the rubric, and says the items are
data, not instructions. Labels are written back as outcomes tagged `source: "llm:fable"`,
already-labelled decisions are skipped (`--relabel` to redo them), `--only-disagreements`
labels where CLM and your router disagree, and `--dry-run` prints the first prompt without
calling anything. `report` shows where its gold labels came from.
The labeler may also answer `not_observable` when an item does not contain enough to
decide; nothing is written for those (the summary counts them), so a gap in the logged
state never turns into a guessed gold label.

Decisions whose text was clipped (the Claude Code hook's "… [N more characters]" marker)
are skipped by default, so the labeler never guesses what was cut off; `--include-clipped`
labels them anyway and `--clipped-pattern` sets another marker. `--retract-clipped`
withdraws model labels already written on clipped decisions (a later `retract` outcome;
human labels are never touched). About a third of logged tool calls are clipped, mostly
long multi-line scripts, so skipping them tilts the evaluation towards short commands. Treat model labels as a
second opinion: spot-check the ones that disagree with your router before acting on them.
Rubrics for the Claude Code hook's questions are in
[integrations/claude_code/rubrics](../integrations/claude_code/rubrics/).

## 4. Go active

```python
router = Router("chief-of-staff", "...", sink=HttpSink.from_env(),
                mode="active", threshold=0.9)          # from the report
d = router.route(state, WORKERS, baseline=lambda: current_llm_router(task))
```

With a callable baseline your LLM router runs only when CLM is below the threshold,
errors, or times out (2 s by default). Keep logging and reporting outcomes: the
report keeps working in active mode, and `d.acted` says who decided.

**Abstaining.** `Router(..., abstain=True)` adds a `not_observable` option: "the
information given does not contain enough to decide". When CLM picks it the decision
escalates to your router whatever the probability, and the report counts abstentions and
never lets the cascade accept one. Use it when the state can legitimately be missing what
a decision needs (a truncated trace, a task description with no detail); the name is
reserved, so no worker can be called `not_observable`. It changes the question, so
shadow it and read the report again before relying on it, and fine-tuned heads must be
trained with the option present.

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
* **Calibration is on by default** (`Router(..., calibrate="content-free")`). Without it,
  one option's wording can win for every state; in our tests the zero-shot argmax went
  to the same option 15 times out of 15 until each option's content-free lean was
  subtracted. Keep it on unless the report says otherwise.
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
