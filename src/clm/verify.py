"""Trajectory verification: pick the best of N agent trajectories with a process head.

This is ``evaluation/bon_eval.py`` as a service. Each step of a trajectory is a
(state, action) pair: ``state`` is the context the agent acted on (chat messages,
or text) and ``action`` the text it produced. A step's score is the cosine of the
projected state and action embeddings; a trajectory's score is the mean over its
final ``window`` steps, and the best trajectory is the highest-scoring one.

Steps are tokenized with ``clm.recipe.Recipe``, the recipe the DeepSWE heads were
trained with (8K encoder context), so only heads trained that way make sense here.
Only the final ``window`` steps are embedded: earlier steps cannot change the score.

    {"model": "deepswe", "window": 12,
     "trajectories": [{"id": "a", "steps": [{"state": [{"role": "user", "content": "..."}],
                                              "action": "..."}, ...]}, ...]}
"""
from __future__ import annotations

from typing import Any

DEFAULT_WINDOW = 12            # the released DeepSWE result's aggregation
MAX_TRAJECTORIES = 32
MAX_WINDOW = 64
MAX_STEPS = 2000


def _messages(state: Any, where: str) -> Any:
    if isinstance(state, str):
        return state
    if not isinstance(state, list) or not state:
        raise ValueError(f"{where}.state must be a non-empty list of chat messages or a string")
    for j, m in enumerate(state):
        if not (isinstance(m, dict) and isinstance(m.get("role"), str) and "content" in m):
            raise ValueError(f"{where}.state[{j}] must be a message {{role, content}}")
    return state


def parse(body: dict) -> tuple[list[str], list[list[dict]], int]:
    """-> (ids, per-trajectory scored steps, window); ValueError on malformed requests."""
    trajectories = body.get("trajectories")
    if not isinstance(trajectories, list) or not 1 <= len(trajectories) <= MAX_TRAJECTORIES:
        raise ValueError(f"trajectories must be a list of 1..{MAX_TRAJECTORIES} trajectories")
    window = body.get("window", DEFAULT_WINDOW)
    if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= MAX_WINDOW:
        raise ValueError(f"window must be an integer in 1..{MAX_WINDOW}")
    ids, scored = [], []
    for i, t in enumerate(trajectories):
        steps = t.get("steps") if isinstance(t, dict) else None
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise ValueError(f"trajectories[{i}].steps must be a list of 1..{MAX_STEPS} steps")
        ids.append(str(t.get("id", i)))
        kept = []
        for j in range(max(0, len(steps) - window), len(steps)):
            s, where = steps[j], f"trajectories[{i}].steps[{j}]"
            if not isinstance(s, dict) or not isinstance(s.get("action"), str):
                raise ValueError(f"{where} must be {{state, action}} with a string action")
            kept.append({"state": _messages(s.get("state"), where), "action": s["action"], "n": len(steps)})
        scored.append(kept)
    if len(set(ids)) != len(ids):
        raise ValueError("trajectory ids must be unique")
    return ids, scored, window


def verify(engine, recipe, head, body: dict) -> dict:
    """Score every trajectory in ``body``; ``head`` is a ``HeadPair``."""
    ids, scored, window = parse(body)
    flat = [s for steps in scored for s in steps]
    state_ids = [recipe.state_ids(s["state"]) for s in flat]
    action_ids = [recipe.text_ids(s["action"], keep="head") for s in flat]
    se, t1 = engine.embedder.embed_ids(state_ids)
    ae, t2 = engine.embedder.embed_ids(action_ids)
    cos = (head.project_states(se) * head.project_actions(ae)).sum(-1).tolist()
    out, k = [], 0
    for tid, steps in zip(ids, scored):
        step_scores = cos[k:k + len(steps)]
        k += len(steps)
        out.append({"id": tid, "score": sum(step_scores) / len(step_scores), "steps": steps[0]["n"],
                    "step_scores": step_scores})
    best = max(range(len(out)), key=lambda i: out[i]["score"])     # ties: the first listed
    return {"window": window, "best": out[best]["id"], "trajectories": out,
            "usage": {"input_tokens": t1 + t2, "encoded_steps": len(flat)}}
