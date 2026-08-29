from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import altium_monkey as altium

from lcsc_altium_loader.convert import _add_footprint


def footprint_source() -> SimpleNamespace:
    return SimpleNamespace(
        bbox=SimpleNamespace(x=2.54, y=5.08, x_px=10, y_px=20),
        info=SimpleNamespace(name="ASYMMETRIC"),
        pads=[SimpleNamespace(
            center_x=3.048, center_y=5.842, width=1, height=0.5,
            shape="RECT", layer_id=1, number="1", rotation=30,
            hole_radius=0, hole_length=0, slot_outline="", is_plated=False,
        )],
        tracks=[], arcs=[], circles=[], solid_regions=[], holes=[],
        vias=[], rectangles=[], texts=[], model_3d=None,
    )


COMPONENT = {"code": "C1", "mpn": "TEST", "package": "TEST", "source_urls": {}}


class FootprintCoordinateTests(unittest.TestCase):
    def test_pad_location_and_angle_convert_svg_down_to_altium_up(self) -> None:
        library = altium.AltiumPcbLib()
        _add_footprint(library, footprint_source(), "ASYMMETRIC", COMPONENT, [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.PcbLib"
            library.save(path)
            parsed = altium.AltiumPcbLib.from_file(path).footprints[0].pads[0]
        self.assertAlmostEqual(parsed.x_mils, 20, places=4)
        self.assertAlmostEqual(parsed.y_mils, -30, places=4)
        self.assertAlmostEqual(parsed.rotation % 360, 330, places=5)

    def test_track_circle_hole_via_rectangle_and_text_share_the_same_handedness(self) -> None:
        source = footprint_source()
        source.tracks = [SimpleNamespace(points="10 21 12 23", stroke_width=.1, layer_id=3)]
        source.circles = [SimpleNamespace(cx=3.048, cy=5.842, radius=.2, stroke_width=.1, layer_id=3)]
        source.holes = [SimpleNamespace(center_x=3.048, center_y=5.842, radius=.2)]
        source.vias = [SimpleNamespace(center_x=3.048, center_y=5.842, diameter=.8, radius=.2)]
        source.rectangles = [SimpleNamespace(x=3.048, y=5.842, width=.254, height=.508, stroke_width=.1, layer_id=3)]
        source.texts = [SimpleNamespace(type="P", center_x=3.048, center_y=5.842, font_size=1, stroke_width=.1, rotation=30, text="REF", is_displayed=True, layer_id=3)]
        library = altium.AltiumPcbLib()
        footprint = library.add_footprint("ASYMMETRIC")
        with patch.object(library, "add_footprint", return_value=footprint), \
             patch.object(footprint, "add_track", wraps=footprint.add_track) as track, \
             patch.object(footprint, "add_arc", wraps=footprint.add_arc) as circle, \
             patch.object(footprint, "add_text", wraps=footprint.add_text) as label:
            _add_footprint(library, source, "ASYMMETRIC", COMPONENT, [])
        self.assertEqual(track.call_args_list[0].args, ((0.0, -10.0), (20.0, -30.0)))
        self.assertAlmostEqual(circle.call_args.kwargs["center_mils"][1], -30)
        self.assertAlmostEqual(footprint.pads[-1].y_mils, -30, places=4)
        self.assertAlmostEqual(footprint.vias[0].y_mils, -30, places=4)
        rectangle_points = [point for call in track.call_args_list[1:] for point in call.args]
        self.assertAlmostEqual(min(point[1] for point in rectangle_points), -50)
        self.assertAlmostEqual(max(point[1] for point in rectangle_points), -30)
        self.assertAlmostEqual(label.call_args.kwargs["position_mils"][1], -30)
        self.assertAlmostEqual(label.call_args.kwargs["rotation_degrees"] % 360, 330)

    def test_arc_reflection_preserves_quadrant_large_arc_and_zero_crossing(self) -> None:
        cases = [
            ("M 1 0 A 1 1 0 0 1 0 1", 90, -45),
            ("M 1 0 A 1 1 0 0 0 0 -1", 90, 45),
            ("M 1 0 A 1 1 0 1 1 0 -1", 270, 225),
            ("M 0.866025403784 -0.5 A 1 1 0 0 1 0.866025403784 0.5", 60, 0),
        ]
        for path, span, midpoint in cases:
            with self.subTest(path=path):
                source = footprint_source()
                source.bbox = SimpleNamespace(x=0, y=0, x_px=0, y_px=0)
                source.arcs = [SimpleNamespace(path=path, stroke_width=.1, layer_id=3)]
                library = altium.AltiumPcbLib()
                footprint = library.add_footprint("ARC")
                with patch.object(library, "add_footprint", return_value=footprint), \
                     patch.object(footprint, "add_arc", wraps=footprint.add_arc) as arc:
                    _add_footprint(library, source, "ARC", COMPONENT, [])
                args = arc.call_args.kwargs
                actual_span = (args["end_angle_degrees"] - args["start_angle_degrees"]) % 360
                self.assertAlmostEqual(actual_span, span, places=6)
                angle = math.radians(args["start_angle_degrees"] + actual_span / 2)
                expected = math.radians(midpoint)
                self.assertAlmostEqual(math.cos(angle), math.cos(expected), places=6)
                self.assertAlmostEqual(math.sin(angle), math.sin(expected), places=6)

    def test_custom_pad_and_region_reflect_absolute_points_once(self) -> None:
        source = footprint_source()
        source.pads[0].shape = "POLYGON"
        source.pads[0].points = "11 22 13 22 12 25"
        source.solid_regions = [SimpleNamespace(layer_id=99, region_type="solid", path="M 11 22 L 13 22 L 12 25 Z")]
        library = altium.AltiumPcbLib()
        footprint = library.add_footprint("CUSTOM")
        with patch.object(library, "add_footprint", return_value=footprint), \
             patch.object(footprint, "add_custom_pad", wraps=footprint.add_custom_pad) as custom, \
             patch.object(footprint, "add_region", wraps=footprint.add_region) as region:
            _add_footprint(library, source, "CUSTOM", COMPONENT, [])
        relative = custom.call_args.kwargs["outline_points_mils"]
        for actual, expected in zip(relative, [(-10, 10), (10, 10), (0, -20)]):
            self.assertAlmostEqual(actual[0], expected[0])
            self.assertAlmostEqual(actual[1], expected[1])
        self.assertEqual(custom.call_args.kwargs["anchor_rotation_degrees"], 0)
        self.assertEqual(region.call_args.kwargs["outline_points_mils"][:3], [(10, -20), (30, -20), (20, -50)])

    def test_slot_angle_is_relative_to_pad_and_uses_explicit_hole_endpoints(self) -> None:
        source = footprint_source()
        source.pads[0].hole_radius = .2
        source.pads[0].hole_length = 1
        source.pads[0].slot_outline = "10 20 12 22"
        library = altium.AltiumPcbLib()
        footprint, _, _ = _add_footprint(library, source, "SLOT", COMPONENT, [])
        pad = footprint.pads[0]
        self.assertAlmostEqual((pad.rotation + pad.slot_rotation) % 180, 135)

    def test_3d_normalizes_step_bottom_and_does_not_double_flip_y(self) -> None:
        source = footprint_source()
        source.model_3d = SimpleNamespace(
            uuid="12345678", rotation=SimpleNamespace(x=0, y=0, z=0),
            translation=SimpleNamespace(x=1.27, y=-2.54, z=0),
        )
        library = altium.AltiumPcbLib()
        footprint = library.add_footprint("MODEL")
        with patch.object(library, "add_footprint", return_value=footprint), \
             patch("lcsc_altium_loader.convert.altium.compute_step_model_bounds_mils", return_value=SimpleNamespace(min_z_mils=-25, max_z_mils=25)), \
             patch.object(library, "add_embedded_model", return_value=object()), \
             patch.object(footprint, "add_embedded_3d_model") as body:
            _, _, three_d = _add_footprint(library, source, "MODEL", COMPONENT, [], step_data=b"test", three_d_requested=True)
        args = body.call_args.kwargs
        self.assertAlmostEqual(args["location_mils"][0], 50)
        self.assertAlmostEqual(args["location_mils"][1], -100)
        self.assertAlmostEqual(args["standoff_height_mils"], 25)
        self.assertEqual(three_d["placement"]["normalization"], "step_bottom_to_source_min_z")
        self.assertAlmostEqual(three_d["placement"]["final_min_z_mils"], 0)

    def test_3d_normalization_preserves_through_hole_pin_depth(self) -> None:
        source = footprint_source()
        source.model_3d = SimpleNamespace(
            uuid="12345678", rotation=SimpleNamespace(x=0, y=0, z=0),
            translation=SimpleNamespace(x=0, y=0, z=-3.5),
        )
        library = altium.AltiumPcbLib()
        footprint = library.add_footprint("MODEL")
        source_min_z_mils = -3.5 * 1000 / 25.4
        with patch.object(library, "add_footprint", return_value=footprint), \
             patch("lcsc_altium_loader.convert.altium.compute_step_model_bounds_mils", return_value=SimpleNamespace(min_z_mils=source_min_z_mils, max_z_mils=8.5 * 1000 / 25.4)), \
             patch.object(library, "add_embedded_model", return_value=object()) as embedded, \
             patch.object(footprint, "add_embedded_3d_model") as body:
            _, _, three_d = _add_footprint(library, source, "MODEL", COMPONENT, [], step_data=b"test", three_d_requested=True)
        self.assertAlmostEqual(embedded.call_args.kwargs["z_offset_mils"], 0)
        self.assertAlmostEqual(body.call_args.kwargs["standoff_height_mils"], 0)
        self.assertAlmostEqual(
            three_d["placement"]["final_min_z_mils"], source_min_z_mils, places=5
        )

    def test_3d_normalization_falls_back_without_dropping_model(self) -> None:
        source = footprint_source()
        source.model_3d = SimpleNamespace(
            uuid="12345678", rotation=SimpleNamespace(x=0, y=0, z=0),
            translation=SimpleNamespace(x=0, y=0, z=.508),
        )
        library = altium.AltiumPcbLib()
        footprint = library.add_footprint("MODEL")
        warnings: list[str] = []
        with patch.object(library, "add_footprint", return_value=footprint), \
             patch("lcsc_altium_loader.convert.altium.compute_step_model_bounds_mils", side_effect=ValueError("bad STEP")), \
             patch.object(library, "add_embedded_model", return_value=object()), \
             patch.object(footprint, "add_embedded_3d_model") as body:
            _, _, three_d = _add_footprint(library, source, "MODEL", COMPONENT, warnings, step_data=b"test", three_d_requested=True)
        self.assertAlmostEqual(body.call_args.kwargs["standoff_height_mils"], 20)
        self.assertEqual(three_d["status"], "embedded")
        self.assertEqual(three_d["placement"]["normalization"], "source_offset_fallback")
        self.assertTrue(any("normalization unavailable" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
