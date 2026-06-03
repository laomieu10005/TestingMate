"""
Efficient LLM data packaging and structuring for optimized inference.

This module pre-processes and packages function analysis data into
lightweight JSON structures optimized for LLM consumption, reducing
token overhead and enabling efficient parallel processing.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from testing_mate.code_database import CodeDatabase, CodeFunctionRecord
from testing_mate.models import CoverageGap, CoverageFeedback, FunctionInfo, TestCase
from testing_mate.vector_generator import TestVectorGenerator


class LLMDataPackager:
    """Efficient structural packaging of function analysis data for LLM consumption.
    
    Balances between providing sufficient context and minimizing token overhead.
    All data is pre-processed, deduplicated, and structured for fast LLM inference.
    """

    # Token budgets per component (rough estimates for Ollama)
    TOKENS_PER_CONDITION = 80
    TOKENS_PER_VECTOR = 40
    TOKENS_PER_CHUNK = 100
    TOKENS_PER_PATH = 50
    
    # Limits to stay within reasonable token budget for 3b model (4096 context)
    MAX_CONDITIONS = 12
    MAX_VECTORS = 8
    MAX_CODE_CHUNKS = 3
    MAX_PATHS = 4
    MAX_EXISTING_CASES = 6

    def __init__(self, max_tokens: int = 3000):
        """Initialize packager with token budget.
        
        Args:
            max_tokens: Maximum tokens to allocate for request data (excluding base prompt)
        """
        self.max_tokens = max_tokens
        self._token_used = 0

    @classmethod
    def package_function_for_generation(
        cls,
        source_name: str,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        coverage: str,
        code_database: CodeDatabase | None = None,
        gap: CoverageGap | None = None,
    ) -> dict[str, Any]:
        """Package function context for initial generation or gap refinement.
        
        Returns optimized JSON with:
        - Compressed function logic (conditions, parameters, calls)
        - Pre-extracted vectors (both existing and candidates)
        - Code chunks (only relevant lines)
        - Execution paths (filtered by gap if provided)
        - Dedupe signatures
        """
        packager = cls()
        record = cls._get_record(code_database, fn.name)
        
        return {
            "source_file": source_name,
            "function_name": fn.name,
            "return_type": fn.return_type,
            "parameter_count": len(fn.parameters),
            "parameters": [
                {
                    "name": p.name,
                    "type": p.ctype,
                    "kind": cls._parameter_kind(p.ctype),
                }
                for p in fn.parameters
            ],
            "logic_summary": packager._compress_logic(fn, record),
            "code_chunks": packager._select_code_chunks(fn, gap),
            "conditions": packager._compress_conditions(fn, gap),
            "vectors": packager._package_vectors(fn, existing_cases, coverage, gap, code_database),
            "paths": packager._package_paths(record, gap),
            "recent_cases": packager._compress_existing_cases(fn, existing_cases),
            "dedupe_signatures": [
                TestVectorGenerator._vector_signature(case.function, case.values or {})
                for case in existing_cases
                if case.function == fn.name
            ][-12:],
            "metadata": {
                "condition_count": len(fn.conditions),
                "has_branches": bool(fn.conditions),
                "has_external_calls": bool(record and record.external_calls) if record else False,
                "gap_target": cls._gap_target(gap) if gap else None,
            },
        }

    @classmethod
    def package_batch_functions(
        cls,
        source_name: str,
        functions: list[FunctionInfo],
        existing_cases: list[TestCase],
        coverage: str,
        code_database: CodeDatabase | None = None,
    ) -> dict[str, Any]:
        """Package multiple functions for batch generation.
        
        Efficiently groups related functions and shares common context
        to reduce redundant data and token overhead.
        """
        packager = cls()
        function_payloads = []
        
        for fn in functions:
            payload = cls.package_function_for_generation(
                source_name, fn, existing_cases, coverage, code_database
            )
            function_payloads.append(payload)
        
        return {
            "task": "batch_test_generation",
            "source": source_name,
            "function_count": len(functions),
            "functions": function_payloads,
            "shared_context": {
                "coverage_target": coverage,
                "analyzer": code_database.analyzer if code_database else "function_info",
                "total_existing_cases": len(existing_cases),
            },
        }

    @classmethod
    def package_gap_refinement(
        cls,
        source_name: str,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        gap: CoverageGap,
        coverage: str,
        feedback: CoverageFeedback | None = None,
        code_database: CodeDatabase | None = None,
    ) -> dict[str, Any]:
        """Package data specifically for coverage gap refinement.
        
        Focuses on the gap target, nearby conditions, and relevant vectors
        while filtering out unrelated code and execution paths.
        """
        packager = cls()
        record = cls._get_record(code_database, fn.name)
        
        return {
            "task": "coverage_gap_refinement",
            "source": source_name,
            "gap_id": f"{source_name}:{fn.name}:{gap.metric}:L{gap.line or '?'}",
            "gap_target": packager._gap_target(gap),
            "coverage_metric": gap.metric or coverage,
            "function": {
                "name": fn.name,
                "return_type": fn.return_type,
                "parameters": [
                    {"name": p.name, "type": p.ctype}
                    for p in fn.parameters
                ],
            },
            "nearby_conditions": packager._nearby_conditions(fn, gap),
            "code_context": packager._gap_code_context(fn, gap),
            "vectors_for_gap": packager._vectors_for_gap(fn, existing_cases, gap, coverage, code_database),
            "feedback_evidence": packager._feedback_chunk(feedback, gap) if feedback else {},
            "recent_cases": packager._compress_existing_cases(fn, existing_cases, limit=4),
            "dedupe_against": [
                TestVectorGenerator._vector_signature(case.function, case.values or {})
                for case in existing_cases
                if case.function == fn.name
            ][-8:],
        }

    @staticmethod
    def _parameter_kind(param_type: str) -> str:
        """Classify parameter kind for efficient LLM processing."""
        t = param_type.strip()
        if "*" in t:
            return "pointer"
        if any(x in t for x in ["enum", "int", "float", "double", "bool"]):
            return "scalar"
        return "struct"

    @staticmethod
    def _get_record(code_database: CodeDatabase | None, fn_name: str) -> CodeFunctionRecord | None:
        """Get code record for function, or None if unavailable."""
        if code_database is None:
            return None
        return next(
            (r for r in code_database.functions if r.name == fn_name),
            None,
        )

    @classmethod
    def _compress_logic(cls, fn: FunctionInfo, record: CodeFunctionRecord | None) -> dict[str, Any]:
        """Compress function logic into minimal essential information."""
        logic = {
            "name": fn.name,
            "condition_count": len(fn.conditions),
            "decision_count": sum(max(1, len(c.decisions or [])) for c in fn.conditions),
        }
        
        if record:
            logic.update({
                "call_count": len(record.external_calls or []),
                "has_globals": bool((record.globals_read or []) or (record.globals_written or [])),
                "return_count": len(record.return_statements or []),
            })
        
        return logic

    @classmethod
    def _compress_conditions(cls, fn: FunctionInfo, gap: CoverageGap | None) -> list[dict[str, Any]]:
        """Extract and compress only relevant conditions."""
        conditions = sorted(fn.conditions, key=lambda c: (c.line, c.depth))
        
        if gap and gap.line:
            # Prioritize conditions near gap
            nearby = [c for c in conditions if abs(c.line - gap.line) <= 6]
            other = [c for c in conditions if abs(c.line - gap.line) > 6]
            conditions = (nearby + other)[:cls.MAX_CONDITIONS]
        else:
            conditions = conditions[:cls.MAX_CONDITIONS]
        
        return [
            {
                "line": c.line,
                "depth": c.depth,
                "text": c.text[:120],  # Truncate long conditions
                "decisions": (c.decisions or [c.text])[:4],  # Top decisions only
            }
            for c in conditions
        ]

    @classmethod
    def _select_code_chunks(
        cls,
        fn: FunctionInfo,
        gap: CoverageGap | None,
    ) -> list[dict[str, Any]]:
        """Select most relevant code chunks intelligently."""
        lines = fn.body.splitlines()
        if not lines:
            return []
        
        chunks = []
        context_lines = 6  # Reduce from 8 to save tokens
        
        # Chunk 1: Gap-focused (if provided)
        if gap and gap.line:
            local_idx = gap.line - fn.start_line
            start = max(0, local_idx - context_lines)
            end = min(len(lines), local_idx + context_lines + 1)
            chunks.append(cls._format_chunk(fn, lines, start, end, "gap_target"))
        
        # Chunk 2: First condition
        if fn.conditions:
            first_cond = fn.conditions[0]
            local_idx = max(0, first_cond.line - fn.start_line)
            start = max(0, local_idx - context_lines // 2)
            end = min(len(lines), local_idx + context_lines // 2 + 1)
            chunks.append(cls._format_chunk(fn, lines, start, end, "entry_condition"))
        
        # Chunk 3: Function start (if different from above)
        if not chunks or (fn.conditions and fn.conditions[0].line > fn.start_line + 10):
            start = 0
            end = min(len(lines), 8)
            chunks.append(cls._format_chunk(fn, lines, start, end, "function_entry"))
        
        return chunks[:cls.MAX_CODE_CHUNKS]

    @staticmethod
    def _format_chunk(
        fn: FunctionInfo,
        lines: list[str],
        start: int,
        end: int,
        label: str,
    ) -> dict[str, Any]:
        """Format a code chunk with line numbers."""
        chunk_lines = [
            f"{fn.start_line + i}: {lines[i]}"
            for i in range(start, end)
        ]
        return {
            "label": label,
            "line_range": [fn.start_line + start, fn.start_line + end - 1],
            "lines": "\n".join(chunk_lines),
        }

    @classmethod
    def _gap_code_context(cls, fn: FunctionInfo, gap: CoverageGap) -> dict[str, Any]:
        """Provide minimal code context specifically for gap target."""
        if gap.line is None:
            return {}
        
        lines = fn.body.splitlines()
        local_idx = max(0, min(len(lines) - 1, gap.line - fn.start_line))
        context = 4
        start = max(0, local_idx - context)
        end = min(len(lines), local_idx + context + 1)
        
        chunk_lines = [
            f"{fn.start_line + i}: {lines[i]}"
            for i in range(start, end)
        ]
        
        return {
            "target_line": gap.line,
            "line_range": [fn.start_line + start, fn.start_line + end - 1],
            "code": "\n".join(chunk_lines),
            "gap_metric": gap.metric or "coverage",
            "gap_evidence": gap.text[:200] if gap.text else "",
        }

    @classmethod
    def _nearby_conditions(cls, fn: FunctionInfo, gap: CoverageGap) -> list[dict[str, Any]]:
        """Extract conditions near gap target."""
        if gap.line is None:
            candidates = fn.conditions
        else:
            candidates = [
                c for c in fn.conditions
                if abs(c.line - gap.line) <= 10 or c.text == gap.condition
            ]
        
        if not candidates:
            candidates = fn.conditions[:4]
        
        return [
            {
                "line": c.line,
                "text": c.text[:100],
                "distance_from_gap": abs(c.line - gap.line) if gap.line else None,
            }
            for c in sorted(candidates, key=lambda c: (abs(c.line - gap.line) if gap.line else 999, c.line))[:6]
        ]

    @classmethod
    def _package_vectors(
        cls,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        coverage: str,
        gap: CoverageGap | None,
        code_database: CodeDatabase | None,
    ) -> list[dict[str, Any]]:
        """Package candidate vectors for generation."""
        generator = TestVectorGenerator()
        if code_database:
            generator._code_records = {r.name: r for r in code_database.functions}
        
        try:
            if gap:
                vectors = generator._vectors_for_gap(fn, gap, coverage)
            else:
                vectors = generator._vectors_for(fn, coverage)
        except Exception:
            vectors = []
        
        existing_sigs = {
            TestVectorGenerator._vector_signature(c.function, c.values or {})
            for c in existing_cases
            if c.function == fn.name
        }
        
        result = []
        seen = set()
        
        for idx, values in enumerate(vectors, 1):
            sig = TestVectorGenerator._vector_signature(fn.name, values)
            if sig in seen:
                continue
            seen.add(sig)
            
            result.append({
                "id": f"V{idx}",
                "values": dict(values),
                "already_exists": sig in existing_sigs,
                "source": "gap_vector" if gap else "function_vector",
            })
            
            if len(result) >= cls.MAX_VECTORS:
                break
        
        return result

    @classmethod
    def _vectors_for_gap(
        cls,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        gap: CoverageGap,
        coverage: str,
        code_database: CodeDatabase | None,
    ) -> list[dict[str, Any]]:
        """Package vectors specifically for gap refinement."""
        generator = TestVectorGenerator()
        if code_database:
            generator._code_records = {r.name: r for r in code_database.functions}
        
        try:
            vectors = generator._vectors_for_gap(fn, gap, coverage)[:8]
        except Exception:
            vectors = []
        
        result = []
        for idx, values in enumerate(vectors, 1):
            result.append({
                "id": f"GV{idx}",
                "values": dict(values),
            })
        
        return result

    @staticmethod
    def _package_paths(
        record: CodeFunctionRecord | None,
        gap: CoverageGap | None,
    ) -> list[dict[str, Any]]:
        """Package execution paths, filtered by gap if provided."""
        if record is None:
            return []
        
        paths = getattr(record, "execution_paths", []) or []
        
        if gap and gap.line:
            # Filter paths that mention gap line
            def mentions_gap(path):
                constraints = path.get("constraints", [])
                return any(
                    (isinstance(c, dict) and c.get("line") == gap.line)
                    or str(c) == str(gap.line)
                    for c in constraints
                )
            
            filtered = [p for p in paths if mentions_gap(p)]
            paths = filtered or paths
        
        return [
            {
                "id": p.get("id", ""),
                "conditions": p.get("conditions", [])[:4],
                "constraints": p.get("constraints", [])[:4],
                "expected_return": p.get("expected_return", ""),
            }
            for p in paths[:LLMDataPackager.MAX_PATHS]
        ]

    @classmethod
    def _compress_existing_cases(
        cls,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Compress existing test cases for reference."""
        limit = limit or cls.MAX_EXISTING_CASES
        cases = [c for c in existing_cases if c.function == fn.name][-limit:]
        
        return [
            {
                "test_id": c.test_id,
                "values_count": len(c.values or {}),
                "coverage": c.coverage,
                "rationale": c.rationale[:120],
            }
            for c in cases
        ]

    @staticmethod
    def _feedback_chunk(feedback: CoverageFeedback | None, gap: CoverageGap | None) -> dict[str, Any]:
        """Extract relevant feedback chunk."""
        if feedback is None:
            return {}
        
        return {
            "source": feedback.source,
            "metric": gap.metric if gap else None,
            "uncovered_items": feedback.uncovered_items[:2],
            "failures": feedback.failures[:2],
        }

    @staticmethod
    def _gap_target(gap: CoverageGap | None) -> dict[str, Any]:
        """Format gap target information."""
        if gap is None:
            return {}
        
        return {
            "line": gap.line,
            "metric": gap.metric or "coverage",
            "condition": gap.condition[:80] if gap.condition else "",
            "text": gap.text[:150] if gap.text else "",
            "description": f"Line {gap.line}: {gap.metric or 'coverage'} gap",
        }

    @staticmethod
    def _gap_target_static(gap: CoverageGap | None) -> dict[str, Any]:
        """Static method version of _gap_target."""
        return LLMDataPackager._gap_target(gap)
