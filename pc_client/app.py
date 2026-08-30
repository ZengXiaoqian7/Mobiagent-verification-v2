"""Tk desktop interface for the PC App-test evaluation agent."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Mapping
import webbrowser

from app_test_agent.mock_executor import MOCK_SCENARIOS

from .service import (
    DEVICE_MUTATION_CONFIRMATION,
    PcEvaluationMode,
    PcEvaluationRequest,
    format_model_event_for_display,
    run_pc_evaluation,
)
from .runtime_paths import (
    BUNDLE_ROOT,
    DEFAULT_CASE_PATH,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_RUNNER_ROOT,
)


MODE_LABELS = {
    "离线 Manifest 回放（推荐）": PcEvaluationMode.MANIFEST_REPLAY,
    "Mock 无设备自检": PcEvaluationMode.MOCK,
    "真机预检（不操作设备）": PcEvaluationMode.DEVICE_PREFLIGHT,
    "真机执行（会操作设备）": PcEvaluationMode.DEVICE_EXECUTION,
}
RESULT_COLORS = {
    "APP_PASS": "#0F9D58",
    "APP_FAIL": "#D93025",
    "TEST_EXECUTION_FAIL": "#E37400",
    "ENV_BLOCKED": "#9C27B0",
    "INCONCLUSIVE": "#5F6368",
    "UNSUPPORTED": "#5F6368",
}


class MobiAgentPcApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MobiAgent 测评智能体")
        self.geometry("1060x760")
        self.minsize(900, 680)
        self.configure(background="#F3F6FA")
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._running = False
        self._last_output: Path | None = None
        self._build_style()
        self._build_variables()
        self._build_ui()
        self._update_mode_fields()
        self.after(120, self._poll_events)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("App.TFrame", background="#F3F6FA")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure(
            "Title.TLabel",
            background="#F3F6FA",
            foreground="#172B4D",
            font=("Microsoft YaHei UI", 20, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#F3F6FA",
            foreground="#5E6C84",
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "CardTitle.TLabel",
            background="#FFFFFF",
            foreground="#172B4D",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.configure("Card.TLabel", background="#FFFFFF", foreground="#344563")
        style.configure("Card.TCheckbutton", background="#FFFFFF", foreground="#344563")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(18, 9))
        style.configure("Secondary.TButton", padding=(12, 7))

    def _build_variables(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.mode_var = tk.StringVar(value=next(iter(MODE_LABELS)))
        self.case_var = tk.StringVar(value=str(DEFAULT_CASE_PATH))
        self.manifest_var = tk.StringVar()
        self.output_var = tk.StringVar(
            value=str(DEFAULT_OUTPUT_ROOT / timestamp)
        )
        self.mock_var = tk.StringVar(value="pass")
        self.device_var = tk.StringVar(value="Harmony")
        self.serial_var = tk.StringVar()
        self.runner_root_var = tk.StringVar(value=str(DEFAULT_RUNNER_ROOT))
        self.recompute_var = tk.BooleanVar(value=True)
        self.confirm_mutation_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="就绪：请选择离线 trace 或运行 Mock 自检")
        self.result_var = tk.StringVar(value="尚未运行")

    def _build_ui(self) -> None:
        root = ttk.Frame(self, style="App.TFrame", padding=24)
        root.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        ttk.Label(root, text="MobiAgent 测评智能体", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            root,
            text="PC 侧执行、离线证据回放与可审计归因；不会在界面中保存模型密钥。",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 18))

        card = ttk.Frame(root, style="Card.TFrame", padding=18)
        card.grid(row=2, column=0, sticky="ew")
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="运行配置", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )

        self._label(card, "模式", 1)
        mode_box = ttk.Combobox(
            card,
            textvariable=self.mode_var,
            values=tuple(MODE_LABELS),
            state="readonly",
        )
        mode_box.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
        mode_box.bind("<<ComboboxSelected>>", lambda _event: self._update_mode_fields())

        self._label(card, "测试用例", 2)
        self.case_entry = ttk.Entry(card, textvariable=self.case_var)
        self.case_entry.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(
            card,
            text="浏览…",
            style="Secondary.TButton",
            command=lambda: self._choose_file(self.case_var, (("JSON", "*.json"),)),
        ).grid(row=2, column=2, padx=(8, 0), pady=4)

        self._label(card, "Manifest", 3)
        self.manifest_entry = ttk.Entry(card, textvariable=self.manifest_var)
        self.manifest_entry.grid(row=3, column=1, sticky="ew", pady=4)
        self.manifest_button = ttk.Button(
            card,
            text="浏览…",
            style="Secondary.TButton",
            command=lambda: self._choose_file(self.manifest_var, (("JSON", "*.json"),)),
        )
        self.manifest_button.grid(row=3, column=2, padx=(8, 0), pady=4)

        self._label(card, "输出目录", 4)
        ttk.Entry(card, textvariable=self.output_var).grid(row=4, column=1, sticky="ew", pady=4)
        ttk.Button(
            card,
            text="选择…",
            style="Secondary.TButton",
            command=self._choose_output_dir,
        ).grid(row=4, column=2, padx=(8, 0), pady=4)

        self._label(card, "Mock 场景", 5)
        self.mock_box = ttk.Combobox(
            card,
            textvariable=self.mock_var,
            values=MOCK_SCENARIOS,
            state="readonly",
        )
        self.mock_box.grid(row=5, column=1, columnspan=2, sticky="ew", pady=4)

        self._label(card, "设备 / 序列号", 6)
        device_row = ttk.Frame(card, style="Card.TFrame")
        device_row.grid(row=6, column=1, columnspan=2, sticky="ew", pady=4)
        device_row.columnconfigure(1, weight=1)
        self.device_box = ttk.Combobox(
            device_row,
            textvariable=self.device_var,
            values=("Harmony", "Android"),
            state="readonly",
            width=14,
        )
        self.device_box.grid(row=0, column=0, sticky="w")
        self.serial_entry = ttk.Entry(device_row, textvariable=self.serial_var)
        self.serial_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        self.recompute_check = ttk.Checkbutton(
            card,
            text="从 raw actions / frames 使用当前规则重算 Step Gate（推荐）",
            variable=self.recompute_var,
            style="Card.TCheckbutton",
        )
        self.recompute_check.grid(row=7, column=1, columnspan=2, sticky="w", pady=(8, 2))
        self.confirm_check = ttk.Checkbutton(
            card,
            text="我确认真机执行会点击、输入并可能产生发布/发送等业务副作用",
            variable=self.confirm_mutation_var,
            style="Card.TCheckbutton",
        )
        self.confirm_check.grid(row=8, column=1, columnspan=2, sticky="w", pady=2)

        action_row = ttk.Frame(card, style="Card.TFrame")
        action_row.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        action_row.columnconfigure(0, weight=1)
        ttk.Label(action_row, textvariable=self.status_var, style="Card.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.open_button = ttk.Button(
            action_row,
            text="打开结果",
            style="Secondary.TButton",
            command=self._open_output,
            state="disabled",
        )
        self.open_button.grid(row=0, column=1, padx=(8, 0))
        self.run_button = ttk.Button(
            action_row,
            text="开始评测",
            style="Primary.TButton",
            command=self._start_run,
        )
        self.run_button.grid(row=0, column=2, padx=(8, 0))

        result_card = ttk.Frame(root, style="Card.TFrame", padding=18)
        result_card.grid(row=3, column=0, sticky="nsew", pady=(16, 0))
        result_card.columnconfigure(0, weight=1)
        result_card.rowconfigure(2, weight=1)
        ttk.Label(result_card, text="评测结果", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.result_label = tk.Label(
            result_card,
            textvariable=self.result_var,
            background="#FFFFFF",
            foreground="#5F6368",
            font=("Microsoft YaHei UI", 18, "bold"),
            anchor="w",
        )
        self.result_label.grid(row=1, column=0, sticky="ew", pady=(8, 10))
        self.log = tk.Text(
            result_card,
            height=12,
            wrap="word",
            relief="flat",
            background="#F7F9FC",
            foreground="#344563",
            font=("Consolas", 10),
            padx=12,
            pady=10,
            state="disabled",
        )
        self.log.grid(row=2, column=0, sticky="nsew")

    @staticmethod
    def _label(parent: ttk.Frame, text: str, row: int) -> None:
        ttk.Label(parent, text=text, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=4
        )

    def _update_mode_fields(self) -> None:
        mode = MODE_LABELS[self.mode_var.get()]
        manifest_state = "normal" if mode == PcEvaluationMode.MANIFEST_REPLAY else "disabled"
        mock_state = "readonly" if mode == PcEvaluationMode.MOCK else "disabled"
        device_state = (
            "readonly"
            if mode in {PcEvaluationMode.DEVICE_PREFLIGHT, PcEvaluationMode.DEVICE_EXECUTION}
            else "disabled"
        )
        serial_state = (
            "normal"
            if mode in {PcEvaluationMode.DEVICE_PREFLIGHT, PcEvaluationMode.DEVICE_EXECUTION}
            else "disabled"
        )
        self.manifest_entry.configure(state=manifest_state)
        self.manifest_button.configure(state=manifest_state)
        self.mock_box.configure(state=mock_state)
        self.device_box.configure(state=device_state)
        self.serial_entry.configure(state=serial_state)
        self.recompute_check.configure(
            state="normal" if mode == PcEvaluationMode.MANIFEST_REPLAY else "disabled"
        )
        self.confirm_check.configure(
            state="normal" if mode == PcEvaluationMode.DEVICE_EXECUTION else "disabled"
        )
        if mode != PcEvaluationMode.DEVICE_EXECUTION:
            self.confirm_mutation_var.set(False)

    def _choose_file(self, variable: tk.StringVar, filetypes: tuple[tuple[str, str], ...]) -> None:
        selected = filedialog.askopenfilename(initialdir=BUNDLE_ROOT, filetypes=filetypes)
        if selected:
            variable.set(selected)

    def _choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=Path(self.output_var.get()).parent)
        if selected:
            self.output_var.set(selected)

    def _start_run(self) -> None:
        if self._running:
            return
        mode = MODE_LABELS[self.mode_var.get()]
        if mode == PcEvaluationMode.DEVICE_EXECUTION:
            if not self.confirm_mutation_var.get():
                messagebox.showwarning("需要确认", "请先确认真机执行的设备与业务副作用。")
                return
            if not messagebox.askyesno(
                "确认真机执行",
                "这会实际操作连接的设备，可能发布内容或发送消息。是否继续？",
                icon="warning",
            ):
                return
        request = PcEvaluationRequest(
            mode=mode,
            test_case_path=Path(self.case_var.get().strip()),
            output_dir=Path(self.output_var.get().strip()),
            manifest_path=(
                Path(self.manifest_var.get().strip())
                if self.manifest_var.get().strip()
                else None
            ),
            recompute_step_gates=self.recompute_var.get(),
            mock_scenario=self.mock_var.get(),
            device=self.device_var.get(),
            device_serial=self.serial_var.get(),
            runner_root=Path(self.runner_root_var.get()),
            device_mutation_confirmation=(
                DEVICE_MUTATION_CONFIRMATION
                if self.confirm_mutation_var.get()
                else None
            ),
            model_event_sink=(
                self._queue_model_event
                if mode == PcEvaluationMode.DEVICE_EXECUTION
                else None
            ),
        )
        self._running = True
        self.run_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.status_var.set("运行中，请勿关闭窗口…")
        self.result_var.set("RUNNING")
        self.result_label.configure(foreground="#1A73E8")
        self._append_log(f"mode={mode}\ntest_case={request.test_case_path}\noutput={request.output_dir}\n")
        threading.Thread(target=self._run_worker, args=(request,), daemon=True).start()

    def _run_worker(self, request: PcEvaluationRequest) -> None:
        try:
            self._events.put(("result", run_pc_evaluation(request)))
        except Exception as exc:  # noqa: BLE001 - surfaced to the desktop operator.
            self._events.put(("error", exc))

    def _queue_model_event(self, event: Mapping[str, object]) -> None:
        self._events.put(("model_event", dict(event)))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "model_event" and isinstance(payload, Mapping):
                    self._append_log(format_model_event_for_display(payload))
                elif kind == "result":
                    self._finish_success(payload)
                else:
                    self._finish_error(payload)
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def _finish_success(self, result: object) -> None:
        payload = result.as_dict()  # type: ignore[attr-defined]
        self._running = False
        self.run_button.configure(state="normal")
        self._last_output = Path(payload["output_dir"])
        self.open_button.configure(state="normal")
        verdict = payload.get("overall_result") or payload["status"]
        self.result_var.set(str(verdict))
        self.result_label.configure(foreground=RESULT_COLORS.get(str(verdict), "#1A73E8"))
        self.status_var.set("完成：结果已写入输出目录")
        self._append_log(json.dumps(payload, ensure_ascii=False, indent=2))

    def _finish_error(self, error: object) -> None:
        self._running = False
        self.run_button.configure(state="normal")
        self.status_var.set("运行失败，请检查配置和日志")
        self.result_var.set("ERROR")
        self.result_label.configure(foreground="#D93025")
        rendered = f"{type(error).__name__}: {error}"
        self._append_log(rendered)
        messagebox.showerror("评测失败", rendered)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_output(self) -> None:
        if self._last_output is None:
            return
        report = self._last_output / "report.md"
        target = report if report.is_file() else self._last_output
        try:
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                webbrowser.open(target.resolve().as_uri())
        except OSError as exc:
            messagebox.showerror("无法打开结果", str(exc))


def main() -> int:
    app = MobiAgentPcApp()
    app.mainloop()
    return 0


__all__ = ["MobiAgentPcApp", "main"]
