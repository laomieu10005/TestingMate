from enum import IntEnum
import os
from typing import Any, Dict, Iterable

from pathlib import Path

from testing_mate.models import CoverageFeedback, ExecutionContext, FunctionInfo, RunResult, TestCase


# Lightweight helpers to avoid hard dependency on external test runner
def snapshot_coverage(cov_db: any, fn: any) -> dict:
    # Expect cov_db to expose .metrics dict or be a mapping
    metrics = {}
    try:
        m = cov_db.metrics
    except Exception:
        m = getattr(cov_db, 'metrics', {}) or (cov_db or {})
    c0 = m.get('C0')
    c1 = m.get('C1')
    mcdc = m.get('MC/DC')
    metrics['C0_line'] = (c0 or 0.0) / 100.0 if c0 is not None else 0.0
    metrics['C1_branch'] = (c1 or 0.0) / 100.0 if c1 is not None else 0.0

    # LLVM reports native MC/DC only for eligible decisions.  For a function
    # whose extracted decisions are all single atomic conditions, MC/DC has no
    # additional independent-condition obligation beyond executing the true and
    # false decision outcomes.  In that case, do not force escalation forever
    # just because LLVM returns MC/DC as N/A/zero-count; use C1 as the effective
    # target signal for the simple-decision case.
    simple_decision_only = False
    try:
        conditions = list(getattr(fn, 'conditions', []) or [])
        simple_decision_only = bool(conditions) and all(len(getattr(cond, 'decisions', None) or [getattr(cond, 'text', '')]) <= 1 for cond in conditions)
    except Exception:
        simple_decision_only = False
    if mcdc is None and simple_decision_only and c1 is not None:
        metrics['MCDC'] = metrics['C1_branch']
    else:
        metrics['MCDC'] = (mcdc or 0.0) / 100.0 if mcdc is not None else 0.0
    return metrics


def _copy_feedback_with_layers(feedback: CoverageFeedback, layer_records: list[dict[str, Any]]) -> CoverageFeedback:
    existing = list(getattr(feedback, 'execution_layers', []) or [])
    return CoverageFeedback(
        source=feedback.source,
        raw_text=feedback.raw_text,
        coverage_percent=feedback.coverage_percent,
        metrics=feedback.metrics,
        metric_notes=feedback.metric_notes,
        uncovered_items=feedback.uncovered_items,
        failures=feedback.failures,
        execution_layers=existing + list(layer_records),
    )


def _execution_context_from_cfg(cfg_json: Any) -> ExecutionContext | None:
    raw = _cfg_option(cfg_json, 'execution_context', None)
    if isinstance(raw, ExecutionContext):
        return raw
    if not isinstance(raw, dict):
        return None
    source_path = raw.get('source_path') or raw.get('source')
    out_dir = raw.get('out_dir')
    backend = raw.get('backend')
    if not source_path or not out_dir or not backend:
        return None
    return ExecutionContext(
        source_path=Path(source_path),
        backend=str(backend),
        out_dir=Path(out_dir),
        include_dirs=[str(item) for item in raw.get('include_dirs', [])],
        defines=[str(item) for item in raw.get('defines', [])],
        compiler=str(raw.get('compiler', '')),
        coverage=str(raw.get('coverage', raw.get('selected_coverage_metric', 'MC/DC'))),
        level=str(raw.get('level', 'UT')),
        asil_level=str(raw.get('asil_level', 'ASIL D')),
        target_coverage_percent=float(raw.get('target_coverage_percent', raw.get('target_percent', 100.0))),
        unit_sources=[Path(item) for item in raw.get('unit_sources', [])],
        toolchain_options=dict(raw.get('toolchain_options', {})),
        run_after_generate=bool(raw.get('run_after_generate', True)),
    )


