import json
import os
import re
import socket
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from testing_mate.models import *
from testing_mate.vector_generator import TestVectorGenerator
from testing_mate.code_database import *
from testing_mate.coverage_gap import CoverageGap, CoverageGapPlanner
from testing_mate.utilities import has_report_evidence, merge_cases, feedback_meets_target
from testing_mate.llm_data_packager import LLMDataPackager
from typing import Any, Callable

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
        code_database: CodeDatabase | None = None,
    ) -> tuple[list[TestCase], str]:
        # Use optimized packager if available
        use_packager = OllamaClient._env_bool("TESTMATE_LLM_USE_PACKAGER", True)
        
        if use_packager:
            return self._refine_packaged(
                source_name, functions, existing_cases, feedback, coverage, level, backend, code_database
            )
        
        prompt = self._prompt(source_name, functions, existing_cases, feedback, coverage, target_percent, backend, code_database)
        payload = self.client.generate_json(prompt)
        notes = self._response_notes(payload)
        raw_cases = self._response_cases(payload)
        if not isinstance(raw_cases, list):
            return [], "Ollama response did not contain a test_cases array."
        cases = self._cases_from_payload(source_name, functions, raw_cases, coverage, level, iteration)
        return cases, notes

    def _refine_packaged(
        self,
        source_name: str,
        functions: list[FunctionInfo],
        existing_cases: list[TestCase],
        feedback: CoverageFeedback,
        coverage: str,
        level: str,
        backend: str,
        code_database: CodeDatabase | None,
    ) -> tuple[list[TestCase], str]:
        """Refine using optimized data packaging."""
        has_feedback = has_report_evidence(feedback)
        
        # Package all functions efficiently
        package = LLMDataPackager.package_batch_functions(
            source_name, functions, existing_cases, coverage, code_database
        )
        
        package["task"] = (
            "coverage_feedback_refinement" if has_feedback else "initial_generation"
        )
        package["backend"] = backend
        package["feedback_summary"] = {
            "source": feedback.source if has_feedback else "none",
            "uncovered_count": len(feedback.uncovered_items) if has_feedback else 0,
            "failure_count": len(feedback.failures) if has_feedback else 0,
        }
        
        prompt = (
            f"You are a senior C unit-test engineer using {backend}. "
            f"Generate at most {3 if has_feedback else 2} test_cases per function. "
            "Use packaged function data: logic_summary, code_chunks, conditions, vectors, paths. "
            "Return strict JSON only. Do not invent project-specific symbols. "
            "Reuse vectors from vectors_for_gap or modify minimally. "
            "Avoid duplicating dedupe_signatures.\n\n"
            + json.dumps(package, separators=(",", ":"))
        )
        
        try:
            payload = self.client.generate_json(prompt)
            notes = payload.get("analysis", "")
            raw_cases = payload.get("test_cases", [])
            
            if not isinstance(raw_cases, list):
                return [], "Invalid packaged response."
            
            cases = self._cases_from_payload(source_name, functions, raw_cases, coverage, level, 1)
            return cases, notes
        except Exception as exc:
            return [], f"Packaged refinement failed: {exc}"


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
        # Use optimized packager if available
        use_packager = OllamaClient._env_bool("TESTMATE_LLM_USE_PACKAGER", True)
        
        if use_packager:
            return self._refine_gap_packaged(
                source_name, fn, existing_cases, feedback, gap, coverage, level, backend, code_database
            )
        
        prompt = self._gap_prompt(source_name, fn, existing_cases, feedback, gap, coverage, target_percent, backend, code_database)
        payload = self.client.generate_json(prompt)
        notes = self._response_notes(payload)
        raw_cases = self._response_cases(payload)
        if not isinstance(raw_cases, list):
            return [], "Ollama response did not contain a test_cases array."
        cases = self._cases_from_payload(source_name, [fn], raw_cases, coverage, level, iteration)
        return cases, notes

    def _refine_gap_packaged(
        self,
        source_name: str,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        feedback: CoverageFeedback,
        gap: CoverageGap,
        coverage: str,
        level: str,
        backend: str,
        code_database: CodeDatabase | None,
    ) -> tuple[list[TestCase], str]:
        """Refine gap using optimized data packaging."""
        package = LLMDataPackager.package_gap_refinement(
            source_name, fn, existing_cases, gap, coverage, feedback, code_database
        )
        
        prompt = (
            f"You are a senior C unit-test engineer using {backend}. "
            "Return strict JSON only. Generate at most 4 test_cases. "
            "Use packaged function/gap context; do not invent missing symbols. "
            "Prefer vectors_for_gap as base, changing values only to close target_gap. "
            "For C1, force missing true/false outcome. For MC/DC, create pairs with atomic condition changes.\n\n"
            + json.dumps(package, separators=(",", ":"))
        )
        
        try:
            payload = self.client.generate_json(prompt)
            notes = payload.get("analysis", "")
            raw_cases = payload.get("test_cases", [])
            
            if not isinstance(raw_cases, list):
                return [], "Invalid packaged response."
            
            cases = self._cases_from_payload(source_name, [fn], raw_cases, coverage, level, 1)
            return cases, notes
        except Exception as exc:
            return [], f"Packaged gap refinement failed: {exc}"

    
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
        code_database: CodeDatabase | None = None,
    ) -> str:
        has_feedback = has_report_evidence(feedback)
        function_names = {fn.name for fn in functions}
        case_limit = 8 if has_feedback else 3
        function_payload = [
            self.llm_context(source_name, fn, existing_cases, coverage, None, code_database)
            for fn in functions
        ]
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
            "llm_contexts": function_payload,
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
            "Use llm_contexts as the source of truth for code chunks, function logic, extracted vectors, and paths. "
            "Only generate cases for listed functions and parameters. "
            "This tool must work for any C project, so do not invent project-specific symbols or enum constants. "
            "Prefer mutating extracted_vectors into new dedupe signatures instead of repeating recent cases. "
            "Prefer small C literals. For MC/DC, vary one atomic condition at a time where possible. "
            f"If expected output cannot be inferred, use the exact string Review/complete expected result in {backend}.\n\n"
            + json.dumps(request, separators=(",", ":"))
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
            "Use llm_context as the source of truth for code chunks, function logic, extracted vectors, and paths. "
            "Focus only on coverage_feedback_chunk; do not request or assume the full report. "
            "Only use the listed function name and its parameters or allowed_pointer_fields. "
            "Do not invent symbols that are absent from llm_context or code_database_chunk. "
            "Prefer small scalar values and explicit pointer-field values. "
            "Start from extracted_vectors/path_candidates when possible, then change values only enough to close target_gap. "
            "For C1, force the missing true or false decision outcome. "
            "For MC/DC, create pairs where one atomic condition changes and the overall decision changes.\n\n"
            + json.dumps(request, separators=(",", ":"))
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
        existing_payload = cls._recent_cases(fn, existing_cases, limit=3)
        feedback_chunk = cls._feedback_chunk(feedback, gap, fn.name)
        llm_context = cls.llm_context(source_name, fn, existing_cases, coverage, gap, code_database)
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
            "llm_context": llm_context,
            "code_database_chunk": cls._compact_code_database_chunk(source_name, fn, code_database),
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

    @classmethod
    def llm_context(
        cls,
        source_name: str,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        coverage: str,
        gap: CoverageGap | None = None,
        code_database: CodeDatabase | None = None,
    ) -> dict[str, Any]:
        vector_limit = OllamaClient._env_int("TESTMATE_LLM_VECTOR_LIMIT", 8, minimum=2)
        record = cls._record_for(code_database, fn.name)
        return {
            "source_file": source_name,
            "analyzer": code_database.analyzer if code_database is not None else "function_info",
            "target_gap": cls._gap_target(gap) if gap is not None else {},
            "function_logic": cls._function_logic(fn, record),
            "code_chunks": cls._code_chunks(fn, gap),
            "extracted_vectors": cls._extracted_vectors(fn, existing_cases, coverage, gap, code_database, vector_limit),
            "execution_paths": cls._execution_path_chunk(record, gap),
            "recent_engine_cases": cls._recent_cases(fn, existing_cases, limit=6),
            "dedupe_signatures": [
                TestVectorGenerator._vector_signature(case.function, case.values or {})
                for case in existing_cases
                if case.function == fn.name
            ][-16:],
        }

    @staticmethod
    def _record_for(code_database: CodeDatabase | None, function_name: str):
        if code_database is None:
            return None
        return next((record for record in code_database.functions if record.name == function_name), None)

    @classmethod
    def _function_logic(cls, fn: FunctionInfo, record) -> dict[str, Any]:
        conditions = [
            {
                "line": condition.line,
                "depth": condition.depth,
                "text": condition.text,
                "decisions": (condition.decisions or [condition.text])[:6],
                "analysis_text": getattr(condition, "analysis_text", "") or "",
                "analysis_line": getattr(condition, "analysis_line", None),
            }
            for condition in sorted(fn.conditions, key=lambda item: (item.line, item.depth))[:12]
        ]
        logic = {
            "name": fn.name,
            "return_type": fn.return_type,
            "line_range": [fn.start_line, fn.end_line],
            "parameters": [asdict(param) for param in fn.parameters],
            "conditions": conditions,
            "condition_count": len(fn.conditions),
            "allowed_pointer_fields": TestVectorGenerator._pointer_field_defaults(fn),
            "summary": {
                "has_branches": bool(fn.conditions),
                "condition_count": len(fn.conditions),
                "decision_count": sum(max(1, len(condition.decisions) or 1) for condition in fn.conditions),
            },
        }
        if record is not None:
            logic["calls"] = list(getattr(record, "calls", []) or [])[:12]
            logic["external_calls"] = list(getattr(record, "external_calls", []) or [])[:12]
            logic["globals_read"] = list(getattr(record, "globals_read", []) or [])[:12]
            logic["globals_written"] = list(getattr(record, "globals_written", []) or [])[:12]
            logic["return_statements"] = list(getattr(record, "return_statements", []) or [])[:8]
            logic["mock_requirements"] = list(getattr(record, "mock_requirements", []) or [])[:8]
            logic["data_dependencies"] = list(getattr(record, "data_dependencies", []) or [])[:12]
            logic["pointer_fields"] = dict(getattr(record, "pointer_fields", {}) or {})
            logic["summary"].update(
                {
                    "return_count": len(getattr(record, "return_statements", []) or []),
                    "execution_path_count": len(getattr(record, "execution_paths", []) or []),
                    "external_call_count": len(getattr(record, "external_calls", []) or []),
                }
            )
        return logic

    @classmethod
    def _code_chunks(cls, fn: FunctionInfo, gap: CoverageGap | None) -> list[dict[str, Any]]:
        lines = fn.body.splitlines()
        if not lines:
            return []
        context = OllamaClient._env_int("TESTMATE_LLM_CODE_CONTEXT_LINES", 8, minimum=3)
        max_chunks = OllamaClient._env_int("TESTMATE_LLM_CODE_CHUNKS", 3, minimum=1)
        ranges: list[tuple[int, int, str]] = []

        def add_range(center_line: int | None, label: str) -> None:
            if center_line is None:
                return
            local_index = max(0, min(len(lines) - 1, center_line - fn.start_line))
            start = max(0, local_index - context)
            end = min(len(lines), local_index + context + 1)
            for old_start, old_end, _ in ranges:
                if start <= old_end and end >= old_start:
                    return
            ranges.append((start, end, label))

        if len(lines) <= context * 2:
            ranges.append((0, len(lines), "function_body"))
        else:
            add_range(gap.line if gap is not None else None, "target_gap")
            for condition in sorted(fn.conditions, key=lambda item: (item.line, item.depth))[:max_chunks]:
                add_range(condition.line, "condition")
            if not ranges:
                ranges.append((0, min(len(lines), context * 2), "function_entry"))

        chunks: list[dict[str, Any]] = []
        for start, end, label in ranges[:max_chunks]:
            numbered = [
                f"{fn.start_line + offset}: {lines[offset]}"
                for offset in range(start, end)
            ]
            chunks.append(
                {
                    "label": label,
                    "line_start": fn.start_line + start,
                    "line_end": fn.start_line + end - 1,
                    "text": "\n".join(numbered)[:1800],
                }
            )
        return chunks

    @classmethod
    def _extracted_vectors(
        cls,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        coverage: str,
        gap: CoverageGap | None,
        code_database: CodeDatabase | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        generator = TestVectorGenerator()
        if code_database is not None:
            generator._code_records = {record.name: record for record in code_database.functions}
        try:
            vectors = generator._vectors_for_gap(fn, gap, coverage) if gap is not None else generator._vectors_for(fn, coverage)
        except Exception:
            vectors = []
        existing = {
            TestVectorGenerator._vector_signature(case.function, case.values or {})
            for case in existing_cases
            if case.function == fn.name
        }
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, values in enumerate(vectors, start=1):
            signature = TestVectorGenerator._vector_signature(fn.name, values)
            if signature in seen:
                continue
            seen.add(signature)
            result.append(
                {
                    "id": f"V{index}",
                    "values": dict(values),
                    "dedupe_signature": signature,
                    "already_exists": signature in existing,
                    "source": "deterministic_gap_vector" if gap is not None else "deterministic_function_vector",
                    "path_id": values.get("__path_id", ""),
                    "target": cls._gap_target(gap) if gap is not None else {},
                }
            )
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _execution_path_chunk(record, gap: CoverageGap | None, limit: int = 6) -> list[dict[str, Any]]:
        if record is None:
            return []
        def safe_int(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return -1

        raw_paths = list(getattr(record, "execution_paths", []) or [])
        if gap is not None and gap.line is not None:
            focused = [
                path for path in raw_paths
                if any(safe_int(item.get("line", -1)) == gap.line for item in path.get("constraints", []) if isinstance(item, dict))
                or safe_int(path.get("line", -1)) == gap.line
            ]
            raw_paths = focused or raw_paths
        paths: list[dict[str, Any]] = []
        for path in raw_paths[:limit]:
            paths.append(
                {
                    "id": path.get("id"),
                    "line": path.get("line"),
                    "conditions": path.get("conditions", [])[:8],
                    "constraints": path.get("constraints", [])[:8],
                    "expected_return": path.get("expected_return"),
                    "expected_calls": path.get("expected_calls", [])[:8],
                    "description": path.get("description", ""),
                }
            )
        return paths

    @classmethod
    def _recent_cases(cls, fn: FunctionInfo, existing_cases: list[TestCase], limit: int) -> list[dict[str, Any]]:
        return [
            {
                **cls._case_chunk(case),
                "test_id": case.test_id,
                "expected": case.expected,
                "dedupe_signature": TestVectorGenerator._vector_signature(case.function, case.values or {}),
            }
            for case in existing_cases
            if case.function == fn.name
        ][-limit:]

    @classmethod
    def _compact_code_database_chunk(cls, source_name: str, fn: FunctionInfo, code_database: CodeDatabase | None) -> dict[str, Any]:
        record = cls._record_for(code_database, fn.name)
        return {
            "source_file": source_name,
            "analyzer": code_database.analyzer if code_database is not None else "function_info",
            "function": fn.name,
            "line_range": [fn.start_line, fn.end_line],
            "calls": list(getattr(record, "calls", []) or [])[:12] if record is not None else [],
            "external_calls": list(getattr(record, "external_calls", []) or [])[:12] if record is not None else [],
            "execution_path_count": len(getattr(record, "execution_paths", []) or []) if record is not None else 0,
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
            "When llm_context.extracted_vectors are present, reuse one as a base and modify only the inputs needed to create a new dedupe_signature.",
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
            "accepted_case_container_keys": [
                "test_cases",
                "cases",
                "vectors",
                "test_vectors",
                "new_test_cases",
                "additional_test_cases",
                "generated_test_cases",
                "coverage_vectors",
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
        case_keys = {
            "test_cases",
            "cases",
            "vectors",
            "test_vectors",
            "new_test_cases",
            "additional_test_cases",
            "generated_cases",
            "generated_test_cases",
            "candidate_cases",
            "coverage_vectors",
        }

        def looks_like_case_list(value: list, key: str = "", container: dict | None = None) -> bool:
            function_hint = ""
            if container is not None:
                function_hint = str(container.get("function") or container.get("function_name") or container.get("target_function") or "")
            return all(isinstance(item, dict) for item in value) and any(
                "function" in item or "values" in item or (function_hint and "vector" in key)
                for item in value
            )

        def normalized_case_list(value: list, key: str, container: dict) -> list:
            function_hint = str(container.get("function") or container.get("function_name") or container.get("target_function") or "").strip()
            normalized: list[dict] = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                if "values" not in item and function_hint and "vector" in key:
                    case = {"function": function_hint, "values": dict(item)}
                else:
                    case = dict(item)
                    if "function" not in case and function_hint:
                        case["function"] = function_hint
                normalized.append(case)
            return normalized

        def find_cases(value, depth: int = 0) -> list:
            if depth > 4:
                return []
            if isinstance(value, list):
                return value if looks_like_case_list(value) else []
            if not isinstance(value, dict):
                return []
            for key in case_keys:
                candidate = value.get(key)
                if isinstance(candidate, list) and looks_like_case_list(candidate, key, value):
                    return normalized_case_list(candidate, key, value)
            for key in ("coverage_enhancement", "enhancement", "result", "response", "output", "data"):
                nested = value.get(key)
                cases = find_cases(nested, depth + 1)
                if cases:
                    return cases
            return []

        cases = find_cases(payload)
        if cases:
            return cases
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
                    code_database,
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


class OllamaParallelRefiner:
    """Parallel LLM refinement with efficient data packaging.
    
    Uses LLMDataPackager to create optimized JSON structures and processes
    multiple coverage gaps concurrently using ThreadPoolExecutor.
    """

    def __init__(self, client: OllamaClient, max_workers: int = 3):
        """Initialize parallel refiner.
        
        Args:
            client: OllamaClient instance
            max_workers: Maximum concurrent LLM requests (default 3, limited by Ollama)
        """
        self.client = client
        self.max_workers = max(1, min(max_workers, 4))  # Cap at 4 to avoid overwhelming Ollama

    def refine_gaps_parallel(
        self,
        source_name: str,
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        gaps: list[CoverageGap],
        coverage: str,
        level: str,
        backend: str = "GoogleTest",
        code_database: CodeDatabase | None = None,
        max_cases_per_gap: int = 3,
    ) -> tuple[list[TestCase], list[str]]:
        """Refine multiple coverage gaps in parallel.
        
        Args:
            source_name: Source file name
            fn: Function being tested
            existing_cases: Existing test cases
            gaps: Coverage gaps to refinePriority-sorted by severity
            coverage: Coverage metric (C0, C1, MC/DC)
            level: ISO compliance level
            backend: Test backend (GoogleTest, CppUTest)
            code_database: Code analysis database
            max_cases_per_gap: Max test cases to generate per gap
            
        Returns:
            (all_cases, notes_list)
        """
        if not gaps:
            return [], []
        
        all_cases: list[TestCase] = []
        notes: list[str] = []
        
        # Package each gap for efficient LLM processing
        packaged_gaps = [
            (
                gap,
                LLMDataPackager.package_gap_refinement(
                    source_name, fn, existing_cases, gap, coverage, None, code_database
                ),
            )
            for gap in gaps[:8]  # Limit to 8 gaps per batch
        ]
        
        # Process gaps concurrently
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(
                    self._refine_single_gap,
                    gap,
                    pkg,
                    fn,
                    existing_cases,
                    level,
                    backend,
                    max_cases_per_gap,
                ): gap.line
                for gap, pkg in packaged_gaps
            }
            
            for future in as_completed(futures):
                try:
                    cases, note = future.result()
                    all_cases.extend(cases)
                    if note:
                        notes.append(note)
                except Exception as exc:
                    gap_line = future_map.get(future, "unknown")
                    notes.append(f"Parallel gap refinement failed (line {gap_line}): {exc}")
        
        return all_cases, notes

    def _refine_single_gap(
        self,
        gap: CoverageGap,
        package: dict[str, Any],
        fn: FunctionInfo,
        existing_cases: list[TestCase],
        level: str,
        backend: str,
        max_cases: int,
    ) -> tuple[list[TestCase], str]:
        """Refine a single gap using packaged data.
        
        Args:
            gap: Coverage gap to refine
            package: Pre-packaged gap refinement data
            fn: Function being tested
            existing_cases: Existing test cases
            level: ISO compliance level
            backend: Test backend
            max_cases: Max cases to generate
            
        Returns:
            (cases, note)
        """
        prompt = self._prompt_from_package(package, backend, max_cases)
        
        try:
            payload = self.client.generate_json(prompt)
        except Exception as exc:
            return [], f"LLM request failed for {gap.metric} at line {gap.line}: {exc}"
        
        raw_cases = payload.get("test_cases", [])
        if not isinstance(raw_cases, list):
            return [], f"Invalid LLM response for gap at line {gap.line}"
        
        cases = self._cases_from_payload(fn.name, raw_cases, existing_cases, level)
        note = payload.get("analysis", "")
        
        return cases, note

    def _prompt_from_package(self, package: dict[str, Any], backend: str, max_cases: int) -> str:
        """Create LLM prompt from packaged data.
        
        Args:
            package: Pre-packaged data from LLMDataPackager
            backend: Test backend
            max_cases: Max test cases to generate
            
        Returns:
            LLM prompt string
        """
        return (
            f"You are a senior C unit-test engineer using {backend}. "
            f"Generate at most {max_cases} test_cases targeting the coverage gap. "
            "Use packaged data structures (code_context, nearby_conditions, vectors_for_gap). "
            "Return strict JSON only. Do not invent symbols absent from the package. "
            "Prefer values from vectors_for_gap or modify them minimally to close the gap. "
            "For C1/MC/DC, vary one atomic condition at a time. "
            "Use Review/complete expected result when oracle cannot be inferred.\n\n"
            + json.dumps(package, separators=(",", ":"))
        )

    @staticmethod
    def _cases_from_payload(
        fn_name: str,
        raw_cases: list[dict],
        existing_cases: list[TestCase],
        level: str,
    ) -> list[TestCase]:
        """Convert LLM response to TestCase objects.
        
        Args:
            fn_name: Function name
            raw_cases: Raw cases from LLM response
            existing_cases: Existing cases for deduplication
            level: ISO compliance level
            
        Returns:
            List of TestCase objects
        """
        cases: list[TestCase] = []
        existing_sigs = {
            TestVectorGenerator._vector_signature(c.function, c.values or {})
            for c in existing_cases
        }
        
        for index, raw in enumerate(raw_cases, 1):
            if not isinstance(raw, dict):
                continue
            
            values = raw.get("values", {})
            sig = TestVectorGenerator._vector_signature(fn_name, values)
            
            if sig in existing_sigs:
                continue
            
            case = TestCase(
                test_id=f"Parallel_{fn_name}_{index:03d}",
                function=fn_name,
                values=dict(values) if values else {},
                expected=raw.get("expected", "Review"),
                rationale=raw.get("rationale", "Parallel LLM refinement"),
                coverage="C0",
                level=level,
                description="Generated via parallel LLM refinement",
                trace_tags="parallel-llm",
            )
            cases.append(case)
            existing_sigs.add(sig)
        
        return cases


