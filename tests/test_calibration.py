"""Content-free calibration: logits minus each option's lean on an empty state."""
import hashlib
import math
import threading

import numpy as np
import pytest
import torch

from clm.engine import CONTENT_FREE_STATES, Engine, calibration_of
from clm.heads import HIDDEN, HeadPair, make_head


class TextEmbedder:
    """Deterministic unit vector per text; records what it embeds."""

    def __init__(self):
        self.seen = []

    def embed(self, texts):
        self.seen.extend(texts)
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha1(t.encode()).digest()[:4], "little")
            v = np.random.default_rng(seed).standard_normal(HIDDEN).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out), len(texts)

    def healthy(self):
        return True


@pytest.fixture
def engine(tmp_path):
    torch.manual_seed(0)
    kw = dict(width=64, depth=3, proj=32)
    path = tmp_path / "head.pt"
    torch.save({"state_head": make_head(**kw).state_dict(), "action_head": make_head(**kw).state_dict(),
                "logit_scale": torch.tensor(math.log(20.0)),
                "cfg": dict(width=64, depth=3, projection_dim=32)}, path)
    e = Engine.__new__(Engine)
    e.embedder, e.device, e.arena = TextEmbedder(), "cpu", None
    e.heads = {"clm-latest": HeadPair("clm-latest", str(path), "cpu").ensure()}
    e._recipe, e._recipe_lock = None, threading.Lock()
    return e


Q = {"route": {"type": "choice", "instructions": "Which worker should act next?",
               "criteria": {"a": "Collects sources.", "b": "Writes the draft.", "c": "Reviews the draft."}}}


def logratios(answer):
    p = answer["probabilities"]
    return {k: math.log(p[k] / p["a"]) for k in p}


def test_calibrated_logits_are_the_raw_ones_minus_the_content_free_lean(engine):
    raw = engine.answer("Nothing collected yet.", Q)["answers"]["route"]
    cal = engine.answer("Nothing collected yet.", Q, calibrate="content-free")
    assert cal["calibrate"] == "content-free"
    cf = [logratios(engine.answer(s, Q)["answers"]["route"]) for s in CONTENT_FREE_STATES]
    want = {k: logratios(raw)[k] - sum(c[k] for c in cf) / len(cf) for k in "abc"}
    got = logratios(cal["answers"]["route"])
    assert got == pytest.approx(want, abs=1e-4)


def test_content_free_states_are_embedded_with_the_question(engine):
    engine.answer({"task": "x"}, Q, calibrate=True)
    assert "N/A\n\nWhich worker should act next?" in engine.embedder.seen
    assert "Which worker should act next?" in engine.embedder.seen     # the empty state: question only


def test_uncalibrated_is_unchanged_by_default(engine):
    a = engine.answer("s", Q)
    b = engine.answer("s", Q, calibrate="none")
    assert a["calibrate"] == b["calibrate"] == "none"
    assert a["answers"] == b["answers"]


def test_every_question_type_can_be_calibrated(engine):
    qs = {"n": {"type": "noul", "instructions": "Is this urgent?"},
          "s": {"type": "score", "instructions": "How angry?", "criteria": ["calm", "annoyed", "furious"]},
          **Q}
    out = engine.answer("Customer: charged twice!", qs, calibrate="content-free")["answers"]
    assert 0 <= out["n"]["noul"] <= 1 and 0 <= out["s"]["score"] <= 2
    assert sum(out["route"]["probabilities"].values()) == pytest.approx(1)


def test_rank_passes_calibration_through(engine):
    plain = engine.rank("", ["The Moon.", "Photosynthesis.", "The Sun."], "What causes tides?")
    cal = engine.rank("", ["The Moon.", "Photosynthesis.", "The Sun."], "What causes tides?", calibrate=True)
    assert {r["candidate"] for r in cal} == {r["candidate"] for r in plain}
    assert [r["prob"] for r in cal] != [r["prob"] for r in plain]


@pytest.mark.parametrize("value, want", [(None, "none"), (False, "none"), ("none", "none"),
                                         (True, "content-free"), ("content-free", "content-free")])
def test_calibration_values(value, want):
    assert calibration_of(value) == want


def test_bad_calibration_values_are_rejected(engine):
    with pytest.raises(ValueError, match="calibrate"):
        engine.answer("s", Q, calibrate="platt")


def test_http(engine):
    from fastapi.testclient import TestClient
    from clm.server import create_app
    c = TestClient(create_app(engine, api_key="k", ui=False))
    h = {"Authorization": "Bearer k"}
    r = c.post("/v1/systemone", json={"state": "s", "questions": Q, "calibrate": "content-free"}, headers=h)
    assert r.status_code == 200 and r.json()["calibrate"] == "content-free"
    assert c.post("/v1/systemone", json={"state": "s", "questions": Q, "calibrate": "x"}, headers=h).status_code == 422
    r = c.post("/v1/rank", json={"question": "q", "answers": ["x", "y"], "calibrate": True}, headers=h)
    assert r.status_code == 200


def test_encoder_endpoint_returns_the_serving_embeddings(engine):
    import base64
    from fastapi.testclient import TestClient
    from clm.server import create_app
    c = TestClient(create_app(engine, api_key="k", ui=False))
    h = {"Authorization": "Bearer k"}
    r = c.post("/v1/encoder", json={"texts": ["a", "b"]}, headers=h)
    assert r.status_code == 200 and r.json()["dim"] == HIDDEN
    got = np.frombuffer(base64.b64decode(r.json()["embeddings"][1]), dtype=np.float32)
    want, _ = TextEmbedder().embed(["b"])
    assert np.allclose(got, want[0])
    assert c.post("/v1/encoder", json={"texts": []}, headers=h).status_code == 422
    assert c.post("/v1/encoder", json={"texts": ["x"] * 257}, headers=h).status_code == 422
    assert c.post("/v1/encoder", json={"texts": ["x"]}).status_code == 401
