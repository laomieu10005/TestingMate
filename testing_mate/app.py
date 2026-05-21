from __future__ import annotations

import csv
import html
import json
import os
import re
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from typing import Callable

from testing_mate.constants import (
    APP_NAME,
    DEFAULT_COMPILER,
    DEFAULT_ENHANCEMENT_STAGES,
    C_SOURCE_EXTENSIONS,
    HEADER_SOURCE_EXTENSIONS,
    SOURCE_EXTENSIONS,
)
from testing_mate.models import CoverageFeedback, CoverageGap, Condition, FunctionInfo, Parameter, RunResult, TestCase
from testing_mate.c_backend import *
from testing_mate.generator import TestBackendGenerator, TestBackendRunner, backend_harness_path, format_run_result
from testing_mate.boolean_expression import BooleanExpressionEvaluator
from testing_mate.clang_analyzer import ClangEnhancedAnalyzer
from testing_mate.code_database import CodeDatabase
from testing_mate.reporter import *
from testing_mate.vector_generator import TestVectorGenerator


def safe_output_folder_name(name: str, fallback: str = "project") -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" ._")
    return clean or fallback


def source_scoped_output_dir(base_out_dir: Path, sources: list[Path]) -> Path:
    if not sources:
        return base_out_dir
    if len(sources) == 1:
        folder_name = safe_output_folder_name(sources[0].stem, "source")
        return base_out_dir if base_out_dir.name == folder_name else base_out_dir / folder_name

    parents = [source.parent for source in sources]
    try:
        common_parent = Path(os.path.commonpath([str(parent) for parent in parents]))
    except ValueError:
        common_parent = parents[0]
    folder_name = safe_output_folder_name(common_parent.name or parents[0].name, "project")
    return base_out_dir if base_out_dir.name == folder_name else base_out_dir / folder_name


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
                outcomes: set[bool] = set()
                decisions = condition.decisions or [condition.text]
                for _case, decision_values, outcome in evaluator._evaluations(condition, decisions, fn_cases):
                    if decision_values is not None and outcome is not None:
                        outcomes.add(outcome)
                total += 2
                covered += len(outcomes)
                if True in outcomes and False in outcomes:
                    notes.append(f"C1 covered {fn.name} line {condition.line}: true and false outcomes observed.")
                else:
                    missing = "true" if True not in outcomes else "false"
                    uncovered.append(f"Uncovered C1: {fn.name} line {condition.line} missing {missing} outcome for `{condition.text}`.")
                    if not outcomes:
                        uncovered.append(f"Uncovered C1: {fn.name} line {condition.line} condition could not be evaluated from generated vectors.")
        percent = (covered / total) * 100.0 if total else 100.0
        note = (
            f"Calculated by Testing Mate from generated vectors: {covered}/{total} decision outcomes covered."
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
                evaluations = self._evaluations(condition, decisions, fn_cases)
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

    def _evaluations(self, condition: Condition, decisions: list[str], cases: list[TestCase]) -> list[tuple[TestCase, list[bool] | None, bool | None]]:
        result: list[tuple[TestCase, list[bool] | None, bool | None]] = []
        for case in cases:
            decision_values = [self._eval_decision(decision, case.values) for decision in decisions]
            if any(value is None for value in decision_values):
                result.append((case, None, None))
                continue
            bool_values = [bool(value) for value in decision_values]
            result.append((case, bool_values, self._combine(condition.text, decisions, bool_values)))
        return result

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
        clean = re.sub(r"\([A-Za-z_]\w+\)\s*", "", clean)
        negated = False
        while clean.startswith("!"):
            negated = not negated
            clean = clean[1:].strip().strip("()")
        match = self.compare_pattern.match(clean)
        reverse = False
        if not match:
            match = self.reverse_compare_pattern.match(clean)
            reverse = bool(match)
        if match:
            left = match.group("right") if reverse else match.group("left")
            right = match.group("left") if reverse else match.group("right")
            op = self._reverse_operator(match.group("op")) if reverse else match.group("op")
            actual = self._lookup_value(left, values)
            if actual is None:
                return None
            result = self._compare(actual, right, op, values)
            return (not result) if negated and result is not None else result
        actual = self._lookup_value(clean, values)
        if actual is None:
            return None
        result = self._truthy(actual)
        return (not result) if negated else result

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


class CoverageGapPlanner:
    def plan(self, feedback: CoverageFeedback, functions: list[FunctionInfo], max_gaps: int = 12) -> list[CoverageGap]:
        gaps: list[CoverageGap] = []
        function_by_name = {fn.name: fn for fn in functions}
        for item in feedback.uncovered_items:
            gap = self._gap_from_text(item, functions, function_by_name)
            if gap:
                gaps.append(gap)
        if not gaps:
            gaps.extend(self._metric_fallback_gaps(feedback, functions))
        unique: list[CoverageGap] = []
        seen: set[tuple[str, int | None, str, str]] = set()
        for gap in gaps:
            key = (gap.function, gap.line, gap.metric, gap.condition or gap.text)
            if key in seen:
                continue
            seen.add(key)
            unique.append(gap)
            if len(unique) >= max_gaps:
                break
        return unique

    def _gap_from_text(self, text: str, functions: list[FunctionInfo], function_by_name: dict[str, FunctionInfo]) -> CoverageGap | None:
        lowered = text.lower()
        metric = "MC/DC" if "mc/dc" in lowered or "mcdc" in lowered else "C1" if "branch" in lowered or "decision" in lowered or "c1" in lowered else "C0"
        line = self._line_from_text(text)
        fn = self._function_from_text(text, functions, function_by_name, line)
        if not fn:
            return None
        condition = self._condition_for_gap(fn, text, line)
        return CoverageGap(function=fn.name, line=line, metric=metric, text=text[:500], condition=condition)

    @staticmethod
    def _line_from_text(text: str) -> int | None:
        match = re.search(r"\bline\s+(\d+)\b", text, flags=re.I)
        if match:
            return int(match.group(1))
        match = re.search(r"\b(?:DA|BRDA):(\d+)", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _function_from_text(text: str, functions: list[FunctionInfo], function_by_name: dict[str, FunctionInfo], line: int | None) -> FunctionInfo | None:
        for name, fn in function_by_name.items():
            if re.search(rf"\b{re.escape(name)}\b", text):
                return fn
        if line is not None:
            for fn in functions:
                if fn.start_line <= line <= fn.end_line:
                    return fn
        return None

    @staticmethod
    def _condition_for_gap(fn: FunctionInfo, text: str, line: int | None) -> str:
        if line is not None:
            same_line = [condition for condition in fn.conditions if condition.line == line]
            if same_line:
                return same_line[0].text
        for condition in fn.conditions:
            if condition.text and condition.text in text:
                return condition.text
            for decision in condition.decisions:
                if decision and decision in text:
                    return condition.text
        return ""

    @staticmethod
    def _metric_fallback_gaps(feedback: CoverageFeedback, functions: list[FunctionInfo]) -> list[CoverageGap]:
        gaps: list[CoverageGap] = []
        weak_metrics = [metric for metric, value in feedback.metrics.items() if value is None or value < 100.0]
        if not weak_metrics:
            weak_metrics = ["C1"]
        for metric in weak_metrics:
            for fn in functions:
                if fn.conditions:
                    condition = fn.conditions[0]
                    gaps.append(
                        CoverageGap(
                            function=fn.name,
                            line=condition.line,
                            metric=metric,
                            text=f"{metric} target still below requested coverage for {fn.name}.",
                            condition=condition.text,
                        )
                    )
        return gaps


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
    def _env_int(name: str, default: int, minimum: int | None = None) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
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
            "Each values key must be a listed parameter or allowed_pointer_fields key.",
            "Use small C literals, enum constants present in the input, or pointer-field assignments.",
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
                    "values": {"listed_parameter_or_pointer_field": "C literal"},
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
            clean_values = {str(name): str(value) for name, value in values.items() if str(name) in allowed}
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


class ArtifactWriter:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        level: str,
        backend: str = "CppUTest",
        feedback: CoverageFeedback | None = None,
        run_result: RunResult | None = None,
        include_backend: bool = True,
        unit_sources: list[Path] | None = None,
        include_dirs: list[str] | None = None,
        target_coverage_percent: float = 100.0,
        enhancement_stages: list[str] | None = None,
        code_database: CodeDatabase | None = None,
    ) -> list[Path]:
        active_feedback = feedback or CoverageFeedback(source="none", raw_text="")
        if any(case.coverage in {"C0/C1", "C1", "MC/DC"} for case in cases) and active_feedback.metrics.get("C1") is None:
            active_feedback = C1CoverageEngine.merge(active_feedback, C1CoverageEngine().analyze(functions, cases))
        if any(case.coverage == "MC/DC" for case in cases) and active_feedback.metrics.get("MC/DC") is None:
            active_feedback = MCDCCoverageEngine.merge(active_feedback, MCDCCoverageEngine().analyze(functions, cases))
        paths = [
            self._write_code_database(source, code_database),
            self._write_json(source, functions, cases, code_database),
            self._write_enhancement_request(
                source,
                functions,
                cases,
                level,
                backend,
                active_feedback,
                target_coverage_percent,
                enhancement_stages or DEFAULT_ENHANCEMENT_STAGES,
                code_database,
            ),
            self._write_summary(source, functions, cases, active_feedback, run_result),
        ]
        paths.append(self._write_csv(source, cases, backend.lower()))
        if include_backend:
            paths.extend(TestBackendGenerator(self.out_dir, include_dirs).write_backend(backend, source, functions, cases, level, unit_sources, code_database))
        paths.append(self._write_html_report(source, functions, cases, backend, active_feedback, run_result))
        paths.append(self._write_audit_checklist(source, functions, cases, backend))
        return paths

    def _write_code_database(self, source: Path, code_database: CodeDatabase | None) -> Path:
        path = self.out_dir / f"{source.stem}_code_database.json"
        payload = code_database.to_dict() if code_database is not None else {
            "version": "1.0",
            "source_file": source.name,
            "source_path": str(source),
            "analysis_source_path": str(source),
            "analyzer": "unavailable",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "functions": [],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _write_csv(self, source: Path, cases: list[TestCase], backend_id: str) -> Path:
        safe_backend = re.sub(r"[^A-Za-z0-9]+", "_", backend_id).strip("_").lower()
        path = self.out_dir / f"{source.stem}_{safe_backend}_cases.csv"
        fieldnames = [
            "test_id",
            "unit",
            "function",
            "level",
            "coverage",
            "artifact_status",
            "oracle_status",
            "execution_status",
            "parameter",
            "value",
            "description",
            "preconditions",
            "setup",
            "test_steps",
            "expected",
            "teardown",
            "pass_fail_criteria",
            "requirement_ref",
            "safety_goal_ref",
            "asil",
            "aspice_level",
            "trace_tags",
            "rationale",
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for case in cases:
                common = {
                    "test_id": case.test_id,
                    "unit": case.unit,
                    "function": case.function,
                    "level": case.level,
                    "coverage": case.coverage,
                    "artifact_status": case.artifact_status,
                    "oracle_status": case.oracle_status,
                    "execution_status": case.execution_status,
                    "description": case.description,
                    "preconditions": case.preconditions,
                    "setup": case.setup,
                    "test_steps": case.test_steps,
                    "expected": case.expected,
                    "teardown": case.teardown,
                    "pass_fail_criteria": case.pass_fail_criteria,
                    "requirement_ref": case.requirement_ref,
                    "safety_goal_ref": case.safety_goal_ref,
                    "asil": case.asil,
                    "aspice_level": case.aspice_level,
                    "trace_tags": case.trace_tags,
                    "rationale": case.rationale,
                }
                if case.values:
                    for name, value in case.values.items():
                        writer.writerow({**common, "parameter": name, "value": value})
                else:
                    writer.writerow({**common, "parameter": "", "value": ""})
        return path

    def _write_json(self, source: Path, functions: list[FunctionInfo], cases: list[TestCase], code_database: CodeDatabase | None = None) -> Path:
        path = self.out_dir / f"{source.stem}_analysis.json"
        records = {record.name: asdict(record) for record in code_database.functions} if code_database else {}
        payload = {
            "source": str(source),
            "functions": [
                {
                    "name": fn.name,
                    "return_type": fn.return_type,
                    "parameters": [asdict(param) for param in fn.parameters],
                    "start_line": fn.start_line,
                    "end_line": fn.end_line,
                    "conditions": [asdict(condition) for condition in fn.conditions],
                    "analysis": records.get(fn.name, {}),
                }
                for fn in functions
            ],
            "test_cases": [asdict(case) for case in cases],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _write_enhancement_request(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        level: str,
        backend: str,
        feedback: CoverageFeedback,
        target_coverage_percent: float,
        enhancement_stages: list[str],
        code_database: CodeDatabase | None = None,
    ) -> Path:
        path = self.out_dir / f"{source.stem}_enhancement_request.json"
        records = {record.name: record for record in code_database.functions} if code_database else {}
        payload = {
            "version": "1.0",
            "source_file": source.name,
            "source_path": str(source),
            "analysis_timestamp": datetime.now().isoformat(timespec="seconds"),
            "coverage_target": self._primary_coverage(cases),
            "asil_level": self._primary_asil(cases),
            "test_level": level,
            "target_coverage_percent": target_coverage_percent,
            "backend": backend,
            "enhancement_stages": enhancement_stages,
            "code_database_file": f"{source.stem}_code_database.json",
            "code_database_summary": (
                {
                    "analyzer": code_database.analyzer,
                    "function_count": len(code_database.functions),
                    "functions": [function.name for function in code_database.functions[:20]],
                    "execution_path_count": sum(len(function.execution_paths) for function in code_database.functions),
                    "mock_requirement_count": sum(len(function.mock_requirements) for function in code_database.functions),
                }
                if code_database is not None
                else {"analyzer": "unavailable", "function_count": len(functions), "functions": [fn.name for fn in functions[:20]]}
            ),
            "ollama_handoff": self._ollama_handoff(
                source,
                functions,
                cases,
                backend,
                feedback,
                target_coverage_percent,
                code_database,
            ),
            "functions": [
                {
                    "name": fn.name,
                    "return_type": fn.return_type,
                    "parameters": [asdict(param) for param in fn.parameters],
                    "conditions": [asdict(condition) for condition in fn.conditions],
                    "body_excerpt": self._body_excerpt(fn.body),
                    "complexity": self._cyclomatic_complexity(fn),
                    "allowed_pointer_fields": TestVectorGenerator._pointer_field_defaults(fn),
                    "calls": records[fn.name].calls if fn.name in records else [],
                    "external_calls": records[fn.name].external_calls if fn.name in records else [],
                    "local_variables": records[fn.name].local_variables if fn.name in records else [],
                    "globals_read": records[fn.name].globals_read if fn.name in records else [],
                    "globals_written": records[fn.name].globals_written if fn.name in records else [],
                    "execution_paths": records[fn.name].execution_paths if fn.name in records else [],
                    "data_dependencies": records[fn.name].data_dependencies if fn.name in records else [],
                    "mock_requirements": records[fn.name].mock_requirements if fn.name in records else [],
                }
                for fn in functions
            ],
            "existing_cases": [
                {
                    "test_id": case.test_id,
                    "function": case.function,
                    "coverage": case.coverage,
                    "values": case.values,
                    "expected": case.expected,
                    "source": "local_engine",
                    "rationale": case.rationale,
                }
                for case in cases
            ],
            "coverage_metrics": {
                "C0": feedback.metrics.get("C0"),
                "C1": feedback.metrics.get("C1"),
                "MC/DC": feedback.metrics.get("MC/DC"),
                "coverage_percent": feedback.coverage_percent,
                "uncovered_items": feedback.uncovered_items,
                "failures": feedback.failures,
                "metric_notes": feedback.metric_notes,
                "source": feedback.source,
            },
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _ollama_handoff(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        backend: str,
        feedback: CoverageFeedback,
        target_coverage_percent: float,
        code_database: CodeDatabase | None,
    ) -> dict:
        coverage = self._primary_coverage(cases)
        by_name = {fn.name: fn for fn in functions}
        chunks: list[dict] = []
        for gap in CoverageGapPlanner().plan(feedback, functions, max_gaps=12):
            fn = by_name.get(gap.function)
            if not fn:
                continue
            chunks.append(
                OllamaTestRefiner.gap_request(
                    source.name,
                    fn,
                    cases,
                    feedback,
                    gap,
                    coverage,
                    target_coverage_percent,
                    backend,
                    code_database,
                )
            )

        mode = "coverage_gap_chunks"
        if not chunks:
            mode = "initial_function_chunks"
            for fn in functions[:12]:
                recent_cases = [case for case in cases if case.function == fn.name][-6:]
                chunks.append(
                    {
                        "task": f"Enhance initial {backend} C unit-test generation for this function chunk only.",
                        "request_type": "initial_function_chunk",
                        "chunk_id": re.sub(r"[^A-Za-z0-9]+", "_", f"{source.stem}_{fn.name}_initial").strip("_").lower(),
                        "source": source.name,
                        "backend": backend,
                        "coverage_target": coverage,
                        "target_percent": target_coverage_percent,
                        "coverage_feedback_chunk": OllamaTestRefiner._compact_feedback(feedback, bool(feedback.uncovered_items or feedback.failures)),
                        "function": {
                            "name": fn.name,
                            "return_type": fn.return_type,
                            "parameters": [asdict(param) for param in fn.parameters],
                            "conditions": [asdict(condition) for condition in fn.conditions[:8]],
                            "allowed_pointer_fields": TestVectorGenerator._pointer_field_defaults(fn),
                        },
                        "code_database_chunk": (
                            code_database.function_chunk(fn.name, cases)
                            if code_database is not None
                            else {
                                "source_file": source.name,
                                "analyzer": "function_info",
                                "functions": [OllamaTestRefiner._function_chunk(fn, 8)],
                                "recent_engine_cases": [OllamaTestRefiner._case_chunk(case) for case in recent_cases],
                            }
                        ),
                        "recent_existing_cases_for_function": [OllamaTestRefiner._case_chunk(case) for case in recent_cases],
                        "dedupe_against": [OllamaTestRefiner._case_signature(case) for case in recent_cases],
                        "acceptance_rules": OllamaTestRefiner._acceptance_rules(backend),
                        "response_contract": OllamaTestRefiner._response_contract(),
                    }
                )

        return {
            "version": "1.1",
            "mode": mode,
            "model_family": "ollama",
            "chunk_count": len(chunks),
            "max_cases_per_chunk": 4,
            "coverage_first": True,
            "response_contract": OllamaTestRefiner._response_contract(),
            "chunks": chunks,
        }

    @staticmethod
    def _primary_coverage(cases: list[TestCase]) -> str:
        for case in cases:
            if case.coverage in {"C0", "C0/C1", "C1", "MC/DC"}:
                return case.coverage
        return cases[0].coverage if cases else "MC/DC"

    @staticmethod
    def _primary_asil(cases: list[TestCase]) -> str:
        return cases[0].asil.replace("ASIL-", "ASIL ") if cases else "ASIL D"

    @staticmethod
    def _body_excerpt(body: str, limit: int = 1200) -> str:
        compact = " ".join(body.split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    @staticmethod
    def _cyclomatic_complexity(fn: FunctionInfo) -> int:
        decisions = sum(max(1, len(condition.decisions) or 1) for condition in fn.conditions)
        return 1 + decisions

    def _write_html_report(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        backend: str,
        feedback: CoverageFeedback | None,
        run_result: RunResult | None,
    ) -> Path:
        path = self.out_dir / f"{source.stem}.html"
        condition_count = sum(len(fn.conditions) for fn in functions)
        coverage = "Not provided"
        if feedback and feedback.coverage_percent is not None:
            coverage = f"{feedback.coverage_percent:.2f}%"
        coverage_metric_html = self._coverage_metric_cards(feedback, functions)
        coverage_obligation_rows = self._coverage_obligation_rows(functions)
        run_status = "Not run"
        if run_result:
            if run_result.returncode is None:
                run_status = f"Skipped: {run_result.skipped_reason}"
            else:
                run_status = "PASS" if run_result.returncode == 0 else f"FAIL ({run_result.returncode})"
        executed_count = self._executed_test_count(run_result)
        report_case_count = executed_count if executed_count is not None else len(cases)
        report_case_label = "Test Cases Run" if executed_count is not None else "Test Cases"
        function_sections = []
        case_counts: dict[str, int] = {}
        for case in cases:
            case_counts[case.function] = case_counts.get(case.function, 0) + 1
        for fn in functions:
            cond_rows = "".join(
                f"<li>line {condition.line}: <code>{html.escape(condition.text)}</code></li>"
                for condition in fn.conditions
            )
            params = ", ".join(f"{param.ctype} {param.name}" for param in fn.parameters) or "void"
            function_sections.append(
                f"<section><h3>{html.escape(fn.name)}({html.escape(params)})</h3>"
                f"<p>Test group: {html.escape(fn.name)}. Lines {fn.start_line}-{fn.end_line}. Conditions: {len(fn.conditions)}. Cases: {case_counts.get(fn.name, 0)}</p>"
                f"<ul>{cond_rows}</ul></section>"
            )
        uncovered = "".join(f"<li>{html.escape(item)}</li>" for item in (feedback.uncovered_items if feedback else []))
        failures = "".join(f"<li>{html.escape(item)}</li>" for item in (feedback.failures if feedback else []))
        stdout = html.escape(run_result.stdout if run_result else "")
        stderr = html.escape(run_result.stderr if run_result else "")
        backend_outputs = self._backend_output_items(source, backend)
        backend_output_html = "".join(f"<li><code>{html.escape(item)}</code></li>" for item in backend_outputs)
        lcov_trace_html = self._lcov_trace_section(source, backend, feedback, functions)
        content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Testing Mate Report - {html.escape(source.name)}</title>
  <style>
    :root {{ --bg: #f5f7fb; --panel: #ffffff; --line: #d8e0ec; --text: #182033; --muted: #5f6f86; --head: #edf2f8; --accent: #2563eb; --good: #0f766e; --warn: #b45309; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 0; background: var(--bg); color: var(--text); }}
    header {{ padding: 22px clamp(16px, 3vw, 34px); background: var(--panel); border-bottom: 1px solid var(--line); }}
    header h1 {{ margin: 0 0 6px; font-size: clamp(22px, 3vw, 32px); }}
    header p {{ margin: 0; color: var(--muted); }}
    main {{ width: min(1480px, 100%); margin: 0 auto; padding: 20px clamp(12px, 2vw, 28px) 32px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .card, section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04); }}
    section {{ margin-top: 14px; overflow: hidden; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; }}
    h3 {{ margin: 12px 0 8px; font-size: 15px; }}
    .metric {{ font-size: 24px; font-weight: 700; margin-top: 6px; line-height: 1.1; color: var(--accent); }}
    .note {{ color: var(--muted); font-size: 12px; line-height: 1.35; margin-top: 6px; overflow-wrap: anywhere; }}
    .table-wrap {{ width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); font-size: 12px; }}
    th, td {{ border-bottom: 1px solid var(--line); border-right: 1px solid var(--line); padding: 7px 8px; text-align: left; vertical-align: top; overflow-wrap: break-word; word-wrap: break-word; word-break: break-word; white-space: normal; max-width: 300px; }}
    th:last-child, td:last-child {{ border-right: 0; }}
    tr:last-child td {{ border-bottom: 0; }}
    th {{ position: sticky; top: 0; z-index: 1; background: var(--head); color: #243044; font-weight: 650; }}
    .obligation-table {{ min-width: 760px; }}
    code, pre {{ font-family: Consolas, monospace; }}
    code {{ background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 4px; padding: 1px 4px; word-break: break-word; overflow-wrap: break-word; white-space: normal; display: inline-block; max-width: 100%; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; overflow-wrap: break-word; background: #111827; color: #e5e7eb; padding: 12px; border-radius: 6px; max-height: 280px; overflow: auto; }}
    .trace-actions {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 8px 0 12px; }}
    .trace-link {{ display: inline-flex; align-items: center; min-height: 34px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 6px; text-decoration: none; color: var(--accent); background: #f8fbff; font-weight: 650; }}
    .lcov-frame {{ width: 100%; height: min(760px, 72vh); border: 1px solid var(--line); border-radius: 6px; background: #fff; }}
    ul {{ margin: 8px 0 0; padding-left: 20px; }}
    li {{ margin: 4px 0; overflow-wrap: anywhere; }}
    @media (max-width: 720px) {{ .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .metric {{ font-size: 20px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Testing Mate Report</h1>
    <p>{html.escape(source.name)} | {html.escape(backend)} | generated {html.escape(datetime.now().isoformat(timespec="seconds"))}</p>
  </header>
  <main>
    <div class="grid">
      <div class="card">Functions<div class="metric">{len(functions)}</div></div>
      <div class="card">Conditions<div class="metric">{condition_count}</div></div>
      <div class="card">{html.escape(report_case_label)}<div class="metric">{report_case_count}</div></div>
      <div class="card">Report Coverage<div class="metric">{html.escape(coverage)}</div></div>
      <div class="card">Run Status<div class="metric">{html.escape(run_status)}</div></div>
      {coverage_metric_html}
    </div>
    <section><h2>Backend Output</h2><ul>{backend_output_html}</ul></section>
    {lcov_trace_html}
    <section><h2>Coverage Obligations</h2><div class="table-wrap"><table class="obligation-table"><thead><tr><th>Metric</th><th>Function</th><th>Line</th><th>Obligation</th><th>Evidence Source</th></tr></thead><tbody>{coverage_obligation_rows}</tbody></table></div></section>
    <section><h2>Functions And Conditions</h2>{''.join(function_sections)}</section>
    <section><h2>Coverage Feedback</h2><h3>Uncovered</h3><ul>{uncovered}</ul><h3>Failures</h3><ul>{failures}</ul></section>
    <section><h2>Run Output</h2><h3>stdout</h3><pre>{stdout}</pre><h3>stderr</h3><pre>{stderr}</pre></section>
  </main>
</body>
</html>"""
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _executed_test_count(run_result: RunResult | None) -> int | None:
        if not run_result or run_result.returncode is None:
            return None
        text = "\n".join(part for part in (run_result.stdout, run_result.stderr) if part)
        patterns = [
            r"(?im)^\s*OK\s*\(\s*(\d+)\s+tests?",
            r"(?im)^\s*\[\s*PASSED\s*\]\s*(\d+)\s+tests?",
            r"(?im)^\s*\[\s*=+\s*\]\s*(\d+)\s+tests?\s+from\b",
            r"(?im)\b(\d+)\s+tests?\s+from\s+\d+\s+test\s+suites?\s+ran\b",
            r"(?im)\b(\d+)\s+tests?\s+ran\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _coverage_metric_cards(feedback: CoverageFeedback | None, functions: list[FunctionInfo]) -> str:
        metrics = feedback.metrics if feedback else {}
        notes = feedback.metric_notes if feedback else {}
        cards: list[str] = []
        obligation_counts = {
            "C0": sum(max(0, fn.end_line - fn.start_line + 1) for fn in functions),
            "C1": sum(max(1, len(condition.decisions) or 1) for fn in functions for condition in fn.conditions),
            "MC/DC": sum(max(1, len(condition.decisions) or 1) for fn in functions for condition in fn.conditions),
        }
        for metric in ("C0", "C1", "MC/DC"):
            value = metrics.get(metric)
            display = f"{value:.2f}%" if value is not None else "Missing"
            note = notes.get(metric) or f"Static obligations: {obligation_counts[metric]}"
            cards.append(
                f'<div class="card">{html.escape(metric)}<div class="metric">{html.escape(display)}</div>'
                f'<div class="note">{html.escape(note)}</div></div>'
            )
        return "".join(cards)

    def _lcov_trace_section(
        self,
        source: Path,
        backend: str,
        feedback: CoverageFeedback | None,
        functions: list[FunctionInfo],
    ) -> str:
        if backend not in {"CppUTest", "GoogleTest"}:
            return (
                "<section><h2>LCOV Source Trace</h2>"
                "<p class=\"note\">LCOV source trace is available for CppUTest and GoogleTest runs.</p>"
                "</section>"
            )
        base_name = f"{source.stem}_cpputest" if backend == "CppUTest" else f"{source.stem}_gtest"
        lcov_dir = self.out_dir / base_name / f"{source.stem}_lcov"
        index_path = lcov_dir / "index.html"
        source_page = self._find_lcov_source_page(lcov_dir, source)
        index_href = self._relative_href(index_path) if index_path.exists() else ""
        source_href = self._relative_href(source_page) if source_page and source_page.exists() else ""
        coverage_info = self.out_dir / base_name / "coverage.info"
        if not index_href:
            expected = f"{base_name}/{source.stem}_lcov/index.html"
            return (
                "<section><h2>LCOV Source Trace</h2>"
                f"<p class=\"note\">LCOV source page is not generated yet. Run the backend with lcov/genhtml, then this report will embed <code>{html.escape(expected)}</code>.</p>"
                "</section>"
            )

        trace_rows = self._lcov_trace_rows(source_href, feedback, functions)
        links = [f'<a class="trace-link" href="{html.escape(index_href)}">Open LCOV Report</a>']
        if source_href:
            links.append(f'<a class="trace-link" href="{html.escape(source_href)}">Open Covered C Source</a>')
        if coverage_info.exists():
            links.append(f'<a class="trace-link" href="{html.escape(self._relative_href(coverage_info))}">Open coverage.info</a>')
        iframe_src = source_href or index_href
        return (
            "<section><h2>LCOV Source Trace</h2>"
            "<p class=\"note\">Trace executed and uncovered C lines back to the lcov/genhtml source coverage page.</p>"
            f"<div class=\"trace-actions\">{''.join(links)}</div>"
            f"{trace_rows}"
            f'<iframe class="lcov-frame" src="{html.escape(iframe_src)}" title="LCOV source coverage"></iframe>'
            "</section>"
        )

    def _lcov_trace_rows(self, source_href: str, feedback: CoverageFeedback | None, functions: list[FunctionInfo]) -> str:
        rows: list[str] = []
        uncovered = feedback.uncovered_items if feedback else []
        for item in uncovered[:80]:
            line = self._line_from_coverage_item(item)
            line_display = str(line) if line is not None else "-"
            link = self._lcov_line_link(source_href, line)
            rows.append(
                "<tr>"
                "<td>Uncovered</td>"
                f"<td>{html.escape(line_display)}</td>"
                f"<td>{link}</td>"
                f"<td>{html.escape(item)}</td>"
                "</tr>"
            )
        if not rows:
            for fn in functions[:80]:
                link = self._lcov_line_link(source_href, fn.start_line)
                rows.append(
                    "<tr>"
                    "<td>Function</td>"
                    f"<td>{fn.start_line}-{fn.end_line}</td>"
                    f"<td>{link}</td>"
                    f"<td>{html.escape(fn.name)}</td>"
                    "</tr>"
                )
        return (
            "<div class=\"table-wrap\"><table class=\"obligation-table\">"
            "<thead><tr><th>Type</th><th>Line</th><th>LCOV Source</th><th>Trace Detail</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )

    @staticmethod
    def _find_lcov_source_page(lcov_dir: Path, source: Path) -> Path | None:
        if not lcov_dir.exists():
            return None
        pages = [path for path in lcov_dir.rglob("*.html") if path.name.lower() != "index.html"]
        if not pages:
            return None
        source_name = source.name.lower()
        source_stem = source.stem.lower()
        preferred = [
            path
            for path in pages
            if (source_name in path.name.lower() or source_stem in path.name.lower()) and path.suffix.lower() == ".html"
        ]
        preferred.extend(
            path
            for path in pages
            if source_name in path.name.lower() or source_stem in path.name.lower()
        )
        seen: set[Path] = set()
        ordered = []
        for path in preferred or pages:
            if path not in seen:
                seen.add(path)
                ordered.append(path)
        return ordered[0]

    def _relative_href(self, path: Path) -> str:
        try:
            return path.relative_to(self.out_dir).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _line_from_coverage_item(text: str) -> int | None:
        match = re.search(r"\bline\s+(\d+)\b", text, flags=re.I)
        if match:
            return int(match.group(1))
        match = re.search(r"\b(?:DA|BRDA):(\d+)", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _lcov_line_link(source_href: str, line: int | None) -> str:
        if not source_href:
            return "Open source page after lcov run"
        if line is None:
            return f'<a href="{html.escape(source_href)}">Open source page</a>'
        return f'<a href="{html.escape(source_href)}#{line}">line {line}</a>'

    @staticmethod
    def _coverage_obligation_rows(functions: list[FunctionInfo]) -> str:
        rows: list[str] = []
        for fn in functions:
            rows.append(
                "<tr>"
                "<td>C0</td>"
                f"<td>{html.escape(fn.name)}</td>"
                f"<td>{fn.start_line}-{fn.end_line}</td>"
                "<td>Statement/line execution over function body</td>"
                "<td>lcov LF/LH line counters when available</td>"
                "</tr>"
            )
            for condition in fn.conditions:
                decisions = condition.decisions or [condition.text]
                rows.append(
                    "<tr>"
                    "<td>C1</td>"
                    f"<td>{html.escape(fn.name)}</td>"
                    f"<td>{condition.line}</td>"
                    f"<td>Decision/branch outcomes for <code>{html.escape(condition.text)}</code></td>"
                    "<td>lcov BRF/BRH branch counters when available</td>"
                    "</tr>"
                )
                for decision in decisions:
                    rows.append(
                        "<tr>"
                        "<td>MC/DC</td>"
                        f"<td>{html.escape(fn.name)}</td>"
                        f"<td>{condition.line}</td>"
                        f"<td>Independent effect pair for <code>{html.escape(decision)}</code></td>"
                        "<td>Generated-vector MC/DC evidence; lcov does not prove executed MC/DC independence</td>"
                        "</tr>"
                    )
        return "".join(rows)

    def _write_audit_checklist(self, source: Path, functions: list[FunctionInfo], cases: list[TestCase], backend: str) -> Path:
        path = self.out_dir / f"{source.stem}_{backend.lower()}_audit_checklist.md"
        safety_levels = ", ".join(sorted({case.asil for case in cases})) or "not specified"
        lines = [
            f"# {source.name} Audit Checklist",
            "",
            f"- Backend: {backend}",
            "- Target process evidence: ASPICE L2 work product support",
            f"- Target safety integrity: {safety_levels} test design support",
            f"- Functions analyzed: {len(functions)}",
            f"- Test cases generated: {len(cases)}",
            "",
            "## Review Gates",
            "",
            "- [ ] Requirements are linked or imported into `requirement_ref` fields.",
            "- [ ] Safety goals are linked or imported into `safety_goal_ref` fields.",
            "- [ ] Preconditions and setup are reviewed against the actual test environment.",
            "- [ ] Pointer/struct fixtures are implemented where TODO fixture comments exist.",
            "- [ ] Expected outputs are reviewed and replaced with concrete oracle values.",
            "- [ ] Generated tests are executed and pass/fail evidence is archived.",
            "- [ ] Coverage report is attached and gaps are dispositioned.",
            "- [ ] Reviewer approves MC/DC independence pairs for ASIL-D claims.",
            "",
            "## Test Case Evidence",
            "",
        ]
        for case in cases:
            values = ", ".join(f"{name}={value}" for name, value in case.values.items()) or "none"
            lines.extend(
                [
                    f"### {case.test_id}",
                    "",
                    f"- Function: `{case.function}`",
                    f"- Coverage target: {case.coverage}",
                    f"- Input vector: {values}",
                    f"- Description: {case.description}",
                    f"- Preconditions: {case.preconditions}",
                    f"- Setup: {case.setup}",
                    f"- Test steps: {case.test_steps}",
                    f"- Expected output: {case.expected}",
                    f"- Teardown: {case.teardown}",
                    f"- Pass/fail criteria: {case.pass_fail_criteria}",
                    f"- Requirement ref: {case.requirement_ref}",
                    f"- Safety goal ref: {case.safety_goal_ref}",
                    f"- Safety/process tags: {case.asil}, {case.aspice_level}",
                    f"- Rationale: {case.rationale}",
                    "",
                ]
            )
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _write_multi_file_report(self, backend: str, items: list[dict]) -> Path:
        path = self.out_dir / "all_files_report.html"
        total_functions = sum(len(item["functions"]) for item in items)
        total_cases = sum(len(item["cases"]) for item in items)
        total_conditions = sum(len(fn.conditions) for item in items for fn in item["functions"])
        metric_cells = self._aggregate_metric_cells(items)
        rows: list[str] = []
        for item in items:
            source = item["source"]
            functions = item["functions"]
            cases = item["cases"]
            feedback = item["feedback"]
            report_path = item.get("report_path")
            run_result = item.get("run_result")
            status = "Not run"
            if run_result:
                if run_result.returncode is None:
                    status = f"Skipped: {run_result.skipped_reason}"
                else:
                    status = "PASS" if run_result.returncode == 0 else f"FAIL ({run_result.returncode})"
            link = ""
            if report_path:
                link = f'<a href="{html.escape(self._relative_href(report_path))}">Open file report</a>'
            metrics = feedback.metrics if feedback else {}
            rows.append(
                "<tr>"
                f"<td>{html.escape(source.name)}</td>"
                f"<td>{len(functions)}</td>"
                f"<td>{sum(len(fn.conditions) for fn in functions)}</td>"
                f"<td>{len(cases)}</td>"
                f"<td>{html.escape(self._metric_display(metrics.get('C0')))}</td>"
                f"<td>{html.escape(self._metric_display(metrics.get('C1')))}</td>"
                f"<td>{html.escape(self._metric_display(metrics.get('MC/DC')))}</td>"
                f"<td>{html.escape(status)}</td>"
                f"<td>{link}</td>"
                "</tr>"
            )

        content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Testing Mate All Files Report</title>
  <style>
    :root {{ --bg: #f5f7fb; --panel: #ffffff; --line: #d8e0ec; --text: #182033; --muted: #5f6f86; --head: #edf2f8; --accent: #2563eb; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 0; background: var(--bg); color: var(--text); }}
    header {{ padding: 22px clamp(16px, 3vw, 34px); background: var(--panel); border-bottom: 1px solid var(--line); }}
    header h1 {{ margin: 0 0 6px; font-size: clamp(22px, 3vw, 32px); }}
    header p {{ margin: 0; color: var(--muted); }}
    main {{ width: min(1480px, 100%); margin: 0 auto; padding: 20px clamp(12px, 2vw, 28px) 32px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .card, section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04); }}
    .metric {{ font-size: 24px; font-weight: 700; margin-top: 6px; line-height: 1.1; color: var(--accent); }}
    .note {{ color: var(--muted); font-size: 12px; line-height: 1.35; margin-top: 6px; }}
    .table-wrap {{ width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); min-width: 980px; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid var(--line); border-right: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    th:last-child, td:last-child {{ border-right: 0; }}
    tr:last-child td {{ border-bottom: 0; }}
    th {{ position: sticky; top: 0; z-index: 1; background: var(--head); color: #243044; font-weight: 650; }}
    a {{ color: var(--accent); font-weight: 650; text-decoration: none; }}
  </style>
</head>
<body>
  <header>
    <h1>Testing Mate All Files Report</h1>
    <p>{html.escape(backend)} | generated {html.escape(datetime.now().isoformat(timespec="seconds"))}</p>
  </header>
  <main>
    <div class="grid">
      <div class="card">Files<div class="metric">{len(items)}</div></div>
      <div class="card">Functions<div class="metric">{total_functions}</div></div>
      <div class="card">Conditions<div class="metric">{total_conditions}</div></div>
      <div class="card">Test Cases<div class="metric">{total_cases}</div></div>
      {metric_cells}
    </div>
    <section>
      <h2>Source Reports</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>File</th><th>Functions</th><th>Conditions</th><th>Cases</th><th>C0</th><th>C1</th><th>MC/DC</th><th>Run Status</th><th>Report</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>"""
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _metric_display(value: float | None) -> str:
        return f"{value:.2f}%" if value is not None else "Missing"

    def _aggregate_metric_cells(self, items: list[dict]) -> str:
        cells: list[str] = []
        for metric in ("C0", "C1", "MC/DC"):
            values = [
                item["feedback"].metrics.get(metric)
                for item in items
                if item.get("feedback") and item["feedback"].metrics.get(metric) is not None
            ]
            display = self._metric_display(min(values) if values else None)
            cells.append(
                f'<div class="card">Min {html.escape(metric)}<div class="metric">{html.escape(display)}</div>'
                '<div class="note">Worst file-level metric across selected sources</div></div>'
            )
        return "".join(cells)

    @staticmethod
    def _backend_output_items(source: Path, backend: str) -> list[str]:
        if backend == "CppUTest":
            return [
                f"{source.stem}_cpputest_cases.csv",
                f"{source.stem}_cpputest/src/ (copied source/header environment)",
                f"{source.stem}_cpputest/{source.stem}_cpputest_test.cpp",
                f"{source.stem}_cpputest/run_cpputest.sh",
                f"{source.stem}_cpputest/coverage.info (after run with lcov)",
                f"{source.stem}_cpputest/{source.stem}_lcov/index.html (after run with lcov/genhtml)",
            ]
        if backend == "GoogleTest":
            return [
                f"{source.stem}_googletest_cases.csv",
                f"{source.stem}_gtest/src/ (copied source/header environment)",
                f"{source.stem}_gtest/{source.stem}_gtest_test.cpp",
                f"{source.stem}_gtest/run_gtest.sh",
                f"{source.stem}_gtest/coverage.info (after run with lcov)",
                f"{source.stem}_gtest/{source.stem}_lcov/index.html (after run with lcov/genhtml)",
            ]
        return []

    def _write_summary(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        feedback: CoverageFeedback | None = None,
        run_result: RunResult | None = None,
    ) -> Path:
        path = self.out_dir / f"{source.stem}_summary.md"
        lines = [f"# {source.name} Analysis", ""]
        lines.append(f"- Functions found: {len(functions)}")
        executed_count = self._executed_test_count(run_result)
        if executed_count is not None:
            lines.append(f"- Test cases run: {executed_count}")
            lines.append(f"- Test cases generated: {len(cases)}")
        else:
            lines.append(f"- Test cases generated: {len(cases)}")
        lines.append("")
        lines.append("## Coverage Metrics")
        metrics = feedback.metrics if feedback else {}
        notes = feedback.metric_notes if feedback else {}
        for metric in ("C0", "C1", "MC/DC"):
            value = metrics.get(metric)
            display = f"{value:.2f}%" if value is not None else "missing"
            note = notes.get(metric, "No executed report has been parsed for this metric yet.")
            lines.append(f"- {metric}: {display} - {note}")
        lines.append("")
        for fn in functions:
            params = ", ".join(f"{p.ctype} {p.name}" for p in fn.parameters) or "void"
            lines.append(f"## {fn.name}({params})")
            lines.append(f"- Lines: {fn.start_line}-{fn.end_line}")
            lines.append(f"- Conditions: {len(fn.conditions)}")
            for condition in fn.conditions:
                lines.append(f"  - line {condition.line}: `{condition.text}`")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


class TestingMateApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1360x860")
        self.minsize(1120, 720)
        self.configure(bg="#f4f7fb")
        self.sources: list[Path] = []
        self.generated_paths: list[Path] = []
        self.report_path: Path | None = None
        self._build_style()
        self._build_ui()

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.option_add("*Font", "{Segoe UI} 10")
        self.option_add("*Listbox.Font", "{Segoe UI} 9")
        style.configure("TFrame", background="#f4f7fb")
        style.configure("Panel.TFrame", background="#ffffff", relief="flat")
        style.configure("Surface.TFrame", background="#f8fafc", relief="flat")
        style.configure("Metric.TFrame", background="#f8fafc", relief="flat")
        style.configure("TLabel", background="#f4f7fb", foreground="#172033", font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background="#ffffff", foreground="#172033", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#64748b", font=("Segoe UI", 9))
        style.configure("Title.TLabel", background="#f4f7fb", foreground="#0f172a", font=("Segoe UI Semibold", 24))
        style.configure("Subtle.TLabel", background="#f4f7fb", foreground="#64748b", font=("Segoe UI", 10))
        style.configure("Status.TLabel", background="#e8f2ff", foreground="#1d4ed8", font=("Segoe UI Semibold", 9), padding=(10, 5))
        style.configure("PanelTitle.TLabel", background="#ffffff", foreground="#0f172a", font=("Segoe UI Semibold", 12))
        style.configure("Section.TLabel", background="#ffffff", foreground="#334155", font=("Segoe UI Semibold", 10))
        style.configure("MetricLabel.TLabel", background="#f8fafc", foreground="#64748b", font=("Segoe UI", 9))
        style.configure("MetricValue.TLabel", background="#f8fafc", foreground="#0f172a", font=("Segoe UI Semibold", 17))
        style.configure("TEntry", padding=(8, 6), fieldbackground="#ffffff", bordercolor="#cbd5e1", lightcolor="#cbd5e1", darkcolor="#cbd5e1")
        style.configure("TCombobox", padding=(8, 6), fieldbackground="#ffffff", bordercolor="#cbd5e1", arrowsize=14)
        style.configure("TButton", font=("Segoe UI Semibold", 10), padding=(12, 8), borderwidth=1)
        style.configure("Accent.TButton", background="#1f6feb", foreground="#ffffff", bordercolor="#1f6feb")
        style.map("Accent.TButton", background=[("active", "#1d4ed8"), ("pressed", "#1e40af")], foreground=[("disabled", "#dbeafe")])
        style.configure("Secondary.TButton", background="#f8fafc", foreground="#172033", bordercolor="#cbd5e1")
        style.map("Secondary.TButton", background=[("active", "#eef2f7"), ("pressed", "#e2e8f0")])
        style.configure("Danger.TButton", background="#fff1f2", foreground="#9f1239", bordercolor="#fecdd3")
        style.map("Danger.TButton", background=[("active", "#ffe4e6"), ("pressed", "#fecdd3")])
        style.configure("TCheckbutton", background="#ffffff", foreground="#172033", font=("Segoe UI", 10))
        style.configure("TRadiobutton", background="#ffffff", foreground="#172033", font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=30, fieldbackground="#ffffff", background="#ffffff", foreground="#172033", font=("Segoe UI", 10), bordercolor="#dbe3ef", borderwidth=1)
        style.configure("Treeview.Heading", background="#eef2f7", foreground="#334155", font=("Segoe UI Semibold", 10), padding=(8, 7))
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#0f172a")])

    def _create_scrollable_panel(self, parent: ttk.Frame) -> ttk.Frame:
        shell = ttk.Frame(parent, style="Panel.TFrame")
        shell.grid(row=0, column=0, sticky="nsw", padx=(0, 18))
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)

        canvas = tk.Canvas(shell, width=398, bg="#ffffff", bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        panel = ttk.Frame(canvas, style="Panel.TFrame", padding=18)
        window_id = canvas.create_window((0, 0), window=panel, anchor="nw")

        def resize_scroll_region(_event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def resize_panel(event: tk.Event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        def bind_mousewheel(_event: tk.Event) -> None:
            canvas.bind_all("<MouseWheel>", on_mousewheel)

        def unbind_mousewheel(_event: tk.Event) -> None:
            canvas.unbind_all("<MouseWheel>")

        def on_mousewheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        panel.bind("<Configure>", resize_scroll_region)
        canvas.bind("<Configure>", resize_panel)
        canvas.bind("<Enter>", bind_mousewheel)
        canvas.bind("<Leave>", unbind_mousewheel)
        return panel

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=24)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 18))
        header.columnconfigure(0, weight=1)
        title_area = ttk.Frame(header)
        title_area.grid(row=0, column=0, sticky="w")
        ttk.Label(title_area, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_area,
            text="Clang-first C test generation for GoogleTest and CppUTest",
            style="Subtle.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        status_area = ttk.Frame(header)
        status_area.grid(row=0, column=1, sticky="e")
        ttk.Label(status_area, text="Clang DB", style="Status.TLabel").pack(side="left", padx=(0, 8))
        ttk.Label(status_area, text="GTest / CppUTest", style="Status.TLabel").pack(side="left")

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = self._create_scrollable_panel(body)
        right = ttk.Frame(body, style="Panel.TFrame", padding=18)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)
        self.output_dir = tk.StringVar(value=str(Path.cwd() / "TestMate_out"))
        
        ttk.Label(left, text="Project Inputs", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        buttons = ttk.Frame(left, style="Panel.TFrame")
        buttons.pack(fill="x", pady=(0, 10), anchor="w")
        self.add_source_button = ttk.Button(buttons, text="Add Source", command=self._show_add_source_menu, style="Accent.TButton")
        self.add_source_button.pack(side="left", fill="x", expand=True)
        ttk.Button(buttons, text="Clear", command=self._clear_sources, style="Danger.TButton").pack(side="right", fill="x", expand=True, padx=(8, 0))
        compile_row = ttk.Frame(left, style="Panel.TFrame")
        compile_row.pack(fill="x", pady=(0, 10))
        self.compile_commands = tk.StringVar()
        ttk.Entry(compile_row, textvariable=self.compile_commands).pack(side="left", fill="x", expand=True)
        ttk.Button(compile_row, text="Compile DB", command=self._choose_compile_commands, style="Secondary.TButton").pack(side="left", padx=(8, 0))
        action_row = ttk.Frame(left, style="Panel.TFrame")
        action_row.pack(fill="x", pady=(8, 0))
        ttk.Button(action_row, text="Generate", command=self._run_async, style="Accent.TButton").pack(side="left", fill="x", expand=True)
        ttk.Button(action_row, text="ReTest", command=self._rerun_async, style="Danger.TButton").pack(side="left", fill="x", expand=True, padx=(8, 0))
        ttk.Button(action_row, text="View Report", command=self._open_report_file, style="Secondary.TButton").pack(side="right", padx=(8, 0))

        self.source_list = tk.Listbox(
            left,
            height=6,
            width=34,
            bd=0,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#dbe3ef",
            highlightcolor="#93c5fd",
            selectbackground="#dbeafe",
            selectforeground="#0f172a",
            background="#f8fafc",
            foreground="#172033",
            font=("Segoe UI", 9),
        )
        self.source_list.pack(fill="x", pady=(10, 16))

        ttk.Label(left, text="Coverage & Level", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        self.coverage = tk.StringVar(value="MC/DC")
        Coverages = ttk.Frame(left, style="Panel.TFrame")
        Coverages.pack(fill="x", pady=(0, 4))
        for text, value in (("C0/C1", "C0/C1"), ("MCDC", "MC/DC")):
            ttk.Radiobutton(Coverages, text=text, variable=self.coverage, value=value).pack(side="left", padx=(0, 20))

        self.level = tk.StringVar(value="UT")
        levels = ttk.Frame(left, style="Panel.TFrame")
        levels.pack(fill="x", pady=(2, 0))
        for text in ("UT", "IT", "ST"):
            ttk.Radiobutton(levels, text=text, variable=self.level, value=text).pack(side="left", padx=(0, 20))
        function_filter_row = ttk.Frame(left, style="Panel.TFrame")
        function_filter_row.pack(fill="x", pady=(6, 0))
        self.include_static_functions = tk.BooleanVar(value=False)
        self.include_main_function = tk.BooleanVar(value=False)
        ttk.Checkbutton(function_filter_row, text="Include static functions", variable=self.include_static_functions).pack(anchor="w")
        ttk.Checkbutton(function_filter_row, text="Include main()", variable=self.include_main_function).pack(anchor="w")
        iso_row = ttk.Frame(left, style="Panel.TFrame")
        iso_row.pack(fill="x", pady=(6, 0))
        self.iso26262_profile = tk.BooleanVar(value=False)
        ttk.Checkbutton(iso_row, text="ISO 26262 method profile", variable=self.iso26262_profile).pack(side="left")
        self.asil_level = tk.StringVar(value="ASIL D")
        asil_box = ttk.Combobox(iso_row, textvariable=self.asil_level, values=("QM", "ASIL A", "ASIL B", "ASIL C", "ASIL D"), state="readonly", width=8)
        asil_box.pack(side="left", padx=(8, 0))

        ttk.Label(left, text="Build Context", style="Section.TLabel").pack(anchor="w", pady=(14, 8))
        self.compiler = tk.StringVar(value=DEFAULT_COMPILER)
        compilerFR = ttk.Frame(left, style="Panel.TFrame")
        compilerFR.pack(fill="x")
        ttk.Entry(compilerFR, textvariable=self.compiler).pack(side="left", fill="x", expand=True)

        self.preprocess = tk.BooleanVar(value=True)
        ttk.Checkbutton(compilerFR, text="Preprocess source first", variable=self.preprocess).pack(anchor="w", padx=(8, 0))

        ttk.Label(left, text="Include dirs (; separated)", style="Muted.TLabel").pack(anchor="w", pady=(8, 4))
        self.include_dirs = tk.StringVar()
        ttk.Entry(left, textvariable=self.include_dirs).pack(fill="x")

        ttk.Label(left, text="Defines (; separated)", style="Muted.TLabel").pack(anchor="w", pady=(8, 4))
        self.defines = tk.StringVar()
        ttk.Entry(left, textvariable=self.defines).pack(fill="x")

        ttk.Label(left, text="Output & Backend", style="Section.TLabel").pack(anchor="w", pady=(14, 8))
        output_row = ttk.Frame(left, style="Panel.TFrame")
        output_row.pack(fill="x")
        # self.output_dir = tk.StringVar(value=str(Path.cwd() / "TestMate_out"))
        ttk.Entry(output_row, textvariable=self.output_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text="...", width=3, command=self._choose_output, style="Secondary.TButton").pack(side="left", padx=(8, 0))

        self.backend = tk.StringVar(value="GoogleTest")
        backend_box = ttk.Combobox(left, textvariable=self.backend, values=("GoogleTest", "CppUTest"), state="readonly")
        backend_box.pack(fill="x", pady=(8, 0))
        self.run_after_generate = tk.BooleanVar(value=True)
        # ttk.Checkbutton(left, text="Run after generate", variable=self.run_after_generate).pack(anchor="w", pady=(8, 0))

        ttk.Label(left, text="LLM Refinement", style="Section.TLabel").pack(anchor="w", pady=(14, 8))
        self.use_ollama = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Use local Ollama", variable=self.use_ollama, command=self._ollama_select).pack(anchor="w")

        model_row = ttk.Frame(left, style="Panel.TFrame")
        model_row.pack(fill="x", pady=(8, 0))
        ttk.Label(model_row, text="Model", style="Panel.TLabel").pack(side="left")
        self.ollama_model = tk.StringVar(value="qwen2.5-coder:3b")
        self.ollama_model_entry = ttk.Entry(model_row, textvariable=self.ollama_model)
        self.ollama_model_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))

        target_row = ttk.Frame(left, style="Panel.TFrame")
        target_row.pack(fill="x", pady=(8, 0))
        ttk.Label(target_row, text="Target %", style="Panel.TLabel").pack(side="left")
        self.target_coverage = tk.StringVar(value="100")
        self.target_coverage_entry = ttk.Entry(target_row, textvariable=self.target_coverage, width=7)
        self.target_coverage_entry.pack(side="left", padx=(8, 16))
        ttk.Label(target_row, text="Rounds", style="Panel.TLabel").pack(side="left")
        self.max_ai_rounds = tk.StringVar(value="1")
        self.max_ai_rounds_entry = ttk.Entry(target_row, textvariable=self.max_ai_rounds, width=5)
        self.max_ai_rounds_entry.pack(side="left", padx=(8, 0))

        report_row = ttk.Frame(left, style="Panel.TFrame")
        report_row.pack(fill="x", pady=(8, 0))
        self.report_file = tk.StringVar()
        self.report_file_entry = ttk.Entry(report_row, textvariable=self.report_file)
        self.report_file_entry.pack(side="left", fill="x", expand=True)
        self.report_button = ttk.Button(report_row, text="Report", command=self._choose_report, style="Secondary.TButton")
        self.report_button.pack(side="left", padx=(8, 0))
        self.refine_report_button = ttk.Button(left, text="Refine From Report & Run", command=self._refine_run_async, style="Accent.TButton")
        self.refine_report_button.pack(fill="x", pady=(8, 0))
        self.ollama_controls = [
            self.ollama_model_entry,
            self.target_coverage_entry,
            self.max_ai_rounds_entry,
            self.report_file_entry,
            self.report_button,
            self.refine_report_button,
        ]
        self._set_ollama_controls_state()

        metrics = ttk.Frame(right, style="Panel.TFrame")
        metrics.grid(row=0, column=0, sticky="ew")
        for idx in range(4):
            metrics.columnconfigure(idx, weight=1)
        self.metric_vars = {
            "files": tk.StringVar(value="0"),
            "functions": tk.StringVar(value="0"),
            "conditions": tk.StringVar(value="0"),
            "tests": tk.StringVar(value="0"),
        }
        for col, (label, var) in enumerate(
            [("Files", self.metric_vars["files"]), ("Functions", self.metric_vars["functions"]), ("Conditions", self.metric_vars["conditions"]), ("Tests", self.metric_vars["tests"])]
        ):
            card = ttk.Frame(metrics, style="Metric.TFrame", padding=(14, 12))
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0))
            ttk.Label(card, text=label, style="MetricLabel.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=var, style="MetricValue.TLabel").pack(anchor="w", pady=(2, 0))

        ttk.Label(right, text="Coverage Summary and Feedback", style="PanelTitle.TLabel").grid(row=1, column=0, sticky="w", pady=(16, 8))
        columns = ("function", "line", "metric", "condition", "status")
        self.tree = ttk.Treeview(right, columns=columns, show="headings")
        for col, title, width in [
            ("function", "Function", 140),
            ("line", "Line", 60),
            ("metric", "Metric", 80),
            ("condition", "Condition / Obligation", 280),
            ("status", "Coverage Status", 200),
        ]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, stretch=True)
        self.tree.grid(row=2, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=2, column=1, sticky="ns")

        log_frame = ttk.Frame(right, style="Surface.TFrame", padding=12)
        log_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Label(log_frame, text="Log", style="MetricLabel.TLabel").pack(anchor="w")
        self.log = tk.Text(
            log_frame,
            height=7,
            bd=0,
            highlightthickness=1,
            highlightbackground="#dbe3ef",
            highlightcolor="#93c5fd",
            bg="#0f172a",
            fg="#dbeafe",
            insertbackground="#ffffff",
            font=("Cascadia Mono", 9),
            wrap="word",
        )
        self.log.pack(fill="x", pady=(8, 0))

    def _add_sources(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(SOURCE_EXTENSIONS))
        paths = filedialog.askopenfilenames(title="Select C/C++ source files", filetypes=[("C/C++ source files", patterns), ("All files", "*.*")])
        self._add_source_paths([Path(raw) for raw in paths])

    def _show_add_source_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Files...", command=self._add_sources)
        menu.add_command(label="Folder...", command=self._add_source_folder)
        button = self.add_source_button
        x = button.winfo_rootx()
        y = button.winfo_rooty() + button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()
        
    def _add_source_folder(self) -> None:
        selected = filedialog.askdirectory(title="Select source/project folder")
        if not selected:
            return
        discovered = ProjectScanner.discover_sources(Path(selected), include_headers=True)
        self._add_source_paths(discovered)
        self._append_log(f"Scanned folder: {selected}; added {len(discovered)} source/header files.")

    def _choose_compile_commands(self) -> None:
        selected = filedialog.askopenfilename(title="Select compile_commands.json", filetypes=[("compile_commands.json", "compile_commands.json"), ("JSON files", "*.json"), ("All files", "*.*")])
        if not selected:
            return
        self.compile_commands.set(selected)
        try:
            database = CompileCommandsDatabase.load(Path(selected))
            sources = database.source_files()
            self._add_source_paths(sources)
            existing_includes = [item.strip() for item in self.include_dirs.get().split(";") if item.strip()]
            merged_includes = Orchestrator._unique_strings(existing_includes + database.include_dirs())
            self.include_dirs.set(";".join(merged_includes))
            existing_defines = [item.strip() for item in self.defines.get().split(";") if item.strip()]
            merged_defines = Orchestrator._unique_strings(existing_defines + database.defines())
            self.defines.set(";".join(merged_defines))
            self._append_log(f"Loaded compile_commands.json: added {len(sources)} files; merged include dirs and defines.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not load compile_commands.json:\n{exc}")

    def _add_source_paths(self, paths: list[Path]) -> None:
        added = 0
        existing = {path.resolve() for path in self.sources if path.exists()}
        for path in paths:
            if path.suffix.lower() not in SOURCE_EXTENSIONS or not path.exists():
                continue
            resolved = path.expanduser().resolve()
            if resolved in existing:
                continue
            existing.add(resolved)
            self.sources.append(resolved)
            added += 1
        self._refresh_source_list()
        if added:
            self._append_log(f"Added {added} source input files.")

    def _refresh_source_list(self) -> None:
        self.source_list.delete(0, "end")
        for path in self.sources:
            self.source_list.insert("end", str(path))
        self.metric_vars["files"].set(str(len(self.sources)))

    def _clear_sources(self) -> None:
        self.sources.clear()
        self.source_list.delete(0, "end")
        self.metric_vars["files"].set("0")

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="Select output folder")
        if selected:
            self.output_dir.set(selected)

    def _ollama_select(self) -> None:
        self._set_ollama_controls_state()

    def _set_ollama_controls_state(self) -> None:
        state = "normal" if self.use_ollama.get() else "disabled"
        for widget in getattr(self, "ollama_controls", []):
            widget.configure(state=state)

    def _choose_report(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select backend fail or coverage report",
            filetypes=[("Report files", "*.txt *.log *.html *.htm *.csv *.json"), ("All files", "*.*")],
        )
        if selected:
            self.report_file.set(selected)
            self.report_path = Path(selected)

    def _open_report_file(self) -> None:
        report = self._latest_report_path()
        if not report:
            messagebox.showwarning(APP_NAME, "No report file is available yet. Generate a report or select a report file first.")
            return
        try:
            os.startfile(str(report))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not open report:\n{report}\n\n{exc}")

    def _latest_report_path(self) -> Path | None:
        for path in reversed(self.generated_paths):
            if path.suffix.lower() in {".html", ".htm"} and path.exists():
                return path
        selected = self.report_file.get().strip()
        if selected:
            path = Path(selected)
            if path.exists():
                return path
        out_dir = source_scoped_output_dir(Path(self.output_dir.get()).expanduser(), self.sources)
        backend = self.backend.get()
        for source in self.sources:
            candidate = out_dir / backend / f"{source.stem}.html"
            if candidate.exists():
                return candidate
        return None

    def _run_async(self) -> None:
        if not self.sources and not self.compile_commands.get().strip():
            messagebox.showwarning(APP_NAME, "Add source files, a source folder, or compile_commands.json.")
            return
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def _refine_run_async(self) -> None:
        if not self.sources and not self.compile_commands.get().strip():
            messagebox.showwarning(APP_NAME, "Add source files, a source folder, or compile_commands.json.")
            return
        report = self.report_file.get().strip()
        if not report or not Path(report).exists():
            messagebox.showwarning(APP_NAME, "Select an existing coverage/fail report first.")
            return
        thread = threading.Thread(target=self._refine_and_run_from_report, daemon=True)
        thread.start()

    def _rerun_async(self) -> None:
        if not self.sources and not self.compile_commands.get().strip():
            messagebox.showwarning(APP_NAME, "Add source files, a source folder, or compile_commands.json.")
            return
        thread = threading.Thread(target=self._rerun_existing_testcases, daemon=True)
        thread.start()

    def _run(self) -> None:
        self._set_log("")
        self._clear_tree()
        result = Orchestrator(self._append_log).analyze_and_generate(self._orchestrator_options())
        self.generated_paths = result.generated_paths
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
        if result.generated_paths:
            self._append_log("Generated artifacts:")
            for path in result.generated_paths:
                self._append_log(f"  {path}")
        title = "Analyze & Generate failed" if result.exit_code else "Analyze & Generate complete"
        self._show_completion_popup(title, result.summary_lines, result.generated_paths, is_error=bool(result.exit_code))

    def _refine_and_run_from_report(self) -> None:
        self._set_log("")
        self._clear_tree()
        result = Orchestrator(self._append_log).refine_from_report(self._orchestrator_options())
        self.generated_paths = result.generated_paths
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
        if result.generated_paths:
            self._append_log("Generated artifacts:")
            for path in result.generated_paths:
                self._append_log(f"  {path}")
        title = "Ollama refine & run failed" if result.exit_code else "Ollama refine & run complete"
        self._show_completion_popup(title, result.summary_lines, result.generated_paths, is_error=bool(result.exit_code))

    def _rerun_existing_testcases(self) -> None:
        self._set_log("")
        self._clear_tree()
        result = Orchestrator(self._append_log).rerun_existing(self._orchestrator_options())
        self.generated_paths = result.generated_paths
        self._update_metrics(len(result.functions), sum(len(fn.conditions) for fn in result.functions), len(result.cases))
        self._populate_tree(result.functions, result.feedback)
        if result.generated_paths:
            self._append_log("Refreshed reports:")
            for path in result.generated_paths:
                self._append_log(f"  {path}")
        title = "Rerun testcases failed" if result.exit_code else "Rerun testcases complete"
        self._show_completion_popup(title, result.summary_lines, result.generated_paths, is_error=bool(result.exit_code))

    def _orchestrator_options(self) -> OrchestratorOptions:
        report = self.report_file.get().strip()
        return OrchestratorOptions(
            sources=list(self.sources),
            output_dir=Path(self.output_dir.get()),
            coverage=self.coverage.get(),
            level=self.level.get(),
            backend=self.backend.get(),
            compiler=self.compiler.get(),
            include_dirs=[item.strip() for item in self.include_dirs.get().split(";") if item.strip()],
            defines=[item.strip() for item in self.defines.get().split(";") if item.strip()],
            preprocess=self.preprocess.get(),
            iso26262_profile=self.iso26262_profile.get(),
            asil_level=self.asil_level.get(),
            run_after_generate=True,
            report_file=Path(report) if report else None,
            compile_commands=Path(self.compile_commands.get().strip()) if self.compile_commands.get().strip() else None,
            include_static_functions=self.include_static_functions.get(),
            include_main_function=self.include_main_function.get(),
            use_ollama=self.use_ollama.get(),
            ollama_model=self.ollama_model.get(),
            target_coverage_percent=self._float_setting(self.target_coverage.get(), 100.0),
            max_ai_rounds=max(1, int(self._float_setting(self.max_ai_rounds.get(), 1.0))),
        )

    @staticmethod
    def _float_setting(value: str, default: float) -> float:
        try:
            return float(value)
        except ValueError:
            return default

    def _set_log(self, text: str) -> None:
        self.after(0, lambda: self._replace_log(text))

    def _replace_log(self, text: str) -> None:
        self.log.delete("1.0", "end")
        if text:
            self.log.insert("end", text)

    def _append_log(self, text: str) -> None:
        self.after(0, lambda: self._append_log_now(text))

    def _append_log_now(self, text: str) -> None:
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def _show_completion_popup(self, title: str, summary_lines: list[str], generated_paths: list[Path], *, is_error: bool = False) -> None:
        def show() -> None:
            details = "\n".join(summary_lines[:8]) if summary_lines else "No source files were processed."
            if len(summary_lines) > 8:
                details += f"\n... {len(summary_lines) - 8} more"
            artifact_note = f"\n\nArtifacts generated: {len(generated_paths)}"
            if generated_paths:
                report_path = next((path for path in reversed(generated_paths) if path.suffix.lower() in {".html", ".htm"}), generated_paths[-1])
                artifact_note += f"\nReport: {report_path}"
            message = f"{title}\n\n{details}{artifact_note}"
            if is_error:
                messagebox.showerror(APP_NAME, message)
            else:
                messagebox.showinfo(APP_NAME, message)

        self.after(0, show)

    def _clear_tree(self) -> None:
        self.after(0, lambda: self.tree.delete(*self.tree.get_children()))

    def _populate_tree(self, functions: list[FunctionInfo] | None = None, feedback: CoverageFeedback | None = None) -> None:
        def insert() -> None:
            self.tree.delete(*self.tree.get_children())
            if not functions:
                return
            
            uncovered_items = feedback.uncovered_items if feedback else []
            
            # Populate coverage obligations from functions
            for fn in functions:
                # Add C0 obligation (statement coverage for function body)
                status = "Covered" if not any("C0" in item and fn.name in item for item in uncovered_items) else "Uncovered"
                self.tree.insert("", "end", values=(fn.name, f"{fn.start_line}-{fn.end_line}", "C0", f"Statement coverage ({fn.start_line}-{fn.end_line})", status))
                
                # Add C1 and MC/DC obligations (conditions)
                for i, condition in enumerate(fn.conditions):
                    c1_status = "Uncovered"
                    mcdc_status = "Uncovered"
                    
                    for item in uncovered_items:
                        if "C1" in item and str(condition.line) in item:
                            c1_status = "Uncovered"
                            break
                    else:
                        c1_status = "Covered"
                    
                    for item in uncovered_items:
                        if "MC/DC" in item and str(condition.line) in item:
                            mcdc_status = "Uncovered"
                            break
                    else:
                        mcdc_status = "Covered"
                    
                    # Show C1 condition
                    self.tree.insert("", "end", values=(fn.name, condition.line, "C1", condition.text, c1_status))
                    # Show MC/DC condition  
                    self.tree.insert("", "end", values=(fn.name, condition.line, "MC/DC", condition.text, mcdc_status))

        self.after(0, insert)

    def _update_metrics(self, functions: int, conditions: int, tests: int) -> None:
        self.after(
            0,
            lambda: (
                self.metric_vars["files"].set(str(len(self.sources))),
                self.metric_vars["functions"].set(str(functions)),
                self.metric_vars["conditions"].set(str(conditions)),
                self.metric_vars["tests"].set(str(tests)),
            ),
        )


def merge_generated_coverage_feedback(feedback: CoverageFeedback, functions: list[FunctionInfo], cases: list[TestCase], coverage: str) -> CoverageFeedback:
    if coverage in {"C0/C1", "MC/DC"}:
        feedback = C1CoverageEngine.merge(feedback, C1CoverageEngine().analyze(functions, cases))
    if coverage == "MC/DC":
        feedback = MCDCCoverageEngine.merge(feedback, MCDCCoverageEngine().analyze(functions, cases))
    return complete_no_obligation_metrics(feedback, functions)


def complete_no_obligation_metrics(feedback: CoverageFeedback, functions: list[FunctionInfo]) -> CoverageFeedback:
    if any(fn.conditions for fn in functions):
        return feedback
    metrics = dict(feedback.metrics)
    notes = dict(feedback.metric_notes)
    changed = False
    if metrics.get("C1") is None:
        metrics["C1"] = 100.0
        notes["C1"] = "No branch/decision obligations were discovered; C1 is complete by definition."
        changed = True
    if metrics.get("MC/DC") is None:
        metrics["MC/DC"] = 100.0
        notes["MC/DC"] = "No branch/condition obligations were discovered; MC/DC is complete by definition."
        changed = True
    if not changed:
        return feedback
    return CoverageFeedback(
        source=feedback.source,
        raw_text=feedback.raw_text,
        coverage_percent=CoverageReportParser()._best_percent("", metrics),
        metrics=metrics,
        metric_notes=notes,
        uncovered_items=feedback.uncovered_items,
        failures=feedback.failures,
    )


def coverage_log_summary(feedback: CoverageFeedback) -> str:
    parts = []
    for metric in ("C0", "C1", "MC/DC"):
        value = feedback.metrics.get(metric)
        parts.append(f"{metric} {'unknown' if value is None else f'{value:.2f}%'}")
    if feedback.coverage_percent is not None and not any(value is not None for value in feedback.metrics.values()):
        parts.append(f"coverage {feedback.coverage_percent:.2f}%")
    return ", ".join(parts) if parts else "coverage unknown"


def backend_csv_id(backend: str) -> str:
    if backend == "CppUTest":
        return "cpputest"
    if backend == "GoogleTest":
        return "googletest"
    return re.sub(r"[^A-Za-z0-9]+", "_", backend).strip("_").lower()


def has_report_evidence(feedback: CoverageFeedback) -> bool:
    if feedback.source in {"none", "generated test vectors"}:
        return bool(feedback.failures)
    return bool(feedback.raw_text.strip() or feedback.metrics or feedback.uncovered_items or feedback.failures)


def feedback_meets_target(feedback: CoverageFeedback, target_percent: float) -> bool:
    values = [value for value in feedback.metrics.values() if value is not None]
    if values:
        return min(values) >= target_percent
    return feedback.coverage_percent is not None and feedback.coverage_percent >= target_percent


def merge_cases(existing: list[TestCase], candidates: list[TestCase]) -> int:
    signatures = {(case.function, tuple(sorted(case.values.items()))) for case in existing}
    added = 0
    for candidate in candidates:
        signature = (candidate.function, tuple(sorted(candidate.values.items())))
        if signature in signatures:
            continue
        signatures.add(signature)
        existing.append(candidate)
        added += 1
    return added


def load_coverage_feedback(report: Path | None, parser: CoverageReportParser, log: Callable[[str], None] | None = None) -> CoverageFeedback:
    if report:
        if report.exists():
            feedback = parser.parse_file(report)
            if log:
                log(f"Loaded coverage/fail report: {report} ({coverage_log_summary(feedback)})")
            return feedback
        if log:
            log(f"Report not found: {report}")
    return CoverageFeedback(source="none", raw_text="", uncovered_items=[], failures=[])


def collect_backend_feedback(parser: CoverageReportParser, out_dir: Path, source: Path, backend: str, log: Callable[[str], None] | None = None) -> CoverageFeedback | None:
    candidates: list[Path] = []
    target_files: list[Path] = [source]
    if backend == "CppUTest":
        base_dir = out_dir / f"{source.stem}_cpputest"
        candidates.append(base_dir / "coverage.info")
        target_files.append(base_dir / "src" / source.name)
    elif backend == "GoogleTest":
        base_dir = out_dir / f"{source.stem}_gtest"
        candidates.append(base_dir / "coverage.info")
        target_files.append(base_dir / "src" / source.name)
    for candidate in candidates:
        if candidate.exists():
            feedback = parser.parse_file(candidate, target_files=target_files)
            if log:
                source_note = "lcov tracefile" if candidate.suffix.lower() in {".info", ".lcov"} else "backend coverage"
                log(f"Parsed {source_note}: {candidate} ({coverage_log_summary(feedback)})")
            return feedback
    if log:
        log(f"No backend coverage artifact found for {backend}; report remains based on selected input only.")
    return None


def refine_with_ollama(
    source: Path,
    functions: list[FunctionInfo],
    cases: list[TestCase],
    feedback: CoverageFeedback,
    coverage: str,
    level: str,
    backend: str,
    model: str,
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
        client = OllamaClient(model=model)
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
    client = OllamaClient(model=model)
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


@dataclass
class OrchestratorOptions:
    sources: list[Path]
    output_dir: Path
    coverage: str = "MC/DC"
    level: str = "UT"
    backend: str = "GoogleTest"
    compiler: str = DEFAULT_COMPILER
    include_dirs: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    preprocess: bool = True
    iso26262_profile: bool = False
    asil_level: str = "ASIL D"
    run_after_generate: bool = True
    report_file: Path | None = None
    compile_commands: Path | None = None
    include_static_functions: bool = False
    include_main_function: bool = False
    use_ollama: bool = False
    ollama_model: str = "qwen2.5-coder:3b"
    target_coverage_percent: float = 100.0
    max_ai_rounds: int = 1


@dataclass
class OrchestratorResult:
    functions: list[FunctionInfo] = field(default_factory=list)
    cases: list[TestCase] = field(default_factory=list)
    generated_paths: list[Path] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)
    feedback: CoverageFeedback | None = None
    exit_code: int = 0


class Orchestrator:
    def __init__(self, log_callback: Callable[[str], None] | None = None):
        self.log_callback = log_callback or (lambda _text: None)

    def _log(self, text: str) -> None:
        self.log_callback(text)

    def analyze_and_generate(self, options: OrchestratorOptions) -> OrchestratorResult:
        return self._run(options, refine_from_report=False)

    def refine_from_report(self, options: OrchestratorOptions) -> OrchestratorResult:
        return self._run(options, refine_from_report=True)

    def rerun_existing(self, options: OrchestratorOptions) -> OrchestratorResult:
        result = OrchestratorResult()
        project_sources, _include_dirs, _defines, _compiler = self._resolve_project_inputs(options)
        out_dir = source_scoped_output_dir(options.output_dir.expanduser(), project_sources)
        if out_dir != options.output_dir.expanduser():
            self._log(f"Output folder: {out_dir}")
        backend = options.backend
        report_parser = CoverageReportParser()
        report_items: list[dict] = []

        for source in project_sources:
            try:
                backend_out_dir = out_dir / backend
                functions, cases = self._load_existing_analysis(backend_out_dir, source)
                runner_path = backend_harness_path(backend_out_dir, source, backend)
                if not runner_path.exists():
                    raise FileNotFoundError(f"Runner script was not generated: {runner_path}")

                self._log(f"Rerunning existing {backend} testcase environment: {runner_path}")
                run_result = TestBackendRunner().run(backend, backend_out_dir, source)
                self._log(format_run_result(run_result))
                if run_result.returncode not in (0, None):
                    result.exit_code = run_result.returncode
                if run_result.stdout.strip():
                    self._log("Run stdout:")
                    self._log(run_result.stdout[-1200:])
                if run_result.stderr.strip():
                    self._log("Run stderr:")
                    self._log(run_result.stderr[-1200:])
                if run_result.returncode not in (0, None) or (run_result.returncode is None and run_result.skipped_reason):
                    result.exit_code = run_result.returncode or 1
                    result.summary_lines.append(f"{source.name}: ERROR {format_run_result(run_result)}")
                    return result

                feedback = collect_backend_feedback(report_parser, backend_out_dir, source, backend, self._log)
                if feedback is None:
                    feedback = CoverageFeedback(source="none", raw_text="", uncovered_items=[], failures=[])
                feedback = merge_generated_coverage_feedback(feedback, functions, cases, options.coverage)

                writer = ArtifactWriter(backend_out_dir)
                report_path = writer._write_html_report(source, functions, cases, backend, feedback, run_result)
                summary_path = writer._write_summary(source, functions, cases, feedback, run_result)
                result.generated_paths.extend([report_path, summary_path])
                report_items.append(
                    {
                        "source": source,
                        "functions": functions,
                        "cases": cases,
                        "feedback": feedback,
                        "run_result": run_result,
                        "report_path": report_path,
                    }
                )
                result.functions.extend(functions)
                result.cases.extend(cases)
                result.summary_lines.append(
                    f"{source.name}: reran {len(cases)} generated cases, {format_run_result(run_result)}, {coverage_log_summary(feedback)}"
                )
            except Exception as exc:
                self._log(f"ERROR rerun {source}: {exc}")
                result.summary_lines.append(f"{source.name}: ERROR {exc}")
                result.exit_code = 1
                return result

        if len(report_items) > 1:
            final_report = ArtifactWriter(out_dir / backend)._write_multi_file_report(backend, report_items)
            result.generated_paths.append(final_report)
            self._log(f"All-files report: {final_report}")
            result.summary_lines.append(f"All files: {len(report_items)} source reports collected in {final_report}")

        return result

    def _run(self, options: OrchestratorOptions, refine_from_report: bool) -> OrchestratorResult:
        result = OrchestratorResult()
        project_sources, include_dirs, defines, compiler = self._resolve_project_inputs(options)
        out_dir = source_scoped_output_dir(options.output_dir.expanduser(), project_sources)
        out_dir.mkdir(parents=True, exist_ok=True)
        if out_dir != options.output_dir.expanduser():
            self._log(f"Output folder: {out_dir}")
        preprocessor = CPreprocessor(compiler, include_dirs, defines)
        analyzer = ClangEnhancedAnalyzer(include_dirs, defines, compiler, self._log)
        generator = TestVectorGenerator()
        report_parser = CoverageReportParser()
        feedback = load_coverage_feedback(options.report_file, report_parser, self._log)
        backend = options.backend
        unit_sources = self._project_unit_sources(project_sources, include_dirs) if backend in {"CppUTest", "GoogleTest"} else list(project_sources)
        if len(unit_sources) > len(project_sources):
            self._log(f"Auto-linked {len(unit_sources) - len(project_sources)} project source files for {backend} harness build.")
        report_items: list[dict] = []

        for source in project_sources:
            try:
                analysis_source = source
                if options.preprocess:
                    analysis_source, note = preprocessor.preprocess_file(source, out_dir / "preprocessed")
                    self._log(note)
                functions = analyzer.analyze(
                    analysis_source,
                    include_static_functions=options.include_static_functions,
                    include_main=options.include_main_function,
                )
                if analysis_source != source:
                    analyzer.apply_original_source_context(functions, source)
                code_database = CodeDatabase.from_functions(source, analysis_source, functions, analyzer=analyzer.last_analyzer)
                self._log(f"Code database: {len(code_database.functions)} functions extracted for {source.name}.")
                if options.include_static_functions and backend in {"CppUTest", "GoogleTest"} and any(fn.return_type.strip().startswith("static") for fn in functions):
                    self._log("Static functions were included. CppUTest/GTest may require source-inclusion wrappers or visibility changes to call file-local functions.")
                cases = generator.generate(
                    source.name,
                    functions,
                    options.level,
                    options.coverage,
                    options.iso26262_profile,
                    options.asil_level,
                    options.include_static_functions,
                    code_database,
                )
                active_feedback = merge_generated_coverage_feedback(feedback, functions, cases, options.coverage)
                backend_out_dir = out_dir / backend
                writer = ArtifactWriter(backend_out_dir)
                run_result: RunResult | None = None

                if refine_from_report or options.use_ollama:
                    cases = refine_with_ollama(
                        source,
                        functions,
                        cases,
                        active_feedback,
                        options.coverage,
                        options.level,
                        backend,
                        options.ollama_model,
                        options.target_coverage_percent,
                        max(1, options.max_ai_rounds),
                        self._log,
                        require_report=refine_from_report,
                        code_database=code_database,
                    )
                    active_feedback = merge_generated_coverage_feedback(active_feedback, functions, cases, options.coverage)

                source_paths = writer.write(
                    source,
                    functions,
                    cases,
                    options.level,
                    backend,
                    active_feedback,
                    run_result,
                    include_backend=True,
                    unit_sources=unit_sources or [source],
                    include_dirs=include_dirs,
                    target_coverage_percent=options.target_coverage_percent,
                    code_database=code_database,
                )
                result.generated_paths.extend(source_paths)
                report_path = next(
                    (path for path in reversed(source_paths) if path.suffix.lower() in {".html", ".htm"}),
                    None,
                )
                self._log(f"{backend} output folder: {backend_out_dir}")
                self._log(f"{backend} code database: {backend_out_dir / (source.stem + '_code_database.json')}")
                self._log(f"{backend} cases CSV: {backend_out_dir / (source.stem + '_' + backend_csv_id(backend) + '_cases.csv')}")
                self._log(f"{backend} runner: {backend_harness_path(backend_out_dir, source, backend)}")

                should_run = options.run_after_generate or refine_from_report
                if should_run:
                    run_result = TestBackendRunner().run(backend, backend_out_dir, source)
                    self._log(format_run_result(run_result))
                    if run_result.returncode not in (0, None):
                        result.exit_code = run_result.returncode
                    if run_result.stdout.strip():
                        self._log("Run stdout:")
                        self._log(run_result.stdout[-1200:])
                    if run_result.stderr.strip():
                        self._log("Run stderr:")
                        self._log(run_result.stderr[-1200:])
                    if run_result.returncode not in (0, None) or (run_result.returncode is None and run_result.skipped_reason):
                        result.exit_code = run_result.returncode or 1
                        result.summary_lines.append(f"{source.name}: ERROR {format_run_result(run_result)}")
                        return result
                    active_feedback = collect_backend_feedback(report_parser, backend_out_dir, source, backend, self._log) or active_feedback
                    active_feedback = merge_generated_coverage_feedback(active_feedback, functions, cases, options.coverage)
                    report_path = writer._write_html_report(source, functions, cases, backend, active_feedback, run_result)
                    result.generated_paths.append(report_path)
                    result.generated_paths.append(writer._write_summary(source, functions, cases, active_feedback, run_result))

                result.functions.extend(functions)
                result.cases.extend(cases)
                result.feedback = active_feedback  # Store coverage feedback for GUI display
                report_items.append(
                    {
                        "source": source,
                        "functions": functions,
                        "cases": cases,
                        "feedback": active_feedback,
                        "run_result": run_result,
                        "report_path": report_path,
                    }
                )
                self._log(f"{source.name}: {len(functions)} functions, {len(cases)} test cases, backend {backend}")
                if refine_from_report and run_result is not None:
                    result.summary_lines.append(
                        f"{source.name}: {len(functions)} functions, {len(cases)} cases, {format_run_result(run_result)}, {coverage_log_summary(active_feedback)}"
                    )
                else:
                    result.summary_lines.append(
                        f"{source.name}: {len(functions)} functions, {len(cases)} cases, {backend}, {coverage_log_summary(active_feedback)}"
                    )
            except Exception as exc:
                self._log(f"ERROR {source}: {exc}")
                result.summary_lines.append(f"{source.name}: ERROR {exc}")
                result.exit_code = 1
                return result

        if len(report_items) > 1:
            final_report = ArtifactWriter(out_dir / backend)._write_multi_file_report(backend, report_items)
            result.generated_paths.append(final_report)
            self._log(f"All-files report: {final_report}")
            result.summary_lines.append(f"All files: {len(report_items)} source reports collected in {final_report}")

        return result

    def _resolve_project_inputs(self, options: OrchestratorOptions) -> tuple[list[Path], list[str], list[str], str]:
        sources = list(options.sources)
        include_dirs = list(options.include_dirs)
        defines = list(options.defines)
        compiler = options.compiler.strip() or DEFAULT_COMPILER

        if options.compile_commands:
            try:
                database = CompileCommandsDatabase.load(options.compile_commands)
                compile_sources = database.source_files()
                sources.extend(compile_sources)
                include_dirs.extend(database.include_dirs())
                defines.extend(database.defines())
                database_compiler = database.compiler()
                if database_compiler and not options.compiler.strip():
                    compiler = database_compiler
                self._log(
                    f"Loaded compile_commands.json: {len(compile_sources)} source files, "
                    f"{len(database.include_dirs())} include dirs, {len(database.defines())} defines."
                )
            except Exception as exc:
                self._log(f"compile_commands.json could not be loaded: {exc}")

        sources = self._unique_existing_sources(sources)
        include_dirs = self._unique_strings(include_dirs)
        defines = self._unique_strings(defines)
        return sources, include_dirs, defines, compiler

    @staticmethod
    def _unique_existing_sources(sources: list[Path]) -> list[Path]:
        result: list[Path] = []
        seen: set[Path] = set()
        for source in sources:
            path = source.expanduser()
            if path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.exists() or resolved in seen:
                continue
            seen.add(resolved)
            result.append(resolved)
        return result

    @staticmethod
    def _unique_strings(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            clean = str(value).strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            result.append(clean)
        return result

    @staticmethod
    def _project_unit_sources(sources: list[Path], include_dirs: list[str]) -> list[Path]:
        discovered: list[Path] = []
        seen: set[Path] = set()

        def add(path: Path) -> None:
            if path.suffix.lower() not in C_SOURCE_EXTENSIONS or not path.exists():
                return
            resolved = path.resolve()
            if resolved in seen:
                return
            seen.add(resolved)
            discovered.append(path)

        for source in sources:
            add(source)

        project_roots: list[Path] = []
        for include_dir in include_dirs:
            inc = Path(include_dir).expanduser()
            if inc.exists() and inc.is_dir() and inc.name.lower() in {"include", "inc", "includes"}:
                project_roots.append(inc.parent)

        for root in project_roots:
            src_dir = root / "src"
            if not src_dir.exists():
                continue
            for extension in C_SOURCE_EXTENSIONS:
                for candidate in src_dir.rglob(f"*{extension}"):
                    if any(part.lower() in {"generated", "build", "out"} for part in candidate.parts):
                        continue
                    add(candidate)

        return discovered

    @staticmethod
    def _load_existing_analysis(backend_out_dir: Path, source: Path) -> tuple[list[FunctionInfo], list[TestCase]]:
        analysis_path = backend_out_dir / f"{source.stem}_analysis.json"
        if not analysis_path.exists():
            raise FileNotFoundError(f"Existing analysis artifact not found: {analysis_path}. Run Analyze & Generate first.")
        payload = json.loads(analysis_path.read_text(encoding="utf-8"))
        functions = [Orchestrator._function_from_payload(item) for item in payload.get("functions", [])]
        cases = [Orchestrator._case_from_payload(item) for item in payload.get("test_cases", [])]
        if not cases:
            raise ValueError(f"No saved test cases were found in {analysis_path}.")
        return functions, cases

    @staticmethod
    def _function_from_payload(item: dict) -> FunctionInfo:
        return FunctionInfo(
            name=str(item.get("name") or ""),
            return_type=str(item.get("return_type") or "void"),
            parameters=[Parameter(**param) for param in item.get("parameters", [])],
            start_line=int(item.get("start_line") or 0),
            end_line=int(item.get("end_line") or 0),
            body=str(item.get("body") or item.get("body_excerpt") or ""),
            conditions=[Condition(**condition) for condition in item.get("conditions", [])],
        )

    @staticmethod
    def _case_from_payload(item: dict) -> TestCase:
        fields = TestCase.__dataclass_fields__
        values = {name: item.get(name) for name in fields if item.get(name) is not None}
        values["test_id"] = str(values.get("test_id") or "TC_RELOADED")
        values["unit"] = str(values.get("unit") or "")
        values["function"] = str(values.get("function") or "")
        values["coverage"] = str(values.get("coverage") or "MC/DC")
        values["level"] = str(values.get("level") or "UT")
        values["values"] = values.get("values") if isinstance(values.get("values"), dict) else {}
        values["expected"] = str(values.get("expected") or "Review and replace with concrete expected output/test oracle")
        values["rationale"] = str(values.get("rationale") or "Reloaded from existing Testing Mate analysis artifact")
        return TestCase(**values)


def main() -> None:
    app = TestingMateApp()
    app.mainloop()


if __name__ == "__main__":
    main()
