# CLM on one GPU: the vLLM Qwen3-8B pooling encoder + clm-serve in one container.
# No weights are baked in: on first boot they download to $HF_HOME / $CLM_CKPT_DIR,
# which point at /workspace (a RunPod network volume) so restarts reuse them.
ARG VLLM_TAG=v0.30.0
FROM vllm/vllm-openai:${VLLM_TAG}

# cloudflared: optional Cloudflare Tunnel, started when TUNNEL_TOKEN is set
ARG CLOUDFLARED_VERSION=2026.9.3
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /usr/local/bin/cloudflared \
        "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64" \
    && chmod +x /usr/local/bin/cloudflared

WORKDIR /opt/clm
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# torch, numpy, requests, fastapi and uvicorn already ship with the vLLM image
RUN pip install --no-cache-dir ".[serve]"

COPY deploy/entrypoint.sh /usr/local/bin/clm-entrypoint
RUN chmod +x /usr/local/bin/clm-entrypoint

ENV HF_HOME=/workspace/hf \
    CLM_CKPT_DIR=/workspace/clm \
    CLM_PORT=8700 \
    EMB_PORT=8090 \
    GPU_UTIL=0.80 \
    MAX_MODEL_LEN=2048 \
    MAX_NUM_SEQS=64

EXPOSE 8700
ENTRYPOINT ["/usr/local/bin/clm-entrypoint"]
