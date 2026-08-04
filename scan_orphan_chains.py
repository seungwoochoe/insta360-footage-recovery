#!/usr/bin/env python3
"""Map PhotoRec MP4 starts back to residual exFAT FAT chains, read-only."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO
from zoneinfo import ZoneInfo

from exfat_recover import EOC_MIN, cluster_offset, next_cluster, parse_geometry, read_exact


PHOTO_NAME = re.compile(r"^f(?P<sector>\d+)_ftyp\.mov$")


@dataclass
class Scan:
    photorec_path: str
    photorec_size: int
    source_sector: int
    first_cluster: int | None
    aligned_cluster: bool
    image_header_matches: bool
    fat_status: str | None
    fat_clusters: int | None
    fat_bytes: int | None
    image_atoms: str | None
    image_mdat_end: int | None
    image_moov_offset: int | None
    image_moov_end: int | None
    image_inst_end: int | None
    image_recoverable_size: int | None
    image_recovery_class: str
    image_creation_time_utc: str | None
    image_creation_time_local: str | None
    photorec_atoms: str | None
    photorec_moov_end: int | None
    photorec_recovery_class: str
    photorec_creation_time_utc: str | None
    known_name: str | None
    known_capture_date: str | None
    known_size: int | None


def chain_to_eoc(handle: BinaryIO, geometry, first: int) -> tuple[list[int], str]:
    chain: list[int] = []
    seen: set[int] = set()
    current = first
    limit = geometry.cluster_count + 1
    while len(chain) < limit:
        if current in seen:
            return chain, "fat-cycle"
        if current < 2 or current >= geometry.cluster_count + 2:
            return chain, f"fat-invalid-{current:#x}"
        chain.append(current)
        seen.add(current)
        following = next_cluster(handle, geometry, current)
        if following >= EOC_MIN:
            return chain, "fat-eoc"
        if following == 0:
            return chain, "fat-free"
        current = following
    return chain, "fat-limit"


def logical_read(handle: BinaryIO, geometry, chain: list[int], offset: int, size: int) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(chain) * geometry.cluster_size:
        return b""
    result = bytearray()
    position = offset
    remaining = size
    while remaining:
        index, within = divmod(position, geometry.cluster_size)
        take = min(remaining, geometry.cluster_size - within)
        result.extend(read_exact(handle, cluster_offset(geometry, chain[index]) + within, take))
        position += take
        remaining -= take
    return bytes(result)


def atom_header(data: bytes) -> tuple[int, str, int] | None:
    if len(data) < 8:
        return None
    size, raw_type = struct.unpack(">I4s", data[:8])
    header_size = 8
    if size == 1:
        if len(data) < 16:
            return None
        size = struct.unpack(">Q", data[8:16])[0]
        header_size = 16
    try:
        atom_type = raw_type.decode("ascii")
    except UnicodeDecodeError:
        atom_type = raw_type.hex()
    if size < header_size:
        return None
    return size, atom_type, header_size


def parse_chain_atoms(handle: BinaryIO, geometry, chain: list[int]) -> tuple[list[dict], str]:
    capacity = len(chain) * geometry.cluster_size
    atoms: list[dict] = []
    position = 0
    for _ in range(8):
        header = atom_header(logical_read(handle, geometry, chain, position, 16))
        if header is None:
            return atoms, "invalid-header"
        size, atom_type, header_size = header
        end = position + size
        atoms.append({"offset": position, "type": atom_type, "size": size, "end": end})
        if end > capacity:
            return atoms, "atom-beyond-chain"
        position = end
        if atom_type == "inst":
            return atoms, "complete-inst"
    return atoms, "atom-limit"


def parse_file_atoms(path: Path) -> tuple[list[dict], str]:
    total = path.stat().st_size
    atoms: list[dict] = []
    position = 0
    with path.open("rb") as handle:
        for _ in range(8):
            if position + 8 > total:
                return atoms, "end-of-file"
            handle.seek(position)
            header = atom_header(handle.read(16))
            if header is None:
                return atoms, "invalid-header"
            size, atom_type, header_size = header
            end = position + size
            atoms.append({"offset": position, "type": atom_type, "size": size, "end": end})
            if end > total:
                return atoms, "atom-beyond-file"
            position = end
            if atom_type == "inst":
                return atoms, "complete-inst"
    return atoms, "atom-limit"


def classify(atoms: list[dict], complete_status: str) -> tuple[str, int | None, int | None, int | None, int | None]:
    mdat = next((atom for atom in atoms if atom["type"] == "mdat"), None)
    moov = next((atom for atom in atoms if atom["type"] == "moov"), None)
    inst = next((atom for atom in atoms if atom["type"] == "inst"), None)
    mdat_end = mdat["end"] if mdat else None
    moov_end = moov["end"] if moov else None
    inst_end = inst["end"] if inst else None
    if inst and complete_status == "complete-inst":
        return "full-with-inst", mdat_end, moov_end, inst_end, inst_end
    if moov and moov_end is not None:
        return "playable-through-moov", mdat_end, moov_end, inst_end, moov_end
    if mdat:
        return "mdat-only", mdat_end, None, None, None
    return "invalid", None, None, None, None


def atom_summary(atoms: list[dict]) -> str:
    return " ".join(f"{atom['type']}@{atom['offset']}+{atom['size']}" for atom in atoms)


def quicktime_creation_time(data: bytes) -> dt.datetime | None:
    """Parse mvhd creation time from the beginning of a moov payload."""
    position = 0
    while position + 16 <= len(data):
        header = atom_header(data[position : position + 16])
        if header is None:
            return None
        size, atom_type, header_size = header
        if position + size > len(data):
            return None
        if atom_type == "mvhd":
            body = position + header_size
            version = data[body]
            if version == 0 and body + 8 <= len(data):
                seconds = struct.unpack_from(">I", data, body + 4)[0]
            elif version == 1 and body + 12 <= len(data):
                seconds = struct.unpack_from(">Q", data, body + 4)[0]
            else:
                return None
            if seconds == 0:
                return None
            epoch = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)
            try:
                return epoch + dt.timedelta(seconds=seconds)
            except OverflowError:
                return None
        position += size
    return None


def chain_creation_time(handle: BinaryIO, geometry, chain: list[int], atoms: list[dict]) -> dt.datetime | None:
    moov = next((atom for atom in atoms if atom["type"] == "moov"), None)
    if not moov:
        return None
    available = min(moov["size"] - 8, 1_048_576)
    if available <= 0:
        return None
    payload = logical_read(handle, geometry, chain, moov["offset"] + 8, available)
    return quicktime_creation_time(payload)


def file_creation_time(path: Path, atoms: list[dict]) -> dt.datetime | None:
    moov = next((atom for atom in atoms if atom["type"] == "moov"), None)
    if not moov:
        return None
    available = min(moov["size"] - 8, 1_048_576)
    if available <= 0:
        return None
    with path.open("rb") as handle:
        handle.seek(moov["offset"] + 8)
        return quicktime_creation_time(handle.read(available))


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
    parser.add_argument("--timezone", default="Asia/Seoul")
    parser.add_argument("--photorec", type=Path, required=True)
    parser.add_argument("--inventory-json", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()

    known_entries = json.loads(args.inventory_json.read_text(encoding="utf-8"))
    known = {
        int(entry["source_sector"]): entry
        for entry in known_entries
        if entry.get("source_sector") is not None and entry.get("kind") in {"VID", "LRV"}
    }
    paths = sorted(
        (path for path in args.photorec.rglob("f*_ftyp.mov") if PHOTO_NAME.match(path.name)),
        key=lambda path: int(PHOTO_NAME.match(path.name).group("sector")),
    )
    scans: list[Scan] = []
    with args.source.open("rb", buffering=0) as image:
        geometry = parse_geometry(image, args.partition_sector)
        heap_sector = geometry.heap_offset // geometry.sector_size
        sectors_per_cluster = geometry.cluster_size // geometry.sector_size
        for number, path in enumerate(paths, 1):
            match = PHOTO_NAME.match(path.name)
            assert match
            sector = int(match.group("sector"))
            delta = sector - heap_sector
            aligned = delta >= 0 and delta % sectors_per_cluster == 0
            first_cluster = delta // sectors_per_cluster + 2 if aligned else None
            photo_atoms, photo_status = parse_file_atoms(path)
            photo_class, _, photo_moov_end, _, _ = classify(photo_atoms, photo_status)
            photo_creation = file_creation_time(path, photo_atoms)
            chain: list[int] = []
            fat_status = None
            image_atoms: list[dict] = []
            image_status = "invalid-header"
            header_matches = False
            if first_cluster is not None and 2 <= first_cluster < geometry.cluster_count + 2:
                image_header = read_exact(image, cluster_offset(geometry, first_cluster), 64)
                with path.open("rb") as carved:
                    header_matches = image_header == carved.read(64)
                chain, fat_status = chain_to_eoc(image, geometry, first_cluster)
                image_atoms, image_status = parse_chain_atoms(image, geometry, chain)
            image_class, mdat_end, moov_end, inst_end, recoverable_size = classify(image_atoms, image_status)
            moov_atom = next((atom for atom in image_atoms if atom["type"] == "moov"), None)
            image_creation = chain_creation_time(image, geometry, chain, image_atoms)
            image_creation_local = image_creation.astimezone(ZoneInfo(args.timezone)) if image_creation else None
            entry = known.get(sector)
            scans.append(
                Scan(
                    photorec_path=str(path),
                    photorec_size=path.stat().st_size,
                    source_sector=sector,
                    first_cluster=first_cluster,
                    aligned_cluster=aligned,
                    image_header_matches=header_matches,
                    fat_status=fat_status,
                    fat_clusters=len(chain) if chain else None,
                    fat_bytes=len(chain) * geometry.cluster_size if chain else None,
                    image_atoms=atom_summary(image_atoms) if image_atoms else None,
                    image_mdat_end=mdat_end,
                    image_moov_offset=moov_atom["offset"] if moov_atom else None,
                    image_moov_end=moov_end,
                    image_inst_end=inst_end,
                    image_recoverable_size=recoverable_size,
                    image_recovery_class=image_class,
                    image_creation_time_utc=image_creation.isoformat() if image_creation else None,
                    image_creation_time_local=image_creation_local.isoformat() if image_creation_local else None,
                    photorec_atoms=atom_summary(photo_atoms) if photo_atoms else None,
                    photorec_moov_end=photo_moov_end,
                    photorec_recovery_class=photo_class,
                    photorec_creation_time_utc=photo_creation.isoformat() if photo_creation else None,
                    known_name=entry.get("name") if entry else None,
                    known_capture_date=entry.get("capture_date") if entry else None,
                    known_size=int(entry["size"]) if entry else None,
                )
            )
            if number % 100 == 0:
                print(f"scanned {number}/{len(paths)}", flush=True)

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    fields = list(Scan.__dataclass_fields__)
    with args.csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for scan in scans:
            writer.writerow(asdict(scan))
    args.json.write_text(json.dumps([asdict(scan) for scan in scans], indent=2) + "\n", encoding="utf-8")
    summary: dict[str, object] = {
        "files": len(scans),
        "image_classes": {},
        "photorec_classes": {},
        "image_recoverable_bytes": sum(scan.image_recoverable_size or 0 for scan in scans),
        "known_files": sum(scan.known_name is not None for scan in scans),
    }
    for field, key in (("image_recovery_class", "image_classes"), ("photorec_recovery_class", "photorec_classes")):
        counts: dict[str, int] = {}
        for scan in scans:
            value = getattr(scan, field)
            counts[value] = counts.get(value, 0) + 1
        summary[key] = counts
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
