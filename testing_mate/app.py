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

from testing_mate.constants import (
    APP_NAME,
    DEFAULT_COMPILER,
    DEFAULT_ENHANCEMENT_STAGES,
    C_SOURCE_EXTENSIONS,
    HEADER_SOURCE_EXTENSIONS,
    SOURCE_EXTENSIONS,
)
from testing_mate.models import CoverageFeedback, CoverageGap, Condition, FunctionInfo, Parameter, RunResult, TestCase
from testing_mate.c_backend import *
from testing_mate.generator import TestBackendGenerator, TestBackendRunner, TestOracleFallback, backend_harness_path, format_run_result
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
        right.rowconfigure(2, weight=1)
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

        ttk.Label(left, text="Coverage & Level", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
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
        ttk.Checkbutton(left, text="Use local Ollama", variable=self.use_ollama, command=self._ollama_select).pack(anchor="w")

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
        tree_scroll = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=2, column=1, sticky="ns")

        log_frame = ttk.Frame(right, style="Surface.TFrame", padding=12)
        log_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
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
        thread = threading.Thread(target=self._refine_and_run_from_report, daemon=True)
        thread.start()

    def _rerun_async(self) -> None:
        if not self.sources and not self.compile_commands.get().strip():
            messagebox.showwarning(APP_NAME, "Add source files, a source folder, or compile_commands.json.")
            return
        thread = threading.Thread(target=self._rerun_existing_testcases, daemon=True)
        thread.start()

    def _run(self) -> None:
        self._set_log("")
        self._clear_tree()
        result = Orchestrator(self._append_log).analyze_and_generate(self._orchestrator_options())
        self.generated_paths = result.generated_paths
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
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
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
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
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
        if result.generated_paths:
            self._append_log("Refreshed reports:")
            for path in result.generated_paths:
                self._append_log(f"  {path}")
        title = "Rerun testcases failed" if result.exit_code else "Rerun testcases complete"
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
            target_coverage_percent=self._float_setting(self.target_coverage.get(), 100.0),
            max_ai_rounds=max(1, int(self._float_setting(self.max_ai_rounds.get(), 1.0))),
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
        self.after(0, lambda: self.tree.delete(*self.tree.get_children()))

    def _populate_tree(self, functions: list[FunctionInfo] | None = None, feedback: CoverageFeedback | None = None) -> None:
        def insert() -> None:
            self.tree.delete(*self.tree.get_children())
            if not functions:
                return
            
            uncovered_items = feedback.uncovered_items if feedback else []
            
            # Populate coverage obligations from functions
            for fn in functions:
                # Add C0 obligation (statement coverage for function body)
                status = "Covered" if not any("C0" in item and fn.name in item for item in uncovered_items) else "Uncovered"
                self.tree.insert("", "end", values=(fn.name, f"{fn.start_line}-{fn.end_line}", "C0", f"Statement coverage ({fn.start_line}-{fn.end_line})", status))
                
                # Add C1 and MC/DC obligations (conditions)
                for i, condition in enumerate(fn.conditions):
                    c1_status = "Uncovered"
                    mcdc_status = "Uncovered"
                    
                    for item in uncovered_items:
                        if "C1" in item and str(condition.line) in item:
                            c1_status = "Uncovered"
                            break
                    else:
                        c1_status = "Covered"
                    
                    for item in uncovered_items:
                        if "MC/DC" in item and str(condition.line) in item:
                            mcdc_status = "Uncovered"
                            break
                    else:
                        mcdc_status = "Covered"
                    
                    # Show C1 condition
                    self.tree.insert("", "end", values=(fn.name, condition.line, "C1", condition.text, c1_status))
                    # Show MC/DC condition  
                    self.tree.insert("", "end", values=(fn.name, condition.line, "MC/DC", condition.text, mcdc_status))

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
    target_coverage_percent: float = 100.0
    max_ai_rounds: int = 1


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

    def _run_backend_with_oracle_fallback(self, backend: str, backend_out_dir: Path, source: Path) -> RunResult:
        runner = TestBackendRunner()
        run_result = runner.run(backend, backend_out_dir, source)
        self._log(format_run_result(run_result))
        self._log_run_output(run_result)
        if run_result.returncode not in (0, None):
            updated, notes = TestOracleFallback.apply(backend, backend_out_dir, source, run_result)
            for note in notes:
                self._log(note)
            if updated:
                self._log(f"Oracle fallback patched {updated} expected value(s); rerunning {backend}.")
                second_result = runner.run(backend, backend_out_dir, source)
                fallback_log = "\n".join(["", "=== Testing Mate Oracle Fallback ===", *notes, format_run_result(second_result)])
                second_result.stdout = (second_result.stdout + fallback_log)[-12000:]
                run_result = second_result
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
                run_result = self._run_backend_with_oracle_fallback(backend, backend_out_dir, source)
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
                    run_result = self._run_backend_with_oracle_fallback(backend, backend_out_dir, source)
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
