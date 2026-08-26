"""Dependency-free Windows desktop interface for the batch workflows."""

from __future__ import annotations

import os
import queue
import re
import threading
import traceback
import webbrowser
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .client import LCSCClient
from .convert import BatchCancelled, prepare_libraries
from .jlc_api import JLCOpenApiSettings
from .library_store import default_output_dir, remember_output_dir
from .models import Candidate
from .workflow import (
    WorkflowCancelled,
    read_codes,
    read_queries,
    resolve_queries,
    write_candidate_csv,
)

_CODE_RE = re.compile(r"^C\d+$", re.IGNORECASE)


class PartsBridgeApp(ttk.Frame):
    def __init__(
        self,
        master: tk.Tk,
        *,
        client_factory: Callable[[], LCSCClient] = LCSCClient,
    ) -> None:
        super().__init__(master, padding=12)
        self.master = master
        self.client_factory = client_factory
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.result_items: dict[str, Candidate] = {}
        self.queue_items: dict[str, dict[str, str]] = {}
        self.job_success: Callable[[Any], None] | None = None
        self.job_label = ""

        self.query_var = tk.StringVar()
        self.limit_var = tk.IntVar(value=10)
        self.in_stock_var = tk.BooleanVar(value=True)
        self.output_var = tk.StringVar(value=str(default_output_dir()))
        self.with_3d_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="就绪")
        self.official_var = tk.StringVar(value=self._official_status())

        self._build()
        self.pack(fill="both", expand=True)
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.master.after(100, self._drain_events)

    @staticmethod
    def _official_status() -> str:
        settings = JLCOpenApiSettings.from_env()
        if settings.configured:
            return "开放平台凭据：已配置；元器件&MRO方法仍需审核通过后在线验收"
        return "开放平台凭据：未配置/审核中；当前使用免登录公开数据模式"

    def _build(self) -> None:
        self.master.title(f"元件库桥 {__version__} - 长期总库（增量追加）")
        self.master.minsize(1040, 720)
        self.master.geometry("1260x820")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        heading = ttk.Frame(self)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        heading.columnconfigure(0, weight=1)
        ttk.Label(
            heading,
            text="元件库桥",
            font=("Microsoft YaHei UI", 18, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            heading,
            text="立创商城数据源 → 原生 Altium SchLib / PcbLib（独立第三方工具）",
        ).grid(row=1, column=0, sticky="w")
        ttk.Label(heading, textvariable=self.official_var).grid(
            row=2, column=0, sticky="w", pady=(4, 0)
        )

        search = ttk.LabelFrame(self, text="1. 搜索并确认元件", padding=8)
        search.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        search.columnconfigure(0, weight=1)
        self.query_entry = ttk.Entry(search, textvariable=self.query_var)
        self.query_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.query_entry.bind("<Return>", lambda _event: self.search())
        ttk.Label(search, text="数量").grid(row=0, column=1, padx=(2, 2))
        self.limit_spin = ttk.Spinbox(
            search, from_=1, to=50, width=5, textvariable=self.limit_var
        )
        self.limit_spin.grid(row=0, column=2, padx=(0, 6))
        self.stock_check = ttk.Checkbutton(
            search, text="仅有库存", variable=self.in_stock_var
        )
        self.stock_check.grid(row=0, column=3, padx=(0, 6))
        self.search_button = ttk.Button(search, text="搜索", command=self.search)
        self.search_button.grid(row=0, column=4, padx=(0, 6))
        self.query_csv_button = ttk.Button(
            search, text="批量查询 CSV", command=self.resolve_csv
        )
        self.query_csv_button.grid(row=0, column=5)

        panes = ttk.Panedwindow(self, orient="vertical")
        panes.grid(row=2, column=0, sticky="nsew")

        result_frame = ttk.LabelFrame(panes, text="候选结果", padding=6)
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)
        result_columns = (
            "lcsc",
            "mpn",
            "manufacturer",
            "package",
            "stock",
            "price",
            "currency",
        )
        self.result_tree = ttk.Treeview(
            result_frame,
            columns=result_columns,
            show="headings",
            selectmode="extended",
            height=7,
        )
        headings = {
            "lcsc": "C 编号",
            "mpn": "制造商料号",
            "manufacturer": "品牌",
            "package": "封装",
            "stock": "库存",
            "price": "阶梯首价",
            "currency": "币种",
        }
        widths = {
            "lcsc": 100,
            "mpn": 220,
            "manufacturer": 130,
            "package": 150,
            "stock": 100,
            "price": 100,
            "currency": 70,
        }
        for column in result_columns:
            self.result_tree.heading(column, text=headings[column])
            self.result_tree.column(column, width=widths[column], anchor="w")
        result_scroll = ttk.Scrollbar(
            result_frame, orient="vertical", command=self.result_tree.yview
        )
        self.result_tree.configure(yscrollcommand=result_scroll.set)
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        result_scroll.grid(row=0, column=1, sticky="ns")
        result_actions = ttk.Frame(result_frame)
        result_actions.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.add_button = ttk.Button(
            result_actions, text="加入生成队列", command=self.add_selected_results
        )
        self.add_button.pack(side="left", padx=(0, 6))
        self.open_product_button = ttk.Button(
            result_actions, text="打开中国站商品页", command=self.open_selected_product
        )
        self.open_product_button.pack(side="left")
        panes.add(result_frame, weight=3)

        queue_frame = ttk.LabelFrame(panes, text="2. 已确认的 C 编号", padding=6)
        queue_frame.columnconfigure(0, weight=1)
        queue_frame.rowconfigure(0, weight=1)
        self.queue_tree = ttk.Treeview(
            queue_frame,
            columns=("lcsc", "mpn", "manufacturer", "package"),
            show="headings",
            selectmode="extended",
            height=5,
        )
        for column, title, width in (
            ("lcsc", "C 编号", 110),
            ("mpn", "制造商料号", 240),
            ("manufacturer", "品牌", 150),
            ("package", "封装", 170),
        ):
            self.queue_tree.heading(column, text=title)
            self.queue_tree.column(column, width=width, anchor="w")
        queue_scroll = ttk.Scrollbar(
            queue_frame, orient="vertical", command=self.queue_tree.yview
        )
        self.queue_tree.configure(yscrollcommand=queue_scroll.set)
        self.queue_tree.grid(row=0, column=0, sticky="nsew")
        queue_scroll.grid(row=0, column=1, sticky="ns")
        queue_actions = ttk.Frame(queue_frame)
        queue_actions.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.import_codes_button = ttk.Button(
            queue_actions, text="导入已确认 C 编号 CSV", command=self.import_codes
        )
        self.import_codes_button.pack(side="left", padx=(0, 6))
        self.remove_button = ttk.Button(
            queue_actions, text="移除选中", command=self.remove_selected_codes
        )
        self.remove_button.pack(side="left", padx=(0, 6))
        self.clear_button = ttk.Button(
            queue_actions, text="清空队列", command=self.clear_codes
        )
        self.clear_button.pack(side="left")
        panes.add(queue_frame, weight=2)

        output = ttk.LabelFrame(self, text="3. 追加到长期总库", padding=8)
        output.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text="总库目录").grid(row=0, column=0, padx=(0, 6))
        self.output_entry = ttk.Entry(output, textvariable=self.output_var)
        self.output_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.browse_button = ttk.Button(
            output, text="选择…", command=self.choose_output_directory
        )
        self.browse_button.grid(row=0, column=2, padx=(0, 6))
        self.open_output_button = ttk.Button(
            output, text="打开目录", command=self.open_output_directory
        )
        self.open_output_button.grid(row=0, column=3)
        self.three_d_check = ttk.Checkbutton(
            output,
            text="下载并嵌入 STEP（默认；无 3D 资源会警告）",
            variable=self.with_3d_var,
        )
        self.three_d_check.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.generate_button = ttk.Button(
            output, text="追加到总库", command=self.generate
        )
        self.generate_button.grid(row=1, column=2, padx=(0, 6), pady=(6, 0))
        self.cancel_button = ttk.Button(
            output, text="停止", command=self.cancel, state="disabled"
        )
        self.cancel_button.grid(row=1, column=3, pady=(6, 0))
        ttk.Label(
            output,
            text=(
                "只维护 LCSC.SchLib/PcbLib，不写原理图；保留历史。"
                "已有 C 编号跳过（不下载、不自动更新模型）。"
                "追加前请在 Altium 保存并关闭这两份库；工程使用前仍需复核。"
            ),
            wraplength=900,
        ).grid(row=2, column=1, columnspan=3, sticky="w", pady=(6, 0))

        log_frame = ttk.LabelFrame(self, text="运行日志", padding=6)
        log_frame.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        self.rowconfigure(4, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=7, wrap="word", state="disabled")
        log_scroll = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        status = ttk.Frame(self)
        status.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status, maximum=100, length=300)
        self.progress.grid(row=0, column=1, sticky="e")

        self._busy_controls = [
            self.search_button,
            self.query_csv_button,
            self.import_codes_button,
            self.generate_button,
            self.browse_button,
            self.add_button,
            self.remove_button,
            self.clear_button,
        ]
        self.query_entry.focus_set()

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self.busy = busy
        for control in self._busy_controls:
            control.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if not busy:
            self.progress["value"] = 0
        self.status_var.set(label if busy else "就绪")

    def _start_job(
        self,
        label: str,
        worker: Callable[[], Any],
        on_success: Callable[[Any], None],
    ) -> None:
        if self.busy:
            return
        self.cancel_event.clear()
        self.job_label = label
        self.job_success = on_success
        self._set_busy(True, label)
        self._log(label)

        def run_worker() -> None:
            try:
                result = worker()
            except (WorkflowCancelled, BatchCancelled) as exc:
                self.events.put(("cancelled", str(exc)))
            except Exception as exc:
                self.events.put(("error", (str(exc), traceback.format_exc())))
            else:
                self.events.put(("success", result))

        threading.Thread(target=run_worker, daemon=True).start()

    def _progress_event(self, done: int, total: int, item: str, status: str) -> None:
        self.events.put(("progress", (done, total, item, status)))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    done, total, item, status = payload
                    self.progress["value"] = (100 * done / total) if total else 0
                    status_text = {"skipped": "已有，跳过"}.get(status, status)
                    suffix = f"：{item} {status_text}" if item else f"：{status_text}"
                    self.status_var.set(self.job_label + suffix)
                elif kind == "search_results":
                    self._show_results(payload)
                elif kind == "success":
                    callback = self.job_success
                    self._set_busy(False)
                    if callback is not None:
                        callback(payload)
                elif kind == "cancelled":
                    self._log("任务已停止；未完成的库没有发布。")
                    self._set_busy(False)
                elif kind == "error":
                    message, details = payload
                    self._log("失败：" + message)
                    self._log(details)
                    self._set_busy(False)
                    messagebox.showerror("任务失败", message, parent=self.master)
        except queue.Empty:
            pass
        self.master.after(100, self._drain_events)

    def search(self) -> None:
        query = self.query_var.get().strip()
        if not query:
            messagebox.showwarning("缺少查询内容", "请输入 C 编号、制造商料号或关键词。", parent=self.master)
            return
        try:
            limit = max(1, min(int(self.limit_var.get()), 50))
        except (TypeError, ValueError, tk.TclError):
            limit = 10
            self.limit_var.set(limit)
        in_stock = bool(self.in_stock_var.get())

        def worker() -> list[Candidate]:
            values = self.client_factory().search(
                query, limit=limit, in_stock=in_stock
            )
            self.events.put(("search_results", values))
            return values

        self._start_job(
            f"正在搜索：{query}",
            worker,
            lambda values: self._log(f"搜索完成：{len(values)} 个候选。"),
        )

    def _show_results(self, candidates: list[Candidate]) -> None:
        children = self.result_tree.get_children()
        if children:
            self.result_tree.delete(*children)
        self.result_items.clear()
        for index, item in enumerate(candidates):
            iid = f"result-{index}"
            self.result_items[iid] = item
            self.result_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    item.lcsc,
                    item.mpn,
                    item.manufacturer,
                    item.package,
                    item.stock,
                    item.price,
                    item.currency,
                ),
            )

    def add_selected_results(self) -> None:
        selected = self.result_tree.selection()
        if not selected:
            messagebox.showinfo("未选择", "请先选择候选元件。", parent=self.master)
            return
        for iid in selected:
            item = self.result_items.get(iid)
            if item is not None and item.lcsc:
                self._add_code(
                    item.lcsc,
                    mpn=item.mpn,
                    manufacturer=item.manufacturer,
                    package=item.package,
                )

    def _add_code(
        self,
        code: str,
        *,
        mpn: str = "",
        manufacturer: str = "",
        package: str = "",
    ) -> None:
        normalized = code.strip().upper()
        if not _CODE_RE.fullmatch(normalized) or normalized in self.queue_items:
            return
        value = {
            "lcsc": normalized,
            "mpn": mpn,
            "manufacturer": manufacturer,
            "package": package,
        }
        self.queue_items[normalized] = value
        self.queue_tree.insert(
            "",
            "end",
            iid=normalized,
            values=(normalized, mpn, manufacturer, package),
        )

    def remove_selected_codes(self) -> None:
        for iid in self.queue_tree.selection():
            self.queue_tree.delete(iid)
            self.queue_items.pop(iid, None)

    def clear_codes(self) -> None:
        children = self.queue_tree.get_children()
        if children:
            self.queue_tree.delete(*children)
        self.queue_items.clear()

    def import_codes(self) -> None:
        filename = filedialog.askopenfilename(
            parent=self.master,
            title="选择已确认 C 编号 CSV",
            filetypes=(("CSV", "*.csv"), ("全部文件", "*.*")),
        )
        if not filename:
            return
        try:
            values = read_codes(Path(filename))
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法读取 CSV", str(exc), parent=self.master)
            return
        invalid = [value for value in values if not _CODE_RE.fullmatch(value)]
        for value in values:
            self._add_code(value)
        self._log(f"已导入 {len(values) - len(invalid)} 个 C 编号。")
        if invalid:
            messagebox.showwarning(
                "忽略无效值",
                "以下内容不是 C 编号：" + ", ".join(invalid[:8]),
                parent=self.master,
            )

    def resolve_csv(self) -> None:
        source = filedialog.askopenfilename(
            parent=self.master,
            title="选择待查询 CSV",
            filetypes=(("CSV", "*.csv"), ("全部文件", "*.*")),
        )
        if not source:
            return
        suggested = Path(source).with_name(Path(source).stem + "_candidates.csv")
        destination = filedialog.asksaveasfilename(
            parent=self.master,
            title="保存候选清单",
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            defaultextension=".csv",
            filetypes=(("CSV", "*.csv"),),
        )
        if not destination:
            return
        try:
            queries = read_queries(Path(source))
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法读取 CSV", str(exc), parent=self.master)
            return
        limit = max(1, min(int(self.limit_var.get()), 50))
        in_stock = bool(self.in_stock_var.get())

        def worker() -> Any:
            result = resolve_queries(
                self.client_factory(),
                queries,
                limit=limit,
                in_stock=in_stock,
                progress=self._progress_event,
                cancelled=self.cancel_event.is_set,
            )
            write_candidate_csv(Path(destination), result.rows)
            return result

        def success(result: Any) -> None:
            self._log(
                f"候选清单已保存：{destination}；"
                f"{result.query_count} 条查询，{result.candidate_count} 个候选，"
                f"{result.no_match_count} 条无结果，{result.error_count} 条错误。"
            )
            messagebox.showinfo("批量查询完成", destination, parent=self.master)

        self._start_job("正在批量查询", worker, success)

    def choose_output_directory(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.master,
            title="选择长期总库目录",
            initialdir=self.output_var.get() or str(default_output_dir()),
        )
        if selected:
            self.output_var.set(selected)

    def open_output_directory(self) -> None:
        path = Path(self.output_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.resolve().as_uri())

    def open_selected_product(self) -> None:
        selected = self.result_tree.selection()
        if len(selected) != 1:
            messagebox.showinfo("请选择一个元件", "一次打开一个商品页。", parent=self.master)
            return
        candidate = self.result_items.get(selected[0])
        if candidate is not None and candidate.product_url:
            webbrowser.open(candidate.product_url)

    def generate(self) -> None:
        codes = list(self.queue_items)
        if not codes:
            messagebox.showwarning("队列为空", "请先加入已确认的 C 编号。", parent=self.master)
            return
        output_text = self.output_var.get().strip()
        if not output_text:
            messagebox.showwarning("缺少总库目录", "请选择长期总库目录。", parent=self.master)
            return
        output = Path(output_text).expanduser()
        with_3d = bool(self.with_3d_var.get())

        def worker() -> tuple[dict[str, Any], int]:
            remember_output_dir(output)
            return prepare_libraries(
                self.client_factory(),
                codes,
                output,
                with_3d=with_3d,
                progress=self._progress_event,
                cancelled=self.cancel_event.is_set,
            )

        def success(value: tuple[dict[str, Any], int]) -> None:
            manifest, status = value
            added_count = int(manifest["added_count"])
            skipped_count = int(manifest["skipped_count"])
            failure_count = len(manifest["failures"])
            total_components = int(manifest["total_components"])
            state = str(manifest.get("status", "published"))
            state_directory = str(manifest["state_directory"])
            backup_directory = manifest.get("backup_directory") or "无"
            added_codes = {str(code).upper() for code in manifest.get("added_codes", [])}
            missing_3d: list[tuple[str, str]] = []
            if with_3d:
                for component in manifest.get("components", []):
                    code = str(component.get("code", "")).upper()
                    if code not in added_codes:
                        continue
                    three_d = component.get("3d")
                    three_d_status = (
                        str(three_d.get("status", "missing"))
                        if isinstance(three_d, dict)
                        else "missing"
                    )
                    if three_d_status != "embedded":
                        missing_3d.append((code, three_d_status))
            if not with_3d:
                three_d_summary = "未请求嵌入 3D（本次未勾选）。"
            elif missing_3d:
                preview = "、".join(code for code, _status in missing_3d[:8])
                suffix = "等" if len(missing_3d) > 8 else ""
                three_d_summary = (
                    f"3D 警告：本次新增有 {len(missing_3d)} 个型号未嵌入 3D"
                    f"（{preview}{suffix}），详情见维护清单。"
                )
            elif added_codes:
                three_d_summary = "本次新增型号均已嵌入 3D；仍需工程复核。"
            else:
                three_d_summary = "本次没有新增型号，未产生新的 3D 模型。"
            failures = manifest["failures"]
            failure_reasons = [
                f"{item.get('code', '?')}: {item.get('error', '未知错误')}"
                for item in failures
            ]
            failure_summary = ""
            if failure_reasons:
                shown = failure_reasons[:8]
                if len(failure_reasons) > len(shown):
                    shown.append(f"……另有 {len(failure_reasons) - len(shown)} 项，详见维护清单")
                failure_summary = "\n失败原因：\n" + "\n".join(shown)
            summary = (
                f"本次新增 {added_count} 个，已有跳过 {skipped_count} 个，"
                f"失败 {failure_count} 个，总量 {total_components} 个；"
                f"状态 {state}，退出码 {status}。"
            )
            self._log(
                ("追加完成：" if not failure_count else "追加结束但有失败：")
                + summary
                + f"库外维护目录：{state_directory}；备份：{backup_directory}。"
                + f"{three_d_summary}{failure_summary}"
            )
            dialog = messagebox.showwarning if failure_count else messagebox.showinfo
            dialog(
                "总库追加有失败" if failure_count else "总库追加完成",
                f"{summary}\n"
                f"总库：{output}\n"
                f"库外维护目录：{state_directory}\n"
                f"发布前备份：{backup_directory}\n"
                f"{three_d_summary}{failure_summary}\n\n"
                "工程使用前仍需复核数据手册、焊盘和 Pin 1。",
                parent=self.master,
            )

        self._start_job("正在追加到长期总库", worker, success)

    def cancel(self) -> None:
        if self.busy:
            self.cancel_event.set()
            self.status_var.set("正在安全停止；当前网络请求结束后生效…")
            self._log("已请求停止；不会发布未完成的库。")

    def _on_close(self) -> None:
        if self.busy:
            messagebox.showwarning(
                "任务仍在运行",
                "请先点击“停止”，等待状态恢复为“就绪”后再关闭。",
                parent=self.master,
            )
            return
        self.master.destroy()


def run() -> None:
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    PartsBridgeApp(root)
    root.mainloop()


if __name__ == "__main__":
    run()


__all__ = ["PartsBridgeApp", "default_output_dir", "run"]
