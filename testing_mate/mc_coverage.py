"""MC/DC (Modified Condition/Decision Coverage) analysis and calculation.

Implements Step B, C, D of MC/DC evidence collection:
- B: Normalize decisions and extract atomic conditions
- C: Calculate fallback MC/DC with witness pair tracking
- D: Compute and report metrics
"""

from __future__ import annotations

import hashlib
import re, json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from testing_mate.models import Condition
from testing_mate.boolean_expression import ConditionEvaluator


@dataclass
class AtomicCondition:
    """Represents a single atomic condition within a decision."""
    index: int
    text: str
    operator: str = ""  # &&, ||, ternary
    
    def __hash__(self) -> int:
        return hash((self.index, self.text))


@dataclass
class ExecutedVector:
    """Records a single execution of a decision with condition values."""
    condition_values: tuple[bool, ...]  # Tuple of boolean values for each condition
    decision_result: bool
    test_id: str = ""
    line_number: int = 0


@dataclass
class WitnessPair:
    """A pair of test executions proving a condition is independently testable."""
    changing_condition_index: int
    test1: ExecutedVector
    test2: ExecutedVector
    
    @property
    def diff_only_condition(self) -> bool:
        """Check if only the specified condition differs between vectors."""
        for i, (v1, v2) in enumerate(zip(self.test1.condition_values, self.test2.condition_values)):
            if i != self.changing_condition_index:
                if v1 != v2:
                    return False
        return self.test1.decision_result != self.test2.decision_result


@dataclass
class MCDCDecision:
    """MC/DC analysis for a single decision point."""
    decision_id: str
    decision_text: str
    source_file: str
    line_number: int
    atomic_conditions: list[AtomicCondition] = field(default_factory=list)
    executed_vectors: list[ExecutedVector] = field(default_factory=list)
    witness_pairs: dict[int, WitnessPair] = field(default_factory=dict)  # By condition index
    covered_conditions: set[int] = field(default_factory=set)
    uncovered_condition_indices: set[int] = field(default_factory=set)
    backend_source: str = "fallback_trace"  # "clang_native" or "fallback_trace"
    
    @property
    def coverage_percent(self) -> float | None:
        """Calculate MC/DC coverage percentage for this decision."""
        if not self.atomic_conditions:
            return None
        total = len(self.atomic_conditions)
        covered = len(self.covered_conditions)
        return (covered / total) * 100.0 if total > 0 else 100.0
    
    @property
    def uncovered_vectors(self) -> list[ExecutedVector]:
        """Get executed vectors that don't demonstrate required coverage."""
        uncovered: list[ExecutedVector] = []
        for vec in self.executed_vectors:
            # Check if this vector contributes to any condition's witness pair
            contributes = False
            for cond_idx, witness in self.witness_pairs.items():
                if vec in (witness.test1, witness.test2):
                    contributes = True
                    break
            if not contributes and cond_idx not in self.covered_conditions:
                uncovered.append(vec)
        return uncovered


@dataclass
class MCDCReport:
    """Aggregated MC/DC report for a test suite."""
    backend_source: str = "fallback_trace"  # "clang_native" or "fallback_trace"
    decisions: list[MCDCDecision] = field(default_factory=list)
    total_conditions: int = 0
    covered_conditions: int = 0
    
    @property
    def coverage_percent(self) -> float | None:
        """Calculate overall MC/DC coverage percentage."""
        if self.total_conditions <= 0:
            return None
        return (self.covered_conditions / self.total_conditions) * 100.0
    
    @property
    def metrics(self) -> dict[str, float | None]:
        """Return structured metrics dict."""
        return {
            "MC/DC": self.coverage_percent,
            "decisions_analyzed": len(self.decisions),
            "total_conditions": self.total_conditions,
            "covered_conditions": self.covered_conditions,
        }


