#!/usr/bin/env python3
"""Probe recovered containers and decode sampled frames to framemd5 hashes."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Validation:
    relative_path: str
    size: int
    capture_local: str | None
    original_name: str | None
    recovery_class: str | None
    probe_ok: bool
    duration_seconds: float | None
    video_codec: str | None
    width: int | None
    height: int | None
    frame_rate: str | None
    declared_frames: int | None
    audio_codec: str | None
    audio_channels: int | None
    container_creation_time: str | None
    sample_positions: str
    decoded_frame_hashes: str
    decoded_samples: int
    validation_status: str
    diagnostic: str
    elapsed_seconds: float


def run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def write_csv(path: Path, rows: list[Validation]) -> None:
    fields = list(Validation.__dataclass_fields__)
    partial = path.with_suffix(path.suffix + ".partial")
    with partial.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


def probe(path: Path) -> tuple[dict | None, str]:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,size,bit_rate:format_tags=creation_time:"
                "stream=index,codec_name,codec_type,width,height,avg_frame_rate,nb_frames,channels:"
                "stream_tags=creation_time,handler_name"
            ),
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    try:
        return json.loads(result.stdout), result.stderr.strip()
    except json.JSONDecodeError as error:
        return None, f"invalid ffprobe JSON: {error}; {result.stderr.strip()}"


def sample_frame(path: Path, position: float) -> tuple[str | None, str]:
    result = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-threads",
            "2",
            "-ss",
            f"{position:.6f}",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-f",
            "framemd5",
            "-",
        ],
        timeout=240,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line and not line.startswith("#")]
    if result.returncode == 0 and lines:
        return lines[-1], result.stderr.strip()
    diagnostic = result.stderr.strip() or "no decoded frame hash returned"
    return None, diagnostic


def validate(path: Path, root: Path, manifest: dict[str, dict]) -> Validation:
    started = time.monotonic()
    relative = str(path.relative_to(root))
    metadata = manifest.get(relative, {})
    data, probe_diagnostic = probe(path)
    if data is None:
        return Validation(
            relative_path=relative,
            size=path.stat().st_size,
            capture_local=metadata.get("capture_local"),
            original_name=metadata.get("original_name"),
            recovery_class=metadata.get("recovery_class"),
            probe_ok=False,
            duration_seconds=None,
            video_codec=None,
            width=None,
            height=None,
            frame_rate=None,
            declared_frames=None,
            audio_codec=None,
            audio_channels=None,
            container_creation_time=None,
            sample_positions="",
            decoded_frame_hashes="",
            decoded_samples=0,
            validation_status="probe-failed",
            diagnostic=probe_diagnostic[-2000:],
            elapsed_seconds=time.monotonic() - started,
        )

    streams = data.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    format_data = data.get("format", {})
    duration_raw = format_data.get("duration") or (video or {}).get("duration")
    try:
        duration = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None
    try:
        declared_frames = int(video["nb_frames"]) if video and video.get("nb_frames") else None
    except (TypeError, ValueError):
        declared_frames = None
    creation = (format_data.get("tags") or {}).get("creation_time")
    if not creation and video:
        creation = (video.get("tags") or {}).get("creation_time")

    if video is None:
        return Validation(
            relative_path=relative,
            size=path.stat().st_size,
            capture_local=metadata.get("capture_local"),
            original_name=metadata.get("original_name"),
            recovery_class=metadata.get("recovery_class"),
            probe_ok=True,
            duration_seconds=duration,
            video_codec=None,
            width=None,
            height=None,
            frame_rate=None,
            declared_frames=None,
            audio_codec=audio.get("codec_name") if audio else None,
            audio_channels=audio.get("channels") if audio else None,
            container_creation_time=creation,
            sample_positions="",
            decoded_frame_hashes="",
            decoded_samples=0,
            validation_status="no-video-stream",
            diagnostic=probe_diagnostic[-2000:],
            elapsed_seconds=time.monotonic() - started,
        )

    if duration is None or duration <= 0:
        positions = [0.0]
    elif duration < 0.5:
        positions = [0.0]
    elif duration < 2.0:
        positions = [0.0, duration * 0.5]
    else:
        positions = [0.0, duration * 0.5, max(0.0, duration * 0.95)]
    unique_positions: list[float] = []
    for position in positions:
        rounded = round(position, 6)
        if rounded not in unique_positions:
            unique_positions.append(rounded)

    hashes: list[str] = []
    diagnostics = [probe_diagnostic] if probe_diagnostic else []
    for position in unique_positions:
        frame_hash, diagnostic = sample_frame(path, position)
        if frame_hash:
            hashes.append(f"{position:.6f}:{frame_hash}")
        if diagnostic:
            diagnostics.append(f"at {position:.6f}s: {diagnostic}")
    decoded = len(hashes)
    if decoded == len(unique_positions):
        status = "decoded-samples-pass"
    elif decoded > 0:
        status = "decoded-samples-partial"
    else:
        status = "decode-failed"
    return Validation(
        relative_path=relative,
        size=path.stat().st_size,
        capture_local=metadata.get("capture_local"),
        original_name=metadata.get("original_name"),
        recovery_class=metadata.get("recovery_class"),
        probe_ok=True,
        duration_seconds=duration,
        video_codec=video.get("codec_name"),
        width=video.get("width"),
        height=video.get("height"),
        frame_rate=video.get("avg_frame_rate"),
        declared_frames=declared_frames,
        audio_codec=audio.get("codec_name") if audio else None,
        audio_channels=audio.get("channels") if audio else None,
        container_creation_time=creation,
        sample_positions=";".join(f"{position:.6f}" for position in unique_positions),
        decoded_frame_hashes=" | ".join(hashes),
        decoded_samples=decoded,
        validation_status=status,
        diagnostic=" | ".join(diagnostics)[-4000:],
        elapsed_seconds=time.monotonic() - started,
    )


def load_manifest(path: Path) -> dict[str, dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["relative_output"]: row for row in csv.DictReader(handle)}


def load_existing(path: Path) -> list[Validation]:
    if not path.exists():
        return []
    rows: list[Validation] = []
    bool_fields = {"probe_ok"}
    int_fields = {"size", "width", "height", "declared_frames", "audio_channels", "decoded_samples"}
    float_fields = {"duration_seconds", "elapsed_seconds"}
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            values: dict[str, object] = {}
            for field in Validation.__dataclass_fields__:
                value = raw.get(field, "")
                if field in bool_fields:
                    values[field] = value.lower() == "true"
                elif field in int_fields:
                    values[field] = int(value) if value else None
                elif field in float_fields:
                    values[field] = float(value) if value else None
                else:
                    values[field] = value or None
            values["sample_positions"] = values["sample_positions"] or ""
            values["decoded_frame_hashes"] = values["decoded_frame_hashes"] or ""
            values["diagnostic"] = values["diagnostic"] or ""
            rows.append(Validation(**values))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    manifest = load_manifest(args.root / "extraction_manifest.csv")
    output_csv = args.root / "validation.csv"
    rows = load_existing(output_csv)
    completed = {row.relative_path: row for row in rows}
    files = sorted(
        path
        for directory in (
            args.root / "Videos",
            args.root / "Previews",
            args.root / "Unclassified",
            args.root / "Unclassified_Previews_1280x720",
        )
        if directory.exists()
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in {".mp4", ".lrv", ".mov"}
    )
    print(json.dumps({"files": len(files), "already_validated": len(completed)}, indent=2))
    for number, path in enumerate(files, 1):
        relative = str(path.relative_to(args.root))
        prior = completed.get(relative)
        if prior and prior.size == path.stat().st_size:
            print(f"SKIP {number}/{len(files)} {relative} {prior.validation_status}", flush=True)
            continue
        print(f"VALIDATE {number}/{len(files)} {relative}", flush=True)
        row = validate(path, args.root, manifest)
        completed[relative] = row
        rows = sorted(completed.values(), key=lambda item: item.relative_path)
        write_csv(output_csv, rows)
        print(
            f"RESULT {number}/{len(files)} {relative} {row.validation_status} "
            f"samples={row.decoded_samples}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
