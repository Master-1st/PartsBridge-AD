from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import altium_monkey as altium

from lcsc_altium_loader.convert import (
    BatchCancelled,
    _add_footprint,
    _add_symbol,
    _import_symbol,
    _import_footprint,
    _path_points,
    _svg_arc,
    prepare_libraries,
)
from lcsc_altium_loader.integrity import verify_output
from lcsc_altium_loader.library_store import LibraryStore, state_directory


def minimal_symbol() -> SimpleNamespace:
    pin = SimpleNamespace(
        settings=SimpleNamespace(
            pos_x=0,
            pos_y=0,
            rotation=0,
            spice_pin_number="1",
            type=SimpleNamespace(value=0),
        ),
        pin_path=SimpleNamespace(path="h 10"),
        name=SimpleNamespace(text="PIN", is_displayed=True),
    )
    return SimpleNamespace(
        bbox=SimpleNamespace(x=0, y=0),
        info=SimpleNamespace(prefix="U?"),
        pins=[pin],
        rectangles=[],
        ellipses=[],
        circles=[],
        arcs=[],
        polylines=[],
        polygons=[],
        paths=[],
        texts=[],
        sub_symbols=[],
    )


def resistor_symbol() -> SimpleNamespace:
    def pin(number: str, x: float, rotation: int, path: str) -> SimpleNamespace:
        return SimpleNamespace(
            settings=SimpleNamespace(
                pos_x=x,
                pos_y=0,
                rotation=rotation,
                spice_pin_number=number,
                type=SimpleNamespace(value=1),
            ),
            pin_path=SimpleNamespace(path=path),
            name=SimpleNamespace(text=number, is_displayed=False),
        )

    rectangle = SimpleNamespace(
        pos_x=10,
        pos_y=-4,
        width=20,
        height=8,
        stroke_color="#000000",
        stroke_width=1,
        fill_color="none",
    )
    return SimpleNamespace(
        bbox=SimpleNamespace(x=20, y=0),
        info=SimpleNamespace(prefix="R"),
        pins=[pin("1", 0, 180, "M 0 0 h10"), pin("2", 40, 0, "M 40 0 h-10")],
        rectangles=[rectangle],
        ellipses=[],
        circles=[],
        arcs=[],
        polylines=[],
        polygons=[],
        paths=[],
        texts=[],
        sub_symbols=[],
    )
def minimal_footprint(shape: str) -> SimpleNamespace:
    pad = SimpleNamespace(
        center_x=0,
        center_y=0,
        width=1,
        height=1,
        shape=shape,
        layer_id=1,
        number="1",
        rotation=0,
        hole_radius=0,
        is_plated=False,
    )
    return SimpleNamespace(
        bbox=SimpleNamespace(x=0, y=0, x_px=0, y_px=0),
        info=SimpleNamespace(name="PKG"),
        pads=[pad],
        tracks=[],
        arcs=[],
        circles=[],
        solid_regions=[],
        holes=[],
        vias=[],
        rectangles=[],
        texts=[],
        model_3d=None,
    )


def fetched(code: str, shape: str) -> tuple:
    component = {
        "code": code,
        "mpn": f"MPN-{code}",
        "name": code,
        "description": "test",
        "manufacturer": "Maker",
        "package": "PKG",
        "source_urls": {"lcsc": f"https://item.szlcsc.com/{code}.html"},
    }
    return (
        component,
        minimal_symbol(),
        minimal_footprint(shape),
        f"{code}_SYMBOL",
        f"{code}_PKG",
        [],
        None,
    )


class ConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": self.state.name})
        environment.start()
        self.addCleanup(environment.stop)
        preflight = patch("lcsc_altium_loader.convert.preflight_ad_write", return_value=None)
        preflight.start()
        self.addCleanup(preflight.stop)

    def test_import_restores_vertical_pin_path_and_authored_origin(self) -> None:
        source = minimal_symbol()
        source.bbox.x, source.bbox.y = 1.25, 2.35
        source.pins[0].settings.id = "pin1"
        source.pins[0].pin_path.path = "M 0 -20 h 17"
        data = {"dataStr": {"head": {"x": 0, "y": 0}, "shape": [
            "P~show~0~1~0~-20~270~pin1~0^^0~-20^^M 0 -20 v 17~#000000"
        ]}}
        with patch("lcsc_altium_loader.convert.EasyedaSymbolImporter") as importer:
            importer.return_value.get_symbol.return_value = source
            restored = _import_symbol(data)
        self.assertEqual(restored.pins[0].pin_path.path, "M 0 -20 v 17")
        self.assertEqual((restored.bbox.x, restored.bbox.y), (0, 0))

    def test_import_retains_native_plated_through_hole_flag(self) -> None:
        source = minimal_footprint("CIRCLE")
        source.pads[0].id = "pad1"
        source.pads[0].is_plated = False
        data = {"packageDetail": {"dataStr": {"shape": [
            "PAD~ELLIPSE~0~0~4~4~11~~1~1~~0~pad1~0~~Y"
        ]}}}
        with patch("lcsc_altium_loader.convert.EasyedaFootprintImporter") as importer:
            importer.return_value.get_footprint.return_value = source
            restored = _import_footprint(data)
        self.assertTrue(restored.pads[0].is_plated)

    def test_single_part_pins_belong_to_visible_part_one_after_round_trip(self) -> None:
        library = altium.AltiumSchLib()
        _add_symbol(library, minimal_symbol(), "PART_ONE", "PKG", {}, [])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "single.SchLib"
            library.save(path)
            parsed = altium.AltiumSchLib(path).symbols[0]
        self.assertEqual(parsed.part_count, 1)
        self.assertEqual([pin.owner_part_id for pin in parsed.pins], [1])

    def test_filled_body_is_drawn_before_pin_names_after_round_trip(self) -> None:
        source = minimal_symbol()
        source.rectangles = [SimpleNamespace(
            pos_x=10, pos_y=-10, width=50, height=20,
            stroke_color="#000000", stroke_width=1, fill_color="#FFFFFF",
        )]
        library = altium.AltiumSchLib()
        _add_symbol(library, source, "FILLED_BODY", "PKG", {}, [])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "filled.SchLib"
            library.save(path)
            parsed = altium.AltiumSchLib(path)
            svg = parsed.symbol_to_svg("FILLED_BODY", part_id=1)
        self.assertIn(">PIN</text>", svg)
        self.assertLess(svg.rfind("<rect "), svg.index(">PIN</text>"))

    def test_resistor_pins_face_body_and_are_passive_after_round_trip(self) -> None:
        library = altium.AltiumSchLib()
        component = {
            "code": "C25804",
            "mpn": "0603WAF1002T5E",
            "name": "10k",
            "description": "resistor",
            "manufacturer": "Maker",
            "source_urls": {"lcsc": "https://item.szlcsc.com/26547.html"},
        }
        _add_symbol(library, resistor_symbol(), "C25804_R", "C25804_0603", component, [])

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resistor.SchLib"
            library.save(path)
            parsed = altium.AltiumSchLib(path).symbols[0]

        pins = {pin.designator: pin for pin in parsed.pins}
        # Native Altium pin location is the body end; orientation points OUT.
        self.assertEqual(int(pins["1"].orientation), 2)
        self.assertEqual(int(pins["2"].orientation), 0)
        self.assertEqual(pins["1"].x_mils, -100)
        self.assertEqual(pins["2"].x_mils, 100)
        self.assertEqual(pins["1"].get_hot_spot().x * 10, -200)
        self.assertEqual(pins["2"].get_hot_spot().x * 10, 200)
        self.assertEqual(pins["1"].electrical, altium.PinElectrical.PASSIVE)
        self.assertEqual(pins["2"].electrical, altium.PinElectrical.PASSIVE)

    def test_pin_geometry_uses_path_and_handles_reversed_path_endpoints(self) -> None:
        cases = [
            (270, (0, -20), "M 0 -20 v 17", (0, 30), 1, (0, 200)),
            (90, (0, 20), "M 0 20 v -19", (0, -10), 3, (0, -200)),
            (180, (0, 0), "M 10 0 h -10", (100, 0), 2, (0, 0)),
        ]
        for rotation, source_tip, path_text, body, orientation, tip in cases:
            with self.subTest(path=path_text):
                source = minimal_symbol()
                source.pins[0].settings.rotation = rotation
                source.pins[0].settings.pos_x, source.pins[0].settings.pos_y = source_tip
                source.pins[0].pin_path.path = path_text
                library = altium.AltiumSchLib()
                _add_symbol(library, source, "PIN_GEOMETRY", "PKG", {}, [])
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "pins.SchLib"
                    library.save(path)
                    pin = altium.AltiumSchLib(path).symbols[0].pins[0]
                self.assertEqual((pin.x_mils, pin.y_mils), body)
                self.assertEqual(int(pin.orientation), orientation)
                hot_spot = pin.get_hot_spot()
                self.assertEqual((hot_spot.x * 10, hot_spot.y * 10), tip)

    def test_symbol_polylines_polygons_paths_and_text_round_trip(self) -> None:
        source = minimal_symbol()
        source.polylines = [
            SimpleNamespace(
                points="0 0 10 0 10 10",
                stroke_color="#102030",
                stroke_width="1",
                fill_color=False,
            )
        ]
        source.polygons = [
            SimpleNamespace(
                points="0 0 10 0 5 10",
                stroke_color="#000000",
                stroke_width="1",
                fill_color=True,
            )
        ]
        source.paths = [
            SimpleNamespace(
                paths="M 0 0 L 10 0 C 10 0 15 5 20 0 Q 25 -5 30 0",
                stroke_color="#000000",
                stroke_width="1",
                fill_color=False,
            )
        ]
        source.texts = [SimpleNamespace(text="A", pos_x=5, pos_y=5, rotation=90)]
        source.arcs = [
            SimpleNamespace(
                path=[
                    SimpleNamespace(start_x=0, start_y=0),
                    SimpleNamespace(
                        radius_x=5,
                        radius_y=5,
                        x_axis_rotation=0,
                        flag_large_arc=False,
                        flag_sweep=True,
                        end_x=10,
                        end_y=0,
                    ),
                ],
                stroke_color="#000000",
                stroke_width="1",
            )
        ]
        library = altium.AltiumSchLib()
        _, counts = _add_symbol(
            library,
            source,
            "C1_GRAPHICS",
            "C1_PACKAGE",
            {
                "code": "C1",
                "mpn": "GRAPHICS",
                "name": "graphics",
                "description": "test",
                "manufacturer": "Maker",
                "source_urls": {"lcsc": "https://item.szlcsc.com/1.html"},
            },
            [],
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "graphics.SchLib"
            library.save(path)
            parsed = altium.AltiumSchLib(path).symbols[0]

        self.assertEqual(counts["POLYLINE"], 1)
        self.assertEqual(counts["POLYGON"], 1)
        self.assertEqual(counts["PATH"], 3)
        self.assertEqual(counts["ARC"], 1)
        self.assertGreaterEqual(len(parsed.polylines), 2)
        self.assertEqual(len(parsed.polygons), 1)
        self.assertEqual(len(parsed.beziers), 2)
        self.assertEqual(len(parsed.arcs), 1)
        self.assertEqual([label.text for label in parsed.labels], ["A"])

    def test_multi_unit_symbol_preserves_parts_and_pin_ownership(self) -> None:
        unit_1 = minimal_symbol()
        unit_2 = minimal_symbol()
        unit_2.pins[0].settings.spice_pin_number = "2"
        source = minimal_symbol()
        source.pins = []
        source.sub_symbols = [unit_1, unit_2]
        library = altium.AltiumSchLib()
        _add_symbol(
            library,
            source,
            "C2_MULTI",
            "C2_PACKAGE",
            {
                "code": "C2",
                "mpn": "MULTI",
                "name": "multi",
                "description": "test",
                "manufacturer": "Maker",
                "source_urls": {"lcsc": "https://item.szlcsc.com/2.html"},
            },
            [],
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "multi.SchLib"
            library.save(path)
            parsed = altium.AltiumSchLib(path).symbols[0]

        self.assertEqual(parsed.part_count, 2)
        self.assertEqual(
            {pin.designator: pin.owner_part_id for pin in parsed.pins},
            {"1": 1, "2": 2},
        )

    def test_custom_pad_hole_via_rectangle_and_text_round_trip(self) -> None:
        source = minimal_footprint("POLYGON")
        source.pads[0].points = "-1 -1 1 -1 1 1 -1 1"
        source.pads[0].hole_length = 0
        source.holes = [
            SimpleNamespace(center_x=2.54, center_y=0, radius=0.5)
        ]
        source.vias = [
            SimpleNamespace(center_x=-2.54, center_y=0, diameter=1.0, radius=0.25)
        ]
        source.rectangles = [
            SimpleNamespace(
                x=-1,
                y=-1,
                width=2,
                height=2,
                stroke_width=0.2,
                layer_id=3,
            )
        ]
        source.texts = [
            SimpleNamespace(
                type="P",
                center_x=0,
                center_y=2,
                stroke_width=0.1,
                rotation=0,
                layer_id=3,
                font_size=1,
                text="REF",
                is_displayed=True,
            )
        ]
        library = altium.AltiumPcbLib()
        _, counts, _ = _add_footprint(
            library,
            source,
            "C3_CUSTOM",
            {
                "code": "C3",
                "mpn": "CUSTOM",
                "name": "custom",
                "package": "CUSTOM",
                "manufacturer": "Maker",
                "source_urls": {"lcsc": "https://item.szlcsc.com/3.html"},
            },
            [],
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "custom.PcbLib"
            library.save(path)
            parsed = altium.AltiumPcbLib.from_file(path).footprints[0]

        self.assertEqual(counts["PAD"], 2)
        self.assertEqual(counts["CUSTOM_PAD"], 1)
        self.assertEqual(counts["HOLE"], 1)
        self.assertEqual(counts["VIA"], 1)
        self.assertEqual(counts["RECT"], 1)
        self.assertEqual(counts["TEXT"], 1)
        self.assertEqual(len(parsed.pads), 2)
        self.assertEqual(
            len([pad for pad in parsed.pads if pad.custom_shape is not None]), 1
        )
        self.assertEqual(len(parsed.vias), 1)
        self.assertEqual(len(parsed.tracks), 4)
        self.assertEqual(len(parsed.texts), 1)

    def test_svg_semicircle_uses_correct_center(self) -> None:
        geometry = _svg_arc("M 1 0 A 1 1 0 0 1 -1 0")

        self.assertIsNotNone(geometry)
        (cx, cy), radius, start, end = geometry
        self.assertAlmostEqual(cx, 0.0, places=7)
        self.assertAlmostEqual(cy, 0.0, places=7)
        self.assertAlmostEqual(radius, 1.0, places=7)
        self.assertAlmostEqual(abs(end - start), 180.0, places=7)
        large = _svg_arc("M 384 299.98 A 4 4 0 1 1 392 300.02")
        self.assertIsNotNone(large)
        self.assertLess(abs(large[3] - large[2]), 360.0)
        self.assertAlmostEqual(abs(large[3] - large[2]), 180.0, places=5)

    def test_polygon_path_rejects_curves(self) -> None:
        self.assertEqual(_path_points("M 0 0 Q 1 1 2 0 Z"), [])
        self.assertEqual(_path_points("M 0 0 L 1 0 L 1 1 Z"), [(0, 0), (1, 0), (1, 1)])

    def test_failed_component_cannot_leak_into_published_libraries(self) -> None:
        def fake_fetch(_client: object, code: str, *, with_3d: bool = False) -> tuple:
            del with_3d
            return fetched(code, "RECT" if code == "C1" else "UNSUPPORTED")

        with tempfile.TemporaryDirectory() as temporary, patch(
            "lcsc_altium_loader.convert._fetch_component", side_effect=fake_fetch
        ):
            output = Path(temporary)
            manifest, status = prepare_libraries(object(), ["C1", "C2"], output)
            symbols = altium.AltiumSchLib(output / "LCSC.SchLib")
            footprints = altium.AltiumPcbLib.from_file(output / "LCSC.PcbLib")

        self.assertEqual(status, 2)
        self.assertEqual([item["code"] for item in manifest["components"]], ["C1"])
        self.assertEqual([item["code"] for item in manifest["failures"]], ["C2"])
        self.assertEqual([symbol.name for symbol in symbols.symbols], ["C1_SYMBOL"])
        self.assertEqual([footprint.name for footprint in footprints.footprints], ["C1_PKG"])

    def test_all_failed_batch_retains_previous_good_libraries(self) -> None:
        def fake_fetch(_client: object, code: str, *, with_3d: bool = False) -> tuple:
            del with_3d
            return fetched(code, "RECT" if code == "C1" else "UNSUPPORTED")

        with tempfile.TemporaryDirectory() as temporary, patch(
            "lcsc_altium_loader.convert._fetch_component", side_effect=fake_fetch
        ):
            output = Path(temporary)
            prepare_libraries(object(), ["C1"], output)
            previous = {name: (output / name).read_bytes() for name in ("LCSC.SchLib", "LCSC.PcbLib")}
            previous_manifest = (state_directory(output) / "manifest.json").read_bytes()

            manifest, status = prepare_libraries(object(), ["C2"], output)

            self.assertEqual(status, 1)
            self.assertFalse(manifest["published"])
            self.assertTrue(manifest["retained_previous_libraries"])
            for name, value in previous.items():
                self.assertEqual((output / name).read_bytes(), value)
            self.assertEqual((state_directory(output) / "manifest.json").read_bytes(), previous_manifest)
            last_run = json.loads((state_directory(output) / "last-run.json").read_text(encoding="utf-8"))
            self.assertEqual(last_run["status"], "failed")

    def test_cancelled_batch_does_not_publish_or_leave_stage_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "LCSC.SchLib").write_bytes(b"previous-sch")

            with self.assertRaises(BatchCancelled):
                prepare_libraries(
                    object(), ["C1"], output, cancelled=lambda: True
                )

            self.assertEqual((output / "LCSC.SchLib").read_bytes(), b"previous-sch")
            last_run = json.loads((state_directory(output) / "last-run.json").read_text(encoding="utf-8"))
            self.assertEqual(last_run["status"], "cancelled")
            self.assertEqual(list(output.glob(".lcsc-stage-*")), [])

    def test_publish_failure_rolls_back_every_replaced_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            names = ["LCSC.SchLib", "LCSC.PcbLib"]
            for name in names:
                (output / name).write_bytes(("old-" + name).encode())
            real_replace = os.replace

            def fail_second(source: object, destination: object) -> None:
                source_path = Path(source)
                if source_path.name == "LCSC.PcbLib" and source_path.parent.name.startswith(".partsbridge-stage-"):
                    raise PermissionError("simulated locked PcbLib")
                real_replace(source, destination)

            with LibraryStore(output) as store:
                store.snapshot()
                for name in names:
                    (store.stage / name).write_bytes(("new-" + name).encode())
                with patch("lcsc_altium_loader.library_store.os.replace", side_effect=fail_second):
                    with self.assertRaises(PermissionError):
                        store.publish({"test": True})

            for name in names:
                self.assertEqual((output / name).read_bytes(), ("old-" + name).encode())
            self.assertFalse((state_directory(output) / "manifest.json").exists())
            self.assertFalse((state_directory(output) / "transaction.json").exists())

    def test_integrity_verifier_detects_output_tampering(self) -> None:
        def fake_fetch(_client: object, code: str, *, with_3d: bool = False) -> tuple:
            del with_3d
            return fetched(code, "RECT")

        with tempfile.TemporaryDirectory() as temporary, patch(
            "lcsc_altium_loader.convert._fetch_component", side_effect=fake_fetch
        ):
            output = Path(temporary)
            manifest, status = prepare_libraries(object(), ["C1"], output)
            before = verify_output(output)
            with (output / "LCSC.SchLib").open("ab") as handle:
                handle.write(b"tampered")
            after = verify_output(output)

        self.assertEqual(status, 0)
        self.assertEqual(manifest["static_verification"]["status"], "passed")
        self.assertTrue(before["ok"])
        self.assertFalse(after["ok"])
        self.assertIn("size mismatch: LCSC.SchLib", after["errors"])


if __name__ == "__main__":
    unittest.main()
