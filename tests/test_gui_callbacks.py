"""Callback-only tests: no Tk root, windows, clicks or desktop automation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lcsc_altium_loader.gui import PartsBridgeApp


class GuiCallbackTests(unittest.TestCase):
    def app(self, *, output: str = "G:/dontdel/AD/_Lib", with_3d: bool = True, refresh_ad: bool = True) -> SimpleNamespace:
        return SimpleNamespace(
            master=object(), queue_items={"C2": {}},
            output_var=SimpleNamespace(get=lambda: output),
            with_3d_var=SimpleNamespace(get=lambda: with_3d),
            auto_refresh_var=SimpleNamespace(get=lambda: refresh_ad),
            client_factory=Mock(return_value=object()),
            cancel_event=SimpleNamespace(is_set=lambda: False),
            _progress_event=Mock(), _start_job=Mock(), _log=Mock(), _report_ad_refresh=Mock(),
        )

    def manifest(self) -> dict:
        return {
            "published": True, "added_count": 1, "skipped_count": 1, "total_components": 42,
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
             patch("lcsc_altium_loader.gui.refresh_after_publish", return_value={"status": "not_running", "message": "AD 未运行"}), \
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

    def test_auto_refresh_runs_in_worker_after_prepare_returns(self) -> None:
        app = self.app()
        PartsBridgeApp.generate(app)
        worker, callback = app._start_job.call_args.args[1:]
        order = []
        manifest = self.manifest()
        refresh_result = {"status": "refreshed", "verified": True, "message": "AD 已回执"}

        def prepare(*_args, **_kwargs):
            order.append("prepare_returned")
            return manifest, 0

        def refresh(*_args, **_kwargs):
            order.append("refresh")
            return refresh_result

        with patch("lcsc_altium_loader.gui.remember_output_dir"), \
             patch("lcsc_altium_loader.gui.prepare_libraries", side_effect=prepare), \
             patch("lcsc_altium_loader.gui.refresh_after_publish", side_effect=refresh) as bridge, \
             patch("lcsc_altium_loader.gui.messagebox.showinfo") as info:
            value = worker()
            self.assertEqual(order, ["prepare_returned", "refresh"])
            self.assertTrue(bridge.call_args.kwargs["enabled"])
            self.assertEqual(value[0]["ad_refresh"], refresh_result)
            callback(value)
            self.assertEqual(bridge.call_count, 1)  # No AD operation in the Tk callback.
        info.assert_called_once()
        self.assertIn("AD 已回执", info.call_args.args[1])

    def test_checkbox_can_disable_auto_refresh(self) -> None:
        app = self.app(refresh_ad=False)
        PartsBridgeApp.generate(app)
        worker = app._start_job.call_args.args[1]
        with patch("lcsc_altium_loader.gui.remember_output_dir"), \
             patch("lcsc_altium_loader.gui.prepare_libraries", return_value=(self.manifest(), 0)), \
             patch("lcsc_altium_loader.gui.refresh_after_publish") as bridge:
            worker()
        self.assertFalse(bridge.call_args.kwargs["enabled"])

    def test_refresh_warning_does_not_misreport_a_successful_append(self) -> None:
        app = self.app()
        PartsBridgeApp.generate(app)
        callback = app._start_job.call_args.args[2]
        manifest = self.manifest()
        manifest["ad_refresh"] = {"status": "permission_required", "verified": False, "message": "需要相同权限"}
        with patch("lcsc_altium_loader.gui.messagebox.showwarning") as warning, \
             patch("lcsc_altium_loader.gui.messagebox.showinfo") as info:
            callback((manifest, 0))
        info.assert_not_called()
        warning.assert_called_once()
        self.assertIn("总库已追加", warning.call_args.args[0])
        self.assertNotIn("追加有失败", warning.call_args.args[0])
        self.assertIn("需要相同权限", warning.call_args.args[1])
        self.assertTrue(manifest["published"])

    def test_manual_refresh_does_not_download_generate_or_change_preferences(self) -> None:
        app = self.app()
        PartsBridgeApp.refresh_ad(app)
        worker, callback = app._start_job.call_args.args[1:]
        expected = {"status": "refreshed", "verified": True, "message": "AD 已回执"}
        with patch("lcsc_altium_loader.gui.refresh_ad_libraries", return_value=expected) as refresh, \
             patch("lcsc_altium_loader.gui.prepare_libraries") as prepare, \
             patch("lcsc_altium_loader.gui.remember_output_dir") as remember:
            callback(worker())
        refresh.assert_called_once()
        prepare.assert_not_called()
        remember.assert_not_called()
        app.client_factory.assert_not_called()
        app._report_ad_refresh.assert_called_once_with(expected)

    def test_manual_refresh_rejects_blank_directory(self) -> None:
        app = self.app(output="  ")
        with patch("lcsc_altium_loader.gui.messagebox.showwarning") as warning:
            PartsBridgeApp.refresh_ad(app)
        warning.assert_called_once()
        app._start_job.assert_not_called()

    def test_manual_refresh_error_is_shown_as_refresh_warning(self) -> None:
        app = self.app()
        with patch("lcsc_altium_loader.gui.messagebox.showwarning") as warning:
            PartsBridgeApp._report_ad_refresh(app, {"status": "timeout", "verified": False, "message": "未收到回执"})
        self.assertEqual(warning.call_args.args[0], "AD 库刷新警告")
        self.assertIn("未收到回执", warning.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
