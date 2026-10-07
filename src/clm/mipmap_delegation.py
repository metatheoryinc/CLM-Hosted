"""Stable CLM policy and deterministic bootstrap rows for MipMap delegation.

The foreground manager uses this only in shadow mode initially.  Keep the
state and question text stable once a head has been trained against them.
"""
from __future__ import annotations

import json
from pathlib import Path
import re


WORKFLOW = "routing/mipmap-delegation"
QID = "route"
INSTRUCTIONS = "Should MipMap answer this request in the foreground or summon a background buddy?"
OPTIONS = {
    "answer_now": "A direct response completes the request without independent multi-step work.",
    "summon_buddy": "The request needs bounded, independent multi-step work while the foreground chat stays available.",
}
MAX_REQUEST_CHARS = 1200

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?key|secret|token|password|passwd|pwd)[\"']?\s*[:=]\s*[\"']?)"
                r"[^\s\"',;]+"), r"\1[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
                r"xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"), "[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
     "[REDACTED PRIVATE KEY]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def build_state(request: str, active_buddies: int = 0) -> dict:
    """Return the exact, bounded state used for a delegation decision."""
    clipped = redact(str(request))[:MAX_REQUEST_CHARS]
    if len(str(request)) > MAX_REQUEST_CHARS:
        clipped += " …"
    return {"request": clipped, "active_buddies": max(0, int(active_buddies))}


def question() -> dict:
    return {QID: {"type": "choice", "instructions": INSTRUCTIONS, "criteria": dict(OPTIONS)}}


_SUBJECTS = (
    "a grocery list for dinner", "a meeting title", "a short thank-you note", "the difference between two terms",
    "a timezone conversion", "a packing checklist", "a recipe substitution", "a calendar wording question",
    "a one-sentence status update", "a definition", "a brief apology", "a simple estimate",
)

_FAMILIES = (
    ("quick-question", "answer_now", "Answer this directly: what should I know about {}?"),
    ("light-advice", "answer_now", "Give me concise advice about {}."),
    ("small-draft", "answer_now", "Draft one short response about {}."),
    ("explain-concept", "answer_now", "Explain {} in plain English."),
    ("everyday-choice", "answer_now", "Help me make a quick decision about {}."),
    ("summarize-provided", "answer_now", "Summarize the key point I should remember about {}."),
    ("code-investigation", "summon_buddy", "Investigate the repository for {} and report the root cause with evidence."),
    ("implementation", "summon_buddy", "Implement the requested change for {}, add focused tests, and report what changed."),
    ("external-research", "summon_buddy", "Research options for {}, compare them with sources, and recommend one."),
    ("multi-file-refactor", "summon_buddy", "Trace every use of {}, make the safe multi-file refactor, and verify it."),
    ("operational-follow-through", "summon_buddy", "Check the current state of {}, fix any issue you find, and return the result."),
    ("project-planning", "summon_buddy", "Turn {} into an implementation plan with dependencies, risks, and acceptance checks."),
)


def seed_records() -> list[dict]:
    """Return 144 family-disjoint synthetic records for the initial choice spike.

    Every third family is held out.  The ordering intentionally gives both
    labels equal representation in train and test without leaking a family.
    """
    records = []
    for family_index, (family, label, template) in enumerate(_FAMILIES):
        split = "test" if family_index % 3 == 0 else "train"
        for subject_index, subject in enumerate(_SUBJECTS):
            request = template.format(subject)
            records.append({
                "id": f"mipmap-seed-{family_index:02d}-{subject_index:02d}",
                "workflow": WORKFLOW,
                "created_at": "2026-10-07T00:00:00.000+00:00",
                "state": build_state(request),
                "questions": question(),
                "baseline": {QID: {"label": label}},
                "acted": "baseline",
                "worker": label,
                "meta": {"source": "synthetic-v1", "family": family, "split": split},
            })
    return records


def write_seed(path: str | Path) -> None:
    """Write the reproducible bootstrap corpus as JSONL for ``clm-decisions``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in seed_records()), encoding="utf-8")
