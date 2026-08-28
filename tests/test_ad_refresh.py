"""Refresh protocol tests; never launch AD, a Tk root, or desktop automation."""

from __future__ import annotations

import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lcsc_altium_loader import ad_refresh
from lcsc_altium_loader.library_store import state_directory


class RefreshGateTests(unittest.TestCase):
    def test_only_successfully_published_additions_trigger_refresh(self) -> None:
        cases = [
            ({"status": "complete", "published": True, "added_count": 1}, True),
            ({"status": "partial", "published": True, "added_count": 1}, True),
            ({"status": "unchanged", "published": True, "added_count": 0}, False),
            ({"status": "failed", "published": False, "added_count": 2}, False),
            ({"status": "cancelled", "published": False, "added_count": 0}, False),
            ({"published": True, "added_count": -1}, False),
            ({"added_count": 1}, False),
        ]
        for manifest, expected in cases:
            with self.subTest(manifest=manifest), patch.object(ad_refresh, "refresh_ad_libraries") as refresh:
                ad_refresh.refresh_after_publish(manifest, Path("library"))
                self.assertEqual(refresh.call_count, int(expected))

    def test_disabled_refresh_does_not_contact_ad(self) -> None:
        with patch.object(ad_refresh, "refresh_ad_libraries") as refresh:
            result = ad_refresh.refresh_after_publish(
                {"published": True, "added_count": 1}, Path("library"), enabled=False,
            )
        refresh.assert_not_called()
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["verified"])

    def test_bridge_exception_cannot_change_successful_publication(self) -> None:
        manifest = {"published": True, "added_count": 1, "status": "complete"}
        before = dict(manifest)
        with patch.object(ad_refresh, "refresh_ad_libraries", side_effect=RuntimeError("bridge failed")):
            result = ad_refresh.refresh_after_publish(manifest, Path("library"))
        self.assertEqual(manifest, before)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["verified"])
        self.assertIn("库已发布", result["message"])

    def test_non_windows_does_not_try_native_process_apis(self) -> None:
        with patch.object(ad_refresh.sys, "platform", "linux"), \
             patch.object(ad_refresh, "_running_altium") as running:
            result = ad_refresh.refresh_ad_libraries(Path("library"))
        self.assertEqual(result["status"], "unsupported")
        running.assert_not_called()

    def test_no_running_ad_does_not_start_a_new_session(self) -> None:
        with patch.object(ad_refresh.sys, "platform", "win32"), \
             patch.object(ad_refresh, "_running_altium", return_value=None), \
             patch.object(ad_refresh, "_launch_script") as launch:
            result = ad_refresh.refresh_ad_libraries(Path("library"))
        self.assertEqual(result["status"], "not_running")
        self.assertFalse(result["verified"])
        launch.assert_not_called()

    def test_permission_and_ambiguous_session_errors_never_launch(self) -> None:
        for status in ("permission_required", "ambiguous"):
            with self.subTest(status=status), \
                 patch.object(ad_refresh.sys, "platform", "win32"), \
                 patch.object(ad_refresh, "_running_altium", side_effect=ad_refresh.RefreshError(status, "blocked")), \
                 patch.object(ad_refresh, "_launch_script") as launch:
                result = ad_refresh.refresh_ad_libraries(Path("library"))
            self.assertEqual(result["status"], status)
            self.assertFalse(result["verified"])
            launch.assert_not_called()


class RefreshProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "library"
        self.directory.mkdir()
        self.before = {"LCSC.SchLib": b"symbol bytes", "LCSC.PcbLib": b"footprint bytes"}
        for name, data in self.before.items():
            (self.directory / name).write_bytes(data)
        self.instance = ad_refresh.AltiumInstance(123, self.root / "Altium with spaces" / "X2.exe")
        system = self.instance.executable.parent / "System"
        system.mkdir(parents=True)
        for name, command in (
            ("IntegratedLibrary.INS", "RefreshInstalledLibraries"),
            ("ScriptingSystem.ins", "RunScriptFile"),
            ("Altium.Edp.ComponentSearch.ins", "ClearCache"),
        ):
            (system / name).write_text(f"Command Name = '{command}' End", encoding="ascii")
        self.inventory = {"symbols": {"C1_A": {}, "C2_B": {}}, "footprints": {"C1_PKG": {}, "C2_PKG": {}}}
        self.enterContext(patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "state")}))
        self.enterContext(patch.object(ad_refresh.sys, "platform", "win32"))
        self.running = self.enterContext(patch.object(ad_refresh, "_running_altium", return_value=self.instance))
        self.native = self.enterContext(patch("lcsc_altium_loader.integrity.native_inventory", return_value=self.inventory))
        self.launch = self.enterContext(patch.object(ad_refresh, "_launch_script", side_effect=self.reply))
        self.receipt_path: Path | None = None

    def reply(self, _instance: ad_refresh.AltiumInstance, script: Path, *, status: str = "refreshed", **fields: str | None) -> Mock:
        self.receipt_path = script.parent / "receipt.txt"
        receipt = {
            "request_id": script.parent.name, "status": status,
            "symbols": "2", "footprints": "2", "symbols_match": "1", "footprints_match": "1",
            "complete": "1", **fields,
        }
        self.receipt_path.write_text(
            "\n".join(f"{key}={value}" for key, value in receipt.items() if value is not None), encoding="ascii",
        )
        return Mock(poll=Mock(return_value=0))

    def assert_libraries_unchanged(self) -> None:
        self.assertEqual({path.name for path in self.directory.iterdir()}, set(self.before))
        for name, data in self.before.items():
            self.assertEqual((self.directory / name).read_bytes(), data)

    def test_success_requires_a_matching_receipt_and_preserves_library_bytes(self) -> None:
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "refreshed")
        self.assertEqual(result["verification_scope"], "ad_library_readback")
        self.assertEqual((result["symbols"], result["footprints"]), (2, 2))
        self.assertEqual(self.running.call_count, 2)
        self.assert_libraries_unchanged()
        self.assertFalse(Path(result["receipt_path"]).parent.is_relative_to(self.directory))
        self.assertFalse((Path(result["receipt_path"]).parent / "active").exists())
        self.assertTrue((Path(result["receipt_path"]).parent / "Refresh.pas").is_file())

    def test_ad_failure_receipts_are_never_reported_as_verified(self) -> None:
        for status in ("dirty", "busy", "expired", "unsupported", "reload_failed", "unavailable", "script_error", "stale", "unknown"):
            with self.subTest(status=status):
                self.launch.side_effect = lambda instance, script: self.reply(instance, script, status=status)
                result = ad_refresh.refresh_ad_libraries(self.directory)
                self.assertFalse(result["verified"])
                self.assertNotEqual(result["status"], "refreshed")
                self.assert_libraries_unchanged()

    def test_wrong_count_in_success_receipt_is_rejected(self) -> None:
        self.launch.side_effect = lambda instance, script: self.reply(instance, script, symbols="1")
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["verified"])

    def test_stale_warning_reports_actual_counts_without_blame_or_redownload(self) -> None:
        self.inventory["symbols"] = {f"C{i}_SYM": {} for i in range(46)}
        self.inventory["footprints"] = {f"C{i}_PKG": {} for i in range(46)}
        self.launch.side_effect = lambda instance, script: self.reply(
            instance, script, status="stale", symbols="46", footprints="0", footprints_match="0",
        )
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["verified"])
        self.assertIn("符号 46/46", result["message"])
        self.assertIn("封装 0/46", result["message"])
        self.assertIn("无需重新下载", result["message"])
        self.assertNotIn("请确认这两份库已安装", result["message"])
        self.assertNotIn("没有未保存修改", result["message"])
        self.assertEqual(result["readback"]["footprints"], {"expected": 46, "actual": 0, "names_match": False})
        self.assert_libraries_unchanged()

    def test_matching_counts_require_explicit_name_match_for_both_libraries(self) -> None:
        for kind in ("symbols", "footprints"):
            for value in ("0", "", "unexpected", None):
                with self.subTest(kind=kind, value=value):
                    self.launch.side_effect = lambda instance, script: self.reply(
                        instance, script, **{f"{kind}_match": value},
                    )
                    result = ad_refresh.refresh_ad_libraries(self.directory)
                    self.assertEqual(result["status"], "stale")
                    self.assertFalse(result["verified"])
                    self.assertIn("数量或名称", result["message"])
        self.assert_libraries_unchanged()

    def test_unavailable_pcb_readback_is_not_reported_as_an_empty_library(self) -> None:
        self.launch.side_effect = lambda instance, script: self.reply(
            instance, script, status="pcb_unavailable", footprints=None, footprints_match=None,
        )
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "pcb_unavailable")
        self.assertFalse(result["verified"])
        self.assertIn("封装 未回读/2", result["message"])
        self.assertEqual(result["readback"]["footprints"], {"expected": 2, "actual": None, "names_match": None})
        self.assert_libraries_unchanged()

    def test_a_launcher_zero_exit_without_receipt_is_not_success(self) -> None:
        self.launch.side_effect = None
        self.launch.return_value = Mock(poll=Mock(return_value=0))
        with patch.object(ad_refresh.time, "sleep"):
            result = ad_refresh.refresh_ad_libraries(self.directory, timeout=0.002)
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["verified"])
        request = Path(result["receipt_path"]).parent
        self.assertFalse((request / "active").exists())
        self.assertTrue((request / "Refresh.pas").exists())
        self.assert_libraries_unchanged()

    def test_stale_request_id_is_not_a_success_ack(self) -> None:
        self.launch.side_effect = lambda instance, script: self.reply(instance, script, request_id="old-request")
        with patch.object(ad_refresh.time, "sleep"):
            result = ad_refresh.refresh_ad_libraries(self.directory, timeout=0.002)
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["verified"])

    def test_nonzero_launcher_exit_is_a_separate_refresh_failure(self) -> None:
        self.launch.side_effect = None
        self.launch.return_value = Mock(poll=Mock(return_value=5))
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "failed")
        self.assertIn("退出码 5", result["message"])
        self.assert_libraries_unchanged()

    def test_launch_elevation_error_has_actionable_message_and_no_retry(self) -> None:
        error = OSError("elevation required")
        error.winerror = 740
        self.launch.side_effect = error
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "permission_required")
        self.assertIn("相同权限", result["message"])
        self.launch.assert_called_once()
        self.assertFalse((Path(result["receipt_path"]).parent / "active").exists())
        self.assert_libraries_unchanged()

    def test_unfinished_publication_is_not_recovered_by_refresh(self) -> None:
        state = state_directory(self.directory)
        state.mkdir(parents=True)
        (state / "transaction.json").write_text("unfinished", encoding="ascii")
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "busy")
        self.launch.assert_not_called()
        self.native.assert_not_called()
        self.assertEqual((state / "transaction.json").read_text(), "unfinished")
        self.assert_libraries_unchanged()

    def test_missing_command_does_not_launch(self) -> None:
        (self.instance.executable.parent / "System" / "ScriptingSystem.ins").write_text("", encoding="ascii")
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "unsupported")
        self.launch.assert_not_called()

    def test_native_parse_failure_does_not_launch_or_modify(self) -> None:
        self.native.side_effect = ValueError("bad native pair")
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "failed")
        self.launch.assert_not_called()
        self.assert_libraries_unchanged()

    def test_concurrent_change_before_dispatch_prevents_refresh(self) -> None:
        def change(_directory: Path) -> dict:
            (self.directory / "LCSC.SchLib").write_bytes(b"external edit")
            return self.inventory

        self.native.side_effect = change
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "changed")
        self.launch.assert_not_called()
        self.assertEqual((self.directory / "LCSC.SchLib").read_bytes(), b"external edit")

    def test_concurrent_change_during_refresh_is_not_reported_as_verified(self) -> None:
        def change(instance: ad_refresh.AltiumInstance, script: Path) -> Mock:
            launcher = self.reply(instance, script)
            (self.directory / "LCSC.PcbLib").write_bytes(b"new external version")
            return launcher

        self.launch.side_effect = change
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "changed")
        self.assertFalse(result["verified"])
        self.assertEqual((self.directory / "LCSC.PcbLib").read_bytes(), b"new external version")

    def test_ad_session_change_is_not_reported_as_verified(self) -> None:
        self.running.side_effect = [self.instance, ad_refresh.AltiumInstance(456, self.instance.executable)]
        result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "changed")
        self.assertFalse(result["verified"])

    def test_request_files_cannot_be_written_inside_library_output(self) -> None:
        with patch.object(ad_refresh, "app_data_dir", return_value=self.directory):
            result = ad_refresh.refresh_ad_libraries(self.directory)
        self.assertEqual(result["status"], "failed")
        self.launch.assert_not_called()
        self.assert_libraries_unchanged()