class BackendExecutionService:
    """Write, execute, and parse a focused backend run for one escalation layer."""

    def __init__(self, context: ExecutionContext):
        self.context = context

    def run_layer_candidates(
        self,
        layer: str,
        fn: FunctionInfo,
        candidate_cases: list[TestCase],
        feedback_before: CoverageFeedback,
        code_database: Any = None,
    ) -> tuple[RunResult, CoverageFeedback, dict[str, Any]]:
        from testing_mate.artifacts_writer import ArtifactWriter
        from testing_mate.generator import (
            TestBackendRunner,
            TestCaseRunReconciler,
            TestMockFallback,
            TestOracleFallback,
            format_run_result,
        )
        from testing_mate.reporter import CoverageReportParser
        from testing_mate.utilities import backend_csv_id, collect_backend_feedback, coverage_log_summary

        source = Path(self.context.source_path).expanduser()
        backend = self.context.backend
        safe_fn = ''.join(ch if ch.isalnum() or ch in {'_', '-'} else '_' for ch in fn.name) or 'function'
        safe_layer = layer.lower().replace(' ', '_')
        layer_out_dir = Path(self.context.out_dir).expanduser() / 'layer_runs' / safe_fn / safe_layer
        unit_sources = self.context.unit_sources or [source]
        for case in candidate_cases:
            case.trace_tags = ';'.join(part for part in [case.trace_tags, f'escalation-layer:{layer}', f'escalation-function:{fn.name}'] if part)
            case.execution_status = 'generated'

        writer = ArtifactWriter(layer_out_dir)
        writer.write(
            source,
            [fn],
            candidate_cases,
            self.context.level,
            backend,
            feedback_before,
            None,
            include_backend=True,
            unit_sources=unit_sources,
            include_dirs=self.context.include_dirs,
            target_coverage_percent=self.context.target_coverage_percent,
            code_database=code_database,
            selected_coverage=self.context.coverage,
        )

        layer_reconcile_updates = 0
        layer_reconcile_notes: list[str] = []
        if not self.context.run_after_generate:
            run_result = RunResult(backend=backend, command=str(layer_out_dir), returncode=None, skipped_reason='Execution disabled by ExecutionContext')
        else:
            runner = TestBackendRunner()
            fast_layer_fallback = str(os.environ.get('TESTING_MATE_FAST_FALLBACK', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}
            final_layer_coverage = str(os.environ.get('TESTING_MATE_FINAL_COVERAGE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}
            run_result = runner.run(backend, layer_out_dir, source, skip_coverage=fast_layer_fallback)
            retry_limit = max(0, min(12, int(os.environ.get('TESTING_MATE_LAYER_RECONCILE_RETRY_LIMIT', '8'))))
            for attempt in range(1, retry_limit + 1):
                if run_result.returncode is None:
                    break
                if run_result.returncode == 0:
                    notes: list[str] = []
                    case_updated, case_notes = TestCaseRunReconciler.apply(backend, layer_out_dir, source, run_result, candidate_cases)
                    notes.extend(case_notes)
                    mock_updated, mock_notes = TestMockFallback.apply(backend, layer_out_dir, source, run_result)
                    notes.extend(mock_notes if mock_updated else [])
                    if case_updated or mock_updated:
                        layer_reconcile_updates += case_updated + mock_updated
                        layer_reconcile_notes.extend([f'attempt {attempt}: {note}' for note in notes])
                        try:
                            writer._write_json(source, [fn], candidate_cases, code_database, None)
                            writer._write_csv(source, candidate_cases, backend_csv_id(backend))
                        except Exception as exc:
                            layer_reconcile_notes.append(f'attempt {attempt}: could not persist warning reconciliation: {exc}')
                    break
                notes: list[str] = []
                case_updated, case_notes = TestCaseRunReconciler.apply(backend, layer_out_dir, source, run_result, candidate_cases)
                notes.extend(case_notes)
                oracle_updated, oracle_notes = TestOracleFallback.apply(backend, layer_out_dir, source, run_result)
                notes.extend(oracle_notes)
                mock_updated, mock_notes = TestMockFallback.apply(backend, layer_out_dir, source, run_result)
                notes.extend(mock_notes)
                updated = case_updated + oracle_updated + mock_updated
                if notes:
                    layer_reconcile_notes.extend([f'attempt {attempt}: {note}' for note in notes])
                if not updated:
                    break
                layer_reconcile_updates += updated
                # Persist candidate-case evidence immediately so the layer report,
                # final merged suite, and any regenerated backend file all keep the
                # observed expected values and mock-call decisions.
                try:
                    writer._write_json(source, [fn], candidate_cases, code_database, None)
                    writer._write_csv(source, candidate_cases, backend_csv_id(backend))
                except Exception as exc:
                    layer_reconcile_notes.append(f'attempt {attempt}: could not persist layer reconciliation: {exc}')
                failed_filter = TestBackendRunner.failed_filter_from_result(backend, run_result)
                run_result = runner.run(
                    backend,
                    layer_out_dir,
                    source,
                    test_filter=failed_filter or None,
                    skip_coverage=fast_layer_fallback,
                )
            if fast_layer_fallback and final_layer_coverage and run_result.returncode == 0:
                run_result = runner.run(backend, layer_out_dir, source, skip_coverage=False)

        parsed = collect_backend_feedback(CoverageReportParser(), layer_out_dir, source, backend)
        feedback_after = parsed or feedback_before
        status = 'PASS' if run_result.returncode == 0 else ('SKIPPED' if run_result.returncode is None else 'FAIL')
        for case in candidate_cases:
            case.execution_status = status.lower()
            if run_result.returncode == 0:
                case.artifact_status = 'executed'
                case.oracle_status = 'checked'
            elif run_result.returncode is None:
                case.artifact_status = 'generated'
            else:
                case.artifact_status = 'backend_failed'
                case.oracle_status = 'failed'

        record = {
            'layer': layer,
            'function': fn.name,
            'candidate_count': len(candidate_cases),
            'backend': backend,
            'out_dir': str(layer_out_dir),
            'run_status': status,
            'returncode': run_result.returncode,
            'skipped_reason': run_result.skipped_reason,
            'command': run_result.command,
            'coverage_before': dict(feedback_before.metrics),
            'coverage_after': dict(feedback_after.metrics),
            'coverage_summary': coverage_log_summary(feedback_after),
            'stdout_tail': run_result.stdout[-1200:],
            'stderr_tail': run_result.stderr[-1200:],
            'result': format_run_result(run_result),
            'reconcile_updates': layer_reconcile_updates,
            'reconcile_notes': layer_reconcile_notes[-20:],
        }
        return run_result, feedback_after, record


def run_tests(fn: any, tests: list, execution_context: ExecutionContext | None = None) -> RunResult:
    if execution_context is None:
        return RunResult(backend='none', command='', returncode=None, skipped_reason='No ExecutionContext supplied')
    feedback = CoverageFeedback(source='none', raw_text='')
    run_result, _feedback_after, _record = BackendExecutionService(execution_context).run_layer_candidates(
        'ad-hoc', fn, list(tests), feedback
    )
    return run_result


def update_coverage(cov_db: any, run_result: RunResult, parsed_feedback: CoverageFeedback | None = None) -> any:
    if parsed_feedback is not None:
        return parsed_feedback
    return cov_db


def _coverage_feedback(cov_db: Any) -> CoverageFeedback:
    if isinstance(cov_db, CoverageFeedback):
        return cov_db
    metrics = getattr(cov_db, "metrics", None)
    if isinstance(metrics, dict):
        return CoverageFeedback(
            source=getattr(cov_db, "source", "coverage_db"),
            raw_text=getattr(cov_db, "raw_text", ""),
            coverage_percent=getattr(cov_db, "coverage_percent", None),
            metrics=metrics,
            metric_notes=getattr(cov_db, "metric_notes", {}) or {},
            uncovered_items=getattr(cov_db, "uncovered_items", []) or [],
            failures=getattr(cov_db, "failures", []) or [],
            execution_layers=getattr(cov_db, "execution_layers", []) or [],
        )
    if isinstance(cov_db, dict):
        known = {"C0", "C1", "MC/DC", "C0_line", "C1_branch", "MCDC"}
        raw_metrics = {key: cov_db.get(key) for key in known if key in cov_db}
        metrics = {
            "C0": raw_metrics.get("C0", raw_metrics.get("C0_line")),
            "C1": raw_metrics.get("C1", raw_metrics.get("C1_branch")),
            "MC/DC": raw_metrics.get("MC/DC", raw_metrics.get("MCDC")),
        }
        return CoverageFeedback(source="coverage_db", raw_text="", metrics=metrics)
    return CoverageFeedback(source="none", raw_text="", metrics={})


def _cfg_option(cfg_json: Any, key: str, default: Any) -> Any:
    if isinstance(cfg_json, dict):
        return cfg_json.get(key, default)
    return getattr(cfg_json, key, default)


def _functions(fn: Any) -> list[FunctionInfo]:
    if isinstance(fn, list):
        return fn
    return [fn]


def _merge_current_case_inventory(base_cfg: Any, current_cfg: Any) -> Any:
    if not isinstance(base_cfg, dict) or not isinstance(current_cfg, dict):
        return base_cfg
    merged = dict(base_cfg)
    for key in ("existing_testcases", "existing_test_ids", "existing_test_names", "dedupe_against"):
        if key in current_cfg:
            merged[key] = current_cfg[key]
    return merged


def _case_values(fn: FunctionInfo, item: dict[str, Any]) -> dict[str, str]:
    values = item.get("values")
    if isinstance(values, dict):
        return {str(key): str(value) for key, value in values.items()}
    return {
        param.name: str(item.get(param.name, ""))
        for param in getattr(fn, "parameters", [])
        if param.name in item
    }


def _materialize_case(
    fn: FunctionInfo,
    item: Any,
    index: int,
    source_name: str,
    level: str,
    coverage: str,
    asil_level: str,
) -> TestCase | None:
    if isinstance(item, TestCase):
        return item
    if not isinstance(item, dict):
        return None
    values = _case_values(fn, item)
    if not values and not item.get("test_id"):
        return None
    test_id = str(item.get("test_id") or f"ESC_{fn.name}_{index:04d}")
    return TestCase(
        test_id=test_id,
        unit=str(item.get("unit") or source_name),
        function=str(item.get("function") or fn.name),
        coverage=str(item.get("coverage") or coverage),
        level=str(item.get("level") or level),
        values=values,
        expected=str(item.get("expected") or item.get("__expected_return") or ""),
        rationale=str(item.get("rationale") or item.get("description") or "Layer fallback generated testcase"),
        description=str(item.get("description") or ""),
        asil=str(item.get("asil") or asil_level),
        artifact_status=str(item.get("artifact_status") or "candidate"),
        oracle_status=str(item.get("oracle_status") or "needs_review"),
        execution_status=str(item.get("execution_status") or "not_run"),
    )


def _materialize_cases(
    fn: FunctionInfo,
    items: Iterable[Any],
    existing_count: int,
    source_name: str,
    level: str,
    coverage: str,
    asil_level: str,
) -> list[TestCase]:
    result: list[TestCase] = []
    for offset, item in enumerate(items, start=existing_count + 1):
        case = _materialize_case(fn, item, offset, source_name, level, coverage, asil_level)
        if case is not None:
            result.append(case)
    return result


def _case_inventory(cases: list[TestCase]) -> list[dict[str, Any]]:
    from testing_mate.vector_generator import TestCaseRegistry

    return TestCaseRegistry.inventory(cases)


def _cfg_with_case_inventory(cfg_json: Any, cases: list[TestCase]) -> Any:
    if not isinstance(cfg_json, dict):
        return cfg_json
    inventory = _case_inventory(cases)
    cfg = dict(cfg_json)
    cfg["existing_testcases"] = inventory
    cfg["existing_test_ids"] = [item["test_id"] for item in inventory]
    cfg["existing_test_names"] = [item["test_name"] for item in inventory]
    cfg["dedupe_against"] = [item["signature"] for item in inventory]
    return cfg


def _merge_generated_fallback(
    cov_db: Any,
    fn: FunctionInfo,
    cases: list[TestCase],
    coverage: str,
    allow_generated_mcdc: bool,
) -> CoverageFeedback:
    feedback = _coverage_feedback(cov_db)
    eligible_cases = [case for case in cases if str(getattr(case, "execution_status", "")).lower() not in {"fail", "failed"}]
    if not eligible_cases:
        return feedback
    from testing_mate.utilities import merge_generated_coverage_feedback

    return merge_generated_coverage_feedback(
        feedback,
        _functions(fn),
        eligible_cases,
        coverage,
        allow_generated_mcdc=allow_generated_mcdc,
    )


class Layer(IntEnum):
    CLANG_ANALYZER = 1
    Z3_SOLVER = 2
    ANGR_BASIC = 3
    ANGR_DEEP = 4


COVERAGE_TARGETS = {
    "C0_line": 0.90,
    "C1_branch": 0.85,
    "MCDC": 0.80,
}


def compute_coverage_gain(cov_before: Dict[str, float], cov_after: Dict[str, float]):
    gains = {}
    for metric in ["C0_line", "C1_branch", "MCDC"]:
        before = cov_before.get(metric, 0.0)
        after = cov_after.get(metric, 0.0)
        gains[metric] = after - before
    return gains


def is_stale(gains: Dict[str, float], threshold: float = 0.01) -> bool:
    return all(g < threshold for g in gains.values())


def coverage_meets_target(cov: Dict[str, float], targets: Dict[str, float]) -> bool:
    for k, v in targets.items():
        if cov.get(k, 0.0) < v:
            return False
    return True


def targets_from_percent(target_percent: float | None, coverage: str | None = None) -> dict[str, float]:
    target = max(0.0, min(100.0, float(target_percent if target_percent is not None else 100.0))) / 100.0
    selected = str(coverage or "").strip().upper()
    if selected in {"C0", "C0/C1", "C1"}:
        return {"C0_line": target, "C1_branch": target}
    if selected in {"MC/DC", "MCDC"}:
        return {"C0_line": target, "C1_branch": target, "MCDC": target}
    return dict(COVERAGE_TARGETS)


def _fallback_gap_cases(
    fn: FunctionInfo,
    cov_db: Any,
    existing_cases: list[TestCase],
    source_name: str,
    level: str,
    coverage: str,
    asil_level: str,
    density: int,
) -> list[TestCase]:
    feedback = _coverage_feedback(cov_db)
    if not (feedback.raw_text.strip() or feedback.uncovered_items or any(value is not None for value in feedback.metrics.values())):
        return []
    try:
        from testing_mate.coverage_gap import CoverageGapPlanner
        from testing_mate.vector_generator import TestVectorGenerator

        gaps = CoverageGapPlanner().plan(feedback, [fn], max_gaps=max(1, density * 8))
        if str(coverage).strip().upper() not in {"MC/DC", "MCDC"}:
            gaps = [gap for gap in gaps if gap.metric != "MC/DC"]
        if not gaps:
            return []
        return TestVectorGenerator().generate_for_coverage_gaps(
            source_name,
            [fn],
            existing_cases,
            gaps,
            level,
            coverage,
            asil_level,
            max_cases_per_gap=max(1, density),
        )
    except Exception:
        return []


def execute_layer(layer: Layer, fn: Any, compile_db: Any, cfg_json: Any, cov_db: Any, prev_data: Dict[str, Any]):
    # Minimal stubs that integrate with later layer implementations
    if layer == Layer.CLANG_ANALYZER:
        # delegate to existing clang analyzer if present
        try:
            from .layers.layer_clang_analyzer import run_clang_analyzer
            tests, pruned_cfg = run_clang_analyzer(fn, compile_db, cfg_json, cov_db)
            return tests, {"pruned_cfg": pruned_cfg}
        except Exception:
            return [], prev_data

    if layer == Layer.Z3_SOLVER:
        try:
            from .layers.layer_z3_solver import run_z3_solver
            pruned_cfg = _merge_current_case_inventory(prev_data.get("pruned_cfg", cfg_json), cfg_json)
            tests, unsolvable = run_z3_solver(fn, pruned_cfg, cov_db)
            return tests, {**prev_data, "unsolvable_edges": unsolvable}
        except Exception:
            return [], prev_data

    if layer == Layer.ANGR_BASIC:
        try:
            from .layers.layer_angr_basic import run_angr_basic
            unsolvable = prev_data.get("unsolvable_edges", [])
            tests, unreachable = run_angr_basic(fn, compile_db, unsolvable, cov_db)
            return tests, {**prev_data, "unreachable_targets": unreachable}
        except Exception:
            return [], prev_data

    if layer == Layer.ANGR_DEEP:
        try:
            from .layers.layer_angr_deep import run_angr_deep
            unreachable = prev_data.get("unreachable_targets", [])
            tests, gaps = run_angr_deep(fn, compile_db, unreachable, cov_db)
            return tests, {**prev_data, "final_gaps": gaps}
        except Exception:
            return [], prev_data

    return [], prev_data


def run_escalation(
    fn: FunctionInfo,
    compile_db: Any,
    cov_db: Any,
    cfg_json: Any,
    max_layer: Layer = Layer.ANGR_DEEP,
    existing_cases: list[TestCase] | None = None,
    source_name: str | None = None,
    level: str | None = None,
    coverage: str | None = None,
    asil_level: str | None = None,
    allow_generated_mcdc: bool | None = None,
    target_percent: float | None = None,
    stale_gain_threshold: float | None = None,
    density: int = 3,
):
    current_layer = Layer.CLANG_ANALYZER
    all_tests: list[TestCase] = list(existing_cases or [])
    from testing_mate.vector_generator import TestCaseRegistry

    registry = TestCaseRegistry(all_tests)
    layer_data: Dict[str, Any] = {}
    source_name = source_name or str(_cfg_option(cfg_json, "source_name", ""))
    level = level or str(_cfg_option(cfg_json, "level", "UT"))
    coverage = coverage or str(_cfg_option(cfg_json, "coverage", "MC/DC"))
    asil_level = asil_level or str(_cfg_option(cfg_json, "asil_level", "ASIL D"))
    target_percent = float(_cfg_option(cfg_json, "target_percent", target_percent if target_percent is not None else 100.0))
    stale_gain_threshold = float(_cfg_option(cfg_json, "stale_gain_threshold", stale_gain_threshold if stale_gain_threshold is not None else 0.01))
    density = max(1, int(_cfg_option(cfg_json, "density", density)))
    coverage_targets = targets_from_percent(target_percent, coverage)
    execution_context = _execution_context_from_cfg(cfg_json)
    execution_service = BackendExecutionService(execution_context) if execution_context is not None else None
    layer_records: list[dict[str, Any]] = []
    if allow_generated_mcdc is None:
        allow_generated_mcdc = coverage.strip().upper() in {"MC/DC", "MCDC"}
    cov_db = _merge_generated_fallback(cov_db, fn, all_tests, coverage, allow_generated_mcdc)

    while current_layer <= max_layer:
        cov_before = snapshot_coverage(cov_db, fn)

        layer_cfg = _cfg_with_case_inventory(cfg_json, all_tests)
        layer_data = {
            **layer_data,
            "existing_testcases": _case_inventory(all_tests),
        }
        new_tests, layer_data = execute_layer(current_layer, fn, compile_db, layer_cfg, cov_db, layer_data)
        materialized = _materialize_cases(
            fn,
            new_tests,
            len(all_tests),
            source_name or getattr(fn, "name", ""),
            level,
            coverage,
            asil_level,
        )
        if not materialized and current_layer == Layer.CLANG_ANALYZER:
            materialized = _fallback_gap_cases(
                fn,
                cov_db,
                all_tests,
                source_name or getattr(fn, "name", ""),
                level,
                coverage,
                asil_level,
                density,
            )

        candidates: list[TestCase] = []
        for case in materialized:
            if registry.has_signature(case.function, case.values):
                continue
            candidates.append(case)

        layer_name = current_layer.name
        if candidates and execution_service is not None:
            _run_result, parsed_feedback, record = execution_service.run_layer_candidates(
                layer_name, fn, candidates, _coverage_feedback(cov_db), compile_db
            )
            cov_db = update_coverage(cov_db, _run_result, parsed_feedback)
            layer_records.append(record)
        elif candidates:
            record = {
                "layer": layer_name,
                "function": fn.name,
                "candidate_count": len(candidates),
                "backend": "none",
                "run_status": "NOT_RUN",
                "coverage_before": dict(_coverage_feedback(cov_db).metrics),
                "coverage_after": dict(_coverage_feedback(cov_db).metrics),
                "result": "No ExecutionContext supplied; candidates were generated but not backend-executed.",
            }
            layer_records.append(record)
        else:
            layer_records.append({
                "layer": layer_name,
                "function": fn.name,
                "candidate_count": 0,
                "backend": execution_context.backend if execution_context else "none",
                "run_status": "NO_CANDIDATES",
                "coverage_before": dict(_coverage_feedback(cov_db).metrics),
                "coverage_after": dict(_coverage_feedback(cov_db).metrics),
            })

        # Accept only the current layer's new candidates after the focused run.
        # This prevents every fallback layer from blindly rerunning the full
        # accumulated suite while still keeping failed/skipped candidates visible
        # for traceability and later manual review.
        for case in candidates:
            if registry.add_case(case):
                all_tests.append(case)

        cov_db = _merge_generated_fallback(cov_db, fn, all_tests, coverage, allow_generated_mcdc)
        cov_db = _copy_feedback_with_layers(_coverage_feedback(cov_db), [layer_records[-1]] if layer_records else [])

        cov_after = snapshot_coverage(cov_db, fn)

        if coverage_meets_target(cov_after, coverage_targets):
            return all_tests, cov_db, 'DONE'

        if current_layer >= Layer.Z3_SOLVER:
            gains = compute_coverage_gain(cov_before, cov_after)
            if is_stale(gains, stale_gain_threshold):
                return all_tests, cov_db, 'REPORT_GAP'

        current_layer = Layer(current_layer + 1)

    return all_tests, cov_db, 'REPORT_GAP'
