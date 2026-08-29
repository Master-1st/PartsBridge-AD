"""Repair one proven uniform pad shift while preserving current component placement."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import altium_monkey as altium

from lcsc_altium_loader.pcbdoc_repair import (
    PadRepairError,
    apply_pad_corrections,
    pad_snapshot,
    plan_shifted_pad_repair,
    write_pad_stream_only,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_reference(path: Path) -> object:
    if path.suffix.casefold() != ".zip":
        return altium.AltiumPcbDoc.from_file(path, verbose=False)
    with zipfile.ZipFile(path) as archive:
        entries = [entry for entry in archive.infolist() if entry.filename.casefold().endswith(".pcbdoc")]
        if len(entries) != 1:
            raise PadRepairError(
                f"reference ZIP must contain exactly one PcbDoc, found {len(entries)}"
            )
        return altium.AltiumPcbDoc.from_bytes(
            archive.read(entries[0]), filename=entries[0].filename, verbose=False
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repair pads matching one proven board-space shift using a clean history file."
    )
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--shift-x-mils", required=True, type=float)
    parser.add_argument("--shift-y-mils", required=True, type=float)
    parser.add_argument("--tolerance-mils", type=float, default=0.01)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    current_path = args.current.resolve()
    reference_path = args.reference.resolve()
    output_path = args.output.resolve()
    if current_path == output_path:
        print("output must not overwrite the current PcbDoc", file=sys.stderr)
        return 3
    if output_path.exists():
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 3
    if not current_path.is_file() or not reference_path.is_file():
        print("current and reference files must exist", file=sys.stderr)
        return 3

    try:
        current = altium.AltiumPcbDoc.from_file(current_path, verbose=False)
        reference = _load_reference(reference_path)
        corrections, report = plan_shifted_pad_repair(
            current,
            reference,
            shift_x_mils=args.shift_x_mils,
            shift_y_mils=args.shift_y_mils,
            tolerance_mils=args.tolerance_mils,
        )
        before_snapshot = pad_snapshot(current)
        apply_pad_corrections(current, corrections)
        expected_snapshot = pad_snapshot(current)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        native_report = write_pad_stream_only(current_path, current, output_path)
        reloaded = altium.AltiumPcbDoc.from_file(output_path, verbose=False)
        if pad_snapshot(reloaded) != expected_snapshot:
            raise PadRepairError("saved PcbDoc pad records do not match the validated repair plan")
        remaining, after_report = plan_shifted_pad_repair(
            reloaded,
            reference,
            shift_x_mils=args.shift_x_mils,
            shift_y_mils=args.shift_y_mils,
            tolerance_mils=args.tolerance_mils,
        )
        if remaining:
            raise PadRepairError(f"{len(remaining)} shifted pads remain after repair")
        changed_records = sum(
            first != second for first, second in zip(before_snapshot, expected_snapshot)
        )
        if changed_records != len(corrections):
            raise PadRepairError("repair changed a different number of pad records than planned")
    except (OSError, ValueError, zipfile.BadZipFile, PadRepairError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    report.update(
        {
            "status": "complete",
            "current": str(current_path),
            "reference": str(reference_path),
            "output": str(output_path),
            "input_sha256": _sha256(current_path),
            "output_sha256": _sha256(output_path),
            "verified_after_reload": True,
            "remaining_corrections": len(remaining),
            "after_compared_pads": after_report["compared_pads"],
            **native_report,
        }
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
