
from __future__ import annotations
import csv
import html
from pathlib import Path
from datetime import datetime
from testing_mate.models import CoverageFeedback, CoverageGap, Condition, FunctionInfo, Parameter, RunResult, TestCase
from testing_mate.code_database import CodeDatabase
from testing_mate.c1_engine import C1CoverageEngine
from testing_mate.constants import DEFAULT_ENHANCEMENT_STAGES
from testing_mate.generator import TestVectorGenerator, TestBackendGenerator
from testing_mate.coverage_gap import CoverageGapPlanner
from dataclasses import asdict
from testing_mate.llm import OllamaTestRefiner
import json 
import re


class ArtifactWriter:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

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
            self._write_json(source, functions, cases, code_database),
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

    def _write_json(self, source: Path, functions: list[FunctionInfo], cases: list[TestCase], code_database: CodeDatabase | None = None) -> Path:
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
        coverage = "Not provided"
        if feedback and feedback.coverage_percent is not None:
            coverage = f"{feedback.coverage_percent:.2f}%"
        coverage_metric_html = self._coverage_metric_cards(feedback, functions)
        coverage_obligation_rows = self._coverage_obligation_rows(functions)
        run_status = "Not run"
        if run_result:
            if run_result.returncode is None:
                run_status = f"Skipped: {run_result.skipped_reason}"
            else:
                run_status = "PASS" if run_result.returncode == 0 else f"FAIL ({run_result.returncode})"
        executed_count = self._executed_test_count(run_result)
        report_case_count = executed_count if executed_count is not None else len(cases)
        report_case_label = "Test Cases Run" if executed_count is not None else "Test Cases"
        function_sections = []
        case_counts: dict[str, int] = {}
        for case in cases:
            case_counts[case.function] = case_counts.get(case.function, 0) + 1
        for fn in functions:
            cond_rows = "".join(
                f"<li>line {condition.line}: <code>{html.escape(condition.text)}</code></li>"
                for condition in fn.conditions
            )
            params = ", ".join(f"{param.ctype} {param.name}" for param in fn.parameters) or "void"
            function_sections.append(
                f"<section><h3>{html.escape(fn.name)}({html.escape(params)})</h3>"
                f"<p>Test group: {html.escape(fn.name)}. Lines {fn.start_line}-{fn.end_line}. Conditions: {len(fn.conditions)}. Cases: {case_counts.get(fn.name, 0)}</p>"
                f"<ul>{cond_rows}</ul></section>"
            )
        uncovered = "".join(f"<li>{html.escape(item)}</li>" for item in (feedback.uncovered_items if feedback else []))
        failures = "".join(f"<li>{html.escape(item)}</li>" for item in (feedback.failures if feedback else []))
        stdout = html.escape(run_result.stdout if run_result else "")
        stderr = html.escape(run_result.stderr if run_result else "")
        backend_outputs = self._backend_output_items(source, backend)
        backend_output_html = "".join(f"<li><code>{html.escape(item)}</code></li>" for item in backend_outputs)
        lcov_trace_html = self._lcov_trace_section(source, backend, feedback, functions)
        content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Testing Mate Report - {html.escape(source.name)}</title>
  <style>
    :root {{ --bg: #f5f7fb; --panel: #ffffff; --line: #d8e0ec; --text: #182033; --muted: #5f6f86; --head: #edf2f8; --accent: #2563eb; --good: #0f766e; --warn: #b45309; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 0; background: var(--bg); color: var(--text); }}
    header {{ padding: 22px clamp(16px, 3vw, 34px); background: var(--panel); border-bottom: 1px solid var(--line); }}
    header h1 {{ margin: 0 0 6px; font-size: clamp(22px, 3vw, 32px); }}
    header p {{ margin: 0; color: var(--muted); }}
    main {{ width: min(1480px, 100%); margin: 0 auto; padding: 20px clamp(12px, 2vw, 28px) 32px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .card, section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04); }}
    section {{ margin-top: 14px; overflow: hidden; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; }}
    h3 {{ margin: 12px 0 8px; font-size: 15px; }}
    .metric {{ font-size: 24px; font-weight: 700; margin-top: 6px; line-height: 1.1; color: var(--accent); }}
    .note {{ color: var(--muted); font-size: 12px; line-height: 1.35; margin-top: 6px; overflow-wrap: anywhere; }}
    .table-wrap {{ width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); font-size: 12px; }}
    th, td {{ border-bottom: 1px solid var(--line); border-right: 1px solid var(--line); padding: 7px 8px; text-align: left; vertical-align: top; overflow-wrap: break-word; word-wrap: break-word; word-break: break-word; white-space: normal; max-width: 300px; }}
    th:last-child, td:last-child {{ border-right: 0; }}
    tr:last-child td {{ border-bottom: 0; }}
    th {{ position: sticky; top: 0; z-index: 1; background: var(--head); color: #243044; font-weight: 650; }}
    .obligation-table {{ min-width: 760px; }}
    code, pre {{ font-family: Consolas, monospace; }}
    code {{ background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 4px; padding: 1px 4px; word-break: break-word; overflow-wrap: break-word; white-space: normal; display: inline-block; max-width: 100%; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; overflow-wrap: break-word; background: #111827; color: #e5e7eb; padding: 12px; border-radius: 6px; max-height: 280px; overflow: auto; }}
    .trace-actions {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 8px 0 12px; }}
    .trace-link {{ display: inline-flex; align-items: center; min-height: 34px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 6px; text-decoration: none; color: var(--accent); background: #f8fbff; font-weight: 650; }}
    .lcov-frame {{ width: 100%; height: min(760px, 72vh); border: 1px solid var(--line); border-radius: 6px; background: #fff; }}
    ul {{ margin: 8px 0 0; padding-left: 20px; }}
    li {{ margin: 4px 0; overflow-wrap: anywhere; }}
    @media (max-width: 720px) {{ .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .metric {{ font-size: 20px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Testing Mate Report</h1>
    <p>{html.escape(source.name)} | {html.escape(backend)} | generated {html.escape(datetime.now().isoformat(timespec="seconds"))}</p>
  </header>
  <main>
    <div class="grid">
      <div class="card">Functions<div class="metric">{len(functions)}</div></div>
      <div class="card">Conditions<div class="metric">{condition_count}</div></div>
      <div class="card">{html.escape(report_case_label)}<div class="metric">{report_case_count}</div></div>
      <div class="card">Report Coverage<div class="metric">{html.escape(coverage)}</div></div>
      <div class="card">Run Status<div class="metric">{html.escape(run_status)}</div></div>
      {coverage_metric_html}
    </div>
    <section><h2>Backend Output</h2><ul>{backend_output_html}</ul></section>
    {lcov_trace_html}
    <section><h2>Coverage Obligations</h2><div class="table-wrap"><table class="obligation-table"><thead><tr><th>Metric</th><th>Function</th><th>Line</th><th>Obligation</th><th>Evidence Source</th></tr></thead><tbody>{coverage_obligation_rows}</tbody></table></div></section>
    <section><h2>Functions And Conditions</h2>{''.join(function_sections)}</section>
    <section><h2>Coverage Feedback</h2><h3>Uncovered</h3><ul>{uncovered}</ul><h3>Failures</h3><ul>{failures}</ul></section>
    <section><h2>Run Output</h2><h3>stdout</h3><pre>{stdout}</pre><h3>stderr</h3><pre>{stderr}</pre></section>
  </main>
</body>
</html>"""
        path.write_text(content, encoding="utf-8")
        return path

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

    @staticmethod
    def _coverage_metric_cards(feedback: CoverageFeedback | None, functions: list[FunctionInfo]) -> str:
        metrics = feedback.metrics if feedback else {}
        notes = feedback.metric_notes if feedback else {}
        cards: list[str] = []
        obligation_counts = {
            "C0": sum(max(0, fn.end_line - fn.start_line + 1) for fn in functions),
            "C1": sum(max(1, len(condition.decisions) or 1) for fn in functions for condition in fn.conditions),
            "MC/DC": sum(max(1, len(condition.decisions) or 1) for fn in functions for condition in fn.conditions),
        }
        for metric in ("C0", "C1", "MC/DC"):
            value = metrics.get(metric)
            display = f"{value:.2f}%" if value is not None else ("N/A" if metric == "MC/DC" else "Missing")
            note = notes.get(metric) or f"Static obligations: {obligation_counts[metric]}"
            cards.append(
                f'<div class="card">{html.escape(metric)}<div class="metric">{html.escape(display)}</div>'
                f'<div class="note">{html.escape(note)}</div></div>'
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
                "<section><h2>LLVM Source Coverage</h2>"
                "<p class=\"note\">LLVM source coverage is available for CppUTest and GoogleTest runs.</p>"
                "</section>"
            )
        base_name = f"{source.stem}_cpputest" if backend == "CppUTest" else f"{source.stem}_gtest"
        base_dir = self.out_dir / base_name
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
                "<p class=\"note\">Trace executed and uncovered C lines back to clang/llvm-cov source-based coverage. MC/DC is native only when LLVM exported MC/DC records.</p>"
                f"<div class=\"trace-actions\">{''.join(links)}</div>"
                f"{trace_rows}"
                f'<iframe class="lcov-frame" src="{html.escape(self._relative_href(llvm_index))}" title="LLVM source coverage"></iframe>'
                "</section>"
            )

        lcov_dir = self.out_dir / base_name / f"{source.stem}_lcov"
        index_path = lcov_dir / "index.html"
        source_page = self._find_lcov_source_page(lcov_dir, source)
        index_href = self._relative_href(index_path) if index_path.exists() else ""
        source_href = self._relative_href(source_page) if source_page and source_page.exists() else ""
        coverage_info = self.out_dir / base_name / "coverage.info"
        if not index_href:
            expected = f"{base_name}/llvm_html/index.html"
            return (
                "<section><h2>LLVM Source Coverage</h2>"
                f"<p class=\"note\">LLVM source page is not generated yet. Run the backend with clang/llvm-cov, then this report will embed <code>{html.escape(expected)}</code>.</p>"
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
            "<p class=\"note\">Legacy lcov/genhtml source coverage is C0/C1 only. MC/DC is N/A in legacy lcov mode.</p>"
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
                "<td>Uncovered</td>"
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
                    "<td>Function</td>"
                    f"<td>{fn.start_line}-{fn.end_line}</td>"
                    f"<td>{link}</td>"
                    f"<td>{html.escape(fn.name)}</td>"
                    "</tr>"
                )
        return (
            "<div class=\"table-wrap\"><table class=\"obligation-table\">"
            "<thead><tr><th>Type</th><th>Line</th><th>LCOV Source</th><th>Trace Detail</th></tr></thead>"
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
            return "Open source page after lcov run"
        if line is None:
            return f'<a href="{html.escape(source_href)}">Open source page</a>'
        return f'<a href="{html.escape(source_href)}#{line}">line {line}</a>'

    @staticmethod
    def _coverage_obligation_rows(functions: list[FunctionInfo]) -> str:
        rows: list[str] = []
        for fn in functions:
            rows.append(
                "<tr>"
                "<td>C0</td>"
                f"<td>{html.escape(fn.name)}</td>"
                f"<td>{fn.start_line}-{fn.end_line}</td>"
                "<td>Statement/line execution over function body</td>"
                "<td>LLVM source-based line coverage from llvm-cov export/report</td>"
                "</tr>"
            )
            for condition in fn.conditions:
                decisions = condition.decisions or [condition.text]
                rows.append(
                    "<tr>"
                    "<td>C1</td>"
                    f"<td>{html.escape(fn.name)}</td>"
                    f"<td>{condition.line}</td>"
                    f"<td>Decision/branch outcomes for <code>{html.escape(condition.text)}</code></td>"
                    "<td>LLVM source-based branch coverage from llvm-cov export/report</td>"
                    "</tr>"
                )
                for decision in decisions:
                    rows.append(
                        "<tr>"
                        "<td>MC/DC</td>"
                        f"<td>{html.escape(fn.name)}</td>"
                        f"<td>{condition.line}</td>"
                        f"<td>Independent effect pair for <code>{html.escape(decision)}</code></td>"
                        "<td>LLVM native MC/DC from -fcoverage-mcdc; N/A when LLVM MC/DC records are absent</td>"
                        "</tr>"
                    )
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
                f"{source.stem}_cpputest/src/ (copied source/header environment)",
                f"{source.stem}_cpputest/{source.stem}_cpputest_test.cpp",
                f"{source.stem}_cpputest/run_cpputest.sh",
                f"{source.stem}_cpputest/coverage.json (after clang/llvm-cov run)",
                f"{source.stem}_cpputest/llvm_report.txt (after clang/llvm-cov run)",
                f"{source.stem}_cpputest/llvm_html/index.html (after clang/llvm-cov run)",
            ]
        if backend == "GoogleTest":
            return [
                f"{source.stem}_googletest_cases.csv",
                f"{source.stem}_gtest/src/ (copied source/header environment)",
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
