"""Embedder: token-id inputs reach vLLM untruncated; text keeps System One's truncation."""
import base64

import numpy as np

from clm.embedder import Embedder


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.bodies = []
        self.headers = {}

    def post(self, url, json, timeout):
        self.bodies.append(json)
        data = [{"index": i, "embedding": base64.b64encode(np.full(8, i + 1, np.float32).tobytes()).decode()}
                for i in range(len(json["input"]))]
        return FakeResponse({"data": data, "usage": {"prompt_tokens": 7 * len(json["input"])}})


def make():
    e = Embedder(max_tokens=2048)
    e.session = FakeSession()
    return e


def test_text_is_truncated_server_side():
    e = make()
    e.embed(["hello"])
    assert e.session.bodies[0]["truncate_prompt_tokens"] == 2048


def test_token_ids_are_sent_as_ids_without_truncation():
    e = make()
    e.embed_ids([[1, 2, 3], [4, 5]])
    body = e.session.bodies[0]
    assert body["input"] == [[1, 2, 3], [4, 5]]
    assert "truncate_prompt_tokens" not in body


def test_token_ids_are_deduplicated_and_cached():
    e = make()
    v, tokens = e.embed_ids([[1, 2], [3], [1, 2]])
    assert e.session.bodies[0]["input"] == [[1, 2], [3]] and tokens == 14
    assert np.allclose(v[0], v[2]) and v.shape == (3, 8)
    _, tokens = e.embed_ids([[3], [1, 2]])
    assert len(e.session.bodies) == 1 and tokens == 0


def test_ids_and_text_do_not_share_cache_entries():
    e = make()
    e.embed(["ids:x"])
    e.embed_ids([[1]])
    assert len(e.session.bodies) == 2
