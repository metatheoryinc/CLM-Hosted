"""The served heads match the evaluation loader that produced the released results.

Downloads the released DeepSWE head (75 MB) and reference head (75 MB).
"""
import hashlib
import os
import sys

import pytest
import torch

from clm.heads import HeadPair, download

pytestmark = pytest.mark.network
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEEPSWE_SHA256 = "554989fe88635606cb978dc45a1ce083be1990c4a51e551ea3b6055ead1a029a"


@pytest.fixture(scope="module")
def ckpt_dir(tmp_path_factory):
    return os.environ.get("CLM_TEST_CKPT_DIR") or str(tmp_path_factory.mktemp("ckpt"))


@pytest.fixture(scope="module")
def deepswe(ckpt_dir):
    return download("Contrastive-LM/deepswe-clm-heads-8k", "best_head.pt", os.path.join(ckpt_dir, "deepswe"))


def test_deepswe_checksum_matches_the_model_card(deepswe):
    with open(deepswe, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == DEEPSWE_SHA256


@pytest.mark.parametrize("which", ["deepswe", "reference"])
def test_served_head_matches_the_evaluation_loader(which, deepswe, ckpt_dir):
    path = deepswe if which == "deepswe" else download(dest_dir=ckpt_dir)
    sys.path.insert(0, os.path.join(REPO, "evaluation"))
    from bon_eval import load_heads
    sh, ah, _ = load_heads(path, "cpu")
    served = HeadPair(which, path, "cpu").ensure()
    x = torch.randn(16, 4096)
    with torch.no_grad():
        for eval_head, project in [(sh, served.project_states), (ah, served.project_actions)]:
            want = torch.nn.functional.normalize(eval_head(x), dim=-1)
            assert torch.allclose(project(x.numpy()), want, atol=1e-5)
