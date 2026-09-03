"""Tk desktop workbench for the PC App-test evaluation agent."""

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

from .device_discovery import ConnectedDevice, DeviceDiscoveryResult, discover_connected_devices
from .environment import check_environment
from .presentation import ReportSummary, load_report_summary, load_test_case_summary
from .service import (
    DEVICE_MUTATION_CONFIRMATION,
    PcEvaluationMode,
    PcEvaluationRequest,
    format_model_event_for_display,
    run_pc_evaluation,
)
from .runtime_paths import BUNDLE_ROOT, DEFAULT_CASE_PATH, DEFAULT_OUTPUT_ROOT, DEFAULT_RUNNER_ROOT


MODE_LABELS = {
    "离线 Manifest 回放": PcEvaluationMode.MANIFEST_REPLAY,
    "Mock 无设备自检": PcEvaluationMode.MOCK,
    "生成真机预检包": PcEvaluationMode.DEVICE_PREFLIGHT,
    "真机执行": PcEvaluationMode.DEVICE_EXECUTION,
}
RESULT_COLORS = {
    "APP_PASS": "#147D64",
    "APP_FAIL": "#C23B32",
    "TEST_EXECUTION_FAIL": "#B85C00",
    "ENV_BLOCKED": "#8E4B9E",
    "INCONCLUSIVE": "#58636E",
    "UNSUPPORTED": "#58636E",
}


class MobiAgentPcApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MobiAgent 应用功能评测")
        self.geometry("1360x860")
        self.minsize(1080, 720)
        self.configure(background="#F4F6F8")
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._running = False
        self._last_output: Path | None = None
        self._devices_by_label: dict[str, ConnectedDevice] = {}
        self._case_refresh_job: str | None = None
        self._nav_buttons: dict[str, tk.Button] = {}
        self._views: dict[str, ttk.Frame] = {}
        self._active_view = "setup"
        self._live_technical_visible = False
        self._result_details_visible = False
        self._build_style()
        self._build_variables()
        self._build_ui()
        self._update_mode_fields()
        self._schedule_case_summary()
        self.after(180, self._refresh_devices)
        self.after(120, self._poll_events)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("App.TFrame", background="#F4F6F8")
        style.configure("Workspace.TFrame", background="#F4F6F8")
        style.configure("Topbar.TFrame", background="#F4F6F8")
        style.configure("HeaderTitle.TLabel", background="#F4F6F8", foreground="#172B3A", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("HeaderMeta.TLabel", background="#F4F6F8", foreground="#6B7A88", font=("Microsoft YaHei UI", 9))
        style.configure("Surface.TFrame", background="#FFFFFF")
        style.configure("Section.TLabel", background="#FFFFFF", foreground="#172B3A", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("CardTitle.TLabel", background="#FFFFFF", foreground="#172B3A", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Field.TLabel", background="#FFFFFF", foreground="#425466", font=("Microsoft YaHei UI", 9))
        style.configure("Muted.TLabel", background="#FFFFFF", foreground="#748494", font=("Microsoft YaHei UI", 9))
        style.configure("TopbarStatus.TLabel", background="#E8F1EE", foreground="#176B52", padding=(11, 6), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TopbarRunning.TLabel", background="#E8F1F7", foreground="#176487", padding=(11, 6), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TopbarComplete.TLabel", background="#E8F5EF", foreground="#156A51", padding=(11, 6), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TopbarError.TLabel", background="#FCEAEA", foreground="#B13B38", padding=(11, 6), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Safe.TLabel", background="#E8F5EF", foreground="#156A51", padding=(9, 6), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Warn.TLabel", background="#FFF3DF", foreground="#965A06", padding=(9, 6), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Metric.TLabel", background="#F4F7FA", foreground="#263B4A", padding=(12, 10), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(18, 10), foreground="#FFFFFF", background="#146C94")
        style.map("Primary.TButton", background=[("active", "#0E587C"), ("disabled", "#AAB9C3")])
        style.configure("Secondary.TButton", padding=(12, 8), foreground="#294B60")
        style.configure("Quiet.TButton", padding=(8, 5), foreground="#42657A")
        style.configure("TEntry", padding=(7, 5), fieldbackground="#FFFFFF")
        style.configure("TCombobox", padding=(6, 4), fieldbackground="#FFFFFF")
        style.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", foreground="#263B4A", rowheight=30, borderwidth=0)
        style.configure("Treeview.Heading", background="#EEF3F6", foreground="#526575", font=("Microsoft YaHei UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#DCECF4")], foreground=[("selected", "#172B3A")])

    def _build_variables(self) -> None:
        self.mode_var = tk.StringVar(value="真机执行")
        self.case_var = tk.StringVar(value=str(DEFAULT_CASE_PATH))
        self.manifest_var = tk.StringVar()
        self.output_var = tk.StringVar(value=self._new_output_dir())
        self.mock_var = tk.StringVar(value="pass")
        self.device_var = tk.StringVar(value="Harmony")
        self.serial_var = tk.StringVar()
        self.device_target_var = tk.StringVar(value="正在检测连接设备...")
        self.device_status_var = tk.StringVar(value="设备发现只读取 hdc / adb 连接列表")
        self.environment_status_var = tk.StringVar(value="运行环境尚未检查")
        self.runner_root_var = tk.StringVar(value=str(DEFAULT_RUNNER_ROOT))
        self.model_base_url_var = tk.StringVar(value=os.getenv("MOBIAGENT_BASE_URL", ""))
        self.model_name_var = tk.StringVar(value=os.getenv("MOBIAGENT_MODEL", ""))
        self.api_key_file_var = tk.StringVar(value=os.getenv("MOBIAGENT_API_KEY_FILE", ""))
        self.wire_api_var = tk.StringVar(value=os.getenv("MOBIAGENT_WIRE_API", "responses"))
        self.reasoning_effort_var = tk.StringVar(value=os.getenv("MOBIAGENT_REASONING_EFFORT", "high"))
        self.recompute_var = tk.BooleanVar(value=True)
        self.confirm_mutation_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="READY")
        self.status_detail_var = tk.StringVar(value="选择用例并检查运行配置")
        self.result_var = tk.StringVar(value="尚未运行")
        self.case_title_var = tk.StringVar(value="正在读取用例...")
        self.case_meta_var = tk.StringVar(value="")
        self.case_detail_meta_var = tk.StringVar(value="")
        self.case_impact_var = tk.StringVar(value="正在分析用例影响...")
        self.case_risk_var = tk.StringVar(value="风险：未知")
        self.case_policy_var = tk.StringVar(value="")
        self.result_execution_var = tk.StringVar(value="执行：-")
        self.result_behavior_var = tk.StringVar(value="应用断言：-")
        self.result_attribution_var = tk.StringVar(value="归因：-")
        self.result_verification_var = tk.StringVar(value="只读核验：-")
        self.result_reason_var = tk.StringVar(value="完成运行后，这里会显示独立的执行、应用行为与归因结论。")
        self.case_var.trace_add("write", lambda *_args: self._schedule_case_summary())

    @staticmethod
    def _new_output_dir() -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return str(DEFAULT_OUTPUT_ROOT / timestamp)

    def _build_ui(self) -> None:
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        shell = tk.Frame(self, background="#102633")
        shell.grid(row=0, column=0, sticky="nsew")
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(1, weight=1)
        self._build_sidebar(shell)

        workspace = ttk.Frame(shell, style="Workspace.TFrame", padding=(28, 22, 28, 18))
        workspace.grid(row=0, column=1, sticky="nsew")
        workspace.rowconfigure(1, weight=1)
        workspace.columnconfigure(0, weight=1)
        topbar = ttk.Frame(workspace, style="Topbar.TFrame")
        topbar.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        topbar.columnconfigure(1, weight=1)
        self.page_title_var = tk.StringVar(value="评测工作台")
        self.page_subtitle_var = tk.StringVar(value="选择用例，检查设备，再启动一次可追溯的评测")
        ttk.Label(topbar, textvariable=self.page_title_var, style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(topbar, textvariable=self.page_subtitle_var, style="HeaderMeta.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.status_chip = ttk.Label(topbar, textvariable=self.status_var, style="TopbarStatus.TLabel")
        self.status_chip.grid(row=0, column=2, rowspan=2, sticky="e")

        self.view_host = ttk.Frame(workspace, style="Workspace.TFrame")
        self.view_host.grid(row=1, column=0, sticky="nsew")
        self.view_host.rowconfigure(0, weight=1)
        self.view_host.columnconfigure(0, weight=1)
        self.setup_tab = ttk.Frame(self.view_host, style="App.TFrame")
        self.live_tab = ttk.Frame(self.view_host, style="App.TFrame")
        self.result_tab = ttk.Frame(self.view_host, style="App.TFrame")
        self._views = {"setup": self.setup_tab, "live": self.live_tab, "result": self.result_tab}
        for view in self._views.values():
            view.grid(row=0, column=0, sticky="nsew")
        self._build_setup_tab()
        self._build_live_tab()
        self._build_result_tab()

        action = ttk.Frame(workspace, style="Surface.TFrame", padding=(18, 11))
        action.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        action.columnconfigure(0, weight=1)
        ttk.Label(action, textvariable=self.status_detail_var, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.open_button = ttk.Button(action, text="打开报告", style="Secondary.TButton", command=self._open_report, state="disabled")
        self.open_button.grid(row=0, column=1, padx=(8, 0))
        self.folder_button = ttk.Button(action, text="证据目录", style="Secondary.TButton", command=self._open_output, state="disabled")
        self.folder_button.grid(row=0, column=2, padx=(8, 0))
        self.run_button = ttk.Button(action, text="开始评测", style="Primary.TButton", command=self._start_run)
        self.run_button.grid(row=0, column=3, padx=(12, 0))
        self._show_view("setup")

    def _build_sidebar(self, parent: tk.Frame) -> None:
        sidebar = tk.Frame(parent, background="#102633", width=238)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        brand = tk.Frame(sidebar, background="#102633")
        brand.pack(fill="x", padx=22, pady=(27, 30))
        tk.Label(brand, text="M", background="#1D7AA3", foreground="#FFFFFF", font=("Microsoft YaHei UI", 14, "bold"), width=3, height=1).pack(side="left")
        brand_copy = tk.Frame(brand, background="#102633")
        brand_copy.pack(side="left", padx=(10, 0))
        tk.Label(brand_copy, text="MobiAgent", background="#102633", foreground="#F6FAFC", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(brand_copy, text="应用功能评测", background="#102633", foreground="#8FA7B5", font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(1, 0))
        tk.Label(sidebar, text="工作区", background="#102633", foreground="#78929F", font=("Microsoft YaHei UI", 8, "bold")).pack(anchor="w", padx=22, pady=(0, 7))
        self._add_nav_button(sidebar, "setup", "01  评测工作台")
        self._add_nav_button(sidebar, "live", "02  运行过程")
        self._add_nav_button(sidebar, "result", "03  结果与证据")
        spacer = tk.Frame(sidebar, background="#102633")
        spacer.pack(fill="both", expand=True)
        status = tk.Frame(sidebar, background="#173544")
        status.pack(fill="x", padx=16, pady=18)
        tk.Label(status, text="操作提示", background="#173544", foreground="#B9D3DF", font=("Microsoft YaHei UI", 8, "bold")).pack(anchor="w", padx=12, pady=(10, 2))
        tk.Label(status, text="先运行预检，再执行会改变应用数据的用例。", background="#173544", foreground="#D9E7ED", font=("Microsoft YaHei UI", 8), justify="left", wraplength=180).pack(anchor="w", padx=12, pady=(0, 11))

    def _add_nav_button(self, parent: tk.Frame, name: str, text: str) -> None:
        button = tk.Button(parent, text=text, command=lambda: self._show_view(name), anchor="w", relief="flat", borderwidth=0, highlightthickness=0, padx=22, pady=11, background="#102633", foreground="#C8D8E0", activebackground="#1A4152", activeforeground="#FFFFFF", font=("Microsoft YaHei UI", 10))
        button.pack(fill="x", padx=10, pady=2)
        self._nav_buttons[name] = button

    def _show_view(self, name: str) -> None:
        if name not in self._views:
            return
        self._active_view = name
        self._views[name].tkraise()
        titles = {
            "setup": ("评测工作台", "选择用例，检查设备，再启动一次可追溯的评测"),
            "live": ("运行过程", "查看模型事件与运行日志；执行期间请勿断开目标设备"),
            "result": ("结果与证据", "阅读结论、断言状态和可打开的证据文件"),
        }
        title, subtitle = titles[name]
        self.page_title_var.set(title)
        self.page_subtitle_var.set(subtitle)
        for key, button in self._nav_buttons.items():
            active = key == name
            button.configure(background="#1D7AA3" if active else "#102633", foreground="#FFFFFF" if active else "#C8D8E0", activebackground="#1D7AA3" if active else "#1A4152")

    def _set_run_status(self, status: str, detail: str) -> None:
        self.status_var.set(status)
        self.status_detail_var.set(detail)
        styles = {
            "RUNNING": "TopbarRunning.TLabel",
            "COMPLETE": "TopbarComplete.TLabel",
            "ERROR": "TopbarError.TLabel",
        }
        self.status_chip.configure(style=styles.get(status, "TopbarStatus.TLabel"))

    def _build_setup_tab(self) -> None:
        self.setup_tab.rowconfigure(0, weight=1)
        self.setup_tab.columnconfigure(0, weight=3)
        self.setup_tab.columnconfigure(1, weight=2)

        workflow = ttk.Frame(self.setup_tab, style="Surface.TFrame", padding=22)
        workflow.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        workflow.columnconfigure(0, weight=1)
        ttk.Label(workflow, text="新建评测", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(workflow, text="只需选择用例和设备。其他配置由客户端自动处理。", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 20))

        ttk.Label(workflow, text="1   选择测试用例", style="Section.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(workflow, text="选择已有用例，客户端会读取应用名称、测试目标和影响范围。", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(2, 8))
        case_row = ttk.Frame(workflow, style="Surface.TFrame")
        case_row.grid(row=4, column=0, sticky="ew")
        case_row.columnconfigure(0, weight=1)
        self.case_entry = ttk.Entry(case_row, textvariable=self.case_var)
        self.case_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(case_row, text="更换用例", style="Secondary.TButton", command=lambda: self._choose_file(self.case_var, (("JSON", "*.json"),))).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(workflow, textvariable=self.case_title_var, style="Section.TLabel", wraplength=620).grid(row=5, column=0, sticky="w", pady=(12, 2))
        ttk.Label(workflow, textvariable=self.case_meta_var, style="Muted.TLabel", wraplength=620).grid(row=6, column=0, sticky="w")

        ttk.Separator(workflow).grid(row=7, column=0, sticky="ew", pady=20)
        ttk.Label(workflow, text="2   选择目标设备", style="Section.TLabel").grid(row=8, column=0, sticky="w")
        ttk.Label(workflow, text="已连接的 HarmonyOS 和 Android 设备会自动显示在这里。", style="Muted.TLabel").grid(row=9, column=0, sticky="w", pady=(2, 8))
        device_row = ttk.Frame(workflow, style="Surface.TFrame")
        device_row.grid(row=10, column=0, sticky="ew")
        device_row.columnconfigure(0, weight=1)
        self.detected_box = ttk.Combobox(device_row, textvariable=self.device_target_var, state="readonly")
        self.detected_box.grid(row=0, column=0, sticky="ew")
        self.detected_box.bind("<<ComboboxSelected>>", lambda _event: self._select_detected_device())
        self.refresh_devices_button = ttk.Button(device_row, text="重新检测", style="Secondary.TButton", command=self._refresh_devices)
        self.refresh_devices_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(workflow, textvariable=self.device_status_var, style="Muted.TLabel", wraplength=620).grid(row=11, column=0, sticky="w", pady=(8, 0))

        ttk.Separator(workflow).grid(row=12, column=0, sticky="ew", pady=20)
        tools_row = ttk.Frame(workflow, style="Surface.TFrame")
        tools_row.grid(row=13, column=0, sticky="ew")
        tools_row.columnconfigure(0, weight=1)
        ttk.Label(tools_row, text="需要更多控制？", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.advanced_settings_button = ttk.Button(tools_row, text="高级工具与设置", style="Secondary.TButton", command=self._open_advanced_settings)
        self.advanced_settings_button.grid(row=0, column=1, sticky="e")

        overview = ttk.Frame(self.setup_tab, style="Surface.TFrame", padding=22)
        overview.grid(row=0, column=1, sticky="nsew", padx=(9, 0))
        overview.columnconfigure(0, weight=1)
        ttk.Label(overview, text="执行前确认", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(overview, text="开始前确认测试目标和实际影响。", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 18))
        ttk.Label(overview, text="本次将测试", style="Field.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(overview, textvariable=self.case_title_var, style="Section.TLabel", wraplength=380).grid(row=3, column=0, sticky="w", pady=(3, 8))
        ttk.Label(overview, textvariable=self.case_impact_var, style="Muted.TLabel", wraplength=380).grid(row=4, column=0, sticky="w")
        self.risk_label = ttk.Label(overview, textvariable=self.case_risk_var, style="Warn.TLabel", wraplength=380)
        self.risk_label.grid(row=5, column=0, sticky="ew", pady=(14, 16))
        ttk.Label(overview, text="设备状态", style="Field.TLabel").grid(row=6, column=0, sticky="w")
        self.environment_label = ttk.Label(overview, textvariable=self.environment_status_var, style="Safe.TLabel", wraplength=380)
        self.environment_label.grid(row=7, column=0, sticky="ew", pady=(4, 8))
        self.confirm_check = ttk.Checkbutton(overview, text="我已确认此用例可能对应用产生真实业务操作", variable=self.confirm_mutation_var)
        self.confirm_check.grid(row=8, column=0, sticky="w", pady=(10, 6))
        ttk.Button(overview, text="查看用例详情", style="Quiet.TButton", command=self._open_case_details).grid(row=9, column=0, sticky="w")

        self._build_advanced_settings_window()
        self._build_case_details_window()

    def _build_advanced_settings_window(self) -> None:
        self.advanced_window = tk.Toplevel(self)
        self.advanced_window.withdraw()
        self.advanced_window.title("高级工具与设置")
        self.advanced_window.geometry("760x650")
        self.advanced_window.minsize(680, 540)
        self.advanced_window.configure(background="#F4F6F8")
        self.advanced_window.transient(self)
        self.advanced_window.protocol("WM_DELETE_WINDOW", self._close_advanced_settings)
        content = ttk.Frame(self.advanced_window, style="Workspace.TFrame", padding=22)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        ttk.Label(content, text="高级工具与设置", style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(content, text="这些设置用于诊断、回放和特殊环境；日常真机评测无需调整。", style="HeaderMeta.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 16))

        run_settings = ttk.Frame(content, style="Surface.TFrame", padding=16)
        run_settings.grid(row=2, column=0, sticky="ew")
        run_settings.columnconfigure(1, weight=1)
        ttk.Label(run_settings, text="运行方式", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._field(run_settings, "模式", 1)
        self.mode_box = ttk.Combobox(run_settings, textvariable=self.mode_var, values=tuple(MODE_LABELS), state="readonly")
        self.mode_box.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
        self.mode_box.bind("<<ComboboxSelected>>", lambda _event: self._update_mode_fields())
        self._field(run_settings, "回放 Manifest", 2)
        self.manifest_entry = ttk.Entry(run_settings, textvariable=self.manifest_var)
        self.manifest_entry.grid(row=2, column=1, sticky="ew", pady=4)
        self.manifest_button = ttk.Button(run_settings, text="选择文件", style="Quiet.TButton", command=lambda: self._choose_file(self.manifest_var, (("JSON", "*.json"),)))
        self.manifest_button.grid(row=2, column=2, padx=(8, 0))
        self._field(run_settings, "Mock 场景", 3)
        self.mock_box = ttk.Combobox(run_settings, textvariable=self.mock_var, values=MOCK_SCENARIOS, state="readonly")
        self.mock_box.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
        self._field(run_settings, "输出目录", 4)
        self.output_entry = ttk.Entry(run_settings, textvariable=self.output_var)
        self.output_entry.grid(row=4, column=1, sticky="ew", pady=4)
        self.output_button = ttk.Button(run_settings, text="选择目录", style="Quiet.TButton", command=self._choose_output_dir)
        self.output_button.grid(row=4, column=2, padx=(8, 0))
        self.recompute_check = ttk.Checkbutton(run_settings, text="回放时按当前规则重算 Step Gate", variable=self.recompute_var)
        self.recompute_check.grid(row=5, column=1, columnspan=2, sticky="w", pady=(5, 0))

        connection = ttk.Frame(content, style="Surface.TFrame", padding=16)
        connection.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="设备与模型连接", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._field(connection, "平台 / 序列号", 1)
        target_row = ttk.Frame(connection, style="Surface.TFrame")
        target_row.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
        target_row.columnconfigure(1, weight=1)
        self.device_box = ttk.Combobox(target_row, textvariable=self.device_var, values=("Harmony", "Android"), state="readonly", width=12)
        self.device_box.grid(row=0, column=0)
        self.device_box.bind("<<ComboboxSelected>>", lambda _event: self._check_device_environment())
        self.serial_entry = ttk.Entry(target_row, textvariable=self.serial_var)
        self.serial_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._field(connection, "服务 / 模型", 2)
        model_row = ttk.Frame(connection, style="Surface.TFrame")
        model_row.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        model_row.columnconfigure(0, weight=2)
        model_row.columnconfigure(1, weight=1)
        self.model_url_entry = ttk.Entry(model_row, textvariable=self.model_base_url_var)
        self.model_url_entry.grid(row=0, column=0, sticky="ew")
        self.model_name_entry = ttk.Entry(model_row, textvariable=self.model_name_var)
        self.model_name_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._field(connection, "凭据文件", 3)
        self.api_key_entry = ttk.Entry(connection, textvariable=self.api_key_file_var)
        self.api_key_entry.grid(row=3, column=1, sticky="ew", pady=4)
        self.api_key_button = ttk.Button(connection, text="选择文件", style="Quiet.TButton", command=lambda: self._choose_file(self.api_key_file_var, (("凭据文件", "*.*"),)))
        self.api_key_button.grid(row=3, column=2, padx=(8, 0))
        self._field(connection, "协议 / 推理", 4)
        protocol_row = ttk.Frame(connection, style="Surface.TFrame")
        protocol_row.grid(row=4, column=1, columnspan=2, sticky="ew", pady=4)
        protocol_row.columnconfigure(0, weight=1)
        protocol_row.columnconfigure(1, weight=1)
        self.wire_api_box = ttk.Combobox(protocol_row, textvariable=self.wire_api_var, values=("responses", "chat_completions"), state="readonly")
        self.wire_api_box.grid(row=0, column=0, sticky="ew")
        self.reasoning_box = ttk.Combobox(protocol_row, textvariable=self.reasoning_effort_var, values=("low", "medium", "high", "xhigh"), state="readonly")
        self.reasoning_box.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._field(connection, "Runner 根目录", 5)
        self.runner_root_entry = ttk.Entry(connection, textvariable=self.runner_root_var)
        self.runner_root_entry.grid(row=5, column=1, sticky="ew", pady=4)
        self.runner_root_button = ttk.Button(connection, text="选择目录", style="Quiet.TButton", command=lambda: self._choose_directory(self.runner_root_var))
        self.runner_root_button.grid(row=5, column=2, padx=(8, 0))
        ttk.Button(content, text="完成", style="Primary.TButton", command=self._close_advanced_settings).grid(row=4, column=0, sticky="e", pady=(16, 0))

    def _build_case_details_window(self) -> None:
        self.case_details_window = tk.Toplevel(self)
        self.case_details_window.withdraw()
        self.case_details_window.title("测试用例详情")
        self.case_details_window.geometry("760x610")
        self.case_details_window.minsize(620, 480)
        self.case_details_window.configure(background="#F4F6F8")
        self.case_details_window.transient(self)
        self.case_details_window.protocol("WM_DELETE_WINDOW", self._close_case_details)
        content = ttk.Frame(self.case_details_window, style="Workspace.TFrame", padding=22)
        content.pack(fill="both", expand=True)
        content.rowconfigure(4, weight=1)
        content.columnconfigure(0, weight=1)
        ttk.Label(content, text="测试用例详情", style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(content, textvariable=self.case_title_var, style="Section.TLabel", wraplength=700).grid(row=1, column=0, sticky="w", pady=(14, 2))
        ttk.Label(content, textvariable=self.case_detail_meta_var, style="Muted.TLabel", wraplength=700).grid(row=2, column=0, sticky="w")
        ttk.Label(content, textvariable=self.case_policy_var, style="Muted.TLabel", wraplength=700).grid(row=3, column=0, sticky="w", pady=(7, 12))
        steps = ttk.Frame(content, style="Surface.TFrame", padding=12)
        steps.grid(row=4, column=0, sticky="nsew")
        steps.rowconfigure(1, weight=1)
        steps.columnconfigure(0, weight=1)
        ttk.Label(steps, text="计划步骤", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.steps_tree = ttk.Treeview(steps, columns=("instruction",), show="tree headings", height=12)
        self.steps_tree.heading("#0", text="步骤")
        self.steps_tree.heading("instruction", text="操作说明")
        self.steps_tree.column("#0", width=120, stretch=False)
        self.steps_tree.column("instruction", width=520)
        self.steps_tree.grid(row=1, column=0, sticky="nsew")
        ttk.Button(content, text="关闭", style="Secondary.TButton", command=self._close_case_details).grid(row=5, column=0, sticky="e", pady=(14, 0))

    def _build_live_tab(self) -> None:
        self.live_tab.rowconfigure(1, weight=1)
        self.live_tab.columnconfigure(0, weight=1)
        status = ttk.Frame(self.live_tab, style="Surface.TFrame", padding=22)
        status.grid(row=0, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        ttk.Label(status, text="评测正在进行", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(status, text="客户端会依次完成设备准备、业务操作、结果核验和报告生成。", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 14))
        self.run_progress = ttk.Progressbar(status, mode="indeterminate")
        self.run_progress.grid(row=2, column=0, sticky="ew", pady=(0, 11))
        ttk.Label(status, textvariable=self.status_detail_var, style="Muted.TLabel", wraplength=980).grid(row=3, column=0, sticky="w")
        self.live_technical_button = ttk.Button(status, text="查看技术详情", style="Quiet.TButton", command=self._toggle_live_technical)
        self.live_technical_button.grid(row=4, column=0, sticky="w", pady=(14, 0))

        self.live_technical_panel = ttk.Frame(self.live_tab, style="App.TFrame")
        self.live_technical_panel.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self.live_technical_panel.rowconfigure(0, weight=1)
        self.live_technical_panel.columnconfigure(0, weight=1)
        pane = ttk.Panedwindow(self.live_technical_panel, orient="horizontal")
        pane.grid(row=0, column=0, sticky="nsew")
        events = ttk.Frame(pane, style="Surface.TFrame", padding=12)
        detail = ttk.Frame(pane, style="Surface.TFrame", padding=12)
        pane.add(events, weight=2)
        pane.add(detail, weight=3)
        events.rowconfigure(1, weight=1)
        events.columnconfigure(0, weight=1)
        ttk.Label(events, text="模型事件", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.event_tree = ttk.Treeview(events, columns=("role", "step", "event"), show="headings")
        for column, title, width in (("role", "角色", 90), ("step", "步骤", 140), ("event", "状态", 180)):
            self.event_tree.heading(column, text=title)
            self.event_tree.column(column, width=width, stretch=column != "role")
        self.event_tree.grid(row=1, column=0, sticky="nsew")
        detail.rowconfigure(1, weight=1)
        detail.columnconfigure(0, weight=1)
        ttk.Label(detail, text="实时日志", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.log = tk.Text(detail, wrap="word", relief="flat", background="#F5F7F8", foreground="#26343D", font=("Consolas", 10), padx=12, pady=10, state="disabled")
        self.log.grid(row=1, column=0, sticky="nsew")
        self._set_live_technical_visible(False)

    def _build_result_tab(self) -> None:
        self.result_tab.rowconfigure(1, weight=1)
        self.result_tab.columnconfigure(0, weight=1)
        headline = ttk.Frame(self.result_tab, style="Surface.TFrame", padding=22)
        headline.grid(row=0, column=0, sticky="ew")
        headline.columnconfigure(1, weight=1)
        ttk.Label(headline, text="评测结论", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.result_label = tk.Label(headline, textvariable=self.result_var, background="#FFFFFF", foreground="#58636E", font=("Microsoft YaHei UI", 24, "bold"), anchor="e")
        self.result_label.grid(row=0, column=1, sticky="e")
        ttk.Label(headline, textvariable=self.result_reason_var, style="Muted.TLabel", wraplength=1050).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.result_details_button = ttk.Button(headline, text="查看详细结果", style="Quiet.TButton", command=self._toggle_result_details)
        self.result_details_button.grid(row=2, column=0, sticky="w", pady=(14, 0))

        self.result_details_panel = ttk.Frame(self.result_tab, style="App.TFrame")
        self.result_details_panel.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self.result_details_panel.rowconfigure(1, weight=1)
        self.result_details_panel.columnconfigure(0, weight=1)
        metrics = ttk.Frame(self.result_details_panel, style="Surface.TFrame", padding=(18, 16, 18, 16))
        metrics.grid(row=0, column=0, sticky="ew")
        for index in range(4):
            metrics.columnconfigure(index, weight=1)
        for index, variable in enumerate((self.result_execution_var, self.result_behavior_var, self.result_attribution_var, self.result_verification_var)):
            ttk.Label(metrics, textvariable=variable, style="Metric.TLabel", anchor="center").grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 5, 0))
        pane = ttk.Panedwindow(self.result_details_panel, orient="horizontal")
        pane.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        assertions = ttk.Frame(pane, style="Surface.TFrame", padding=12)
        evidence = ttk.Frame(pane, style="Surface.TFrame", padding=12)
        pane.add(assertions, weight=3)
        pane.add(evidence, weight=2)
        assertions.rowconfigure(1, weight=1)
        assertions.columnconfigure(0, weight=1)
        ttk.Label(assertions, text="应用断言", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.assertion_tree = ttk.Treeview(assertions, columns=("status", "reason"), show="tree headings")
        self.assertion_tree.heading("#0", text="断言")
        self.assertion_tree.heading("status", text="状态")
        self.assertion_tree.heading("reason", text="原因")
        self.assertion_tree.column("#0", width=190)
        self.assertion_tree.column("status", width=110, stretch=False)
        self.assertion_tree.column("reason", width=380)
        self.assertion_tree.grid(row=1, column=0, sticky="nsew")
        evidence.rowconfigure(1, weight=1)
        evidence.columnconfigure(0, weight=1)
        ttk.Label(evidence, text="证据文件", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.artifact_tree = ttk.Treeview(evidence, columns=("path",), show="tree headings")
        self.artifact_tree.heading("#0", text="类型")
        self.artifact_tree.heading("path", text="路径")
        self.artifact_tree.column("#0", width=150)
        self.artifact_tree.column("path", width=280)
        self.artifact_tree.grid(row=1, column=0, sticky="nsew")
        self.artifact_tree.bind("<Double-1>", lambda _event: self._open_selected_artifact())
        self._set_result_details_visible(False)

    @staticmethod
    def _field(parent: ttk.Frame, text: str, row: int) -> ttk.Label:
        label = ttk.Label(parent, text=text, style="Field.TLabel")
        label.grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        return label

    def _open_advanced_settings(self) -> None:
        self.advanced_window.deiconify()
        self.advanced_window.lift()
        self.advanced_window.focus_set()

    def _close_advanced_settings(self) -> None:
        self.advanced_window.withdraw()

    def _open_case_details(self) -> None:
        self.case_details_window.deiconify()
        self.case_details_window.lift()
        self.case_details_window.focus_set()

    def _close_case_details(self) -> None:
        self.case_details_window.withdraw()

    def _toggle_live_technical(self) -> None:
        self._set_live_technical_visible(not self._live_technical_visible)

    def _set_live_technical_visible(self, visible: bool) -> None:
        self._live_technical_visible = bool(visible)
        if visible:
            self.live_technical_panel.grid()
        else:
            self.live_technical_panel.grid_remove()
        self.live_technical_button.configure(text="收起技术详情" if visible else "查看技术详情")

    def _toggle_result_details(self) -> None:
        self._set_result_details_visible(not self._result_details_visible)

    def _set_result_details_visible(self, visible: bool) -> None:
        self._result_details_visible = bool(visible)
        if visible:
            self.result_details_panel.grid()
        else:
            self.result_details_panel.grid_remove()
        self.result_details_button.configure(text="收起详细结果" if visible else "查看详细结果")

    def _schedule_case_summary(self) -> None:
        if self._case_refresh_job is not None:
            self.after_cancel(self._case_refresh_job)
        self._case_refresh_job = self.after(180, self._refresh_case_summary)

    def _refresh_case_summary(self) -> None:
        self._case_refresh_job = None
        for item in self.steps_tree.get_children():
            self.steps_tree.delete(item)
        try:
            summary = load_test_case_summary(Path(self.case_var.get().strip()))
        except Exception as exc:  # noqa: BLE001 - invalid drafts are shown to the operator.
            self.case_title_var.set("用例不可用")
            self.case_meta_var.set("请选择一个可读取的 JSON 测试用例。")
            self.case_detail_meta_var.set(str(exc))
            self.case_impact_var.set("当前无法判断此用例是否会产生业务操作。")
            self.case_risk_var.set("需要处理：用例文件不可用")
            self.case_policy_var.set("")
            self.risk_label.configure(style="Warn.TLabel")
            return
        self.case_title_var.set(f"{summary.app_name} · {summary.feature or '未命名功能'}")
        package = summary.package or "未指定包名"
        self.case_meta_var.set(f"已载入 · {summary.step_count} 个操作步骤")
        self.case_detail_meta_var.set(f"用例编号：{summary.test_case_id}\n应用标识：{package}")
        if summary.mutates_device:
            self.case_impact_var.set("评测会在应用中执行真实操作，可能发送、发布或修改业务数据。")
            self.case_risk_var.set("需要确认：此用例可能产生真实业务操作")
        else:
            self.case_impact_var.set("该用例不包含发布、发送、删除等真实业务操作。")
            self.case_risk_var.set("低风险：不会改变应用业务数据")
        if summary.verification_step_count:
            self.case_policy_var.set(f"完成主要操作后，将执行 {summary.verification_step_count} 个只读核验步骤以确认结果。")
        else:
            self.case_policy_var.set("完成主要操作后，会根据用例定义生成评测结论。")
        self.risk_label.configure(style="Warn.TLabel" if summary.mutates_device else "Safe.TLabel")
        for step_id, _action_type, instruction in summary.steps:
            self.steps_tree.insert("", "end", text=step_id, values=(instruction,))

    def _update_mode_fields(self) -> None:
        mode = MODE_LABELS[self.mode_var.get()]
        manifest = mode == PcEvaluationMode.MANIFEST_REPLAY
        mock = mode == PcEvaluationMode.MOCK
        execution = mode == PcEvaluationMode.DEVICE_EXECUTION
        device_mode = mode in {PcEvaluationMode.DEVICE_EXECUTION, PcEvaluationMode.DEVICE_PREFLIGHT}
        self.manifest_entry.configure(state="normal" if manifest else "disabled")
        self.manifest_button.configure(state="normal" if manifest else "disabled")
        self.mock_box.configure(state="readonly" if mock else "disabled")
        self.output_entry.configure(state="normal")
        self.output_button.configure(state="normal")
        for widget, active_state in ((self.device_box, "readonly"), (self.serial_entry, "normal"), (self.detected_box, "readonly"), (self.refresh_devices_button, "normal")):
            widget.configure(state=active_state if device_mode else "disabled")
        for widget, active_state in ((self.model_url_entry, "normal"), (self.model_name_entry, "normal"), (self.api_key_entry, "normal"), (self.api_key_button, "normal"), (self.wire_api_box, "readonly"), (self.reasoning_box, "readonly"), (self.runner_root_entry, "normal"), (self.runner_root_button, "normal")):
            widget.configure(state=active_state if execution else "disabled")
        self.recompute_check.configure(state="normal" if manifest else "disabled")
        self.confirm_check.configure(state="normal" if execution else "disabled")
        if not execution:
            self.confirm_mutation_var.set(False)
            if not device_mode:
                self.environment_status_var.set("此模式不会连接或操作设备")
                self.environment_label.configure(style="Safe.TLabel")
        else:
            self._check_device_environment()
        if device_mode and not execution:
            self._check_device_environment()
        run_labels = {
            PcEvaluationMode.MANIFEST_REPLAY: "开始回放",
            PcEvaluationMode.MOCK: "运行自检",
            PcEvaluationMode.DEVICE_PREFLIGHT: "生成预检包",
            PcEvaluationMode.DEVICE_EXECUTION: "开始真机评测",
        }
        self.run_button.configure(text=run_labels[mode])

    def _refresh_devices(self) -> None:
        if self._running:
            return
        self.refresh_devices_button.configure(state="disabled")
        self.device_status_var.set("正在读取 hdc / adb 已连接设备列表...")
        threading.Thread(target=lambda: self._events.put(("devices", discover_connected_devices())), daemon=True).start()

    def _finish_device_discovery(self, result: DeviceDiscoveryResult) -> None:
        self._devices_by_label = {item.label: item for item in result.devices}
        labels = tuple(self._devices_by_label)
        self.detected_box.configure(values=labels)
        if MODE_LABELS[self.mode_var.get()] == PcEvaluationMode.DEVICE_EXECUTION:
            self.refresh_devices_button.configure(state="normal")
        if labels:
            selected = self.device_target_var.get()
            self.device_target_var.set(selected if selected in self._devices_by_label else labels[0])
            self._select_detected_device()
            self.device_status_var.set(f"已检测到 {len(labels)} 台在线设备；可手动覆盖序列号。")
        else:
            self.device_target_var.set("未检测到在线设备")
            detail = "；".join(result.diagnostics) or "请检查 USB 调试与设备工具链"
            self.device_status_var.set(detail)

    def _select_detected_device(self) -> None:
        selected = self._devices_by_label.get(self.device_target_var.get())
        if selected is not None:
            self.device_var.set(selected.platform)
            self.serial_var.set(selected.serial)
            self._check_device_environment()

    def _check_device_environment(self) -> bool:
        profile = "harmony" if self.device_var.get() == "Harmony" else "android"
        report = check_environment(profile)
        if report.ready:
            self.environment_status_var.set(f"{self.device_var.get()} 运行环境已就绪")
            self.environment_label.configure(style="Safe.TLabel")
            return True
        self.environment_status_var.set("缺少：" + "、".join(report.missing))
        self.environment_label.configure(style="Warn.TLabel")
        return False

    def _choose_file(self, variable: tk.StringVar, filetypes: tuple[tuple[str, str], ...]) -> None:
        selected = filedialog.askopenfilename(initialdir=BUNDLE_ROOT, filetypes=filetypes)
        if selected:
            variable.set(selected)

    def _choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=Path(self.output_var.get()).parent)
        if selected:
            self.output_var.set(selected)

    def _choose_directory(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(initialdir=variable.get() or BUNDLE_ROOT)
        if selected:
            variable.set(selected)

    def _start_run(self) -> None:
        if self._running:
            return
        mode = MODE_LABELS[self.mode_var.get()]
        serial = self.serial_var.get().strip()
        if mode == PcEvaluationMode.DEVICE_EXECUTION:
            if not serial:
                messagebox.showwarning("需要设备", "请选择在线设备或手动填写设备序列号。")
                return
            if not self._check_device_environment():
                messagebox.showerror("运行环境未就绪", self.environment_status_var.get())
                return
            key_file = self.api_key_file_var.get().strip()
            if key_file and not Path(key_file).is_file():
                messagebox.showerror("凭据文件不可用", "选择的模型凭据文件不存在或不可访问。")
                return
            if not self.confirm_mutation_var.get():
                messagebox.showwarning("需要确认", "请先确认所选设备与用例可能产生的业务副作用。")
                return
            try:
                summary = load_test_case_summary(Path(self.case_var.get().strip()))
                risk = summary.risk_level
                target = f"{summary.app_name} ({summary.test_case_id})"
            except Exception:
                risk, target = "UNKNOWN", self.case_var.get().strip()
            if not messagebox.askyesno("确认真机执行", f"设备：{self.device_var.get()} · {serial}\n用例：{target}\n风险：{risk}\n\n评测会实际点击和输入，可能发布、发送或修改业务数据。是否继续？", icon="warning"):
                return
        request = PcEvaluationRequest(
            mode=mode,
            test_case_path=Path(self.case_var.get().strip()),
            output_dir=Path(self.output_var.get().strip()),
            manifest_path=Path(self.manifest_var.get().strip()) if self.manifest_var.get().strip() else None,
            recompute_step_gates=self.recompute_var.get(),
            mock_scenario=self.mock_var.get(),
            device=self.device_var.get(),
            device_serial=serial,
            runner_root=Path(self.runner_root_var.get()),
            device_mutation_confirmation=DEVICE_MUTATION_CONFIRMATION if self.confirm_mutation_var.get() else None,
            model_event_sink=self._queue_model_event if mode == PcEvaluationMode.DEVICE_EXECUTION else None,
            runtime_environment=self._runtime_environment() if mode == PcEvaluationMode.DEVICE_EXECUTION else None,
        )
        self._running = True
        self.run_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.folder_button.configure(state="disabled")
        self._set_run_status("RUNNING", "评测运行中，请勿关闭窗口或断开目标设备")
        self.result_var.set("RUNNING")
        self.result_label.configure(foreground="#087E8B")
        self._clear_runtime_views()
        self._set_live_technical_visible(False)
        self._set_result_details_visible(False)
        self.run_progress.start(12)
        self._append_log(f"mode={mode}\ntest_case={request.test_case_path}\noutput={request.output_dir}\n")
        self._show_view("live")
        threading.Thread(target=self._run_worker, args=(request,), daemon=True).start()

    def _clear_runtime_views(self) -> None:
        for tree in (self.event_tree, self.assertion_tree, self.artifact_tree):
            for item in tree.get_children():
                tree.delete(item)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

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
                    self._append_model_event(payload)
                elif kind == "devices" and isinstance(payload, DeviceDiscoveryResult):
                    self._finish_device_discovery(payload)
                elif kind == "result":
                    self._finish_success(payload)
                else:
                    self._finish_error(payload)
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def _append_model_event(self, event: Mapping[str, object]) -> None:
        self.event_tree.insert("", "end", values=(event.get("role") or "Model", event.get("step_id") or "-", event.get("event_type") or "MODEL_EVENT"))
        self.event_tree.yview_moveto(1)
        self._append_log(format_model_event_for_display(event))

    def _runtime_environment(self) -> Mapping[str, str] | None:
        values = {
            "MOBIAGENT_BASE_URL": self.model_base_url_var.get().strip(),
            "MOBIAGENT_MODEL": self.model_name_var.get().strip(),
            "MOBIAGENT_API_KEY_FILE": self.api_key_file_var.get().strip(),
            "MOBIAGENT_WIRE_API": self.wire_api_var.get().strip(),
            "MOBIAGENT_REASONING_EFFORT": self.reasoning_effort_var.get().strip(),
            "MOBIAGENT_DISABLE_RESPONSE_STORAGE": "true",
        }
        return {name: value for name, value in values.items() if value} or None

    def _finish_success(self, result: object) -> None:
        payload = result.as_dict()  # type: ignore[attr-defined]
        self._running = False
        self.run_progress.stop()
        self.run_button.configure(state="normal")
        self._last_output = Path(payload["output_dir"])
        self.folder_button.configure(state="normal")
        verdict = str(payload.get("overall_result") or payload["status"])
        self.result_var.set(verdict)
        self.result_label.configure(foreground=RESULT_COLORS.get(verdict, "#087E8B"))
        self._set_run_status("COMPLETE", "运行完成，结果与证据已写入输出目录")
        report_summary = load_report_summary(self._last_output)
        if report_summary is not None:
            self.open_button.configure(state="normal")
            self._show_report_summary(report_summary)
            self._show_view("result")
        else:
            summary = payload.get("summary") or {}
            self.result_reason_var.set(str(summary.get("safety") or "预检产物已生成；未连接或操作设备。"))
            self._show_view("result")
        self._append_log(json.dumps(payload, ensure_ascii=False, indent=2))
        self.output_var.set(self._new_output_dir())
        self.confirm_mutation_var.set(False)

    def _show_report_summary(self, summary: ReportSummary) -> None:
        self.result_execution_var.set(f"执行：{summary.execution_status} ({summary.completed_steps}/{summary.step_count})")
        self.result_behavior_var.set(f"应用断言：{summary.app_behavior_status}")
        self.result_attribution_var.set(f"归因：{summary.attribution}")
        self.result_verification_var.set(f"只读核验：{summary.verification_status}")
        self.result_reason_var.set(summary.reason or "报告未提供归因原因。")
        for assertion_id, status, reason in summary.assertions:
            self.assertion_tree.insert("", "end", text=assertion_id, values=(status, reason))
        for name, path in summary.artifacts:
            self.artifact_tree.insert("", "end", text=name, values=(path,))

    def _finish_error(self, error: object) -> None:
        self._running = False
        self.run_progress.stop()
        self.run_button.configure(state="normal")
        candidate = Path(self.output_var.get().strip())
        if candidate.exists():
            self._last_output = candidate
            self.folder_button.configure(state="normal")
        self._set_run_status("ERROR", "运行失败，请检查配置与实时日志")
        self.result_var.set("ERROR")
        self.result_label.configure(foreground="#C23B32")
        rendered = f"{type(error).__name__}: {error}"
        self._append_log(rendered)
        messagebox.showerror("评测失败", rendered)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_report(self) -> None:
        if self._last_output is not None:
            self._open_path(self._last_output / "report.md")

    def _open_output(self) -> None:
        if self._last_output is not None:
            self._open_path(self._last_output)

    def _open_selected_artifact(self) -> None:
        if self._last_output is None:
            return
        selected = self.artifact_tree.selection()
        if selected:
            value = self.artifact_tree.item(selected[0], "values")
            if value:
                self._open_path(self._last_output / str(value[0]))

    @staticmethod
    def _open_path(target: Path) -> None:
        if not target.exists():
            messagebox.showerror("无法打开", f"文件不存在：{target}")
            return
        try:
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                webbrowser.open(target.resolve().as_uri())
        except OSError as exc:
            messagebox.showerror("无法打开", str(exc))


def main() -> int:
    app = MobiAgentPcApp()
    app.mainloop()
    return 0


__all__ = ["MobiAgentPcApp", "main"]
