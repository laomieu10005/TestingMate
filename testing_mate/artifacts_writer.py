
from __future__ import annotations
import csv
import html
from pathlib import Path
from datetime import datetime
from testing_mate.models import CoverageFeedback, CoverageGap, Condition, FunctionInfo, Parameter, RunResult, TestCase
from testing_mate.code_database import CodeDatabase
from testing_mate.c1_engine import C1CoverageEngine
from testing_mate.constants import DEFAULT_ENHANCEMENT_STAGES, RERUN_CMD
from testing_mate.generator import TestVectorGenerator, TestBackendGenerator
from testing_mate.vector_generator import TestCaseRegistry
from testing_mate.coverage_gap import CoverageGapPlanner
from dataclasses import asdict
from testing_mate.llm import OllamaTestRefiner
import json 
import re


class ArtifactWriter:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _cases_request_mcdc(cases: list[TestCase]) -> bool:
        return any(str(case.coverage).strip().upper() in {"MC/DC", "MCDC"} for case in cases)

    def write(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        level: str,
        backend: str = "CppUTest",
        feedback: CoverageFeedback | None = None,
        run_result: RunResult | None = None,
        include_backend: bool = True,
        unit_sources: list[Path] | None = None,
        include_dirs: list[str] | None = None,
        target_coverage_percent: float = 100.0,
        enhancement_stages: list[str] | None = None,
        code_database: CodeDatabase | None = None,
    ) -> list[Path]:
        source = source if isinstance(source, Path) else Path(str(source))
        unit_sources = [item if isinstance(item, Path) else Path(str(item)) for item in (unit_sources or []) if item]
        TestCaseRegistry.ensure_unique_test_ids(cases)
        active_feedback = feedback or CoverageFeedback(source="none", raw_text="")
        if any(case.coverage in {"C0/C1", "C1", "MC/DC"} for case in cases) and active_feedback.metrics.get("C1") is None:
            active_feedback = C1CoverageEngine.merge(active_feedback, C1CoverageEngine().analyze(functions, cases))
        if any(case.coverage == "MC/DC" for case in cases) and active_feedback.metrics.get("MC/DC") is None:
            notes = dict(active_feedback.metric_notes)
            notes["MC/DC"] = "MC/DC unavailable: LLVM native MC/DC records were not found. LCOV/gcov branch counters and generated vectors are not used as official MC/DC coverage."
            active_feedback = CoverageFeedback(
                source=active_feedback.source,
                raw_text=active_feedback.raw_text,
                coverage_percent=active_feedback.coverage_percent,
                metrics=active_feedback.metrics,
                metric_notes=notes,
                uncovered_items=active_feedback.uncovered_items,
                failures=active_feedback.failures,
            )
        paths = [
            self._write_code_database(source, code_database),
            self._write_json(source, functions, cases, code_database, active_feedback),
            self._write_enhancement_request(
                source,
                functions,
                cases,
                level,
                backend,
                active_feedback,
                target_coverage_percent,
                enhancement_stages or DEFAULT_ENHANCEMENT_STAGES,
                code_database,
            ),
            self._write_summary(source, functions, cases, active_feedback, run_result),
        ]
        paths.append(self._write_csv(source, cases, backend.lower()))
        if include_backend:
            paths.extend(TestBackendGenerator(self.out_dir, include_dirs).write_backend(backend, source, functions, cases, level, unit_sources, code_database))
        paths.append(self._write_html_report(source, functions, cases, backend, active_feedback, run_result))
        paths.append(self._write_audit_checklist(source, functions, cases, backend))
        return paths

    def _write_code_database(self, source: Path, code_database: CodeDatabase | None) -> Path:
        path = self.out_dir / f"{source.stem}_code_database.json"
        payload = code_database.to_dict() if code_database is not None else {
            "version": "1.0",
            "source_file": source.name,
            "source_path": str(source),
            "analysis_source_path": str(source),
            "analyzer": "unavailable",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "functions": [],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _write_csv(self, source: Path, cases: list[TestCase], backend_id: str) -> Path:
        safe_backend = re.sub(r"[^A-Za-z0-9]+", "_", backend_id).strip("_").lower()
        path = self.out_dir / f"{source.stem}_{safe_backend}_cases.csv"
        fieldnames = [
            "test_id",
            "unit",
            "function",
            "level",
            "coverage",
            "artifact_status",
            "oracle_status",
            "execution_status",
            "parameter",
            "value",
            "description",
            "preconditions",
            "setup",
            "test_steps",
            "expected",
            "teardown",
            "pass_fail_criteria",
            "requirement_ref",
            "safety_goal_ref",
            "asil",
            "aspice_level",
            "trace_tags",
            "rationale",
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for case in cases:
                common = {
                    "test_id": case.test_id,
                    "unit": case.unit,
                    "function": case.function,
                    "level": case.level,
                    "coverage": case.coverage,
                    "artifact_status": case.artifact_status,
                    "oracle_status": case.oracle_status,
                    "execution_status": case.execution_status,
                    "description": case.description,
                    "preconditions": case.preconditions,
                    "setup": case.setup,
                    "test_steps": case.test_steps,
                    "expected": case.expected,
                    "teardown": case.teardown,
                    "pass_fail_criteria": case.pass_fail_criteria,
                    "requirement_ref": case.requirement_ref,
                    "safety_goal_ref": case.safety_goal_ref,
                    "asil": case.asil,
                    "aspice_level": case.aspice_level,
                    "trace_tags": case.trace_tags,
                    "rationale": case.rationale,
                }
                if case.values:
                    for name, value in case.values.items():
                        writer.writerow({**common, "parameter": name, "value": value})
                else:
                    writer.writerow({**common, "parameter": "", "value": ""})
        return path

    def _write_json(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        code_database: CodeDatabase | None = None,
        feedback: CoverageFeedback | None = None,
    ) -> Path:
        path = self.out_dir / f"{source.stem}_analysis.json"
        records = {record.name: asdict(record) for record in code_database.functions} if code_database else {}
        payload = {
            "source": str(source),
            "functions": [
                {
                    "name": fn.name,
                    "return_type": fn.return_type,
                    "parameters": [asdict(param) for param in fn.parameters],
                    "start_line": fn.start_line,
                    "end_line": fn.end_line,
                    "conditions": [asdict(condition) for condition in fn.conditions],
                    "analysis": records.get(fn.name, {}),
                }
                for fn in functions
            ],
            "test_cases": [asdict(case) for case in cases],
            "execution_layers": list(getattr(feedback, "execution_layers", []) or []),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _write_enhancement_request(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        level: str,
        backend: str,
        feedback: CoverageFeedback,
        target_coverage_percent: float,
        enhancement_stages: list[str],
        code_database: CodeDatabase | None = None,
    ) -> Path:
        path = self.out_dir / f"{source.stem}_enhancement_request.json"
        records = {record.name: record for record in code_database.functions} if code_database else {}
        payload = {
            "version": "1.0",
            "source_file": source.name,
            "source_path": str(source),
            "analysis_timestamp": datetime.now().isoformat(timespec="seconds"),
            "coverage_target": self._primary_coverage(cases),
            "asil_level": self._primary_asil(cases),
            "test_level": level,
            "target_coverage_percent": target_coverage_percent,
            "backend": backend,
            "enhancement_stages": enhancement_stages,
            "code_database_file": f"{source.stem}_code_database.json",
            "code_database_summary": (
                {
                    "analyzer": code_database.analyzer,
                    "function_count": len(code_database.functions),
                    "functions": [function.name for function in code_database.functions[:20]],
                    "execution_path_count": sum(len(function.execution_paths) for function in code_database.functions),
                    "mock_requirement_count": sum(len(function.mock_requirements) for function in code_database.functions),
                }
                if code_database is not None
                else {"analyzer": "unavailable", "function_count": len(functions), "functions": [fn.name for fn in functions[:20]]}
            ),
            "ollama_handoff": self._ollama_handoff(
                source,
                functions,
                cases,
                backend,
                feedback,
                target_coverage_percent,
                code_database,
            ),
            "functions": [
                {
                    "name": fn.name,
                    "return_type": fn.return_type,
                    "parameters": [asdict(param) for param in fn.parameters],
                    "conditions": [asdict(condition) for condition in fn.conditions],
                    "body_excerpt": self._body_excerpt(fn.body),
                    "complexity": self._cyclomatic_complexity(fn),
                    "allowed_pointer_fields": TestVectorGenerator._pointer_field_defaults(fn),
                    "calls": records[fn.name].calls if fn.name in records else [],
                    "external_calls": records[fn.name].external_calls if fn.name in records else [],
                    "local_variables": records[fn.name].local_variables if fn.name in records else [],
                    "globals_read": records[fn.name].globals_read if fn.name in records else [],
                    "globals_written": records[fn.name].globals_written if fn.name in records else [],
                    "execution_paths": records[fn.name].execution_paths if fn.name in records else [],
                    "data_dependencies": records[fn.name].data_dependencies if fn.name in records else [],
                    "mock_requirements": records[fn.name].mock_requirements if fn.name in records else [],
                }
                for fn in functions
            ],
            "existing_cases": [
                {
                    "test_id": case.test_id,
                    "function": case.function,
                    "coverage": case.coverage,
                    "values": case.values,
                    "expected": case.expected,
                    "source": "local_engine",
                    "rationale": case.rationale,
                }
                for case in cases
            ],
            "coverage_metrics": {
                "C0": feedback.metrics.get("C0"),
                "C1": feedback.metrics.get("C1"),
                "MC/DC": feedback.metrics.get("MC/DC"),
                "coverage_percent": feedback.coverage_percent,
                "uncovered_items": feedback.uncovered_items,
                "failures": feedback.failures,
                "metric_notes": feedback.metric_notes,
                "source": feedback.source,
            },
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _ollama_handoff(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        backend: str,
        feedback: CoverageFeedback,
        target_coverage_percent: float,
        code_database: CodeDatabase | None,
    ) -> dict:
        coverage = self._primary_coverage(cases)
        by_name = {fn.name: fn for fn in functions}
        chunks: list[dict] = []
        for gap in CoverageGapPlanner().plan(feedback, functions, max_gaps=12):
            fn = by_name.get(gap.function)
            if not fn:
                continue
            chunks.append(
                OllamaTestRefiner.gap_request(
                    source.name,
                    fn,
                    cases,
                    feedback,
                    gap,
                    coverage,
                    target_coverage_percent,
                    backend,
                    code_database,
                )
            )

        mode = "coverage_gap_chunks"
        if not chunks:
            mode = "initial_function_chunks"
            for fn in functions[:12]:
                recent_cases = [case for case in cases if case.function == fn.name][-6:]
                chunks.append(
                    {
                        "task": f"Enhance initial {backend} C unit-test generation for this function chunk only.",
                        "request_type": "initial_function_chunk",
                        "chunk_id": re.sub(r"[^A-Za-z0-9]+", "_", f"{source.stem}_{fn.name}_initial").strip("_").lower(),
                        "source": source.name,
                        "backend": backend,
                        "coverage_target": coverage,
                        "target_percent": target_coverage_percent,
                        "coverage_feedback_chunk": OllamaTestRefiner._compact_feedback(feedback, bool(feedback.uncovered_items or feedback.failures)),
                        "function": {
                            "name": fn.name,
                            "return_type": fn.return_type,
                            "parameters": [asdict(param) for param in fn.parameters],
                            "conditions": [asdict(condition) for condition in fn.conditions[:8]],
                            "allowed_pointer_fields": TestVectorGenerator._pointer_field_defaults(fn),
                            "fixit_hints": getattr(fn, "fixit_hints", [])[:20],
                        },
                        "code_database_chunk": (
                            code_database.function_chunk(fn.name, cases)
                            if code_database is not None
                            else {
                                "source_file": source.name,
                                "analyzer": "function_info",
                                "functions": [OllamaTestRefiner._function_chunk(fn, 8)],
                                "recent_engine_cases": [OllamaTestRefiner._case_chunk(case) for case in recent_cases],
                            }
                        ),
                        "recent_existing_cases_for_function": [OllamaTestRefiner._case_chunk(case) for case in recent_cases],
                        "dedupe_against": [OllamaTestRefiner._case_signature(case) for case in recent_cases],
                        "acceptance_rules": OllamaTestRefiner._acceptance_rules(backend),
                        "response_contract": OllamaTestRefiner._response_contract(),
                    }
                )

        return {
            "version": "1.1",
            "mode": mode,
            "model_family": "ollama",
            "chunk_count": len(chunks),
            "max_cases_per_chunk": 4,
            "coverage_first": True,
            "response_contract": OllamaTestRefiner._response_contract(),
            "chunks": chunks,
        }

    @staticmethod
    def _primary_coverage(cases: list[TestCase]) -> str:
        for case in cases:
            if case.coverage in {"C0", "C0/C1", "C1", "MC/DC"}:
                return case.coverage
        return cases[0].coverage if cases else "MC/DC"

    @staticmethod
    def _primary_asil(cases: list[TestCase]) -> str:
        return cases[0].asil.replace("ASIL-", "ASIL ") if cases else "ASIL D"

    @staticmethod
    def _body_excerpt(body: str, limit: int = 1200) -> str:
        compact = " ".join(body.split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    @staticmethod
    def _cyclomatic_complexity(fn: FunctionInfo) -> int:
        decisions = sum(max(1, len(condition.decisions) or 1) for condition in fn.conditions)
        return 1 + decisions

    def _backend_test_dir(self, source: Path, backend: str) -> Path:
        if backend == "CppUTest":
            return self.out_dir / f"{source.stem}_cpputest"
        if backend == "GoogleTest":
            return self.out_dir / f"{source.stem}_gtest"
        return self.out_dir

    @staticmethod
    def _metric_level(value: float | None) -> str:
        if value is None:
            return "na"
        if value >= 90.0:
            return "good"
        if value >= 70.0:
            return "warn"
        return "bad"

    @staticmethod
    def _metric_bar_width(value: float | None) -> str:
        if value is None:
            return "0"
        return f"{max(0.0, min(100.0, float(value))):.2f}"

    @staticmethod
    def _percent_from_llvm_metric(metric: object) -> float | None:
        if not isinstance(metric, dict):
            return None
        for key in ("percent", "coverage", "ratio"):
            if key in metric:
                try:
                    return float(metric[key])
                except (TypeError, ValueError):
                    pass
        count = metric.get("count", metric.get("total", metric.get("found")))
        covered = metric.get("covered", metric.get("hit", metric.get("executed")))
        try:
            count_f = float(count)
            covered_f = float(covered)
        except (TypeError, ValueError):
            return None
        if count_f <= 0:
            return None
        return (covered_f / count_f) * 100.0

    def _read_backend_metrics(self, source: Path, backend: str) -> tuple[dict[str, float | None], dict[str, str]]:
        """Read executed C0/C1/MC/DC metrics from backend artifacts when present.

        The normal feedback object is still the primary data source. This helper makes
        the HTML report useful when it is regenerated after a backend run and the
        llvm-cov artifacts already exist under the backend folder.
        """
        base_dir = self._backend_test_dir(source, backend)
        metrics: dict[str, float | None] = {"C0": None, "C1": None, "MC/DC": None}
        notes: dict[str, str] = {}

        json_path = None
        for candidate in (base_dir / "coverage.llvm.json", base_dir / "coverage.json"):
            if candidate.exists():
                json_path = candidate
                break

        if json_path:
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
                data = payload.get("data") if isinstance(payload, dict) else None
                block = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
                totals = block.get("totals") if isinstance(block.get("totals"), dict) else {}

                # Prefer target-file summaries if available, otherwise fall back to totals.
                file_summary = {}
                files = block.get("files")
                if isinstance(files, list):
                    target_name = source.name.replace("\\", "/").lower()
                    for item in files:
                        if not isinstance(item, dict):
                            continue
                        filename = str(item.get("filename") or item.get("name") or "").replace("\\", "/").lower()
                        if filename.endswith("/" + target_name) or filename == target_name or filename.endswith(target_name):
                            summary = item.get("summary")
                            if isinstance(summary, dict):
                                file_summary = summary
                                break

                for display_key, json_key in (("C0", "lines"), ("C1", "branches"), ("MC/DC", "mcdc")):
                    metric_node = file_summary.get(json_key) if file_summary else totals.get(json_key)
                    percent = self._percent_from_llvm_metric(metric_node)
                    if percent is not None:
                        metrics[display_key] = percent
                        notes[display_key] = f"From {json_path.name}."
            except Exception as exc:
                notes["parser"] = f"Could not parse {json_path.name}: {exc}"

        summary_path = None
        for candidate in (base_dir / "coverage.summary.txt", base_dir / "llvm_report.txt"):
            if candidate.exists():
                summary_path = candidate
                break

        if summary_path:
            try:
                summary_text = summary_path.read_text(encoding="utf-8", errors="replace")
                report_metrics = self._extract_llvm_report_total_metrics(summary_text)
                for key, value in report_metrics.items():
                    if value is not None and metrics.get(key) is None:
                        metrics[key] = value
                        notes[key] = f"From {summary_path.name}."
            except Exception as exc:
                notes["parser"] = f"Could not parse {summary_path.name}: {exc}"

        return metrics, notes

    @staticmethod
    def _extract_llvm_report_total_metrics(text: str) -> dict[str, float | None]:
        metrics: dict[str, float | None] = {"C0": None, "C1": None, "MC/DC": None}
        for raw_line in text.splitlines():
            line = " ".join(raw_line.strip().split())
            if not line.startswith("TOTAL"):
                continue
            percentages = [float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*%", line)]
            # llvm-cov report with region/function/line/branch/mcdc columns normally
            # ends with: Lines %, Branches %, MC/DC %.
            if len(percentages) >= 5:
                metrics["C0"] = percentages[-3]
                metrics["C1"] = percentages[-2]
                metrics["MC/DC"] = percentages[-1]
            elif len(percentages) >= 3:
                metrics["C0"] = percentages[-3]
                metrics["C1"] = percentages[-2]
                metrics["MC/DC"] = percentages[-1]
        return metrics

    def _effective_coverage_metrics(
        self,
        source: Path,
        backend: str,
        feedback: CoverageFeedback | None,
    ) -> tuple[dict[str, float | None], dict[str, str]]:
        metrics: dict[str, float | None] = {"C0": None, "C1": None, "MC/DC": None}
        notes: dict[str, str] = {}

        if feedback:
            metrics.update(feedback.metrics or {})
            notes.update(feedback.metric_notes or {})

        backend_metrics, backend_notes = self._read_backend_metrics(source, backend)
        for key, value in backend_metrics.items():
            if value is not None:
                metrics[key] = value
                notes[key] = backend_notes.get(key, notes.get(key, "From backend coverage artifacts."))

        if metrics.get("MC/DC") is None:
            notes["MC/DC"] = notes.get(
                "MC/DC",
                "MC/DC unavailable: use Clang/LLVM source coverage with -fcoverage-mcdc; LCOV/gcov branch counters are not official MC/DC evidence.",
            )

        return metrics, notes

    def _write_html_report(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        backend: str,
        feedback: CoverageFeedback | None,
        run_result: RunResult | None,
    ) -> Path:
        path = self.out_dir / f"{source.stem}.html"
        condition_count = sum(len(fn.conditions) for fn in functions)
        decision_count = sum(max(1, len(condition.decisions) or 1) for fn in functions for condition in fn.conditions)
        metrics, metric_notes = self._effective_coverage_metrics(source, backend, feedback)
        include_mcdc = self._cases_request_mcdc(cases)
        active_metric_keys = ("C0", "C1", "MC/DC") if include_mcdc else ("C0", "C1")
        min_executed_metric = min([metrics.get(key) for key in active_metric_keys if metrics.get(key) is not None], default=None)
        coverage = f"{min_executed_metric:.2f}%" if min_executed_metric is not None else "N/A"

        run_status = "Not run"
        run_status_class = "na"
        if run_result:
            if run_result.returncode is None:
                run_status = f"Skipped: {run_result.skipped_reason}"
                run_status_class = "warn"
            else:
                run_status = "PASS" if run_result.returncode == 0 else f"FAIL ({run_result.returncode})"
                run_status_class = "good" if run_result.returncode == 0 else "bad"

        executed_count = self._executed_test_count(run_result)
        report_case_count = executed_count if executed_count is not None else len(cases)
        report_case_label = "Tests Run" if executed_count is not None else "Tests Generated"

        case_counts: dict[str, int] = {}
        for case in cases:
            case_counts[case.function] = case_counts.get(case.function, 0) + 1

        function_rows: list[str] = []
        function_sections: list[str] = []
        for fn in functions:
            params = ", ".join(f"{param.ctype} {param.name}" for param in fn.parameters) or "void"
            fn_condition_count = len(fn.conditions)
            fn_decision_count = sum(max(1, len(condition.decisions) or 1) for condition in fn.conditions)
            function_rows.append(
                "<tr>"
                f"<td><strong>{html.escape(fn.name)}</strong><div class=\"subtle\">{html.escape(params)}</div></td>"
                f"<td>{fn.start_line}-{fn.end_line}</td>"
                f"<td>{fn_condition_count}</td>"
                f"<td>{fn_decision_count}</td>"
                f"<td>{case_counts.get(fn.name, 0)}</td>"
                "</tr>"
            )
            cond_rows = "".join(
                "<tr>"
                f"<td>{condition.line}</td>"
                f"<td><code>{html.escape(condition.text)}</code></td>"
                f"<td>{max(1, len(condition.decisions) or 1)}</td>"
                "</tr>"
                for condition in fn.conditions
            )
            if not cond_rows:
                cond_rows = "<tr><td colspan=\"3\" class=\"subtle\">No extracted conditions.</td></tr>"
            function_sections.append(
                "<details class=\"function-detail\">"
                f"<summary>{html.escape(fn.name)} <span>lines {fn.start_line}-{fn.end_line}, {fn_condition_count} condition(s)</span></summary>"
                "<div class=\"table-wrap compact\"><table>"
                "<thead><tr><th>Line</th><th>Condition</th><th>Decision Leaves</th></tr></thead>"
                f"<tbody>{cond_rows}</tbody>"
                "</table></div>"
                "</details>"
            )

        coverage_metric_html = self._coverage_metric_cards(feedback, functions, source, backend, include_mcdc)
        coverage_obligation_rows = self._coverage_obligation_rows(functions, include_mcdc)
        coverage_notice = (
            '<div class="notice"><strong>MC/DC rule:</strong> MC/DC is only claimed from LLVM native <code>-fcoverage-mcdc</code> evidence. LCOV/gcov branch counters are not treated as MC/DC.</div>'
            if include_mcdc
            else '<div class="notice"><strong>C0/C1 mode:</strong> MC/DC instrumentation, MC/DC report options, and MC/DC obligations are disabled for this run.</div>'
        )
        worst_metric_note = "Minimum available C0/C1/MC/DC value" if include_mcdc else "Minimum available C0/C1 value"
        uncovered = "".join(f"<li>{html.escape(item)}</li>" for item in (feedback.uncovered_items if feedback else []))
        failures = "".join(f"<li>{html.escape(item)}</li>" for item in (feedback.failures if feedback else []))
        if not uncovered:
            uncovered = "<li class=\"subtle\">No uncovered item was reported by the parser.</li>"
        if not failures:
            failures = "<li class=\"subtle\">No backend failure was reported.</li>"
        stdout = html.escape(run_result.stdout if run_result else "")
        stderr = html.escape(run_result.stderr if run_result else "")
        backend_outputs = self._backend_output_items(source, backend)
        backend_output_html = "".join(f"<li><code>{html.escape(item)}</code></li>" for item in backend_outputs)
        lcov_trace_html = self._lcov_trace_section(source, backend, feedback, functions)

        source_name = html.escape(source.name)
        backend_name = html.escape(backend)
        generated_at = html.escape(datetime.now().isoformat(timespec="seconds"))
        dashboard_metric = html.escape(coverage)
        status_html = html.escape(run_status)
        status_class = html.escape(run_status_class)
        function_rows_html = "".join(function_rows) or "<tr><td colspan=\"5\" class=\"subtle\">No functions were analyzed.</td></tr>"
        function_sections_html = "".join(function_sections) or "<p class=\"subtle\">No function detail available.</p>"
        layer_rows_html = self._execution_layer_rows(feedback)

        content = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Testing Mate Report - @@SOURCE_NAME@@</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #07111f;
      --panel: rgba(15, 23, 42, 0.78);
      --panel-strong: rgba(17, 24, 39, 0.96);
      --line: rgba(148, 163, 184, 0.22);
      --text: #e5e7eb;
      --muted: #94a3b8;
      --head: rgba(30, 41, 59, 0.95);
      --accent: #38bdf8;
      --good: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
      --na: #64748b;
      --shadow: 0 26px 90px rgba(0, 0, 0, 0.38);
    }
    body.light-theme {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: rgba(255, 255, 255, 0.88);
      --panel-strong: rgba(255, 255, 255, 0.98);
      --line: rgba(100, 116, 139, 0.24);
      --text: #0f172a;
      --muted: #475569;
      --head: #e2e8f0;
      --accent: #2563eb;
      --good: #047857;
      --warn: #b45309;
      --bad: #be123c;
      --na: #64748b;
      --shadow: 0 20px 60px rgba(15, 23, 42, 0.14);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(56, 189, 248, 0.22), transparent 34rem),
        radial-gradient(circle at 80% 0%, rgba(34, 197, 94, 0.10), transparent 30rem),
        linear-gradient(180deg, #0f172a 0%, var(--bg) 62%);
      color: var(--text);
    }
    a { color: #7dd3fc; text-decoration: none; font-weight: 700; }
    a:hover { text-decoration: underline; }
    body.light-theme {
      background:
        radial-gradient(circle at top left, rgba(37, 99, 235, 0.15), transparent 34rem),
        radial-gradient(circle at 80% 0%, rgba(16, 185, 129, 0.10), transparent 30rem),
        linear-gradient(180deg, #eaf1fb 0%, var(--bg) 62%);
    }
    body.light-theme a { color: #1d4ed8; }
    header {
      width: min(1480px, calc(100% - 32px));
      margin: 0 auto;
      padding: 30px 0 14px;
    }
    .hero {
      background: linear-gradient(135deg, rgba(15, 23, 42, 0.96), rgba(30, 41, 59, 0.72));
      border: 1px solid var(--line);
      border-radius: 28px;
      padding: clamp(22px, 4vw, 38px);
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }
    .hero:after {
      content: "";
      position: absolute;
      inset: auto -120px -180px auto;
      width: 360px;
      height: 360px;
      background: radial-gradient(circle, rgba(56,189,248,0.22), transparent 62%);
      pointer-events: none;
    }
    .hero-row {
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      flex-wrap: wrap;
      position: relative;
      z-index: 1;
    }
    h1 {
      margin: 0 0 10px;
      font-size: clamp(28px, 4vw, 44px);
      letter-spacing: -0.05em;
      line-height: 1;
    }
    h2 { margin: 0 0 14px; font-size: 20px; letter-spacing: -0.02em; }
    h3 { margin: 18px 0 8px; font-size: 15px; color: #cbd5e1; }
    .meta { color: var(--muted); line-height: 1.7; font-size: 14px; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      padding: 9px 13px;
      border: 1px solid var(--line);
      font-weight: 900;
      letter-spacing: 0.03em;
      background: rgba(15, 23, 42, 0.70);
      white-space: nowrap;
    }
    .pill.good { color: var(--good); background: rgba(34,197,94,0.12); }
    .pill.warn { color: var(--warn); background: rgba(245,158,11,0.12); }
    .pill.bad { color: var(--bad); background: rgba(239,68,68,0.12); }
    .pill.na { color: var(--muted); }
    main {
      width: min(1480px, calc(100% - 32px));
      margin: 0 auto;
      padding: 10px 0 42px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-top: 20px;
      position: relative;
      z-index: 1;
    }
    .card, section, .metric-card, .function-detail {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: 0 18px 55px rgba(0, 0, 0, 0.18);
      backdrop-filter: blur(10px);
    }
    .card {
      padding: 18px;
      min-height: 112px;
    }
    .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-weight: 900;
    }
    .metric {
      margin-top: 8px;
      color: var(--text);
      font-size: clamp(24px, 3vw, 34px);
      font-weight: 950;
      letter-spacing: -0.05em;
      line-height: 1;
    }
    .subtle, .note {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }
    section {
      margin-top: 16px;
      padding: 20px;
      overflow: hidden;
    }
    .metric-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      margin-top: 16px;
    }
    .metric-card {
      padding: 20px;
      overflow: hidden;
    }
    .metric-card .metric-value {
      margin-top: 10px;
      font-size: clamp(32px, 4vw, 48px);
      letter-spacing: -0.06em;
      font-weight: 950;
    }
    .metric-card.good .metric-value { color: var(--good); }
    .metric-card.warn .metric-value { color: var(--warn); }
    .metric-card.bad .metric-value { color: var(--bad); }
    .metric-card.na .metric-value { color: var(--na); }
    .bar {
      margin-top: 16px;
      height: 10px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(148, 163, 184, 0.18);
    }
    .bar-fill {
      height: 100%;
      border-radius: 999px;
      background: var(--accent);
    }
    .metric-card.good .bar-fill { background: var(--good); }
    .metric-card.warn .bar-fill { background: var(--warn); }
    .metric-card.bad .bar-fill { background: var(--bad); }
    .metric-card.na .bar-fill { background: var(--na); width: 0 !important; }
    .table-wrap {
      width: 100%;
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(15, 23, 42, 0.42);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 850px;
      font-size: 13px;
    }
    .compact table { min-width: 680px; }
    th, td {
      border-bottom: 1px solid var(--line);
      border-right: 1px solid var(--line);
      padding: 10px 11px;
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th:last-child, td:last-child { border-right: 0; }
    tr:last-child td { border-bottom: 0; }
    th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: var(--head);
      color: #dbeafe;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 900;
    }
    code, pre {
      font-family: Consolas, "Cascadia Mono", monospace;
    }
    code {
      color: #bae6fd;
      background: rgba(14, 165, 233, 0.10);
      border: 1px solid rgba(125, 211, 252, 0.22);
      border-radius: 7px;
      padding: 2px 5px;
      word-break: break-word;
      white-space: normal;
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      color: #e5e7eb;
      background: rgba(2, 6, 23, 0.92);
      padding: 14px;
      border-radius: 16px;
      border: 1px solid var(--line);
      max-height: 360px;
      overflow: auto;
    }
    .trace-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin: 10px 0 14px;
    }
    .trace-link {
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      padding: 9px 12px;
      border: 1px solid rgba(125, 211, 252, 0.28);
      border-radius: 12px;
      color: #e0f2fe;
      background: rgba(14,165,233,0.12);
      font-weight: 850;
    }
    .lcov-frame {
      width: 100%;
      height: min(840px, 74vh);
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fff;
      margin-top: 12px;
    }
    ul { margin: 8px 0 0; padding-left: 20px; }
    li { margin: 5px 0; overflow-wrap: anywhere; }
    details.function-detail {
      margin-top: 12px;
      padding: 0;
      overflow: hidden;
    }
    details.function-detail summary {
      cursor: pointer;
      padding: 14px 16px;
      font-weight: 850;
      list-style: none;
    }
    details.function-detail summary::-webkit-details-marker { display: none; }
    details.function-detail summary span {
      color: var(--muted);
      font-weight: 500;
      margin-left: 8px;
    }
    details.function-detail[open] summary {
      border-bottom: 1px solid var(--line);
      background: rgba(30, 41, 59, 0.55);
    }
    .notice {
      margin-top: 12px;
      padding: 13px 15px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(245, 158, 11, 0.10);
      color: #fde68a;
    }
    body.light-theme .notice { color: #92400e; background: #fffbeb; }
    body.light-theme h3 { color: #334155; }
    body.light-theme th { color: #0f172a; }
    body.light-theme code { color: #1e40af; background: #eff6ff; border-color: #bfdbfe; }
    body.light-theme pre { color: #0f172a; background: #f8fafc; }
    body.light-theme .table-wrap { background: rgba(255,255,255,0.70); }
    body.light-theme .trace-link { color: #1d4ed8; background: #eff6ff; border-color: #bfdbfe; }
    .toolbar {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      margin-bottom: 12px;
    }
    .theme-toggle {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 10px 14px;
      color: var(--text);
      background: var(--panel);
      cursor: pointer;
      font-weight: 900;
      box-shadow: 0 10px 28px rgba(0,0,0,0.16);
    }
    @media (max-width: 1100px) {
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .metric-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 680px) {
      header, main { width: min(100% - 20px, 1480px); }
      .grid { grid-template-columns: 1fr; }
      .hero { border-radius: 20px; padding: 20px; }
      section { padding: 14px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="toolbar"><button id="themeToggle" class="theme-toggle" type="button">Switch Light Theme</button></div>
    <div class="hero">
      <div class="hero-row">
        <div>
          <h1>Testing Mate Coverage Report</h1>
          <div class="meta">
            <div><strong>Source:</strong> @@SOURCE_NAME@@</div>
            <div><strong>Backend:</strong> @@BACKEND_NAME@@</div>
            <div><strong>Generated:</strong> @@GENERATED_AT@@</div>
            <div><strong>Coverage evidence:</strong> Clang/LLVM source coverage where artifacts exist; legacy LCOV is C0/C1 only.</div>
          </div>
        </div>
        <div class="pill @@STATUS_CLASS@@">● @@RUN_STATUS@@</div>
      </div>
      <div class="grid">
        <div class="card"><div class="label">Functions</div><div class="metric">@@FUNCTION_COUNT@@</div><div class="note">Analyzed units</div></div>
        <div class="card"><div class="label">Conditions</div><div class="metric">@@CONDITION_COUNT@@</div><div class="note">Extracted Boolean decisions</div></div>
        <div class="card"><div class="label">@@CASE_LABEL@@</div><div class="metric">@@CASE_COUNT@@</div><div class="note">Backend execution evidence</div></div>
        <div class="card"><div class="label">Worst Metric</div><div class="metric">@@DASHBOARD_METRIC@@</div><div class="note">@@WORST_METRIC_NOTE@@</div></div>
      </div>
    </div>
  </header>
  <main>
    <section>
      <h2>Coverage Metrics</h2>
      <div class="metric-grid">@@COVERAGE_METRIC_HTML@@</div>
      @@COVERAGE_NOTICE@@
    </section>

    <section>
      <h2>Function Summary</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Function</th><th>Lines</th><th>Conditions</th><th>Decision Leaves</th><th>Cases</th></tr></thead>
          <tbody>@@FUNCTION_ROWS@@</tbody>
        </table>
      </div>
    </section>

    <section>
      <h2>Backend Output</h2>
      <ul>@@BACKEND_OUTPUT_HTML@@</ul>
    </section>

    @@LCOV_TRACE_HTML@@

    <section>
      <h2>Coverage Obligations</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Metric</th><th>Function</th><th>Line</th><th>Obligation</th><th>Evidence Source</th></tr></thead>
          <tbody>@@OBLIGATION_ROWS@@</tbody>
        </table>
      </div>
    </section>

    <section>
      <h2>Functions And Conditions</h2>
      @@FUNCTION_SECTIONS@@
    </section>

    <section>
      <h2>Escalation Layer Evidence</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Layer</th><th>Function</th><th>Candidates</th><th>Backend</th><th>Status</th><th>Reconciled</th><th>Coverage After</th><th>Artifact</th></tr></thead>
          <tbody>@@LAYER_ROWS@@</tbody>
        </table>
      </div>
    </section>

    <section>
      <h2>Coverage Feedback</h2>
      <h3>Uncovered / Gaps</h3>
      <ul>@@UNCOVERED@@</ul>
      <h3>Failures</h3>
      <ul>@@FAILURES@@</ul>
    </section>

    <section>
      <h2>Run Output</h2>
      <h3>stdout</h3>
      <pre>@@STDOUT@@</pre>
      <h3>stderr</h3>
      <pre>@@STDERR@@</pre>
    </section>
  </main>
  <script>
    (function () {
      const key = "testingMateReportTheme";
      const button = document.getElementById("themeToggle");
      function apply(theme) {
        document.body.classList.toggle("light-theme", theme === "light");
        if (button) {
          button.textContent = theme === "light" ? "Switch Dark Theme" : "Switch Light Theme";
        }
      }
      let theme = localStorage.getItem(key) || "dark";
      apply(theme);
      if (button) {
        button.addEventListener("click", function () {
          theme = document.body.classList.contains("light-theme") ? "dark" : "light";
          localStorage.setItem(key, theme);
          apply(theme);
        });
      }
    }());
  </script>
</body>
</html>"""

        content = (
            content
            .replace("@@SOURCE_NAME@@", source_name)
            .replace("@@BACKEND_NAME@@", backend_name)
            .replace("@@GENERATED_AT@@", generated_at)
            .replace("@@STATUS_CLASS@@", status_class)
            .replace("@@RUN_STATUS@@", status_html)
            .replace("@@FUNCTION_COUNT@@", str(len(functions)))
            .replace("@@CONDITION_COUNT@@", str(condition_count))
            .replace("@@CASE_LABEL@@", html.escape(report_case_label))
            .replace("@@CASE_COUNT@@", str(report_case_count))
            .replace("@@DASHBOARD_METRIC@@", dashboard_metric)
            .replace("@@WORST_METRIC_NOTE@@", worst_metric_note)
            .replace("@@COVERAGE_METRIC_HTML@@", coverage_metric_html)
            .replace("@@COVERAGE_NOTICE@@", coverage_notice)
            .replace("@@FUNCTION_ROWS@@", function_rows_html)
            .replace("@@BACKEND_OUTPUT_HTML@@", backend_output_html)
            .replace("@@LCOV_TRACE_HTML@@", lcov_trace_html)
            .replace("@@OBLIGATION_ROWS@@", coverage_obligation_rows)
            .replace("@@FUNCTION_SECTIONS@@", function_sections_html)
            .replace("@@LAYER_ROWS@@", layer_rows_html)
            .replace("@@UNCOVERED@@", uncovered)
            .replace("@@FAILURES@@", failures)
            .replace("@@STDOUT@@", stdout)
            .replace("@@STDERR@@", stderr)
        )
        path.write_text(content, encoding="utf-8")
        return path

    def _execution_layer_rows(self, feedback: CoverageFeedback | None) -> str:
        records = list(getattr(feedback, "execution_layers", []) or []) if feedback else []
        if not records:
            return '<tr><td colspan="8" class="subtle">No escalation backend layer run was recorded.</td></tr>'
        rows: list[str] = []
        for record in records:
            after = record.get("coverage_after", {}) or {}
            metrics = ", ".join(
                f"{html.escape(str(key))}={float(value):.2f}%"
                for key, value in after.items()
                if value is not None
            ) or html.escape(str(record.get("coverage_summary") or "n/a"))
            artifact = html.escape(str(record.get("out_dir") or ""))
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(record.get('layer', '')))}</td>"
                f"<td>{html.escape(str(record.get('function', '')))}</td>"
                f"<td>{html.escape(str(record.get('candidate_count', 0)))}</td>"
                f"<td>{html.escape(str(record.get('backend', '')))}</td>"
                f'<td><span class="pill {self._status_class(str(record.get("run_status", "")))}">{html.escape(str(record.get("run_status", "")))}</span></td>'
                f"<td>{html.escape(str(record.get('reconcile_updates', 0)))}</td>"
                f"<td>{metrics}</td>"
                f"<td><code>{artifact}</code></td>"
                "</tr>"
            )
        return "".join(rows)

    @staticmethod
    def _status_class(status: str) -> str:
        status = status.strip().upper()
        if status == "PASS":
            return "good"
        if status in {"FAIL", "ERROR"}:
            return "bad"
        if status in {"SKIPPED", "NOT_RUN", "NO_CANDIDATES"}:
            return "warn"
        return "na"

    @staticmethod
    def _executed_test_count(run_result: RunResult | None) -> int | None:
        if not run_result or run_result.returncode is None:
            return None
        text = "\n".join(part for part in (run_result.stdout, run_result.stderr) if part)
        patterns = [
            r"(?im)^\s*OK\s*\(\s*(\d+)\s+tests?",
            r"(?im)^\s*\[\s*PASSED\s*\]\s*(\d+)\s+tests?",
            r"(?im)^\s*\[\s*=+\s*\]\s*(\d+)\s+tests?\s+from\b",
            r"(?im)\b(\d+)\s+tests?\s+from\s+\d+\s+test\s+suites?\s+ran\b",
            r"(?im)\b(\d+)\s+tests?\s+ran\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return int(match.group(1))
        return None

    def _coverage_metric_cards(
        self,
        feedback: CoverageFeedback | None,
        functions: list[FunctionInfo],
        source: Path | None = None,
        backend: str = "",
        include_mcdc: bool = True,
    ) -> str:
        if source is not None and backend:
            metrics, notes = self._effective_coverage_metrics(source, backend, feedback)
        else:
            metrics = feedback.metrics if feedback else {}
            notes = feedback.metric_notes if feedback else {}

        obligation_counts = {
            "C0": sum(max(0, fn.end_line - fn.start_line + 1) for fn in functions),
            "C1": sum(max(1, len(condition.decisions) or 1) for fn in functions for condition in fn.conditions),
            "MC/DC": sum(max(1, len(condition.decisions) or 1) for fn in functions for condition in fn.conditions),
        }
        subtitles = {
            "C0": "Line coverage",
            "C1": "Branch coverage",
            "MC/DC": "Native LLVM MC/DC",
        }
        metric_keys = ("C0", "C1", "MC/DC") if include_mcdc else ("C0", "C1")
        cards: list[str] = []
        for metric in metric_keys:
            value = metrics.get(metric)
            level = self._metric_level(value)
            display = f"{value:.2f}%" if value is not None else "N/A"
            note = notes.get(metric) or f"Static obligations: {obligation_counts[metric]}"
            cards.append(
                f'<article class="metric-card {level}">'
                f'<div class="label">{html.escape(metric)}</div>'
                f'<div class="metric-value">{html.escape(display)}</div>'
                f'<div class="note">{html.escape(subtitles[metric])}</div>'
                f'<div class="bar"><div class="bar-fill" style="width: {self._metric_bar_width(value)}%"></div></div>'
                f'<div class="note">{html.escape(note)}</div>'
                f'</article>'
            )
        return "".join(cards)

    def _lcov_trace_section(
        self,
        source: Path,
        backend: str,
        feedback: CoverageFeedback | None,
        functions: list[FunctionInfo],
    ) -> str:
        if backend not in {"CppUTest", "GoogleTest"}:
            return (
                "<section><h2>Source Coverage Evidence</h2>"
                "<p class=\"note\">Source coverage is available for CppUTest and GoogleTest backend runs.</p>"
                "</section>"
            )

        base_dir = self._backend_test_dir(source, backend)
        llvm_dir = base_dir / "llvm_html"
        legacy_llvm_dir = base_dir / f"{source.stem}_llvmcov"
        if not (llvm_dir / "index.html").exists() and (legacy_llvm_dir / "index.html").exists():
            llvm_dir = legacy_llvm_dir

        llvm_index = llvm_dir / "index.html"
        llvm_json = base_dir / "coverage.json"
        llvm_legacy_json = base_dir / "coverage.llvm.json"
        llvm_report = base_dir / "llvm_report.txt"
        llvm_legacy_report = base_dir / "coverage.summary.txt"

        if llvm_index.exists():
            links = [f'<a class="trace-link" href="{html.escape(self._relative_href(llvm_index))}">Open LLVM HTML Report</a>']
            if llvm_json.exists():
                links.append(f'<a class="trace-link" href="{html.escape(self._relative_href(llvm_json))}">Open coverage.json</a>')
            elif llvm_legacy_json.exists():
                links.append(f'<a class="trace-link" href="{html.escape(self._relative_href(llvm_legacy_json))}">Open coverage.llvm.json</a>')
            if llvm_report.exists():
                links.append(f'<a class="trace-link" href="{html.escape(self._relative_href(llvm_report))}">Open llvm_report.txt</a>')
            elif llvm_legacy_report.exists():
                links.append(f'<a class="trace-link" href="{html.escape(self._relative_href(llvm_legacy_report))}">Open coverage.summary.txt</a>')
            trace_rows = self._lcov_trace_rows(self._relative_href(llvm_index), feedback, functions)
            return (
                "<section><h2>LLVM Source Coverage</h2>"
                "<p class=\"note\">Embedded clang/llvm-cov source report. This is the official evidence view for C0/C1 and native MC/DC when built with <code>-fcoverage-mcdc</code>.</p>"
                f"<div class=\"trace-actions\">{''.join(links)}</div>"
                f"{trace_rows}"
                f'<iframe class="lcov-frame" src="{html.escape(self._relative_href(llvm_index))}" title="LLVM source coverage"></iframe>'
                "</section>"
            )

        lcov_dir = base_dir / f"{source.stem}_lcov"
        index_path = lcov_dir / "index.html"
        source_page = self._find_lcov_source_page(lcov_dir, source)
        index_href = self._relative_href(index_path) if index_path.exists() else ""
        source_href = self._relative_href(source_page) if source_page and source_page.exists() else ""
        coverage_info = base_dir / "coverage.info"

        if not index_href:
            expected = f"{base_dir.name}/llvm_html/index.html"
            return (
                "<section><h2>Source Coverage Evidence</h2>"
                f"<p class=\"note\">No source coverage page has been generated yet. Run the backend and regenerate this report to embed <code>{html.escape(expected)}</code>.</p>"
                "</section>"
            )

        trace_rows = self._lcov_trace_rows(source_href, feedback, functions)
        links = [f'<a class="trace-link" href="{html.escape(index_href)}">Open LCOV Report</a>']
        if source_href:
            links.append(f'<a class="trace-link" href="{html.escape(source_href)}">Open Covered C Source</a>')
        if coverage_info.exists():
            links.append(f'<a class="trace-link" href="{html.escape(self._relative_href(coverage_info))}">Open coverage.info</a>')
        iframe_src = source_href or index_href
        return (
            "<section><h2>Legacy LCOV Source Trace</h2>"
            "<p class=\"note\">Legacy lcov/genhtml is C0/C1 evidence only. MC/DC remains N/A in LCOV mode.</p>"
            f"<div class=\"trace-actions\">{''.join(links)}</div>"
            f"{trace_rows}"
            f'<iframe class="lcov-frame" src="{html.escape(iframe_src)}" title="LCOV source coverage"></iframe>'
            "</section>"
        )

    def _lcov_trace_rows(self, source_href: str, feedback: CoverageFeedback | None, functions: list[FunctionInfo]) -> str:
        rows: list[str] = []
        uncovered = feedback.uncovered_items if feedback else []
        for item in uncovered[:80]:
            line = self._line_from_coverage_item(item)
            line_display = str(line) if line is not None else "-"
            link = self._lcov_line_link(source_href, line)
            rows.append(
                "<tr>"
                "<td><span class=\"pill bad\">Gap</span></td>"
                f"<td>{html.escape(line_display)}</td>"
                f"<td>{link}</td>"
                f"<td>{html.escape(item)}</td>"
                "</tr>"
            )
        if not rows:
            for fn in functions[:80]:
                link = self._lcov_line_link(source_href, fn.start_line)
                rows.append(
                    "<tr>"
                    "<td><span class=\"pill na\">Function</span></td>"
                    f"<td>{fn.start_line}-{fn.end_line}</td>"
                    f"<td>{link}</td>"
                    f"<td>{html.escape(fn.name)}</td>"
                    "</tr>"
                )
        return (
            "<div class=\"table-wrap\"><table>"
            "<thead><tr><th>Type</th><th>Line</th><th>Source Link</th><th>Trace Detail</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )

    @staticmethod
    def _find_lcov_source_page(lcov_dir: Path, source: Path) -> Path | None:
        if not lcov_dir.exists():
            return None
        pages = [path for path in lcov_dir.rglob("*.html") if path.name.lower() != "index.html"]
        if not pages:
            return None
        source_name = source.name.lower()
        source_stem = source.stem.lower()
        preferred = [
            path
            for path in pages
            if (source_name in path.name.lower() or source_stem in path.name.lower()) and path.suffix.lower() == ".html"
        ]
        preferred.extend(
            path
            for path in pages
            if source_name in path.name.lower() or source_stem in path.name.lower()
        )
        seen: set[Path] = set()
        ordered = []
        for path in preferred or pages:
            if path not in seen:
                seen.add(path)
                ordered.append(path)
        return ordered[0]

    def _relative_href(self, path: Path) -> str:
        try:
            return path.relative_to(self.out_dir).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _line_from_coverage_item(text: str) -> int | None:
        match = re.search(r"\bline\s+(\d+)\b", text, flags=re.I)
        if match:
            return int(match.group(1))
        match = re.search(r"\b(?:DA|BRDA):(\d+)", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _lcov_line_link(source_href: str, line: int | None) -> str:
        if not source_href:
            return "Open source page after coverage run"
        if line is None:
            return f'<a href="{html.escape(source_href)}">Open source page</a>'
        return f'<a href="{html.escape(source_href)}#{line}">line {line}</a>'

    @staticmethod
    def _coverage_obligation_rows(functions: list[FunctionInfo], include_mcdc: bool = True) -> str:
        rows: list[str] = []
        for fn in functions:
            rows.append(
                "<tr>"
                "<td><span class=\"pill na\">C0</span></td>"
                f"<td>{html.escape(fn.name)}</td>"
                f"<td>{fn.start_line}-{fn.end_line}</td>"
                "<td>Statement/line execution over the function body</td>"
                "<td>LLVM source-based line coverage from llvm-cov export/report</td>"
                "</tr>"
            )
            for condition in fn.conditions:
                decisions = condition.decisions or [condition.text]
                rows.append(
                    "<tr>"
                    "<td><span class=\"pill na\">C1</span></td>"
                    f"<td>{html.escape(fn.name)}</td>"
                    f"<td>{condition.line}</td>"
                    f"<td>True/false branch outcomes for <code>{html.escape(condition.text)}</code></td>"
                    "<td>LLVM source-based branch coverage from llvm-cov export/report</td>"
                    "</tr>"
                )
                if include_mcdc:
                    for decision in decisions:
                        rows.append(
                            "<tr>"
                            "<td><span class=\"pill na\">MC/DC</span></td>"
                            f"<td>{html.escape(fn.name)}</td>"
                            f"<td>{condition.line}</td>"
                            f"<td>Independent effect pair for <code>{html.escape(decision)}</code></td>"
                            "<td>LLVM native MC/DC from -fcoverage-mcdc; N/A when LLVM MC/DC records are absent</td>"
                            "</tr>"
                        )
        if not rows:
            return "<tr><td colspan=\"5\" class=\"subtle\">No coverage obligations were extracted.</td></tr>"
        return "".join(rows)

    def _write_audit_checklist(self, source: Path, functions: list[FunctionInfo], cases: list[TestCase], backend: str) -> Path:
        path = self.out_dir / f"{source.stem}_{backend.lower()}_audit_checklist.md"
        safety_levels = ", ".join(sorted({case.asil for case in cases})) or "not specified"
        lines = [
            f"# {source.name} Audit Checklist",
            "",
            f"- Backend: {backend}",
            "- Target process evidence: ASPICE L2 work product support",
            f"- Target safety integrity: {safety_levels} test design support",
            f"- Functions analyzed: {len(functions)}",
            f"- Test cases generated: {len(cases)}",
            "",
            "## Review Gates",
            "",
            "- [ ] Requirements are linked or imported into `requirement_ref` fields.",
            "- [ ] Safety goals are linked or imported into `safety_goal_ref` fields.",
            "- [ ] Preconditions and setup are reviewed against the actual test environment.",
            "- [ ] Pointer/struct fixtures are implemented where TODO fixture comments exist.",
            "- [ ] Expected outputs are reviewed and replaced with concrete oracle values.",
            "- [ ] Generated tests are executed and pass/fail evidence is archived.",
            "- [ ] Coverage report is attached and gaps are dispositioned.",
            "- [ ] Reviewer approves MC/DC independence pairs for ASIL-D claims.",
            "",
            "## Test Case Evidence",
            "",
        ]
        for case in cases:
            values = ", ".join(f"{name}={value}" for name, value in case.values.items()) or "none"
            lines.extend(
                [
                    f"### {case.test_id}",
                    "",
                    f"- Function: `{case.function}`",
                    f"- Coverage target: {case.coverage}",
                    f"- Input vector: {values}",
                    f"- Description: {case.description}",
                    f"- Preconditions: {case.preconditions}",
                    f"- Setup: {case.setup}",
                    f"- Test steps: {case.test_steps}",
                    f"- Expected output: {case.expected}",
                    f"- Teardown: {case.teardown}",
                    f"- Pass/fail criteria: {case.pass_fail_criteria}",
                    f"- Requirement ref: {case.requirement_ref}",
                    f"- Safety goal ref: {case.safety_goal_ref}",
                    f"- Safety/process tags: {case.asil}, {case.aspice_level}",
                    f"- Rationale: {case.rationale}",
                    "",
                ]
            )
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _write_multi_file_report(self, backend: str, items: list[dict]) -> Path:
        path = self.out_dir / "all_files_report.html"
        total_functions = sum(len(item["functions"]) for item in items)
        total_cases = sum(len(item["cases"]) for item in items)
        total_conditions = sum(len(fn.conditions) for item in items for fn in item["functions"])
        metric_cells = self._aggregate_metric_cells(items)
        rows: list[str] = []
        for item in items:
            source = item["source"]
            functions = item["functions"]
            cases = item["cases"]
            feedback = item["feedback"]
            report_path = item.get("report_path")
            run_result = item.get("run_result")
            status = "Not run"
            if run_result:
                if run_result.returncode is None:
                    status = f"Skipped: {run_result.skipped_reason}"
                else:
                    status = "PASS" if run_result.returncode == 0 else f"FAIL ({run_result.returncode})"
            link = ""
            if report_path:
                link = f'<a href="{html.escape(self._relative_href(report_path))}">Open file report</a>'
            metrics = feedback.metrics if feedback else {}
            rows.append(
                "<tr>"
                f"<td>{html.escape(source.name)}</td>"
                f"<td>{len(functions)}</td>"
                f"<td>{sum(len(fn.conditions) for fn in functions)}</td>"
                f"<td>{len(cases)}</td>"
                f"<td>{html.escape(self._metric_display(metrics.get('C0'), 'C0'))}</td>"
                f"<td>{html.escape(self._metric_display(metrics.get('C1'), 'C1'))}</td>"
                f"<td>{html.escape(self._metric_display(metrics.get('MC/DC'), 'MC/DC'))}</td>"
                f"<td>{html.escape(status)}</td>"
                f"<td>{link}</td>"
                "</tr>"
            )

        content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Testing Mate All Files Report</title>
  <style>
    :root {{ --bg: #f5f7fb; --panel: #ffffff; --line: #d8e0ec; --text: #182033; --muted: #5f6f86; --head: #edf2f8; --accent: #2563eb; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 0; background: var(--bg); color: var(--text); }}
    header {{ padding: 22px clamp(16px, 3vw, 34px); background: var(--panel); border-bottom: 1px solid var(--line); }}
    header h1 {{ margin: 0 0 6px; font-size: clamp(22px, 3vw, 32px); }}
    header p {{ margin: 0; color: var(--muted); }}
    main {{ width: min(1480px, 100%); margin: 0 auto; padding: 20px clamp(12px, 2vw, 28px) 32px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .card, section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04); }}
    .metric {{ font-size: 24px; font-weight: 700; margin-top: 6px; line-height: 1.1; color: var(--accent); }}
    .note {{ color: var(--muted); font-size: 12px; line-height: 1.35; margin-top: 6px; }}
    .table-wrap {{ width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); min-width: 980px; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid var(--line); border-right: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    th:last-child, td:last-child {{ border-right: 0; }}
    tr:last-child td {{ border-bottom: 0; }}
    th {{ position: sticky; top: 0; z-index: 1; background: var(--head); color: #243044; font-weight: 650; }}
    a {{ color: var(--accent); font-weight: 650; text-decoration: none; }}
  </style>
</head>
<body>
  <header>
    <h1>Testing Mate All Files Report</h1>
    <p>{html.escape(backend)} | generated {html.escape(datetime.now().isoformat(timespec="seconds"))}</p>
  </header>
  <main>
    <div class="grid">
      <div class="card">Files<div class="metric">{len(items)}</div></div>
      <div class="card">Functions<div class="metric">{total_functions}</div></div>
      <div class="card">Conditions<div class="metric">{total_conditions}</div></div>
      <div class="card">Test Cases<div class="metric">{total_cases}</div></div>
      {metric_cells}
    </div>
    <section>
      <h2>Source Reports</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>File</th><th>Functions</th><th>Conditions</th><th>Cases</th><th>C0</th><th>C1</th><th>MC/DC</th><th>Run Status</th><th>Report</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>"""
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _metric_display(value: float | None, metric: str = "") -> str:
        return f"{value:.2f}%" if value is not None else ("N/A" if metric == "MC/DC" else "Missing")

    def _aggregate_metric_cells(self, items: list[dict]) -> str:
        cells: list[str] = []
        for metric in ("C0", "C1", "MC/DC"):
            values = [
                item["feedback"].metrics.get(metric)
                for item in items
                if item.get("feedback") and item["feedback"].metrics.get(metric) is not None
            ]
            display = self._metric_display(min(values) if values else None, metric)
            cells.append(
                f'<div class="card">Min {html.escape(metric)}<div class="metric">{html.escape(display)}</div>'
                '<div class="note">Worst file-level metric across selected sources</div></div>'
            )
        return "".join(cells)

    @staticmethod
    def _backend_output_items(source: Path, backend: str) -> list[str]:
        if backend == "CppUTest":
            return [
                f"{source.stem}_cpputest_cases.csv",
                f"{source.stem}_cpputest/src/ + stub/include/ (local workspace)",
                f"{source.stem}_cpputest/{source.stem}_cpputest_test.cpp",
                f"{source.stem}_cpputest/run_cpputest.sh",
                f"{source.stem}_cpputest/coverage.json (after clang/llvm-cov run)",
                f"{source.stem}_cpputest/llvm_report.txt (after clang/llvm-cov run)",
                f"{source.stem}_cpputest/llvm_html/index.html (after clang/llvm-cov run)",
            ]
        if backend == "GoogleTest":
            return [
                f"{source.stem}_googletest_cases.csv",
                f"{source.stem}_gtest/src/ + stub/include/ (local workspace)",
                f"{source.stem}_gtest/{source.stem}_gtest_test.cpp",
                f"{source.stem}_gtest/run_gtest.sh",
                f"{source.stem}_gtest/coverage.json (after clang/llvm-cov run)",
                f"{source.stem}_gtest/llvm_report.txt (after clang/llvm-cov run)",
                f"{source.stem}_gtest/llvm_html/index.html (after clang/llvm-cov run)",
            ]
        return []

    def _write_summary(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        feedback: CoverageFeedback | None = None,
        run_result: RunResult | None = None,
    ) -> Path:
        path = self.out_dir / f"{source.stem}_summary.md"
        lines = [f"# {source.name} Analysis", ""]
        lines.append(f"- Functions found: {len(functions)}")
        executed_count = self._executed_test_count(run_result)
        if executed_count is not None:
            lines.append(f"- Test cases run: {executed_count}")
            lines.append(f"- Test cases generated: {len(cases)}")
        else:
            lines.append(f"- Test cases generated: {len(cases)}")
        lines.append("")
        lines.append("## Coverage Metrics")
        metrics = feedback.metrics if feedback else {}
        notes = feedback.metric_notes if feedback else {}
        for metric in ("C0", "C1", "MC/DC"):
            value = metrics.get(metric)
            display = f"{value:.2f}%" if value is not None else ("n/a" if metric == "MC/DC" else "missing")
            note = notes.get(metric, "No executed report has been parsed for this metric yet.")
            lines.append(f"- {metric}: {display} - {note}")
        lines.append("")
        for fn in functions:
            params = ", ".join(f"{p.ctype} {p.name}" for p in fn.parameters) or "void"
            lines.append(f"## {fn.name}({params})")
            lines.append(f"- Lines: {fn.start_line}-{fn.end_line}")
            lines.append(f"- Conditions: {len(fn.conditions)}")
            for condition in fn.conditions:
                lines.append(f"  - line {condition.line}: `{condition.text}`")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
