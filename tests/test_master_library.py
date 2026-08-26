from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import altium_monkey as altium

from lcsc_altium_loader.convert import BatchCancelled, ConversionError, _add_footprint, _add_symbol, prepare_libraries
from lcsc_altium_loader.integrity import native_inventory, preserved_inventory, verify_output
from lcsc_altium_loader.library_store import LIBRARY_NAMES, LibraryStore, state_directory
from test_conversion import fetched


class MasterLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "libraries"
        self.environment = patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "state")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        fetch_patch = patch(
            "lcsc_altium_loader.convert._fetch_component",
            side_effect=lambda _client, code, **kwargs: fetched(code, "RECT"),
        )
        self.fetch = fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

    def library_bytes(self) -> dict[str, bytes]:
        return {name: (self.output / name).read_bytes() for name in LIBRARY_NAMES}

    def test_second_batch_retains_the_first_batch(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        manifest, status = prepare_libraries(object(), ["C2"], self.output)

        symbols = altium.AltiumSchLib(self.output / "LCSC.SchLib")
        footprints = altium.AltiumPcbLib.from_file(self.output / "LCSC.PcbLib")
        self.assertEqual(status, 0)
        self.assertEqual({item.name for item in symbols.symbols}, {"C1_SYMBOL", "C2_SYMBOL"})
        self.assertEqual({item.name for item in footprints.footprints}, {"C1_PKG", "C2_PKG"})
        self.assertEqual({item["code"] for item in manifest["components"]}, {"C1", "C2"})

    def test_repeated_code_does_not_fetch_or_rewrite_libraries(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        previous = {name: (self.output / name).read_bytes() for name in ("LCSC.SchLib", "LCSC.PcbLib")}
        self.fetch.reset_mock()

        _, status = prepare_libraries(object(), ["c1", "C1"], self.output, with_3d=True)

        self.assertEqual(status, 0)
        self.fetch.assert_not_called()
        for name, data in previous.items():
            self.assertEqual((self.output / name).read_bytes(), data)

    def test_third_batch_reports_only_new_items_and_keeps_a_cumulative_index(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        prepare_libraries(object(), ["C2"], self.output)
        previous = self.library_bytes()
        self.fetch.reset_mock()

        manifest, status = prepare_libraries(object(), ["C1", "C3", "C2", "c3"], self.output)

        self.assertEqual(status, 0)
        self.assertEqual((manifest["added_count"], manifest["skipped_count"], manifest["total_components"]), (1, 2, 3))
        self.assertEqual(manifest["added_codes"], ["C3"])
        self.assertEqual([call.args[1] for call in self.fetch.call_args_list], ["C3"])
        self.assertTrue(verify_output(self.output)["ok"])
        self.assertEqual({path.name for path in self.output.iterdir()}, set(LIBRARY_NAMES))
        backup = Path(manifest["backup_directory"])
        self.assertFalse(backup.is_relative_to(self.output))
        for name, data in previous.items():
            self.assertEqual((backup / name).read_bytes(), data)

    def test_native_pair_without_manifest_is_adopted_without_refetching(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        (state_directory(self.output) / "manifest.json").unlink()
        previous = native_inventory(self.output)
        self.fetch.reset_mock()

        manifest, status = prepare_libraries(object(), ["C1", "C2"], self.output)

        self.assertEqual(status, 0)
        self.assertEqual([call.args[1] for call in self.fetch.call_args_list], ["C2"])
        self.assertEqual(manifest["total_components"], 2)
        self.assertEqual(manifest["components"][0]["origin"], "existing_native_library")
        self.assertEqual(preserved_inventory(previous, native_inventory(self.output)), [])
        self.assertTrue(verify_output(self.output)["ok"])

    def test_manual_native_changes_are_preserved_even_with_stale_catalog(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        sch = altium.AltiumSchLib(self.output / "LCSC.SchLib")
        sch.symbols[0].pins[0].name = "USER_EDITED_PIN"
        sch.save(self.output / "LCSC.SchLib")
        self.assertEqual(altium.AltiumSchLib(self.output / "LCSC.SchLib").symbols[0].pins[0].name, "USER_EDITED_PIN")
        previous = native_inventory(self.output)

        manifest, status = prepare_libraries(object(), ["C2"], self.output)

        self.assertEqual(status, 0)
        self.assertEqual(preserved_inventory(previous, native_inventory(self.output)), [])
        self.assertEqual(manifest["components"][0]["origin"], "existing_native_library")
        self.assertTrue(verify_output(self.output)["ok"])

    def test_corrupt_external_catalog_is_rebuilt_from_native_files(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        previous = self.library_bytes()
        (state_directory(self.output) / "manifest.json").write_text("broken", encoding="utf-8")
        self.fetch.reset_mock()
        manifest, status = prepare_libraries(object(), ["C1"], self.output)
        self.assertEqual(status, 0)
        self.assertEqual(manifest["status"], "unchanged")
        self.assertEqual(self.library_bytes(), previous)
        self.fetch.assert_not_called()
        self.assertTrue(verify_output(self.output)["ok"])

    def test_malformed_schema3_index_is_rebuilt_instead_of_trusted(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        (state_directory(self.output) / "manifest.json").write_text(
            '{"schema_version":3,"native_inventory":[],"components":null}', encoding="utf-8"
        )
        self.fetch.reset_mock()
        manifest, status = prepare_libraries(object(), ["C1"], self.output)
        self.assertEqual(status, 0)
        self.assertEqual(manifest["total_components"], 1)
        self.fetch.assert_not_called()
        self.assertTrue(verify_output(self.output)["ok"])

    def test_variants_manual_parts_and_unlinked_footprints_are_all_retained(self) -> None:
        self.output.mkdir()
        sch, pcb = altium.AltiumSchLib(), altium.AltiumPcbLib()
        for suffix, code in (("VARIANT_A", "C1"), ("VARIANT_B", "C1"), ("MANUAL", "")):
            component, symbol, footprint, *_ = fetched("C1", "RECT")
            component["code"] = code
            _add_symbol(sch, symbol, suffix, suffix + "_PKG", component, [])
            _add_footprint(pcb, footprint, suffix + "_PKG", component, [])
        pcb.add_footprint("UNLINKED_FOOTPRINT")
        sch.save(self.output / "LCSC.SchLib")
        pcb.save(self.output / "LCSC.PcbLib")
        previous = native_inventory(self.output)

        manifest, status = prepare_libraries(object(), ["C1", "C2"], self.output)

        self.assertEqual(status, 0)
        self.assertEqual(manifest["total_components"], 4)
        self.assertEqual(manifest["skipped_count"], 1)
        self.assertEqual(len(native_inventory(self.output)["footprints"]), 5)
        self.assertEqual(preserved_inventory(previous, native_inventory(self.output)), [])
        self.assertEqual([call.args[1] for call in self.fetch.call_args_list], ["C2"])

    def test_broken_local_footprint_link_stops_before_fetch(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        pcb = altium.AltiumPcbLib()
        pcb.add_footprint("SOME_OTHER_FOOTPRINT")
        pcb.save(self.output / "LCSC.PcbLib")
        before = self.library_bytes()
        self.fetch.reset_mock()
        with self.assertRaisesRegex(ConversionError, "symbol-footprint link is unresolved"):
            prepare_libraries(object(), ["C2"], self.output)
        self.fetch.assert_not_called()
        self.assertEqual(self.library_bytes(), before)

    def test_catalog_commit_failure_rolls_back_both_libraries_and_index(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        before = self.library_bytes()
        catalog = state_directory(self.output) / "manifest.json"
        before_catalog = catalog.read_bytes()
        real_replace = os.replace

        def fail_catalog(source: object, destination: object) -> None:
            if Path(source).name.startswith(".manifest-") and Path(destination) == catalog:
                raise PermissionError("simulated catalog commit failure")
            real_replace(source, destination)

        with patch("lcsc_altium_loader.library_store.os.replace", side_effect=fail_catalog):
            with self.assertRaisesRegex(PermissionError, "catalog commit failure"):
                prepare_libraries(object(), ["C2"], self.output)
        self.assertEqual(self.library_bytes(), before)
        self.assertEqual(catalog.read_bytes(), before_catalog)
        self.assertTrue(verify_output(self.output)["ok"])

    def test_first_publication_failure_leaves_no_half_pair(self) -> None:
        real_replace = os.replace

        def fail_second(source: object, destination: object) -> None:
            if Path(destination) == self.output / "LCSC.PcbLib":
                raise PermissionError("simulated PcbLib lock")
            real_replace(source, destination)

        with patch("lcsc_altium_loader.library_store.os.replace", side_effect=fail_second):
            with self.assertRaises(PermissionError):
                prepare_libraries(object(), ["C1"], self.output)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse((state_directory(self.output) / "manifest.json").exists())

    def test_missing_native_file_blocks_before_fetch(self) -> None:
        self.output.mkdir()
        (self.output / "LCSC.SchLib").write_bytes(b"must not overwrite")
        with self.assertRaisesRegex(RuntimeError, "缺少"):
            prepare_libraries(object(), ["C2"], self.output)
        self.fetch.assert_not_called()
        self.assertEqual((self.output / "LCSC.SchLib").read_bytes(), b"must not overwrite")

    def test_corrupt_native_pair_blocks_before_fetch(self) -> None:
        self.output.mkdir()
        for name in LIBRARY_NAMES:
            (self.output / name).write_bytes(b"must not overwrite")
        with self.assertRaisesRegex(ConversionError, "无法安全读取"):
            prepare_libraries(object(), ["C2"], self.output)
        self.fetch.assert_not_called()
        self.assertEqual(set(self.library_bytes().values()), {b"must not overwrite"})

    def test_name_collisions_are_not_silently_renamed_or_overwritten(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        previous = self.library_bytes()
        for field in (3, 4):
            with self.subTest(field=field):
                item = list(fetched("C2", "RECT"))
                item[field] = "c1_symbol" if field == 3 else "c1_pkg"
                self.fetch.side_effect = lambda *args, **kwargs: tuple(item)
                manifest, status = prepare_libraries(object(), ["C2"], self.output)
                self.assertEqual(status, 1)
                self.assertEqual(manifest["added_count"], 0)
                self.assertIn("name conflict", manifest["failures"][0]["error"])
                self.assertEqual(self.library_bytes(), previous)

    def test_partial_success_retains_history_but_isolates_the_failed_item(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        self.fetch.side_effect = lambda _client, code, **kwargs: fetched(code, "UNSUPPORTED" if code == "C3" else "RECT")
        manifest, status = prepare_libraries(object(), ["C1", "C2", "C3"], self.output)
        self.assertEqual(status, 2)
        self.assertEqual((manifest["added_count"], manifest["skipped_count"], manifest["total_components"]), (1, 1, 2))
        self.assertEqual(set(native_inventory(self.output)["symbols"]), {"C1_SYMBOL", "C2_SYMBOL"})
        self.assertTrue(verify_output(self.output)["ok"])

    def test_cancellation_after_fetch_retains_the_old_pair(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        previous = self.library_bytes()
        stopped = False

        def stop_after_fetch(_client: object, code: str, **kwargs: object) -> tuple:
            nonlocal stopped
            stopped = True
            return fetched(code, "RECT")

        self.fetch.side_effect = stop_after_fetch
        with self.assertRaises(BatchCancelled):
            prepare_libraries(object(), ["C2", "C3"], self.output, cancelled=lambda: stopped)
        self.assertEqual(self.library_bytes(), previous)
        self.assertEqual(list(self.root.glob(".partsbridge-stage-*")), [])
        report = json.loads((state_directory(self.output) / "last-run.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["added_count"], 0)

    def test_external_edit_during_fetch_is_not_overwritten(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        previous = self.library_bytes()

        def edit_during_fetch(_client: object, code: str, **kwargs: object) -> tuple:
            (self.output / "LCSC.SchLib").write_bytes(previous["LCSC.SchLib"] + b"external edit")
            return fetched(code, "RECT")

        self.fetch.side_effect = edit_during_fetch
        with self.assertRaisesRegex(RuntimeError, "其他程序修改"):
            prepare_libraries(object(), ["C2"], self.output)
        self.assertEqual((self.output / "LCSC.SchLib").read_bytes(), previous["LCSC.SchLib"] + b"external edit")
        self.assertEqual((self.output / "LCSC.PcbLib").read_bytes(), previous["LCSC.PcbLib"])

    def test_second_writer_is_rejected_before_fetch(self) -> None:
        with LibraryStore(self.output):
            with self.assertRaisesRegex(RuntimeError, "另一个生成任务"):
                prepare_libraries(object(), ["C1"], self.output)
        self.fetch.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_merged_old_geometry_change_prevents_publication(self) -> None:
        prepare_libraries(object(), ["C1"], self.output)
        previous = self.library_bytes()
        original_merge = altium.AltiumSchLib.merge

        def damaging_merge(paths: list[Path], output: Path, **kwargs: object) -> object:
            library = original_merge(paths, output, **kwargs)
            library.get_symbol("C1_SYMBOL").pins[0].name = "damaged"
            library.save(output)
            return library

        with patch.object(altium.AltiumSchLib, "merge", side_effect=damaging_merge):
            with self.assertRaisesRegex(ConversionError, "preservation failed"):
                prepare_libraries(object(), ["C2"], self.output)
        self.assertEqual(self.library_bytes(), previous)

    def test_embedded_model_id_metadata_and_payload_survive_append(self) -> None:
        step = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;"
        item = list(fetched("C1", "RECT"))
        item[-1] = step
        self.fetch.side_effect = lambda *args, **kwargs: tuple(item)
        first, status = prepare_libraries(object(), ["C1"], self.output, with_3d=True)
        self.assertEqual(status, 0)
        self.assertEqual(first["components"][0]["3d"]["status"], "embedded")
        before = native_inventory(self.output)
        self.assertEqual(len(before["models"]), 1)
        self.fetch.side_effect = lambda _client, code, **kwargs: fetched(code, "RECT")

        prepare_libraries(object(), ["C2"], self.output)

        self.assertEqual(preserved_inventory(before, native_inventory(self.output)), [])
        self.assertTrue(verify_output(self.output)["ok"])

    def test_transformed_models_survive_publication_and_later_append(self) -> None:
        step = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;"
        poses = (
            # The cached EasyEDA pose of the reported failing C41355418.
            ("C41355418", (0.0, 0.0, 0.0), (0.0, -2.050008600000042, -3.9999919999999998)),
            ("C2", (90.0, 180.0, 45.1234), (1.25, -0.3, 1.234567)),
            ("C3", (-90.00001, 0.00001, -45.1236), (-1.5, 2.0, -0.00000127)),
        )
        for code, rotation, translation in poses:
            with self.subTest(code=code):
                output = self.root / code
                self.fetch.side_effect = lambda _client, code, **kwargs: fetched(code, "RECT")
                prepare_libraries(object(), ["C10"], output)
                original = native_inventory(output)
                item = list(fetched(code, "RECT"))
                item[2].model_3d = SimpleNamespace(
                    uuid="offline-test-model",
                    rotation=SimpleNamespace(**dict(zip(("x", "y", "z"), rotation))),
                    translation=SimpleNamespace(**dict(zip(("x", "y", "z"), translation))),
                )
                item[-1] = step
                self.fetch.side_effect = lambda *args, **kwargs: tuple(item)

                manifest, status = prepare_libraries(object(), [code], output, with_3d=True)

                self.assertEqual(status, 0)
                self.assertEqual(manifest["components"][-1]["3d"]["status"], "embedded")
                self.assertEqual(preserved_inventory(original, native_inventory(output)), [])
                pcb = altium.AltiumPcbLib.from_file(output / "LCSC.PcbLib")
                model, compressed = pcb.get_embedded_model_entries()[0]
                body = next(fp for fp in pcb.footprints if fp.name == code + "_PKG").component_bodies[0]
                self.assertEqual(model.id, "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, code)).upper() + "}")
                self.assertEqual(zlib.decompress(compressed), step)
                self.assertEqual((model.rotation_x, model.rotation_y, model.rotation_z), tuple(round(value, 3) for value in rotation))
                self.assertEqual(model.z_offset, round(translation[2] / 25.4 * 1000 * 10000))
                self.assertEqual((body.model_3d_rotx, body.model_3d_roty, body.model_3d_rotz), (model.rotation_x, model.rotation_y, model.rotation_z))
                self.assertEqual(body.model_3d_dz, model.z_offset)
                before_append = native_inventory(output)
                self.fetch.side_effect = lambda _client, code, **kwargs: fetched(code, "RECT")

                prepare_libraries(object(), ["C11"], output)

                self.assertEqual(preserved_inventory(before_append, native_inventory(output)), [])
                self.assertTrue(verify_output(output)["ok"])

    def test_damaged_merged_model_metadata_or_payload_still_blocks_publication(self) -> None:
        step = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;"
        item = list(fetched("C1", "RECT"))
        item[-1] = step
        self.fetch.side_effect = lambda *args, **kwargs: tuple(item)
        prepare_libraries(object(), ["C1"], self.output, with_3d=True)
        previous = self.library_bytes()
        self.fetch.side_effect = lambda _client, code, **kwargs: fetched(code, "RECT")
        original_combine = altium.AltiumPcbLib.combine

        for damage in ("metadata", "payload"):
            with self.subTest(damage=damage):
                def damaging_combine(*args: object, **kwargs: object) -> object:
                    pcb = original_combine(*args, **kwargs)
                    if damage == "metadata":
                        pcb.raw_models_data = pcb.raw_models_data.replace(b"ROTX=0.000", b"ROTX=1.000", 1)
                    else:
                        pcb.raw_models[0] = zlib.compress(step + b"\nchanged")
                    return pcb

                with patch.object(altium.AltiumPcbLib, "combine", side_effect=damaging_combine):
                    with self.assertRaisesRegex(ConversionError, "models changed or missing"):
                        prepare_libraries(object(), ["C2"], self.output)

                self.assertEqual(self.library_bytes(), previous)
                self.assertTrue(verify_output(self.output)["ok"])


class StoreRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "libraries"
        self.output.mkdir()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        for name in LIBRARY_NAMES:
            (self.output / name).write_bytes(("old-" + name).encode())
        self.state = state_directory(self.output)
        self.state.mkdir(parents=True)
        (self.state / "manifest.json").write_bytes(b"old manifest")

    def interrupt_publish(self, target_name: str, *, exception: type[BaseException] = SystemExit) -> None:
        real_replace = os.replace

        def interrupt(source: object, destination: object) -> None:
            real_replace(source, destination)
            if Path(destination) == (self.state if target_name == "manifest.json" else self.output) / target_name:
                raise exception("simulated process interruption")

        with self.assertRaises(exception), LibraryStore(self.output) as store:
            store.snapshot()
            for name in LIBRARY_NAMES:
                (store.stage / name).write_bytes(("new-" + name).encode())
            with patch("lcsc_altium_loader.library_store.os.replace", side_effect=interrupt):
                store.publish({"test": "new manifest"})

    def test_crash_after_first_replace_rolls_back_on_next_open(self) -> None:
        self.interrupt_publish("LCSC.SchLib")
        self.assertTrue((self.state / "transaction.json").exists())
        with LibraryStore(self.output):
            pass
        for name in LIBRARY_NAMES:
            self.assertEqual((self.output / name).read_bytes(), ("old-" + name).encode())
        self.assertEqual((self.state / "manifest.json").read_bytes(), b"old manifest")
        self.assertFalse((self.state / "transaction.json").exists())

    def test_crash_after_both_libraries_before_catalog_recovers_consistently(self) -> None:
        self.interrupt_publish("LCSC.PcbLib")
        with LibraryStore(self.output):
            pass
        for name in LIBRARY_NAMES:
            self.assertEqual((self.output / name).read_bytes(), ("old-" + name).encode())

    def test_crash_after_catalog_commit_keeps_complete_new_pair(self) -> None:
        self.interrupt_publish("manifest.json")
        with LibraryStore(self.output):
            pass
        for name in LIBRARY_NAMES:
            self.assertEqual((self.output / name).read_bytes(), ("new-" + name).encode())
        self.assertEqual(json.loads((self.state / "manifest.json").read_text(encoding="utf-8"))["test"], "new manifest")
        self.assertFalse((self.state / "transaction.json").exists())

    def test_external_edit_after_crash_blocks_recovery_without_any_overwrite(self) -> None:
        self.interrupt_publish("LCSC.SchLib")
        (self.output / "LCSC.SchLib").write_bytes(b"user edit after crash")
        before = {name: (self.output / name).read_bytes() for name in LIBRARY_NAMES}
        with self.assertRaisesRegex(RuntimeError, "随后发生变化"):
            with LibraryStore(self.output):
                pass
        self.assertEqual({name: (self.output / name).read_bytes() for name in LIBRARY_NAMES}, before)
        self.assertTrue((self.state / "transaction.json").exists())

    def test_bad_backup_blocks_recovery_before_touching_any_target(self) -> None:
        self.interrupt_publish("LCSC.SchLib")
        journal = json.loads((self.state / "transaction.json").read_text(encoding="utf-8"))
        (self.state / "backups" / journal["run_id"] / "LCSC.PcbLib").write_bytes(b"bad backup")
        before = {name: (self.output / name).read_bytes() for name in LIBRARY_NAMES}
        with self.assertRaisesRegex(RuntimeError, "恢复备份不完整"):
            with LibraryStore(self.output):
                pass
        self.assertEqual({name: (self.output / name).read_bytes() for name in LIBRARY_NAMES}, before)


if __name__ == "__main__":
    unittest.main()
