from testing_mate.models import *
import re
import os
from pathlib import Path    
from testing_mate.reporter import CoverageReportParser
from testing_mate.c1_engine import C1CoverageEngine
from testing_mate.mc_coverage import MCDCCoverageEngine
from typing import Callable

def has_report_evidence(feedback: CoverageFeedback) -> bool:
    if feedback.source in {"none", "generated test vectors"}:
        return bool(feedback.failures)
    return bool(feedback.raw_text.strip() or feedback.metrics or feedback.uncovered_items or feedback.failures)


def coverage_log_summary(feedback: CoverageFeedback) -> str:
    parts = []
    for metric in ("C0", "C1", "MC/DC"):
        value = feedback.metrics.get(metric)
        parts.append(f"{metric} {'unknown' if value is None else f'{value:.2f}%'}")
    if feedback.coverage_percent is not None and not any(value is not None for value in feedback.metrics.values()):
        parts.append(f"coverage {feedback.coverage_percent:.2f}%")
    return ", ".join(parts) if parts else "coverage unknown"


def backend_csv_id(backend: str) -> str:
    if backend == "CppUTest":
        return "cpputest"
    if backend == "GoogleTest":
        return "googletest"
    return re.sub(r"[^A-Za-z0-9]+", "_", backend).strip("_").lower()



def feedback_meets_target(feedback: CoverageFeedback, target_percent: float) -> bool:
    values = [value for value in feedback.metrics.values() if value is not None]
    if values:
        return min(values) >= target_percent
    return feedback.coverage_percent is not None and feedback.coverage_percent >= target_percent



def safe_output_folder_name(name: str, fallback: str = "project") -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" ._")
    return clean or fallback


def source_scoped_output_dir(base_out_dir: Path, sources: list[Path]) -> Path:
    if not sources:
        return base_out_dir
    if len(sources) == 1:
        folder_name = safe_output_folder_name(sources[0].stem, "source")
        return base_out_dir if base_out_dir.name == folder_name else base_out_dir / folder_name

    parents = [source.parent for source in sources]
    try:
        common_parent = Path(os.path.commonpath([str(parent) for parent in parents]))
    except ValueError:
        common_parent = parents[0]
    folder_name = safe_output_folder_name(common_parent.name or parents[0].name, "project")
    return base_out_dir if base_out_dir.name == folder_name else base_out_dir / folder_name

def complete_no_obligation_metrics(feedback: CoverageFeedback, functions: list[FunctionInfo]) -> CoverageFeedback:
    if any(fn.conditions for fn in functions):
        return feedback
    metrics = dict(feedback.metrics)
    notes = dict(feedback.metric_notes)
    changed = False
    if metrics.get("C1") is None:
        metrics["C1"] = None
        notes["C1"] = "No branch/decision obligations were discovered; C1 is complete by definition."
        changed = True
    if metrics.get("MC/DC") is None:
        metrics["MC/DC"] = None
        notes["MC/DC"] = "No branch/condition obligations were discovered; MC/DC is complete by definition."
        changed = True
    if not changed:
        return feedback
    return CoverageFeedback(
        source=feedback.source,
        raw_text=feedback.raw_text,
        coverage_percent=CoverageReportParser()._best_percent("", metrics),
        metrics=metrics,
        metric_notes=notes,
        uncovered_items=feedback.uncovered_items,
        failures=feedback.failures,
    )

def merge_cases(existing: list[TestCase], candidates: list[TestCase]) -> int:
    signatures = {(case.function, tuple(sorted(case.values.items()))) for case in existing}
    added = 0
    for candidate in candidates:
        signature = (candidate.function, tuple(sorted(candidate.values.items())))
        if signature in signatures:
            continue
        signatures.add(signature)
        existing.append(candidate)
        added += 1
    return added


def merge_generated_coverage_feedback(feedback: CoverageFeedback, functions: list[FunctionInfo], cases: list[TestCase], coverage: str) -> CoverageFeedback:
    if coverage in {"C0/C1", "MC/DC"}:
        feedback = C1CoverageEngine.merge(feedback, C1CoverageEngine().analyze(functions, cases))
    if coverage == "MC/DC":
        feedback = MCDCCoverageEngine.merge(feedback, MCDCCoverageEngine().analyze(functions, cases))
    return complete_no_obligation_metrics(feedback, functions)

def load_coverage_feedback(report: Path | None, parser: CoverageReportParser, log: Callable[[str], None] | None = None) -> CoverageFeedback:
    if report:
        if report.exists():
            feedback = parser.parse_file(report)
            if log:
                log(f"Loaded coverage/fail report: {report} ({coverage_log_summary(feedback)})")
            return feedback
        if log:
            log(f"Report not found: {report}")
    return CoverageFeedback(source="none", raw_text="", uncovered_items=[], failures=[])


def collect_backend_feedback(parser: CoverageReportParser, out_dir: Path, source: Path, backend: str, log: Callable[[str], None] | None = None) -> CoverageFeedback | None:
    candidates: list[Path] = []
    target_files: list[Path] = [source]
    if backend == "CppUTest":
        base_dir = out_dir / f"{source.stem}_cpputest"
        candidates.append(base_dir / "coverage.info")
        target_files.append(base_dir / "src" / source.name)
    elif backend == "GoogleTest":
        base_dir = out_dir / f"{source.stem}_gtest"
        candidates.append(base_dir / "coverage.info")
        target_files.append(base_dir / "src" / source.name)
    for candidate in candidates:
        if candidate.exists():
            feedback = parser.parse_file(candidate, target_files=target_files)
            if log:
                source_note = "lcov tracefile" if candidate.suffix.lower() in {".info", ".lcov"} else "backend coverage"
                log(f"Parsed {source_note}: {candidate} ({coverage_log_summary(feedback)})")
            return feedback
    if log:
        log(f"No backend coverage artifact found for {backend}; report remains based on selected input only.")
    return None
