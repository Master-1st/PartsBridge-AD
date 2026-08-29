from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import altium_monkey as altium

from lcsc_altium_loader.pcbdoc_repair import (
    PadRepairError,
    apply_pad_corrections,
    model_table_counts,
    native_stream_inventory,
    plan_shifted_pad_repair,
    write_pad_stream_only,
)


def component(
    designator: str,
    unique_id: str,
    *,
    x: float,
    y: float,
    rotation: float = 0.0,
    layer: str = "TOP",
) -> SimpleNamespace:
    value = SimpleNamespace(
        designator=designator,
        footprint=f"FP_{designator}",
        unique_id=unique_id,
        layer=layer,
    )
    value.get_x_mils = lambda: x
    value.get_y_mils = lambda: y
    value.get_rotation_degrees = lambda: rotation
    return value


def pad(
    designator: str,
    component_index: int,
    *,
    x: float,
    y: float,
) -> SimpleNamespace:
    return SimpleNamespace(
        designator=designator,
        component_index=component_index,
        net_index=1,
        x=round(x * 10000),
        y=round(y * 10000),
        width=100000,
        height=50000,
        hole_size=0,
        shape=1,
        rotation=0.0,
        is_plated=True,
    )


class PcbDocPadRepairTests(unittest.TestCase):
    def test_repairs_only_known_shift_and_leaves_new_components_untouched(self) -> None:
        reference = SimpleNamespace(
            components=[component("U1", "uid-1", x=100, y=200)],
            pads=[
                pad("1", 0, x=90, y=200),
                pad("2", 0, x=110, y=200),
            ],
        )
        current = SimpleNamespace(
            components=[
                component("U1", "uid-1", x=500, y=600, rotation=90),
                component("U2", "new-uid", x=0, y=0),
            ],
            pads=[
                pad("1", 0, x=705, y=590),
                pad("2", 0, x=500, y=610),
                pad("1", 1, x=0, y=0),
            ],
        )

        corrections, report = plan_shifted_pad_repair(
            current, reference, shift_x_mils=205, shift_y_mils=0
        )

        self.assertEqual(report["compared_pads"], 2)
        self.assertEqual(report["correction_count"], 1)
        self.assertEqual(report["unchanged_count"], 1)
        self.assertEqual(report["unmatched_current_components"], ["U2"])
        apply_pad_corrections(current, corrections)
        self.assertEqual((current.pads[0].x, current.pads[0].y), (5000000, 5900000))
        self.assertEqual((current.pads[2].x, current.pads[2].y), (0, 0))

    def test_native_write_changes_only_the_pad_stream(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.PcbDoc"
            output = Path(directory) / "output.PcbDoc"
            authored = altium.AltiumPcbDoc()
            authored.add_pad(
                designator="1",
                position_mils=(10, 20),
                width_mils=12,
                height_mils=8,
            )
            authored.save(source)
            parsed = altium.AltiumPcbDoc.from_file(source, verbose=False)
            parsed.pads[0].x += 50000

            report = write_pad_stream_only(source, parsed, output)

            before = native_stream_inventory(source)
            after = native_stream_inventory(output)
            self.assertEqual(set(before), set(after))
            self.assertEqual(report["changed_native_streams"], ["Pads6/Data"])
            self.assertTrue(
                all(before[name] == after[name] for name in before if name != "Pads6/Data")
            )
            reloaded = altium.AltiumPcbDoc.from_file(output, verbose=False)
            self.assertEqual(reloaded.pads[0].x, parsed.pads[0].x)
            self.assertEqual(model_table_counts(output), model_table_counts(source))

    def test_native_write_rejects_model_header_data_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.PcbDoc"
            corrupt = Path(directory) / "corrupt.PcbDoc"
            output = Path(directory) / "output.PcbDoc"
            authored = altium.AltiumPcbDoc()
            authored.add_pad(
                designator="1",
                position_mils=(10, 20),
                width_mils=12,
                height_mils=8,
            )
            authored.save(source)
            with altium.AltiumOleFile(source) as ole:
                ole.modify_stream("Models/Header", (1).to_bytes(4, "little"))
                ole.write(corrupt)
            parsed = altium.AltiumPcbDoc.from_file(corrupt, verbose=False)

            with self.assertRaisesRegex(PadRepairError, "declares 1"):
                write_pad_stream_only(corrupt, parsed, output)

            self.assertFalse(output.exists())

    def test_native_write_rejects_pad_stream_size_change(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.PcbDoc"
            output = Path(directory) / "output.PcbDoc"
            authored = altium.AltiumPcbDoc()
            authored.add_pad(
                designator="1",
                position_mils=(10, 20),
                width_mils=12,
                height_mils=8,
            )
            authored.save(source)
            parsed = altium.AltiumPcbDoc.from_file(source, verbose=False)
            parsed.pads.append(parsed.pads[0])

            with self.assertRaisesRegex(PadRepairError, "size changed"):
                write_pad_stream_only(source, parsed, output)

            self.assertFalse(output.exists())

    def test_rejects_any_offset_outside_the_proven_pattern(self) -> None:
        reference = SimpleNamespace(
            components=[component("U1", "uid-1", x=0, y=0)],
            pads=[pad("1", 0, x=10, y=20)],
        )
        current = SimpleNamespace(
            components=[component("U1", "uid-1", x=0, y=0)],
            pads=[pad("1", 0, x=17, y=20)],
        )

        with self.assertRaisesRegex(PadRepairError, "outside the allowed pattern"):
            plan_shifted_pad_repair(
                current, reference, shift_x_mils=205, shift_y_mils=0
            )

    def test_bottom_side_transform_matches_mirror_then_rotation(self) -> None:
        reference = SimpleNamespace(
            components=[component("U1", "uid-1", x=0, y=0)],
            pads=[pad("1", 0, x=10, y=20)],
        )
        current = SimpleNamespace(
            components=[
                component(
                    "U1", "uid-1", x=100, y=200, rotation=90, layer="BOTTOM"
                )
            ],
            pads=[pad("1", 0, x=120, y=210)],
        )

        corrections, report = plan_shifted_pad_repair(
            current, reference, shift_x_mils=205, shift_y_mils=0
        )

        self.assertEqual(corrections, [])
        self.assertEqual(report["unchanged_count"], 1)


if __name__ == "__main__":
    unittest.main()
