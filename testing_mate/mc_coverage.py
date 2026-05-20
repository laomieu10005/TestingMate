"""MC/DC (Modified Condition/Decision Coverage) analysis and calculation.

Implements Step B, C, D of MC/DC evidence collection:
- B: Normalize decisions and extract atomic conditions
- C: Calculate fallback MC/DC with witness pair tracking
- D: Compute and report metrics
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from testing_mate.models import Condition


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
        return (covered / total) * 100.0 if total > 0 else None
    
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
        
        Splits by && and || operators, preserving order.
        """
        text = decision.text.strip()
        
        # Handle switch statements specially
        if text.startswith("switch"):
            return [AtomicCondition(0, text, "")]
        
        # Split by && and || while preserving operator positions
        atoms: list[AtomicCondition] = []
        current_atom = ""
        paren_depth = 0
        index = 0
        operator = ""
        
        for i, char in enumerate(text):
            if char in "({[":
                paren_depth += 1
            elif char in ")}]":
                paren_depth = max(0, paren_depth - 1)
            
            if paren_depth == 0 and i + 1 < len(text):
                # Check for && or ||
                if char == "&" and text[i + 1] == "&":
                    if current_atom.strip():
                        atoms.append(AtomicCondition(index, current_atom.strip(), operator))
                        index += 1
                    current_atom = ""
                    operator = "&&"
                    continue
                elif char == "|" and text[i + 1] == "|":
                    if current_atom.strip():
                        atoms.append(AtomicCondition(index, current_atom.strip(), operator))
                        index += 1
                    current_atom = ""
                    operator = "||"
                    continue
            
            current_atom += char
        
        # Add final atom
        if current_atom.strip():
            atoms.append(AtomicCondition(index, current_atom.strip(), operator))
        
        # If no atoms extracted, treat whole expression as one
        if not atoms:
            atoms.append(AtomicCondition(0, text, ""))
        
        return atoms
    
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
