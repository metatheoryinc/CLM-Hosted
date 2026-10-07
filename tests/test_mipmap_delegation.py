import json

import pytest

from clm import mipmap_delegation as policy


def test_state_and_question_are_stable_and_redact_secrets():
    state = policy.build_state("Please rotate api_key=abcdefghijklmnop and investigate the failed deploy", active_buddies=2)

    assert state == {
        "request": "Please rotate api_key=[REDACTED] and investigate the failed deploy",
        "active_buddies": 2,
    }


@pytest.mark.parametrize("task, secret", [
    ("Investigate Authorization: Bearer abcdef0123456789xyz", "abcdef0123456789xyz"),
    ("Deploy with api_key=abc", "abc"),
    ("Use -----BEGIN RSA PRIVATE KEY-----\\nMIIEow\\n-----END RSA PRIVATE KEY-----", "MIIEow"),
])
def test_build_state_redacts_all_credential_formats(task, secret):
    state = policy.build_state(task)

    assert "[REDACTED" in state["request"]
    assert secret not in state["request"]
    assert policy.question() == {
        "route": {
            "type": "choice",
            "instructions": "Should MipMap answer this request in the foreground or summon a background buddy?",
            "criteria": {
                "answer_now": "A direct response completes the request without independent multi-step work.",
                "summon_buddy": "The request needs bounded, independent multi-step work while the foreground chat stays available.",
            },
        },
    }


def test_seed_records_are_balanced_deterministic_and_family_disjoint():
    first = policy.seed_records()
    second = policy.seed_records()

    assert first == second
    assert len(first) >= 120
    assert {r["workflow"] for r in first} == {policy.WORKFLOW}
    assert {r["questions"] == policy.question() for r in first} == {True}
    assert {r["baseline"]["route"]["label"] for r in first} == {"answer_now", "summon_buddy"}
    assert sum(r["baseline"]["route"]["label"] == "answer_now" for r in first) == len(first) // 2

    train_families = {r["meta"]["family"] for r in first if r["meta"]["split"] == "train"}
    test_families = {r["meta"]["family"] for r in first if r["meta"]["split"] == "test"}
    assert train_families and test_families and train_families.isdisjoint(test_families)
    assert all(json.loads(json.dumps(r))["state"] == r["state"] for r in first)


def test_write_seed_emits_jsonl_consumable_by_clm_decisions(tmp_path):
    out = tmp_path / "seed.jsonl"

    policy.write_seed(out)

    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert records == policy.seed_records()
