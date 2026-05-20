from __future__ import annotations

import re
from pathlib import Path

from testing_mate.boolean_expression import BooleanExpressionEvaluator
from testing_mate.code_database import CodeDatabase, CodeFunctionRecord
from testing_mate.constants import C_KEYWORDS
from testing_mate.generation_workflow import TestGenerationWorkflow, VerificationObjective
from testing_mate.models import Condition, FunctionInfo, Parameter, TestCase

# Maximum vectors to generate per function to prevent explosion
MAX_VECTORS_PER_FUNCTION = 512


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
    def _case(
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
        path_id = values.get("__path_id")
        expected_return = values.get("__expected_return")
        expected_calls = values.get("__expected_calls")
        case_values = {
            name: value
            for name, value in values.items()
            if not name.startswith("__path_") and not name.startswith("__expected_")
        }
        if path_id:
            rationale = f"Execution path {path_id}: {rationale}"
        if expected_calls:
            rationale = f"{rationale}; expected calls: {expected_calls}"
        expected = "Review and replace with concrete expected output/test oracle"
        if expected_return is not None and str(expected_return).strip() not in {"", "void"}:
            expected = f"Expected return: {expected_return}"
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
                    f"1. Apply integration input vector: {param_summary}. "
                    f"2. Invoke integration entry {fn.name}. "
                    "3. Observe function-call arguments/order, return value, output parameters, shared data updates, and propagated status. "
                    f"4. Trace interface/data-flow obligations and condition lines: {condition_lines}."
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
                f"1. Apply input vector: {param_summary}. "
                f"2. Invoke {fn.name}. "
                "3. Capture return value and modified outputs. "
                f"4. Check targeted coverage/condition evidence for lines: {condition_lines}."
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
        params = fn.parameters
        base = {param.name: self._nominal_value(param) for param in params}
        base.update(self._pointer_field_defaults(fn))
        base.update(self._database_pointer_defaults(fn))
        vectors: list[dict[str, str]] = []
        self._append_unique(vectors, base)
        for vec in self._database_execution_path_vectors(fn, base):
            self._append_unique(vectors, vec)
        for vec in self._database_data_flow_vectors(fn, base):
            self._append_unique(vectors, vec)
        for vec in self._path_optimized_vectors(fn, base):
            self._append_unique(vectors, vec)
        if coverage in {"C0/C1", "C1", "MC/DC"}:
            for condition in fn.conditions:
                for vec in self._condition_vectors(params, condition, coverage):
                    self._append_unique(vectors, vec)
        if coverage == "MC/DC":
            for condition in fn.conditions:
                for vec in self._mcdc_pair_vectors(params, condition):
                    self._append_unique(vectors, vec)
            for param in params:
                for value in self._interesting_values(param):
                    vec = dict(base)
                    vec[param.name] = value
                    self._append_unique(vectors, vec)
            for left_index, left in enumerate(params):
                for right in params[left_index + 1 :]:
                    for left_value in self._interesting_values(left)[:5]:
                        for right_value in self._interesting_values(right)[:5]:
                            vec = dict(base)
                            vec[left.name] = left_value
                            vec[right.name] = right_value
                            self._append_unique(vectors, vec)
            for field_key, values in self._pointer_field_values(fn).items():
                for value in values:
                    vec = dict(base)
                    vec[field_key] = value
                    self._append_unique(vectors, vec)
        return vectors[:MAX_VECTORS_PER_FUNCTION]

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
        for target_index in range(len(decisions)):
            assignments = self._mcdc_boolean_assignments(condition.text, decisions, target_index)
            for assignment in assignments:
                vec = dict(base)
                for decision, desired in zip(decisions, assignment):
                    self._drive_decision(vec, decision, desired)
                self._append_unique(vectors, vec)
        return vectors

    @staticmethod
    def _mcdc_boolean_assignments(condition_text: str, decisions: list[str], target_index: int) -> list[tuple[bool, ...]]:
        if not decisions:
            return []
        engine = BooleanExpressionEvaluator()
        result: list[tuple[bool, ...]] = []
        count = 1 << len(decisions)
        for mask in range(count):
            left = tuple(bool(mask & (1 << index)) for index in range(len(decisions)))
            right_list = list(left)
            right_list[target_index] = not right_list[target_index]
            right = tuple(right_list)
            if engine.combine(condition_text, decisions, list(left)) == engine.combine(condition_text, decisions, list(right)):
                continue
            if left not in result:
                result.append(left)
            if right not in result:
                result.append(right)
        return result[:4]

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
