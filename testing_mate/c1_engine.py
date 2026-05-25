from testing_mate.coverage_gap import CoverageGap
from testing_mate.models import * 
from testing_mate.mcdc_engine import MCDCCoverageEngine
from testing_mate.reporter import *

class C1CoverageEngine:
    def analyze(self, functions: list[FunctionInfo], cases: list[TestCase], source: str = "generated test vectors") -> CoverageFeedback:
        evaluator = MCDCCoverageEngine()
        case_by_function: dict[str, list[TestCase]] = {}
        for case in cases:
            case_by_function.setdefault(case.function, []).append(case)
        total = 0
        covered = 0
        uncovered: list[str] = []
        notes: list[str] = []
        for fn in functions:
            fn_cases = case_by_function.get(fn.name, [])
            if not fn_cases:
                continue
            for condition in fn.conditions:
                decisions = condition.decisions or [condition.text]
                for index, decision in enumerate(decisions):
                    observed: set[bool] = set()
                    for _case, decision_values, outcome in evaluator.evaluations(condition, decisions, fn_cases):
                        if decision_values is not None:
                            observed.add(decision_values[index])
                    total += 2
                    covered += len(observed)
                    if True in observed and False in observed:
                        notes.append(
                            f"C1 covered {fn.name} line {condition.line} atomic decision `{decisions[index]}`: true and false outcomes observed."
                        )
                    else:
                        missing = "true" if True not in observed else "false"
                        uncovered.append(
                            f"Uncovered C1: {fn.name} line {condition.line} atomic decision `{decisions[index]}` missing {missing} outcome."
                        )
                        if not observed:
                            uncovered.append(
                                f"Uncovered C1: {fn.name} line {condition.line} atomic decision `{decisions[index]}` could not be evaluated from generated vectors."
                            )
        percent = (covered / total) * 100.0 if total else 100.0
        note = (
            f"Calculated by Testing Mate from generated vectors: {covered}/{total} atomic decision outcomes covered."
            if total
            else "No branch/decision obligations were discovered; C1 is complete by definition."
        )
        return CoverageFeedback(
            source=source,
            raw_text="\n".join(notes + uncovered)[:12000],
            coverage_percent=percent,
            metrics={"C0": None, "C1": percent, "MC/DC": None},
            metric_notes={"C1": note},
            uncovered_items=uncovered[:80],
            failures=[],
        )

    @staticmethod
    def merge(feedback: CoverageFeedback, c1: CoverageFeedback) -> CoverageFeedback:
        metrics = dict(feedback.metrics)
        metric_notes = dict(feedback.metric_notes)
        use_generated_items = False
        if metrics.get("C1") is None and c1.metrics.get("C1") is not None:
            metrics["C1"] = c1.metrics["C1"]
            metric_notes["C1"] = c1.metric_notes.get("C1", "")
            use_generated_items = True
        else:
            metric_notes.setdefault("C1", c1.metric_notes.get("C1", ""))
        return CoverageFeedback(
            source=feedback.source if feedback.source != "none" else c1.source,
            raw_text=(feedback.raw_text + ("\n" + c1.raw_text if use_generated_items else "")).strip()[:12000],
            coverage_percent=CoverageReportParser()._best_percent("", metrics),
            metrics=metrics,
            metric_notes=metric_notes,
            uncovered_items=(feedback.uncovered_items + (c1.uncovered_items if use_generated_items else []))[:80],
            failures=feedback.failures,
        )
