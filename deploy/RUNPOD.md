# The CLM container

`ghcr.io/metatheoryinc/clm-hosted` runs both processes on one GPU: the vLLM
Qwen3-8B pooling encoder (`127.0.0.1:8090`, never exposed) and `clm-serve`
(`:8700`), plus `cloudflared` when a tunnel token is set. Deploying it, with
the Cloudflare edge in front, is [infra/](../infra/README.md).

## Image

Pushing to `main` (or running the `image` workflow by hand) builds on GitHub's
runners and pushes `ghcr.io/metatheoryinc/clm-hosted:latest` and `:<sha>`.
The package is private; RunPod pulls it with a GHCR registry credential
(a GitHub token with `read:packages`), whose ID is in the Pulumi config.

No weights are baked in. On first boot they download to the volume at
`/workspace`: Qwen3-8B (~16 GB) to `/workspace/hf`, the CLM head (~75 MB) to
`/workspace/clm`, so restarts skip the downloads.

## Environment

| variable | |
| --- | --- |
| `CLM_API_KEY` | required (the entrypoint refuses to start without it); set by Pulumi from a RunPod secret |
| `TUNNEL_TOKEN` | runs `cloudflared` for the tunnel; set by Pulumi from a RunPod secret |
| `HF_TOKEN` | optional; only de-rate-limits the Hugging Face download |
| `GPU_UTIL` | default `0.80`; the vLLM share of GPU memory |
| `MAX_NUM_SEQS` | default `64`; concurrent encoder sequences |
| `MAX_MODEL_LEN` | default `2048`; also the truncation length in `clm-serve` |
| `CLM_EXTRA_ARGS` | extra `clm-serve` flags, e.g. `--action-cache 1GiB` |

If either server process exits, the container exits and RunPod restarts it.

## Logs

The Pod's logs on RunPod, or on the volume: `/workspace/logs/vllm.log` and
`/workspace/logs/cloudflared.log`.
