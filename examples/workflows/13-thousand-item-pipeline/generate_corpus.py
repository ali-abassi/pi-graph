#!/usr/bin/env python3
"""Generate a deterministic JSONL corpus for the bulk example."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--out", type=Path, default=Path("corpus.jsonl"))
    args = parser.parse_args()
    if args.count < 1:
        raise SystemExit("--count must be at least 1")
    args.out.write_text("".join(
        json.dumps({"id": f"record-{index:04d}", "content": f"customer record {index}"}) + "\n"
        for index in range(1, args.count + 1)
    ), encoding="utf-8")
    print(f"wrote {args.count} items to {args.out}")


if __name__ == "__main__":
    main()
