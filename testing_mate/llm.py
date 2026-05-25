import json
import os
import re
import socket
import subprocess
import urllib.request
from dataclasses import asdict
from pathlib import Path
from testing_mate.models import *
from testing_mate.vector_generator import TestVectorGenerator
from testing_mate.code_database import *
from testing_mate.coverage_gap import CoverageGap, CoverageGapPlanner
from testing_mate.utilities import has_report_evidence, merge_cases, feedback_meets_target
from typing import Callable

class OllamaClient:
    def __init__(self, model: str = "qwen2.5-coder:3b", host: str = "http://localhost:11434", timeout: int | None = None):
        self.model = model.strip() or "qwen2.5-coder:3b"
        self.host = os.environ.get("TESTMATE_OLLAMA_HOST", host).rstrip("/")
        self.timeout = self._env_int("TESTMATE_OLLAMA_TIMEOUT", timeout if timeout is not None else 45, minimum=5)
        self.num_ctx = self._env_int("TESTMATE_OLLAMA_NUM_CTX", 4096, minimum=1024)
        self.num_predict = self._env_int("TESTMATE_OLLAMA_NUM_PREDICT", 512, minimum=128)

        # Do not pull the model automatically on every refine run. Pulling can block for minutes
        # and looks like an Ollama generation timeout. Enable explicitly only when desired.
        if self._env_bool("TESTMATE_OLLAMA_AUTO_PULL", False):
            self._pull_model_once()

    @staticmethod
    def _env_int(name: str, defaults: int, minimum: int | None = None) -> int:
        try:
            value = int(os.environ.get(name, str(defaults)))
        except ValueError:
            value = defaults
        if minimum is not None:
            value = max(minimum, value)
        return value

    @staticmethod
    def _env_bool(name: str, defaults: bool = False) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return defaults
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def _pull_model_once(self) -> None:
        try:
            subprocess.run(
                ["ollama", "pull", self.model],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(self.timeout, 30),
                check=False,
            )
        except Exception:
            # A failed pull should not prevent a local model that already exists from running.
            pass

    def generate_json(self, prompt: str) -> dict:
        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.1,
                    "num_ctx": self.num_ctx,
                    "num_predict": self.num_predict,
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.host}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(
                f"Ollama request timed out after {self.timeout}s. "
                "Use a smaller model, lower TESTMATE_OLLAMA_NUM_CTX/NUM_PREDICT, "
                "or increase TESTMATE_OLLAMA_TIMEOUT."
            ) from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama request failed. Start Ollama and run `ollama pull {self.model}`. Detail: {exc}") from exc
        raw = body.get("response", "")
        return self._loads_json(raw)

    @staticmethod
    def _loads_json(raw: str) -> dict:
        raw = raw.strip()
        if not raw:
            return {}
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"(\{.*\}|\[.*\])", raw, flags=re.S)
            if match:
                payload = json.loads(match.group(0))
            else:
                raise
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            return {"test_cases": payload}
        raise ValueError("Ollama response JSON must be an object or a test case array.")


