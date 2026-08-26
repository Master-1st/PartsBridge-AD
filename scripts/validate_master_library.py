"""Offline acceptance on a COPY of a real native pair; never writes the source."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import altium_monkey as altium

from lcsc_altium_loader.convert import prepare_libraries
from lcsc_altium_loader.integrity import native_inventory, verify_output
from lcsc_altium_loader.library_store import LIBRARY_NAMES, output_metadata, write_json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_conversion import fetched


def verify_original_objects(source: Path, destination: Path) -> dict[str, int]:
    old_sch = altium.AltiumSchLib(source / "LCSC.SchLib")
    new_sch = altium.AltiumSchLib(destination / "LCSC.SchLib")
    old_pcb = altium.AltiumPcbLib.from_file(source / "LCSC.PcbLib")
    new_pcb = altium.AltiumPcbLib.from_file(destination / "LCSC.PcbLib")
    for font_id, value in old_sch.font_manager.fonts.items():
        assert new_sch.font_manager.fonts[font_id] == value
    for old in old_sch.symbols:
        new = new_sch.get_symbol(old.name)
        assert new is not None and new.part_count == old.part_count
        assert new.component_record == old.component_record
        assert [obj.serialize_to_record() for obj in new.objects] == [obj.serialize_to_record() for obj in old.objects], old.name
    footprints = {fp.name: fp for fp in new_pcb.footprints}
    for old in old_pcb.footprints:
        new = footprints[old.name]
        assert new.parameters == old.parameters
        assert [obj.serialize_to_binary() for obj in new.primitives] == [obj.serialize_to_binary() for obj in old.primitives], old.name
    models = {model.id: (model.serialize_to_binary(), zlib.decompress(data)) for model, data in new_pcb.get_embedded_model_entries()}
    for model, data in old_pcb.get_embedded_model_entries():
        assert models[model.id] == (model.serialize_to_binary(), zlib.decompress(data)), model.id
    return {"symbols": len(old_sch.symbols), "footprints": len(old_pcb.footprints), "embedded_models": len(old_pcb.get_embedded_model_entries())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New acceptance-artifact directory outside the source library")
    args = parser.parse_args()
    source, root = args.source.resolve(), args.output.resolve()
    if root == source or root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError("Acceptance output must be separate from the source library tree")
    root.mkdir(parents=True, exist_ok=False)
    output = root / "library-copy"
    output.mkdir()
    before = {name: output_metadata(source / name) for name in LIBRARY_NAMES}
    for name in LIBRARY_NAMES:
        shutil.copy2(source / name, output / name)
    initial = native_inventory(output)
    baseline_count = len(initial["symbols"])
    existing_code = next(value["code"] for value in initial["symbols"].values() if value["code"])
    synthetic_codes = ["C900000001", "C900000002"]
    assert not set(synthetic_codes) & {item["code"] for item in initial["symbols"].values()}
    source_pcb = altium.AltiumPcbLib.from_file(source / "LCSC.PcbLib")
    step = zlib.decompress(source_pcb.get_embedded_model_entries()[0][1])
    fetched_codes: list[str] = []
    poses = {
        synthetic_codes[0]: ((0.0, 0.0, 0.0), (0.0, -2.050008600000042, -3.9999919999999998)),
        synthetic_codes[1]: ((90.0, 180.0, 45.1234), (1.25, -0.3, 1.234567)),
    }

    def offline_fetch(_client: object, code: str, **kwargs: object) -> tuple:
        assert code in synthetic_codes, f"Existing item unexpectedly fetched: {code}"
        fetched_codes.append(code)
        result = list(fetched(code, "RECT"))
        result[0]["mpn"] = f"SYNTHETIC-OFFLINE-APPEND-CHECK-{code}"
        rotation, translation = poses[code]
        result[2].model_3d = SimpleNamespace(
            uuid="offline-reused-step",
            rotation=SimpleNamespace(**dict(zip(("x", "y", "z"), rotation))),
            translation=SimpleNamespace(**dict(zip(("x", "y", "z"), translation))),
        )
        result[-1] = step
        return tuple(result)

    def hashes() -> dict[str, dict]:
        return {name: output_metadata(output / name) for name in LIBRARY_NAMES}

    try:
        with patch.dict("os.environ", {"LOCALAPPDATA": str(root / "app-data")}), \
             patch("socket.socket.connect", side_effect=AssertionError("Network is prohibited in acceptance")) as network, \
             patch("lcsc_altium_loader.convert._fetch_component", side_effect=offline_fetch):
            adopted, status = prepare_libraries(object(), [existing_code], output, with_3d=True)
            assert status == 0 and adopted["added_count"] == 0
            assert hashes() == before and not fetched_codes
            first, status = prepare_libraries(object(), [existing_code, synthetic_codes[0]], output, with_3d=True)
            assert status == 0 and first["total_components"] == baseline_count + 1
            first_hashes = hashes()
            repeated, status = prepare_libraries(object(), [synthetic_codes[0], existing_code], output, with_3d=True)
            assert status == 0 and repeated["skipped_count"] == 2 and repeated["added_count"] == 0
            assert hashes() == first_hashes and fetched_codes == synthetic_codes[:1]
            second, status = prepare_libraries(object(), [synthetic_codes[1]], output, with_3d=True)
            assert status == 0 and second["total_components"] == baseline_count + 2
            assert fetched_codes == synthetic_codes
            preserved = verify_original_objects(source, output)
            verification = verify_output(output)
            assert verification["ok"], verification
            assert {path.name for path in output.iterdir()} == set(LIBRARY_NAMES)
            assert network.call_count == 0
            final = native_inventory(output)
            assert len(final["models"]) == len(initial["models"]) + 2
            report = {
                "ok": True,
                "scope": "offline_copy_only_no_new_real_components",
                "source": str(source), "output": str(output),
                "source_hashes": before,
                "counts_across_runs": [baseline_count, first["total_components"], repeated["total_components"], second["total_components"]],
                "unchanged_original_objects": preserved,
                "symbol_records_identical": True,
                "pcb_primitive_bytes_identical": True,
                "old_step_payload_and_metadata_bytes_identical": True,
                "total_embedded_models_in_copy": len(final["models"]),
                "model_network_requests": 0,
                "synthetic_fixture_reads": fetched_codes,
                "synthetic_model_poses_degrees_mm": poses,
                "repeat_downloads": 0,
                "library_directory_contains_only_native_pair": True,
                "verification": verification,
                "manual_engineering_validation": "not performed; synthetic parts stay in test copy",
            }
    finally:
        assert {name: output_metadata(source / name) for name in LIBRARY_NAMES} == before, "Source library changed during acceptance"
    report["source_library_unchanged"] = True
    write_json(root / "acceptance.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
