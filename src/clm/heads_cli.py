"""``clm-heads``: upload, list and delete trained heads on a clm-serve (``/v1/admin/heads``).

    export CLM_BASE_URL=https://clm.example.com CLM_API_KEY=<an admin agent's key>
    clm-heads upload runs/tier/best_head.pt --name subagent-tier
    clm-heads list
    clm-heads delete subagent-tier

An uploaded head is served at once as model ``<name>`` and reloaded when the server
restarts. It lives on the server's volume only: keep the checkpoint to upload it again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import requests


def _session() -> tuple[str, requests.Session]:
    base = os.environ.get("CLM_BASE_URL")
    if not base:
        raise SystemExit("CLM_BASE_URL is not set")
    s = requests.Session()
    if os.environ.get("CLM_API_KEY"):
        s.headers["Authorization"] = f"Bearer {os.environ['CLM_API_KEY']}"
    return base.rstrip("/"), s


def _check(r: requests.Response) -> dict:
    if r.status_code != 200:
        raise SystemExit(f"{r.status_code}: {r.text[:300]}")
    return r.json()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="clm-heads", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload")
    up.add_argument("path")
    up.add_argument("--name", required=True, help="model name to serve it as ([a-z0-9-], up to 40)")
    sub.add_parser("list")
    rm = sub.add_parser("delete")
    rm.add_argument("name")
    args = ap.parse_args(argv)
    base, s = _session()
    if args.cmd == "upload":
        data = open(args.path, "rb").read()
        out = _check(s.put(f"{base}/v1/admin/heads/{args.name}", data=data, timeout=300,
                           headers={"Content-Type": "application/octet-stream"}))
        if out["sha256"] != hashlib.sha256(data).hexdigest():
            raise SystemExit(f"checksum mismatch after upload: {out}")
        print(json.dumps(out))
    elif args.cmd == "list":
        json.dump(_check(s.get(f"{base}/v1/admin/heads", timeout=60)), sys.stdout, indent=2)
        print()
    else:
        print(json.dumps(_check(s.delete(f"{base}/v1/admin/heads/{args.name}", timeout=60))))


if __name__ == "__main__":
    main()
