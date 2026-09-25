#!/bin/bash
# Container entrypoint: start the Qwen3-8B pooling encoder, wait until it answers,
# then run clm-serve in the foreground. If either process dies the container exits,
# so RunPod restarts it.
#
# Environment (defaults in the Dockerfile):
#   CLM_API_KEY     required unless ALLOW_NO_AUTH=1; requests need "Authorization: Bearer <key>"
#   TUNNEL_TOKEN    optional Cloudflare Tunnel token; when set, cloudflared runs alongside
#   DEEPSWE         1 (default) serves the DeepSWE verifier head at POST /v1/verify; 0 skips it
#   GPU_UTIL, MAX_MODEL_LEN, CLM_MAX_TOKENS, MAX_NUM_SEQS, EMB_PORT, CLM_PORT, HF_TOKEN, CLM_EXTRA_ARGS
set -euo pipefail

if [ -z "${CLM_API_KEY:-}" ] && [ "${ALLOW_NO_AUTH:-0}" != "1" ]; then
    echo "[entrypoint] CLM_API_KEY is not set; refusing to start a public server without auth" >&2
    exit 1
fi

mkdir -p "$HF_HOME" "$CLM_CKPT_DIR" "$CLM_CKPT_DIR/heads" /workspace/logs

# DeepSWE verifier head (~75 MB), checked against the SHA-256 on its model card
EXTRA_MODELS=()
if [ "${DEEPSWE:-1}" = "1" ]; then
    DEEPSWE_DIR="$CLM_CKPT_DIR/deepswe"
    DEEPSWE_SHA256=554989fe88635606cb978dc45a1ce083be1990c4a51e551ea3b6055ead1a029a
    # optional: a failed download or bad checksum leaves System One serving without it
    if ! python3 -c "from clm.heads import download; download('Contrastive-LM/deepswe-clm-heads-8k', 'best_head.pt', '$DEEPSWE_DIR')"; then
        echo "[entrypoint] DeepSWE head download failed; serving without /v1/verify" >&2
    elif ! echo "$DEEPSWE_SHA256  $DEEPSWE_DIR/best_head.pt" | sha256sum -c --quiet; then
        echo "[entrypoint] DeepSWE head checksum mismatch; serving without /v1/verify" >&2
        rm -f "$DEEPSWE_DIR/best_head.pt"
    else
        EXTRA_MODELS=(--model "deepswe=$DEEPSWE_DIR/best_head.pt")
    fi
fi

# Same encoder settings as serve_qwen3_8b.sh (last-token pooling + prefix cache, what the
# head was trained against), minus --enforce-eager and with a higher memory share since
# the GPU is dedicated. clm-serve's heads and vector cache use the remaining memory.
# MAX_MODEL_LEN (8192) is the DeepSWE verifier's context; System One text is still cut
# to CLM_MAX_TOKENS (2048), the reference head's budget.
echo "[entrypoint] starting vLLM Qwen3-8B on :$EMB_PORT (first boot downloads ~16 GB to $HF_HOME)"
vllm serve Qwen/Qwen3-8B \
    --served-model-name qwen3-8b \
    --runner pooling \
    --enable-prefix-caching \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization "$GPU_UTIL" \
    --max-num-seqs "$MAX_NUM_SEQS" \
    --host 127.0.0.1 --port "$EMB_PORT" \
    > >(tee -a /workspace/logs/vllm.log) 2>&1 &
VLLM_PID=$!

until curl -sf "http://127.0.0.1:$EMB_PORT/v1/models" >/dev/null; do
    if ! kill -0 "$VLLM_PID" 2>/dev/null; then
        echo "[entrypoint] vLLM exited during start-up; see /workspace/logs/vllm.log" >&2
        exit 1
    fi
    sleep 5
done
echo "[entrypoint] vLLM is up"

if [ -n "${TUNNEL_TOKEN:-}" ]; then
    cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN" \
        > >(tee -a /workspace/logs/cloudflared.log) 2>&1 &
fi

# The head (~75 MB) downloads to $CLM_CKPT_DIR on first boot.
clm-serve --host 0.0.0.0 --port "$CLM_PORT" \
    --emb-url "http://127.0.0.1:$EMB_PORT/v1/embeddings" \
    --max-tokens "$CLM_MAX_TOKENS" \
    --ckpt-dir "$CLM_CKPT_DIR/heads" \
    --no-ui "${EXTRA_MODELS[@]}" ${CLM_EXTRA_ARGS:-} &
CLM_PID=$!

wait -n "$VLLM_PID" "$CLM_PID"
echo "[entrypoint] a server process exited; stopping the container" >&2
exit 1
