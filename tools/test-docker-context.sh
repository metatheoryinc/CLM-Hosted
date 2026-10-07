#!/bin/sh
# Static pages are deployed to Cloudflare Pages.  They must never be copied
# into the RunPod image, otherwise a content-only site change rebuilds the pod.
set -eu

grep -Fxq 'src/clm/static/' .dockerignore
