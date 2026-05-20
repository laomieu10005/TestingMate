from __future__ import annotations

from dataclasses import dataclass

from testing_mate.code_database import CodeFunctionRecord
from testing_mate.constants import ISO_EXECUTABLE_METHODS, ISO_RECOMMENDATIONS
from testing_mate.models import FunctionInfo


ISO_OBJECTIVE_CATEGORIES: dict[str, str] = {
    "1j": "requirements",
    "1k": "interface",
    "1l": "fault",
    "1n": "comparison",
    "T8a": "analysis",
    "T8b": "equivalence",
    "T8c": "boundary",
    "T8d": "error_guessing",
    "C0": "coverage",
    "C1": "coverage",
    "MC/DC": "coverage",
}


@dataclass(frozen=True)
class VerificationObjective:
    method_id: str
    table: str
    method_name: str
    recommendation: str
    category: str
    coverage_metric: str
    strength: int
    target: str
    rationale: str

    @property
    def coverage_label(self) -> str:
        return f"ISO26262 {self.method_id} {self.method_name}"

    @property
    def vector_limit(self) -> int:
        if self.category == "coverage":
            return 4 if self.strength >= 2 else 2
        return 2 if self.strength >= 2 else 1


class TestGenerationWorkflow:
    """Plan generic generation objectives from analysis facts and ASIL policy."""

    def objectives_for(
        self,
        fn: FunctionInfo,
        level: str,
        coverage: str,
        asil_level: str,
        record: CodeFunctionRecord | None = None,
    ) -> list[VerificationObjective]:
        recommendations = ISO_RECOMMENDATIONS.get(asil_level, ISO_RECOMMENDATIONS["ASIL D"])
        objectives: list[VerificationObjective] = []
        for method_id, recommendation in recommendations.items():
            method_info = ISO_EXECUTABLE_METHODS.get(method_id)
            if not method_info:
                continue
            table, method_name = method_info
            category = ISO_OBJECTIVE_CATEGORIES.get(method_id, "analysis")
            objectives.append(
                VerificationObjective(
                    method_id=method_id,
                    table=table,
                    method_name=method_name,
                    recommendation=recommendation,
                    category=category,
                    coverage_metric=self._coverage_metric(method_id, coverage),
                    strength=self._recommendation_strength(recommendation),
                    target=self._target_summary(fn, category, record),
                    rationale=self._rationale(fn, level, asil_level, table, method_id, method_name, recommendation, category),
                )
            )
        return objectives

    @staticmethod
    def _coverage_metric(method_id: str, selected_coverage: str) -> str:
        if method_id in {"C0", "C1", "MC/DC"}:
            return method_id
        return selected_coverage

    @staticmethod
    def _recommendation_strength(recommendation: str) -> int:
        if recommendation == "++":
            return 2
        if recommendation == "+":
            return 1
        return 0

    @staticmethod
    def _target_summary(fn: FunctionInfo, category: str, record: CodeFunctionRecord | None) -> str:
        condition_count = len(fn.conditions)
        parameter_count = len(fn.parameters)
        calls = len(record.calls) if record else 0
        if category == "coverage":
            return f"{condition_count} discovered condition(s) and function lines {fn.start_line}-{fn.end_line}"
        if category == "interface":
            return f"{parameter_count} parameter(s), return type {fn.return_type}, and {calls} internal call(s)"
        if category in {"fault", "error_guessing"}:
            return f"{parameter_count} input/interface point(s) and discovered dependency behavior"
        if category in {"equivalence", "boundary"}:
            return f"{parameter_count} input domain(s), pointer field reads, and comparison-derived values"
        if category == "comparison":
            return f"{calls} callable dependency relation(s) and observable outputs"
        return f"function body lines {fn.start_line}-{fn.end_line}"

    @staticmethod
    def _rationale(
        fn: FunctionInfo,
        level: str,
        asil_level: str,
        table: str,
        method_id: str,
        method_name: str,
        recommendation: str,
        category: str,
    ) -> str:
        return (
            f"{table} {method_id} {method_name} ({recommendation}) for {asil_level}; "
            f"{level} objective category={category}; generated from generic code-analysis facts for {fn.name}"
        )
