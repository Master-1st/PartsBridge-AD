"""Ask a running Altium session to reload libraries, without desktop automation.

Refresh is deliberately separate from library publication. An AD error must
never turn a committed library pair into an apparent failed conversion.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .library_store import LIBRARY_NAMES, app_data_dir, sha256_file, state_directory


@dataclass(frozen=True)
class AltiumInstance:
    pid: int
    executable: Path


class RefreshError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _result(status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"status": status, "verified": status == "refreshed", "message": message, **details}


_PERMISSION_MESSAGE = (
    "AD 与元件库桥的运行权限不一致，Windows 阻止了刷新。"
    "请用相同权限运行两者，再点击“刷新 AD 库”；无需重新下载元件。"
)
_PREFLIGHT_STOP_MESSAGE = (
    "已在下载和写库前停止，不会自动提权。"
    "请让 AD 与元件库桥以相同权限运行后再试。"
)


def _running_altium() -> AltiumInstance | None:
    """Find exactly one X2 in this Windows session; never guess an installation."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260),
        ]

    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        method = getattr(kernel, name)
        method.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        method.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    security.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetTokenInformation.restype = wintypes.BOOL

    def elevated(handle: Any) -> bool:
        token = wintypes.HANDLE()
        if not security.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
            raise RefreshError("permission_required", _PERMISSION_MESSAGE)
        try:
            flag, length = wintypes.DWORD(), wintypes.DWORD()
            if not security.GetTokenInformation(
                token, 20, ctypes.byref(flag), ctypes.sizeof(flag), ctypes.byref(length),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return bool(flag.value)
        finally:
            kernel.CloseHandle(token)

    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    pids: list[int] = []
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.name.casefold() == "x2.exe":
                other_session = wintypes.DWORD()
                if kernel.ProcessIdToSessionId(entry.pid, ctypes.byref(other_session)):
                    if other_session.value == session.value:
                        pids.append(entry.pid)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(snapshot)
    if not pids:
        return None
    if len(pids) != 1:
        raise RefreshError("ambiguous", "检测到多个 AD 会话，未选择或刷新任何一个。请只保留要刷新的 AD 会话后重试。")
    handle = kernel.OpenProcess(0x1000, False, pids[0])  # Query only, never terminate/inject.
    if not handle:
        raise RefreshError("permission_required", _PERMISSION_MESSAGE)
    try:
        if elevated(handle) != elevated(kernel.GetCurrentProcess()):
            raise RefreshError("permission_required", _PERMISSION_MESSAGE)
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return AltiumInstance(pids[0], Path(buffer.value))
    finally:
        kernel.CloseHandle(handle)


def preflight_ad_write() -> AltiumInstance | None:
    """Allow offline work or one same-privilege AD session, and fail closed otherwise.

    The probe is read-only: it only inspects the current process and running
    X2 session.  It never starts AD, requests elevation, or touches a library.
    """
    if sys.platform != "win32":
        return None
    try:
        return _running_altium()
    except RefreshError as exc:
        status = exc.status if exc.status in {"permission_required", "ambiguous"} else "permission_required"
        if exc.status == "ambiguous":
            reason = "检测到多个 AD 会话，请只保留一个需要使用的 AD 会话"
        elif exc.status == "permission_required":
            reason = "无法确认 AD 与元件库桥具有相同运行权限"
        else:
            reason = str(exc) or "AD 权限预检失败"
        raise RefreshError(status, f"{reason}；{_PREFLIGHT_STOP_MESSAGE}") from exc
    except Exception as exc:
        raise RefreshError(
            "permission_required",
            f"无法可靠确认 AD 的运行权限（{exc}）；{_PREFLIGHT_STOP_MESSAGE}",
        ) from exc


def _check_commands(executable: Path) -> None:
    required = {
        "IntegratedLibrary.INS": "RefreshInstalledLibraries",
        "ScriptingSystem.ins": "RunScriptFile",
        "Altium.Edp.ComponentSearch.ins": "ClearCache",
    }
    for filename, command in required.items():
        path = executable.parent / "System" / filename
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RefreshError("unsupported", f"无法确认当前 AD 提供 {command} 命令，未执行刷新。") from exc
        if not re.search(r"Command\s+Name\s*=\s*'" + command + r"'", text, re.I):
            raise RefreshError("unsupported", f"当前 AD 未提供必需的 {command} 命令，未执行刷新。")


def _pascal_string(value: str) -> str:
    """ASCII-only Delphi literals: paths/names can never become script code."""
    units = value.encode("utf-16-le")
    return "''" + "".join(f"#{int.from_bytes(units[i:i + 2], 'little')}" for i in range(0, len(units), 2))


def _refresh_script(
    directory: Path, request: Path, request_id: str,
    symbols: list[str], footprints: list[str], expires: datetime,
) -> str:
    literal = _pascal_string
    expected_sch = "\n".join(f"        Expected.Add({literal(name)});" for name in symbols)
    expected_pcb = "\n".join(f"        Expected.Add({literal(name)});" for name in footprints)
    deadline = (
        f"EncodeDate({expires.year}, {expires.month}, {expires.day}) + "
        f"EncodeTime({expires.hour}, {expires.minute}, {expires.second}, {expires.microsecond // 1000})"
    )
    # AD DelphiScript cannot capture outer locals in nested routines.
    # Keep helpers standalone and pass the request-owned report explicitly.
    return f"""// No saves or user-document closes; only a clean temporary PCB read may be closed.
Procedure PartsBridgeFinish(Report : TStringList; State : String);
Begin
    Report.Add('status=' + State);
    Report.Add('complete=1');
    Report.SaveToFile({literal(str(request / 'receipt.tmp'))});
    RenameFile({literal(str(request / 'receipt.tmp'))}, {literal(str(request / 'receipt.txt'))});
End;

Function PartsBridgeCanContinue(Report : TStringList) : Boolean;
Begin
    Result := False;
    If (Now > {deadline}) Or (Not FileExists({literal(str(request / 'active'))})) Then
    Begin
        PartsBridgeFinish(Report, 'expired');
        Exit;
    End;
    If FileExists({literal(str(state_directory(directory) / 'transaction.json'))}) Then
    Begin
        PartsBridgeFinish(Report, 'busy');
        Exit;
    End;
    Result := True;
End;

Function PartsBridgeReadFootprints(APath : String; Actual : TStringList) : String;
Var
    ReadDocument : IServerDocument;
    ReadLibrary : IPCB_Library;
    Iterator : IPCB_LibraryIterator;
    Footprint : IPCB_LibComponent;
    WasOpen, OpenedForRead : Boolean;
Begin
    Result := 'pcb_unavailable';
    WasOpen := Client.IsDocumentOpen(APath);
    ReadDocument := Client.GetDocumentByPath(APath);
    OpenedForRead := False;
    Try
        If ReadDocument = Nil Then
        Begin
            If WasOpen Then Exit;
            ReadDocument := Client.OpenDocumentShowOrHide('PCBLIB', APath, False);
            OpenedForRead := ReadDocument <> Nil;
        End;
        If ReadDocument = Nil Then Exit;
        If ReadDocument.Modified Then
        Begin
            Result := 'dirty';
            Exit;
        End;
        If PCBServer = Nil Then Exit;
        ReadLibrary := PCBServer.GetPCBLibraryByPath(APath);
        If ReadLibrary = Nil Then Exit;
        Iterator := ReadLibrary.LibraryIterator_Create;
        If Iterator = Nil Then Exit;
        Try
            Iterator.SetState_FilterAll;
            Footprint := Iterator.FirstPCBObject;
            While Footprint <> Nil Do
            Begin
                Actual.Add(Footprint.Name);
                Footprint := Iterator.NextPCBObject;
            End;
        Finally
            ReadLibrary.LibraryIterator_Destroy(Iterator);
        End;
        If ReadDocument.Modified Then
            Result := 'dirty'
        Else
            Result := 'ok';
    Finally
        If OpenedForRead Then
            If ReadDocument <> Nil Then
                If ReadDocument.Modified Then
                    Result := 'dirty'
                Else
                    Client.CloseDocument(ReadDocument);
    End;
End;

Procedure PartsBridgeRefresh;
Var
    Report, Expected, Actual : TStringList;
    Manager : IIntegratedLibraryManager;
    SchDocument, PcbDocument : IServerDocument;
    I, Count : Integer;
    SymbolsMatch, FootprintsMatch : Boolean;
    ReadStatus : String;
Begin
    Report := TStringList.Create;
    Expected := TStringList.Create;
    Actual := TStringList.Create;
    Try
        Try
            Report.Add('request_id={request_id}');
            If Not PartsBridgeCanContinue(Report) Then Exit;
            Manager := IntegratedLibraryManager;
            If Manager = Nil Then
            Begin
                PartsBridgeFinish(Report, 'unavailable');
                Exit;
            End;
            SchDocument := Client.GetDocumentByPath({literal(str(directory / 'LCSC.SchLib'))});
            PcbDocument := Client.GetDocumentByPath({literal(str(directory / 'LCSC.PcbLib'))});
            If SchDocument <> Nil Then
                If SchDocument.Modified Then
                Begin
                    PartsBridgeFinish(Report, 'dirty');
                    Exit;
                End;
            If PcbDocument <> Nil Then
                If PcbDocument.Modified Then
                Begin
                    PartsBridgeFinish(Report, 'dirty');
                    Exit;
                End;
            If SchDocument <> Nil Then
            Begin
                If Not SchDocument.SupportsReload Then
                Begin
                    PartsBridgeFinish(Report, 'unsupported');
                    Exit;
                End;
            End;
            If PcbDocument <> Nil Then
            Begin
                If Not PcbDocument.SupportsReload Then
                Begin
                    PartsBridgeFinish(Report, 'unsupported');
                    Exit;
                End;
            End;
            If SchDocument <> Nil Then
            Begin
                If Not PartsBridgeCanContinue(Report) Then Exit;
                If Not SchDocument.DoFileLoad Then
                Begin
                    PartsBridgeFinish(Report, 'reload_failed');
                    Exit;
                End;
            End;
            If PcbDocument <> Nil Then
            Begin
                If Not PartsBridgeCanContinue(Report) Then Exit;
                If Not PcbDocument.DoFileLoad Then
                Begin
                    PartsBridgeFinish(Report, 'reload_failed');
                    Exit;
                End;
            End;
            If Not PartsBridgeCanContinue(Report) Then Exit;
            ResetParameters;
            AddStringParameter('AllLibraries', 'True');
            RunProcess('IntegratedLibrary:RefreshInstalledLibraries');
            ResetParameters;
            If Not PartsBridgeCanContinue(Report) Then Exit;
            RunProcess('Altium.Edp.ComponentSearch.Plugin:ClearCache');
            ResetParameters;
{expected_sch}
            Count := Manager.GetComponentCount({literal(str(directory / 'LCSC.SchLib'))});
            Report.Add('symbols=' + IntToStr(Count));
            For I := 0 To Count - 1 Do
                Actual.Add(Manager.GetComponentName({literal(str(directory / 'LCSC.SchLib'))}, I));
            SymbolsMatch := Actual.Count = Expected.Count;
            For I := 0 To Expected.Count - 1 Do
                If Actual.IndexOf(Expected[I]) < 0 Then SymbolsMatch := False;
            If SymbolsMatch Then Report.Add('symbols_match=1') Else Report.Add('symbols_match=0');
            Expected.Clear;
            Actual.Clear;
{expected_pcb}
            If Not PartsBridgeCanContinue(Report) Then Exit;
            ReadStatus := PartsBridgeReadFootprints({literal(str(directory / 'LCSC.PcbLib'))}, Actual);
            If ReadStatus <> 'ok' Then
            Begin
                PartsBridgeFinish(Report, ReadStatus);
                Exit;
            End;
            If Not PartsBridgeCanContinue(Report) Then Exit;
            Count := Actual.Count;
            Report.Add('footprints=' + IntToStr(Count));
            FootprintsMatch := Actual.Count = Expected.Count;
            For I := 0 To Expected.Count - 1 Do
                If Actual.IndexOf(Expected[I]) < 0 Then FootprintsMatch := False;
            If FootprintsMatch Then Report.Add('footprints_match=1') Else Report.Add('footprints_match=0');
            If SymbolsMatch And FootprintsMatch Then PartsBridgeFinish(Report, 'refreshed') Else PartsBridgeFinish(Report, 'stale');
        Except
            PartsBridgeFinish(Report, 'script_error');
        End;
    Finally
        Actual.Free;
        Expected.Free;
        Report.Free;
    End;
End;
"""


def _launch_script(instance: AltiumInstance, script: Path) -> subprocess.Popen[Any]:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    # Preserve the quotes in X2's documented process-launcher syntax rather
    # than adding MSVC-style backslash escapes to its embedded parameters.
    # Windows paths cannot contain double quotes; no shell interprets this.
    command = (
        f'"{instance.executable}" -RScriptingSystem:RunScriptFile('
        f'FileName="{script}"|ProcName="PartsBridgeRefresh")'
    )
    return subprocess.Popen(command, shell=False, startupinfo=startup)


def _read_receipt(path: Path, request_id: str) -> dict[str, str] | None:
    if not path.is_file():
        return None
    raw = path.read_bytes()
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    fields = dict(line.split("=", 1) for line in raw.decode(encoding).splitlines() if "=" in line)
    if fields.get("request_id") != request_id or fields.get("complete") != "1":
        return None
    return fields


def refresh_ad_libraries(output_dir: Path, *, timeout: float = 20.0) -> dict[str, Any]:
    """Refresh a committed pair and require an AD receipt plus matching names.

    Not-running and permission failures never launch AD or request elevation.
    Requests expire so a busy/modal AD cannot execute a queued stale request.
    """
    active: Path | None = None
    details: dict[str, Any] = {}
    try:
        if sys.platform != "win32":
            return _result("unsupported", "自动刷新仅支持 Windows 上正在运行的 Altium Designer。")
        if timeout <= 0:
            raise ValueError("refresh timeout must be positive")
        instance = _running_altium()
        if instance is None:
            return _result("not_running", "AD 未运行，已跳过刷新；未启动 AD。下次打开库即可读取最新内容。")
        _check_commands(instance.executable)
        directory = Path(output_dir).expanduser().resolve()
        state = state_directory(directory)
        if (state / "transaction.json").exists():
            raise RefreshError("busy", "总库存在未完成的发布事务，未刷新。请先完成或恢复追加。")
        before = {name: sha256_file(directory / name) for name in LIBRARY_NAMES}
        from .integrity import native_inventory

        inventory = native_inventory(directory)
        symbols = sorted(inventory["symbols"])
        footprints = sorted(inventory["footprints"])
        if not symbols or not footprints:
            raise RefreshError("failed", "库中没有完整的符号和封装，未执行 AD 刷新。")
        if before != {name: sha256_file(directory / name) for name in LIBRARY_NAMES}:
            raise RefreshError("changed", "读取期间总库发生变化，未执行刷新；请等追加完成后重试。")
        request_id = uuid.uuid4().hex
        request = app_data_dir() / "ad-refresh" / request_id
        if request.resolve().is_relative_to(directory):
            raise RefreshError("failed", "刷新维护目录不能位于总库目录内部，请选择独立的总库目录。")
        request.mkdir(parents=True, exist_ok=False)
        active = request / "active"
        script = request / "Refresh.pas"
        receipt = request / "receipt.txt"
        details = {"receipt_path": str(receipt), "ad_pid": instance.pid}
        script.write_text(
            _refresh_script(directory, request, request_id, symbols, footprints, datetime.now() + timedelta(seconds=timeout)),
            encoding="ascii",
        )
        active.write_text(request_id, encoding="ascii")
        try:
            launcher = _launch_script(instance, script)
        except OSError as exc:
            if getattr(exc, "winerror", None) in (5, 740):
                raise RefreshError("permission_required", _PERMISSION_MESSAGE) from exc
            raise
        deadline = time.monotonic() + timeout
        fields = None
        while time.monotonic() < deadline:
            fields = _read_receipt(receipt, request_id)
            if fields is not None:
                break
            # A successful launcher exit only means delivery, not a refresh ack.
            exit_code = launcher.poll()
            if exit_code is not None and exit_code != 0:
                raise RefreshError("failed", f"AD 命令转交失败（退出码 {exit_code}）；库文件不受影响。")
            time.sleep(0.1)
        if fields is None:
            return _result("timeout", "未收到 AD 刷新回执，不能确认新零件已显示。请结束 AD 中的弹窗/操作后点击“刷新 AD 库”。", **details)
        readback: dict[str, Any] = {}
        summaries = []
        for kind, label, expected in (("symbols", "符号", len(symbols)), ("footprints", "封装", len(footprints))):
            raw_count = fields.get(kind, "")
            actual = int(raw_count) if raw_count.isdecimal() else None
            raw_match = fields.get(f"{kind}_match")
            readback[kind] = {
                "expected": expected, "actual": actual,
                "names_match": raw_match == "1" if raw_match in ("0", "1") else None,
            }
            summaries.append(f"{label} {actual if actual is not None else '未回读'}/{expected}")
        details["readback"] = readback
        summary = "，".join(summaries) + "（AD 回读/库文件）。"
        status = fields.get("status", "script_error")
        messages = {
            "dirty": "目标库存在未保存修改，已停止后续操作并保留文档，未自动保存或关闭。请先处理修改，再点击“刷新 AD 库”。",
            "busy": "总库正在发布，已跳过刷新；请等追加完成后重试。",
            "expired": "AD 未及时处理刷新请求，本次请求已过期；请结束弹窗/操作后重试。",
            "unsupported": "AD 中打开的库不支持安全重载，未强制关闭库；请保存并关闭这两份库后重试。",
            "reload_failed": "AD 无法重新读取当前库，未保存或关闭任何工程；请检查库状态后重试。",
            "unavailable": "AD 库管理接口不可用，未确认刷新成功。",
            "pcb_unavailable": "AD 的 PCB 库接口未完成封装回读，未确认刷新成功；" + summary + "无需重新下载元件。",
            "script_error": "AD 刷新脚本未完整执行，未确认刷新成功；库文件不受影响。",
            "stale": "AD 回读的数量或名称未匹配：" + summary + "已追加的元件不受影响，无需重新下载。",
        }
        if status != "refreshed":
            return _result(status if status in messages else "failed", messages.get(status, "AD 返回了未知刷新状态，未确认成功。"), **details)
        if any(
            fields.get(kind) != str(expected) or fields.get(f"{kind}_match") != "1"
            for kind, expected in (("symbols", len(symbols)), ("footprints", len(footprints)))
        ):
            raise RefreshError("stale", messages["stale"])
        if (state / "transaction.json").exists() or before != {name: sha256_file(directory / name) for name in LIBRARY_NAMES}:
            raise RefreshError("changed", "刷新期间总库又发生变化，请等追加完成后再次刷新。")
        current = _running_altium()
        if current is None or current.pid != instance.pid:
            raise RefreshError("changed", "刷新期间 AD 会话发生变化，未确认当前会话已刷新。")
        return _result(
            "refreshed", f"AD 接口已回执：已执行重载/缓存刷新请求，回读 {len(symbols)} 个符号、{len(footprints)} 个封装一致。",
            verification_scope="ad_library_readback",
            symbols=len(symbols), footprints=len(footprints), **details,
        )
    except RefreshError as exc:
        return _result(exc.status, str(exc), **details)
    except Exception as exc:
        return _result("failed", f"AD 刷新未完成：{exc}。已生成的库文件不受影响，可单独重试刷新。", **details)
    finally:
        if active is not None:
            try:
                active.unlink(missing_ok=True)
            except OSError:
                pass  # The script also checks its embedded expiration time.


def refresh_after_publish(
    manifest: dict[str, Any], output_dir: Path, *, enabled: bool = True,
) -> dict[str, Any]:
    """Call only after prepare_libraries has returned and released its lock."""
    if not enabled:
        return _result("disabled", "本次已关闭自动刷新 AD 库。")
    try:
        if manifest.get("published") is not True or int(manifest.get("added_count", 0)) <= 0:
            return _result("not_needed", "本次没有已发布的新元件，未自动刷新 AD；可点击“刷新 AD 库”单独重试。")
        return refresh_ad_libraries(output_dir)
    except Exception as exc:
        # Keep the publication result truthful even after an unexpected bridge error.
        return _result("failed", f"库已发布，但 AD 刷新未完成：{exc}。可单独重试刷新。")
