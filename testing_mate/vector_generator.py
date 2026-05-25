from __future__ import annotations

import re
from pathlib import Path

from testing_mate.boolean_expression import BooleanExpressionEvaluator
from testing_mate.code_database import CodeDatabase, CodeFunctionRecord
from testing_mate.constants import C_KEYWORDS
from testing_mate.generation_workflow import ClassificationResult, Complexity, TestGenerationWorkflow, VerificationObjective, classify
from testing_mate.models import Condition, CoverageGap, FunctionInfo, Parameter, TestCase
from testing_mate.boolean_expression import ConditionEvaluator

# Maximum vectors to generate per function to prevent explosion
MAX_VECTORS_PER_FUNCTION = 512
SIMPLE_CONDITION_LIMIT = 1
SIMPLE_PARAMETER_LIMIT = 3
MODERATE_CONDITION_LIMIT = 10


class TestVectorGenerator:
    def __init__(self):
        """Initialize generator with deduplication tracking."""
        self._vector_signatures: set[str] = set()
        self._code_records: dict[str, CodeFunctionRecord] = {}
        self.workflow = TestGenerationWorkflow()
    
    @staticmethod
    def _vector_signature(fn_name: str, values: dict[str, str]) -> str:
        """Generate a signature for a test vector for deduplication.
        
        Signature is based on function name and sorted values, enabling
        identification of truly duplicate test cases.
        """
        # Only include non-internal values in signature
        visible_values = {k: v for k, v in sorted(values.items()) if not k.startswith("__")}
        sig_parts = [fn_name] + [f"{k}={v}" for k, v in visible_values.items()]
        return "|".join(sig_parts)
    
    def _is_duplicate_vector(self, fn_name: str, values: dict[str, str]) -> bool:
        """Check if vector signature has been seen before."""
        sig = self._vector_signature(fn_name, values)
        if sig in self._vector_signatures:
            return True
        self._vector_signatures.add(sig)
        return False

    def generate(
        self,
        source_name: str,
        functions: list[FunctionInfo],
        level: str,
        coverage: str,
        iso_enabled: bool = False,
        asil_level: str = "ASIL D",
        include_static_functions: bool = False,
        code_database: CodeDatabase | None = None,
    ) -> list[TestCase]:
        cases: list[TestCase] = []
        self._vector_signatures.clear()  # Reset deduplication tracking for each generation run
        self._code_records = {record.name: record for record in code_database.functions} if code_database else {}
        callable_names = {fn.name for fn in functions}
        for fn in functions:
            if fn.return_type.strip().startswith("static") and not include_static_functions:
                continue
            if not fn.parameters:
                cases.append(self._case(source_name, fn, level, coverage, 1, {}, "Nominal no-argument call", asil_level))
                seed_vectors = self._vectors_for(fn, coverage) or [{}]
                if level == "IT":
                    cases.extend(self._integration_cases_for(source_name, fn, level, asil_level, seed_vectors, callable_names))
                if iso_enabled:
                    cases.extend(self._workflow_cases_for(source_name, fn, level, coverage, asil_level, seed_vectors))
                continue
            vectors = self._vectors_for(fn, coverage)
            out_index = 1
            for values in vectors:
                # Skip duplicate vectors at case generation time (determinism & audit readiness)
                if self._is_duplicate_vector(fn.name, values):
                    continue
                
                cases.append(self._case(source_name, fn, level, coverage, out_index, values, self._rationale(fn, coverage, out_index), asil_level))
                out_index += 1
            if level == "IT":
                cases.extend(self._integration_cases_for(source_name, fn, level, asil_level, vectors, callable_names))
            if iso_enabled:
                cases.extend(self._workflow_cases_for(source_name, fn, level, coverage, asil_level, vectors))
        return cases

    def generate_for_coverage_gaps(
        self,
        source_name: str,
        functions: list[FunctionInfo],
        existing_cases: list[TestCase],
        gaps: list[CoverageGap],
        level: str,
        coverage: str,
        asil_level: str = "ASIL D",
        code_database: CodeDatabase | None = None,
        max_cases_per_gap: int = 3,
    ) -> list[TestCase]:
        """Generate focused candidate test cases for executed coverage gaps.

        This is a deterministic, local-engine refinement pass used by the GUI
        "Add Gap Cases" action. It consumes uncovered C0/C1/MC/DC gaps parsed
        from clang/llvm-cov or LCOV reports and creates extra vectors that try to
        drive the matching function line/condition. The cases are still reviewable
        candidates; MC/DC credit remains proven only by the next llvm-cov run.
        """
        self._code_records = {record.name: record for record in code_database.functions} if code_database else {}
        by_name = {fn.name: fn for fn in functions}
        result: list[TestCase] = []
        existing_signatures = {
            self._vector_signature(case.function, case.values or {})
            for case in existing_cases
        }
        existing_ids = {case.test_id for case in existing_cases}
        next_index: dict[str, int] = {}
        for case in existing_cases:
            next_index[case.function] = next_index.get(case.function, 0) + 1

        for gap in gaps:
            fn = by_name.get(gap.function) or self._function_for_gap_line(functions, gap.line)
            if fn is None:
                continue
            vectors = self._vectors_for_gap(fn, gap, coverage)[:max(1, max_cases_per_gap)]
            for vector in vectors:
                signature = self._vector_signature(fn.name, vector)
                if signature in existing_signatures:
                    continue
                existing_signatures.add(signature)
                next_index[fn.name] = next_index.get(fn.name, 0) + 1
                idx = next_index[fn.name]
                metric = gap.metric or coverage
                gap_line = gap.line if gap.line is not None else 0
                annotated = dict(vector)
                annotated["__gap_metric"] = metric
                annotated["__gap_line"] = str(gap_line or "")
                annotated["__gap_text"] = str(gap.text or "")[:220]
                annotated["__gap_condition"] = str(gap.condition or "")[:220]
                rationale = (
                    f"Coverage-gap candidate for {metric} at line {gap_line or 'unknown'}; "
                    f"source evidence: {str(gap.text or '').strip()[:180]}"
                )
                case = self._case(source_name, fn, level, f"Gap {metric}", idx, annotated, rationale, asil_level)
                safe_metric = re.sub(r"[^A-Za-z0-9]+", "_", metric).strip("_").lower() or "coverage"
                base_id = f"{fn.name}_gap_{safe_metric}_line_{gap_line or 'unknown'}_{idx:03d}"
                test_id = re.sub(r"[^A-Za-z0-9_]+", "_", base_id).strip("_")
                while test_id in existing_ids:
                    idx += 1
                    test_id = re.sub(r"[^A-Za-z0-9_]+", "_", f"{base_id}_{idx}").strip("_")
                existing_ids.add(test_id)
                case.test_id = test_id
                case.description = f"Add coverage-gap test candidate for {fn.name} line {gap_line or 'unknown'} ({metric})."
                case.rationale = rationale
                case.trace_tags = f"{case.trace_tags};coverage-gap;line-{gap_line or 'unknown'};{metric}"
                result.append(case)
        return result

    @staticmethod
    def _function_for_gap_line(functions: list[FunctionInfo], line: int | None) -> FunctionInfo | None:
        if line is None:
            return None
        for fn in functions:
            if fn.start_line <= line <= fn.end_line:
                return fn
        return None

    @staticmethod
    def _condition_for_gap_line(fn: FunctionInfo, line: int | None, text: str = "") -> Condition | None:
        if line is not None:
            for condition in fn.conditions:
                if condition.line == line:
                    return condition
        lowered = str(text or "").lower()
        for condition in fn.conditions:
            if condition.text and condition.text.lower() in lowered:
                return condition
            for decision in condition.decisions:
                if decision and decision.lower() in lowered:
                    return condition
        return None

    def _vectors_for_gap(self, fn: FunctionInfo, gap: CoverageGap, coverage: str) -> list[dict[str, str]]:
        base = self._base_vector_for(fn)
        metric = (gap.metric or coverage or "").upper()
        text = str(gap.text or "")
        condition = self._condition_for_gap_line(fn, gap.line, text or gap.condition)
        vectors: list[dict[str, str]] = []

        if condition is not None and "MC/DC" in metric:
            for vec in self._mcdc_pair_vectors(fn.parameters, condition):
                merged = dict(base)
                merged.update(vec)
                self._append_unique(vectors, merged)
            return vectors

        if condition is not None:
            desired_values = self._gap_desired_truths(text)
            for desired in desired_values:
                vec = dict(base)
                # Bias earlier conditions toward reachability before toggling the target.
                if gap.line is not None:
                    for previous in fn.conditions:
                        if previous.line < gap.line:
                            self._drive_condition(vec, previous, True)
                self._drive_condition(vec, condition, desired)
                self._append_unique(vectors, vec)
            return vectors

        vec = dict(base)
        if gap.line is not None:
            # For a pure uncovered line, aim to enter the line's containing block by
            # driving previous conditions true. This is a heuristic, then the backend
            # coverage run is the actual evidence.
            for condition in fn.conditions:
                if condition.line <= gap.line:
                    self._drive_condition(vec, condition, True)
        self._append_unique(vectors, vec)

        for path_vec in self._path_optimized_vectors(fn, base)[:4]:
            self._append_unique(vectors, path_vec)
        return vectors

    @staticmethod
    def _gap_desired_truths(text: str) -> list[bool]:
        lowered = str(text or "").lower()
        if "false branch" in lowered or "false" in lowered and "true" not in lowered:
            return [False, True]
        if "true branch" in lowered or "true" in lowered and "false" not in lowered:
            return [True, False]
        return [True, False]

    def _integration_cases_for(
        self,
        source_name: str,
        fn: FunctionInfo,
        level: str,
        asil_level: str,
        base_vectors: list[dict[str, str]],
        callable_names: set[str],
    ) -> list[TestCase]:
        base = dict(base_vectors[0]) if base_vectors else {param.name: self._nominal_value(param) for param in fn.parameters}
        base.update(self._pointer_field_defaults(fn))
        calls = self._internal_calls(fn, callable_names)
        scenarios: list[tuple[str, dict[str, str], str]] = []
        scenarios.append((
            "IT Interface Contract",
            dict(base),
            "Validate integration interface contract: parameter mapping, return value, output pointers, and error/status propagation.",
        ))
        data_flow_vector = self._data_flow_vector(fn, base)
        if data_flow_vector != base:
            scenarios.append((
                "IT Data Flow",
                data_flow_vector,
                "Validate data flow across input parameters, pointer fields, transformed outputs, global/status data, and downstream observable effects.",
            ))
        if calls:
            call_vector = self._call_flow_vector(fn, base)
            scenarios.append((
                "IT Call Flow",
                call_vector,
                "Validate functional call integration: caller input drives callee invocation, call order/arguments are observable, and outputs return to caller.",
            ))
        else:
            interface_variants = self._interface_vectors(fn, [base])[:2]
            for variant in interface_variants:
                scenarios.append((
                    "IT Interface Boundary",
                    variant,
                    "Validate boundary handling at this integration interface, including invalid/null input handling and stable output contract.",
                ))
        result: list[TestCase] = []
        for idx, (coverage, values, rationale) in enumerate(scenarios, start=1):
            result.append(self._case(source_name, fn, level, coverage, idx, values, rationale, asil_level, calls))
        return result

    @staticmethod
    def _internal_calls(fn: FunctionInfo, callable_names: set[str]) -> list[str]:
        calls = []
        for name in sorted(callable_names):
            if name == fn.name:
                continue
            if re.search(rf"\b{re.escape(name)}\s*\(", fn.body):
                calls.append(name)
        return calls

    def _data_flow_vector(self, fn: FunctionInfo, base: dict[str, str]) -> dict[str, str]:
        vector = dict(base)
        for param in fn.parameters:
            if param.kind == "pointer":
                vector[param.name] = "&fixture_" + param.name
            else:
                values = self._interesting_values(param)
                if len(values) > 2:
                    vector[param.name] = values[-2]
        for key, values in self._pointer_field_values(fn).items():
            if values:
                vector[key] = values[-1]
        return vector

    def _call_flow_vector(self, fn: FunctionInfo, base: dict[str, str]) -> dict[str, str]:
        vector = dict(base)
        for condition in fn.conditions[:4]:
            self._drive_condition(vector, condition, True)
        return vector

    def _workflow_cases_for(
        self,
        source_name: str,
        fn: FunctionInfo,
        level: str,
        coverage: str,
        asil_level: str,
        base_vectors: list[dict[str, str]],
    ) -> list[TestCase]:
        result: list[TestCase] = []
        objectives = self.workflow.objectives_for(fn, level, coverage, asil_level, self._record_for(fn))
        for objective in objectives:
            vectors = self._vectors_for_objective(fn, objective, base_vectors)
            for idx, values in enumerate(vectors, start=1):
                result.append(self._case(source_name, fn, level, objective.coverage_label, idx, values, objective.rationale, asil_level))
        return result

    def _vectors_for_objective(
        self,
        fn: FunctionInfo,
        objective: VerificationObjective,
        base_vectors: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        limit = max(1, objective.vector_limit)
        if objective.category == "coverage":
            coverage = "MC/DC" if objective.coverage_metric == "MC/DC" else objective.coverage_metric
            return self._vectors_for(fn, coverage)[:limit]
        if objective.category == "interface":
            return self._interface_vectors(fn, base_vectors)[:limit]
        if objective.category == "fault":
            return self._fault_injection_vectors(fn, base_vectors)[:limit]
        if objective.category == "equivalence":
            return self._equivalence_vectors(fn, base_vectors)[:limit]
        if objective.category == "boundary":
            return self._boundary_vectors(fn, base_vectors)[:limit]
        if objective.category == "error_guessing":
            return self._error_guess_vectors(fn, base_vectors)[:limit]
        if objective.category == "comparison":
            return self._comparison_vectors(fn, base_vectors)[:limit]
        if objective.category == "analysis":
            return self._analysis_vectors(fn, base_vectors)[:limit]
        return self._requirements_vectors(fn, base_vectors)[:limit]

    def _requirements_vectors(self, fn: FunctionInfo, base_vectors: list[dict[str, str]]) -> list[dict[str, str]]:
        base = dict(base_vectors[0]) if base_vectors else {param.name: self._nominal_value(param) for param in fn.parameters}
        vectors = list(base_vectors[:2])
        for vec in self._database_data_flow_vectors(fn, base):
            self._append_unique(vectors, vec)
        for vec in self._path_optimized_vectors(fn, base):
            self._append_unique(vectors, vec)
        return vectors or [base]

    def _analysis_vectors(self, fn: FunctionInfo, base_vectors: list[dict[str, str]]) -> list[dict[str, str]]:
        vectors = self._requirements_vectors(fn, base_vectors)
        base = dict(base_vectors[0]) if base_vectors else {param.name: self._nominal_value(param) for param in fn.parameters}
        for condition in fn.conditions[:3]:
            vec = dict(base)
            self._drive_condition(vec, condition, True)
            self._append_unique(vectors, vec)
        return vectors

    def _comparison_vectors(self, fn: FunctionInfo, base_vectors: list[dict[str, str]]) -> list[dict[str, str]]:
        vectors = list(base_vectors[:1])
        base = dict(base_vectors[0]) if base_vectors else {param.name: self._nominal_value(param) for param in fn.parameters}
        vectors.append(self._data_flow_vector(fn, base))
        if self._record_for(fn) and self._record_for(fn).calls:
            vectors.append(self._call_flow_vector(fn, base))
        result: list[dict[str, str]] = []
        for vector in vectors:
            self._append_unique(result, vector)
        return result or [base]

    def _external_call_names(self, fn: FunctionInfo) -> list[str]:
        record = self._record_for(fn)
        internal_calls = set(record.calls if record else [])
        known_functions = set(self._code_records)
        names: set[str] = set()
        body = self._strip_comments_and_strings(fn.body)
        for match in re.finditer(r"(?<![.#>])\b([A-Za-z_]\w*)\s*\(", body):
            name = match.group(1)
            if name == fn.name or name in C_KEYWORDS or name in internal_calls or name in known_functions:
                continue
            names.add(name)
        return sorted(names)

    @staticmethod
    def _strip_comments_and_strings(text: str) -> str:
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        text = re.sub(r"//.*", " ", text)
        text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
        text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
        return text

    def _interface_vectors(self, fn: FunctionInfo, base_vectors: list[dict[str, str]]) -> list[dict[str, str]]:
        vectors = list(base_vectors[:2])
        base = base_vectors[0] if base_vectors else {}
        for param in fn.parameters:
            vec = dict(base)
            if param.kind == "pointer":
                vec[param.name] = "NULL"
            else:
                vec[param.name] = self._interesting_values(param)[0]
            self._append_unique(vectors, vec)
        return vectors

    def _fault_injection_vectors(self, fn: FunctionInfo, base_vectors: list[dict[str, str]]) -> list[dict[str, str]]:
        vectors: list[dict[str, str]] = []
        base = base_vectors[0] if base_vectors else {}
        for param in fn.parameters:
            vec = dict(base)
            if param.kind == "pointer":
                vec[param.name] = "NULL"
            elif "unsigned" in param.ctype.lower():
                vec[param.name] = "255"
            else:
                values = self._interesting_values(param)
                vec[param.name] = values[0] if values else "0"
            self._append_unique(vectors, vec)
        for call in self._external_call_names(fn)[:3]:
            vec = dict(base)
            vec[f"__stub_return.{call}"] = "1"
            self._append_unique(vectors, vec)
        return vectors or base_vectors[:1]

    def _equivalence_vectors(self, fn: FunctionInfo, base_vectors: list[dict[str, str]]) -> list[dict[str, str]]:
        vectors = list(base_vectors[:3])
        base = base_vectors[0] if base_vectors else {}
        for param in fn.parameters:
            values = self._interesting_values(param)
            if len(values) > 2:
                vec = dict(base)
                vec[param.name] = values[len(values) // 2]
                self._append_unique(vectors, vec)
        return vectors

    def _boundary_vectors(self, fn: FunctionInfo, base_vectors: list[dict[str, str]]) -> list[dict[str, str]]:
        vectors: list[dict[str, str]] = []
        base = base_vectors[0] if base_vectors else {}
        for param in fn.parameters:
            values = self._interesting_values(param)
            for value in (values[:2] + values[-2:]):
                vec = dict(base)
                vec[param.name] = value
                self._append_unique(vectors, vec)
        return vectors or base_vectors[:1]

    def _error_guess_vectors(self, fn: FunctionInfo, base_vectors: list[dict[str, str]]) -> list[dict[str, str]]:
        vectors = self._fault_injection_vectors(fn, base_vectors)
        base = base_vectors[0] if base_vectors else {}
        for key, values in self._pointer_field_values(fn).items():
            vec = dict(base)
            vec[key] = values[0]
            self._append_unique(vectors, vec)
        for call in self._external_call_names(fn)[:3]:
            vec = dict(base)
            vec[f"__stub_return.{call}"] = "1"
            self._append_unique(vectors, vec)
        return vectors


    @staticmethod
    def _metadata_items(obj) -> list:
        items: list = []
        for attr in (
            "fixit_hints", "fixits", "fix_it_hints", "fixItHints", "fixit",
            "hints", "test_hints", "generation_hints", "expected_hints", "analysis_hints",
        ):
            value = getattr(obj, attr, None)
            if isinstance(value, list):
                items.extend(value)
            elif value:
                items.append(value)
        metadata = getattr(obj, "metadata", None)
        if isinstance(metadata, dict):
            for key in ("fixit_hints", "generation_hints", "expected_hints", "analysis_hints"):
                value = metadata.get(key)
                if isinstance(value, list):
                    items.extend(value)
                elif value:
                    items.append(value)
        return items

    @classmethod
    def _fixit_hints_for(cls, fn: FunctionInfo, values: dict[str, str] | None = None) -> list[dict[str, str]]:
        hints: list[dict[str, str]] = []
        for item in cls._metadata_items(fn):
            parsed = cls._normalize_fixit_hint(item)
            if parsed:
                hints.append(parsed)
        if not hints:
            hints.extend(cls._fallback_fixit_hints_from_body(fn))
        return hints

    @classmethod
    def _normalize_fixit_hint(cls, item) -> dict[str, str] | None:
        if isinstance(item, str):
            return cls._parse_text_fixit_hint(item)
        if isinstance(item, dict):
            kind = str(item.get("kind") or item.get("action") or item.get("type") or item.get("category") or "").strip()
            target = str(item.get("target") or item.get("name") or item.get("symbol") or item.get("field") or "").strip()
            value = str(item.get("value") or item.get("replacement") or item.get("expected") or item.get("literal") or "").strip()
            condition = str(item.get("condition") or item.get("when") or item.get("predicate") or "").strip()
        else:
            kind = str(getattr(item, "kind", "") or getattr(item, "action", "") or getattr(item, "type", "")).strip()
            target = str(getattr(item, "target", "") or getattr(item, "name", "") or getattr(item, "symbol", "")).strip()
            value = str(getattr(item, "value", "") or getattr(item, "replacement", "") or getattr(item, "expected", "")).strip()
            condition = str(getattr(item, "condition", "") or getattr(item, "when", "") or "").strip()
        if not kind and target and value:
            kind = "expected_output" if "." in target else "input"
        if not kind or not value:
            return None
        result = {"kind": kind, "target": target, "value": value}
        if condition:
            result["condition"] = condition
        return result

    @staticmethod
    def _parse_text_fixit_hint(text: str) -> dict[str, str] | None:
        clean = " ".join(str(text or "").strip().split())
        patterns = [
            (r"expected\s+return\s*[:=]\s*(.+)$", "expected_return", "return"),
            (r"expected\s+(?:output|out)\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*[:=]\s*(.+)$", "expected_output", None),
            (r"expected\s+(?:global|state)\s+([A-Za-z_]\w*)\s*[:=]\s*(.+)$", "expected_global", None),
            (r"(?:input|param|parameter)\s+([A-Za-z_]\w*)\s*[:=]\s*(.+)$", "input", None),
            (r"local\s+([A-Za-z_]\w*)\s*[:=]\s*(.+)$", "local", None),
            (r"stub\s+([A-Za-z_]\w*)\s*[:=]\s*(.+)$", "stub_return", None),
        ]
        for pattern, kind, fixed_target in patterns:
            match = re.match(pattern, clean, flags=re.I)
            if not match:
                continue
            if fixed_target is None:
                return {"kind": kind, "target": match.group(1), "value": match.group(2)}
            return {"kind": kind, "target": fixed_target, "value": match.group(1)}
        return None

    @staticmethod
    def _local_variable_names(fn: FunctionInfo | None) -> set[str]:
        if fn is None:
            return set()
        body = TestVectorGenerator._strip_comments_and_strings(getattr(fn, "body", "") or "")
        names: set[str] = set()
        declaration = re.compile(
            r"\b(?:const\s+|volatile\s+|static\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|"
            r"char\s+|int\s+|float\s+|double\s+|bool\s+|uint\d+_t\s+|int\d+_t\s+|size_t\s+|[A-Za-z_]\w+_t\s+|[A-Za-z_]\w+\s+)+"
            r"[*\s]*(?P<decls>[^;]+);"
        )
        for match in declaration.finditer(body):
            for item in TestVectorGenerator._split_top_level_commas(match.group("decls")):
                item = item.split("=", 1)[0].strip()
                name_match = re.search(r"(?:\*\s*)?([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?$", item)
                if name_match:
                    names.add(name_match.group(1))
        macro_decl = re.compile(
            r"\b(?:VAR|CONST|P2VAR|P2CONST|CONSTP2VAR|P2FUNC)\s*\([^;()]*\)\s*"
            r"(?P<name>[A-Za-z_]\w*)\s*(?:=|;|\[)"
        )
        for match in macro_decl.finditer(body):
            names.add(match.group("name"))
        for match in re.finditer(r"\breturn\s+([A-Za-z_]\w*)\s*;", body):
            returned = match.group(1)
            if returned in names or returned.lower() in {"ret", "retval", "returnvalue", "result", "status"}:
                names.add(returned)
        return names

    @staticmethod
    def _split_top_level_commas(text: str) -> list[str]:
        parts: list[str] = []
        depth = 0
        current: list[str] = []
        for ch in str(text or ""):
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            if ch == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        tail = "".join(current).strip()
        if tail:
            parts.append(tail)
        return parts

    @staticmethod
    def _parameter_names(fn: FunctionInfo | None) -> set[str]:
        return {getattr(param, "name", "") for param in getattr(fn, "parameters", []) or [] if getattr(param, "name", "")} if fn is not None else set()

    @staticmethod
    def _pointer_parameter_names(fn: FunctionInfo | None) -> set[str]:
        return {getattr(param, "name", "") for param in getattr(fn, "parameters", []) or [] if getattr(param, "name", "") and getattr(param, "kind", "") == "pointer"} if fn is not None else set()

    @staticmethod
    def _global_symbol_names(fn: FunctionInfo | None) -> set[str]:
        if fn is None:
            return set()
        payload = getattr(fn, "global_variable_types", None) or getattr(fn, "global_symbols", None) or {}
        if isinstance(payload, dict):
            return {str(k) for k in payload}
        if isinstance(payload, (set, list, tuple)):
            return {str(item) for item in payload}
        return set()

    @staticmethod
    def _looks_like_global_name(name: str) -> bool:
        return bool(re.match(r"(?:g_|s_|m_|G_|S_|M_|global_|Global|g[A-Z]).+", name or ""))

    @classmethod
    def _scope_kind(cls, fn: FunctionInfo | None, name: str) -> str:
        if not name:
            return "unknown"
        root = str(name).split(".", 1)[0]
        if root in cls._parameter_names(fn):
            return "parameter"
        if root in cls._local_variable_names(fn):
            return "local"
        if root in cls._global_symbol_names(fn) or cls._looks_like_global_name(root):
            return "global"
        return "unknown"

    @classmethod
    def _is_local_symbol(cls, fn: FunctionInfo | None, name: str) -> bool:
        return cls._scope_kind(fn, name) == "local"

    @classmethod
    def _apply_fixit_hints(cls, fn: FunctionInfo, values: dict[str, str]) -> dict[str, str]:
        result = {str(k): str(v) for k, v in (values or {}).items()}
        local_symbols = cls._local_variable_names(fn)
        for hint in cls._fixit_hints_for(fn, result):
            if not cls._fixit_condition_applies(hint.get("condition", ""), result):
                continue
            kind = hint.get("kind", "").lower().replace("-", "_").replace(" ", "_")
            target = hint.get("target", "").strip()
            value = hint.get("value", "").strip()
            if not value:
                continue
            if kind in {"expected_return", "return", "return_value"}:
                result.setdefault("__expected_return", value)
            elif kind in {"expected_output", "output", "out_param", "parameter_output"} and target:
                root = target.split(".", 1)[0]
                if root in cls._pointer_parameter_names(fn):
                    result.setdefault(f"__expected_output.{target}", value)
            elif kind in {"expected_global", "expected_state", "global_expected", "state_expected"} and target:
                if cls._scope_kind(fn, target) == "global":
                    result.setdefault(f"__expected_global.{target}", value)
            elif kind in {"input", "parameter", "param", "precondition"} and target:
                scope = cls._scope_kind(fn, target)
                if scope == "local":
                    result.setdefault(f"__local.{target}", value)
                elif scope == "global":
                    result.setdefault(f"__global.{target}", value)
                else:
                    result.setdefault(target, value)
            elif kind in {"local", "setup_local"} and target:
                if cls._scope_kind(fn, target) != "parameter":
                    result.setdefault(f"__local.{target}", value)
            elif kind in {"global", "setup_global", "input_global"} and target:
                if cls._scope_kind(fn, target) == "global":
                    result.setdefault(f"__global.{target}", value)
            elif kind in {"stub", "stub_return", "mock_return"} and target:
                result.setdefault(f"__stub_return.{target}", value)
        return result

    @classmethod
    def _fixit_condition_applies(cls, condition: str, values: dict[str, str]) -> bool:
        condition = str(condition or "").strip()
        if not condition:
            return True
        result = ConditionEvaluator().evaluate(condition, values)
        return True if result is None else result

    @staticmethod
    def _lookup_vector_value(name: str, values: dict[str, str]) -> str | None:
        key = re.sub(r"\s*(?:->|\.)\s*", ".", name.strip())
        if key in values:
            return values[key]
        if key.startswith("tm_") and key[3:] in values:
            return values[key[3:]]
        return None

    @staticmethod
    def _compare_literal_values(actual_raw: str, expected_raw: str, op: str) -> bool | None:
        def lit(raw: str):
            value = str(raw).strip()
            if value.lower() in {"true", "false"}:
                return 1.0 if value.lower() == "true" else 0.0
            if value in {"NULL", "nullptr"}:
                return 0.0
            value = re.sub(r"[uUlLfF]+$", "", value)
            try:
                return float(value)
            except ValueError:
                return str(raw).strip()
        actual = lit(actual_raw)
        expected = lit(expected_raw)
        try:
            if op == "==": return actual == expected
            if op == "!=": return actual != expected
            if op == ">": return actual > expected
            if op == "<": return actual < expected
            if op == ">=": return actual >= expected
            if op == "<=": return actual <= expected
        except TypeError:
            return None
        return None

    @classmethod
    def _fallback_fixit_hints_from_body(cls, fn: FunctionInfo) -> list[dict[str, str]]:
        # Keep this fallback intentionally small; the analyzer/backend normally attach richer hints.
        body = cls._strip_comments_and_strings(getattr(fn, "body", "") or "")
        hints: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        def add(kind: str, target: str, value: str) -> None:
            value = str(value).strip().rstrip(";")
            if not value:
                return
            key = (kind, target, value)
            if key in seen:
                return
            seen.add(key)
            hints.append({"kind": kind, "target": target, "value": value})
        for match in re.finditer(r"\breturn\s+([-+]?\d+[uUlL]*|[A-Za-z_]\w*|true|false|TRUE|FALSE|NULL|nullptr)\s*;", body):
            add("expected_return", "return", match.group(1))
        for param in getattr(fn, "parameters", []) or []:
            if getattr(param, "kind", "") != "pointer":
                continue
            name = re.escape(getattr(param, "name", ""))
            if not name:
                continue
            for match in re.finditer(rf"\*\s*{name}\s*=\s*([^;]+);", body):
                add("expected_output", getattr(param, "name", ""), match.group(1))
            for match in re.finditer(rf"\b{name}\s*->\s*([A-Za-z_]\w*)\s*=\s*([^;]+);", body):
                add("expected_output", f"{getattr(param, 'name', '')}.{match.group(1)}", match.group(2))
        return hints

    def _case(
        self,
        source_name: str,
        fn: FunctionInfo,
        level: str,
        coverage: str,
        index: int,
        values: dict[str, str],
        rationale: str,
        asil_level: str = "ASIL D",
        integration_calls: list[str] | None = None,
    ) -> TestCase:
        values = TestVectorGenerator._apply_fixit_hints(fn, values)
        values = self._with_reachable_expected_calls(fn, values)
        path_id = values.get("__path_id")
        expected_return = values.get("__expected_return")
        expected_calls = values.get("__expected_calls")
        case_values = {
            name: value
            for name, value in values.items()
            if not name.startswith("__path_")
        }
        if path_id:
            rationale = f"Execution path {path_id}: {rationale}"
        if expected_calls:
            rationale = f"{rationale}; expected calls: {expected_calls}"
        expected_parts: list[str] = []
        if expected_return is not None and str(expected_return).strip() not in {"", "void"}:
            expected_parts.append(f"Expected return: {expected_return}")
        for key, value in sorted(case_values.items()):
            if key.startswith("__expected_output."):
                expected_parts.append(f"Expected output {key.split('.', 1)[1]}: {value}")
            elif key.startswith("__expected_global.") or key.startswith("__expected_state."):
                expected_parts.append(f"Expected global {key.split('.', 1)[1]}: {value}")
        expected = "; ".join(expected_parts) if expected_parts else "Review and replace with concrete expected output/test oracle"
        coverage_id = re.sub(r"[^A-Za-z0-9]+", "_", coverage).strip("_").lower()
        audit = TestVectorGenerator._audit_fields(fn, coverage, level, index, case_values, rationale, asil_level, integration_calls or [])
        return TestCase(
            test_id=f"{fn.name}_{coverage_id}_{index:03d}",
            unit=Path(source_name).stem,
            function=fn.name,
            coverage=coverage,
            level=level,
            values=case_values,
            expected=expected,
            rationale=rationale,
            asil=asil_level,
            **audit,
        )

    def _with_reachable_expected_calls(self, fn: FunctionInfo, values: dict[str, str]) -> dict[str, str]:
        record = self._record_for(fn)
        contexts = getattr(record, "call_contexts", []) if record else []
        if not contexts:
            return values
        result = dict(values)
        expected_calls: list[str] = []
        for context in contexts:
            if not self._call_context_reachable(context, result):
                continue
            name = str(context.get("function") or "").strip()
            if not name:
                continue
            if name not in expected_calls:
                expected_calls.append(name)
            for arg in context.get("arguments", []) or []:
                arg_text = str(arg.get("text") or "").strip()
                resolved = self._resolve_call_argument_value(arg_text, result)
                if resolved is None:
                    continue
                index = str(arg.get("index") or 0)
                result.setdefault(f"__expected_call_arg.{name}.{index}", resolved)
        if expected_calls:
            result["__expected_calls"] = ",".join(expected_calls)
        else:
            result.pop("__expected_calls", None)
            for key in list(result):
                if key.startswith("__expected_call_arg."):
                    result.pop(key, None)
        return result

    @staticmethod
    def _call_context_reachable(context: dict, values: dict[str, str]) -> bool:
        for guard in context.get("guards", []) or []:
            text = str(guard.get("text") or "").strip()
            if not text:
                continue
            evaluated = ConditionEvaluator().evaluate(text, values)
            if evaluated is None:
                return False
            if bool(evaluated) != bool(guard.get("truth", True)):
                return False
        return True

    @staticmethod
    def _resolve_call_argument_value(arg_text: str, values: dict[str, str]) -> str | None:
        clean = re.sub(r"\s+", "", str(arg_text or ""))
        if not clean:
            return None
        aliases = [
            clean,
            clean.replace("->", "."),
            clean.lstrip("&"),
            clean.lstrip("*"),
            clean.replace("[0]", ""),
        ]
        for key in aliases:
            if key in values:
                return str(values[key])
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?[uUlLfF]*|true|false|TRUE|FALSE|NULL|nullptr", clean):
            return clean
        return None

    @staticmethod
    def _audit_fields(
        fn: FunctionInfo,
        coverage: str,
        level: str,
        index: int,
        values: dict[str, str],
        rationale: str,
        asil_level: str = "ASIL D",
        integration_calls: list[str] | None = None,
    ) -> dict[str, str]:
        visible_values = {name: value for name, value in values.items() if not name.startswith("__")}
        setup_values = {name: value for name, value in values.items() if name.startswith("__")}
        param_summary = ", ".join(f"{name}={value}" for name, value in visible_values.items()) or "no input parameters"
        if setup_values:
            param_summary = "no input parameters; harness state: " + ", ".join(
                f"{name.removeprefix('__')}={value}" for name, value in setup_values.items()
            )
        condition_lines = ", ".join(str(condition.line) for condition in fn.conditions) or "none"
        integration_calls = integration_calls or []
        if level == "IT":
            call_summary = ", ".join(integration_calls) if integration_calls else "external or lower-level dependencies through this interface"
            return {
                "description": f"Verify {fn.name} integration behavior for {coverage} using {param_summary}.",
                "preconditions": (
                    "Integrated units are linked with reviewed stubs/mocks for external dependencies; interface fixtures, "
                    "shared data, output buffers, and call monitors are initialized before execution."
                ),
                "setup": (
                    f"Configure integration harness for {fn.name}; initialize caller input data, output storage, shared/global state, "
                    f"and call-observation hooks for: {call_summary}."
                ),
                "test_steps": (
                    f"\n\t\t1. Apply integration input vector: {param_summary}."
                    f"\n\t\t2. Invoke integration entry {fn.name}."
                    "\n\t\t3. Observe function-call arguments/order, return value, output parameters, shared data updates, and propagated status. "
                    f"\n\t\t4. Trace interface/data-flow obligations and condition lines: {condition_lines}.\n"
                ),
                "teardown": "Reset integration stubs/mocks, call logs, shared data, output fixtures, and transient diagnostic state.",
                "pass_fail_criteria": (
                    "Pass when caller input is propagated through expected interfaces, observed callee arguments/return handling match the "
                    "integration contract, output data is correct, and no unexpected side effect occurs."
                ),
                "requirement_ref": f"REQ-{fn.name.upper()}-{coverage.replace('/', '').replace(' ', '')}-{index:03d}",
                "safety_goal_ref": f"SG-{asil_level.replace(' ', '').upper()}-{fn.name.upper()}",
                "trace_tags": f"{level};integration;interface;data-flow;call-flow;{coverage};{rationale}",
            }
        return {
            "description": f"Verify {fn.name} behavior for {coverage} candidate {index} using {param_summary}.",
            "preconditions": (
                "Unit under test is built with the selected compiler; dependent RTE/BSW services, globals, "
                "and pointer fixtures are initialized or stubbed before execution."
            ),
            "setup": (
                "Initialize test harness, reset mocks/stubs, load calibration defaults, set input parameters, "
                "and create valid fixture objects for pointer parameters where required."
            ),
            "test_steps": (
                f"\n\t\t1. Apply input vector: {param_summary}."
                f"\n\t\t2. Invoke {fn.name}."
                "\n\t\t3. Capture return value and modified outputs."
                f"\n\t\t4. Check targeted coverage/condition evidence for lines: {condition_lines}.\n"
            ),
            "teardown": "Reset mocks/stubs, clear transient diagnostic state, release fixtures, and restore default calibration values.",
            "pass_fail_criteria": (
                "Pass when actual return/output values match reviewed expected results and no unexpected safety-state, "
                "diagnostic, memory, or assertion failure occurs."
            ),
            "requirement_ref": f"REQ-{fn.name.upper()}-{coverage.replace('/', '').replace(' ', '')}-{index:03d}",
            "safety_goal_ref": f"SG-{asil_level.replace(' ', '').upper()}-{fn.name.upper()}",
            "trace_tags": f"{level};{coverage};{rationale}",
        }

    def _vectors_for(self, fn: FunctionInfo, coverage: str) -> list[dict[str, str]]:
        classification = self._function_classification(fn)
        if classification.complexity == Complexity.SIMPLE:
            return self._simple_vectors_for(fn, coverage)[:MAX_VECTORS_PER_FUNCTION]
        if classification.complexity == Complexity.MODERATE:
            return self._moderate_vectors_for(fn, coverage)[:MAX_VECTORS_PER_FUNCTION]
        return self._deep_vectors_for(fn, coverage)[:MAX_VECTORS_PER_FUNCTION]

    def _function_classification(self, fn: FunctionInfo) -> ClassificationResult:
        return classify(fn, self._record_for(fn))

    def _function_complexity(self, fn: FunctionInfo) -> str:
        return self._function_classification(fn).complexity.name

    def _base_vector_for(self, fn: FunctionInfo) -> dict[str, str]:
        params = fn.parameters
        base = {param.name: self._nominal_value(param) for param in params}
        base.update(self._pointer_field_defaults(fn))
        base.update(self._database_pointer_defaults(fn))
        return base

    def _simple_vectors_for(self, fn: FunctionInfo, coverage: str) -> list[dict[str, str]]:
        params = fn.parameters
        base = self._base_vector_for(fn)
        vectors: list[dict[str, str]] = []
        self._append_unique(vectors, base)
        if coverage in {"C0/C1", "C1"}:
            for condition in fn.conditions[:1]:
                for vec in self._condition_vectors(params, condition, coverage):
                    merged = dict(base)
                    merged.update(vec)
                    self._append_unique(vectors, merged)
        if coverage == "MC/DC":
            for condition in fn.conditions[:1]:
                for vec in self._mcdc_pair_vectors(params, condition):
                    merged = dict(base)
                    merged.update(vec)
                    self._append_unique(vectors, merged)
        return vectors

    def _moderate_vectors_for(self, fn: FunctionInfo, coverage: str) -> list[dict[str, str]]:
        params = fn.parameters
        base = self._base_vector_for(fn)
        vectors: list[dict[str, str]] = []
        self._append_unique(vectors, base)
        for vec in self._database_execution_path_vectors(fn, base):
            self._append_unique(vectors, vec)
        for vec in self._database_data_flow_vectors(fn, base):
            self._append_unique(vectors, vec)
        for vec in self._path_optimized_vectors(fn, base):
            self._append_unique(vectors, vec)
        for vec in self._boundary_vectors(fn, [base])[:16]:
            self._append_unique(vectors, vec)
        for vec in self._equivalence_vectors(fn, [base])[:16]:
            self._append_unique(vectors, vec)
        if coverage in {"C0/C1", "C1"}:
            for condition in fn.conditions:
                for vec in self._condition_vectors(params, condition, coverage):
                    merged = dict(base)
                    merged.update(vec)
                    self._append_unique(vectors, merged)
        if coverage == "MC/DC":
            for condition in fn.conditions:
                for vec in self._mcdc_pair_vectors(params, condition):
                    merged = dict(base)
                    merged.update(vec)
                    self._append_unique(vectors, merged)
        return vectors

    def _deep_vectors_for(self, fn: FunctionInfo, coverage: str) -> list[dict[str, str]]:
        params = fn.parameters
        base = self._base_vector_for(fn)
        vectors = self._moderate_vectors_for(fn, coverage)
        if coverage == "MC/DC":
            for condition in fn.conditions:
                for vec in self._mcdc_pair_vectors(params, condition):
                    merged = dict(base)
                    merged.update(vec)
                    self._append_unique(vectors, merged)
            for param in params[:8]:
                values = self._interesting_values(param)
                for value in (values[:2] + values[-2:]):
                    vec = dict(base)
                    vec[param.name] = value
                    self._append_unique(vectors, vec)
            for field_key, values in self._pointer_field_values(fn).items():
                for value in (values[:2] + values[-2:]):
                    vec = dict(base)
                    vec[field_key] = value
                    self._append_unique(vectors, vec)
        return vectors

    def _record_for(self, fn: FunctionInfo) -> CodeFunctionRecord | None:
        return self._code_records.get(fn.name)

    def _database_pointer_defaults(self, fn: FunctionInfo) -> dict[str, str]:
        record = self._record_for(fn)
        if not record:
            return {}
        return dict(record.pointer_fields)

    def _database_data_flow_vectors(self, fn: FunctionInfo, base: dict[str, str]) -> list[dict[str, str]]:
        record = self._record_for(fn)
        if not record:
            return []
        vectors: list[dict[str, str]] = []
        graph = record.data_flow_graph or {}
        for edge in graph.get("edges", []):
            kind = edge.get("kind")
            target = str(edge.get("to", ""))
            if kind in {"field_read", "writes"} and ":" in target:
                key = target.split(":", 1)[1]
                if "." in key:
                    vec = dict(base)
                    vec[key] = self._nominal_field_value(key.rsplit(".", 1)[-1])
                    self._append_unique(vectors, vec)
            if kind in {"decision_read", "return_read"}:
                source = str(edge.get("from", ""))
                if source.startswith("param:"):
                    param_name = source.split(":", 1)[1]
                    param = next((item for item in fn.parameters if item.name == param_name), None)
                    if param:
                        for value in self._interesting_values(param)[:3]:
                            vec = dict(base)
                            vec[param_name] = value
                            self._append_unique(vectors, vec)
        return vectors

    def _database_execution_path_vectors(self, fn: FunctionInfo, base: dict[str, str]) -> list[dict[str, str]]:
        record = self._record_for(fn)
        if not record:
            return []
        paths = getattr(record, "execution_paths", []) or []
        vectors: list[dict[str, str]] = []
        for path in paths[:16]:
            vec = dict(base)
            for constraint in path.get("constraints", []):
                text = str(constraint.get("text") or "").strip()
                if not text:
                    continue
                self._drive_decision(vec, text, bool(constraint.get("truth", True)))
            if path.get("expected_return") is not None:
                vec["__expected_return"] = str(path.get("expected_return"))
            if path.get("expected_calls"):
                vec["__expected_calls"] = ",".join(str(item) for item in path.get("expected_calls", []))
            vec["__path_id"] = str(path.get("id") or f"P{len(vectors) + 1}")
            self._append_unique(vectors, vec)
        return vectors

    @staticmethod
    def _append_unique(vectors: list[dict[str, str]], candidate: dict[str, str]) -> None:
        if candidate not in vectors:
            vectors.append(candidate)

    def _path_optimized_vectors(self, fn: FunctionInfo, base: dict[str, str]) -> list[dict[str, str]]:
        if not fn.conditions:
            return []
        vectors: list[dict[str, str]] = []

        fallthrough = dict(base)
        for condition in fn.conditions:
            self._drive_condition(fallthrough, condition, False)
        self._append_unique(vectors, fallthrough)

        all_true = dict(base)
        for condition in fn.conditions:
            self._drive_condition(all_true, condition, True)
        self._append_unique(vectors, all_true)

        for index, condition in enumerate(fn.conditions):
            prefix = dict(base)
            for previous in fn.conditions[:index]:
                self._drive_condition(prefix, previous, previous.depth < condition.depth)
            for target_true in (False, True):
                vec = dict(prefix)
                self._drive_condition(vec, condition, target_true)
                self._append_unique(vectors, vec)
        return vectors

    def _drive_condition(self, vector: dict[str, str], condition: Condition, target_true: bool) -> None:
        decisions = condition.decisions or [condition.text]
        if "&&" in condition.text:
            if target_true:
                for decision in decisions:
                    self._drive_decision(vector, decision, True)
            elif decisions:
                self._drive_decision(vector, decisions[0], False)
            return
        if "||" in condition.text:
            if target_true and decisions:
                self._drive_decision(vector, decisions[0], True)
            else:
                for decision in decisions:
                    self._drive_decision(vector, decision, False)
            return
        for decision in decisions:
            self._drive_decision(vector, decision, target_true)

    def _condition_vectors(self, params: list[Parameter], condition: Condition, coverage: str) -> list[dict[str, str]]:
        base = {param.name: self._nominal_value(param) for param in params}
        base.update(self._pointer_field_defaults_from_params(params, condition.text))
        vectors: list[dict[str, str]] = []
        decisions = condition.decisions or [condition.text]
        if "&&" in condition.text:
            true_vec = dict(base)
            for decision in decisions:
                self._drive_decision(true_vec, decision, True)
            vectors.append(true_vec)
            for decision in decisions:
                false_vec = dict(true_vec)
                self._drive_decision(false_vec, decision, False)
                vectors.append(false_vec)
        elif "||" in condition.text:
            false_vec = dict(base)
            for decision in decisions:
                self._drive_decision(false_vec, decision, False)
            vectors.append(false_vec)
            for decision in decisions:
                true_vec = dict(false_vec)
                self._drive_decision(true_vec, decision, True)
                vectors.append(true_vec)
        else:
            for decision in decisions:
                true_vec = dict(base)
                false_vec = dict(base)
                self._drive_decision(true_vec, decision, True)
                self._drive_decision(false_vec, decision, False)
                vectors.extend([true_vec, false_vec])
        return vectors

    def _mcdc_pair_vectors(self, params: list[Parameter], condition: Condition) -> list[dict[str, str]]:
        base = {param.name: self._nominal_value(param) for param in params}
        base.update(self._pointer_field_defaults_from_params(params, condition.text))
        decisions = condition.decisions or [condition.text]
        vectors: list[dict[str, str]] = []
        for assignment in self._mcdc_boolean_assignments(condition.text, decisions):
            vec = dict(base)
            for decision, desired in zip(decisions, assignment):
                self._drive_decision(vec, decision, desired)
            self._append_unique(vectors, vec)
        return vectors

    @staticmethod
    def _mcdc_boolean_assignments(condition_text: str, decisions: list[str], target_index: int | None = None) -> list[tuple[bool, ...]]:
        if not decisions:
            return []
        expr = str(condition_text or "")
        atom_names = [f"D{index}" for index in range(len(decisions))]
        for index, decision in enumerate(decisions):
            expr = re.sub(re.escape(decision), atom_names[index], expr, count=1)
        from testing_mate.mcdc_engine import IndependencePairSolver

        solver = IndependencePairSolver()
        if target_index is None:
            return [vector.condition_values for vector in solver.minimum_vectors(expr, atom_names)]
        for pair in solver.solve(expr, atom_names):
            if pair.changing_condition_index == target_index:
                return [pair.test1.condition_values, pair.test2.condition_values]
        return []

    @staticmethod
    def _direct_mcdc_pair(
        condition_text: str,
        decisions: list[str],
        target_index: int,
        engine: BooleanExpressionEvaluator,
    ) -> list[tuple[bool, ...]]:
        clean = re.sub(r"\([^()]*\)", "", condition_text)
        if "||" not in clean and "&&" in clean:
            left = tuple(index != target_index for index in range(len(decisions)))
            right = tuple(True for _ in decisions)
        elif "&&" not in clean and "||" in clean:
            left = tuple(False for _ in decisions)
            right = tuple(index == target_index for index in range(len(decisions)))
        else:
            return []
        if engine.combine(condition_text, decisions, list(left)) == engine.combine(condition_text, decisions, list(right)):
            return []
        return [left, right]

    @staticmethod
    def _mcdc_candidate_contexts(count: int, target_index: int) -> list[tuple[bool, ...]]:
        candidates: list[tuple[bool, ...]] = []

        def add(values: list[bool]) -> None:
            values[target_index] = False
            item = tuple(values)
            if item not in candidates:
                candidates.append(item)

        add([False] * count)
        add([True] * count)
        for index in range(count):
            if index == target_index:
                continue
            values = [False] * count
            values[index] = True
            add(values)
        for index in range(count):
            if index == target_index:
                continue
            values = [True] * count
            values[index] = False
            add(values)
        for split in range(count + 1):
            add([index < split for index in range(count)])
            add([index >= split for index in range(count)])
        return candidates

    @staticmethod
    def _nominal_value(param: Parameter) -> str:
        ctype = param.ctype.lower()
        if param.kind == "pointer":
            return "&fixture_" + param.name
        enum_value = TestVectorGenerator._nominal_enum_value(param)
        if enum_value:
            return enum_value
        if "float" in ctype or "double" in ctype:
            return "1.0"
        if "char" in ctype and "signed" not in ctype and "unsigned" not in ctype:
            return "'A'"
        if "bool" in ctype or "_boolean" in ctype:
            return "1"
        return "1"

    @staticmethod
    def _interesting_values(param: Parameter) -> list[str]:
        ctype = param.ctype.lower()
        if param.kind == "pointer":
            return ["NULL", "&fixture_" + param.name]
        enum_values = TestVectorGenerator._enum_values(param)
        if enum_values:
            return enum_values
        if "float" in ctype or "double" in ctype:
            return ["-1.0", "0.0", "1.0", "2.0"]
        if "char" in ctype and "signed" not in ctype and "unsigned" not in ctype:
            return ["'\\0'", "'A'", "'Z'"]
        if "unsigned" in ctype:
            return ["0", "1", "2", "3", "15", "16", "255"]
        return ["-2", "-1", "0", "1", "2", "3", "15", "16"]

    @staticmethod
    def _nominal_enum_value(param: Parameter) -> str | None:
        values = TestVectorGenerator._enum_values(param)
        return values[0] if values else None

    @staticmethod
    def _enum_values(param: Parameter) -> list[str]:
        return list(param.enum_values)

    def _drive_decision(self, vector: dict[str, str], decision: str, target_true: bool) -> None:
        decision = decision.strip()
        decision = self._strip_wrapping_parentheses(decision)
        decision = re.sub(r"\([A-Za-z_]\w+\)\s*", "", decision)
        field_match = re.search(r"\b([A-Za-z_]\w*)\s*->\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\b\s*(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?u?)", decision)
        if field_match:
            key = self._field_key(field_match.group(1), field_match.group(2))
            key = self._resolve_vector_key(key, vector)
            if key in vector:
                raw = field_match.group(4).rstrip("uU")
                number = float(raw) if "." in raw else int(raw)
                vector[key] = self._value_for_compare(number, field_match.group(3), target_true)
                return
        enum_match = re.search(r"\b([A-Za-z_]\w*)\b\s*(==|!=)\s*\b([A-Z][A-Za-z0-9_]+)\b", decision)
        if enum_match and enum_match.group(1) in vector:
            name, op, constant = enum_match.group(1), enum_match.group(2), enum_match.group(3)
            vector[name] = constant if (op == "==") == target_true else "0"
            return
        enum_match = re.search(r"\b([A-Z][A-Za-z0-9_]+)\b\s*(==|!=)\s*\b([A-Za-z_]\w*)\b", decision)
        if enum_match and enum_match.group(3) in vector:
            constant, op, name = enum_match.group(1), enum_match.group(2), enum_match.group(3)
            vector[name] = constant if (op == "==") == target_true else "0"
            return
        negated_field = re.fullmatch(r"!\s*([A-Za-z_]\w*)\s*->\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)", decision)
        if negated_field:
            key = self._field_key(negated_field.group(1), negated_field.group(2))
            key = self._resolve_vector_key(key, vector)
            if key in vector:
                vector[key] = "0" if target_true else "1"
                return
        bare_field = re.fullmatch(r"([A-Za-z_]\w*)\s*->\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)", decision)
        if bare_field:
            key = self._field_key(bare_field.group(1), bare_field.group(2))
            key = self._resolve_vector_key(key, vector)
            if key in vector:
                vector[key] = "1" if target_true else "0"
                return
        negated = re.fullmatch(r"!\s*([A-Za-z_]\w*)", decision)
        if negated and negated.group(1) in vector:
            vector[negated.group(1)] = "0" if target_true else "1"
            return
        match = re.search(r"\b([A-Za-z_]\w*)\b\s*(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)", decision)
        if match and match.group(1) in vector:
            name, op, raw = match.group(1), match.group(2), match.group(3)
            number = float(raw) if "." in raw else int(raw)
            vector[name] = self._value_for_compare(number, op, target_true)
            return
        match = re.search(r"\b([A-Za-z_]\w*)\b\s*(==|!=|>=|<=|>|<)\s*([A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*)", decision)
        if match and match.group(1) in vector:
            key = self._resolve_vector_key(re.sub(r"\s*(?:->|\.)\s*", ".", match.group(3)), vector)
            raw = vector.get(key)
            if raw is not None:
                parsed = self._literal(raw)
                if isinstance(parsed, float):
                    vector[match.group(1)] = self._value_for_compare(parsed, match.group(2), target_true)
                    return
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*(==|!=|>=|<=|>|<)\s*\b([A-Za-z_]\w*)\b", decision)
        if match and match.group(3) in vector:
            number = float(match.group(1)) if "." in match.group(1) else int(match.group(1))
            op = self._flip_operator(match.group(2))
            vector[match.group(3)] = self._value_for_compare(number, op, target_true)
            return
        match = re.search(r"\b([A-Za-z_]\w*)\b", decision)
        if match and match.group(1) in vector:
            vector[match.group(1)] = "1" if target_true else "0"

    @staticmethod
    def _field_key(param_name: str, field_path: str) -> str:
        clean_path = re.sub(r"\s+", "", field_path)
        return f"{param_name}.{clean_path}"

    @staticmethod
    def _resolve_vector_key(key: str, vector: dict[str, str]) -> str:
        if key in vector:
            return key
        suffix = "." + key.split(".", 1)[1] if "." in key else ""
        if suffix:
            for existing in vector:
                if existing.endswith(suffix):
                    return existing
        return key

    @staticmethod
    def _strip_wrapping_parentheses(text: str) -> str:
        clean = text.strip()
        changed = True
        while changed and clean.startswith("(") and clean.endswith(")"):
            changed = False
            depth = 0
            balanced_outer = True
            for index, ch in enumerate(clean):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0 and index != len(clean) - 1:
                        balanced_outer = False
                        break
            if balanced_outer:
                clean = clean[1:-1].strip()
                changed = True
        return clean

    @staticmethod
    def _value_for_compare(number: float | int, op: str, target_true: bool) -> str:
        if op == "==":
            value = number if target_true else number + 1
        elif op == "!=":
            value = number + 1 if target_true else number
        elif op == ">":
            value = number + 1 if target_true else number
        elif op == ">=":
            value = number if target_true else number - 1
        elif op == "<":
            value = number - 1 if target_true else number
        else:
            value = number if target_true else number + 1
        return str(int(value)) if float(value).is_integer() else str(value)

    @staticmethod
    def _flip_operator(op: str) -> str:
        return {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "!=": "!="}[op]

    @staticmethod
    def _literal(raw: str) -> float | str | None:
        value = str(raw).strip()
        if value in {"true", "TRUE"}:
            return 1.0
        if value in {"false", "FALSE", "NULL", "nullptr"}:
            return 0.0
        value = re.sub(r"[uUlLfF]+$", "", value)
        try:
            return float(value)
        except ValueError:
            return value

    @staticmethod
    def _rationale(fn: FunctionInfo, coverage: str, idx: int) -> str:
        if coverage == "C0":
            return "Nominal statement coverage candidate"
        if coverage in {"C0/C1", "C1"}:
            return f"Branch/decision coverage candidate for {fn.name}"
        return f"MC/DC independence candidate {idx} for {fn.name}; review coupled conditions"

    @staticmethod
    def _pointer_field_defaults(fn: FunctionInfo) -> dict[str, str]:
        condition_text = "\n".join(condition.text for condition in fn.conditions)
        defaults = TestVectorGenerator._pointer_field_defaults_from_params(fn.parameters, condition_text)
        defaults.update(TestVectorGenerator._pointer_field_read_defaults_from_body(fn))
        defaults.update(TestVectorGenerator._pointer_field_alias_defaults(fn))
        return defaults

    @staticmethod
    def _pointer_field_read_defaults_from_body(fn: FunctionInfo) -> dict[str, str]:
        defaults: dict[str, str] = {}
        for param in fn.parameters:
            if param.kind != "pointer":
                continue
            pattern = rf"(?<![&.])\b{re.escape(param.name)}\s*->\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\b(?!\s*=)"
            for field in sorted(set(re.findall(pattern, fn.body))):
                clean_field = re.sub(r"\s+", "", field)
                defaults[f"{param.name}.{clean_field}"] = TestVectorGenerator._nominal_field_value(clean_field)
        return defaults

    @staticmethod
    def _pointer_field_alias_defaults(fn: FunctionInfo) -> dict[str, str]:
        defaults: dict[str, str] = {}
        pointer_params = [param.name for param in fn.parameters if param.kind == "pointer"]
        if not pointer_params:
            return defaults
        alias_map: dict[str, str] = {}
        for param_name in pointer_params:
            pattern = rf"\b([A-Za-z_]\w*)\s*=\s*[A-Za-z_]\w*\s*\(\s*{re.escape(param_name)}\s*\)"
            for alias in re.findall(pattern, fn.body):
                alias_map[alias] = param_name
        if not alias_map:
            return defaults
        for alias, param_name in alias_map.items():
            pattern = rf"\b{re.escape(alias)}\s*->\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)"
            for field in sorted(set(re.findall(pattern, fn.body))):
                clean_field = re.sub(r"\s+", "", field)
                defaults[f"{param_name}.{clean_field}"] = TestVectorGenerator._nominal_field_value(clean_field)
        return defaults

    @staticmethod
    def _pointer_field_defaults_from_params(params: list[Parameter], text: str) -> dict[str, str]:
        defaults: dict[str, str] = {}
        for param in params:
            if param.kind != "pointer":
                continue
            fields = set(re.findall(rf"\b{re.escape(param.name)}\s*->\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)", text))
            for field in sorted(fields):
                clean_field = re.sub(r"\s+", "", field)
                defaults[f"{param.name}.{clean_field}"] = TestVectorGenerator._nominal_field_value(clean_field)
        return defaults

    @staticmethod
    def _pointer_field_values(fn: FunctionInfo) -> dict[str, list[str]]:
        values = {key: ["0", "1", "2", "3", "15", "16", "255"] for key in TestVectorGenerator._pointer_field_defaults(fn)}
        for condition in fn.conditions:
            for param in fn.parameters:
                if param.kind != "pointer":
                    continue
                pattern = rf"\b{re.escape(param.name)}\s*->\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\b\s*(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?u?)"
                for field, op, raw in re.findall(pattern, condition.text):
                    field = re.sub(r"\s+", "", field)
                    clean = raw.rstrip("uU")
                    number = float(clean) if "." in clean else int(clean)
                    candidates = {
                        TestVectorGenerator._value_for_compare(number, op, True),
                        TestVectorGenerator._value_for_compare(number, op, False),
                        TestVectorGenerator._nominal_field_value(field),
                    }
                    values[f"{param.name}.{field}"] = sorted(candidates, key=str)
        return values

    @staticmethod
    def _nominal_field_value(field: str) -> str:
        lowered = field.lower()
        if any(token in lowered for token in ("valid", "fresh", "ok", "enable", "allow")):
            return "1"
        if any(token in lowered for token in ("age", "counter", "threshold", "max", "min", "delta")):
            return "1"
        return "1"
