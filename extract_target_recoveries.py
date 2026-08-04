#!/usr/bin/env python3
"""Extract target-date recoveries from a read-only SD source."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from zoneinfo import ZoneInfo

from exfat_recover import cluster_chain, cluster_offset, parse_geometry, read_exact
from scan_orphan_chains import chain_to_eoc


@dataclass
class Recovery:
    source: str
    source_sector: int
    first_cluster: int
    capture_local: str
    original_name: str | None
    recovery_class: str
    exact_size: int
    relative_output: str
    atom_summary: str | None
    status: str = "pending"


def make_recoveries(
    scan_path: Path,
    inventory_path: Path,
    date_from: str,
    date_to: str,
    timezone: str,
) -> list[Recovery]:
    scans = json.loads(scan_path.read_text(encoding="utf-8"))
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    recoveries: list[Recovery] = []
    covered_sectors: set[int] = set()
    for row in scans:
        local = row.get("image_creation_time_local") or ""
        local_date = local[:10]
        if not (date_from <= local_date <= date_to):
            continue
        exact_size = row.get("image_recoverable_size")
        if not exact_size:
            continue
        sector = int(row["source_sector"])
        covered_sectors.add(sector)
        original = row.get("known_name")
        if original and original.upper().startswith("VID_"):
            relative = f"Videos/{original}"
        elif original and original.upper().startswith("LRV_"):
            relative = f"Previews/{original}"
        else:
            stamp = dt.datetime.fromisoformat(local).strftime("%Y%m%d_%H%M%S")
            relative = f"Unclassified/REC_{stamp}_s{sector}.mp4"
        recoveries.append(
            Recovery(
                source="photorec-fat-chain",
                source_sector=sector,
                first_cluster=int(row["first_cluster"]),
                capture_local=local,
                original_name=original,
                recovery_class=row["image_recovery_class"],
                exact_size=int(exact_size),
                relative_output=relative,
                atom_summary=row.get("image_atoms"),
            )
        )

    for row in inventory:
        capture_date = row.get("capture_date") or ""
        if not (date_from.replace("-", "") <= capture_date <= date_to.replace("-", "")) or row.get(
            "kind"
        ) not in {"VID", "LRV"}:
            continue
        sector = int(row["source_sector"])
        if sector in covered_sectors:
            continue
        original = row["name"]
        relative = f"Videos/{original}" if row["kind"] == "VID" else f"Previews/{original}"
        naive_capture = dt.datetime.strptime(
            f"{capture_date}{row['capture_time']}", "%Y%m%d%H%M%S"
        )
        capture_local = naive_capture.replace(tzinfo=ZoneInfo(timezone)).isoformat()
        recoveries.append(
            Recovery(
                source="active-directory-chain",
                source_sector=sector,
                first_cluster=int(row["first_cluster"]),
                capture_local=capture_local,
                original_name=original,
                recovery_class="full-directory-length",
                exact_size=int(row["size"]),
                relative_output=relative,
                atom_summary=None,
            )
        )
    return sorted(recoveries, key=lambda item: (item.capture_local, item.source_sector))


def write_manifest(path: Path, recoveries: list[Recovery]) -> None:
    fields = list(Recovery.__dataclass_fields__)
    partial = path.with_suffix(path.suffix + ".partial")
    with partial.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for recovery in recoveries:
            writer.writerow(asdict(recovery))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


def extract_one(image, geometry, recovery: Recovery, destination: Path, number: int, total: int) -> None:
    if recovery.source == "active-directory-chain":
        chain, chain_status = cluster_chain(
            image, geometry, recovery.first_cluster, recovery.exact_size, False
        )
        if not chain_status.startswith("complete"):
            raise ValueError(f"{recovery.relative_output}: {chain_status}")
    else:
        chain, chain_status = chain_to_eoc(image, geometry, recovery.first_cluster)
        needed = math.ceil(recovery.exact_size / geometry.cluster_size)
        if len(chain) < needed:
            raise ValueError(
                f"{recovery.relative_output}: chain has {len(chain)} clusters, needs {needed}"
            )
        chain = chain[:needed]

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.stat().st_size == recovery.exact_size:
            recovery.status = "already-present-exact-size"
            print(f"SKIP {number}/{total} {recovery.relative_output}", flush=True)
            return
        raise FileExistsError(f"refusing to overwrite mismatched {destination}")
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        raise FileExistsError(f"remove or inspect interrupted partial file: {partial}")

    remaining = recovery.exact_size
    copied = 0
    started = time.monotonic()
    last_report = started
    print(
        f"START {number}/{total} {recovery.relative_output} {recovery.exact_size} bytes",
        flush=True,
    )
    with partial.open("xb") as output:
        for cluster in chain:
            take = min(remaining, geometry.cluster_size)
            output.write(read_exact(image, cluster_offset(geometry, cluster), take))
            copied += take
            remaining -= take
            now = time.monotonic()
            if now - last_report >= 10:
                rate = copied / max(now - started, 0.001)
                print(
                    f"PROGRESS {number}/{total} {recovery.relative_output} "
                    f"{copied}/{recovery.exact_size} {rate / 1_000_000:.1f} MB/s",
                    flush=True,
                )
                last_report = now
        output.flush()
        os.fsync(output.fileno())
    if remaining != 0 or partial.stat().st_size != recovery.exact_size:
        raise IOError(f"size mismatch for {recovery.relative_output}")
    os.replace(partial, destination)
    recovery.status = "extracted-exact-size"
    elapsed = time.monotonic() - started
    print(
        f"DONE {number}/{total} {recovery.relative_output} "
        f"{recovery.exact_size} bytes {elapsed:.1f}s",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        "--image",
        dest="source",
        type=Path,
        required=True,
        help="whole-card raw device or disk image (opened read-only)",
    )
    parser.add_argument("--scan-json", type=Path, required=True)
    parser.add_argument("--inventory-json", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--date-from", required=True, help="local date in YYYY-MM-DD format")
    parser.add_argument("--date-to", required=True, help="local date in YYYY-MM-DD format")
    parser.add_argument("--timezone", default="Asia/Seoul")
    parser.add_argument(
        "--partition-sector",
        type=int,
        default=65536,
        help="exFAT partition start sector; 65536 matched the card in this case study",
    )
    args = parser.parse_args()

    recoveries = make_recoveries(
        args.scan_json,
        args.inventory_json,
        args.date_from,
        args.date_to,
        args.timezone,
    )
    expected_bytes = sum(item.exact_size for item in recoveries)
    print(json.dumps({"recoveries": len(recoveries), "expected_bytes": expected_bytes}, indent=2))
    args.destination.mkdir(parents=True, exist_ok=True)
    manifest = args.destination / "extraction_manifest.csv"
    write_manifest(manifest, recoveries)
    with args.source.open("rb", buffering=0) as image:
        geometry = parse_geometry(image, args.partition_sector)
        for number, recovery in enumerate(recoveries, 1):
            extract_one(
                image,
                geometry,
                recovery,
                args.destination / recovery.relative_output,
                number,
                len(recoveries),
            )
            write_manifest(manifest, recoveries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
