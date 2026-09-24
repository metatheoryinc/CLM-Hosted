"""The encoder token recipe the agentic (DeepSWE) heads were trained with.

Shared by training (``train/embed_utils.py``) and serving (``clm.verify``), so a
trajectory scored through the API is tokenized exactly like the training steps:

* ``Recipe.state_ids``: chat template, last ``max_len - 1`` tokens; string states are
  tokenized as text, tail kept.
* ``Recipe.text_ids``: text without special tokens; ``keep="head"`` keeps the first
  ``max_len - 1`` tokens, ``keep="tail"`` the last.

The ids go to the encoder as they are (``/v1/embeddings`` accepts token ids), so the
server-side truncation used for System One text never applies to them.
"""
from __future__ import annotations


class Recipe:
    def __init__(self, model: str = "Qwen/Qwen3-8B", max_len: int = 8192):
        from transformers import AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained(model, trust_remote_code=True)
        self.cap = max_len - 1

    @staticmethod
    def _flatten(out) -> list[int]:
        # transformers 4.x returns List[int]; 5.x may return a BatchEncoding / nested list
        if hasattr(out, "keys"):
            out = out["input_ids"]
        if out and isinstance(out[0], list):
            out = out[0]
        return list(out)

    def text_ids(self, text: str, keep: str = "head") -> list[int]:
        ids = self._flatten(self.tok(text, add_special_tokens=False)["input_ids"])
        if not ids:
            ids = self._flatten(self.tok(" ", add_special_tokens=False)["input_ids"])
        return ids[:self.cap] if keep == "head" else ids[-self.cap:]

    def state_ids(self, state) -> list[int]:
        if isinstance(state, str):
            return self.text_ids(state, keep="tail")
        ids = self._flatten(self.tok.apply_chat_template(state, tokenize=True, add_generation_prompt=False))
        return ids[-self.cap:]
