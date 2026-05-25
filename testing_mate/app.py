from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from typing import Callable

from testing_mate.constants import *
from testing_mate.models import CoverageFeedback, CoverageGap, Condition, FunctionInfo, Parameter, RunResult, TestCase
from testing_mate.c_backend import *
from testing_mate.generator import TestBackendGenerator, TestBackendRunner, TestMockFallback, TestOracleFallback, backend_harness_path, format_run_result
from testing_mate.boolean_expression import BooleanExpressionEvaluator
from testing_mate.clang_analyzer import ClangEnhancedAnalyzer
from testing_mate.code_database import CodeDatabase
from testing_mate.artifacts_writer import *
from testing_mate.reporter import *
from testing_mate.vector_generator import TestVectorGenerator
from testing_mate.llm import refine_with_ollama
from testing_mate.coverage_gap import *
from testing_mate.c1_engine import *
from testing_mate.utilities import *
from testing_mate.toolchain import DEFAULT_MINGW_ROOT, configure_mingw_root, configured_mingw_root, mingw_toolchain_available


class TestingMateApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1360x860")
        self.minsize(1120, 720)
        self.configure(bg="#f4f7fb")
        self.sources: list[Path] = []
        self.generated_paths: list[Path] = []
        self.report_path: Path | None = None
        self.last_functions: list[FunctionInfo] = []
        self.last_cases: list[TestCase] = []
        self.last_feedback: CoverageFeedback | None = None
        self._case_by_iid: dict[str, TestCase] = {}
        self._build_style()
        self._build_ui()

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.option_add("*Font", "{Segoe UI} 10")
        self.option_add("*Listbox.Font", "{Segoe UI} 9")
        style.configure("TFrame", background="#f4f7fb")
        style.configure("Panel.TFrame", background="#ffffff", relief="flat")
        style.configure("Surface.TFrame", background="#f8fafc", relief="flat")
        style.configure("Metric.TFrame", background="#f8fafc", relief="flat")
        style.configure("TLabel", background="#f4f7fb", foreground="#172033", font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background="#ffffff", foreground="#172033", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#64748b", font=("Segoe UI", 9))
        style.configure("Title.TLabel", background="#f4f7fb", foreground="#0f172a", font=("Segoe UI Semibold", 24))
        style.configure("Subtle.TLabel", background="#f4f7fb", foreground="#64748b", font=("Segoe UI", 10))
        style.configure("Status.TLabel", background="#e8f2ff", foreground="#1d4ed8", font=("Segoe UI Semibold", 9), padding=(10, 5))
        style.configure("PanelTitle.TLabel", background="#ffffff", foreground="#0f172a", font=("Segoe UI Semibold", 12))
        style.configure("Section.TLabel", background="#ffffff", foreground="#334155", font=("Segoe UI Semibold", 10))
        style.configure("MetricLabel.TLabel", background="#f8fafc", foreground="#64748b", font=("Segoe UI", 9))
        style.configure("MetricValue.TLabel", background="#f8fafc", foreground="#0f172a", font=("Segoe UI Semibold", 17))
        style.configure("TEntry", padding=(8, 6), fieldbackground="#ffffff", bordercolor="#cbd5e1", lightcolor="#cbd5e1", darkcolor="#cbd5e1")
        style.configure("TCombobox", padding=(8, 6), fieldbackground="#ffffff", bordercolor="#cbd5e1", arrowsize=14)
        style.configure("TButton", font=("Segoe UI Semibold", 10), padding=(12, 8), borderwidth=1)
        style.configure("Accent.TButton", background="#1f6feb", foreground="#ffffff", bordercolor="#1f6feb")
        style.map("Accent.TButton", background=[("active", "#1d4ed8"), ("pressed", "#1e40af")], foreground=[("disabled", "#dbeafe")])
        style.configure("Secondary.TButton", background="#f8fafc", foreground="#172033", bordercolor="#cbd5e1")
        style.map("Secondary.TButton", background=[("active", "#eef2f7"), ("pressed", "#e2e8f0")])
        style.configure("Danger.TButton", background="#fff1f2", foreground="#9f1239", bordercolor="#fecdd3")
        style.map("Danger.TButton", background=[("active", "#ffe4e6"), ("pressed", "#fecdd3")])
        style.configure("TCheckbutton", background="#ffffff", foreground="#172033", font=("Segoe UI", 10))
        style.configure("TRadiobutton", background="#ffffff", foreground="#172033", font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=30, fieldbackground="#ffffff", background="#ffffff", foreground="#172033", font=("Segoe UI", 10), bordercolor="#dbe3ef", borderwidth=1)
        style.configure("Treeview.Heading", background="#eef2f7", foreground="#334155", font=("Segoe UI Semibold", 10), padding=(8, 7))
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#0f172a")])

    def _create_scrollable_panel(self, parent: ttk.Frame) -> ttk.Frame:
        shell = ttk.Frame(parent, style="Panel.TFrame")
        shell.grid(row=0, column=0, sticky="nsw", padx=(0, 18))
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)

        canvas = tk.Canvas(shell, width=398, bg="#ffffff", bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        panel = ttk.Frame(canvas, style="Panel.TFrame", padding=18)
        window_id = canvas.create_window((0, 0), window=panel, anchor="nw")

        def resize_scroll_region(_event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def resize_panel(event: tk.Event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        def bind_mousewheel(_event: tk.Event) -> None:
            canvas.bind_all("<MouseWheel>", on_mousewheel)

        def unbind_mousewheel(_event: tk.Event) -> None:
            canvas.unbind_all("<MouseWheel>")

        def on_mousewheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        panel.bind("<Configure>", resize_scroll_region)
        canvas.bind("<Configure>", resize_panel)
        canvas.bind("<Enter>", bind_mousewheel)
        canvas.bind("<Leave>", unbind_mousewheel)
        return panel

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=24)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 18))
        header.columnconfigure(0, weight=1)
        title_area = ttk.Frame(header)
        title_area.grid(row=0, column=0, sticky="w")
        ttk.Label(title_area, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_area,
            text="Clang-first C test generation for GoogleTest and CppUTest",
            style="Subtle.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        status_area = ttk.Frame(header)
        status_area.grid(row=0, column=1, sticky="e")
        ttk.Label(status_area, text="Clang DB", style="Status.TLabel").pack(side="left", padx=(0, 8))
        ttk.Label(status_area, text="GTest / CppUTest", style="Status.TLabel").pack(side="left")

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = self._create_scrollable_panel(body)
        right = ttk.Frame(body, style="Panel.TFrame", padding=18)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(2, weight=2)
        right.rowconfigure(4, weight=1)
        right.columnconfigure(0, weight=1)
        self.output_dir = tk.StringVar(value=str(Path.cwd() / "TestMate_out"))
        
        ttk.Label(left, text="Project Inputs", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        buttons = ttk.Frame(left, style="Panel.TFrame")
        buttons.pack(fill="x", pady=(0, 10), anchor="w")
        self.add_source_button = ttk.Button(buttons, text="Add Source", command=self._show_add_source_menu, style="Accent.TButton")
        self.add_source_button.pack(side="left", fill="x", expand=True)
        ttk.Button(buttons, text="Clear", command=self._clear_sources, style="Danger.TButton").pack(side="right", fill="x", expand=True, padx=(8, 0))
        compile_row = ttk.Frame(left, style="Panel.TFrame")
        compile_row.pack(fill="x", pady=(0, 10))
        self.compile_commands = tk.StringVar()
        ttk.Entry(compile_row, textvariable=self.compile_commands).pack(side="left", fill="x", expand=True)
        ttk.Button(compile_row, text="Compile DB", command=self._choose_compile_commands, style="Secondary.TButton").pack(side="left", padx=(8, 0))
        action_row = ttk.Frame(left, style="Panel.TFrame")
        action_row.pack(fill="x", pady=(8, 0))
        ttk.Button(action_row, text="Generate", command=self._run_async, style="Accent.TButton").pack(side="left", fill="x", expand=True)
        ttk.Button(action_row, text="ReTest", command=self._rerun_async, style="Danger.TButton").pack(side="left", fill="x", expand=True, padx=(8, 0))
        ttk.Button(action_row, text="View Report", command=self._open_report_file, style="Secondary.TButton").pack(side="right", padx=(8, 0))

        gap_action_row = ttk.Frame(left, style="Panel.TFrame")
        gap_action_row.pack(fill="x", pady=(8, 0))
        self.add_gap_cases_button = ttk.Button(
            gap_action_row,
            text="Add Gap Cases",
            command=self._add_uncovered_cases_async,
            style="Accent.TButton",
        )
        self.add_gap_cases_button.pack(side="left", fill="x", expand=True)
        ttk.Label(
            gap_action_row,
            text="Select an uncovered row to target one line, or no row for all gaps.",
            style="Muted.TLabel",
            wraplength=220,
        ).pack(side="left", padx=(8, 0))

        self.source_list = tk.Listbox(
            left,
            height=6,
            width=34,
            bd=0,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#dbe3ef",
            highlightcolor="#93c5fd",
            selectbackground="#dbeafe",
            selectforeground="#0f172a",
            background="#f8fafc",
            foreground="#172033",
            font=("Segoe UI", 9),
        )
        self.source_list.pack(fill="x", pady=(10, 16))

        coverage_title = ttk.Frame(left, style="Panel.TFrame")
        coverage_title.pack(fill="x", pady=(0, 8))
        ttk.Label(coverage_title, text="Coverage & Level", style="Section.TLabel").pack(side="left")
        ttk.Label(coverage_title, text="Retry", style="Muted.TLabel").pack(side="right", padx=(8, 0))
        self.mock_retry_limit = tk.StringVar(value="5")
        ttk.Entry(coverage_title, textvariable=self.mock_retry_limit, width=4).pack(side="right")
        self.coverage = tk.StringVar(value="MC/DC")
        Coverages = ttk.Frame(left, style="Panel.TFrame")
        Coverages.pack(fill="x", pady=(0, 4))
        for text, value in (("C0/C1", "C0/C1"), ("MCDC", "MC/DC")):
            ttk.Radiobutton(Coverages, text=text, variable=self.coverage, value=value).pack(side="left", padx=(0, 20))

        self.level = tk.StringVar(value="UT")
        levels = ttk.Frame(left, style="Panel.TFrame")
        levels.pack(fill="x", pady=(2, 0))
        for text in ("UT", "IT", "ST"):
            ttk.Radiobutton(levels, text=text, variable=self.level, value=text).pack(side="left", padx=(0, 20))
        function_filter_row = ttk.Frame(left, style="Panel.TFrame")
        function_filter_row.pack(fill="x", pady=(6, 0))
        self.include_static_functions = tk.BooleanVar(value=False)
        self.include_main_function = tk.BooleanVar(value=False)
        ttk.Checkbutton(function_filter_row, text="Include static functions", variable=self.include_static_functions).pack(anchor="w")
        ttk.Checkbutton(function_filter_row, text="Include main()", variable=self.include_main_function).pack(anchor="w")
        iso_row = ttk.Frame(left, style="Panel.TFrame")
        iso_row.pack(fill="x", pady=(6, 0))
        self.iso26262_profile = tk.BooleanVar(value=False)
        ttk.Checkbutton(iso_row, text="ISO 26262 method profile", variable=self.iso26262_profile).pack(side="left")
        self.asil_level = tk.StringVar(value="ASIL D")
        asil_box = ttk.Combobox(iso_row, textvariable=self.asil_level, values=("QM", "ASIL A", "ASIL B", "ASIL C", "ASIL D"), state="readonly", width=8)
        asil_box.pack(side="left", padx=(8, 0))

        ttk.Label(left, text="Build Context", style="Section.TLabel").pack(anchor="w", pady=(14, 8))
        self.compiler = tk.StringVar(value=DEFAULT_COMPILER)
        compilerFR = ttk.Frame(left, style="Panel.TFrame")
        compilerFR.pack(fill="x")
        ttk.Entry(compilerFR, textvariable=self.compiler).pack(side="left", fill="x", expand=True)

        self.preprocess = tk.BooleanVar(value=True)
        ttk.Checkbutton(compilerFR, text="Preprocess source first", variable=self.preprocess).pack(anchor="w", padx=(8, 0))

        ttk.Label(left, text="Include dirs (; separated)", style="Muted.TLabel").pack(anchor="w", pady=(8, 4))
        self.include_dirs = tk.StringVar()
        ttk.Entry(left, textvariable=self.include_dirs).pack(fill="x")

        ttk.Label(left, text="Defines (; separated)", style="Muted.TLabel").pack(anchor="w", pady=(8, 4))
        self.defines = tk.StringVar()
        ttk.Entry(left, textvariable=self.defines).pack(fill="x")

        ttk.Label(left, text="Output & Backend", style="Section.TLabel").pack(anchor="w", pady=(14, 8))
        output_row = ttk.Frame(left, style="Panel.TFrame")
        output_row.pack(fill="x")
        # self.output_dir = tk.StringVar(value=str(Path.cwd() / "TestMate_out"))
        ttk.Entry(output_row, textvariable=self.output_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text="...", width=3, command=self._choose_output, style="Secondary.TButton").pack(side="left", padx=(8, 0))

        self.backend = tk.StringVar(value="GoogleTest")
        backend_box = ttk.Combobox(left, textvariable=self.backend, values=("GoogleTest", "CppUTest"), state="readonly")
        backend_box.pack(fill="x", pady=(8, 0))
        self.run_after_generate = tk.BooleanVar(value=True)
        # ttk.Checkbutton(left, text="Run after generate", variable=self.run_after_generate).pack(anchor="w", pady=(8, 0))

        ttk.Label(left, text="LLM Refinement", style="Section.TLabel").pack(anchor="w", pady=(14, 8))
        self.use_ollama = tk.BooleanVar(value=False)
        ollama_toggle_row = ttk.Frame(left, style="Panel.TFrame")
        ollama_toggle_row.pack(fill="x")
        ttk.Checkbutton(ollama_toggle_row, text="Use local Ollama", variable=self.use_ollama, command=self._ollama_select).pack(side="left")
        ttk.Label(ollama_toggle_row, text="Timeout", style="Muted.TLabel").pack(side="right", padx=(8, 0))
        self.ollama_timeout = tk.StringVar(value="45")
        self.ollama_timeout_entry = ttk.Entry(ollama_toggle_row, textvariable=self.ollama_timeout, width=6)
        self.ollama_timeout_entry.pack(side="right")

        model_row = ttk.Frame(left, style="Panel.TFrame")
        model_row.pack(fill="x", pady=(8, 0))
        ttk.Label(model_row, text="Model", style="Panel.TLabel").pack(side="left")
        self.ollama_model = tk.StringVar(value="qwen2.5-coder:3b")
        self.ollama_model_entry = ttk.Entry(model_row, textvariable=self.ollama_model)
        self.ollama_model_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))

        target_row = ttk.Frame(left, style="Panel.TFrame")
        target_row.pack(fill="x", pady=(8, 0))
        ttk.Label(target_row, text="Target %", style="Panel.TLabel").pack(side="left")
        self.target_coverage = tk.StringVar(value="100")
        self.target_coverage_entry = ttk.Entry(target_row, textvariable=self.target_coverage, width=7)
        self.target_coverage_entry.pack(side="left", padx=(8, 16))
        ttk.Label(target_row, text="Rounds", style="Panel.TLabel").pack(side="left")
        self.max_ai_rounds = tk.StringVar(value="1")
        self.max_ai_rounds_entry = ttk.Entry(target_row, textvariable=self.max_ai_rounds, width=5)
        self.max_ai_rounds_entry.pack(side="left", padx=(8, 0))

        report_row = ttk.Frame(left, style="Panel.TFrame")
        report_row.pack(fill="x", pady=(8, 0))
        self.report_file = tk.StringVar()
        self.report_file_entry = ttk.Entry(report_row, textvariable=self.report_file)
        self.report_file_entry.pack(side="left", fill="x", expand=True)
        self.report_button = ttk.Button(report_row, text="Report", command=self._choose_report, style="Secondary.TButton")
        self.report_button.pack(side="left", padx=(8, 0))
        self.refine_report_button = ttk.Button(left, text="Refine From Report & Run", command=self._refine_run_async, style="Accent.TButton")
        self.refine_report_button.pack(fill="x", pady=(8, 0))
        self.ollama_controls = [
            self.ollama_model_entry,
            self.ollama_timeout_entry,
            self.target_coverage_entry,
            self.max_ai_rounds_entry,
            self.report_file_entry,
            self.report_button,
            self.refine_report_button,
        ]
        self._set_ollama_controls_state()

        metrics = ttk.Frame(right, style="Panel.TFrame")
        metrics.grid(row=0, column=0, sticky="ew")
        for idx in range(4):
            metrics.columnconfigure(idx, weight=1)
        self.metric_vars = {
            "files": tk.StringVar(value="0"),
            "functions": tk.StringVar(value="0"),
            "conditions": tk.StringVar(value="0"),
            "tests": tk.StringVar(value="0"),
        }
        for col, (label, var) in enumerate(
            [("Files", self.metric_vars["files"]), ("Functions", self.metric_vars["functions"]), ("Conditions", self.metric_vars["conditions"]), ("Tests", self.metric_vars["tests"])]
        ):
            card = ttk.Frame(metrics, style="Metric.TFrame", padding=(14, 12))
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0))
            ttk.Label(card, text=label, style="MetricLabel.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=var, style="MetricValue.TLabel").pack(anchor="w", pady=(2, 0))

        ttk.Label(right, text="Coverage Summary and Feedback", style="PanelTitle.TLabel").grid(row=1, column=0, sticky="w", pady=(16, 8))
        columns = ("function", "line", "metric", "condition", "status")
        self.tree = ttk.Treeview(right, columns=columns, show="headings")
        for col, title, width in [
            ("function", "Function", 140),
            ("line", "Line", 60),
            ("metric", "Metric", 80),
            ("condition", "Condition / Obligation", 280),
            ("status", "Coverage Status", 200),
        ]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, stretch=True)
        self.tree.grid(row=2, column=0, sticky="nsew")
        self.tree.tag_configure("covered", background="#ecfdf5", foreground="#065f46")
        self.tree.tag_configure("uncovered", background="#fff1f2", foreground="#9f1239")
        self.tree.tag_configure("unknown", background="#f8fafc", foreground="#475569")
        self.tree.bind("<Double-1>", lambda _event: self._add_uncovered_cases_async())
        tree_scroll = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=2, column=1, sticky="ns")

        case_header = ttk.Frame(right, style="Panel.TFrame")
        case_header.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 8))
        case_header.columnconfigure(0, weight=1)
        ttk.Label(case_header, text="Generated Test Cases", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(
            case_header,
            text="Add Manual Testcase",
            command=lambda: self._open_manual_case_popup(None),
            style="Accent.TButton",
        ).grid(row=0, column=1, sticky="e")
        ttk.Label(
            case_header,
            text="Click a testcase row to open a template popup. Use Steps mode for review-only cases or Harness Code mode for direct C++ test-body code.",
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        case_columns = ("test_id", "function", "coverage", "mode", "summary")
        self.case_tree = ttk.Treeview(right, columns=case_columns, show="headings", height=7)
        for col, title, width in [
            ("test_id", "Test ID", 190),
            ("function", "Function", 140),
            ("coverage", "Coverage", 80),
            ("mode", "Mode", 110),
            ("summary", "Summary", 360),
        ]:
            self.case_tree.heading(col, text=title)
            self.case_tree.column(col, width=width, stretch=True)
        self.case_tree.tag_configure("manual", background="#fefce8", foreground="#713f12")
        self.case_tree.tag_configure("auto", background="#ffffff", foreground="#172033")
        self.case_tree.grid(row=4, column=0, sticky="nsew")
        self.case_tree.bind("<ButtonRelease-1>", self._on_case_tree_click)
        self.case_tree.bind("<Double-1>", self._on_case_tree_click)
        self.case_tree.bind("<Return>", lambda _event: self._open_manual_case_popup_from_selection())
        case_scroll = ttk.Scrollbar(right, orient="vertical", command=self.case_tree.yview)
        self.case_tree.configure(yscrollcommand=case_scroll.set)
        case_scroll.grid(row=4, column=1, sticky="ns")

        log_frame = ttk.Frame(right, style="Surface.TFrame", padding=12)
        log_frame.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Label(log_frame, text="Log", style="MetricLabel.TLabel").pack(anchor="w")
        self.log = tk.Text(
            log_frame,
            height=7,
            bd=0,
            highlightthickness=1,
            highlightbackground="#dbe3ef",
            highlightcolor="#93c5fd",
            bg="#0f172a",
            fg="#dbeafe",
            insertbackground="#ffffff",
            font=("Cascadia Mono", 9),
            wrap="word",
        )
        self.log.pack(fill="x", pady=(8, 0))

    def _add_sources(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(SOURCE_EXTENSIONS))
        paths = filedialog.askopenfilenames(title="Select C/C++ source files", filetypes=[("C/C++ source files", patterns), ("All files", "*.*")])
        self._add_source_paths([Path(raw) for raw in paths])

    def _show_add_source_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Files...", command=self._add_sources)
        menu.add_command(label="Folder...", command=self._add_source_folder)
        button = self.add_source_button
        x = button.winfo_rootx()
        y = button.winfo_rooty() + button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()
        
    def _add_source_folder(self) -> None:
        selected = filedialog.askdirectory(title="Select source/project folder")
        if not selected:
            return
        discovered = ProjectScanner.discover_sources(Path(selected), include_headers=True)
        self._add_source_paths(discovered)
        self._append_log(f"Scanned folder: {selected}; added {len(discovered)} source/header files.")

    def _choose_compile_commands(self) -> None:
        selected = filedialog.askopenfilename(title="Select compile_commands.json", filetypes=[("compile_commands.json", "compile_commands.json"), ("JSON files", "*.json"), ("All files", "*.*")])
        if not selected:
            return
        self.compile_commands.set(selected)
        try:
            database = CompileCommandsDatabase.load(Path(selected))
            sources = database.source_files()
            self._add_source_paths(sources)
            existing_includes = [item.strip() for item in self.include_dirs.get().split(";") if item.strip()]
            merged_includes = Orchestrator._unique_strings(existing_includes + database.include_dirs() + discover_include_dirs_for_sources(sources))
            self.include_dirs.set(";".join(merged_includes))
            existing_defines = [item.strip() for item in self.defines.get().split(";") if item.strip()]
            merged_defines = Orchestrator._unique_strings(existing_defines + database.defines())
            self.defines.set(";".join(merged_defines))
            self._append_log(f"Loaded compile_commands.json: added {len(sources)} files; merged include dirs and defines.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not load compile_commands.json:\n{exc}")

    def _add_source_paths(self, paths: list[Path]) -> None:
        added = 0
        existing = {path.resolve() for path in self.sources if path.exists()}
        for path in paths:
            if path.suffix.lower() not in SOURCE_EXTENSIONS or not path.exists():
                continue
            resolved = path.expanduser().resolve()
            if resolved in existing:
                continue
            existing.add(resolved)
            self.sources.append(resolved)
            added += 1
        self._refresh_source_list()
        if added:
            auto_added = self._merge_auto_include_dirs_for_sources(self.sources)
            msg = f"Added {added} source input files."
            if auto_added:
                msg += f" Auto-added {auto_added} include dirs."
            self._append_log(msg)
    def _merge_auto_include_dirs_for_sources(self, sources: list[Path]) -> int:
        existing = [item.strip() for item in self.include_dirs.get().split(";") if item.strip()]
        merged = Orchestrator._unique_strings([*existing, *discover_include_dirs_for_sources(sources)])
        added = max(0, len(merged) - len(existing))
        if added:
            self.include_dirs.set(";".join(merged))
        return added

    def _refresh_source_list(self) -> None:
        self.source_list.delete(0, "end")
        for path in self.sources:
            self.source_list.insert("end", str(path))
        self.metric_vars["files"].set(str(len(self.sources)))

    def _clear_sources(self) -> None:
        self.sources.clear()
        self.source_list.delete(0, "end")
        self.metric_vars["files"].set("0")

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="Select output folder")
        if selected:
            self.output_dir.set(selected)

    def _ollama_select(self) -> None:
        self._set_ollama_controls_state()

    def _set_ollama_controls_state(self) -> None:
        state = "normal" if self.use_ollama.get() else "disabled"
        for widget in getattr(self, "ollama_controls", []):
            widget.configure(state=state)

    def _choose_report(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select backend fail or coverage report",
            filetypes=[("Report files", "*.txt *.log *.html *.htm *.csv *.json"), ("All files", "*.*")],
        )
        if selected:
            self.report_file.set(selected)
            self.report_path = Path(selected)

    def _open_report_file(self) -> None:
        report = self._latest_report_path()
        if not report:
            messagebox.showwarning(APP_NAME, "No report file is available yet. Generate a report or select a report file first.")
            return
        try:
            os.startfile(str(report))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not open report:\n{report}\n\n{exc}")

    def _latest_report_path(self) -> Path | None:
        for path in reversed(self.generated_paths):
            if path.suffix.lower() in {".html", ".htm"} and path.exists():
                return path
        selected = self.report_file.get().strip()
        if selected:
            path = Path(selected)
            if path.exists():
                return path
        out_dir = source_scoped_output_dir(Path(self.output_dir.get()).expanduser(), self.sources)
        backend = self.backend.get()
        for source in self.sources:
            candidate = out_dir / backend / f"{source.stem}.html"
            if candidate.exists():
                return candidate
        return None

    def _run_async(self) -> None:
        if not self.sources and not self.compile_commands.get().strip():
            messagebox.showwarning(APP_NAME, "Add source files, a source folder, or compile_commands.json.")
            return
        if not self._ensure_msys2_toolchain():
            return
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def _refine_run_async(self) -> None:
        if not self.sources and not self.compile_commands.get().strip():
            messagebox.showwarning(APP_NAME, "Add source files, a source folder, or compile_commands.json.")
            return
        report = self.report_file.get().strip()
        if not report or not Path(report).exists():
            messagebox.showwarning(APP_NAME, "Select an existing coverage/fail report first.")
            return
        if not self._ensure_msys2_toolchain():
            return
        thread = threading.Thread(target=self._refine_and_run_from_report, daemon=True)
        thread.start()

    def _rerun_async(self) -> None:
        if not self.sources and not self.compile_commands.get().strip():
            messagebox.showwarning(APP_NAME, "Add source files, a source folder, or compile_commands.json.")
            return
        if not self._ensure_msys2_toolchain():
            return
        thread = threading.Thread(target=self._rerun_existing_testcases, daemon=True)
        thread.start()

    def _add_uncovered_cases_async(self) -> None:
        if not self.sources and not self.compile_commands.get().strip():
            messagebox.showwarning(APP_NAME, "Add source files, a source folder, or compile_commands.json first.")
            return
        if not self._ensure_msys2_toolchain():
            return
        selected_gap = self._selected_gap_request()
        if selected_gap and "uncovered" not in selected_gap.get("status", "").lower():
            if not messagebox.askyesno(APP_NAME, "Selected row is not marked uncovered. Add gap cases for all uncovered rows instead?"):
                return
            selected_gap = None
        thread = threading.Thread(target=lambda: self._add_uncovered_cases(selected_gap), daemon=True)
        thread.start()

    def _selected_gap_request(self) -> dict | None:
        selection = self.tree.selection()
        if not selection:
            return None
        values = self.tree.item(selection[0], "values")
        if len(values) < 5:
            return None
        line_text = str(values[1])
        line_match = re.search(r"\d+", line_text)
        return {
            "function": str(values[0]),
            "line": int(line_match.group(0)) if line_match else None,
            "metric": str(values[2]),
            "text": str(values[3]),
            "status": str(values[4]),
        }

    def _populate_case_tree(self, cases: list[TestCase] | None = None) -> None:
        def insert() -> None:
            if not hasattr(self, "case_tree"):
                return
            self.case_tree.delete(*self.case_tree.get_children())
            self._case_by_iid.clear()
            for case in cases or []:
                values = case.values or {}
                mode = "Manual Code" if values.get("__manual_action_code") else "Steps" if values.get("__manual_steps_only") else "Auto"
                summary = case.description or case.rationale or case.expected or ""
                if len(summary) > 160:
                    summary = summary[:157] + "..."
                tags = ("manual",) if mode != "Auto" or str(case.test_id).startswith("MANUAL_") else ("auto",)
                iid = self.case_tree.insert(
                    "",
                    "end",
                    values=(case.test_id, case.function, case.coverage, mode, summary),
                    tags=tags,
                )
                self._case_by_iid[iid] = case
        self.after(0, insert)

    def _on_case_tree_click(self, event: tk.Event) -> None:
        if not hasattr(self, "case_tree"):
            return
        row_id = self.case_tree.identify_row(event.y)
        if not row_id:
            return
        self.case_tree.selection_set(row_id)
        self._open_manual_case_popup(self._case_by_iid.get(row_id))

    def _open_manual_case_popup_from_selection(self) -> None:
        if not hasattr(self, "case_tree"):
            return
        selection = self.case_tree.selection()
        selected = self._case_by_iid.get(selection[0]) if selection else None
        self._open_manual_case_popup(selected)

    def _selected_coverage_function_name(self) -> str:
        selection = self.tree.selection()
        if not selection:
            return ""
        values = self.tree.item(selection[0], "values")
        return str(values[0]) if values else ""

    def _manual_case_template(self, fn: FunctionInfo | None, backend: str, mode: str) -> tuple[str, str, str]:
        fn_name = fn.name if fn else "FunctionUnderTest"
        if fn:
            arg_names = []
            values = {}
            for param in fn.parameters:
                safe = re.sub(r"[^A-Za-z0-9_]", "_", param.name)
                if getattr(param, "kind", "") == "pointer":
                    arg_names.append(f"tm_arg_{safe}")
                    values[param.name] = "non_null"
                else:
                    arg_names.append(f"tm_{safe}")
                    values[param.name] = "0"
        else:
            arg_names = []
            values = {}
        call = f"{fn_name}({', '.join(arg_names)})"
        values_json = json.dumps(values, indent=2)
        steps = (
            "1. Configure the precondition values shown in the Values JSON.\n"
            f"2. Invoke `{fn_name}` through the generated harness.\n"
            "3. Observe return value, output parameters, globals, and expected dependency calls.\n"
            "4. Confirm the selected uncovered line/branch/MC/DC obligation is exercised."
        )
        if mode == "Harness Code":
            if backend == "CppUTest":
                if fn and fn.return_type.strip() != "void":
                    code = f"actual_return = {call};\nCHECK_EQUAL(/* expected */ 0, actual_return);"
                else:
                    code = f"{call};\n/* Add CHECK/CHECK_EQUAL/MEMCMP_EQUAL assertions here. */"
            else:
                if fn and fn.return_type.strip() != "void":
                    code = f"actual_return = {call};\nEXPECT_EQ(/* expected */ 0, actual_return);"
                else:
                    code = f"{call};\n/* Add EXPECT_* assertions here. */"
            return values_json, steps, code
        return values_json, steps, ""

    def _open_manual_case_popup(self, selected_case: TestCase | None = None) -> None:
        if not self.last_functions:
            messagebox.showwarning(APP_NAME, "Generate or rerun first so functions are available for the testcase template.")
            return

        popup = tk.Toplevel(self)
        popup.title("Manual Testcase Template")
        popup.geometry("980x760")
        popup.minsize(840, 620)
        popup.transient(self)
        popup.grab_set()
        popup.columnconfigure(0, weight=1)
        popup.rowconfigure(1, weight=1)

        header = ttk.Frame(popup, padding=14)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        function_names = [fn.name for fn in self.last_functions]
        selected_function = selected_case.function if selected_case else self._selected_coverage_function_name()
        if selected_function not in function_names:
            selected_function = function_names[0]

        test_id_var = tk.StringVar(value=(f"MANUAL_{selected_function}_{len(self.last_cases) + 1:03d}" if selected_case is None else f"MANUAL_{selected_case.test_id}"))
        function_var = tk.StringVar(value=selected_function)
        coverage_var = tk.StringVar(value=(selected_case.coverage if selected_case else self.coverage.get()))
        mode_var = tk.StringVar(value="Harness Code" if selected_case and (selected_case.values or {}).get("__manual_action_code") else "Steps Only")
        source_var = tk.StringVar(value=self.sources[0].name if self.sources else "")

        ttk.Label(header, text="Test ID", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(header, textvariable=test_id_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(header, text="Source", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(12, 8), pady=4)
        ttk.Combobox(header, textvariable=source_var, values=[p.name for p in self.sources], state="readonly", width=24).grid(row=0, column=3, sticky="ew", pady=4)

        ttk.Label(header, text="Function", style="Panel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        function_box = ttk.Combobox(header, textvariable=function_var, values=function_names, state="readonly")
        function_box.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(header, text="Coverage", style="Panel.TLabel").grid(row=1, column=2, sticky="w", padx=(12, 8), pady=4)
        ttk.Combobox(header, textvariable=coverage_var, values=("C0", "C0/C1", "C1", "MC/DC", "Manual"), state="readonly", width=24).grid(row=1, column=3, sticky="ew", pady=4)

        ttk.Label(header, text="Mode", style="Panel.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        mode_box = ttk.Combobox(header, textvariable=mode_var, values=("Steps Only", "Harness Code"), state="readonly")
        mode_box.grid(row=2, column=1, sticky="ew", pady=4)

        body = ttk.Frame(popup, padding=(14, 0, 14, 14))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(4, weight=1)

        desc_var = tk.StringVar(value=(selected_case.description if selected_case else f"Manual testcase for {selected_function}"))
        expected_var = tk.StringVar(value=(selected_case.expected if selected_case else "Review expected return/output/global side effects."))
        precond_var = tk.StringVar(value=(selected_case.preconditions if selected_case else "Unit is initialized; mocks/stubs are reset; required globals/fixtures are configured."))

        ttk.Label(body, text="Description", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Entry(body, textvariable=desc_var).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(body, text="Preconditions", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 4))
        ttk.Entry(body, textvariable=precond_var).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        ttk.Label(body, text="Values JSON / Harness Inputs", style="Panel.TLabel").grid(row=4, column=0, sticky="nw")
        ttk.Label(body, text="Steps or Manual C++ Test Body", style="Panel.TLabel").grid(row=4, column=1, sticky="nw")
        values_text = tk.Text(body, height=11, font=("Cascadia Mono", 9), wrap="none")
        code_text = tk.Text(body, height=11, font=("Cascadia Mono", 9), wrap="none")
        values_text.grid(row=5, column=0, sticky="nsew", padx=(0, 8), pady=(4, 8))
        code_text.grid(row=5, column=1, sticky="nsew", padx=(8, 0), pady=(4, 8))

        ttk.Label(body, text="Expected Result", style="Panel.TLabel").grid(row=6, column=0, sticky="w", pady=(0, 4))
        ttk.Entry(body, textvariable=expected_var).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        hint = ttk.Label(
            body,
            text="Template keys: normal parameter names set harness inputs. Internal keys such as __expected_return, __expected_output.<name>, __expected_global.<name>, and __expected_calls are also supported. In Harness Code mode, the code block is inserted directly into the generated TEST body.",
            style="Muted.TLabel",
            wraplength=900,
        )
        hint.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        current_template: dict[str, str] = {"function": "", "mode": ""}

        def selected_fn() -> FunctionInfo | None:
            return next((fn for fn in self.last_functions if fn.name == function_var.get()), None)

        def fill_template(force: bool = False) -> None:
            fn = selected_fn()
            mode = "Harness Code" if mode_var.get() == "Harness Code" else "Steps Only"
            if not force and current_template.get("function") == function_var.get() and current_template.get("mode") == mode:
                return
            values_json, steps, code = self._manual_case_template(fn, self.backend.get(), mode)
            if force or not values_text.get("1.0", "end").strip():
                values_text.delete("1.0", "end")
                values_text.insert("1.0", values_json)
            if force or not code_text.get("1.0", "end").strip():
                code_text.delete("1.0", "end")
                code_text.insert("1.0", code if mode == "Harness Code" else steps)
            current_template["function"] = function_var.get()
            current_template["mode"] = mode

        if selected_case:
            values = dict(selected_case.values or {})
            manual_code = values.pop("__manual_action_code", "")
            values.pop("__manual_steps_only", None)
            values.pop("__manual_source", None)
            values_text.insert("1.0", json.dumps(values, indent=2))
            code_text.insert("1.0", manual_code or selected_case.test_steps or "")
        else:
            fill_template(force=True)

        function_box.bind("<<ComboboxSelected>>", lambda _event: fill_template(force=True))
        mode_box.bind("<<ComboboxSelected>>", lambda _event: fill_template(force=True))

        actions = ttk.Frame(popup, padding=(14, 0, 14, 14))
        actions.grid(row=2, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)

        def save() -> None:
            fn = selected_fn()
            if fn is None:
                messagebox.showerror(APP_NAME, "Select a valid function.", parent=popup)
                return
            try:
                raw_values = values_text.get("1.0", "end").strip()
                values = json.loads(raw_values) if raw_values else {}
                if not isinstance(values, dict):
                    raise ValueError("Values JSON must be an object.")
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Invalid Values JSON: {exc}", parent=popup)
                return
            code_or_steps = code_text.get("1.0", "end").rstrip()
            payload = {
                "source_name": source_var.get(),
                "test_id": test_id_var.get().strip(),
                "function": fn.name,
                "coverage": coverage_var.get(),
                "mode": mode_var.get(),
                "values": values,
                "description": desc_var.get().strip(),
                "preconditions": precond_var.get().strip(),
                "expected": expected_var.get().strip(),
                "code_or_steps": code_or_steps,
            }
            popup.destroy()
            self._add_manual_testcase_async(payload)

        ttk.Button(actions, text="Insert Template", command=lambda: fill_template(force=True), style="Secondary.TButton").grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="Cancel", command=popup.destroy, style="Secondary.TButton").grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Button(actions, text="Add Testcase", command=save, style="Accent.TButton").grid(row=0, column=2, sticky="e", padx=(8, 0))

    def _add_manual_testcase_async(self, payload: dict) -> None:
        if not self.sources and not self.compile_commands.get().strip():
            messagebox.showwarning(APP_NAME, "Add source files, a source folder, or compile_commands.json first.")
            return
        if not self._ensure_msys2_toolchain():
            return
        thread = threading.Thread(target=lambda: self._add_manual_testcase(payload), daemon=True)
        thread.start()

    def _add_manual_testcase(self, payload: dict) -> None:
        self._set_log("")
        self._append_log(f"Adding manual testcase for {payload.get('function', 'unknown function')}...")
        result = Orchestrator(self._append_log).add_manual_test_case(self._orchestrator_options(), payload)
        self.generated_paths = result.generated_paths
        self.last_functions = result.functions
        self.last_cases = result.cases
        self.last_feedback = result.feedback
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
        self._populate_case_tree(result.cases)
        if result.generated_paths:
            self._append_log("Updated artifacts:")
            for path in result.generated_paths:
                self._append_log(f"  {path}")
        title = "Add manual testcase failed" if result.exit_code else "Add manual testcase complete"
        self._show_completion_popup(title, result.summary_lines, result.generated_paths, is_error=bool(result.exit_code))

    def _ensure_msys2_toolchain(self) -> bool:
        if mingw_toolchain_available():
            return True

        default_text = str(DEFAULT_MINGW_ROOT)
        messagebox.showwarning(
            APP_NAME,
            f"MSYS2 MinGW64 was not found at:\n{default_text}\n\n"
            "Select your MSYS2 mingw64 folder, for example C:\\msys64\\mingw64.",
        )
        initial = DEFAULT_MINGW_ROOT.parent if DEFAULT_MINGW_ROOT.parent.exists() else Path.home()
        selected = filedialog.askdirectory(
            title="Select MSYS2 mingw64 folder",
            initialdir=str(initial),
        )
        if not selected:
            return False

        root = configure_mingw_root(selected)
        if mingw_toolchain_available():
            self._append_log(f"MSYS2 MinGW64 path: {configured_mingw_root()}")
            return True

        messagebox.showerror(
            APP_NAME,
            "The selected folder is not a usable MSYS2 MinGW64 installation.\n\n"
            f"Selected: {root}\n\n"
            "Expected files:\n"
            f"- {root / 'bin'}\n"
            f"- {root.parent / 'usr' / 'bin' / 'bash.exe'}",
        )
        return False

    def _run(self) -> None:
        self._set_log("")
        self._clear_tree()
        result = Orchestrator(self._append_log).analyze_and_generate(self._orchestrator_options())
        self.generated_paths = result.generated_paths
        self.last_functions = result.functions
        self.last_cases = result.cases
        self.last_feedback = result.feedback
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
        self._populate_case_tree(result.cases)
        if result.generated_paths:
            self._append_log("Generated artifacts:")
            for path in result.generated_paths:
                self._append_log(f"  {path}")
        title = "Analyze & Generate failed" if result.exit_code else "Analyze & Generate complete"
        self._show_completion_popup(title, result.summary_lines, result.generated_paths, is_error=bool(result.exit_code))

    def _refine_and_run_from_report(self) -> None:
        self._set_log("")
        self._clear_tree()
        result = Orchestrator(self._append_log).refine_from_report(self._orchestrator_options())
        self.generated_paths = result.generated_paths
        self.last_functions = result.functions
        self.last_cases = result.cases
        self.last_feedback = result.feedback
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
        self._populate_case_tree(result.cases)
        if result.generated_paths:
            self._append_log("Generated artifacts:")
            for path in result.generated_paths:
                self._append_log(f"  {path}")
        title = "Ollama refine & run failed" if result.exit_code else "Ollama refine & run complete"
        self._show_completion_popup(title, result.summary_lines, result.generated_paths, is_error=bool(result.exit_code))

    def _rerun_existing_testcases(self) -> None:
        self._set_log("")
        self._clear_tree()
        result = Orchestrator(self._append_log).rerun_existing(self._orchestrator_options())
        self.generated_paths = result.generated_paths
        self.last_functions = result.functions
        self.last_cases = result.cases
        self.last_feedback = result.feedback
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
        self._populate_case_tree(result.cases)
        if result.generated_paths:
            self._append_log("Refreshed reports:")
            for path in result.generated_paths:
                self._append_log(f"  {path}")
        title = "Rerun testcases failed" if result.exit_code else "Rerun testcases complete"
        self._show_completion_popup(title, result.summary_lines, result.generated_paths, is_error=bool(result.exit_code))

    def _add_uncovered_cases(self, selected_gap: dict | None = None) -> None:
        self._set_log("")
        self._append_log("Adding coverage-gap test cases from current uncovered evidence...")
        result = Orchestrator(self._append_log).add_uncovered_cases(self._orchestrator_options(), selected_gap)
        self.generated_paths = result.generated_paths
        self.last_functions = result.functions
        self.last_cases = result.cases
        self.last_feedback = result.feedback
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
        self._populate_case_tree(result.cases)
        if result.generated_paths:
            self._append_log("Updated artifacts:")
            for path in result.generated_paths:
                self._append_log(f"  {path}")
        title = "Add gap cases failed" if result.exit_code else "Add gap cases complete"
        self._show_completion_popup(title, result.summary_lines, result.generated_paths, is_error=bool(result.exit_code))

    def _orchestrator_options(self) -> OrchestratorOptions:
        report = self.report_file.get().strip()
        return OrchestratorOptions(
            sources=list(self.sources),
            output_dir=Path(self.output_dir.get()),
            coverage=self.coverage.get(),
            level=self.level.get(),
            backend=self.backend.get(),
            compiler=self.compiler.get(),
            include_dirs=[item.strip() for item in self.include_dirs.get().split(";") if item.strip()],
            defines=[item.strip() for item in self.defines.get().split(";") if item.strip()],
            preprocess=self.preprocess.get(),
            iso26262_profile=self.iso26262_profile.get(),
            asil_level=self.asil_level.get(),
            run_after_generate=True,
            report_file=Path(report) if report else None,
            compile_commands=Path(self.compile_commands.get().strip()) if self.compile_commands.get().strip() else None,
            include_static_functions=self.include_static_functions.get(),
            include_main_function=self.include_main_function.get(),
            use_ollama=self.use_ollama.get(),
            ollama_model=self.ollama_model.get(),
            ollama_timeout_seconds=max(5, int(self._float_setting(self.ollama_timeout.get(), 45.0))),
            target_coverage_percent=self._float_setting(self.target_coverage.get(), 100.0),
            max_ai_rounds=max(1, int(self._float_setting(self.max_ai_rounds.get(), 1.0))),
            mock_retry_limit=max(0, min(5, int(self._float_setting(self.mock_retry_limit.get(), 5.0)))),
        )

    @staticmethod
    def _float_setting(value: str, defaults: float) -> float:
        try:
            return float(value)
        except ValueError:
            return defaults

    def _set_log(self, text: str) -> None:
        self.after(0, lambda: self._replace_log(text))

    def _replace_log(self, text: str) -> None:
        self.log.delete("1.0", "end")
        if text:
            self.log.insert("end", text)

    def _append_log(self, text: str) -> None:
        self.after(0, lambda: self._append_log_now(text))

    def _append_log_now(self, text: str) -> None:
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def _show_completion_popup(self, title: str, summary_lines: list[str], generated_paths: list[Path], *, is_error: bool = False) -> None:
        def show() -> None:
            details = "\n".join(summary_lines[:8]) if summary_lines else "No source files were processed."
            if len(summary_lines) > 8:
                details += f"\n... {len(summary_lines) - 8} more"
            artifact_note = f"\n\nArtifacts generated: {len(generated_paths)}"
            if generated_paths:
                report_path = next((path for path in reversed(generated_paths) if path.suffix.lower() in {".html", ".htm"}), generated_paths[-1])
                artifact_note += f"\nReport: {report_path}"
            message = f"{title}\n\n{details}{artifact_note}"
            if is_error:
                messagebox.showerror(APP_NAME, message)
            else:
                messagebox.showinfo(APP_NAME, message)

        self.after(0, show)

    def _clear_tree(self) -> None:
        def clear() -> None:
            self.tree.delete(*self.tree.get_children())
            if hasattr(self, "case_tree"):
                self.case_tree.delete(*self.case_tree.get_children())
                self._case_by_iid.clear()
        self.after(0, clear)

    def _populate_tree(self, functions: list[FunctionInfo] | None = None, feedback: CoverageFeedback | None = None) -> None:
        def insert() -> None:
            self.tree.delete(*self.tree.get_children())
            if not functions:
                return

            gaps = CoverageGapPlanner().plan(feedback, functions, max_gaps=120) if feedback else []
            gap_keys = {(gap.function, gap.line, gap.metric) for gap in gaps}

            # Show concrete backend gaps first. Selecting one of these rows lets the
            # Add Gap Cases action target a specific uncovered line/branch/MC/DC item.
            for gap in gaps:
                self.tree.insert(
                    "",
                    "end",
                    values=(
                        gap.function,
                        gap.line if gap.line is not None else "-",
                        gap.metric,
                        gap.condition or gap.text,
                        "Uncovered",
                    ),
                    tags=("uncovered",),
                )

            if gaps:
                self.tree.insert("", "end", values=("", "", "", "— static obligations below —", ""), tags=("unknown",))

            # Populate static obligations from analyzed functions.
            for fn in functions:
                fn_has_c0_gap = any(gap.function == fn.name and gap.metric == "C0" for gap in gaps)
                status = "Uncovered" if fn_has_c0_gap else "Covered"
                self.tree.insert(
                    "",
                    "end",
                    values=(fn.name, f"{fn.start_line}-{fn.end_line}", "C0", f"Statement coverage ({fn.start_line}-{fn.end_line})", status),
                    tags=("uncovered" if status == "Uncovered" else "covered",),
                )

                for condition in fn.conditions:
                    c1_status = "Uncovered" if (fn.name, condition.line, "C1") in gap_keys else "Covered"
                    mcdc_status = "Uncovered" if (fn.name, condition.line, "MC/DC") in gap_keys else "Covered"
                    self.tree.insert(
                        "",
                        "end",
                        values=(fn.name, condition.line, "C1", condition.text, c1_status),
                        tags=("uncovered" if c1_status == "Uncovered" else "covered",),
                    )
                    self.tree.insert(
                        "",
                        "end",
                        values=(fn.name, condition.line, "MC/DC", condition.text, mcdc_status),
                        tags=("uncovered" if mcdc_status == "Uncovered" else "covered",),
                    )

        self.after(0, insert)

    def _update_metrics(self, functions: int, conditions: int, tests: int) -> None:
        self.after(
            0,
            lambda: (
                self.metric_vars["files"].set(str(len(self.sources))),
                self.metric_vars["functions"].set(str(functions)),
                self.metric_vars["conditions"].set(str(conditions)),
                self.metric_vars["tests"].set(str(tests)),
            ),
        )



@dataclass
class OrchestratorOptions:
    sources: list[Path]
    output_dir: Path
    coverage: str = "MC/DC"
    level: str = "UT"
    backend: str = "GoogleTest"
    compiler: str = DEFAULT_COMPILER
    include_dirs: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    preprocess: bool = True
    iso26262_profile: bool = False
    asil_level: str = "ASIL D"
    run_after_generate: bool = True
    report_file: Path | None = None
    compile_commands: Path | None = None
    include_static_functions: bool = False
    include_main_function: bool = False
    use_ollama: bool = False
    ollama_model: str = "qwen2.5-coder:3b"
    ollama_timeout_seconds: int = 45
    target_coverage_percent: float = 100.0
    max_ai_rounds: int = 1
    mock_retry_limit: int = 5


@dataclass
class OrchestratorResult:
    functions: list[FunctionInfo] = field(default_factory=list)
    cases: list[TestCase] = field(default_factory=list)
    generated_paths: list[Path] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)
    feedback: CoverageFeedback | None = None
    exit_code: int = 0


class Orchestrator:
    def __init__(self, log_callback: Callable[[str], None] | None = None):
        self.log_callback = log_callback or (lambda _text: None)

    def _log(self, text: str) -> None:
        self.log_callback(text)

    def _run_backend_with_oracle_fallback(self, backend: str, backend_out_dir: Path, source: Path, retry_limit: int = 5) -> RunResult:
        runner = TestBackendRunner()
        run_result = runner.run(backend, backend_out_dir, source)
        self._log(format_run_result(run_result))
        self._log_run_output(run_result)
        retry_limit = max(0, min(5, int(retry_limit)))
        for attempt in range(1, retry_limit + 1):
            if run_result.returncode in (0, None):
                break
            notes: list[str] = []
            oracle_updated, oracle_notes = TestOracleFallback.apply(backend, backend_out_dir, source, run_result)
            notes.extend(oracle_notes)
            mock_updated, mock_notes = TestMockFallback.apply(backend, backend_out_dir, source, run_result)
            notes.extend(mock_notes)
            for note in notes:
                self._log(note)
            updated = oracle_updated + mock_updated
            if not updated:
                break
            self._log(f"Fallback retry {attempt}/{retry_limit}: patched {updated} generated test line(s); rerunning {backend}.")
            next_result = runner.run(backend, backend_out_dir, source)
            fallback_log = "\n".join([
                "",
                f"=== Testing Mate Fallback Retry {attempt}/{retry_limit} ===",
                *notes,
                format_run_result(next_result),
            ])
            next_result.stdout = (next_result.stdout + fallback_log)[-12000:]
            run_result = next_result
            self._log(format_run_result(run_result))
            self._log_run_output(run_result)
        return run_result

    def _log_run_output(self, run_result: RunResult) -> None:
        if run_result.stdout.strip():
            self._log("Run stdout:")
            self._log(run_result.stdout[-1200:])
        if run_result.stderr.strip():
            self._log("Run stderr:")
            self._log(run_result.stderr[-1200:])

    @staticmethod
    def _merge_run_failures(feedback: CoverageFeedback, run_result: RunResult | None) -> CoverageFeedback:
        if run_result is None or run_result.returncode in (0, None):
            return feedback
        failures = list(feedback.failures)
        for item in TestOracleFallback.failure_summaries(run_result):
            if item not in failures:
                failures.append(item)
        return CoverageFeedback(
            source=feedback.source,
            raw_text=feedback.raw_text,
            coverage_percent=feedback.coverage_percent,
            metrics=feedback.metrics,
            metric_notes=feedback.metric_notes,
            uncovered_items=feedback.uncovered_items,
            failures=failures[:120],
        )

    def analyze_and_generate(self, options: OrchestratorOptions) -> OrchestratorResult:
        return self._run(options, refine_from_report=False)

    def refine_from_report(self, options: OrchestratorOptions) -> OrchestratorResult:
        return self._run(options, refine_from_report=True)

    def rerun_existing(self, options: OrchestratorOptions) -> OrchestratorResult:
        result = OrchestratorResult()
        project_sources, _include_dirs, _defines, _compiler = self._resolve_project_inputs(options)
        out_dir = source_scoped_output_dir(options.output_dir.expanduser(), project_sources)
        if out_dir != options.output_dir.expanduser():
            self._log(f"Output folder: {out_dir}")
        backend = options.backend
        report_parser = CoverageReportParser()
        report_items: list[dict] = []

        for source in project_sources:
            try:
                backend_out_dir = out_dir / backend
                functions, cases = self._load_existing_analysis(backend_out_dir, source)
                runner_path = backend_harness_path(backend_out_dir, source, backend)
                if not runner_path.exists():
                    raise FileNotFoundError(f"Runner script was not generated: {runner_path}")

                self._log(f"Rerunning existing {backend} testcase environment: {runner_path}")
                run_result = self._run_backend_with_oracle_fallback(backend, backend_out_dir, source, options.mock_retry_limit)
                if run_result.returncode not in (0, None):
                    result.exit_code = run_result.returncode

                feedback = collect_backend_feedback(report_parser, backend_out_dir, source, backend, self._log)
                if feedback is None:
                    feedback = CoverageFeedback(source="none", raw_text="", uncovered_items=[], failures=[])
                feedback = merge_generated_coverage_feedback(feedback, functions, cases, options.coverage)
                feedback = self._merge_run_failures(feedback, run_result)

                writer = ArtifactWriter(backend_out_dir)
                report_path = writer._write_html_report(source, functions, cases, backend, feedback, run_result)
                summary_path = writer._write_summary(source, functions, cases, feedback, run_result)
                result.generated_paths.extend([report_path, summary_path])
                if run_result.returncode not in (0, None) or (run_result.returncode is None and run_result.skipped_reason):
                    result.exit_code = run_result.returncode or 1
                    result.summary_lines.append(f"{source.name}: ERROR {format_run_result(run_result)}")
                    return result
                report_items.append(
                    {
                        "source": source,
                        "functions": functions,
                        "cases": cases,
                        "feedback": feedback,
                        "run_result": run_result,
                        "report_path": report_path,
                    }
                )
                result.functions.extend(functions)
                result.cases.extend(cases)
                result.summary_lines.append(
                    f"{source.name}: reran {len(cases)} generated cases, {format_run_result(run_result)}, {coverage_log_summary(feedback)}"
                )
            except Exception as exc:
                self._log(f"ERROR rerun {source}: {exc}")
                result.summary_lines.append(f"{source.name}: ERROR {exc}")
                result.exit_code = 1
                return result

        if len(report_items) > 1:
            final_report = ArtifactWriter(out_dir / backend)._write_multi_file_report(backend, report_items)
            result.generated_paths.append(final_report)
            self._log(f"All-files report: {final_report}")
            result.summary_lines.append(f"All files: {len(report_items)} source reports collected in {final_report}")

        return result

    def add_manual_test_case(self, options: OrchestratorOptions, payload: dict) -> OrchestratorResult:
        """Append one user-authored testcase, regenerate the backend harness, rerun, and refresh reports."""
        result = OrchestratorResult()
        project_sources, include_dirs, defines, compiler = self._resolve_project_inputs(options)
        out_dir = source_scoped_output_dir(options.output_dir.expanduser(), project_sources)
        out_dir.mkdir(parents=True, exist_ok=True)
        if out_dir != options.output_dir.expanduser():
            self._log(f"Output folder: {out_dir}")

        requested_source = str(payload.get("source_name") or "").strip()
        if requested_source:
            project_sources = [source for source in project_sources if source.name == requested_source or source.stem == Path(requested_source).stem] or project_sources

        preprocessor = CPreprocessor(compiler, include_dirs, defines)
        analyzer = ClangEnhancedAnalyzer(include_dirs, defines, compiler, self._log)
        generator = TestVectorGenerator()
        report_parser = CoverageReportParser()
        loaded_feedback = load_coverage_feedback(options.report_file, report_parser, self._log)
        backend = options.backend
        unit_sources = self._project_unit_sources(project_sources, include_dirs) if backend in {"CppUTest", "GoogleTest"} else list(project_sources)
        report_items: list[dict] = []

        for source in project_sources[:1]:
            try:
                analysis_source = source
                if options.preprocess:
                    analysis_source, note = preprocessor.preprocess_file(source, out_dir / "preprocessed")
                    self._log(note)
                functions = analyzer.analyze(
                    analysis_source,
                    include_static_functions=options.include_static_functions,
                    include_main=options.include_main_function,
                )
                if analysis_source != source:
                    analyzer.apply_original_source_context(functions, source)
                code_database = CodeDatabase.from_functions(source, analysis_source, functions, analyzer=analyzer.last_analyzer)
                backend_out_dir = out_dir / backend
                writer = ArtifactWriter(backend_out_dir)
                try:
                    _saved_functions, existing_cases = self._load_existing_analysis(backend_out_dir, source)
                    self._log(f"Loaded {len(existing_cases)} existing cases for {source.name}.")
                except Exception as exc:
                    self._log(f"Existing analysis not available for {source.name}; generating initial cases first: {exc}")
                    existing_cases = generator.generate(
                        source.name,
                        functions,
                        options.level,
                        options.coverage,
                        options.iso26262_profile,
                        options.asil_level,
                        options.include_static_functions,
                        code_database,
                    )

                manual_case = self._manual_case_from_payload(source, functions, existing_cases, options, payload)
                cases = [*existing_cases, manual_case]
                feedback = collect_backend_feedback(report_parser, backend_out_dir, source, backend, self._log) or loaded_feedback
                feedback = merge_generated_coverage_feedback(feedback, functions, cases, options.coverage)

                paths = writer.write(
                    source,
                    functions,
                    cases,
                    options.level,
                    backend,
                    feedback,
                    None,
                    include_backend=True,
                    unit_sources=unit_sources or [source],
                    include_dirs=include_dirs,
                    target_coverage_percent=options.target_coverage_percent,
                    code_database=code_database,
                )
                result.generated_paths.extend(paths)
                self._log(f"Added manual testcase {manual_case.test_id} for {manual_case.function}.")

                run_result = self._run_backend_with_oracle_fallback(backend, backend_out_dir, source, options.mock_retry_limit)
                if run_result.returncode not in (0, None):
                    result.exit_code = run_result.returncode
                refreshed_feedback = collect_backend_feedback(report_parser, backend_out_dir, source, backend, self._log) or feedback
                refreshed_feedback = merge_generated_coverage_feedback(refreshed_feedback, functions, cases, options.coverage)
                refreshed_feedback = self._merge_run_failures(refreshed_feedback, run_result)
                report_path = writer._write_html_report(source, functions, cases, backend, refreshed_feedback, run_result)
                summary_path = writer._write_summary(source, functions, cases, refreshed_feedback, run_result)
                result.generated_paths.extend([report_path, summary_path])
                result.functions.extend(functions)
                result.cases.extend(cases)
                result.feedback = refreshed_feedback
                result.summary_lines.append(
                    f"{source.name}: added manual testcase {manual_case.test_id}, total {len(cases)} cases, {format_run_result(run_result)}, {coverage_log_summary(refreshed_feedback)}"
                )
                report_items.append({
                    "source": source,
                    "functions": functions,
                    "cases": cases,
                    "feedback": refreshed_feedback,
                    "run_result": run_result,
                    "report_path": report_path,
                })
            except Exception as exc:
                self._log(f"ERROR add manual testcase {source}: {exc}")
                result.summary_lines.append(f"{source.name}: ERROR {exc}")
                result.exit_code = 1
                return result

        if len(report_items) > 1:
            final_report = ArtifactWriter(out_dir / backend)._write_multi_file_report(backend, report_items)
            result.generated_paths.append(final_report)
            self._log(f"All-files report: {final_report}")
        return result

    @staticmethod
    def _manual_case_from_payload(
        source: Path,
        functions: list[FunctionInfo],
        existing_cases: list[TestCase],
        options: OrchestratorOptions,
        payload: dict,
    ) -> TestCase:
        function_name = str(payload.get("function") or "").strip()
        fn = next((item for item in functions if item.name == function_name), None)
        if fn is None:
            raise ValueError(f"Manual testcase function not found: {function_name}")

        raw_id = str(payload.get("test_id") or "").strip()
        if not raw_id:
            raw_id = f"MANUAL_{fn.name}_{len(existing_cases) + 1:03d}"
        safe_id = re.sub(r"[^A-Za-z0-9_]", "_", raw_id).strip("_") or f"MANUAL_{fn.name}_{len(existing_cases) + 1:03d}"
        existing_ids = {case.test_id for case in existing_cases}
        base_id = safe_id
        suffix = 2
        while safe_id in existing_ids:
            safe_id = f"{base_id}_{suffix:02d}"
            suffix += 1

        values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
        values = {str(key): str(value) for key, value in values.items()}
        mode = str(payload.get("mode") or "Steps Only")
        code_or_steps = str(payload.get("code_or_steps") or "").rstrip()
        if mode == "Harness Code" and code_or_steps:
            values["__manual_action_code"] = code_or_steps
        else:
            values["__manual_steps_only"] = "1"
        values["__manual_source"] = "gui"

        coverage = str(payload.get("coverage") or options.coverage or "Manual")
        description = str(payload.get("description") or f"Manual testcase for {fn.name}")
        preconditions = str(payload.get("preconditions") or "Review manual preconditions before execution.")
        expected = str(payload.get("expected") or "Review expected return/output/global side effects.")
        steps = code_or_steps if mode != "Harness Code" else (
            "1. Execute the user-supplied harness code inserted into the generated TEST body.\n"
            "2. Review and maintain all assertions in the manual code block."
        )
        audit = TestVectorGenerator._audit_fields(
            fn,
            coverage,
            options.level,
            len(existing_cases) + 1,
            values,
            "Manual GUI-authored testcase",
            options.asil_level,
            [],
        )
        audit.update({
            "description": description,
            "preconditions": preconditions,
            "setup": "Manual GUI testcase. Apply values JSON and any user-authored harness setup code.",
            "test_steps": steps,
            "teardown": "Reset mocks/stubs and restore modified globals/fixtures after the manual testcase.",
            "pass_fail_criteria": "Pass when all user-authored assertions and reviewed expected outcomes are satisfied.",
            "trace_tags": f"{options.level};{coverage};manual;gui-authored",
        })
        return TestCase(
            test_id=safe_id,
            unit=source.stem,
            function=fn.name,
            coverage=coverage,
            level=options.level,
            values=values,
            expected=expected,
            rationale="Manual testcase added from GUI popup; reviewer owns oracle/assertion correctness.",
            asil=options.asil_level,
            **audit,
        )

    def add_uncovered_cases(self, options: OrchestratorOptions, selected_gap: dict | None = None) -> OrchestratorResult:
        """Add deterministic candidate cases for uncovered lines/branches shown by the latest report.

        If `selected_gap` comes from the GUI tree, only that row is targeted.
        Otherwise all parsed coverage gaps are used. The method rewrites the backend
        harness with the merged case list, reruns it, and regenerates the HTML report.
        """
        result = OrchestratorResult()
        project_sources, include_dirs, defines, compiler = self._resolve_project_inputs(options)
        out_dir = source_scoped_output_dir(options.output_dir.expanduser(), project_sources)
        out_dir.mkdir(parents=True, exist_ok=True)
        if out_dir != options.output_dir.expanduser():
            self._log(f"Output folder: {out_dir}")

        preprocessor = CPreprocessor(compiler, include_dirs, defines)
        analyzer = ClangEnhancedAnalyzer(include_dirs, defines, compiler, self._log)
        generator = TestVectorGenerator()
        report_parser = CoverageReportParser()
        loaded_feedback = load_coverage_feedback(options.report_file, report_parser, self._log)
        backend = options.backend
        unit_sources = self._project_unit_sources(project_sources, include_dirs) if backend in {"CppUTest", "GoogleTest"} else list(project_sources)
        report_items: list[dict] = []

        for source in project_sources:
            try:
                analysis_source = source
                if options.preprocess:
                    analysis_source, note = preprocessor.preprocess_file(source, out_dir / "preprocessed")
                    self._log(note)
                functions = analyzer.analyze(
                    analysis_source,
                    include_static_functions=options.include_static_functions,
                    include_main=options.include_main_function,
                )
                if analysis_source != source:
                    analyzer.apply_original_source_context(functions, source)
                code_database = CodeDatabase.from_functions(source, analysis_source, functions, analyzer=analyzer.last_analyzer)
                backend_out_dir = out_dir / backend
                writer = ArtifactWriter(backend_out_dir)
                try:
                    _saved_functions, existing_cases = self._load_existing_analysis(backend_out_dir, source)
                    self._log(f"Loaded {len(existing_cases)} existing cases for {source.name}.")
                except Exception as exc:
                    self._log(f"Existing analysis not available for {source.name}; generating initial cases first: {exc}")
                    existing_cases = generator.generate(
                        source.name,
                        functions,
                        options.level,
                        options.coverage,
                        options.iso26262_profile,
                        options.asil_level,
                        options.include_static_functions,
                        code_database,
                    )

                feedback = collect_backend_feedback(report_parser, backend_out_dir, source, backend, self._log) or loaded_feedback
                feedback = merge_generated_coverage_feedback(feedback, functions, existing_cases, options.coverage)

                gaps = CoverageGapPlanner().plan(feedback, functions, max_gaps=48)
                if selected_gap:
                    gaps = self._filter_selected_gaps(gaps, selected_gap, functions)
                if not gaps:
                    result.summary_lines.append(f"{source.name}: no uncovered gap was found in the current coverage feedback.")
                    result.functions.extend(functions)
                    result.cases.extend(existing_cases)
                    result.feedback = feedback
                    continue

                new_cases = generator.generate_for_coverage_gaps(
                    source.name,
                    functions,
                    existing_cases,
                    gaps,
                    options.level,
                    options.coverage,
                    options.asil_level,
                    code_database,
                )
                if not new_cases:
                    result.summary_lines.append(f"{source.name}: gaps found, but all generated candidate vectors duplicated existing cases.")
                    result.functions.extend(functions)
                    result.cases.extend(existing_cases)
                    result.feedback = feedback
                    continue

                cases = [*existing_cases, *new_cases]
                paths = writer.write(
                    source,
                    functions,
                    cases,
                    options.level,
                    backend,
                    feedback,
                    None,
                    include_backend=True,
                    unit_sources=unit_sources or [source],
                    include_dirs=include_dirs,
                    target_coverage_percent=options.target_coverage_percent,
                    code_database=code_database,
                )
                result.generated_paths.extend(paths)
                self._log(f"Added {len(new_cases)} coverage-gap candidate cases for {source.name}.")

                run_result = self._run_backend_with_oracle_fallback(backend, backend_out_dir, source, options.mock_retry_limit)
                if run_result.returncode not in (0, None):
                    result.exit_code = run_result.returncode
                refreshed_feedback = collect_backend_feedback(report_parser, backend_out_dir, source, backend, self._log) or feedback
                refreshed_feedback = merge_generated_coverage_feedback(refreshed_feedback, functions, cases, options.coverage)
                refreshed_feedback = self._merge_run_failures(refreshed_feedback, run_result)
                report_path = writer._write_html_report(source, functions, cases, backend, refreshed_feedback, run_result)
                summary_path = writer._write_summary(source, functions, cases, refreshed_feedback, run_result)
                result.generated_paths.extend([report_path, summary_path])
                result.functions.extend(functions)
                result.cases.extend(cases)
                result.feedback = refreshed_feedback
                result.summary_lines.append(
                    f"{source.name}: added {len(new_cases)} gap cases, total {len(cases)} cases, {format_run_result(run_result)}, {coverage_log_summary(refreshed_feedback)}"
                )
                report_items.append({
                    "source": source,
                    "functions": functions,
                    "cases": cases,
                    "feedback": refreshed_feedback,
                    "run_result": run_result,
                    "report_path": report_path,
                })
            except Exception as exc:
                self._log(f"ERROR add uncovered cases {source}: {exc}")
                result.summary_lines.append(f"{source.name}: ERROR {exc}")
                result.exit_code = 1
                return result

        if len(report_items) > 1:
            final_report = ArtifactWriter(out_dir / backend)._write_multi_file_report(backend, report_items)
            result.generated_paths.append(final_report)
            self._log(f"All-files report: {final_report}")
        return result

    @staticmethod
    def _filter_selected_gaps(gaps: list[CoverageGap], selected_gap: dict, functions: list[FunctionInfo]) -> list[CoverageGap]:
        selected_function = str(selected_gap.get("function") or "")
        selected_metric = str(selected_gap.get("metric") or "")
        selected_line = selected_gap.get("line")
        selected_text = str(selected_gap.get("text") or "")
        matched: list[CoverageGap] = []
        for gap in gaps:
            if selected_function and gap.function != selected_function:
                continue
            if selected_metric and selected_metric != gap.metric:
                continue
            if selected_line is not None and gap.line != selected_line:
                continue
            matched.append(gap)
        if matched:
            return matched
        fn = next((item for item in functions if item.name == selected_function), None)
        if fn is None and selected_line is not None:
            fn = next((item for item in functions if item.start_line <= int(selected_line) <= item.end_line), None)
        if fn is None:
            return []
        condition = ""
        if selected_line is not None:
            for item in fn.conditions:
                if item.line == int(selected_line):
                    condition = item.text
                    break
        return [CoverageGap(
            function=fn.name,
            line=int(selected_line) if selected_line is not None else None,
            metric=selected_metric or "C0",
            text=selected_text or f"Selected uncovered row for {fn.name}",
            condition=condition,
        )]

    def _run(self, options: OrchestratorOptions, refine_from_report: bool) -> OrchestratorResult:
        result = OrchestratorResult()
        project_sources, include_dirs, defines, compiler = self._resolve_project_inputs(options)
        out_dir = source_scoped_output_dir(options.output_dir.expanduser(), project_sources)
        out_dir.mkdir(parents=True, exist_ok=True)
        if out_dir != options.output_dir.expanduser():
            self._log(f"Output folder: {out_dir}")
        preprocessor = CPreprocessor(compiler, include_dirs, defines)
        analyzer = ClangEnhancedAnalyzer(include_dirs, defines, compiler, self._log)
        generator = TestVectorGenerator()
        report_parser = CoverageReportParser()
        feedback = load_coverage_feedback(options.report_file, report_parser, self._log)
        backend = options.backend
        unit_sources = self._project_unit_sources(project_sources, include_dirs) if backend in {"CppUTest", "GoogleTest"} else list(project_sources)
        if len(unit_sources) > len(project_sources):
            self._log(f"Auto-linked {len(unit_sources) - len(project_sources)} project source files for {backend} harness build.")
        report_items: list[dict] = []

        for source in project_sources:
            try:
                analysis_source = source
                if options.preprocess:
                    analysis_source, note = preprocessor.preprocess_file(source, out_dir / "preprocessed")
                    self._log(note)
                functions = analyzer.analyze(
                    analysis_source,
                    include_static_functions=options.include_static_functions,
                    include_main=options.include_main_function,
                )
                if analysis_source != source:
                    analyzer.apply_original_source_context(functions, source)
                code_database = CodeDatabase.from_functions(source, analysis_source, functions, analyzer=analyzer.last_analyzer)
                self._log(f"Code database: {len(code_database.functions)} functions extracted for {source.name}.")
                if options.include_static_functions and backend in {"CppUTest", "GoogleTest"} and any(fn.return_type.strip().startswith("static") for fn in functions):
                    self._log("Static functions were included. CppUTest/GTest may require source-inclusion wrappers or visibility changes to call file-local functions.")
                cases = generator.generate(
                    source.name,
                    functions,
                    options.level,
                    options.coverage,
                    options.iso26262_profile,
                    options.asil_level,
                    options.include_static_functions,
                    code_database,
                )
                active_feedback = merge_generated_coverage_feedback(feedback, functions, cases, options.coverage)
                backend_out_dir = out_dir / backend
                writer = ArtifactWriter(backend_out_dir)
                run_result: RunResult | None = None

                if refine_from_report or options.use_ollama:
                    cases = refine_with_ollama(
                        source,
                        functions,
                        cases,
                        active_feedback,
                        options.coverage,
                        options.level,
                        backend,
                        options.ollama_model,
                        options.ollama_timeout_seconds,
                        options.target_coverage_percent,
                        max(1, options.max_ai_rounds),
                        self._log,
                        require_report=refine_from_report,
                        code_database=code_database,
                    )
                    active_feedback = merge_generated_coverage_feedback(active_feedback, functions, cases, options.coverage)

                source_paths = writer.write(
                    source,
                    functions,
                    cases,
                    options.level,
                    backend,
                    active_feedback,
                    run_result,
                    include_backend=True,
                    unit_sources=unit_sources or [source],
                    include_dirs=include_dirs,
                    target_coverage_percent=options.target_coverage_percent,
                    code_database=code_database,
                )
                result.generated_paths.extend(source_paths)
                report_path = next(
                    (path for path in reversed(source_paths) if path.suffix.lower() in {".html", ".htm"}),
                    None,
                )
                self._log(f"{backend} output folder: {backend_out_dir}")
                self._log(f"{backend} code database: {backend_out_dir / (source.stem + '_code_database.json')}")
                self._log(f"{backend} cases CSV: {backend_out_dir / (source.stem + '_' + backend_csv_id(backend) + '_cases.csv')}")
                self._log(f"{backend} runner: {backend_harness_path(backend_out_dir, source, backend)}")

                should_run = options.run_after_generate or refine_from_report
                if should_run:
                    run_result = self._run_backend_with_oracle_fallback(backend, backend_out_dir, source, options.mock_retry_limit)
                    if run_result.returncode not in (0, None):
                        result.exit_code = run_result.returncode
                    active_feedback = collect_backend_feedback(report_parser, backend_out_dir, source, backend, self._log) or active_feedback
                    active_feedback = merge_generated_coverage_feedback(active_feedback, functions, cases, options.coverage)
                    active_feedback = self._merge_run_failures(active_feedback, run_result)
                    report_path = writer._write_html_report(source, functions, cases, backend, active_feedback, run_result)
                    result.generated_paths.append(report_path)
                    result.generated_paths.append(writer._write_summary(source, functions, cases, active_feedback, run_result))
                    if run_result.returncode not in (0, None) or (run_result.returncode is None and run_result.skipped_reason):
                        result.exit_code = run_result.returncode or 1
                        result.summary_lines.append(f"{source.name}: ERROR {format_run_result(run_result)}")
                        return result

                result.functions.extend(functions)
                result.cases.extend(cases)
                result.feedback = active_feedback  # Store coverage feedback for GUI display
                report_items.append(
                    {
                        "source": source,
                        "functions": functions,
                        "cases": cases,
                        "feedback": active_feedback,
                        "run_result": run_result,
                        "report_path": report_path,
                    }
                )
                self._log(f"{source.name}: {len(functions)} functions, {len(cases)} test cases, backend {backend}")
                if refine_from_report and run_result is not None:
                    result.summary_lines.append(
                        f"{source.name}: {len(functions)} functions, {len(cases)} cases, {format_run_result(run_result)}, {coverage_log_summary(active_feedback)}"
                    )
                else:
                    result.summary_lines.append(
                        f"{source.name}: {len(functions)} functions, {len(cases)} cases, {backend}, {coverage_log_summary(active_feedback)}"
                    )
            except Exception as exc:
                self._log(f"ERROR {source}: {exc}")
                result.summary_lines.append(f"{source.name}: ERROR {exc}")
                result.exit_code = 1
                return result

        if len(report_items) > 1:
            final_report = ArtifactWriter(out_dir / backend)._write_multi_file_report(backend, report_items)
            result.generated_paths.append(final_report)
            self._log(f"All-files report: {final_report}")
            result.summary_lines.append(f"All files: {len(report_items)} source reports collected in {final_report}")

        return result

    def _resolve_project_inputs(self, options: OrchestratorOptions) -> tuple[list[Path], list[str], list[str], str]:
        sources = list(options.sources)
        include_dirs = list(options.include_dirs)
        defines = list(options.defines)
        compiler = options.compiler.strip() or DEFAULT_COMPILER

        if options.compile_commands:
            try:
                database = CompileCommandsDatabase.load(options.compile_commands)
                compile_sources = database.source_files()
                sources.extend(compile_sources)
                include_dirs.extend(database.include_dirs())
                defines.extend(database.defines())
                database_compiler = database.compiler()
                if database_compiler and not options.compiler.strip():
                    compiler = database_compiler
                self._log(
                    f"Loaded compile_commands.json: {len(compile_sources)} source files, "
                    f"{len(database.include_dirs())} include dirs, {len(database.defines())} defines."
                )
            except Exception as exc:
                self._log(f"compile_commands.json could not be loaded: {exc}")

        sources = self._unique_existing_sources(sources)
        auto_include_dirs = discover_include_dirs_for_sources(sources)
        include_dirs = self._unique_strings([*include_dirs, *auto_include_dirs])
        defines = self._unique_strings(defines)
        if auto_include_dirs:
            self._log(f"Auto include dirs: {len(auto_include_dirs)} discovered from source parents/include-inc-header folders.")
        return sources, include_dirs, defines, compiler

    @staticmethod
    def _unique_existing_sources(sources: list[Path]) -> list[Path]:
        result: list[Path] = []
        seen: set[Path] = set()
        for source in sources:
            path = source.expanduser()
            if path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.exists() or resolved in seen:
                continue
            seen.add(resolved)
            result.append(resolved)
        return result

    @staticmethod
    def _unique_strings(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            clean = str(value).strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            result.append(clean)
        return result

    @staticmethod
    def _project_unit_sources(sources: list[Path], include_dirs: list[str]) -> list[Path]:
        discovered: list[Path] = []
        seen: set[Path] = set()

        def add(path: Path) -> None:
            if path.suffix.lower() not in C_SOURCE_EXTENSIONS or not path.exists():
                return
            resolved = path.resolve()
            if resolved in seen:
                return
            seen.add(resolved)
            discovered.append(path)

        for source in sources:
            add(source)

        project_roots: list[Path] = []
        for include_dir in include_dirs:
            inc = Path(include_dir).expanduser()
            if inc.exists() and inc.is_dir() and inc.name.lower() in {"include", "inc", "includes"}:
                project_roots.append(inc.parent)

        for root in project_roots:
            src_dir = root / "src"
            if not src_dir.exists():
                continue
            for extension in C_SOURCE_EXTENSIONS:
                for candidate in src_dir.rglob(f"*{extension}"):
                    if any(part.lower() in {"generated", "build", "out"} for part in candidate.parts):
                        continue
                    add(candidate)

        return discovered

    @staticmethod
    def _load_existing_analysis(backend_out_dir: Path, source: Path) -> tuple[list[FunctionInfo], list[TestCase]]:
        analysis_path = backend_out_dir / f"{source.stem}_analysis.json"
        if not analysis_path.exists():
            raise FileNotFoundError(f"Existing analysis artifact not found: {analysis_path}. Run Analyze & Generate first.")
        payload = json.loads(analysis_path.read_text(encoding="utf-8"))
        functions = [Orchestrator._function_from_payload(item) for item in payload.get("functions", [])]
        cases = [Orchestrator._case_from_payload(item) for item in payload.get("test_cases", [])]
        if not cases:
            raise ValueError(f"No saved test cases were found in {analysis_path}.")
        return functions, cases

    @staticmethod
    def _function_from_payload(item: dict) -> FunctionInfo:
        return FunctionInfo(
            name=str(item.get("name") or ""),
            return_type=str(item.get("return_type") or "void"),
            parameters=[Parameter(**param) for param in item.get("parameters", [])],
            start_line=int(item.get("start_line") or 0),
            end_line=int(item.get("end_line") or 0),
            body=str(item.get("body") or item.get("body_excerpt") or ""),
            conditions=[Condition(**condition) for condition in item.get("conditions", [])],
        )

    @staticmethod
    def _case_from_payload(item: dict) -> TestCase:
        fields = TestCase.__dataclass_fields__
        values = {name: item.get(name) for name in fields if item.get(name) is not None}
        values["test_id"] = str(values.get("test_id") or "TC_RELOADED")
        values["unit"] = str(values.get("unit") or "")
        values["function"] = str(values.get("function") or "")
        values["coverage"] = str(values.get("coverage") or "MC/DC")
        values["level"] = str(values.get("level") or "UT")
        values["values"] = values.get("values") if isinstance(values.get("values"), dict) else {}
        values["expected"] = str(values.get("expected") or "Review and replace with concrete expected output/test oracle")
        values["rationale"] = str(values.get("rationale") or "Reloaded from existing Testing Mate analysis artifact")
        return TestCase(**values)


def main() -> None:
    app = TestingMateApp()
    app.mainloop()


if __name__ == "__main__":
    main()
