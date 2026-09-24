"""The shared token recipe, with the real Qwen3-8B tokenizer (downloads ~10 MB)."""
import os
import sys

import pytest

from clm.recipe import Recipe

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def recipe():
    return Recipe("Qwen/Qwen3-8B", max_len=64)


def test_training_uses_the_same_recipe():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "train"))
    import embed_utils
    assert embed_utils.Recipe is Recipe


def test_state_is_chat_templated_and_keeps_the_tail(recipe):
    msgs = [{"role": "user", "content": "word " * 200}, {"role": "assistant", "content": "the end"}]
    ids = recipe.state_ids(msgs)
    full = recipe._flatten(recipe.tok.apply_chat_template(msgs, tokenize=True, add_generation_prompt=False))
    assert len(ids) == 63 and ids == full[-63:]
    assert "the end" in recipe.tok.decode(ids)


def test_action_keeps_the_head_without_special_tokens(recipe):
    ids = recipe.text_ids("first " + "filler " * 200, keep="head")
    assert len(ids) == 63 and recipe.tok.decode(ids).startswith("first")
    assert recipe.tok.bos_token_id not in ids and recipe.tok.eos_token_id not in ids


def test_empty_action_embeds_a_space(recipe):
    assert recipe.text_ids("") == recipe.text_ids(" ")
