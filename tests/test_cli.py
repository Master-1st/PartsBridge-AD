from __future__ import annotations

import tempfile
import io
import json
import runpy
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
        self.assertTrue(args.refresh_ad)
        self.assertFalse(build_parser().parse_args(["prepare", "C1", "--no-refresh-ad"]).refresh_ad)

    def test_refresh_only_parser_accepts_remembered_or_explicit_directory(self) -> None:
        self.assertIsNone(build_parser().parse_args(["refresh-ad"]).output)
        args = build_parser().parse_args(["refresh-ad", "--output", "library", "--json"])
        self.assertEqual(args.output, "library")
        self.assertTrue(args.json)

    def test_refresh_only_never_downloads_or_generates(self) -> None:
        stream = io.StringIO()
        expected = {"status": "refreshed", "verified": True, "message": "AD 已回执"}
        with patch("lcsc_altium_loader.cli.refresh_ad_libraries", return_value=expected) as refresh, \
             patch("lcsc_altium_loader.cli._client") as client, \
             patch("lcsc_altium_loader.cli.prepare_libraries") as prepare, redirect_stdout(stream):
            status = main(["refresh-ad", "--output", "library", "--json"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stream.getvalue())["ad_refresh"], expected)
        refresh.assert_called_once_with(Path("library"))
        client.assert_not_called()
        prepare.assert_not_called()

    def test_refresh_only_without_verified_ack_returns_nonzero(self) -> None:
        stream = io.StringIO()
        with patch("lcsc_altium_loader.cli.refresh_ad_libraries", return_value={"status": "timeout", "verified": False, "message": "未收到回执"}), \
             patch("lcsc_altium_loader.cli.default_output_dir", return_value=Path("remembered")), redirect_stdout(stream):
            status = main(["refresh-ad"])
        self.assertEqual(status, 1)
        self.assertIn("未收到回执", stream.getvalue())

    def test_prepare_keeps_publication_exit_code_even_when_refresh_fails(self) -> None:
        for publication_status in (0, 2):
            manifest = {
                "components": [{"code": "C1"}], "published": True, "added_count": 1,
                "skipped_count": 0, "total_components": 1, "added_codes": ["C1"], "skipped": [],
                "failures": [] if publication_status == 0 else [{"code": "C2", "error": "not available"}],
                "status": "complete" if publication_status == 0 else "partial",
                "state_directory": "state", "backup_directory": None,
            }
            stream = io.StringIO()
            order = []

            def prepare(*_args, **_kwargs):
                order.append("prepare_returned")
                return manifest, publication_status

            def refresh(*_args, **_kwargs):
                order.append("refresh")
                return {"status": "permission_required", "verified": False, "message": "权限不一致"}

            with self.subTest(publication_status=publication_status), \
                 patch("lcsc_altium_loader.cli._client", return_value=object()), \
                 patch("lcsc_altium_loader.cli.prepare_libraries", side_effect=prepare), \
                 patch("lcsc_altium_loader.cli.refresh_after_publish", side_effect=refresh) as bridge, redirect_stdout(stream):
                status = main(["prepare", "C1", "--output", "library"])
            self.assertEqual(status, publication_status)
            self.assertEqual(order, ["prepare_returned", "refresh"])
            self.assertTrue(bridge.call_args.kwargs["enabled"])
            value = json.loads(stream.getvalue())
            self.assertEqual(value["added_count"], 1)
            self.assertEqual(value["ad_refresh"]["status"], "permission_required")
            self.assertEqual(value["status"], publication_status)

    def test_failed_prepare_never_reaches_refresh_hook(self) -> None:
        with patch("lcsc_altium_loader.cli._client", return_value=object()), \
             patch("lcsc_altium_loader.cli.prepare_libraries", side_effect=RuntimeError("not published")), \
             patch("lcsc_altium_loader.cli.refresh_after_publish") as refresh, redirect_stderr(io.StringIO()):
            status = main(["prepare", "C1", "--output", "library"])
        self.assertEqual(status, 1)
        refresh.assert_not_called()

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