class OllamaTestRefiner:
    def __init__(self, client: OllamaClient):
        self.client = client

    def refine(
        self,
        source_name: str,
        functions: list[FunctionInfo],
        existing_cases: list[TestCase],
        feedback: CoverageFeedback,
        coverage: str,
        level: str,
        target_percent: float,
        iteration: int,
        backend: str = "GoogleTest",
    ) -> tuple[list[TestCase], str]:
        prompt = self._prompt(source_name, functions, existing_cases, feedback, coverage, target_percent, backend)
        payload = self.client.generate_json(prompt)
        notes = self._response_notes(payload)
        raw_cases = self._response_cases(payload)
        if not isinstance(raw_cases, list):
            return [], "Ollama response did not contain a test_cases array."
        cases = self._cases_from_payload(source_name, functions, raw_cases, coverage, level, iteration)
        return cases, notes

    def refine_gap(
        self,
        source_name: str,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        feedback: CoverageFeedback,
        gap: CoverageGap,
        coverage: str,
        level: str,
        target_percent: float,
        iteration: int,
        backend: str = "GoogleTest",
        code_database: CodeDatabase | None = None,
    ) -> tuple[list[TestCase], str]:
        prompt = self._gap_prompt(source_name, fn, existing_cases, feedback, gap, coverage, target_percent, backend, code_database)
        payload = self.client.generate_json(prompt)
        notes = self._response_notes(payload)
        raw_cases = self._response_cases(payload)
        if not isinstance(raw_cases, list):
            return [], "Ollama response did not contain a test_cases array."
        cases = self._cases_from_payload(source_name, [fn], raw_cases, coverage, level, iteration)
        return cases, notes
    
    def refine_oracle(
        self,
        source_name: str,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        iteration: int,
        backend: str = "GoogleTest",
    ) -> tuple[list[TestCase], str]:
        """Generate oracle values (expected outputs) for non-void functions.
        
        This stage fills in expected values using LLM reasoning about function behavior.
        Validates that expected values are C-literals, enums, or valid casts.
        Results are tagged as advisory for human review.
        """
        if fn.return_type.strip() == "void":
            return [], "Function returns void; oracle generation not applicable."
        
        samples = [case for case in existing_cases if case.function == fn.name][:5]
        if not samples:
            return [], "No existing test cases to generate oracles for."
        
        request = {
            "task": f"Generate expected output values (oracle) for test cases of {fn.name}.",
            "backend": backend,
            "function": {
                "name": fn.name,
                "return_type": fn.return_type,
                "parameters": [asdict(param) for param in fn.parameters],
            },
            "sample_test_cases": [
                {
                    "values": case.values,
                    "current_expected": case.expected,
                    "rationale": case.rationale,
                }
                for case in samples
            ],
            "output_schema": {
                "analysis": "reasoning about function behavior and output constraints",
                "test_cases": [
                    {
                        "function": fn.name,
                        "values": {"parameter_name": "input value"},
                        "expected": "C literal or enum constant matching return_type",
                        "rationale": "why this output is expected for these inputs",
                    }
                ],
            },
        }
        
        prompt = (
            f"You are a senior C developer. Generate realistic expected output values (oracles) for {fn.name} "
            f"based on its return type ({fn.return_type}) and parameter constraints. "
            "Return strict JSON only. Validate that expected output is a C-literal or enum that matches the return type. "
            "If output cannot be inferred, use: ORACLE_REVIEW_REQUIRED. Mark your analysis with reasoning.\n\n"
            + json.dumps(request, indent=2)
        )
        
        try:
            payload = self.client.generate_json(prompt)
            notes = self._response_notes(payload)
            raw_cases = self._response_cases(payload)
            if not isinstance(raw_cases, list):
                return [], "Oracle generation did not produce test_cases array."
            
            cases = self._cases_from_payload(source_name, [fn], raw_cases, "oracle", "UT", iteration)
            # Tag oracle cases for reviewer awareness
            for case in cases:
                case.rationale = "ORACLE: " + case.rationale
            
            return cases, notes
        except Exception as exc:
            return [], f"Oracle generation error: {exc}"
    
    def refine_negative(
        self,
        source_name: str,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        iteration: int,
        backend: str = "GoogleTest",
    ) -> tuple[list[TestCase], str]:
        """Generate negative/security test cases for robustness verification.
        
        Covers boundary violations, invalid enums, NULL/overflow scenarios, and
        robustness aspects aligned with ISO/SAE 21434 security testing guidance.
        Results are tagged as SECURITY: for audit trail.
        """
        request = {
            "task": f"Generate negative/robustness test vectors for {fn.name} (security/boundary testing).",
            "backend": backend,
            "function": {
                "name": fn.name,
                "return_type": fn.return_type,
                "parameters": [asdict(param) for param in fn.parameters],
            },
            "test_intent": "Verify robustness: NULL pointers, invalid enum values, boundary violations, overflow conditions, "
                          "uninitialized inputs, and constraint violations per ISO/SAE 21434.",
            "output_schema": {
                "analysis": "security/robustness gaps this set addresses",
                "test_cases": [
                    {
                        "function": fn.name,
                        "values": {"parameter_name": "invalid/boundary/overflow value"},
                        "expected": "expected behavior (error code, safe fallback, assertion, or fault)",
                        "rationale": "which security/robustness aspect or boundary this targets",
                    }
                ],
            },
        }
        
        prompt = (
            f"You are a security/robustness test engineer. Generate negative test vectors for {fn.name} "
            "to verify boundary condition handling, invalid input rejection, and safe error paths. "
            "Cover: NULL pointers, invalid enum values, overflow/underflow, uninitialized data, and constraint violations. "
            "Return strict JSON only. Prefer realistic malformed inputs that might expose weaknesses. "
            "Mark your analysis with security/robustness concerns.\n\n"
            + json.dumps(request, indent=2)
        )
        
        try:
            payload = self.client.generate_json(prompt)
            notes = self._response_notes(payload)
            raw_cases = self._response_cases(payload)
            if not isinstance(raw_cases, list):
                return [], "Negative test generation did not produce test_cases array."
            
            cases = self._cases_from_payload(source_name, [fn], raw_cases, "security", "UT", iteration)
            # Tag negative cases for reviewer awareness and traceability
            for case in cases:
                case.rationale = "SECURITY: " + case.rationale
            
            return cases, notes
        except Exception as exc:
            return [], f"Negative test generation error: {exc}"

    def _prompt(
        self,
        source_name: str,
        functions: list[FunctionInfo],
        existing_cases: list[TestCase],
        feedback: CoverageFeedback,
        coverage: str,
        target_percent: float,
        backend: str,
    ) -> str:
        has_feedback = has_report_evidence(feedback)
        function_names = {fn.name for fn in functions}
        condition_limit = 4 if has_feedback else 3
        case_limit = 8 if has_feedback else 3
        function_payload = [self._function_chunk(fn, condition_limit) for fn in functions]
        existing_payload = [
            self._case_chunk(case)
            for case in existing_cases
            if case.function in function_names
        ][-case_limit:]
        request = {
            "task": (
                f"Generate additional {backend} C unit-test vectors from this coverage feedback chunk."
                if has_feedback
                else f"Enhance initial {backend} C unit-test generation for this function chunk only."
            ),
            "source": source_name,
            "backend": backend,
            "request_type": "initial_function_chunk" if not has_feedback else "coverage_feedback_chunk",
            "coverage_target": coverage,
            "target_percent": target_percent,
            "coverage_feedback_chunk": self._compact_feedback(feedback, has_feedback),
            "functions": function_payload,
            "recent_existing_cases_for_these_functions": existing_payload,
            "acceptance_rules": self._acceptance_rules(backend),
            "response_contract": self._response_contract(),
            "output_schema": {
                "analysis": "short reason for the missing coverage and failed cases",
                "test_cases": [
                    {
                        "function": "function name from input only",
                        "values": {"parameter_name": "C literal value"},
                        "expected": "expected result or Review and replace with concrete expected output/test oracle",
                        "rationale": "which uncovered branch, condition, or failure this targets",
                        "coverage_intent": "C0, C1 true, C1 false, or MC/DC pair target",
                    }
                ],
            },
        }
        return (
            f"You are a senior C unit-test engineer using the {backend} backend. "
            "Return strict JSON only. Do not use markdown. Generate at most 3 test_cases. "
            "This is a small chunked request; do not ask for or assume missing project context. "
            "Only generate cases for listed functions and parameters. "
            "This tool must work for any C project, so do not invent project-specific symbols or enum constants. "
            "Prefer small C literals. For MC/DC, vary one atomic condition at a time where possible. "
            f"If expected output cannot be inferred, use the exact string Review/complete expected result in {backend}.\n\n"
            + json.dumps(request, indent=2)
        )

    @staticmethod
    def _function_chunk(fn: FunctionInfo, condition_limit: int) -> dict:
        conditions = sorted(fn.conditions, key=lambda condition: (condition.depth, condition.line))[:condition_limit]
        return {
            "name": fn.name,
            "return_type": fn.return_type,
            "parameters": [asdict(param) for param in fn.parameters],
            "conditions": [asdict(condition) for condition in conditions],
            "fixit_hints": getattr(fn, "fixit_hints", [])[:20],
            "condition_count": len(fn.conditions),
        }

    @staticmethod
    def _compact_feedback(feedback: CoverageFeedback, include_items: bool) -> dict:
        if not include_items:
            return {"source": "none", "metrics": {}, "uncovered_items": [], "failures": []}
        return {
            "source": feedback.source,
            "metrics": feedback.metrics,
            "metric_notes": {key: value[:240] for key, value in feedback.metric_notes.items()},
            "uncovered_items": [item[:280] for item in feedback.uncovered_items[:4]],
            "failures": [item[:280] for item in feedback.failures[:3]],
        }

    @staticmethod
    def _case_chunk(case: TestCase) -> dict:
        value_items = list(case.values.items())
        return {
            "function": case.function,
            "sample_values": dict(value_items[:8]),
            "value_count": len(value_items),
            "rationale": case.rationale[:180],
        }

    def _gap_prompt(
        self,
        source_name: str,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        feedback: CoverageFeedback,
        gap: CoverageGap,
        coverage: str,
        target_percent: float,
        backend: str,
        code_database: CodeDatabase | None = None,
    ) -> str:
        request = self.gap_request(source_name, fn, existing_cases, feedback, gap, coverage, target_percent, backend, code_database)
        return (
            f"You are a senior C unit-test engineer using {backend}. "
            "Return strict JSON only. Generate at most 4 test_cases. "
            "Use code_database_chunk as the source of truth for function shape, conditions, calls, and field hints. "
            "Focus only on coverage_feedback_chunk; do not request or assume the full report. "
            "Only use the listed function name and its parameters or allowed_pointer_fields. "
            "Do not invent symbols that are absent from code_database_chunk. "
            "Prefer small scalar values and explicit pointer-field values. "
            "For C1, force the missing true or false decision outcome. "
            "For MC/DC, create pairs where one atomic condition changes and the overall decision changes.\n\n"
            + json.dumps(request, indent=2)
        )

    @classmethod
    def gap_request(
        cls,
        source_name: str,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        feedback: CoverageFeedback,
        gap: CoverageGap,
        coverage: str,
        target_percent: float,
        backend: str,
        code_database: CodeDatabase | None = None,
    ) -> dict:
        nearby_conditions = [
            asdict(condition)
            for condition in fn.conditions
            if gap.line is None or abs(condition.line - gap.line) <= 8 or condition.text == gap.condition
        ] or [asdict(condition) for condition in fn.conditions[:2]]
        existing_payload = [
            {"values": case.values, "rationale": case.rationale}
            for case in existing_cases
            if case.function == fn.name
        ][-3:]
        feedback_chunk = cls._feedback_chunk(feedback, gap, fn.name)
        return {
            "task": f"Generate additional {backend} test vectors from this coverage feedback chunk only.",
            "request_type": "coverage_gap_chunk",
            "chunk_id": cls._chunk_id(source_name, gap),
            "source": source_name,
            "backend": backend,
            "coverage_target": coverage,
            "target_percent": target_percent,
            "target_gap": cls._gap_target(gap),
            "coverage_feedback_chunk": feedback_chunk,
            "function": {
                "name": fn.name,
                "return_type": fn.return_type,
                "parameters": [asdict(param) for param in fn.parameters],
                "nearby_conditions": nearby_conditions,
                "allowed_pointer_fields": TestVectorGenerator._pointer_field_defaults(fn),
            },
            "code_database_chunk": (
                code_database.function_chunk(fn.name, existing_cases)
                if code_database is not None
                else {
                    "source_file": source_name,
                    "analyzer": "function_info",
                    "functions": [cls._function_chunk(fn, 8)],
                    "recent_engine_cases": existing_payload,
                }
            ),
            "recent_existing_cases_for_function": existing_payload,
            "dedupe_against": [cls._case_signature(case) for case in existing_cases if case.function == fn.name][-12:],
            "acceptance_rules": cls._acceptance_rules(backend),
            "response_contract": cls._response_contract(),
            "output_schema": {
                "analysis": "short reason these vectors target the gap",
                "test_cases": [
                    {
                        "function": fn.name,
                        "values": {"parameter_or_pointer_field": "C literal value"},
                        "expected": "Review and replace with concrete expected output/test oracle",
                        "rationale": "specific uncovered line/branch/MC/DC point targeted",
                        "coverage_intent": "exact missing true/false branch, line, condition, or MC/DC pair",
                    }
                ],
            },
        }

    @staticmethod
    def _feedback_chunk(feedback: CoverageFeedback, gap: CoverageGap, function_name: str) -> dict:
        def relevant(items: list[str], limit: int) -> list[str]:
            result: list[str] = []
            tokens = [function_name.lower(), str(gap.line or ""), gap.metric.lower()]
            for item in items:
                lowered = item.lower()
                if any(token and token in lowered for token in tokens):
                    result.append(item[:320])
                if len(result) >= limit:
                    break
            return result

        metric_notes = {
            key: value
            for key, value in feedback.metric_notes.items()
            if key in {gap.metric, "C0", "C1", "MC/DC"} and value
        }
        return {
            "source": feedback.source,
            "metrics": feedback.metrics,
            "metric_notes": metric_notes,
            "target_gap": asdict(gap),
            "related_uncovered_items": relevant(feedback.uncovered_items, 4),
            "related_failures": relevant(feedback.failures, 3),
        }

    @staticmethod
    def _chunk_id(source_name: str, gap: CoverageGap) -> str:
        raw = f"{Path(source_name).stem}_{gap.metric}_{gap.function}_{gap.line or 'na'}_{gap.condition or gap.text[:40]}"
        return re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()[:120]

    @staticmethod
    def _gap_target(gap: CoverageGap) -> dict:
        text = f"{gap.text} {gap.condition}".lower()
        missing_outcome = ""
        if "missing true" in text or "true outcome" in text:
            missing_outcome = "true"
        elif "missing false" in text or "false outcome" in text:
            missing_outcome = "false"
        return {
            "function": gap.function,
            "line": gap.line,
            "metric": gap.metric,
            "condition": gap.condition,
            "missing_outcome": missing_outcome,
            "evidence": gap.text,
        }

    @staticmethod
    def _case_signature(case: TestCase) -> dict:
        return {
            "function": case.function,
            "values": dict(sorted(case.values.items())),
            "coverage": case.coverage,
        }

    @staticmethod
    def _acceptance_rules(backend: str) -> list[str]:
        return [
            "Return a JSON object with analysis and test_cases only; do not return markdown.",
            "Each test case must use exactly one listed function name.",
            "Each values key should be a listed parameter, allowed_pointer_fields key, or an internal oracle key such as __expected_return, __expected_output.<param.field>, __expected_global.<symbol>, __stub_return.<call>, __global.<symbol>, or __local.<name>.",
            "Use small C literals, enum constants present in the input, pointer-field assignments, or fixit_hints when provided by the analyzer.",
            "Avoid duplicating any values combination listed in dedupe_against or recent_existing_cases.",
            f"Use Review/complete expected result in {backend} when a reliable oracle cannot be inferred.",
        ]

    @staticmethod
    def _response_contract() -> dict:
        return {
            "analysis": "short string explaining the coverage strategy",
            "test_cases": [
                {
                    "function": "listed function name",
                    "values": {"listed_parameter_or_pointer_field_or_internal_oracle_key": "C literal"},
                    "fixit_hints": [{"kind": "expected_return|expected_output|expected_global|input|global|local|stub_return", "target": "return|name|param.field", "value": "C literal", "condition": "optional condition"}],
                    "expected": "C literal, enum, or review placeholder",
                    "rationale": "targeted coverage gap",
                    "coverage_intent": "optional machine-readable line/branch/MC/DC target",
                }
            ],
        }

    @staticmethod
    def _response_notes(payload: dict) -> str:
        parts = []
        for key in ("analysis", "coverage_strategy", "validation_notes"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        return " ".join(parts)[:600] if parts else "Ollama returned candidate vectors."

    @staticmethod
    def _response_cases(payload: dict) -> list:
        for key in ("test_cases", "cases", "vectors"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        enhancement = payload.get("coverage_enhancement")
        if isinstance(enhancement, dict):
            for key in ("test_cases", "cases", "vectors"):
                value = enhancement.get(key)
                if isinstance(value, list):
                    return value
        return []

    @staticmethod
    def _values_from_response_fixit_hints(raw_hints) -> dict[str, str]:
        if not isinstance(raw_hints, list):
            return {}
        result: dict[str, str] = {}
        for hint in raw_hints:
            if not isinstance(hint, dict):
                continue
            kind = str(hint.get("kind") or hint.get("type") or hint.get("action") or "").lower().replace("-", "_").strip()
            target = str(hint.get("target") or hint.get("name") or hint.get("symbol") or "").strip()
            value = str(hint.get("value") or hint.get("expected") or hint.get("replacement") or "").strip()
            if not kind or not value:
                continue
            if kind in {"expected_return", "return", "return_value"}:
                result.setdefault("__expected_return", value)
            elif kind in {"expected_output", "output", "out_param", "parameter_output"} and target:
                result.setdefault(f"__expected_output.{target}", value)
            elif kind in {"expected_global", "expected_state", "global_expected", "state_expected"} and target:
                result.setdefault(f"__expected_global.{target}", value)
            elif kind in {"stub", "stub_return", "mock_return"} and target:
                result.setdefault(f"__stub_return.{target}", value)
            elif kind in {"global", "setup_global", "input_global"} and target:
                result.setdefault(f"__global.{target}", value)
            elif kind in {"local", "setup_local"} and target:
                result.setdefault(f"__local.{target}", value)
            elif kind in {"input", "parameter", "param"} and target:
                result.setdefault(target, value)
        return result
    def _cases_from_payload(
        self,
        source_name: str,
        functions: list[FunctionInfo],
        raw_cases: list[dict],
        coverage: str,
        level: str,
        iteration: int,
    ) -> list[TestCase]:
        by_name = {fn.name: fn for fn in functions}
        result: list[TestCase] = []
        counters: dict[str, int] = {}
        coverage_id = re.sub(r"[^A-Za-z0-9]+", "_", coverage).strip("_").lower()
        for raw in raw_cases:
            if not isinstance(raw, dict):
                continue
            function_name = str(raw.get("function", "")).strip()
            fn = by_name.get(function_name)
            if not fn:
                continue
            allowed = {param.name for param in fn.parameters}
            allowed.update(TestVectorGenerator._pointer_field_defaults(fn))
            values = raw.get("values", {})
            if not isinstance(values, dict):
                continue
            internal_prefixes = (
                "__expected_return", "__expect_return", "expected_return",
                "__expected_output.", "__expect_output.", "expected.output.",
                "__expected_global.", "__expected_state.", "expected.global.", "expected.state.",
                "__stub_return.", "__global.", "__set_global.", "__input_global.",
                "__local.", "local.", "__var.",
            )
            clean_values = {
                str(name): str(value)
                for name, value in values.items()
                if str(name) in allowed or str(name).startswith(internal_prefixes)
            }
            clean_values.update(self._values_from_response_fixit_hints(raw.get("fixit_hints") or raw.get("hints") or []))
            raw_expected = str(raw.get("expected") or "").strip()
            if raw_expected and raw_expected not in {"Review and replace with concrete expected output/test oracle", "ORACLE_REVIEW_REQUIRED"}:
                if "__expected_return" not in clean_values and (fn.return_type or "").strip() and (fn.return_type or "").strip() != "void":
                    if re.fullmatch(r"[-+]?0[xX][0-9A-Fa-f]+[uUlL]*|[-+]?\d+(?:[uUlL]*)|[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?[fFlL]?|[A-Za-z_]\w*|true|false|TRUE|FALSE|NULL|nullptr", raw_expected):
                        clean_values["__expected_return"] = raw_expected
            required_params = {param.name for param in fn.parameters}
            if not required_params.issubset(clean_values):
                for param in fn.parameters:
                    clean_values.setdefault(param.name, TestVectorGenerator._nominal_value(param))
            counters[function_name] = counters.get(function_name, 0) + 1
            result.append(
                TestCase(
                    test_id=f"{function_name}_{coverage_id}_ai{iteration}_{counters[function_name]:03d}",
                    unit=Path(source_name).stem,
                    function=function_name,
                    coverage=coverage,
                    level=level,
                    values=clean_values,
                    expected=str(raw.get("expected") or "Review and replace with concrete expected output/test oracle"),
                    rationale="AI: " + str(raw.get("rationale") or "Targets uncovered coverage or failed case"),
                    **TestVectorGenerator._audit_fields(
                        fn,
                        coverage,
                        level,
                        counters[function_name],
                        clean_values,
                        "AI-refined case targeting uncovered coverage or failed test evidence",
                    ),
                )
            )
        return result


def refine_with_ollama(
    source: Path,
    functions: list[FunctionInfo],
    cases: list[TestCase],
    feedback: CoverageFeedback,
    coverage: str,
    level: str,
    backend: str,
    model: str,
    timeout_seconds: int,
    target_percent: float,
    max_rounds: int,
    log: Callable[[str], None],
    require_report: bool = False,
    code_database: CodeDatabase | None = None,
) -> list[TestCase]:
    """Refine test cases with Ollama LLM support for multiple stages.
    
    Stages controlled by TESTMATE_LLM_STAGES env var (comma-separated):
    - coverage_gap: Close code coverage gaps (existing behavior)
    - oracle: Generate expected output values for test oracles
    - negative: Generate negative/security test cases
    
    Default stage: coverage_gap. Enable oracle/negative explicitly with TESTMATE_LLM_STAGES if needed.
    """
    # Parse enabled stages from environment variable
    stages_env = os.environ.get("TESTMATE_LLM_STAGES", "coverage_gap").strip()
    enabled_stages = {s.strip().lower() for s in stages_env.split(",") if s.strip()}
    
    if require_report and not has_report_evidence(feedback):
        log("Ollama skipped: selected report does not contain usable coverage/failure evidence.")
        return cases
    if not has_report_evidence(feedback):
        log("Ollama generation enhancement: no coverage report yet, refining small per-function chunks.")
        client = OllamaClient(model=model, timeout=timeout_seconds)
        refiner = OllamaTestRefiner(client)
        merged = list(cases)
        max_functions = OllamaClient._env_int("TESTMATE_LLM_MAX_FUNCTIONS", 8, minimum=1)
        for index, fn in enumerate(functions[:max_functions], start=1):
            try:
                new_cases, notes = refiner.refine(
                    source.name,
                    [fn],
                    merged,
                    feedback,
                    coverage,
                    level,
                    target_percent,
                    index,
                    backend,
                )
            except Exception as exc:
                log(f"Ollama generation enhancement failed for {fn.name}: {exc}")
                continue
            added = merge_cases(merged, new_cases)
            if added:
                log(f"Ollama generation enhancement {fn.name}: added {added}. {notes[:240]}")
        if len(functions) > max_functions:
            log(f"Ollama generation enhancement capped at {max_functions}/{len(functions)} functions. Set TESTMATE_LLM_MAX_FUNCTIONS to increase.")
        return merged
    if feedback_meets_target(feedback, target_percent):
        log(f"Ollama skipped: parsed report already meets target {target_percent:.2f}%.")
        return cases
    client = OllamaClient(model=model, timeout=timeout_seconds)
    refiner = OllamaTestRefiner(client)
    planner = CoverageGapPlanner()
    merged = list(cases)
    
    # Main coverage-gap refinement stage (if enabled)
    if "coverage_gap" in enabled_stages:
        for iteration in range(1, max_rounds + 1):
            max_gaps = OllamaClient._env_int("TESTMATE_LLM_MAX_GAPS", 3, minimum=1)
            gaps = planner.plan(feedback, functions, max_gaps=max_gaps)
            if not gaps:
                log("Ollama skipped: no uncovered coverage points were found in the report.")
                break
            log(f"Ollama round {iteration}: refining {len(gaps)} coverage-feedback chunks.")
            round_added = 0
            by_name = {fn.name: fn for fn in functions}
            for gap_index, gap in enumerate(gaps, start=1):
                fn = by_name.get(gap.function)
                if not fn:
                    continue
                try:
                    new_cases, notes = refiner.refine_gap(
                        source.name,
                        fn,
                        merged,
                        feedback,
                        gap,
                        coverage,
                        level,
                        target_percent,
                        (iteration * 100) + gap_index,
                        backend,
                        code_database,
                    )
                except Exception as exc:
                    log(f"Ollama chunk failed for {gap.function} line {gap.line or '-'}: {exc}")
                    continue
                added = merge_cases(merged, new_cases)
                round_added += added
                log(f"Ollama feedback chunk {gap_index}/{len(gaps)} {gap.metric} {gap.function}:{gap.line or '-'} added {added}. {notes[:240]}")
            if round_added == 0:
                log("Ollama refinement stopped: no new unique cases were added from coverage chunks.")
                break
            log(f"Ollama round {iteration}: added {round_added} total cases.")
            if feedback.coverage_percent is None and not any(value is not None for value in feedback.metrics.values()):
                log("No numeric coverage percentage is available; generated AI candidates but cannot prove target closure until the backend report is refreshed.")
                break
    
    # Oracle generation stage (if enabled): fill expected values
    if "oracle" in enabled_stages:
        log("Ollama oracle generation: generating expected output values for test oracles...")
        oracle_added = 0
        max_functions = OllamaClient._env_int("TESTMATE_LLM_MAX_FUNCTIONS", 8, minimum=1)
        for fn in functions[:max_functions]:
            if fn.return_type.strip() == "void":
                continue
            try:
                oracle_cases, notes = refiner.refine_oracle(source.name, fn, merged, max_rounds + 1, backend)
            except Exception as exc:
                log(f"Ollama oracle generation failed for {fn.name}: {exc}")
                continue
            added = merge_cases(merged, oracle_cases)
            oracle_added += added
            if added:
                log(f"Ollama oracle {fn.name}: added {added} oracle cases. {notes[:240]}")
        if oracle_added > 0:
            log(f"Ollama oracle generation complete: added {oracle_added} total oracle cases (tagged as advisory for review).")
    
    # Negative/security test generation stage (if enabled)
    if "negative" in enabled_stages:
        log("Ollama negative test generation: generating security/robustness test cases...")
        negative_added = 0
        max_functions = OllamaClient._env_int("TESTMATE_LLM_MAX_FUNCTIONS", 8, minimum=1)
        for fn in functions[:max_functions]:
            try:
                negative_cases, notes = refiner.refine_negative(source.name, fn, merged, max_rounds + 2, backend)
            except Exception as exc:
                log(f"Ollama negative test generation failed for {fn.name}: {exc}")
                continue
            added = merge_cases(merged, negative_cases)
            negative_added += added
            if added:
                log(f"Ollama negative {fn.name}: added {added} security/robustness cases. {notes[:240]}")
        if negative_added > 0:
            log(f"Ollama negative test generation complete: added {negative_added} total security cases (tagged as SECURITY: for audit).")
    
    return merged

