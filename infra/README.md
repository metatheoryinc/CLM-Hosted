# infra: the CLM stack (Pulumi)

[Pulumi.yaml](Pulumi.yaml) manages everything outside GitHub:

```
agent ──► https://<hostname> ── gateway Worker ── Cloudflare Tunnel ──► RunPod Pod :8700
          per-agent key → upstream key,           outbound from the     vLLM Qwen3-8B + clm-serve,
          per-agent rate limit, request logs      Pod, no open port     weights on a network volume
```

| | resources |
| --- | --- |
| RunPod | GPU Pod (`ghcr.io/metatheoryinc/clm-hosted:<imageTag>`), 40 GB network volume (protected), secrets for the upstream key and tunnel token |
| Cloudflare | tunnel + ingress config, proxied CNAME for `hostname`, gateway Worker ([gateway/worker.js](gateway/worker.js)) and its route |

The image itself is built by `.github/workflows/image.yml`.

It uses the Pulumi YAML runtime, so there is nothing to install beyond the
`pulumi` CLI; the provider plugins download on first run. The RunPod provider
([runpod/pulumi-runpod](https://github.com/runpod/pulumi-runpod) v0.1.5) comes
from its GitHub releases. Don't `pip install pulumi_runpod`: that PyPI name
belongs to the older `pulumi-runpod-native` provider.

## Credentials

Every credential, including the providers' own API keys, is a Pulumi secret:
encrypted in `Pulumi.prod.yaml` (safe to commit), so there is no `.env` file.
You need a RunPod API key (Settings → API Keys) and a Cloudflare API token.
The Cloudflare API token needs, for the account: **Cloudflare Tunnel: Edit**
and **Workers Scripts: Edit**; for the `metatheory.dev` zone: **Zone: Read**,
**DNS: Edit** and **Workers Routes: Edit**. Zero Trust must be enabled on the account (the free
plan is enough) for tunnels.

## One-time setup

```bash
cd infra
pulumi stack init metatheory/prod
pulumi config set --secret runpodApiKey      # prompts, so the key stays out of shell history
pulumi config set --secret cloudflareApiToken
```

The lookups below need the keys in your shell for one command each:

```bash
export RUNPOD_API_KEY=$(pulumi config get runpodApiKey)
export CLOUDFLARE_API_TOKEN=$(pulumi config get cloudflareApiToken)
```

Find the ID of your existing GHCR registry credential on RunPod:

```bash
curl -s https://api.runpod.io/graphql -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ myself { containerRegistryCreds { id name } } }"}'
```

The account and zone IDs for `metatheory.dev`:

```bash
curl -s "https://api.cloudflare.com/client/v4/zones?name=metatheory.dev" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | jq '.result[0] | {zone: .id, account: .account.id}'
```

```bash
pulumi config set registryAuthId <id>
pulumi config set cloudflareAccountId <account id>
pulumi config set cloudflareZoneId <zone id>
pulumi config set --secret clmApiKey "$(openssl rand -hex 32)"
pulumi config set --secret agentKeys "{\"agent-a\": \"$(openssl rand -hex 32)\"}"
```

Optional: `hostname` (default `clm.metatheory.dev`), `dataCenterId` (default
`US-MO-2`) and `gpuTypes` (default `["NVIDIA L4"]`), `rateLimitPerMinute` (per
agent, default 600), `imageTag`.

The volume pins the Pod to one datacenter, so pick one near your agents with a
24 GB+ GPU in stock (secure cloud, network storage):

```bash
curl -s https://api.runpod.io/graphql -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ dataCenters { id location storageSupport gpuAvailability { gpuTypeId stockStatus } } }"}' \
  | jq -r '.data.dataCenters[] | select(.storageSupport) | "\(.id)\t\([.gpuAvailability[]? | select(.stockStatus != null and .stockStatus != "None") | .gpuTypeId] | join(", "))"'
```

## Deploy

```bash
pulumi up
pulumi stack output url
```

The first boot downloads Qwen3-8B (~16 GB) and the CLM head onto the volume;
follow it in the Pod's logs on RunPod. Then, with an agent key:

```bash
curl https://clm.metatheory.dev/health
```

```bash
curl https://clm.metatheory.dev/v1/systemone -H "Authorization: Bearer <agent key>" \
  -H "Content-Type: application/json" \
  -d '{"state": "Customer: my invoice was charged twice!", "questions": {"urgency": {"type": "noul", "instructions": "Is this urgent?"}}}'
```

Agents using `CLMClient` set `CLM_BASE_URL=https://clm.metatheory.dev` and
`CLM_API_KEY=<agent key>`. The gateway logs each request's agent, path,
status and latency to Workers Logs.

## Agents

Agent keys live in the `agentKeys` JSON. To add or revoke one, edit it and
redeploy the Worker (the Pod is unaffected):

```bash
pulumi config get agentKeys
pulumi config set --secret agentKeys '{"agent-a": "...", "agent-b": "..."}'
pulumi up
```

## Changes

| change | effect |
| --- | --- |
| `imageTag` (pin a commit SHA from the image workflow) | Pod updated in place |
| `agentKeys`, `rateLimitPerMinute`, `gateway/worker.js` | Worker redeployed, Pod untouched |
| `gpuTypes`, `dataCenterId`, Pod name or volume | Pod **replaced**; the public URL stays the same |
| `dataCenterId` | also replaces the volume, which is `protect`ed; unprotect it deliberately |

`podProxyUrl` reaches the Pod directly through RunPod's proxy, bypassing
Cloudflare; it needs the upstream `clmApiKey` and is for debugging only.

`pulumi destroy --exclude-protected` removes the Pod (stopping its billing)
and the Cloudflare resources but keeps the volume and its downloaded weights
(the volume itself bills per GB); unprotect it (`pulumi state unprotect`) to
destroy it too. The tunnel's ingress config is removed with the tunnel.
