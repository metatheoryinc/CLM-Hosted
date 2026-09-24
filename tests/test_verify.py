"""POST /v1/verify: parsing, windowing, scoring and the HTTP layer, with a fake encoder."""
import hashlib

import numpy as np
import pytest
import torch

from clm import verify as V
from clm.engine import Engine, ModelNotFound
from clm.heads import HIDDEN, HeadPair, make_head


class FakeRecipe:
    """Token ids = utf-8 bytes, so tests can see exactly what reached the encoder."""

    def state_ids(self, state):
        text = state if isinstance(state, str) else "|".join(f"{m['role']}:{m['content']}" for m in state)
        return list(text.encode())[-8191:]

    def text_ids(self, text, keep="head"):
        ids = list(text.encode()) or [32]
        return ids[:8191] if keep == "head" else ids[-8191:]


class FakeEmbedder:
    """Deterministic unit vectors per token-id list; records every input it embeds."""

    def __init__(self):
        self.seen = []

    def embed_ids(self, id_lists):
        self.seen.extend(id_lists)
        out = []
        for ids in id_lists:
            seed = int.from_bytes(hashlib.sha1(bytes(ids)).digest()[:4], "little")
            v = np.random.default_rng(seed).standard_normal(HIDDEN).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out), sum(len(i) for i in id_lists)

    def healthy(self):
        return True


@pytest.fixture
def head(tmp_path):
    torch.manual_seed(0)
    cfg = dict(width=64, depth=3, projection_dim=32, activation="gelu", layernorm=True, residual=False)
    kw = dict(width=64, depth=3, proj=32, layernorm=True)
    path = tmp_path / "head.pt"
    torch.save({"state_head": make_head(**kw).state_dict(), "action_head": make_head(**kw).state_dict(),
                "logit_scale": torch.tensor(2.0), "cfg": cfg}, path)
    return HeadPair("deepswe", str(path), "cpu").ensure()


@pytest.fixture
def engine(head):
    e = Engine.__new__(Engine)          # skip the reference-head download and GPU arena
    e.embedder, e.heads, e.device, e.arena = FakeEmbedder(), {"deepswe": head}, "cpu", None
    e._recipe, e.tokenizer, e.verify_max_len = FakeRecipe(), "fake", 8192
    import threading
    e._recipe_lock = threading.Lock()
    return e


def traj(tid, n, tag=""):
    return {"id": tid, "steps": [{"state": [{"role": "user", "content": f"{tid}{tag} step {i}"}],
                                  "action": f"{tid}{tag} action {i}"} for i in range(n)]}


def expected_score(head, recipe, t, window):
    steps = t["steps"][-window:]
    emb = FakeEmbedder()
    se, _ = emb.embed_ids([recipe.state_ids(s["state"]) for s in steps])
    ae, _ = emb.embed_ids([recipe.text_ids(s["action"]) for s in steps])
    zs = torch.nn.functional.normalize(head.state_head(torch.from_numpy(se)), dim=-1)
    za = torch.nn.functional.normalize(head.action_head(torch.from_numpy(ae)), dim=-1)
    return (zs * za).sum(-1).mean().item()


def test_scores_are_final_window_means_and_best_is_argmax(engine, head):
    body = {"trajectories": [traj("a", 20), traj("b", 5), traj("c", 13)], "window": 12}
    out = engine.verify(body)
    assert out["model"] == "deepswe" and out["window"] == 12
    got = {t["id"]: t["score"] for t in out["trajectories"]}
    for t in body["trajectories"]:
        assert got[t["id"]] == pytest.approx(expected_score(head, FakeRecipe(), t, 12), abs=1e-5)
    assert out["best"] == max(got, key=got.get)
    assert [t["steps"] for t in out["trajectories"]] == [20, 5, 13]
    assert [len(t["step_scores"]) for t in out["trajectories"]] == [12, 5, 12]


def test_only_the_final_window_is_embedded(engine):
    engine.verify({"trajectories": [traj("a", 30)], "window": 4})
    texts = {bytes(i).decode() for i in engine.embedder.seen}
    assert {f"a action {i}" for i in range(26, 30)} <= texts
    assert not any(f"a action {i}" == t for i in range(26) for t in texts)
    assert len(engine.embedder.seen) == 8          # 4 states + 4 actions


def test_default_window_is_the_released_twelve(engine):
    out = engine.verify({"trajectories": [traj("a", 40)]})
    assert out["window"] == 12 and len(out["trajectories"][0]["step_scores"]) == 12


def test_ties_pick_the_first_listed(engine):
    t = traj("x", 3)
    out = engine.verify({"trajectories": [dict(t, id="first"), dict(t, id="second")]})
    assert out["best"] == "first"


def test_string_states_are_accepted(engine):
    out = engine.verify({"trajectories": [{"steps": [{"state": "plain text context", "action": "ls"}]}]})
    assert out["best"] == "0"


@pytest.mark.parametrize("body, message", [
    ({}, "trajectories must be a list"),
    ({"trajectories": []}, "trajectories must be a list"),
    ({"trajectories": [traj("a", 1)] * 33}, "1..32"),
    ({"trajectories": [{"steps": []}]}, "steps must be a list"),
    ({"trajectories": [{"steps": [{"state": [], "action": "x"}]}]}, "non-empty list of chat messages"),
    ({"trajectories": [{"steps": [{"state": [{"content": "no role"}], "action": "x"}]}]}, "message {role, content}"),
    ({"trajectories": [{"steps": [{"state": "s", "action": 3}]}]}, "string action"),
    ({"trajectories": [traj("a", 1)], "window": 0}, "window must be"),
    ({"trajectories": [traj("a", 1)], "window": True}, "window must be"),
    ({"trajectories": [traj("a", 1), traj("a", 2)]}, "unique"),
])
def test_malformed_requests_are_rejected(engine, body, message):
    with pytest.raises(ValueError, match=message):
        engine.verify(body)


def test_unknown_verifier(engine):
    with pytest.raises(ModelNotFound):
        engine.verify({"trajectories": [traj("a", 1)]}, model="nope")


# ── HTTP ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(engine):
    from fastapi.testclient import TestClient
    from clm.server import create_app
    return TestClient(create_app(engine, api_key="k", ui=False))


def test_http_verify(client):
    h = {"Authorization": "Bearer k"}
    r = client.post("/v1/verify", json={"trajectories": [traj("a", 3), traj("b", 3)]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["best"] in {"a", "b"} and "X-CLM-Latency-Ms" in r.headers
    assert client.post("/v1/verify", json={"trajectories": []}, headers=h).status_code == 422
    assert client.post("/v1/verify", json={"trajectories": [traj("a", 1)], "model": "nope"},
                       headers=h).status_code == 422
    assert client.post("/v1/verify", content=b"not json", headers=h).status_code == 422
    assert client.post("/v1/verify", json={"trajectories": [traj("a", 1)]}).status_code == 401
