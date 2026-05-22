from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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
        classification = classify(fn, record)
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
                    target=self._target_summary(fn, category, record, classification),
                    rationale=self._rationale(fn, level, asil_level, table, method_id, method_name, recommendation, category, classification),
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
    def _target_summary(
        fn: FunctionInfo,
        category: str,
        record: CodeFunctionRecord | None,
        classification: ClassificationResult | None = None,
    ) -> str:
        condition_count = len(fn.conditions)
        parameter_count = len(fn.parameters)
        calls = len(record.calls) if record else 0
        hint_count = len(getattr(fn, "fixit_hints", []) or [])
        prefix = f"{classification.complexity.value}/{classification.strategy}: " if classification else ""
        if category == "coverage":
            return f"{prefix}{condition_count} discovered condition(s), {hint_count} FixItHint oracle hint(s), and function lines {fn.start_line}-{fn.end_line}"
        if category == "interface":
            return f"{prefix}{parameter_count} parameter(s), return type {fn.return_type}, and {calls} internal call(s)"
        if category in {"fault", "error_guessing"}:
            return f"{prefix}{parameter_count} input/interface point(s) and discovered dependency behavior"
        if category in {"equivalence", "boundary"}:
            return f"{prefix}{parameter_count} input domain(s), pointer field reads, and comparison-derived values"
        if category == "comparison":
            return f"{prefix}{calls} callable dependency relation(s) and observable outputs"
        return f"{prefix}function body lines {fn.start_line}-{fn.end_line}"

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
        classification: ClassificationResult | None = None,
    ) -> str:
        strategy = f"; classification={classification.complexity.value}, strategy={classification.strategy}" if classification else ""
        return (
            f"{table} {method_id} {method_name} ({recommendation}) for {asil_level}; "
            f"{level} objective category={category}; generated from generic code-analysis facts for {fn.name}{strategy}"
        )


"""Classify functions for optimal test generation strategy."""

class Complexity(Enum):
    SIMPLE = "simple"       # Template-based, no deep AST
    MODERATE = "moderate"   # Standard branch extraction
    COMPLEX = "complex"     # Full MC/DC + optional LLM

@dataclass
class ClassificationResult:
    complexity: Complexity
    cyclomatic: int
    param_count: int
    condition_count: int
    has_pointers: bool
    max_nesting: int
    strategy: str  # Human-readable recommendation

SIMPLE_CONDITION_LIMIT = 1
SIMPLE_PARAMETER_LIMIT = 3
MODERATE_CONDITION_LIMIT = 10

def classify(fn: FunctionInfo, record: CodeFunctionRecord | None = None) -> ClassificationResult:
    cc = len(fn.conditions) + 1  # Cyclomatic approximation
    params = len(fn.parameters)
    has_ptrs = any(p.kind == "pointer" for p in fn.parameters)
    conditions = len(fn.conditions)
    nesting = max((c.depth for c in fn.conditions), default=0)
    external_calls = len(record.external_calls) if record else 0
    decision_atoms = sum(max(1, len(condition.decisions or [])) for condition in fn.conditions)

    if conditions <= SIMPLE_CONDITION_LIMIT and params <= SIMPLE_PARAMETER_LIMIT and not has_ptrs and external_calls == 0:
        complexity = Complexity.SIMPLE
        strategy = "fast_path_template"
    elif conditions <= MODERATE_CONDITION_LIMIT and nesting < 3 and decision_atoms <= 16:
        complexity = Complexity.MODERATE
        strategy = "branch_extraction_with_solver_mcdc"
    else:
        complexity = Complexity.COMPLEX
        strategy = "full_mcdc_with_llm_assist"

    return ClassificationResult(
        complexity=complexity,
        cyclomatic=cc,
        param_count=params,
        condition_count=conditions,
        has_pointers=has_ptrs,
        max_nesting=nesting,
        strategy=strategy,)
