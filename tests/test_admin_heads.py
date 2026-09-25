"""PUT / GET / DELETE /v1/admin/heads: serving trained heads without a restart."""
import io
import math
import os
import threading

import pytest
import torch

from clm.engine import Engine
from clm.heads import HeadPair, make_head
from test_calibration import TextEmbedder

Q = {"route": {"type": "choice", "instructions": "Which tier?",
               "criteria": {"haiku": "Looks it up.", "sonnet": "Makes a change.", "opus": "Designs it."}}}


def head_bytes(seed=0, width=32):
    torch.manual_seed(seed)
    kw = dict(width=width, depth=2, proj=16)
    buf = io.BytesIO()
    torch.save({"state_head": make_head(**kw).state_dict(), "action_head": make_head(**kw).state_dict(),
                "logit_scale": torch.tensor(math.log(10.0)),
                "cfg": dict(width=width, depth=2, projection_dim=16), "epoch": 3, "metrics": {"acc": 0.5}}, buf)
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    base = tmp_path / "base.pt"
    base.write_bytes(head_bytes(1))
    monkeypatch.setenv("CLM_HEADS_DIR", str(tmp_path / "heads"))
    e = Engine.__new__(Engine)
    e.embedder, e.device, e.arena = TextEmbedder(), "cpu", None
    e.heads = {"clm-latest": HeadPair("clm-latest", str(base), "cpu").ensure()}
    e._recipe, e._recipe_lock = None, threading.Lock()
    from fastapi.testclient import TestClient
    from clm.server import create_app
    c = TestClient(create_app(e, api_key="k", ui=False))
    c.headers["Authorization"] = "Bearer k"
    c.engine, c.dir = e, tmp_path / "heads"
    return c


def test_uploaded_heads_are_served_at_once_and_persisted(client):
    r = client.put("/v1/admin/heads/subagent-tier", content=head_bytes())
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "subagent-tier" and len(r.json()["sha256"]) == 64
    assert (client.dir / "subagent-tier.pt").exists()
    assert "subagent-tier" in [m["name"] for m in client.get("/v1/models").json()["models"]]
    out = client.post("/v1/systemone", json={"state": "find the loader", "questions": Q, "model": "subagent-tier"})
    assert out.status_code == 200 and out.json()["model"] == "subagent-tier"
    assert [h["name"] for h in client.get("/v1/admin/heads").json()["heads"]] == ["subagent-tier"]


def test_a_bad_upload_leaves_the_served_head_alone(client):
    client.put("/v1/admin/heads/tier", content=head_bytes(0))
    before = (client.dir / "tier.pt").read_bytes()
    r = client.put("/v1/admin/heads/tier", content=b"not a checkpoint")
    assert r.status_code == 422
    assert (client.dir / "tier.pt").read_bytes() == before
    assert not [f for f in os.listdir(client.dir) if f.endswith(".upload")]
    assert client.post("/v1/systemone", json={"state": "s", "questions": Q, "model": "tier"}).status_code == 200


class Exploit:
    def __reduce__(self):
        return (os.system, ("echo pwned > /tmp/clm-head-exploit",))


def test_checkpoints_cannot_run_code(client):
    buf = io.BytesIO()
    torch.save({"state_head": Exploit()}, buf)
    assert client.put("/v1/admin/heads/evil", content=buf.getvalue()).status_code == 422
    assert not os.path.exists("/tmp/clm-head-exploit")


@pytest.mark.parametrize("name", ["clm-latest", "clm-raw", "deepswe", "Bad_Name", "-x", "a" * 41])
def test_names_are_validated(client, name):
    assert client.put(f"/v1/admin/heads/{name}", content=head_bytes()).status_code == 422


def test_delete(client):
    client.put("/v1/admin/heads/tmp", content=head_bytes())
    assert client.delete("/v1/admin/heads/tmp").status_code == 200
    assert not (client.dir / "tmp.pt").exists()
    assert client.post("/v1/systemone", json={"state": "s", "questions": Q, "model": "tmp"}).status_code == 422
    assert client.delete("/v1/admin/heads/tmp").status_code == 404
    assert client.delete("/v1/admin/heads/clm-latest").status_code == 404


def test_admin_routes_need_the_key(client):
    del client.headers["Authorization"]
    assert client.put("/v1/admin/heads/x", content=head_bytes()).status_code == 401
    assert client.get("/v1/admin/heads").status_code == 401


def test_boot_skips_unloadable_heads(tmp_path):
    d = tmp_path / "heads"
    d.mkdir()
    (d / "good.pt").write_bytes(head_bytes())
    (d / "broken.pt").write_bytes(b"junk")
    base = tmp_path / "base.pt"
    base.write_bytes(head_bytes(1))
    e = Engine(TextEmbedder(), checkpoint=str(base), checkpoint_dir=str(d), device="cpu", action_cache=0)
    assert "good" in e.heads and "broken" not in e.heads
