#!/usr/bin/env python3
"""Write the reproducible bootstrap corpus for MipMap delegation training.

Usage:
    python tools/mipmap-delegation-seed.py /tmp/mipmap-delegation-seed.jsonl
    clm-decisions export /tmp/mipmap-delegation-seed.jsonl --workflow routing/mipmap-delegation \
      --labels baseline --out /tmp/mipmap-delegation-data
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Run from a checkout without requiring an editable install in the invoking Python.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clm.mipmap_delegation import seed_records, write_seed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", help="JSONL output path")
    args = parser.parse_args()
    write_seed(args.out)
    print(f"wrote {len(seed_records())} records to {args.out}")


if __name__ == "__main__":
    main()
