#!/usr/bin/env python3
"""Record non-content source-file attributes for before/after immutability checks."""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for path in sorted(args.source.rglob("*")):
        if not path.is_file() or "Repaired" in path.relative_to(args.source).parts:
            continue
        stat = path.stat()
        rows.append(
            {
                "relative_path": str(path.relative_to(args.source)),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "inode": stat.st_ino,
            }
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial = args.output.with_suffix(args.output.suffix + ".partial")
    with partial.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["relative_path", "size", "mtime_ns", "inode"])
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, args.output)
    print(f"files={len(rows)} bytes={sum(row['size'] for row in rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
