from __future__ import annotations

import tempfile
import io
import runpy
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from lcsc_altium_loader.cli import build_parser, main
from lcsc_altium_loader.library_store import default_output_dir, remember_output_dir, state_directory
from lcsc_altium_loader.workflow import read_queries


class CliTests(unittest.TestCase):
    def test_read_queries_accepts_bom_header_and_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bom.csv"
            path.write_text(
                "\ufeffmanufacturer_part_number,quantity\nSTM32F103C8T6,2\n10k 0603,50\n",
                encoding="utf-8",
            )

            self.assertEqual(read_queries(path), ["STM32F103C8T6", "10k 0603"])

    def test_prepare_parser_accepts_component_csv(self) -> None:
        args = build_parser().parse_args(
            ["prepare", "--input-csv", "components.csv", "--output", "out"]
        )

        self.assertEqual(args.input_csv, "components.csv")
        self.assertEqual(args.codes, [])

    def test_doctor_parser_supports_online_json_report(self) -> None:
        args = build_parser().parse_args(["doctor", "--online", "--json"])

        self.assertTrue(args.online)
        self.assertTrue(args.json)

    def test_verify_parser_accepts_explicit_output(self) -> None:
        args = build_parser().parse_args(["verify", "--output", "out", "--json"])

        self.assertEqual(args.output, "out")
        self.assertTrue(args.json)

    def test_prepare_and_verify_can_use_default_master_library(self) -> None:
        args = build_parser().parse_args(["prepare", "C1"])
        self.assertIsNone(args.output)
        self.assertTrue(args.with_3d)
        self.assertIsNone(build_parser().parse_args(["verify"]).output)
        self.assertFalse(build_parser().parse_args(["prepare", "C1", "--no-with-3d"]).with_3d)

    def test_default_and_remembered_directory_live_outside_library(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
            self.assertEqual(default_output_dir(), Path("G:/dontdel/AD/_Lib"))
            chosen = Path(temporary) / "chosen-library"
            remember_output_dir(chosen)
            self.assertEqual(default_output_dir(), chosen.resolve())
            self.assertFalse(chosen.exists())
            self.assertFalse(state_directory(chosen).is_relative_to(chosen))

    def test_prepare_reports_store_error_without_a_traceback(self) -> None:
        stream = io.StringIO()
        with patch("lcsc_altium_loader.cli._client", return_value=object()), \
             patch("lcsc_altium_loader.cli.prepare_libraries", side_effect=RuntimeError("locked library")), \
             redirect_stderr(stream):
            status = main(["prepare", "C1", "--output", "out"])
        self.assertEqual(status, 1)
        self.assertIn("locked library", stream.getvalue())
        self.assertNotIn("Traceback", stream.getvalue())

    def test_packaged_launcher_dispatches_cli_without_starting_gui(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "start_gui.py"
        with patch.object(sys, "argv", [str(launcher), "verify", "--output", "out"]), \
             patch("lcsc_altium_loader.cli.main", return_value=0) as command, \
             patch("lcsc_altium_loader.gui.run") as gui:
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(launcher), run_name="__main__")
        self.assertEqual(result.exception.code, 0)
        command.assert_called_once_with()
        gui.assert_not_called()


if __name__ == "__main__":
    unittest.main()