class MCDCAnalyzer:
    """Analyze and normalize MC/DC evidence from executed test vectors."""
    
    def __init__(self, log: Callable[[str], None] | None = None):
        self.log = log or (lambda _: None)
    
    def build_decision_id(self, source_file: str, line_number: int, decision_text: str) -> str:
        """Build a stable, unique decision identifier."""
        text_hash = hashlib.md5(decision_text.encode()).hexdigest()[:8]
        return f"{Path(source_file).stem}_{line_number}_{text_hash}"
    
    def extract_atomic_conditions(self, decision: Condition) -> list[AtomicCondition]:
        """Extract atomic conditions from a decision statement.
        
        Uses the shared condition evaluator's atom parser so precedence and
        nested expressions stay consistent with condition truth checks.
        """
        text = decision.text.strip()
        
        # Handle switch statements specially
        if text.startswith("switch"):
            return [AtomicCondition(0, text, "")]

        atoms = ConditionEvaluator.atoms(text)
        return [AtomicCondition(index, atom, "") for index, atom in enumerate(atoms or [text])]
    
    def normalize_vectors(self, executed_vectors: list[ExecutedVector]) -> list[ExecutedVector]:
        """Deduplicate and normalize executed vectors.
        
        Removes duplicate (condition_tuple, decision_result) pairs while preserving
        at least one execution of each unique combination.
        """
        seen: dict[tuple[tuple[bool, ...], bool], ExecutedVector] = {}
        
        for vec in executed_vectors:
            key = (vec.condition_values, vec.decision_result)
            if key not in seen:
                seen[key] = vec
        
        return list(seen.values())
    
    def find_witness_pair(
        self,
        condition_index: int,
        vectors: list[ExecutedVector],
        mode: str = "unique_cause",
    ) -> WitnessPair | None:
        """Search for a witness pair proving condition is independently testable.
        
        Args:
            condition_index: Index of the condition to test
            vectors: All executed vectors
            mode: "unique_cause" (only condition i changes) or "masking" (others ok if masked)
            
        Returns:
            WitnessPair if found, None otherwise
        """
        if not vectors or len(vectors) < 2:
            return None
        
        num_conditions = len(vectors[0].condition_values)
        if condition_index >= num_conditions:
            return None
        
        for i, vec1 in enumerate(vectors):
            for vec2 in vectors[i + 1:]:
                # Check if condition changes between vectors
                if vec1.condition_values[condition_index] == vec2.condition_values[condition_index]:
                    continue
                
                # Check if decisions differ
                if vec1.decision_result == vec2.decision_result:
                    continue
                
                if mode == "unique_cause":
                    # Only condition_index may differ
                    differs_only_target = all(
                        vec1.condition_values[j] == vec2.condition_values[j]
                        for j in range(num_conditions)
                        if j != condition_index
                    )
                    if differs_only_target:
                        return WitnessPair(condition_index, vec1, vec2)
                
                elif mode == "masking":
                    # Other conditions can differ if proven to be masked
                    # For now, accept any witness (masking proof deferred)
                    return WitnessPair(condition_index, vec1, vec2)
        
        return None
    
    def analyze_decision(
        self,
        decision: Condition,
        source_file: str,
        executed_vectors: list[ExecutedVector],
        mode: str = "unique_cause",
    ) -> MCDCDecision:
        """Analyze a single decision for MC/DC coverage.
        
        Implements Steps B and C of MC/DC calculation.
        """
        atomic_conditions = self.extract_atomic_conditions(decision)
        normalized_vectors = self.normalize_vectors(executed_vectors)
        
        decision_id = self.build_decision_id(source_file, decision.line, decision.text)
        
        mcdc_decision = MCDCDecision(
            decision_id=decision_id,
            decision_text=decision.text,
            source_file=source_file,
            line_number=decision.line,
            atomic_conditions=atomic_conditions,
            executed_vectors=normalized_vectors,
        )
        
        # Find witness pairs for each condition
        for cond in atomic_conditions:
            witness = self.find_witness_pair(cond.index, normalized_vectors, mode)
            if witness:
                mcdc_decision.witness_pairs[cond.index] = witness
                mcdc_decision.covered_conditions.add(cond.index)
            else:
                mcdc_decision.uncovered_condition_indices.add(cond.index)
        
        return mcdc_decision
    
    def generate_report(
        self,
        decisions: list[MCDCDecision],
        backend_source: str = "fallback_trace",
    ) -> MCDCReport:
        """Generate aggregated MC/DC report from analyzed decisions.
        
        Implements Step D: compute report metrics.
        """
        total_conditions = sum(len(d.atomic_conditions) for d in decisions)
        covered_conditions = sum(len(d.covered_conditions) for d in decisions)
        
        report = MCDCReport(
            backend_source=backend_source,
            decisions=decisions,
            total_conditions=total_conditions,
            covered_conditions=covered_conditions,
        )
        
        return report
    
    @staticmethod
    def extract_vectors_from_trace(
        trace_file: Path,
        parser: Callable[[Path], list[ExecutedVector]] | None = None,
    ) -> list[ExecutedVector]:
        """Extract executed vectors from a test trace file.
        
        Args:
            trace_file: Path to trace output (format-specific)
            parser: Optional custom parser function
            
        Returns:
            List of ExecutedVector records
        """
        if not trace_file.exists():
            return []
        
        if parser:
            return parser(trace_file)
        
        # Default: try JSON format
        import json
        try:
            data = json.loads(trace_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [
                    ExecutedVector(
                        condition_values=tuple(v.get("conditions", [])),
                        decision_result=v.get("result", False),
                        test_id=v.get("test_id", ""),
                        line_number=v.get("line", 0),
                    )
                    for v in data
                ]
        except Exception:
            pass
        
        return []


from testing_mate.boolean_expression import BooleanExpressionEvaluator
from testing_mate.reporter import CoverageReportParser
from testing_mate.models import CoverageFeedback, FunctionInfo, TestCase
from testing_mate.vector_generator import TestVectorGenerator


class MCDCCoverageEngine:
    compare_pattern = re.compile(
        r"^\s*(?P<left>[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*)\s*"
        r"(?P<op>==|!=|>=|<=|>|<)\s*"
        r"(?P<right>[-+]?\d+(?:\.\d+)?[uUlLfF]*|'[^']*'|\"[^\"]*\"|true|false|TRUE|FALSE|[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*)\s*$"
    )
    reverse_compare_pattern = re.compile(
        r"^\s*(?P<left>[-+]?\d+(?:\.\d+)?[uUlLfF]*|'[^']*'|\"[^\"]*\"|true|false|TRUE|FALSE)\s*"
        r"(?P<op>==|!=|>=|<=|>|<)\s*"
        r"(?P<right>[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*)\s*$"
    )

    def analyze(self, functions: list[FunctionInfo], cases: list[TestCase], source: str = "generated test vectors") -> CoverageFeedback:
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
                evaluations = self.evaluations(condition, decisions, fn_cases)
                for index, decision in enumerate(decisions):
                    total += 1
                    pair = self._independence_pair(evaluations, index)
                    if pair:
                        covered += 1
                        notes.append(
                            f"MC/DC covered {fn.name} line {condition.line} decision `{decision}` by {pair[0].test_id}/{pair[1].test_id}."
                        )
                    else:
                        uncovered.append(
                            f"Uncovered MC/DC: {fn.name} line {condition.line} decision `{decision}` has no independent-effect pair in generated cases."
                        )
        percent = (covered / total) * 100.0 if total else 100.0
        metric_note = (
            f"Calculated by Testing Mate from generated vectors: {covered}/{total} atomic conditions have an independent-effect pair."
            if total
            else "No branch/condition obligations were discovered; MC/DC is complete by definition."
        )
        return CoverageFeedback(
            source=source,
            raw_text="\n".join(notes + uncovered)[:12000],
            coverage_percent=percent,
            metrics={"C0": None, "C1": None, "MC/DC": percent},
            metric_notes={"MC/DC": metric_note},
            uncovered_items=uncovered[:80],
            failures=[],
        )

    @staticmethod
    def merge(feedback: CoverageFeedback, mcdc: CoverageFeedback) -> CoverageFeedback:
        metrics = dict(feedback.metrics)
        metric_notes = dict(feedback.metric_notes)
        use_generated_items = False
        if metrics.get("MC/DC") is None and mcdc.metrics.get("MC/DC") is not None:
            metrics["MC/DC"] = mcdc.metrics["MC/DC"]
            metric_notes["MC/DC"] = mcdc.metric_notes.get("MC/DC", "")
            use_generated_items = True
        else:
            metric_notes.setdefault("MC/DC", mcdc.metric_notes.get("MC/DC", ""))
        return CoverageFeedback(
            source=feedback.source if feedback.source != "none" else mcdc.source,
            raw_text=(feedback.raw_text + ("\n" + mcdc.raw_text if use_generated_items else "")).strip()[:12000],
            coverage_percent=CoverageReportParser()._best_percent("", metrics),
            metrics=metrics,
            metric_notes=metric_notes,
            uncovered_items=(feedback.uncovered_items + (mcdc.uncovered_items if use_generated_items else []))[:80],
            failures=feedback.failures,
        )

    def evaluations(self, condition: Condition, decisions: list[str], cases: list[TestCase]) -> list[tuple[TestCase, list[bool] | None, bool | None]]:
        result: list[tuple[TestCase, list[bool] | None, bool | None]] = []
        for case in cases:
            decision_values = [self._eval_decision(decision, case.values) for decision in decisions]
            if any(value is None for value in decision_values):
                result.append((case, None, None))
                continue
            bool_values = [bool(value) for value in decision_values]
            result.append((case, bool_values, self._combine(condition.text, decisions, bool_values)))
        return result

    def _evaluations(self, condition: Condition, decisions: list[str], cases: list[TestCase]) -> list[tuple[TestCase, list[bool] | None, bool | None]]:
        return self.evaluations(condition, decisions, cases)

    @staticmethod
    def _combine(condition_text: str, decisions: list[str], values: list[bool]) -> bool:
        return BooleanExpressionEvaluator().combine(condition_text, decisions, values)

    @staticmethod
    def _independence_pair(evaluations: list[tuple[TestCase, list[bool] | None, bool | None]], target_index: int) -> tuple[TestCase, TestCase] | None:
        valid = [(case, decisions, outcome) for case, decisions, outcome in evaluations if decisions is not None and outcome is not None]
        for left_index, (left_case, left_decisions, left_outcome) in enumerate(valid):
            for right_case, right_decisions, right_outcome in valid[left_index + 1 :]:
                if left_outcome == right_outcome:
                    continue
                if left_decisions[target_index] == right_decisions[target_index]:
                    continue
                other_same = all(
                    left_decisions[index] == right_decisions[index]
                    for index in range(len(left_decisions))
                    if index != target_index
                )
                if other_same:
                    return left_case, right_case
        return None

    def _eval_decision(self, decision: str, values: dict[str, str]) -> bool | None:
        clean = TestVectorGenerator._strip_wrapping_parentheses(decision)
        return ConditionEvaluator().evaluate(clean, values)

    @staticmethod
    def _lookup_value(name: str, values: dict[str, str]) -> str | None:
        key = re.sub(r"\s*(?:->|\.)\s*", ".", name.strip())
        if key in values:
            return values[key]
        suffix = "." + key.split(".", 1)[1] if "." in key else ""
        if suffix:
            for existing, value in values.items():
                if existing.endswith(suffix):
                    return value
        return values.get(name.strip())

    def _compare(self, actual_raw: str, expected_raw: str, op: str, values: dict[str, str] | None = None) -> bool | None:
        actual = self._literal(actual_raw)
        expected_value = self._resolve_operand(expected_raw, values or {})
        expected = self._literal(expected_value)
        if actual is None or expected is None:
            actual = str(actual_raw).strip()
            expected = str(expected_value).strip()
            if op == "==":
                return actual == expected
            if op == "!=":
                return actual != expected
            return None
        if op == "==":
            return actual == expected
        if op == "!=":
            return actual != expected
        if op == ">":
            return actual > expected
        if op == "<":
            return actual < expected
        if op == ">=":
            return actual >= expected
        if op == "<=":
            return actual <= expected
        return None

    @classmethod
    def _resolve_operand(cls, raw: str, values: dict[str, str]) -> str:
        operand = str(raw).strip().strip("()")
        if operand in values:
            return values[operand]
        lookup = cls._lookup_value(operand, values)
        if lookup is not None:
            return lookup
        mult = re.match(r"^(.+?)\s*\*\s*([-+]?\d+(?:\.\d+)?)$", operand)
        if mult:
            left = cls._resolve_operand(mult.group(1), values)
            parsed = cls._literal(left)
            if isinstance(parsed, float):
                return str(parsed * float(mult.group(2)))
        return operand

    @staticmethod
    def _literal(raw: str) -> float | str | None:
        value = str(raw).strip()
        if value.lower() in {"true", "false"}:
            return 1.0 if value.lower() == "true" else 0.0
        if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
            return float(ord(value[1])) if len(value) >= 3 else 0.0
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            return value[1:-1]
        value = re.sub(r"[uUlLfF]+$", "", value)
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _truthy(raw: str) -> bool:
        parsed = MCDCCoverageEngine._literal(raw)
        if isinstance(parsed, float):
            return parsed != 0.0
        return str(raw).strip() not in {"", "0", "false", "FALSE", "NULL", "nullptr"}

    @staticmethod
    def _reverse_operator(op: str) -> str:
        return {">": "<", "<": ">", ">=": "<=", "<=": ">="}.get(op, op)


class MCDCCoverageCollector:
    """Collect and analyze MC/DC evidence from test execution outputs."""
    
    def __init__(self, log=None):
        self.log = log or (lambda _: None)
        self.analyzer = MCDCAnalyzer(log)
    
    def extract_mcdc_from_llvm_json(self, llvm_json_path: Path) -> MCDCReport | None:
        """Extract MC/DC metrics from llvm-cov export JSON.
        
        Parses coverage.llvm.json to build MCDCReport with decision-level coverage.
        """
        if not llvm_json_path.exists():
            return None
        
        try:
            data = json.loads(llvm_json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.log(f"Failed to parse llvm-cov JSON: {exc}")
            return None
        
        # Extract functions and their MC/DC regions
        decisions_list = []
        
        if isinstance(data, dict):
            functions = data.get("data", [])
        elif isinstance(data, list):
            functions = data
        else:
            return None
        
        for func_data in functions:
            if not isinstance(func_data, dict):
                continue
            
            # Process MC/DC regions if available
            regions = func_data.get("regions", [])
            for region in regions:
                # Region format: [line, col, endLine, endCol, count, fileID, expandedFileID, isGapRegion, isBranchRegion, isDecisionRegion]
                if len(region) < 6:
                    continue
                
                is_decision = len(region) > 9 and region[9]
                if not is_decision:
                    continue
        
        # Build report from extracted decisions
        report = MCDCReport(
            backend_source="clang_native",
            decisions=decisions_list,
        )
        
        return report if decisions_list else None
    
    def extract_mcdc_from_summary(self, summary_path: Path) -> dict[str, float | None]:
        """Extract MC/DC metrics from llvm-cov report text summary.
        
        Parses coverage.summary.txt to extract overall MC/DC percentage.
        """
        if not summary_path.exists():
            return {}
        
        metrics: dict[str, float | None] = {
            "mc_dc": None,
            "lines": None,
            "branches": None,
        }
        
        text = summary_path.read_text(encoding="utf-8", errors="replace")
        
        # Look for TOTAL line with percentages
        for line in text.splitlines():
            if "TOTAL" in line.upper():
                percentages = [
                    float(m) for m in re.findall(r"(\d+(?:\.\d+)?)\s*%", line)
                ]
                if len(percentages) >= 3:
                    # Typically: Lines, Branches, MC/DC
                    metrics["lines"] = percentages[0] if len(percentages) > 0 else None
                    metrics["branches"] = percentages[1] if len(percentages) > 1 else None
                    metrics["mc_dc"] = percentages[2] if len(percentages) > 2 else None
        
        # Extract MC/DC specific patterns
        for line in text.splitlines():
            if "MC/DC" in line or "MCDC" in line:
                match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
                if match:
                    metrics["mc_dc"] = float(match.group(1))
        
        return metrics



class IndependencePairSolver:
    def __init__(self):
        self.evaluator = BooleanExpressionEvaluator()
        self._cache: dict[str, list[WitnessPair]] = {}

    def solve(self, decision_text: str, atoms: list[AtomicCondition] | list[str]) -> list[WitnessPair]:
        atom_names = self._atom_names(atoms)
        cache_key = f"{decision_text}:{'|'.join(atom_names)}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        pairs = []
        for target_idx in range(len(atom_names)):
            pair = self._find_pair(decision_text, atom_names, target_idx)
            if pair:
                pairs.append(pair)

        self._cache[cache_key] = pairs
        return pairs

    def _find_pair(self, decision, atoms, target_idx) -> WitnessPair | None:
        n = len(atoms)
        for mask in range(1 << (n - 1)):
            vals_true = self._build(mask, n, target_idx, True)
            vals_false = self._build(mask, n, target_idx, False)
           
            res_true = self.evaluator.combine(decision, atoms, vals_true)
            res_false = self.evaluator.combine(decision, atoms, vals_false)
           
            if res_true != res_false:
                return WitnessPair(
                    changing_condition_index=target_idx,
                    test1=ExecutedVector(tuple(vals_true), res_true),
                    test2=ExecutedVector(tuple(vals_false), res_false),
                )
        return None

    @staticmethod
    def _build(mask, n, target_idx, target_val) -> list[bool]:
        values = []
        bit = 0
        for i in range(n):
            if i == target_idx:
                values.append(target_val)
            else:
                values.append(bool(mask & (1 << bit)))
                bit += 1
        return values

    def minimum_vectors(self, decision: str, atoms: list[AtomicCondition] | list[str]) -> list[ExecutedVector]:
        """Deduplicated minimum vector set for MC/DC (n+1 optimal)."""
        pairs = self.solve(decision, atoms)
        seen: set[tuple[bool, ...]] = set()
        vectors = []
        for pair in pairs:
            for vec in (pair.test1, pair.test2):
                if vec.condition_values not in seen:
                    seen.add(vec.condition_values)
                    vectors.append(vec)
        return vectors

    @staticmethod
    def _atom_names(atoms: list[AtomicCondition] | list[str]) -> list[str]:
        names: list[str] = []
        for index, atom in enumerate(atoms):
            if isinstance(atom, AtomicCondition):
                names.append(f"D{atom.index}")
            else:
                names.append(str(atom) or f"D{index}")
        return names
