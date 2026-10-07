#!/bin/sh
# Publish the website (src/clm/static) to Cloudflare Pages; the gateway Worker serves it.
# No image rebuild, no `pulumi up`. First run creates the Pages project.
set -e
cd "$(dirname "$0")/.."
npx wrangler pages project create clm-site --production-branch main 2>/dev/null || true
npx wrangler pages deploy src/clm/static --project-name clm-site --branch main
