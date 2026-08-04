#!/usr/bin/env python3
"""Read-only exFAT inventory/extraction helper for an Insta360 SD source.

The raw device or image is always opened read-only. Extracted files are created
in a separate destination and committed with os.replace only after their exact
recorded byte length has been read.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import struct
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Iterable


EOC_MIN = 0xFFFFFFF8
MEDIA_NAME = re.compile(
    r"^(?P<kind>VID|LRV)_(?P<date>\d{8})_(?P<time>\d{6})_(?P<sequence>\d+)\.(?P<ext>mp4|lrv)$",
    re.IGNORECASE,
)


@dataclass
class Geometry:
    partition_offset: int
    sector_size: int
    cluster_size: int
    fat_offset: int
    fat_length: int
    heap_offset: int
    cluster_count: int
    root_cluster: int


@dataclass
class Entry:
    path: str
    name: str
    active: bool
    is_directory: bool
    no_fat_chain: bool
    first_cluster: int
    valid_size: int
    size: int
    created: str | None
    modified: str | None
    kind: str | None = None
    capture_date: str | None = None
    capture_time: str | None = None
    sequence: str | None = None
    source_sector: int | None = None
    expected_clusters: int | None = None
    chain_clusters: int | None = None
    chain_status: str | None = None
    photorec_path: str | None = None
    photorec_size: int | None = None


def read_exact(handle: BinaryIO, offset: int, size: int) -> bytes:
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise EOFError(f"short read at {offset}: wanted {size}, got {len(data)}")
    return data


def parse_geometry(handle: BinaryIO, partition_sector: int = 65536) -> Geometry:
    provisional_offset = partition_sector * 512
    boot = read_exact(handle, provisional_offset, 512)
    if boot[3:11] != b"EXFAT   ":
        raise ValueError(f"no exFAT signature at byte {provisional_offset}")
    sector_size = 1 << boot[108]
    cluster_size = sector_size * (1 << boot[109])
    return Geometry(
        partition_offset=struct.unpack_from("<Q", boot, 64)[0] * sector_size,
        sector_size=sector_size,
        cluster_size=cluster_size,
        fat_offset=struct.unpack_from("<I", boot, 80)[0] * sector_size,
        fat_length=struct.unpack_from("<I", boot, 84)[0] * sector_size,
        heap_offset=struct.unpack_from("<I", boot, 88)[0] * sector_size,
        cluster_count=struct.unpack_from("<I", boot, 92)[0],
        root_cluster=struct.unpack_from("<I", boot, 96)[0],
    )


def cluster_offset(geometry: Geometry, cluster: int) -> int:
    if cluster < 2 or cluster >= geometry.cluster_count + 2:
        raise ValueError(f"cluster {cluster} outside exFAT heap")
    return geometry.partition_offset + geometry.heap_offset + (cluster - 2) * geometry.cluster_size


def next_cluster(handle: BinaryIO, geometry: Geometry, cluster: int) -> int:
    offset = geometry.partition_offset + geometry.fat_offset + cluster * 4
    return struct.unpack("<I", read_exact(handle, offset, 4))[0]


def cluster_chain(
    handle: BinaryIO,
    geometry: Geometry,
    first_cluster: int,
    size: int,
    no_fat_chain: bool,
) -> tuple[list[int], str]:
    expected = math.ceil(size / geometry.cluster_size) if size else 0
    if expected == 0:
        return [], "empty"
    if first_cluster < 2:
        return [], "invalid-first-cluster"
    if no_fat_chain:
        last = first_cluster + expected - 1
        if last >= geometry.cluster_count + 2:
            return [], "contiguous-chain-out-of-range"
        return list(range(first_cluster, last + 1)), "complete-contiguous"

    chain: list[int] = []
    seen: set[int] = set()
    current = first_cluster
    while len(chain) < expected:
        if current in seen:
            return chain, "fat-cycle"
        if current < 2 or current >= geometry.cluster_count + 2:
            return chain, f"fat-invalid-{current:#x}"
        seen.add(current)
        chain.append(current)
        if len(chain) == expected:
            return chain, "complete-fat"
        following = next_cluster(handle, geometry, current)
        if following >= EOC_MIN:
            return chain, "fat-ended-early"
        if following == 0:
            return chain, "fat-free-early"
        current = following
    return chain, "complete-fat"


def exfat_timestamp(raw: bytes, offset: int, ten_ms_offset: int, utc_offset: int) -> str | None:
    value = struct.unpack_from("<I", raw, offset)[0]
    if value == 0:
        return None
    time_word = value & 0xFFFF
    date_word = value >> 16
    year = 1980 + ((date_word >> 9) & 0x7F)
    month = (date_word >> 5) & 0x0F
    day = date_word & 0x1F
    hour = (time_word >> 11) & 0x1F
    minute = (time_word >> 5) & 0x3F
    second = (time_word & 0x1F) * 2
    hundredths = raw[ten_ms_offset] if ten_ms_offset < len(raw) else 0
    second += hundredths // 100
    microsecond = (hundredths % 100) * 10_000
    try:
        stamp = dt.datetime(year, month, day, hour, minute, second, microsecond)
    except ValueError:
        return None
    zone_byte = raw[utc_offset] if utc_offset < len(raw) else 0x80
    if zone_byte & 0x80:
        return stamp.isoformat(timespec="milliseconds")
    signed = zone_byte if zone_byte < 128 else zone_byte - 256
    zone = dt.timezone(dt.timedelta(minutes=signed * 15))
    return stamp.replace(tzinfo=zone).isoformat(timespec="milliseconds")


def read_entry_sets(directory_data: bytes, parent: str) -> Iterable[Entry]:
    index = 0
    total = len(directory_data) // 32
    while index < total:
        primary = directory_data[index * 32 : (index + 1) * 32]
        entry_type = primary[0]
        if entry_type == 0:
            break
        if (entry_type & 0x7F) != 0x05:
            index += 1
            continue
        secondary_count = primary[1]
        if secondary_count < 2 or index + secondary_count >= total:
            index += 1
            continue
        secondaries = [
            directory_data[(index + n) * 32 : (index + n + 1) * 32]
            for n in range(1, secondary_count + 1)
        ]
        stream = next((item for item in secondaries if (item[0] & 0x7F) == 0x40), None)
        names = [item for item in secondaries if (item[0] & 0x7F) == 0x41]
        if stream is None or not names:
            index += 1 + secondary_count
            continue
        name_length = stream[3]
        name_bytes = b"".join(item[2:32] for item in names)[: name_length * 2]
        name = name_bytes.decode("utf-16le", errors="replace")
        attributes = struct.unpack_from("<H", primary, 4)[0]
        first_cluster = struct.unpack_from("<I", stream, 20)[0]
        valid_size = struct.unpack_from("<Q", stream, 8)[0]
        size = struct.unpack_from("<Q", stream, 24)[0]
        match = MEDIA_NAME.match(name)
        entry = Entry(
            path=f"{parent}/{name}" if parent else name,
            name=name,
            active=bool(entry_type & 0x80),
            is_directory=bool(attributes & 0x10),
            no_fat_chain=bool(stream[1] & 0x02),
            first_cluster=first_cluster,
            valid_size=valid_size,
            size=size,
            created=exfat_timestamp(primary, 8, 20, 22),
            modified=exfat_timestamp(primary, 12, 21, 23),
        )
        if match:
            entry.kind = match.group("kind").upper()
            entry.capture_date = match.group("date")
            entry.capture_time = match.group("time")
            entry.sequence = match.group("sequence")
        yield entry
        index += 1 + secondary_count


def read_file_data(
    handle: BinaryIO,
    geometry: Geometry,
    first_cluster: int,
    size: int,
    no_fat_chain: bool,
) -> tuple[bytes, list[int], str]:
    chain, status = cluster_chain(handle, geometry, first_cluster, size, no_fat_chain)
    remaining = size
    chunks: list[bytes] = []
    for cluster in chain:
        take = min(remaining, geometry.cluster_size)
        chunks.append(read_exact(handle, cluster_offset(geometry, cluster), take))
        remaining -= take
    return b"".join(chunks), chain, status


def walk_directory(
    handle: BinaryIO,
    geometry: Geometry,
    cluster: int,
    size: int,
    no_fat_chain: bool,
    parent: str,
    visited: set[int],
) -> list[Entry]:
    if cluster in visited:
        return []
    visited.add(cluster)
    data, _, status = read_file_data(handle, geometry, cluster, size, no_fat_chain)
    if not status.startswith("complete"):
        raise ValueError(f"cannot read directory {parent or '/'}: {status}")
    results: list[Entry] = []
    for entry in read_entry_sets(data, parent):
        results.append(entry)
        if entry.active and entry.is_directory and entry.first_cluster >= 2 and entry.size:
            results.extend(
                walk_directory(
                    handle,
                    geometry,
                    entry.first_cluster,
                    entry.size,
                    entry.no_fat_chain,
                    entry.path,
                    visited,
                )
            )
    return results


def inventory(handle: BinaryIO, geometry: Geometry, photorec: Path | None) -> list[Entry]:
    root_size = geometry.cluster_size
    entries = walk_directory(
        handle,
        geometry,
        geometry.root_cluster,
        root_size,
        False,
        "",
        set(),
    )
    photorec_index: dict[int, Path] = {}
    if photorec:
        for candidate in photorec.rglob("f*_ftyp.mov"):
            match = re.match(r"f(\d+)_ftyp\.mov$", candidate.name)
            if match:
                photorec_index[int(match.group(1))] = candidate
    for entry in entries:
        if entry.first_cluster >= 2:
            physical = cluster_offset(geometry, entry.first_cluster)
            entry.source_sector = (physical - geometry.partition_offset) // geometry.sector_size
        if entry.size:
            chain, status = cluster_chain(
                handle,
                geometry,
                entry.first_cluster,
                entry.size,
                entry.no_fat_chain,
            )
            entry.expected_clusters = math.ceil(entry.size / geometry.cluster_size)
            entry.chain_clusters = len(chain)
            entry.chain_status = status
        if entry.source_sector is not None and entry.source_sector in photorec_index:
            source = photorec_index[entry.source_sector]
            entry.photorec_path = str(source)
            entry.photorec_size = source.stat().st_size
    return entries


def write_inventory(entries: list[Entry], csv_path: Path, json_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(Entry.__dataclass_fields__)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for entry in entries:
            writer.writerow(asdict(entry))
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump([asdict(entry) for entry in entries], handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def extract_entry(handle: BinaryIO, geometry: Geometry, entry: Entry, destination: Path) -> None:
    chain, status = cluster_chain(
        handle,
        geometry,
        entry.first_cluster,
        entry.size,
        entry.no_fat_chain,
    )
    if not status.startswith("complete") or len(chain) != entry.expected_clusters:
        raise ValueError(f"incomplete cluster chain for {entry.path}: {status}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    remaining = entry.size
    with partial.open("xb") as output:
        for cluster in chain:
            take = min(remaining, geometry.cluster_size)
            output.write(read_exact(handle, cluster_offset(geometry, cluster), take))
            remaining -= take
        output.flush()
        os.fsync(output.fileno())
    if remaining != 0 or partial.stat().st_size != entry.size:
        raise IOError(f"size mismatch extracting {entry.path}")
    os.replace(partial, destination)


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
    parser.add_argument(
        "--partition-sector",
        type=int,
        default=65536,
        help="exFAT partition start sector; 65536 matched the card in this case study",
    )
    parser.add_argument("--photorec", type=Path)
    parser.add_argument("--csv", type=Path, default=Path("inventory.csv"))
    parser.add_argument("--json", type=Path, default=Path("inventory.json"))
    parser.add_argument("--extract", type=Path)
    parser.add_argument("--date-from", default="20260727")
    parser.add_argument("--date-to", default="20260730")
    parser.add_argument("--include-lrv", action="store_true")
    parser.add_argument("--include-deleted", action="store_true")
    parser.add_argument("--only", help="extract exactly one original filename")
    args = parser.parse_args()

    with args.source.open("rb", buffering=0) as image:
        geometry = parse_geometry(image, args.partition_sector)
        entries = inventory(image, geometry, args.photorec)
        write_inventory(entries, args.csv, args.json)
        media = [entry for entry in entries if entry.kind and not entry.is_directory]
        target = [
            entry
            for entry in media
            if entry.capture_date
            and args.date_from <= entry.capture_date <= args.date_to
            and (entry.kind == "VID" or args.include_lrv)
            and (entry.active or args.include_deleted)
            and (args.only is None or entry.name == args.only)
        ]
        summary = {
            "geometry": asdict(geometry),
            "entries": len(entries),
            "media_entries": len(media),
            "target_entries": len(target),
            "target_bytes": sum(entry.size for entry in target),
            "target_complete_chains": sum(
                entry.chain_status is not None and entry.chain_status.startswith("complete") for entry in target
            ),
            "target_photorec_matches": sum(entry.photorec_path is not None for entry in target),
        }
        print(json.dumps(summary, indent=2))
        if args.extract:
            for number, entry in enumerate(target, 1):
                destination = args.extract / ("LRV" if entry.kind == "LRV" else "Videos") / entry.name
                if destination.exists():
                    if destination.stat().st_size == entry.size:
                        print(f"SKIP {number}/{len(target)} {entry.name} (already exact size)", flush=True)
                        continue
                    raise FileExistsError(f"refusing to overwrite mismatched {destination}")
                print(f"EXTRACT {number}/{len(target)} {entry.name} {entry.size} bytes", flush=True)
                extract_entry(image, geometry, entry, destination)
    return 0


if __name__ == "__main__":
    sys.exit(main())