class ScriptContractTests(unittest.TestCase):
    def test_pcb_names_are_enumerated_with_the_pcb_library_iterator(self) -> None:
        directory = Path("library").resolve()
        script = ad_refresh._refresh_script(
            directory, Path("request").resolve(), "abc123",
            ["C1_SYM"], ["C1_PKG"], datetime(2026, 8, 26, 12, 34, 56),
        )
        sch_literal = ad_refresh._pascal_string(str(directory / "LCSC.SchLib"))
        pcb_literal = ad_refresh._pascal_string(str(directory / "LCSC.PcbLib"))
        self.assertNotIn(f"Manager.GetComponentCount({pcb_literal})", script)
        self.assertNotIn(f"Manager.GetComponentName({pcb_literal},", script)
        self.assertIn(f"Manager.GetComponentCount({sch_literal})", script)
        self.assertIn(f"PartsBridgeReadFootprints({pcb_literal}, Actual)", script)
        for required in (
            "PCBServer.GetPCBLibraryByPath(APath)", "ReadLibrary.LibraryIterator_Create",
            "Iterator.SetState_FilterAll", "Iterator.FirstPCBObject", "Iterator.NextPCBObject",
            "Actual.Add(Footprint.Name)", "ReadLibrary.LibraryIterator_Destroy(Iterator)",
        ):
            self.assertIn(required, script)
        self.assertLess(script.index("ReadLibrary.LibraryIterator_Destroy(Iterator)"), script.index("Client.CloseDocument(ReadDocument)"))

    def test_only_a_clean_hidden_document_owned_by_the_reader_may_be_closed(self) -> None:
        script = ad_refresh._refresh_script(
            Path("library").resolve(), Path("request").resolve(), "abc123",
            ["C1_SYM"], ["C1_PKG"], datetime(2026, 8, 26, 12, 34, 56),
        )
        self.assertIn("ReadDocument := Client.GetDocumentByPath(APath);", script)
        self.assertIn("WasOpen := Client.IsDocumentOpen(APath);", script)
        self.assertIn("OpenedForRead := False;", script)
        self.assertRegex(script, r"If ReadDocument = Nil Then\s+Begin\s+If WasOpen Then Exit;\s+ReadDocument := Client.OpenDocumentShowOrHide\('PCBLIB', APath, False\);")
        self.assertIn("OpenedForRead := ReadDocument <> Nil;", script)
        self.assertRegex(script, r"If ReadDocument.Modified Then\s+Result := 'dirty'\s+Else\s+Result := 'ok';")
        self.assertRegex(script, r"Finally\s+If OpenedForRead Then\s+If ReadDocument <> Nil Then\s+If ReadDocument.Modified Then\s+Result := 'dirty'\s+Else\s+Client.CloseDocument\(ReadDocument\);")
        self.assertEqual(script.count("Client.CloseDocument("), 1)
        for forbidden in ("ShowDocument(", "ShowDocumentDontFocus(", "DoFileSave", "SetState_Modified", "CloseAllDocuments", "CloseProject"):
            self.assertNotIn(forbidden, script)

    def test_script_reports_separate_complete_name_matches(self) -> None:
        script = ad_refresh._refresh_script(
            Path("library").resolve(), Path("request").resolve(), "abc123",
            ["C1_SYM"], ["C1_PKG"], datetime(2026, 8, 26, 12, 34, 56),
        )
        for kind in ("symbols", "footprints"):
            self.assertIn(f"Report.Add('{kind}_match=1')", script)
            self.assertIn(f"Report.Add('{kind}_match=0')", script)
        self.assertIn("SymbolsMatch := Actual.Count = Expected.Count;", script)
        self.assertIn("FootprintsMatch := Actual.Count = Expected.Count;", script)
        self.assertIn("If Actual.IndexOf(Expected[I]) < 0 Then SymbolsMatch := False;", script)
        self.assertIn("If Actual.IndexOf(Expected[I]) < 0 Then FootprintsMatch := False;", script)
        self.assertIn("If SymbolsMatch And FootprintsMatch Then", script)

    def test_helpers_are_standalone_with_explicit_receipt_state(self) -> None:
        script = ad_refresh._refresh_script(
            Path("library").resolve(), Path("request").resolve(), "abc123",
            ["C1_SYM"], ["C1_PKG"], datetime(2026, 8, 26, 12, 34, 56),
        )
        self.assertFalse(
            re.search(r"(?mi)^[ \t]+(?:procedure|function)\b", script),
            "AD DelphiScript helpers must not be nested or capture outer local variables",
        )
        self.assertEqual(
            re.findall(r"(?mi)^(?:procedure|function)\s+(\w+)", script),
            ["PartsBridgeFinish", "PartsBridgeCanContinue", "PartsBridgeReadFootprints", "PartsBridgeRefresh"],
        )
        helpers = script.split("Procedure PartsBridgeRefresh;", 1)[0]
        self.assertIn("Procedure PartsBridgeFinish(Report : TStringList; State : String);", helpers)
        self.assertIn("Function PartsBridgeCanContinue(Report : TStringList) : Boolean;", helpers)
        self.assertEqual(len(re.findall(r"(?mi)^var\b", script)), 2)
        self.assertIn("Function PartsBridgeReadFootprints(APath : String; Actual : TStringList) : String;\nVar", helpers)
        self.assertIn("Procedure PartsBridgeRefresh;\nVar", script)
        self.assertEqual(re.findall(r"(?mi)^procedure\s+(\w+);$", script), ["PartsBridgeRefresh"])

    def test_every_helper_call_receives_the_current_report(self) -> None:
        script = ad_refresh._refresh_script(
            Path("library").resolve(), Path("request").resolve(), "abc123",
            ["C1_SYM"], ["C1_PKG"], datetime(2026, 8, 26, 12, 34, 56),
        )
        states = re.findall(r"PartsBridgeFinish\(Report, '([a-z_]+)'\)", script)
        self.assertCountEqual(states, [
            "expired", "busy", "unavailable", "dirty", "dirty",
            "unsupported", "unsupported", "reload_failed", "reload_failed",
            "refreshed", "stale", "script_error",
        ])
        self.assertEqual(script.count("If Not PartsBridgeCanContinue(Report) Then Exit;"), 7)
        self.assertNotRegex(script, r"\b(?:Finish|CanContinue)\b")

    def test_path_and_name_literals_are_ascii_and_cannot_inject_script(self) -> None:
        value = "O'Brien\\零件'; RunProcess('Bad'); // 😀"
        literal = ad_refresh._pascal_string(value)
        self.assertTrue(literal.isascii())
        self.assertNotIn("RunProcess", literal)
        self.assertTrue(literal.startswith("''#"))
        recovered = b"".join(int(item).to_bytes(2, "little") for item in literal[2:].split("#")[1:])
        self.assertEqual(recovered.decode("utf-16-le"), value)

    def test_script_checks_both_dirty_libraries_before_reloading(self) -> None:
        script = ad_refresh._refresh_script(
            Path("library").resolve(), Path("request").resolve(), "abc123",
            ["C1_SYM"], ["C1_PKG"], datetime(2026, 8, 26, 12, 34, 56),
        )
        self.assertLess(script.index("SchDocument.Modified"), script.index("SchDocument.DoFileLoad"))
        self.assertLess(script.index("PcbDocument.Modified"), script.index("SchDocument.DoFileLoad"))
        self.assertLess(script.index("PcbDocument.SupportsReload"), script.index("SchDocument.DoFileLoad"))
        self.assertIn("IntegratedLibrary:RefreshInstalledLibraries", script)
        self.assertIn("Altium.Edp.ComponentSearch.Plugin:ClearCache", script)
        self.assertIn("AddStringParameter('AllLibraries', 'True')", script)
        for forbidden in ("DoFileSave", "CloseDocument(SchDocument)", "CloseDocument(PcbDocument)", "InstallLibrary", "UninstallLibrary", "TerminateWithExitCode"):
            self.assertNotIn(forbidden, script)
        self.assertTrue(script.isascii())
        self.assertIn("EncodeDate(2026, 8, 26)", script)
        self.assertIn("EncodeTime(12, 34, 56, 0)", script)
        self.assertIn("RenameFile(", script)
        self.assertGreaterEqual(script.count("If Not PartsBridgeCanContinue(Report) Then Exit;"), 5)

    def test_receipt_supports_ad_utf16_and_rejects_incomplete_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.txt"
            path.write_text("request_id=abc\ncomplete=1\nstatus=dirty\n", encoding="utf-16")
            self.assertEqual(ad_refresh._read_receipt(path, "abc")["status"], "dirty")
            self.assertIsNone(ad_refresh._read_receipt(path, "different"))
            path.write_text("request_id=abc\nstatus=refreshed\n", encoding="utf-8")
            self.assertIsNone(ad_refresh._read_receipt(path, "abc"))

    def test_launcher_uses_hidden_no_shell_command_with_native_quotes(self) -> None:
        startup = SimpleNamespace(dwFlags=0, wShowWindow=None)
        instance = ad_refresh.AltiumInstance(123, Path("AD with spaces") / "X2.exe")
        script = Path("request with spaces") / "Refresh.pas"
        with patch.object(ad_refresh.subprocess, "STARTUPINFO", return_value=startup, create=True), \
             patch.object(ad_refresh.subprocess, "STARTF_USESHOWWINDOW", 1, create=True), \
             patch.object(ad_refresh.subprocess, "Popen") as launch:
            ad_refresh._launch_script(instance, script)
        command = launch.call_args.args[0]
        self.assertIn(f'FileName="{script}"', command)
        self.assertNotIn('\\"', command)
        self.assertFalse(launch.call_args.kwargs["shell"])
        self.assertEqual(startup.wShowWindow, 0)


if __name__ == "__main__":
    unittest.main()
