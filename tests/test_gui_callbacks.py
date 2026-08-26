"""Callback-only tests: no Tk root, windows, clicks or desktop automation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lcsc_altium_loader.gui import PartsBridgeApp


class GuiCallbackTests(unittest.TestCase):
    def app(self, *, output: str = "G:/dontdel/AD/_Lib", with_3d: bool = True) -> SimpleNamespace:
        return SimpleNamespace(
            master=object(), queue_items={"C2": {}},
            output_var=SimpleNamespace(get=lambda: output),
            with_3d_var=SimpleNamespace(get=lambda: with_3d),
            client_factory=lambda: object(),
            cancel_event=SimpleNamespace(is_set=lambda: False),
            _progress_event=Mock(), _start_job=Mock(), _log=Mock(),
        )

    def manifest(self) -> dict:
        return {
            "added_count": 1, "skipped_count": 1, "total_components": 42,
            "failures": [], "status": "complete", "state_directory": "C:/state", "backup_directory": "C:/backup",
            "added_codes": ["C2"], "components": [{"code": "C2", "3d": {"status": "missing"}}],
        }

    def test_blank_output_is_rejected_before_starting_any_job(self) -> None:
        app = self.app(output="   ")
        with patch("lcsc_altium_loader.gui.messagebox.showwarning") as warning:
            PartsBridgeApp.generate(app)
        warning.assert_called_once()
        app._start_job.assert_not_called()

    def test_success_displays_added_skipped_total_and_missing_3d(self) -> None:
        app = self.app()
        PartsBridgeApp.generate(app)
        worker, callback = app._start_job.call_args.args[1:]
        with patch("lcsc_altium_loader.gui.remember_output_dir") as remember, \
             patch("lcsc_altium_loader.gui.prepare_libraries", return_value=(self.manifest(), 0)) as prepare, \
             patch("lcsc_altium_loader.gui.messagebox.showinfo") as info:
            callback(worker())
        remember.assert_called_once()
        self.assertTrue(prepare.call_args.kwargs["with_3d"])
        message = info.call_args.args[1]
        for expected in ("新增 1", "跳过 1", "总量 42", "1 个型号未嵌入 3D", "C:/state"):
            self.assertIn(expected, message)

    def test_disabled_3d_is_not_reported_as_requested(self) -> None:
        app = self.app(with_3d=False)
        PartsBridgeApp.generate(app)
        callback = app._start_job.call_args.args[2]
        with patch("lcsc_altium_loader.gui.messagebox.showinfo") as info:
            callback((self.manifest(), 0))
        self.assertIn("未请求嵌入 3D", info.call_args.args[1])

    def test_partial_failure_uses_warning_and_includes_the_failed_code(self) -> None:
        app = self.app()
        PartsBridgeApp.generate(app)
        callback = app._start_job.call_args.args[2]
        manifest = self.manifest()
        manifest["failures"] = [{"code": "C3", "error": "name conflict"}]
        manifest["status"] = "partial"
        with patch("lcsc_altium_loader.gui.messagebox.showwarning") as warning, \
             patch("lcsc_altium_loader.gui.messagebox.showinfo") as info:
            callback((manifest, 2))
        info.assert_not_called()
        self.assertIn("C3: name conflict", warning.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
