from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from testing_mate.code_database import CodeDatabase
from testing_mate.constants import C_SOURCE_EXTENSIONS, HEADER_SOURCE_EXTENSIONS, SCALAR_TOKENS
from testing_mate.mcdc_engine import MCDCCoverageCollector
from testing_mate.models import FunctionInfo, Parameter, RunResult, TestCase
from testing_mate.boolean_expression import ConditionEvaluator
from testing_mate.stub_engine import StubHarnessEngine, StubSymbol
from testing_mate.toolchain import local_mingw_bin, msys_bash_path, toolchain_env, windows_to_msys_path
from testing_mate.vector_generator import TestVectorGenerator


def backend_harness_path(out_dir: Path | str, source: Path | str, backend: str) -> Path:
    out_dir = out_dir if isinstance(out_dir, Path) else Path(str(out_dir))
    source = source if isinstance(source, Path) else Path(str(source))
    if backend == "CppUTest":
        return out_dir / f"{source.stem}_cpputest" / "run_cpputest.sh"
    if backend == "GoogleTest":
        return out_dir / f"{source.stem}_gtest" / "run_gtest.sh"
    return out_dir


def format_run_result(result: RunResult) -> str:
    if result.returncode is None:
        return f"{result.backend} run skipped: {result.skipped_reason}"
    status = "passed" if result.returncode == 0 else f"failed with code {result.returncode}"
    return f"{result.backend} run {status}: {result.command}"


class TestOracleFallback:
    """Patch generated expected fixtures from first-run assertion evidence."""

    @classmethod
    def apply(cls, backend: str, out_dir: Path | str, source: Path | str, run_result: RunResult) -> tuple[int, list[str]]:
        out_dir = out_dir if isinstance(out_dir, Path) else Path(str(out_dir))
        source = source if isinstance(source, Path) else Path(str(source))
        if run_result.returncode is None:
            return 0, []
        test_source = cls._test_source_path(backend, out_dir, source)
        if not test_source.exists():
            return 0, [f"Oracle fallback skipped: test source not found: {test_source}"]
        try:
            text = test_source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return 0, [f"Oracle fallback skipped: cannot read {test_source}: {exc}"]

        mismatches = cls.expected_actual_mismatches(run_result, test_source)
        if not mismatches:
            return 0, ["Oracle fallback found no parseable expected/actual equality failures."]

        type_map = cls._fixture_type_map(text)
        return_types = cls._extract_function_return_types(text)
        updated = 0
        notes: list[str] = []
        for mismatch in mismatches:
            literal = cls._literal_from_observed_value(mismatch.get("actual_value", ""))
            expected = mismatch.get("expected", "")
            test_name = mismatch.get("test", "")
            if not literal or not expected or not test_name:
                continue
            next_text, changed = cls._patch_expected_assignment(text, test_name, expected, literal, type_map, return_types)
            if not changed:
                notes.append(f"Oracle fallback could not patch {test_name}: expected fixture {expected} was not assigned.")
                continue
            text = next_text
            updated += 1
            notes.append(f"Oracle fallback updated {test_name}: {expected} = {literal}.")

        if not updated:
            return 0, notes or ["Oracle fallback did not change any testcase."]
        try:
            test_source.write_text(text, encoding="utf-8")
        except OSError as exc:
            return 0, [f"Oracle fallback could not write {test_source}: {exc}"]
        return updated, notes

    @classmethod
    def expected_actual_mismatches(cls, run_result: RunResult, test_source: Path | str | None = None) -> list[dict[str, str]]:
        text = cls.backend_execution_text(run_result)
        if not text.strip():
            return []
        source_text = ""
        if test_source is not None:
            try:
                source_text = Path(str(test_source)).read_text(encoding="utf-8", errors="replace")
            except OSError:
                source_text = ""
        test_source_name = Path(str(test_source)).name if test_source is not None else ""
        failures = cls.failed_test_names_from_text(text)
        items = cls._gtest_mismatches(text, source_text, test_source_name) + cls._cpputest_mismatches(text, source_text, test_source_name)
        if failures:
            items = [item for item in items if cls._test_matches_failure_set(item.get("test", ""), failures)]
        return items

    @staticmethod
    def backend_execution_text(run_result: RunResult) -> str:
        """Return current backend stdout/stderr without Testing Mate retry notes.

        Retry notes are appended into stdout for report visibility.  In long or
        nested runs those notes may appear before another real CTest/GTest block,
        so do not simply truncate at the first retry marker.  Remove only the
        note block, then select the latest real execution block.
        """
        text = "\n".join(part for part in (run_result.stdout, run_result.stderr) if part)
        if not text:
            return ""
        text = html.unescape(text)
        text = TestOracleFallback._strip_testing_mate_retry_notes(text)
        starts = [m.start() for m in re.finditer(r"(?m)^(?:Start testing:|Running main\(\) from|\[=+\] Running )", text)]
        if len(starts) > 1:
            # Prefer the latest backend execution, but avoid selecting a short
            # inner banner when a later CTest LastTest.log block is appended.
            ctest_starts = [m.start() for m in re.finditer(r"(?m)^Start testing:", text)]
            text = text[(ctest_starts[-1] if ctest_starts else starts[-1]):]
        return text

    @staticmethod
    def _strip_testing_mate_retry_notes(text: str) -> str:
        note_start = re.compile(r"^===\s+Testing Mate Fallback Retry\s+\d+/\d+\s+===\s*$")
        execution_start = re.compile(r"^(?:Start testing:|Running main\(\) from|\[=+\] Running )")
        kept: list[str] = []
        skipping = False
        for line in str(text or "").splitlines():
            if note_start.match(line.strip()):
                skipping = True
                continue
            if skipping and execution_start.search(line):
                skipping = False
            if skipping:
                continue
            kept.append(line)
        return "\n".join(kept)

    @staticmethod
    def _next_failed_test_name(lines: list[str], start_index: int, window: int = 40) -> str:
        tail = "\n".join(lines[start_index:min(len(lines), start_index + window)])
        failed_match = re.search(r"(?m)^\[\s*FAILED\s*\]\s+([^\s(]+)", tail)
        return failed_match.group(1).strip().rsplit(".", 1)[-1] if failed_match else ""

    @staticmethod
    def failed_test_names_from_text(text: str) -> set[str]:
        failed: set[str] = set()
        text = str(text or "")
        for match in re.finditer(r"(?m)^\[\s*FAILED\s*\]\s+([^\s(]+)", text):
            name = match.group(1).strip()
            if not name or name.isdigit():
                continue
            failed.add(name)
            failed.add(name.rsplit(".", 1)[-1])
        for match in re.finditer(r"Failure\s+in\s+TEST\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", text, re.I):
            group = match.group(1).strip()
            name = match.group(2).strip()
            if not name or name.isdigit():
                continue
            failed.add(name)
            if group:
                failed.add(f"{group}.{name}")
        return failed

    @staticmethod
    def _test_matches_failure_set(test_name: str, failures: set[str]) -> bool:
        if not failures:
            return True
        clean = str(test_name or "").strip()
        return bool(clean and (clean in failures or clean.rsplit(".", 1)[-1] in failures))

    @staticmethod
    def failure_summaries(run_result: RunResult, limit: int = 20) -> list[str]:
        mismatches = TestOracleFallback.expected_actual_mismatches(run_result)
        summaries = []
        for item in mismatches[:limit]:
            summaries.append(
                "Backend failure: "
                f"{item.get('test', 'unknown test')} expected {item.get('expected', '?')} "
                f"but observed {item.get('actual', '?')} = {item.get('actual_value', '?')}"
            )
        if summaries:
            return summaries
        text = TestOracleFallback.backend_execution_text(run_result)
        for line in text.splitlines():
            clean = " ".join(line.strip().split())
            if clean and any(token in clean.lower() for token in ("failed", "failure", "error", "expected", "actual")):
                summaries.append(clean[:500])
            if len(summaries) >= limit:
                break
        return summaries

    @staticmethod
    def _test_source_path(backend: str, out_dir: Path | str, source: Path | str) -> Path:
        out_dir = out_dir if isinstance(out_dir, Path) else Path(str(out_dir))
        source = source if isinstance(source, Path) else Path(str(source))
        if backend == "CppUTest":
            return out_dir / f"{source.stem}_cpputest" / f"{source.stem}_cpputest_test.cpp"
        return out_dir / f"{source.stem}_gtest" / f"{source.stem}_gtest_test.cpp"

    @staticmethod
    def _gtest_mismatches(text: str, source_text: str = "", test_source_name: str = "") -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        current_test = ""
        pending_failure_line: int | None = None
        lines = text.splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            run_match = re.search(r"^\[\s*RUN\s*\]\s+(.+)$", line)
            if run_match:
                current_test = run_match.group(1).strip().rsplit(".", 1)[-1]
                pending_failure_line = None
            failure_match = TestOracleFallback._output_cpp_line_match(line)
            if failure_match and re.search(r"(?i):\s+Failure", line):
                failure_file, failure_line = failure_match
                pending_failure_line = failure_line if TestOracleFallback._is_same_output_source(failure_file, test_source_name) else None
            if "Expected equality of these values:" in line:
                parsed = TestOracleFallback._parse_gtest_equality_block(lines[index + 1 : index + 10])
                if parsed:
                    mapped_test = (
                        current_test
                        or TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                        or TestOracleFallback._next_failed_test_name(lines, index + 1)
                    )
                    parsed["test"] = mapped_test
                    if pending_failure_line is not None:
                        parsed["line"] = str(pending_failure_line)
                    result.append(parsed)
            else:
                # Tail-truncated output can start inside a GTest equality block,
                # e.g. `tual_return` followed by `Which is: '\x1' (1)`.  When
                # that happens, infer expected_return/actual_return and map to
                # the following [ FAILED ] line so the case can still be put back.
                clean = line.strip()
                if (clean.endswith("actual_return") or clean.endswith("tual_return")) and "Expected equality of these values:" not in "\n".join(lines[max(0, index - 4):index]):
                    if index + 1 < len(lines):
                        value_match = re.search(r"Which is:\s*(.+)$", lines[index + 1].strip())
                        mapped_test = (
                            current_test
                            or TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                            or TestOracleFallback._next_failed_test_name(lines, index + 2)
                        )
                        if value_match and mapped_test:
                            result.append({
                                "test": mapped_test,
                                "expected": "expected_return",
                                "expected_value": "",
                                "actual": "actual_return",
                                "actual_value": value_match.group(1).strip(),
                                "line": str(pending_failure_line or ""),
                            })
            index += 1
        return result

    @staticmethod
    def _parse_gtest_equality_block(lines: list[str]) -> dict[str, str] | None:
        expected = actual = expected_value = actual_value = ""
        index = 0
        while index < len(lines):
            name = lines[index].strip()
            if not name or name.startswith("Which is:"):
                index += 1
                continue
            value = ""
            if index + 1 < len(lines):
                value_match = re.search(r"Which is:\s*(.+)$", lines[index + 1].strip())
                if value_match:
                    value = value_match.group(1).strip()
            if not expected:
                expected, expected_value = name, value
            else:
                actual, actual_value = name, value
                break
            index += 1
        if not expected or not actual or not actual_value:
            return None
        return {"expected": expected, "expected_value": expected_value, "actual": actual, "actual_value": actual_value}

    @staticmethod
    def _cpputest_mismatches(text: str, source_text: str = "", test_source_name: str = "") -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        current_test = ""
        pending_failure_line: int | None = None
        pending_expected = ""
        lines = text.splitlines()
        for index, line in enumerate(lines):
            test_match = re.search(r"TEST\(([^,]+),\s*([^)]+)\)", line)
            if test_match:
                current_test = test_match.group(2).strip()
            failure_test_match = re.search(r"Failure\s+in\s+TEST\(([^,]+),\s*([^)]+)\)", line, re.I)
            if failure_test_match:
                current_test = failure_test_match.group(2).strip()
            line_match = TestOracleFallback._output_cpp_line_match(line)
            if line_match:
                failure_file, failure_line = line_match
                if TestOracleFallback._is_same_output_source(failure_file, test_source_name):
                    pending_failure_line = failure_line
                    mapped = TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                    if mapped:
                        current_test = mapped
                else:
                    # Failures from generated stubs/TearDown helpers still belong to the active test.
                    pending_failure_line = None
            one_line = re.search(r"expected\s*<([^>]+)>.*(?:but was|actual)\s*<([^>]+)>", line, re.I)
            if one_line:
                result.append({
                    "test": current_test or TestOracleFallback._test_name_at_line(source_text, pending_failure_line),
                    "expected": "expected_return",
                    "expected_value": one_line.group(1).strip(),
                    "actual": "actual_return",
                    "actual_value": one_line.group(2).strip(),
                    "line": str(pending_failure_line or ""),
                })
                pending_expected = ""
                continue
            expected_match = re.search(r"expected\s*<([^>]+)>", line, re.I)
            if expected_match:
                pending_expected = expected_match.group(1).strip()
                continue
            actual_match = re.search(r"(?:but was|actual)\s*<([^>]+)>", line, re.I)
            if pending_expected and actual_match:
                result.append({
                    "test": current_test or TestOracleFallback._test_name_at_line(source_text, pending_failure_line),
                    "expected": "expected_return",
                    "expected_value": pending_expected,
                    "actual": "actual_return",
                    "actual_value": actual_match.group(1).strip(),
                    "line": str(pending_failure_line or ""),
                })
                pending_expected = ""
        return result

    @staticmethod
    def _output_cpp_line_match(line: str) -> tuple[str, int] | None:
        match = re.search(r"(?i)(?P<file>(?:[A-Za-z]:)?[^:\n]*?\.cpp):(?P<line>\d+):", line)
        if not match:
            return None
        return match.group("file"), int(match.group("line"))

    @staticmethod
    def _is_same_output_source(output_file: str, test_source_name: str) -> bool:
        if not test_source_name:
            return True
        normalized = str(output_file or "").replace("\\", "/").rsplit("/", 1)[-1]
        return normalized == test_source_name

    @staticmethod
    def _fixture_type_map(text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for match in re.finditer(r"(?m)^\s*(?P<type>[A-Za-z_]\w*(?:\s+[*A-Za-z_]\w*)*)\s+(?P<name>[A-Za-z_]\w*)\s*;", text):
            result[match.group("name")] = " ".join(match.group("type").split())
        return result

    @staticmethod
    def _extract_function_return_types(text: str) -> dict[str, str]:
        """Extract function return types from test harness.
        
        For each TEST(Fixture, TestName), find actual_return type declaration
        and map it from test_name -> return_type_string.
        
        This fallback allows oracle patching to use the correct return type
        instead of assuming Std_ReturnType when patching expected_return values.
        """
        result: dict[str, str] = {}
        
        # Pattern: TEST(..., TestName) { ... actual_return_type actual_return = ...; ... }
        for test_match in re.finditer(r"TEST(?:_F)?\s*\([^,]+,\s*([A-Za-z_]\w*)\)\s*\{", text):
            test_name = test_match.group(1)
            test_start = test_match.start()
            
            # Find test block boundaries
            depth = 0
            test_end = len(text)
            for idx in range(test_match.end(), len(text)):
                if text[idx] == '{':
                    depth += 1
                elif text[idx] == '}':
                    depth -= 1
                    if depth == -1:
                        test_end = idx
                        break
            
            test_block = text[test_start:test_end]
            
            # Extract actual_return type - improved to handle:
            # - Simple types: uint8_t
            # - Namespace-qualified: SafetySwc::MyType
            # - Template types: std::vector<int>
            # - Pointers and references: uint8_t*, const uint8_t*
            type_match = re.search(
                r"((?:const\s+)?(?:[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*|[A-Za-z_]\w*\s*<[^>]*>)(?:\s*\*+)?(?:\s*[&]+)?)"
                r"\s+actual_return\b",
                test_block
            )
            if type_match:
                return_type = " ".join(type_match.group(1).split())
                result[test_name] = return_type
        
        return result

    @staticmethod
    def _patch_expected_assignment(
        text: str,
        test_name: str,
        expected: str,
        literal: str,
        type_map: dict[str, str],
        return_types: dict[str, str] | None = None,
    ) -> tuple[str, bool]:
        span = TestOracleFallback._test_block_span(text, test_name)
        if span is None:
            return text, False
        start, end = span
        block = text[start:end]
        expected_root = expected.split(".", 1)[0]
        ctype = type_map.get(expected_root, "")
        
        # Fallback: if expected_root is "expected_return" and not in type_map,
        # try to extract the actual function return type from test harness
        if not ctype and expected_root == "expected_return" and return_types:
            ctype = return_types.get(test_name, "")
        
        typed = TestBackendGenerator._typed_value(ctype, literal) if ctype else literal
        if re.fullmatch(r"[A-Za-z_]\w*", expected):
            # Generated suites usually assign fixture members as
            # `expected_return = ...;`, but focused/synthetic layer suites can
            # also declare a local such as `uint8 expected_return = ...;`.
            # Patch both forms so immediate retry uses the observed oracle.
            assign_pattern = rf"(?m)^(\s*(?:(?:[A-Za-z_]\w*(?:::[A-Za-z_]\w*)?|[A-Za-z_]\w*<[^;=]*>|[*&])\s+)*{re.escape(expected)}\s*=\s*)([^;]+)(;)"
        else:
            assign_pattern = rf"(?m)^(\s*{re.escape(expected)}\s*=\s*)([^;]+)(;)"
        pattern = re.compile(assign_pattern)
        if pattern.search(block):
            block = pattern.sub(rf"\g<1>{typed}\3", block, count=1)
            return text[:start] + block + text[end:], True
        marker = re.search(r"(?m)^(\s*/\*\s*ACTIONS\s*\*/)", block)
        if marker:
            insert_at = marker.start()
            line = f"    {expected} = {typed};\n"
            block = block[:insert_at] + line + block[insert_at:]
            return text[:start] + block + text[end:], True
        return text, False

    @staticmethod
    def _test_block_span(text: str, test_name: str) -> tuple[int, int] | None:
        pattern = re.compile(rf"\bTEST(?:_F)?\s*\(\s*[A-Za-z_]\w*\s*,\s*{re.escape(test_name)}\s*\)\s*\{{")
        match = pattern.search(text)
        if not match:
            return None
        open_brace = text.find("{", match.start())
        if open_brace < 0:
            return None
        depth = 0
        for index in range(open_brace, len(text)):
            ch = text[index]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return match.start(), index + 1
        return None

    @staticmethod
    def _test_name_at_line(text: str, line_no: int | None) -> str:
        if not text or line_no is None or line_no <= 0:
            return ""
        candidates: list[tuple[int, int, str]] = []
        pattern = re.compile(r"\bTEST(?:_F)?\s*\(\s*[A-Za-z_]\w*\s*,\s*([A-Za-z_]\w*)\s*\)\s*\{")
        for match in pattern.finditer(text):
            open_brace = text.find("{", match.start())
            if open_brace < 0:
                continue
            depth = 0
            end_index = len(text)
            for index in range(open_brace, len(text)):
                ch = text[index]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end_index = index + 1
                        break
            start_line = text[: match.start()].count("\n") + 1
            end_line = text[:end_index].count("\n") + 1
            candidates.append((start_line, end_line, match.group(1)))
        for start_line, end_line, name in candidates:
            if start_line <= line_no <= end_line:
                return name
        return ""

    @staticmethod
    def _literal_from_observed_value(value: str) -> str:
        clean = html.unescape(value.strip())
        paren_number = re.search(r"\((-?\d+(?:\.\d+)?)\)", clean)
        if paren_number:
            return paren_number.group(1)
        if re.fullmatch(r"-?\d+(?:\.\d+)?", clean):
            return clean
        if clean.lower() in {"true", "false"}:
            return clean.lower()
        char_match = re.search(r"'((?:\\.|[^'])*)'", clean)
        if char_match:
            raw = char_match.group(1)
            if raw == r"\0":
                return "0"
            hex_match = re.fullmatch(r"\\x([0-9A-Fa-f]+)", raw)
            if hex_match:
                return str(int(hex_match.group(1), 16))
            if len(raw) == 1:
                return str(ord(raw))
        return ""


class TestCaseRunReconciler:
    """Persist backend-discovered oracles and mock-call expectations into TestCase values.

    The generated C/C++ file can be patched for the immediate retry, but later
    coverage layers call ArtifactWriter.write(...) again.  Without updating the
    TestCase objects, those regenerated files lose the observed expected_return
    and mock expectation fixes.  This reconciler stores the same evidence in the
    testcase model so every next layer keeps it.
    """

    EXPECTED_CALL_KEYS = {"__expected_calls", "__expect_calls", "expected_calls", "expect_calls"}
    NON_MOCK_CALL_NAMES = {
        "Unexpected",
        "Uninteresting",
        "Function",
        "FunctionCall",
        "Mock",
        "GMOCK",
        "WARNING",
        "NOTE",
        "Expected",
        "Actual",
        "returning",
        "directly",
    }

    @classmethod
    def _is_valid_mock_call_name(cls, name: object, stubbed_calls: set[str] | None = None) -> bool:
        clean = str(name or "").strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", clean):
            return False
        if clean in cls.NON_MOCK_CALL_NAMES:
            return False
        if stubbed_calls is not None and clean not in stubbed_calls:
            return False
        return True


    @classmethod
    def apply(
        cls,
        backend: str,
        out_dir: Path | str,
        source: Path | str,
        run_result: RunResult,
        cases: list[TestCase] | None,
    ) -> tuple[int, list[str]]:
        if not cases or run_result.returncode is None:
            return 0, []
        test_source = TestOracleFallback._test_source_path(backend, out_dir, source)
        source_text = ""
        if test_source.exists():
            try:
                source_text = test_source.read_text(encoding="utf-8", errors="replace")
            except OSError:
                source_text = ""

        by_name = cls._case_map(cases)
        changed = 0
        notes: list[str] = []

        for mismatch in TestOracleFallback.expected_actual_mismatches(run_result, test_source):
            test_name = str(mismatch.get("test") or "").strip()
            case = by_name.get(test_name)
            if case is None:
                continue
            expected = str(mismatch.get("expected") or "").strip()
            value_key = cls._expected_value_key(expected)
            if not value_key:
                continue
            literal = TestOracleFallback._literal_from_observed_value(str(mismatch.get("actual_value") or ""))
            if not literal:
                continue
            values = dict(case.values or {})
            if str(values.get(value_key, "")).strip() == literal:
                continue
            values[value_key] = literal
            case.values = values
            case.expected = cls._merge_expected_note(case.expected, f"{value_key}={literal}")
            case.oracle_status = "auto_observed"
            case.execution_status = "reconciled"
            case.trace_tags = cls._merge_trace_tag(case.trace_tags, "oracle-reconciled")
            changed += 1
            notes.append(f"Run reconciliation stored {case.test_id}: {value_key} = {literal}.")

        for failure in TestMockFallback.mock_failures(run_result, test_source):
            test_name = str(failure.get("test") or "").strip()
            function = str(failure.get("function") or "").strip()
            action = str(failure.get("action") or "").strip()
            if not test_name or not function or action not in {"add", "remove"}:
                continue
            if not cls._is_valid_mock_call_name(function):
                continue
            case = by_name.get(test_name)
            if case is None:
                continue
            values = dict(case.values or {})
            calls = cls._expected_calls_for_case(values, source_text, test_name)
            before = list(calls)
            if action == "add":
                if function not in calls:
                    calls.append(function)
            else:
                calls = [item for item in calls if item != function]
            if calls == before and "__expected_calls" in values:
                continue
            # Even an empty explicit list is meaningful: it disables automatic
            # call inference for a path where the backend proved the call is not reached.
            values["__expected_calls"] = ",".join(calls)
            case.values = values
            case.oracle_status = "auto_observed"
            case.execution_status = "reconciled"
            case.trace_tags = cls._merge_trace_tag(case.trace_tags, "mock-reconciled")
            changed += 1
            verb = "added" if action == "add" else "removed"
            notes.append(f"Run reconciliation {verb} mock expectation in {case.test_id}: {function}.")

        for observed in TestMockFallback.observed_mock_calls(run_result, test_source):
            test_name = str(observed.get("test") or "").strip()
            function = str(observed.get("function") or "").strip()
            args = observed.get("args") or []
            if not test_name or not function:
                continue
            if not cls._is_valid_mock_call_name(function):
                continue
            case = by_name.get(test_name)
            if case is None:
                continue
            values = dict(case.values or {})
            calls = cls._expected_calls_for_case(values, source_text, test_name)
            modified = False
            if function not in calls:
                calls.append(function)
                values["__expected_calls"] = ",".join(calls)
                modified = True
            for arg_index, raw_arg in enumerate(args):
                literal = TestMockFallback._literal_from_mock_arg(str(raw_arg))
                if not literal:
                    continue
                key = f"__expected_call_arg.{function}.{arg_index}"
                if str(values.get(key, "")).strip() != literal:
                    values[key] = literal
                    modified = True
            if not modified:
                continue
            case.values = values
            case.oracle_status = "auto_observed"
            case.execution_status = "reconciled"
            case.trace_tags = cls._merge_trace_tag(case.trace_tags, "mock-param-reconciled")
            changed += 1
            arg_note = ", ".join(
                f"arg{idx}={TestMockFallback._literal_from_mock_arg(str(raw)) or str(raw).strip()}"
                for idx, raw in enumerate(args)
            )
            notes.append(f"Run reconciliation stored mock parameters in {case.test_id}: {function}({arg_note}).")

        return changed, notes

    @staticmethod
    def _case_map(cases: list[TestCase]) -> dict[str, TestCase]:
        result: dict[str, TestCase] = {}
        for case in cases:
            safe = re.sub(r"[^A-Za-z0-9_]", "_", str(case.test_id or ""))
            if safe:
                result[safe] = case
            if case.test_id:
                result[str(case.test_id)] = case
        return result

    @staticmethod
    def _expected_value_key(expected: str) -> str:
        root = expected.split(".", 1)[0].strip()
        if root == "expected_return":
            return "__expected_return"
        # Whole-struct or field output/global names are intentionally left for
        # direct source patching because fixture names may not round-trip back to
        # the original parameter/global name safely.
        return ""

    @classmethod
    def _expected_calls_for_case(cls, values: dict[str, str], source_text: str, test_name: str) -> list[str]:
        explicit = cls._parse_call_list(values.get("__expected_calls", ""))
        if "__expected_calls" in values:
            return explicit
        for key in cls.EXPECTED_CALL_KEYS:
            if key in values:
                return cls._parse_call_list(values.get(key, ""))
        for key in sorted(values):
            if key.startswith("__expected_call."):
                call = key.split(".", 1)[1].strip()
                if call and call not in explicit:
                    explicit.append(call)
            elif key.startswith("__stub_return."):
                call = key.split(".", 1)[1].strip()
                if call and call not in explicit:
                    explicit.append(call)
        if explicit:
            return explicit
        return cls._expected_calls_from_generated_block(source_text, test_name)

    @staticmethod
    def _parse_call_list(raw: object) -> list[str]:
        result: list[str] = []
        for item in re.split(r"[,;\s]+", str(raw or "")):
            clean = item.strip()
            if TestCaseRunReconciler._is_valid_mock_call_name(clean) and clean not in result:
                result.append(clean)
        return result

    @staticmethod
    def _expected_calls_from_generated_block(source_text: str, test_name: str) -> list[str]:
        span = TestOracleFallback._test_block_span(source_text, test_name)
        if span is None:
            return []
        block = source_text[span[0]:span[1]]
        result: list[str] = []
        for match in re.finditer(r'mock\(\)\.expectOneCall\(\s*"([A-Za-z_]\w*)"', block):
            name = match.group(1)
            if TestCaseRunReconciler._is_valid_mock_call_name(name) and name not in result:
                result.append(name)
        for match in re.finditer(r"EXPECT_CALL\(\s*TestingMate_Mock\(\)\s*,\s*([A-Za-z_]\w*)\s*\(", block):
            name = match.group(1)
            if TestCaseRunReconciler._is_valid_mock_call_name(name) and name not in result:
                result.append(name)
        return result

    @staticmethod
    def _merge_expected_note(current: str, note: str) -> str:
        clean = str(current or "").strip()
        if note in clean:
            return clean
        return f"{clean}; observed {note}".strip("; ") if clean else f"observed {note}"

    @staticmethod
    def _merge_trace_tag(current: str, tag: str) -> str:
        tags = [item.strip() for item in re.split(r"[,;\s]+", str(current or "")) if item.strip()]
        if tag not in tags:
            tags.append(tag)
        return ",".join(tags)


class TestMockFallback:
    """Patch generated mock expectations from backend mock-failure evidence."""

    NON_MOCK_CALL_NAMES = {
        "Unexpected",
        "Uninteresting",
        "Function",
        "FunctionCall",
        "Mock",
        "GMOCK",
        "WARNING",
        "NOTE",
        "Expected",
        "Actual",
        "returning",
        "directly",
    }

    @classmethod
    def _is_valid_mock_call_name(cls, name: object) -> bool:
        clean = str(name or "").strip()
        return bool(re.fullmatch(r"[A-Za-z_]\w*", clean)) and clean not in cls.NON_MOCK_CALL_NAMES

    @classmethod
    def apply(cls, backend: str, out_dir: Path | str, source: Path | str, run_result: RunResult) -> tuple[int, list[str]]:
        out_dir = out_dir if isinstance(out_dir, Path) else Path(str(out_dir))
        source = source if isinstance(source, Path) else Path(str(source))
        if run_result.returncode is None:
            return 0, []
        test_source = TestOracleFallback._test_source_path(backend, out_dir, source)
        if not test_source.exists():
            return 0, [f"Mock fallback skipped: test source not found: {test_source}"]
        try:
            text = test_source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return 0, [f"Mock fallback skipped: cannot read {test_source}: {exc}"]

        failures = cls.mock_failures(run_result, test_source) + cls.observed_mock_calls(run_result, test_source)
        if not failures:
            return 0, ["Mock fallback found no parseable mock failures or uninteresting mock-call warnings."]

        mock_info = cls._mock_info_map(test_source)
        updated = 0
        notes: list[str] = []
        for failure in failures:
            test_name = failure.get("test", "")
            function = failure.get("function", "")
            action = failure.get("action", "")
            if not test_name or not function or action not in {"add", "remove"}:
                continue
            if not cls._is_valid_mock_call_name(function):
                continue
            if action == "remove":
                text, changed = cls._remove_mock_expectation(text, test_name, function)
                if changed:
                    updated += 1
                    notes.append(f"Mock fallback removed redundant expectation: {test_name} -> {function}.")
                else:
                    notes.append(f"Mock fallback could not remove expectation: {test_name} -> {function}.")
            else:
                text, changed = cls._add_mock_expectation(text, backend, test_name, function, mock_info, failure.get("args") or [])
                if changed:
                    updated += 1
                    notes.append(f"Mock fallback added missing expectation: {test_name} -> {function}.")
                else:
                    notes.append(f"Mock fallback could not add expectation: {test_name} -> {function}.")

        if not updated:
            return 0, notes or ["Mock fallback did not change any testcase."]
        try:
            test_source.write_text(text, encoding="utf-8")
        except OSError as exc:
            return 0, [f"Mock fallback could not write {test_source}: {exc}"]
        return updated, notes

    @staticmethod
    def mock_failures(run_result: RunResult, test_source: Path | str | None = None) -> list[dict[str, str]]:
        text = TestOracleFallback.backend_execution_text(run_result)
        if not text.strip():
            return []
        source_text = ""
        if test_source is not None:
            try:
                source_text = Path(str(test_source)).read_text(encoding="utf-8", errors="replace")
            except OSError:
                source_text = ""
        test_source_name = Path(str(test_source)).name if test_source is not None else ""
        failures: list[dict[str, str]] = []
        current_test = ""
        pending_failure_line: int | None = None
        cpputest_unfulfilled = False
        unexpected_context = False
        failed_tests = TestOracleFallback.failed_test_names_from_text(text)
        lines = text.splitlines()
        for index, line in enumerate(lines):
            test_match = re.search(r"\bTEST(?:_F)?\s*\(\s*[^,]+,\s*([^)]+)\)", line)
            if test_match:
                current_test = test_match.group(1).strip()
            failure_test_match = re.search(r"Failure\s+in\s+TEST\(([^,]+),\s*([^)]+)\)", line, re.I)
            if failure_test_match:
                current_test = failure_test_match.group(2).strip()
            run_match = re.search(r"^\[\s*RUN\s*\]\s+(.+)$", line)
            if run_match:
                current_test = run_match.group(1).strip().rsplit(".", 1)[-1]
            line_match = TestOracleFallback._output_cpp_line_match(line)
            if line_match:
                failure_file, failure_line = line_match
                if TestOracleFallback._is_same_output_source(failure_file, test_source_name):
                    pending_failure_line = failure_line
                    mapped = TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                    if mapped:
                        current_test = mapped
                else:
                    # GMock VerifyAndClear may report from TearDown/stubs.cpp; keep the active [ RUN ] testcase.
                    pending_failure_line = None

            # Generated harnesses may assert stub call counters explicitly:
            #   Expected: (TestingMate_Foo_CallCount()) > (0U), actual: 0 vs 0
            # That is semantically the same as an unreachable expected mock call
            # for this path, so remove the expectation from the owning testcase.
            if "TestingMate_" in line and "CallCount" in line and re.search(r"actual:\s*0\s+vs\s+0", line, re.I):
                mapped_test = (
                    current_test
                    or TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                    or TestOracleFallback._next_failed_test_name(lines, index + 1)
                )
                for call_match in re.finditer(r"TestingMate_([A-Za-z_]\w*)_CallCount\s*\(\s*\)", line):
                    function = call_match.group(1)
                    if mapped_test:
                        failures.append({"test": mapped_test, "function": function, "action": "remove"})
                continue

            if "EXPECTED calls that WERE NOT fulfilled" in line:
                cpputest_unfulfilled = True
                unexpected_context = False
                continue
            if "EXPECTED calls that WERE fulfilled" in line:
                cpputest_unfulfilled = False
                continue
            if cpputest_unfulfilled:
                call_match = re.search(r"^\s*([A-Za-z_]\w*)\s*->", line)
                mapped_test = (
                    current_test
                    or TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                    or TestOracleFallback._next_failed_test_name(lines, index + 1)
                )
                if call_match and mapped_test:
                    failures.append({"test": mapped_test, "function": call_match.group(1), "action": "remove"})
                continue

            lower = line.lower()
            unexpected_direct = re.search(
                r"unexpected\s+(?:mock\s+)?(?:function\s+)?call(?:\s+to\s+function)?\s*:?\s*\"?([A-Za-z_]\w*)",
                line,
                re.I,
            )
            mapped_test = (
                current_test
                or TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                or TestOracleFallback._next_failed_test_name(lines, index + 1)
            )
            if unexpected_direct and mapped_test:
                failures.append({"test": mapped_test, "function": unexpected_direct.group(1), "action": "add"})
                continue
            if "unexpected mock function call" in lower or ("mock failure" in lower and "unexpected" in lower):
                unexpected_context = True
            if unexpected_context:
                call_match = re.search(r"(?:Function call:\s*|^\s*)([A-Za-z_]\w*)\s*(?:\(|->|\b)", line)
                mapped_test = (
                    current_test
                    or TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                    or TestOracleFallback._next_failed_test_name(lines, index + 1)
                )
                if call_match and mapped_test and call_match.group(1) not in {"Mock", "Function", "FunctionCall"}:
                    failures.append({"test": mapped_test, "function": call_match.group(1), "action": "add"})
                    unexpected_context = False
                    continue
                if index + 1 < len(lines):
                    next_match = re.search(r"^\s*(?:Function call:\s*)?([A-Za-z_]\w*)\s*(?:\(|->|\b)", lines[index + 1])
                    mapped_test = (
                        current_test
                        or TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                        or TestOracleFallback._next_failed_test_name(lines, index + 2)
                    )
                    if next_match and mapped_test and next_match.group(1) not in {"Mock", "Function", "FunctionCall"}:
                        failures.append({"test": mapped_test, "function": next_match.group(1), "action": "add"})
                        unexpected_context = False
                        continue

            if "actual function call count doesn't match" in lower and "expect_call" in lower:
                for function in TestMockFallback._gmock_expect_call_functions(line):
                    mapped_test = (
                        current_test
                        or TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                        or TestOracleFallback._next_failed_test_name(lines, index + 1)
                    )
                    if mapped_test:
                        failures.append({"test": mapped_test, "function": function, "action": "remove"})

            if "actual: never called" in lower or "unsatisfied and active" in lower:
                window = "\n".join(lines[max(0, index - 12): min(len(lines), index + 3)])
                for function in TestMockFallback._gmock_expect_call_functions(window):
                    mapped_test = (
                        current_test
                        or TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                        or TestOracleFallback._next_failed_test_name(lines, index + 1)
                    )
                    if mapped_test:
                        failures.append({"test": mapped_test, "function": function, "action": "remove"})

        unique: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in failures:
            if not TestMockFallback._is_valid_mock_call_name(item.get("function", "")):
                continue
            if failed_tests and not TestOracleFallback._test_matches_failure_set(item.get("test", ""), failed_tests):
                continue
            key = (item["test"], item["function"], item["action"])
            if key not in seen:
                unique.append(item)
                seen.add(key)
        return unique

    @staticmethod
    def observed_mock_calls(run_result: RunResult, test_source: Path | str | None = None) -> list[dict[str, object]]:
        """Parse GoogleMock uninteresting-call warnings and keep exact observed arguments.

        These warnings are emitted for calls that happened without a matching
        EXPECT_CALL.  They are not hard failures by themselves, but they are
        valuable fallback evidence: the generated testcase should explicitly
        expect the mock call and preserve the parameter values that selected the
        branch.
        """
        text = TestOracleFallback.backend_execution_text(run_result)
        if not text.strip():
            return []
        source_text = ""
        if test_source is not None:
            try:
                source_text = Path(str(test_source)).read_text(encoding="utf-8", errors="replace")
            except OSError:
                source_text = ""
        test_source_name = Path(str(test_source)).name if test_source is not None else ""
        observations: list[dict[str, object]] = []
        current_test = ""
        pending_failure_line: int | None = None
        warning_context = False
        lines = text.splitlines()
        for index, line in enumerate(lines):
            run_match = re.search(r"^\[\s*RUN\s*\]\s+(.+)$", line)
            if run_match:
                current_test = run_match.group(1).strip().rsplit(".", 1)[-1]
                pending_failure_line = None
                warning_context = False
            line_match = TestOracleFallback._output_cpp_line_match(line)
            if line_match:
                failure_file, failure_line = line_match
                if TestOracleFallback._is_same_output_source(failure_file, test_source_name):
                    pending_failure_line = failure_line
                    mapped = TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                    if mapped:
                        current_test = mapped
                else:
                    pending_failure_line = None
            lower = line.lower()
            if "uninteresting mock function call" in lower:
                warning_context = True
            if warning_context or "function call:" in lower:
                parsed = TestMockFallback._parse_gmock_function_call(line)
                if parsed:
                    function, args = parsed
                    mapped_test = (
                        current_test
                        or TestOracleFallback._test_name_at_line(source_text, pending_failure_line)
                        or TestOracleFallback._next_failed_test_name(lines, index + 1)
                    )
                    if mapped_test:
                        observations.append({"test": mapped_test, "function": function, "action": "add", "args": args})
                    warning_context = False
                    continue
        unique: list[dict[str, object]] = []
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        for item in observations:
            if not TestMockFallback._is_valid_mock_call_name(item.get("function", "")):
                continue
            key = (str(item.get("test") or ""), str(item.get("function") or ""), tuple(str(v) for v in item.get("args", []) or []))
            if key not in seen:
                unique.append(item)
                seen.add(key)
        return unique

    @staticmethod
    def _parse_gmock_function_call(line: str) -> tuple[str, list[str]] | None:
        match = re.search(r"Function call:\s*([A-Za-z_]\w*)\s*\((.*)\)\s*$", html.unescape(str(line or "")))
        if not match:
            return None
        return match.group(1), TestMockFallback._split_mock_call_args(match.group(2))

    @staticmethod
    def _split_mock_call_args(raw: str) -> list[str]:
        text = str(raw or "").strip()
        if not text:
            return []
        args: list[str] = []
        current: list[str] = []
        depth = 0
        quote = ""
        escape = False
        for ch in text:
            if quote:
                current.append(ch)
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    quote = ""
                continue
            if ch in {"'", '"'}:
                quote = ch
                current.append(ch)
                continue
            if ch in "([{<":
                depth += 1
            elif ch in ")]}>":
                depth = max(0, depth - 1)
            if ch == "," and depth == 0:
                args.append("".join(current).strip())
                current = []
                continue
            current.append(ch)
        if current or text.endswith(","):
            args.append("".join(current).strip())
        return args

    @staticmethod
    def _literal_from_mock_arg(raw: str) -> str:
        literal = TestOracleFallback._literal_from_observed_value(raw)
        if literal:
            return literal
        clean = html.unescape(str(raw or "")).strip()
        if clean in {"NULL", "nullptr", "0"}:
            return "0" if clean == "0" else clean
        if re.fullmatch(r"0[xX][0-9A-Fa-f]+", clean):
            return clean
        if re.fullmatch(r"[A-Za-z_]\w*", clean) or re.fullmatch(r"&[A-Za-z_]\w*", clean):
            return clean
        return ""

    @staticmethod
    def _gmock_expect_call_functions(text: str) -> list[str]:
        result: list[str] = []
        compact = " ".join(str(text or "").split())
        patterns = [
            r"EXPECT_CALL\s*\(\s*TestingMate_Mock\s*\(\s*\)\s*,\s*([A-Za-z_]\w*)\s*\(",
            r"EXPECT_CALL\s*\([^,]+,\s*([A-Za-z_]\w*)\s*\(",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, compact):
                name = match.group(1)
                if TestMockFallback._is_valid_mock_call_name(name) and name not in result:
                    result.append(name)
        return result

    @staticmethod
    def _remove_mock_expectation(text: str, test_name: str, function: str) -> tuple[str, bool]:
        span = TestOracleFallback._test_block_span(text, test_name)
        if span is None:
            return text, False
        start, end = span
        block = text[start:end]
        safe = TestBackendGenerator._safe_identifier(function)
        patterns = [
            rf"(?m)^\s*mock\(\)\.expectOneCall\(\s*\"{re.escape(function)}\"\s*\).*?;\s*$\n?",
            rf"(?m)^\s*EXPECT_CALL\(\s*TestingMate_Mock\(\)\s*,\s*{re.escape(safe)}\s*\(.*?;\s*$\n?",
            rf"(?m)^\s*(?:CHECK|EXPECT_GT)\s*\(\s*TestingMate_{re.escape(safe)}_CallCount\(\).*?;\s*$\n?",
        ]
        changed = False
        for pattern in patterns:
            next_block, count = re.subn(pattern, "", block)
            if count:
                block = next_block
                changed = True
        return (text[:start] + block + text[end:], True) if changed else (text, False)

    @staticmethod
    def _add_mock_expectation(
        text: str,
        backend: str,
        test_name: str,
        function: str,
        mock_info: dict[str, dict[str, str]],
        observed_args: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[str, bool]:
        span = TestOracleFallback._test_block_span(text, test_name)
        if span is None:
            return text, False
        start, end = span
        block = text[start:end]
        if not TestMockFallback._is_valid_mock_call_name(function):
            return text, False
        safe = TestBackendGenerator._safe_identifier(function)
        if f'expectOneCall("{function}")' in block or f"EXPECT_CALL(TestingMate_Mock(), {safe}(" in block:
            return text, False
        info = mock_info.get(safe)
        if info is None:
            # Do not emit EXPECT_CALL for a name that is not present in the
            # generated GMock harness. The model-level reconciler may persist
            # the call so a later full regeneration can create a declaration,
            # but direct source patching cannot safely update stubs.h here.
            return text, False
        observed_literals = {
            idx: TestMockFallback._literal_from_mock_arg(str(raw))
            for idx, raw in enumerate(observed_args or [])
            if TestMockFallback._literal_from_mock_arg(str(raw))
        }
        if backend == "CppUTest":
            params = info.get("params", "")
            symbol = StubSymbol(name=function, return_type=info.get("return_type", "void"), params=params, kind="function")
            param_expectations = TestBackendGenerator._cpputest_expected_param_chain(symbol, observed_literals)
            ignore_params = ".ignoreOtherParameters()" if not param_expectations else ""
            line = f'    mock().expectOneCall("{function}"){param_expectations}{ignore_params};\n'
        else:
            arity = int(info.get("arity") or 0)
            matcher_items: list[str] = []
            for idx in range(max(arity, len(observed_literals))):
                literal = observed_literals.get(idx)
                matcher_items.append(f"::testing::Eq({literal})" if literal is not None else "::testing::_")
            args = ", ".join(matcher_items)
            ret = info.get("return_type", "void")
            if ret == "void":
                line = f"    EXPECT_CALL(TestingMate_Mock(), {safe}({args}));\n"
            else:
                typed_ret = TestBackendGenerator._typed_value(ret, cls_default_return(ret))
                line = f"    EXPECT_CALL(TestingMate_Mock(), {safe}({args})).WillOnce(::testing::Return({typed_ret}));\n"
        marker = re.search(r"(?m)^(\s*/\*\s*PRECONDITION\s*\*/\s*\n)", block)
        if marker:
            insert_at = marker.end()
        else:
            marker = re.search(r"(?m)^(\s*/\*\s*ACTIONS\s*\*/)", block)
            insert_at = marker.start() if marker else len(block) - 2
        block = block[:insert_at] + line + block[insert_at:]
        return text[:start] + block + text[end:], True

    @staticmethod
    def _mock_info_map(test_source: Path) -> dict[str, dict[str, str]]:
        header = test_source.parent / "stub" / "include" / "stubs.h"
        if not header.exists():
            return {}
        try:
            text = header.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        result: dict[str, dict[str, str]] = {}
        for match in re.finditer(r"MOCK_METHOD\s*\(\s*([^,]+?)\s*,\s*([A-Za-z_]\w*)\s*,\s*\((.*?)\)\s*,", text, re.S):
            params = match.group(3).strip()
            arity = 0 if not params else len(StubHarnessEngine._split_params(params))
            result[match.group(2)] = {"return_type": " ".join(match.group(1).split()), "arity": str(arity), "params": params}
        return result


def cls_default_return(return_type: str) -> str:
    clean = " ".join((return_type or "").replace("const", "").replace("volatile", "").split()).strip()
    lowered = clean.lower()
    if not clean or lowered == "void":
        return "0"
    if "*" in clean:
        return "nullptr"
    if lowered in {"bool", "_bool", "boolean"}:
        return "false"
    if lowered.startswith(("struct ", "union ")):
        return "{}"
    return "0"


class TestBackendGenerator:
    NON_MOCK_CALL_NAMES = TestCaseRunReconciler.NON_MOCK_CALL_NAMES

    @classmethod
    def _is_valid_mock_call_name(cls, name: object, stubbed_calls: set[str] | None = None) -> bool:
        return TestCaseRunReconciler._is_valid_mock_call_name(name, stubbed_calls)

    include_pattern = re.compile(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", re.M)

    def __init__(self, out_dir: Path, include_dirs: list[str] | None = None):
        self.out_dir = self._as_path(out_dir)
        self.include_dirs = [self._as_path(item).expanduser() for item in (include_dirs or []) if item]
        self.out_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _as_path(value: Path | str) -> Path:
        return value if isinstance(value, Path) else Path(str(value))

    @classmethod
    def _as_path_list(cls, values: list[Path | str] | tuple[Path | str, ...] | None) -> list[Path]:
        return [cls._as_path(item).expanduser() for item in (values or []) if item]

    @classmethod
    def _safe_resolve(cls, value: Path | str) -> Path:
        path = cls._as_path(value).expanduser()
        try:
            return path.resolve()
        except OSError:
            return path

    def write_backend(
        self,
        backend: str,
        source: Path | str,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        level: str,
        unit_sources: list[Path | str] | None = None,
        code_database: CodeDatabase | None = None,
        selected_coverage: str | None = None,
        **_ignored_kwargs,
    ) -> list[Path]:
        # GUI/CLI/project JSON code can pass Windows paths as strings. Normalize
        # at the public boundary so downstream path operations never fail with
        # ``'str' object has no attribute 'resolve'``.
        source_path = self._as_path(source).expanduser()
        normalized_unit_sources = self._as_path_list(unit_sources) or [source_path]
        # Keep native LLVM MC/DC instrumentation strictly tied to the selected
        # coverage objective. C0/C1 generation must not compile or report with
        # -fcoverage-mcdc, even when escalation layers add MC/DC-labelled cases.
        # Legacy callers that do not pass selected_coverage still fall back to
        # testcase labels.
        enable_mcdc = self._coverage_requests_mcdc(selected_coverage, cases)
        if backend == "CppUTest":
            return self._write_cpputest(source_path, functions, cases, normalized_unit_sources, code_database, enable_mcdc)
        if backend == "GoogleTest":
            return self._write_gtest(source_path, functions, cases, normalized_unit_sources, code_database, enable_mcdc)
        return []

    @staticmethod
    def _coverage_requests_mcdc(selected_coverage: str | None, cases: list[TestCase]) -> bool:
        selected = str(selected_coverage or "").strip().upper().replace(" ", "")
        if selected:
            return selected in {"MC/DC", "MCDC"}
        return any("MC/DC" in str(case.coverage).strip().upper() or "MCDC" in str(case.coverage).strip().upper() for case in cases)

    @staticmethod
    def _dedupe_cases(cases: list[TestCase]) -> list[TestCase]:
        from testing_mate.vector_generator import TestCaseRegistry

        TestCaseRegistry.ensure_unique_test_ids(cases)
        manual_keys = (
            "__manual_precondition_code",
            "__manual_action_code",
            "__manual_expected_code",
            "__manual_teardown_code",
            "__manual_steps_only",
        )
        seen: set[str] = set()
        result: list[TestCase] = []
        for case in cases:
            signature = TestVectorGenerator._vector_signature(case.function, case.values or {})
            manual_parts = [
                f"{key}={(case.values or {}).get(key)}"
                for key in manual_keys
                if (case.values or {}).get(key)
            ]
            if manual_parts:
                signature = "|".join([signature, *manual_parts])
            if signature in seen:
                continue
            seen.add(signature)
            result.append(case)
        return result

    def _write_cpputest(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        unit_sources: list[Path],
        code_database: CodeDatabase | None = None,
        enable_mcdc: bool = False,
    ) -> list[Path]:
        test_dir = self.out_dir / f"{source.stem}_cpputest"
        test_dir.mkdir(parents=True, exist_ok=True)
        source_env_dir = test_dir / "src"
        include_env_dir = test_dir / "stub" / "include"
        harness_sources = self._ordered_unit_sources(source, unit_sources)
        harness_include_dirs = self._workspace_include_discovery_roots(source, harness_sources)
        copied = self._copy_workspace_files(source, harness_sources, source_env_dir, include_env_dir)
        test_file = test_dir / f"{source.stem}_cpputest_test.cpp"
        cmake_file = test_dir / "CMakeLists.txt"
        runner = test_dir / "run_cpputest.sh"
        direct_source_include = self._includes_source_directly(source, harness_sources)
        link_sources = self._workspace_link_sources(source, source_env_dir, direct_source_include)
        stub_result = StubHarnessEngine().write_for_backend("CppUTest", test_dir, source, functions, cases, harness_sources, harness_include_dirs, code_database)
        c_sources = " ".join([*self._cmake_source_paths(link_sources), *self._relative_cmake_paths(stub_result.source_files, test_dir)])
        include_dirs = self._cmake_include_dirs([Path("stub/include")])
        test_file.write_text(
            self._cpp_test_source("CppUTest", source, functions, cases, harness_sources, stub_result.stub_symbols),
            encoding="utf-8",
        )
        cmake_file.write_text(
            "\n".join(
                [
                    "cmake_minimum_required(VERSION 3.16)",
                    f"project({source.stem}_cpputest C CXX)",
                    "set(CMAKE_CXX_STANDARD 11)",
                    "enable_testing()",
                    "option(TESTING_MATE_LLVM_COVERAGE \"Enable Clang/LLVM source-based coverage\" ON)",
                    f"option(TESTING_MATE_LLVM_MCDC \"Enable Clang/LLVM MC/DC coverage\" {'ON' if enable_mcdc else 'OFF'})",
                    "option(TESTING_MATE_LEGACY_LCOV \"Enable legacy gcov/lcov C0/C1 coverage\" OFF)",
                    "if(TESTING_MATE_LLVM_COVERAGE)",
                    "  if(NOT CMAKE_C_COMPILER_ID MATCHES \"Clang\")",
                    "    message(FATAL_ERROR \"TESTING_MATE_LLVM_COVERAGE requires Clang\")",
                    "  endif()",
                    "  add_compile_options(-O0 -g -fprofile-instr-generate -fcoverage-mapping)",
                    "  add_link_options(-fprofile-instr-generate)",
                    "  if(TESTING_MATE_LLVM_MCDC)",
                    "    add_compile_options(-fcoverage-mcdc)",
                    "    add_compile_options(\"SHELL:-Xclang -fmcdc-max-conditions=32\")",
                    "    add_compile_options(\"SHELL:-Xclang -fmcdc-max-test-vectors=4096\")",
                    "  endif()",
                    "elseif(TESTING_MATE_LEGACY_LCOV AND CMAKE_C_COMPILER_ID MATCHES \"GNU\")",
                    "  add_compile_options(--coverage -O0 -g)",
                    "  add_link_options(--coverage)",
                    "endif()",
                    "if(MINGW)",
                    "  add_compile_options($<$<COMPILE_LANGUAGE:CXX>:-Wa,-mbig-obj>)",
                    "endif()",
                    "find_package(CppUTest REQUIRED)",
                    'message(STATUS "C compiler: ${CMAKE_C_COMPILER}")',
                    'message(STATUS "CXX compiler: ${CMAKE_CXX_COMPILER}")',
                    'message(STATUS "CMAKE_PREFIX_PATH: ${CMAKE_PREFIX_PATH}")',
                    'message(STATUS "CppUTest_DIR: ${CppUTest_DIR}")',
                    'if(CMAKE_CXX_COMPILER MATCHES "clang64" AND CppUTest_DIR MATCHES "mingw64")',
                    '  message(FATAL_ERROR "Invalid mixed environment: CLANG64 compiler with MINGW64 CppUTest")',
                    'endif()',
                    'if(CMAKE_CXX_COMPILER MATCHES "mingw64" AND CppUTest_DIR MATCHES "clang64")',
                    '  message(FATAL_ERROR "Invalid mixed environment: MINGW64 compiler with CLANG64 CppUTest")',
                    'endif()',
                    f"add_executable({source.stem}_cpputest {test_file.name} {c_sources})",
                    f"target_include_directories({source.stem}_cpputest PRIVATE {include_dirs})",
                    f"target_compile_options({source.stem}_cpputest PRIVATE $<$<COMPILE_LANGUAGE:C>:-include> $<$<COMPILE_LANGUAGE:C>:stubs.h>)",
                    'if(TARGET CppUTest::CppUTest)',
                    f'  target_link_libraries({source.stem}_cpputest CppUTest::CppUTest CppUTest::CppUTestExt)',
                    'else()',
                    f'  target_link_libraries({source.stem}_cpputest CppUTest CppUTestExt)',
                    'endif()',
                    f"add_test(NAME {source.stem}_cpputest COMMAND {source.stem}_cpputest)",
                ]
            ),
            encoding="utf-8",
        )
        runner.write_text(self._cmake_runner(source.stem + "_cpputest", self._workspace_runner_target_source(source), enable_mcdc), encoding="utf-8")
        return [*copied, *stub_result.generated_paths, test_file, cmake_file, runner]

    def _write_gtest(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        unit_sources: list[Path],
        code_database: CodeDatabase | None = None,
        enable_mcdc: bool = False,
    ) -> list[Path]:
        test_dir = self.out_dir / f"{source.stem}_gtest"
        test_dir.mkdir(parents=True, exist_ok=True)
        source_env_dir = test_dir / "src"
        include_env_dir = test_dir / "stub" / "include"
        harness_sources = self._ordered_unit_sources(source, unit_sources)
        harness_include_dirs = self._workspace_include_discovery_roots(source, harness_sources)
        copied = self._copy_workspace_files(source, harness_sources, source_env_dir, include_env_dir)
        test_file = test_dir / f"{source.stem}_gtest_test.cpp"
        cmake_file = test_dir / "CMakeLists.txt"
        runner = test_dir / "run_gtest.sh"
        direct_source_include = self._includes_source_directly(source, harness_sources)
        link_sources = self._workspace_link_sources(source, source_env_dir, direct_source_include)
        stub_result = StubHarnessEngine().write_for_backend("GoogleTest", test_dir, source, functions, cases, harness_sources, harness_include_dirs, code_database)
        c_sources = " ".join([*self._cmake_source_paths(link_sources), *self._relative_cmake_paths(stub_result.source_files, test_dir)])
        include_dirs = self._cmake_include_dirs([Path("stub/include")])
        test_file.write_text(
            self._cpp_test_source("GoogleTest", source, functions, cases, harness_sources, stub_result.stub_symbols),
            encoding="utf-8",
        )
        cmake_file.write_text(
            "\n".join(
                [
                    "cmake_minimum_required(VERSION 3.16)",
                    f"project({source.stem}_gtest C CXX)",
                    "set(CMAKE_CXX_STANDARD 14)",
                    "enable_testing()",
                    "option(TESTING_MATE_LLVM_COVERAGE \"Enable Clang/LLVM source-based coverage\" ON)",
                    f"option(TESTING_MATE_LLVM_MCDC \"Enable Clang/LLVM MC/DC coverage\" {'ON' if enable_mcdc else 'OFF'})",
                    "option(TESTING_MATE_LEGACY_LCOV \"Enable legacy gcov/lcov C0/C1 coverage\" OFF)",
                    "if(TESTING_MATE_LLVM_COVERAGE)",
                    "  if(NOT CMAKE_C_COMPILER_ID MATCHES \"Clang\")",
                    "    message(FATAL_ERROR \"TESTING_MATE_LLVM_COVERAGE requires Clang\")",
                    "  endif()",
                    "  add_compile_options(-O0 -g -fprofile-instr-generate -fcoverage-mapping)",
                    "  add_link_options(-fprofile-instr-generate)",
                    "  if(TESTING_MATE_LLVM_MCDC)",
                    "    add_compile_options(-fcoverage-mcdc)",
                    "    add_compile_options(\"SHELL:-Xclang -fmcdc-max-conditions=32\")",
                    "    add_compile_options(\"SHELL:-Xclang -fmcdc-max-test-vectors=4096\")",
                    "  endif()",
                    "elseif(TESTING_MATE_LEGACY_LCOV AND CMAKE_C_COMPILER_ID MATCHES \"GNU\")",
                    "  add_compile_options(--coverage -O0 -g)",
                    "  add_link_options(--coverage)",
                    "endif()",
                    "if(MINGW)",
                    "  add_compile_options($<$<COMPILE_LANGUAGE:CXX>:-Wa,-mbig-obj>)",
                    "endif()",
                    "find_package(GTest REQUIRED)",
                    'message(STATUS "C compiler: ${CMAKE_C_COMPILER}")',
                    'message(STATUS "CXX compiler: ${CMAKE_CXX_COMPILER}")',
                    'message(STATUS "CMAKE_PREFIX_PATH: ${CMAKE_PREFIX_PATH}")',
                    'message(STATUS "GTest_DIR: ${GTest_DIR}")',
                    'if(CMAKE_CXX_COMPILER MATCHES "clang64" AND GTest_DIR MATCHES "mingw64")',
                    '  message(FATAL_ERROR "Invalid mixed environment: CLANG64 compiler with MINGW64 GTest")',
                    'endif()',
                    'if(CMAKE_CXX_COMPILER MATCHES "mingw64" AND GTest_DIR MATCHES "clang64")',
                    '  message(FATAL_ERROR "Invalid mixed environment: MINGW64 compiler with CLANG64 GTest")',
                    'endif()',
                    f"add_executable({source.stem}_gtest {test_file.name} {c_sources})",
                    f"target_include_directories({source.stem}_gtest PRIVATE {include_dirs})",
                    f"target_compile_options({source.stem}_gtest PRIVATE $<$<COMPILE_LANGUAGE:C>:-include> $<$<COMPILE_LANGUAGE:C>:stubs.h>)",
                    "if(TARGET GTest::gmock)",
                    f"  target_link_libraries({source.stem}_gtest GTest::gmock GTest::gtest GTest::gtest_main)",
                    "else()",
                    f"  target_link_libraries({source.stem}_gtest GTest::gtest GTest::gtest_main)",
                    "endif()",
                    f"add_test(NAME {source.stem}_gtest COMMAND {source.stem}_gtest)",
                ]
            ),
            encoding="utf-8",
        )
        runner.write_text(self._cmake_runner(source.stem + "_gtest", self._workspace_runner_target_source(source), enable_mcdc), encoding="utf-8")
        return [*copied, *stub_result.generated_paths, test_file, cmake_file, runner]

    @classmethod
    def _ordered_unit_sources(cls, source: Path | str, unit_sources: list[Path | str] | None) -> list[Path]:
        ordered: list[Path] = []
        seen: set[str] = set()
        for raw_item in [source, *(unit_sources or [])]:
            if raw_item is None:
                continue
            item = cls._as_path(raw_item).expanduser()
            key = str(cls._safe_resolve(item) if item.exists() else item).lower()
            if key not in seen:
                seen.add(key)
                ordered.append(item)
        return ordered

    def _includes_source_directly(self, source: Path, unit_sources: list[Path]) -> bool:
        include_name = self._guess_header(source, unit_sources)
        return source.suffix.lower() in C_SOURCE_EXTENSIONS and Path(include_name).name == source.name

    def _workspace_link_sources(self, source: Path | str, source_env_dir: Path, direct_source_include: bool = False) -> list[Path]:
        """Return source files compiled by the generated backend project.

        The workspace model intentionally compiles only the selected target
        source copied under ``src/``. Other project C/C++ files are treated as
        dependencies and are represented by generated stubs, so they are not
        copied or linked into the backend executable.
        """
        source_path = self._as_path(source).expanduser()
        if source_path.suffix.lower() not in C_SOURCE_EXTENSIONS:
            return []
        if self._defines_application_main(source_path):
            return []
        if direct_source_include:
            return []
        return [Path("src") / source_path.name]

    @classmethod
    def _workspace_runner_target_source(cls, source: Path | str) -> str:
        """Coverage target path used inside the generated backend folder."""
        source_path = cls._as_path(source).expanduser()
        return f"src/{source_path.name}".replace("\\", "/")

    def _copy_workspace_files(
        self,
        source: Path | str,
        unit_sources: list[Path | str],
        source_env_dir: Path,
        include_env_dir: Path,
    ) -> list[Path]:
        """Create the isolated backend workspace.

        Layout:
        - ``src/`` receives only the selected target source file.
        - ``stub/include/`` receives headers required by the target source and
          its copied header dependency chain.

        Original project include directories are used only as discovery roots
        while copying. Generated CMake does not expose those original include
        directories to the compiler, which makes the backend project portable
        and prevents accidental direct compilation against production folders.
        """
        copied: list[Path] = []
        seen: set[Path] = set()
        queue: list[Path] = []
        scanned: set[Path] = set()

        source_path = self._as_path(source).expanduser()
        normalized_sources = self._as_path_list(unit_sources)

        local_source = self._copy_target_source(source_path, source_env_dir)
        copied.extend(local_source)

        if source_path.exists() and source_path.is_file():
            queue.append(self._safe_resolve(source_path))  # scan includes from original source

        # Prefer headers associated with the target source, then any explicitly
        # supplied header unit files. Do not copy dependency C/C++ implementation
        # files into the workspace.
        for path in self._unit_header_candidates(source_path):
            self._copy_project_file(path, include_env_dir, copied, seen, queue, preserve_from=None)
        for unit in normalized_sources:
            if unit.suffix.lower() in HEADER_SOURCE_EXTENSIONS:
                self._copy_project_file(unit, include_env_dir, copied, seen, queue, preserve_from=self._matching_include_root(unit, normalized_sources))
            elif unit != source_path:
                for path in self._unit_header_candidates(unit):
                    self._copy_project_file(path, include_env_dir, copied, seen, queue, preserve_from=self._matching_include_root(path, normalized_sources))

        index = 0
        while index < len(queue):
            path = queue[index]
            index += 1
            resolved_path = self._safe_resolve(path) if path.exists() else path
            if resolved_path in scanned:
                continue
            scanned.add(resolved_path)
            for include_name in self._extract_includes(path):
                resolved = self._resolve_include(include_name, path.parent, normalized_sources)
                if resolved and resolved.suffix.lower() in HEADER_SOURCE_EXTENSIONS:
                    self._copy_project_file(
                        resolved,
                        include_env_dir,
                        copied,
                        seen,
                        queue,
                        preserve_from=self._matching_include_root(resolved, normalized_sources),
                    )
        return copied

    def _copy_target_source(self, source: Path | str, source_env_dir: Path) -> list[Path]:
        copied: list[Path] = []
        source = self._as_path(source).expanduser()
        if not source.exists() or not source.is_file() or source.suffix.lower() not in C_SOURCE_EXTENSIONS:
            return copied
        source_env_dir.mkdir(parents=True, exist_ok=True)
        target = source_env_dir / source.name
        shutil.copy2(source, target)
        copied.append(target)
        return copied

    @staticmethod
    def _unit_header_candidates(source: Path) -> list[Path]:
        candidates: list[Path] = []
        source = TestBackendGenerator._as_path(source).expanduser()
        if source.suffix.lower() in HEADER_SOURCE_EXTENSIONS:
            candidates.append(source)
        for suffix in sorted(HEADER_SOURCE_EXTENSIONS):
            candidates.append(source.with_suffix(suffix))
        return candidates

    def _link_unit_sources(self, sources: list[Path]) -> list[Path]:
        # Kept for compatibility with any external callers. Backend generation
        # now uses _workspace_link_sources() to compile only the local UUT copy.
        return [
            source
            for source in sources
            if source.suffix.lower() in C_SOURCE_EXTENSIONS and not self._defines_application_main(source)
        ]

    @staticmethod
    def _defines_application_main(source: Path) -> bool:
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        clean = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        clean = re.sub(r"//.*", "", clean)
        return bool(re.search(r"\b(?:int|void)\s+main\s*\(", clean))

    def _copy_project_file(
        self,
        path: Path,
        test_dir: Path,
        copied: list[Path],
        seen: set[Path],
        queue: list[Path],
        preserve_from: Path | None,
    ) -> None:
        if not path.exists() or not path.is_file():
            return
        if path.suffix.lower() not in HEADER_SOURCE_EXTENSIONS:
            return
        path = self._as_path(path).expanduser()
        resolved = self._safe_resolve(path)
        if resolved in seen:
            return
        seen.add(resolved)
        if preserve_from:
            try:
                relative = resolved.relative_to(self._safe_resolve(preserve_from))
            except ValueError:
                relative = Path(path.name)
        else:
            relative = Path(path.name)
        target = test_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(target)
        if path.suffix.lower() in HEADER_SOURCE_EXTENSIONS:
            queue.append(resolved)

    def _cpp_test_source(
        self,
        backend: str,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        unit_sources: list[Path],
        stub_symbols: list[StubSymbol] | None = None,
    ) -> str:
        include = self._guess_header(source, unit_sources)
        struct_field_types = self._struct_field_type_map(source, unit_sources)
        stubbed_calls = {symbol.name for symbol in (stub_symbols or []) if symbol.kind == "function"}
        stub_symbols_by_name = {symbol.name: symbol for symbol in (stub_symbols or []) if symbol.kind == "function"}
        lines = []
        if backend == "CppUTest":
            lines.extend([
                '#include "CppUTest/CommandLineTestRunner.h"',
                '#include "CppUTest/TestHarness.h"',
                '#include <string.h>',
                "",
                'extern "C" {',
                f'#include "{include}"',
                "}",
                '#include "stubs.h"',
                "",
            ])
        else:
            lines.extend([
                "#include <gtest/gtest.h>",
                "#include <string.h>",
                "",
                'extern "C" {',
                f'#include "{include}"',
                "}",
                '#include "stubs.h"',
                "",
            ])
        grouped: dict[str, list[TestCase]] = {}
        for case in self._dedupe_cases(cases):
            grouped.setdefault(case.function, []).append(case)
        for fn in functions:
            if fn.return_type.strip().startswith("static"):
                continue
            fn_cases = grouped.get(fn.name, [])[:512]
            if not fn_cases:
                continue
            group_name = re.sub(r"[^A-Za-z0-9_]", "_", fn.name)
            suite_name = group_name if backend == "CppUTest" else f"{group_name}_Group"
            pointer_writes = self._written_pointer_observables(fn)
            global_type_map = self._global_variable_type_map(unit_sources)
            global_type_map.update(self._function_global_variable_type_map(fn))
            static_global_names = self._static_global_variable_names(unit_sources)
            global_type_map = {name: ctype for name, ctype in global_type_map.items() if name not in static_global_names}
            global_writes = self._filter_global_names(fn, self._written_global_names(fn), global_type_map, static_global_names)
            inferred_case_expectations = {
                case.test_id: self._merge_expectation_sources(
                    self._infer_case_expected_literals_from_ast_or_source(fn, case, global_writes),
                    self._fixit_case_expected_literals(fn, case),
                )
                for case in fn_cases
            }
            inferred_global_names: set[str] = set()
            inferred_output_names: set[str] = set()
            for inferred in inferred_case_expectations.values():
                inferred["globals"] = self._filter_global_literals(fn, inferred.get("globals") or {}, global_type_map, static_global_names)
                inferred_global_names.update((inferred.get("globals") or {}).keys())
                inferred_output_names.update((inferred.get("outputs") or {}).keys())
            global_writes = self._filter_global_names(fn, set(global_writes) | inferred_global_names, global_type_map, static_global_names)
            suite_globals = self._filter_global_names(
                fn,
                self._suite_lifecycle_global_names(fn_cases, global_writes) | inferred_global_names,
                global_type_map,
                static_global_names,
            )
            param_by_name = {param.name: param for param in fn.parameters}
            suite_expected_outputs = set(inferred_output_names)
            for case in fn_cases:
                suite_expected_outputs.update(self._expected_output_literals(case).keys())

            fixture_declarations: list[str] = []
            declared_fixture_names: set[str] = set()

            def add_fixture_declaration(c_type: str, name: str, array_suffix: str = "") -> str:
                # Every variable assigned in setup/teardown/PRECONDITION/ACTION must be
                # declared once in the test group before any generated assignment uses it.
                # Source locals are also materialized as fixture members so expected
                # expressions such as `ret`, `status`, or `result` never become
                # undeclared in the generated test translation unit.
                safe_name = self._safe_identifier(name)
                if not self._is_c_identifier(safe_name) or safe_name in declared_fixture_names:
                    return ""
                safe_type = self._clean_fixture_type(c_type, defaults="uint8", mutable=True)
                if not self._is_valid_fixture_type(safe_type):
                    safe_type = "uint8"
                suffix = " ".join(str(array_suffix or "").split())
                if suffix and not re.fullmatch(r"(?:\[[^;{}()]*\]\s*)+", suffix):
                    suffix = ""
                fixture_declarations.append(f"{safe_type} {safe_name}{suffix};")
                declared_fixture_names.add(safe_name)
                return safe_name

            setup_reset_lines: list[str] = ["TestingMate_ResetStubs();", "TestingMate_AllowUnexpectedMockCalls();"]
            teardown_reset_lines: list[str] = ["TestingMate_VerifyAndClearMocks();", "TestingMate_ResetStubs();"]
            call_args_by_param: dict[str, str] = {}
            pointer_fixtures: dict[str, str] = {}
            pointer_arg_names: dict[str, str] = {}

            for param in fn.parameters:
                if param.kind == "pointer":
                    fixture_name = f"tm_{self._safe_identifier(param.name)}"
                    arg_name = f"tm_arg_{self._safe_identifier(param.name)}"
                    fixture_type = self._pointer_fixture_type(param)
                    arg_type = self._pointer_argument_type(param, fixture_type)
                    pointer_fixtures[param.name] = fixture_name
                    pointer_arg_names[param.name] = arg_name
                    call_args_by_param[param.name] = arg_name
                    add_fixture_declaration(fixture_type, fixture_name)
                    add_fixture_declaration(arg_type, arg_name)
                    setup_reset_lines.append(f"{fixture_name} = {{}};")
                    setup_reset_lines.append(f"{arg_name} = &{fixture_name};")
                    teardown_reset_lines.append(f"{fixture_name} = {{}};")
                    teardown_reset_lines.append(f"{arg_name} = &{fixture_name};")
                else:
                    local_name = f"tm_{self._safe_identifier(param.name)}"
                    local_type = self._param_local_type(param)
                    call_args_by_param[param.name] = local_name
                    add_fixture_declaration(local_type, local_name)
                    setup_reset_lines.append(f"{local_name} = {{}};")
                    teardown_reset_lines.append(f"{local_name} = {{}};")

            call_args = [call_args_by_param[param.name] for param in fn.parameters]
            call = f"{fn.name}({', '.join(call_args)})"

            return_fixture_name = ""
            expected_return_fixture_name = ""
            return_type = ""
            if not self._is_void_return_type(fn.return_type):
                return_fixture_name = "actual_return"
                expected_return_fixture_name = "expected_return"
                return_type = self._return_fixture_type(fn)
                add_fixture_declaration(return_type, return_fixture_name)
                add_fixture_declaration(return_type, expected_return_fixture_name)
                setup_reset_lines.append(f"{return_fixture_name} = {{}};")
                setup_reset_lines.append(f"{expected_return_fixture_name} = {{}};")
                teardown_reset_lines.append(f"{return_fixture_name} = {{}};")
                teardown_reset_lines.append(f"{expected_return_fixture_name} = {{}};")

            expected_output_fixtures: dict[str, str] = {}
            for output_name in sorted(suite_expected_outputs):
                root = self._normalize_observable_name(output_name).split(".", 1)[0]
                param = param_by_name.get(root)
                if param is None or param.kind != "pointer" or root in expected_output_fixtures:
                    continue
                fixture_name = f"expected_{pointer_fixtures.get(root, 'tm_' + self._safe_identifier(root))}"
                expected_output_fixtures[root] = fixture_name
                add_fixture_declaration(self._pointer_fixture_type(param), fixture_name)
                setup_reset_lines.append(f"{fixture_name} = {{}};")
                teardown_reset_lines.append(f"{fixture_name} = {{}};")

            expected_global_fixtures: dict[str, str] = {}
            for global_name in sorted(suite_globals):
                if not self._is_c_identifier(global_name):
                    continue
                global_type = global_type_map.get(global_name, "uint8")
                expected_name = f"expected_global_{self._safe_identifier(global_name)}"
                expected_global_fixtures[global_name] = expected_name
                add_fixture_declaration(global_type, expected_name)
                typed_zero = self._typed_value(global_type, self._default_expected_literal(global_type))
                setup_reset_lines.append(f"{global_name} = {typed_zero};")
                setup_reset_lines.append(f"{expected_name} = {typed_zero};")
                teardown_reset_lines.append(f"{global_name} = {typed_zero};")
                teardown_reset_lines.append(f"{expected_name} = {typed_zero};")

            # Materialize all non-static implementation locals as fixture members.
            # Static locals belong to the UUT and must not be duplicated in the test fixture.
            for spec in self._local_variable_fixture_specs(fn):
                local_name = str(spec.get("name") or "")
                local_type = str(spec.get("ctype") or "uint8")
                array_suffix = str(spec.get("array") or "")
                safe_local_name = add_fixture_declaration(local_type, local_name, array_suffix)
                if not safe_local_name:
                    continue
                if array_suffix:
                    setup_reset_lines.append(f"memset({safe_local_name}, 0, sizeof({safe_local_name}));")
                    teardown_reset_lines.append(f"memset({safe_local_name}, 0, sizeof({safe_local_name}));")
                else:
                    setup_reset_lines.append(f"{safe_local_name} = {{}};")
                    teardown_reset_lines.append(f"{safe_local_name} = {{}};")

            for local_name, local_type in self._suite_local_fixture_declarations(fn_cases, fn).items():
                safe_local_name = add_fixture_declaration(local_type, local_name)
                if not safe_local_name:
                    continue
                setup_reset_lines.append(f"{safe_local_name} = {{}};")
                teardown_reset_lines.append(f"{safe_local_name} = {{}};")

            lines.extend(self._test_group_lines(backend, suite_name, fn_cases[0], fixture_declarations, setup_reset_lines, teardown_reset_lines))

            for case in fn_cases:
                test_name = re.sub(r"[^A-Za-z0-9_]", "_", case.test_id)
                if backend == "CppUTest":
                    lines.append(f"TEST({suite_name}, {test_name})")
                else:
                    lines.append(f"TEST_F({suite_name}, {test_name})")
                lines.append("{")
                lines.extend(self._case_comment_lines(case, "    "))

                precondition_lines = self._case_precondition_lines(
                    case,
                    fn,
                    pointer_writes,
                    pointer_fixtures,
                    pointer_arg_names,
                    "    ",
                    struct_field_types,
                    backend,
                    stubbed_calls,
                    stub_symbols_by_name,
                    global_type_map,
                    static_global_names,
                )
                lines.append("    /* PRECONDITION */")
                lines.extend(precondition_lines if precondition_lines else ["    /* No explicit precondition values were generated. */"])
                manual_precondition = self._manual_code_lines(case, "__manual_precondition_code", "    ")
                if manual_precondition:
                    lines.append("    /* MANUAL PRECONDITION CODE */")
                    lines.extend(manual_precondition)
                
                values = {**self._fixit_input_literals(fn, case), **(case.values or {})}
                expected_calls = self._case_stub_call_names(fn, case, stubbed_calls or set())
                if expected_calls:
                    lines.append(f"    /* MOCK EXPECTATIONS */")
                lines.extend(self._stub_expectation_setup_lines(
                    backend,
                    expected_calls,
                    values,
                    stub_symbols_by_name or {},
                    "    ",
                ))                

                inferred_expectations = inferred_case_expectations.get(case.test_id, {})
                case_expected_outputs = set((inferred_expectations.get("outputs") or {}).keys()) | set(self._expected_output_literals(case).keys())
                case_expected_globals = set((inferred_expectations.get("globals") or {}).keys()) | set(self._expected_global_literals(case).keys())
                expected_setup_lines = self._case_expected_precondition_lines(
                    case,
                    return_type,
                    expected_return_fixture_name,
                    case_expected_outputs,
                    expected_output_fixtures,
                    param_by_name,
                    inferred_expectations,
                    expected_global_fixtures,
                    global_type_map,
                    "    ",
                    struct_field_types,
                )
                if expected_setup_lines:
                    lines.append("    /* EXPECTED RESULTS SETUP */")
                    lines.extend(expected_setup_lines)

                lines.append("    /* ACTIONS */")
                manual_action = self._manual_code_lines(case, "__manual_action_code", "    ")
                if manual_action:
                    lines.extend(manual_action)
                elif self._is_void_return_type(fn.return_type):
                    lines.append(f"    {call};")
                else:
                    lines.append(f"    {return_fixture_name} = {{}};")
                    lines.append(f"    {return_fixture_name} = {call};")
                expected_call_lines = self._stub_call_assertion_lines(
                    backend,
                    self._case_stub_call_names(fn, case, stubbed_calls),
                    "    ",
                )
                # if expected_call_lines:
                #     lines.append("    /* EXPECTED CALLS */")
                #     lines.extend(expected_call_lines)
                assertion_lines = self._case_expected_assertion_lines(
                    backend,
                    case,
                    fn,
                    return_type,
                    expected_return_fixture_name,
                    return_fixture_name,
                    case_expected_outputs,
                    expected_output_fixtures,
                    pointer_fixtures,
                    param_by_name,
                    inferred_expectations,
                    case_expected_globals,
                    expected_global_fixtures,
                    global_type_map,
                    "    ",
                    struct_field_types,
                )
                if assertion_lines:
                    lines.append("    /* EXPECTED RESULTS */")
                    lines.extend(assertion_lines)
                manual_expected = self._manual_code_lines(case, "__manual_expected_code", "    ")
                if manual_expected:
                    lines.append("    /* MANUAL EXPECTED CODE */")
                    lines.extend(manual_expected)
                manual_teardown = self._manual_code_lines(case, "__manual_teardown_code", "    ")
                if manual_teardown:
                    lines.append("    /* MANUAL TEARDOWN CODE */")
                    lines.extend(manual_teardown)
                lines.append(f"    /* {case.rationale} */")
                lines.append("}")
                lines.append("")
        if backend == "CppUTest":
            lines.extend(["int main(int argc, char **argv)", "{", "    return CommandLineTestRunner::RunAllTests(argc, argv);", "}", ""])
        return "\n".join(lines)

    @staticmethod
    def _manual_code_lines(case: TestCase, key: str, indent: str = "") -> list[str]:
        values = case.values or {}
        raw = str(values.get(key) or "").rstrip()
        if not raw:
            return []
        lines = [f"{indent}// BEGIN user manual testcase block: {key}"]
        for line in raw.splitlines():
            lines.append(f"{indent}{line}" if line.strip() else "")
        lines.append(f"{indent}// END user manual testcase block")
        return lines

    @staticmethod
    def _state_setup_lines(
        case: TestCase | dict,
        indent: str = "",
        global_type_map: dict[str, str] | None = None,
        blocked_global_names: set[str] | None = None,
    ) -> list[str]:
        values = case if isinstance(case, dict) else (case.values or {})
        global_type_map = global_type_map or {}
        blocked_global_names = blocked_global_names or set()
        lines: list[str] = []
        for key, value in sorted(values.items()):
            for prefix in ("__global.", "__set_global.", "__input_global.", "__setup_global.", "setup.global."):
                if key.startswith(prefix):
                    symbol = key[len(prefix) :]
                    if symbol in blocked_global_names:
                        break
                    if global_type_map and symbol not in global_type_map:
                        break
                    if TestBackendGenerator._is_c_identifier(symbol):
                        ctype = global_type_map.get(symbol, TestBackendGenerator._fixture_type_from_value(str(value)))
                        lines.append(f"{indent}{symbol} = {TestBackendGenerator._typed_value(ctype, str(value))};")
                    break
        return lines

    @staticmethod
    def _stub_expectation_setup_lines(
        backend: str,
        call_names: list[str],
        values: dict[str, str],
        stub_symbols: dict[str, StubSymbol],
        indent: str = "",
    ) -> list[str]:
        if backend not in {"CppUTest", "GoogleTest"}:
            return []
        lines: list[str] = []
        for name in call_names:
            if not TestBackendGenerator._is_valid_mock_call_name(name, set(stub_symbols)):
                continue
            symbol = stub_symbols.get(name)
            if symbol is None or getattr(symbol, "kind", "function") != "function":
                continue
            safe = TestBackendGenerator._safe_identifier(name)
            has_stub_return = f"__stub_return.{name}" in values
            value = values.get(f"__stub_return.{name}", "0")
            expected_args = TestBackendGenerator._expected_call_arg_values(name, values)
            output_args = TestBackendGenerator._expected_call_output_values(name, values)
            if backend == "CppUTest":
                param_expectations = TestBackendGenerator._cpputest_expected_param_chain(symbol, expected_args)
                output_decls, output_chain = TestBackendGenerator._cpputest_output_param_chain(symbol, output_args, safe, indent)
                lines.extend(output_decls)
                ignore_params = ".ignoreOtherParameters()" if symbol and TestBackendGenerator._has_mock_params(symbol.params) and not param_expectations and not output_chain else ""
                if symbol and TestBackendGenerator._is_void_return_type(symbol.return_type):
                    lines.append(f'{indent}mock().expectOneCall("{name}"){param_expectations}{output_chain}{ignore_params};')
                else:
                    return_type = TestBackendGenerator._mock_return_type(symbol.return_type if symbol else "")
                    lines.append(f'{indent}mock().expectOneCall("{name}"){param_expectations}{output_chain}{ignore_params}.andReturnValue({TestBackendGenerator._typed_value(return_type, str(value))});')
            else:
                args = TestBackendGenerator._gmock_argument_matchers(symbol.params if symbol else "void", expected_args, output_args)
                actions = TestBackendGenerator._gmock_mock_actions(symbol, value, output_args, has_stub_return)
                action_suffix = f".WillOnce({actions})" if actions else ""
                lines.append(f"{indent}EXPECT_CALL(TestingMate_Mock(), {safe}({args})){action_suffix};")
        return lines

    @staticmethod
    def _expected_stub_call_names(case: TestCase, stubbed_calls: set[str]) -> list[str]:
        values = case.values or {}
        names: list[str] = []

        def add(raw: object) -> None:
            for item in re.split(r"[,;\s]+", str(raw or "")):
                clean = item.strip()
                if TestBackendGenerator._is_valid_mock_call_name(clean, stubbed_calls) and clean not in names:
                    names.append(clean)

        for key, value in sorted(values.items()):
            if key in {"__expected_calls", "__expect_calls", "expected_calls", "expect_calls"}:
                add(value)
            elif key.startswith("__expected_call."):
                candidate = key.split(".", 1)[1]
                if TestBackendGenerator._is_valid_mock_call_name(candidate, stubbed_calls):
                    add(candidate)
            elif key.startswith("__stub_return."):
                clean = key.split(".", 1)[1]
                if TestBackendGenerator._is_valid_mock_call_name(clean, stubbed_calls) and clean not in names:
                    names.append(clean)
        return names

    @staticmethod
    def _case_stub_call_names(fn: FunctionInfo, case: TestCase, stubbed_calls: set[str]) -> list[str]:
        values = case.values or {}
        explicit_keys = {"__expected_calls", "__expect_calls", "expected_calls", "expect_calls"}
        has_explicit = any(key in values for key in explicit_keys) or any(
            key.startswith("__expected_call.") or key.startswith("__stub_return.")
            for key in values
        )
        explicit = TestBackendGenerator._expected_stub_call_names(case, stubbed_calls)
        if has_explicit:
            return explicit
        return TestBackendGenerator._auto_stub_call_names(fn, stubbed_calls)

    @staticmethod
    def _auto_stub_call_names(fn: FunctionInfo, stubbed_calls: set[str]) -> list[str]:
        if not stubbed_calls:
            return []
        body = re.sub(r"//.*?$|/\*.*?\*/", " ", fn.body or "", flags=re.S | re.M)
        names: list[str] = []
        for match in re.finditer(r"(?<![.#>])\b([A-Za-z_]\w*)\s*\(", body):
            name = match.group(1)
            if TestBackendGenerator._is_valid_mock_call_name(name, stubbed_calls) and name not in names:
                names.append(name)
        return names

    @staticmethod
    def _gmock_argument_matchers(
        params: str,
        expected_args: dict[int, str] | None = None,
        output_args: dict[int, str] | None = None,
    ) -> str:
        trimmed = str(params or "").strip()
        if not trimmed or trimmed == "void" or trimmed == "...":
            return ""
        depth = 0
        items: list[str] = []
        current = ""
        for ch in trimmed:
            if ch == "," and depth == 0:
                items.append(current.strip())
                current = ""
                continue
            current += ch
            if ch in "([{<":
                depth += 1
            elif ch in ")]}>":
                depth = max(0, depth - 1)
        if current.strip():
            items.append(current.strip())
        expected_args = expected_args or {}
        output_args = output_args or {}
        matchers: list[str] = []
        for index, item in enumerate(item for item in items if item and item != "void"):
            value = expected_args.get(index)
            name = StubHarnessEngine._param_name(item, index)
            ctype = StubHarnessEngine._param_type(item, name) if name else item
            if value is None:
                if index in output_args and "*" in ctype:
                    matchers.append("::testing::NotNull()")
                else:
                    matchers.append("::testing::_")
                continue
            if "*" in ctype and str(value).strip() not in {"0", "NULL", "nullptr"} and not str(value).strip().startswith("&"):
                matchers.append("::testing::_")
                continue
            matchers.append(f"::testing::Eq({TestBackendGenerator._typed_value(ctype, value)})")
        return ", ".join(matchers)

    @staticmethod
    def _gmock_mock_actions(
        symbol: StubSymbol | None,
        return_value: str,
        output_args: dict[int, str],
        has_stub_return: bool = True,
    ) -> str:
        if symbol is None:
            return ""
        actions: list[str] = []
        pointer_types = TestBackendGenerator._pointer_pointee_types(symbol.params)
        for index in sorted(output_args):
            pointee_type = pointer_types.get(index)
            if not pointee_type:
                continue
            typed = TestBackendGenerator._typed_value(pointee_type, str(output_args[index]))
            actions.append(f"::testing::SetArgPointee<{index}>({typed})")
        if not TestBackendGenerator._is_void_return_type(symbol.return_type) and has_stub_return:
            return_type = TestBackendGenerator._mock_return_type(symbol.return_type)
            typed_return = TestBackendGenerator._typed_value(return_type, str(return_value))
            actions.append(f"::testing::Return({typed_return})")
        if not actions:
            return ""
        if len(actions) == 1:
            return actions[0]
        return f"::testing::DoAll({', '.join(actions)})"

    @staticmethod
    def _pointer_pointee_types(params: str) -> dict[int, str]:
        result: dict[int, str] = {}
        trimmed = StubHarnessEngine._definition_params(params)
        if trimmed == "void":
            return result
        for index, raw in enumerate(StubHarnessEngine._split_params(trimmed)):
            name = StubHarnessEngine._param_name(raw, index) or f"p{index}"
            ctype = StubHarnessEngine._param_type(raw, name)
            if "*" not in ctype:
                continue
            pointee = ctype.replace("const", "").replace("volatile", "").replace("*", " ")
            result[index] = " ".join(pointee.split()).strip() or "int"
        return result

    @staticmethod
    def _expected_call_arg_values(call_name: str, values: dict[str, str]) -> dict[int, str]:
        prefix = f"__expected_call_arg.{call_name}."
        result: dict[int, str] = {}
        for key, value in values.items():
            if not key.startswith(prefix):
                continue
            raw = key[len(prefix):]
            if raw.isdigit():
                result[int(raw)] = str(value)
        return result

    @staticmethod
    def _expected_call_output_values(call_name: str, values: dict[str, str]) -> dict[int, str]:
        prefixes = (
            f"__stub_output.{call_name}.",
            f"__mock_output.{call_name}.",
            f"__expected_call_output.{call_name}.",
            f"__output_param.{call_name}.",
        )
        result: dict[int, str] = {}
        for key, value in values.items():
            for prefix in prefixes:
                if key.startswith(prefix):
                    raw = key[len(prefix):]
                    if raw.isdigit():
                        result[int(raw)] = str(value)
                    break
        return result

    @staticmethod
    def _cpputest_output_param_chain(
        symbol: StubSymbol | None,
        output_args: dict[int, str],
        safe_name: str,
        indent: str = "",
    ) -> tuple[list[str], str]:
        if symbol is None or not output_args:
            return [], ""
        params = StubHarnessEngine._definition_params(symbol.params)
        if params == "void":
            return [], ""
        declarations: list[str] = []
        parts: list[str] = []
        pointer_types = TestBackendGenerator._pointer_pointee_types(symbol.params)
        for index, raw in enumerate(StubHarnessEngine._split_params(params)):
            if index not in output_args or index not in pointer_types:
                continue
            name = StubHarnessEngine._param_name(raw, index) or f"p{index}"
            pointee_type = pointer_types[index]
            local = f"testing_mate_mock_out_{safe_name}_{index}"
            declarations.append(f"{indent}{pointee_type} {local} = {TestBackendGenerator._typed_value(pointee_type, str(output_args[index]))};")
            parts.append(f'.withOutputParameterReturning("{name}", &{local}, sizeof({local}))')
        return declarations, "".join(parts)

    @staticmethod
    def _cpputest_expected_param_chain(symbol: StubSymbol | None, expected_args: dict[int, str]) -> str:
        if symbol is None or not expected_args:
            return ""
        params = StubHarnessEngine._definition_params(symbol.params)
        if params == "void":
            return ""
        parts: list[str] = []
        for index, raw in enumerate(StubHarnessEngine._split_params(params)):
            if index not in expected_args:
                continue
            name = StubHarnessEngine._param_name(raw, index) or f"p{index}"
            ctype = StubHarnessEngine._param_type(raw, name)
            call = TestBackendGenerator._cpputest_expected_param_call(ctype, name, expected_args[index])
            if call:
                parts.append(call)
        return "".join(parts)

    @staticmethod
    def _cpputest_expected_param_call(ctype: str, name: str, value: str) -> str:
        clean = " ".join((ctype or "").replace("const", "").replace("volatile", "").split()).strip()
        lowered = clean.lower()
        if "*" in clean:
            if str(value).strip() not in {"0", "NULL", "nullptr"} and not str(value).strip().startswith("&"):
                return ""
            return f'.withPointerParameter("{name}", (void *){value})'
        if lowered in {"float", "double"}:
            return f'.withDoubleParameter("{name}", (double)({value}))'
        if "unsigned" in lowered or re.fullmatch(r"u(?:int)?(?:8|16|32|64)(?:_t)?", lowered):
            return f'.withUnsignedIntParameter("{name}", (unsigned int)({value}))'
        if "long" in lowered or re.fullmatch(r"int(?:32|64)(?:_t)?", lowered):
            return f'.withLongIntParameter("{name}", (long int)({value}))'
        return f'.withIntParameter("{name}", (int)({value}))'

    @staticmethod
    def _has_mock_params(params: str) -> bool:
        trimmed = str(params or "").strip()
        return bool(trimmed and trimmed not in {"void", "..."})

    @staticmethod
    def _mock_return_type(return_type: str) -> str:
        return StubHarnessEngine._mock_return_type(return_type or "int")

    @staticmethod
    def _stub_call_assertion_lines(backend: str, call_names: list[str], indent: str = "    ") -> list[str]:
        lines: list[str] = []
        for name in call_names:
            safe = TestBackendGenerator._safe_identifier(name)
            accessor = f"TestingMate_{safe}_CallCount()"
            if backend == "CppUTest":
                lines.append(f"{indent}CHECK({accessor} > 0U);")
            else:
                lines.append(f"{indent}EXPECT_GT({accessor}, 0U);")
        return lines

    @staticmethod
    def _state_teardown_lines(
        case: TestCase,
        required_globals: set[str],
        local_teardown_names: list[str],
        return_local_name: str = "",
        indent: str = "",
    ) -> list[str]:
        values = case.values or {}
        explicit_resets: dict[str, str] = {}
        for key, value in sorted(values.items()):
            for prefix in ("__teardown_global.", "__reset_global.", "teardown.global.", "reset.global."):
                if key.startswith(prefix):
                    symbol = key[len(prefix) :]
                    if TestBackendGenerator._is_c_identifier(symbol):
                        explicit_resets[symbol] = str(value)
                    break
        lines: list[str] = []
        for symbol in sorted(required_globals | set(explicit_resets)):
            reset_value = explicit_resets.get(symbol, "0")
            if TestBackendGenerator._is_c_identifier(symbol):
                lines.append(f"{indent}{symbol} = {reset_value};")
        for name in [*local_teardown_names, *([return_local_name] if return_local_name else [])]:
            if TestBackendGenerator._is_c_identifier(name):
                lines.append(f"{indent}{name} = {{}};")
        lines.append(f"{indent}TestingMate_ResetStubs();")
        return lines

    @staticmethod
    def _case_local_setup_lines(case: TestCase, local_teardown_names: list[str], indent: str = "") -> list[str]:
        values = case.values or {}
        lines: list[str] = []
        for key, value in sorted(values.items()):
            local_name = ""
            for prefix in ("__local.", "local.", "__var.", "var.", "__setup_local.", "setup.local."):
                if key.startswith(prefix):
                    local_name = key[len(prefix) :]
                    break
            if local_name and TestBackendGenerator._is_c_identifier(local_name):
                # Local variables are fixture members. Do not declare them inside TEST bodies.
                ctype = TestBackendGenerator._fixture_type_from_value(str(value))
                lines.append(f"{indent}{local_name} = {TestBackendGenerator._typed_value(ctype, str(value))};")
                local_teardown_names.append(local_name)
        return lines

    @staticmethod
    def _fixture_local_key_name(key: str) -> str:
        for prefix in ("__local.", "local.", "__var.", "var.", "__setup_local.", "setup.local."):
            if key.startswith(prefix):
                return key[len(prefix) :]
        return ""

    @staticmethod
    def _fixture_type_from_value(value: str) -> str:
        value = str(value or "").strip()
        if value in {"true", "false", "TRUE", "FALSE"}:
            return "bool"
        if re.fullmatch(r"[-+]?0[xX][0-9A-Fa-f]+[uUlL]*", value):
            return "long long"
        if re.fullmatch(r"[-+]?\d+[uUlL]*", value):
            return "long long"
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?[fFlL]?", value):
            return "double"
        if re.fullmatch(r'"(?:\\.|[^"\\])*"', value):
            return "const char *"
        if value in {"NULL", "nullptr"}:
            return "void *"
        return "long long"

    @staticmethod
    def _suite_local_fixture_declarations(cases: list[TestCase], fn: FunctionInfo | None = None) -> dict[str, str]:
        """Return explicit local fixture variables requested by a testcase.

        Function-scope locals from the implementation are not visible through the
        public header in generated CppUTest/GTest translation units. Declaring all
        parsed locals here can leak file-local typedefs into the harness and break
        compilation, so only materialize locals requested by explicit testcase or
        FixIt metadata.
        """
        locals_: dict[str, str] = {}

        for case in cases:
            for name, value in TestBackendGenerator._fixit_local_literals(fn, case).items():
                if name and TestBackendGenerator._is_c_identifier(name) and name not in locals_:
                    locals_[name] = TestBackendGenerator._fixture_type_from_value(str(value))
            for key, value in (case.values or {}).items():
                name = TestBackendGenerator._fixture_local_key_name(key)
                if name and TestBackendGenerator._is_c_identifier(name) and name not in locals_:
                    locals_[name] = TestBackendGenerator._fixture_type_from_value(str(value))
        return locals_

    @staticmethod
    def _suite_global_reset_value(cases: list[TestCase], symbol: str) -> str:
        for case in cases:
            for prefix in ("__teardown_global.", "__reset_global.", "teardown.global.", "reset.global."):
                key = f"{prefix}{symbol}"
                if key in (case.values or {}):
                    return str(case.values[key])
        return "0"

    @staticmethod
    def _suite_required_outputs(
        cases: list[TestCase],
        pointer_writes: dict[str, set[str]],
        pointer_fixtures: dict[str, str],
    ) -> set[str]:
        required: set[str] = set()
        for param_name, suffixes in pointer_writes.items():
            if param_name in pointer_fixtures:
                required.update(f"{param_name}{suffix}" for suffix in suffixes)
        for case in cases:
            required.update(TestBackendGenerator._case_required_outputs(case, pointer_writes, pointer_fixtures))
        return required

    @staticmethod
    def _case_required_outputs(
        case: TestCase,
        pointer_writes: dict[str, set[str]],
        pointer_fixtures: dict[str, str],
    ) -> set[str]:
        required: set[str] = set()
        for param_name, suffixes in pointer_writes.items():
            if param_name in pointer_fixtures:
                required.update(f"{param_name}{suffix}" for suffix in suffixes)
        for output_name in TestBackendGenerator._expected_output_literals(case):
            root = output_name.split(".", 1)[0]
            if root in pointer_fixtures:
                required.add(output_name)
        return required

    def _case_expected_precondition_lines(
        self,
        case: TestCase,
        return_type: str,
        expected_return_name: str,
        required_outputs: set[str],
        expected_output_fixtures: dict[str, str],
        param_by_name: dict[str, Parameter],
        inferred_expectations: dict | None = None,
        expected_global_fixtures: dict[str, str] | None = None,
        global_type_map: dict[str, str] | None = None,
        indent: str = "",
        struct_field_types: dict[str, dict[str, str]] | None = None,
    ) -> list[str]:
        lines: list[str] = []
        inferred_expectations = inferred_expectations or {}
        expected_global_fixtures = expected_global_fixtures or {}
        global_type_map = global_type_map or {}

        if expected_return_name:
            explicit_return = self._expected_return_literal(case)
            inferred_return = str(inferred_expectations.get("return") or "")
            expected_return = explicit_return
            if expected_return and not self._is_safe_expected_expression_for_harness(expected_return, case.values or {}, param_by_name):
                # Do not leak implementation-local variables such as `ret`, `status`,
                # or `result` into the external GTest/CppUTest translation unit.
                # Prefer the path-aware inferred literal when available.
                expected_return = ""
            if not expected_return:
                expected_return = inferred_return
            if expected_return and not self._is_safe_expected_expression_for_harness(expected_return, case.values or {}, param_by_name):
                expected_return = ""
            if not expected_return:
                expected_return = self._default_expected_literal(return_type)
            expected_return = self._harness_expression_for_expected(expected_return, case.values or {}, param_by_name)
            lines.append(f"{indent}{expected_return_name} = {self._typed_value(return_type, expected_return)};")

        inferred_outputs = inferred_expectations.get("outputs") or {}
        expected_outputs = {**inferred_outputs, **self._expected_output_literals(case)}
        reset_output_roots: set[str] = set()
        for output_name in sorted(required_outputs):
            normalized = self._normalize_observable_name(output_name)
            root, _, suffix = normalized.partition(".")
            expected_fixture = expected_output_fixtures.get(root)
            param = param_by_name.get(root)
            if not expected_fixture or param is None:
                continue
            output_type = self._pointer_fixture_type(param)
            if root not in reset_output_roots:
                lines.append(f"{indent}{expected_fixture} = {{}};")
                reset_output_roots.add(root)
            expected_value = expected_outputs.get(normalized, "")
            if not expected_value:
                expected_value = self._default_expected_literal(output_type if not suffix else "")
            expected_value = self._harness_expression_for_expected(str(expected_value), case.values or {}, param_by_name)
            if suffix:
                field_type = self._field_type_for_path(output_type, suffix, struct_field_types or {})
                lines.append(f"{indent}{expected_fixture}.{suffix} = {self._typed_field_value(field_type, expected_value)};")
            else:
                lines.append(f"{indent}{expected_fixture} = {self._typed_value(output_type, expected_value)};")

        inferred_globals = inferred_expectations.get("globals") or {}
        expected_globals = {**inferred_globals, **self._expected_global_literals(case)}
        for global_name, expected_value in sorted(expected_globals.items()):
            expected_fixture = expected_global_fixtures.get(global_name)
            if not expected_fixture:
                continue
            expected_expr = self._harness_expression_for_expected(str(expected_value), case.values or {}, param_by_name)
            global_type = global_type_map.get(global_name) or self._fixture_type_from_value(str(expected_expr))
            lines.append(f"{indent}{expected_fixture} = {self._typed_value(global_type, expected_expr)};")
        return lines

    def _case_expected_assertion_lines(
        self,
        backend: str,
        case: TestCase,
        fn: FunctionInfo,
        return_type: str,
        expected_return_name: str,
        actual_return_name: str,
        required_outputs: set[str],
        expected_output_fixtures: dict[str, str],
        pointer_fixtures: dict[str, str],
        param_by_name: dict[str, Parameter],
        inferred_expectations: dict | None = None,
        expected_global_names: set[str] | None = None,
        expected_global_fixtures: dict[str, str] | None = None,
        global_type_map: dict[str, str] | None = None,
        indent: str = "",
        struct_field_types: dict[str, dict[str, str]] | None = None,
    ) -> list[str]:
        lines: list[str] = []
        inferred_expectations = inferred_expectations or {}
        expected_global_fixtures = expected_global_fixtures or {}
        global_type_map = global_type_map or {}
        if expected_return_name and actual_return_name:
            lines.extend(self._assert_observable_equal_lines(backend, expected_return_name, actual_return_name, return_type, struct_field_types, indent))

        expected_outputs = {**(inferred_expectations.get("outputs") or {}), **self._expected_output_literals(case)}
        for output_name in sorted(required_outputs):
            normalized = self._normalize_observable_name(output_name)
            if normalized not in expected_outputs:
                continue
            actual = self._output_actual_expression(normalized, pointer_fixtures)
            expected = self._output_expected_expression(normalized, expected_output_fixtures)
            if not actual or not expected:
                continue
            root, _, suffix = normalized.partition(".")
            param = param_by_name.get(root)
            ctype = self._pointer_fixture_type(param) if param else ""
            if suffix:
                ctype = self._field_type_for_path(ctype, suffix, struct_field_types or {})
            lines.extend(self._assert_observable_equal_lines(backend, expected, actual, ctype, struct_field_types, indent))

        expected_globals = {**(inferred_expectations.get("globals") or {}), **self._expected_global_literals(case)}
        if expected_global_names is not None:
            expected_globals = {name: value for name, value in expected_globals.items() if name in expected_global_names}
        for global_name in sorted(expected_globals):
            fixture = expected_global_fixtures.get(global_name)
            if not fixture:
                continue
            ctype = global_type_map.get(global_name, "")
            lines.extend(self._assert_observable_equal_lines(backend, fixture, global_name, ctype, struct_field_types, indent))
        return lines

    def _case_precondition_lines(
        self,
        case: TestCase,
        fn: FunctionInfo,
        pointer_writes: dict[str, set[str]],
        pointer_fixtures: dict[str, str],
        pointer_arg_names: dict[str, str],
        indent: str = "",
        struct_field_types: dict[str, dict[str, str]] | None = None,
        backend: str = "",
        stubbed_calls: set[str] | None = None,
        stub_symbols: dict[str, StubSymbol] | None = None,
        global_type_map: dict[str, str] | None = None,
        blocked_global_names: set[str] | None = None,
    ) -> list[str]:
        values = {**self._fixit_input_literals(fn, case), **(case.values or {})}
        lines: list[str] = []
        lines.extend(self._state_setup_lines(values, indent, global_type_map, blocked_global_names))
        # expected_calls = self._case_stub_call_names(fn, case, stubbed_calls or set())
        # if expected_calls:
        #     lines.append(f"{indent}/* MOCK EXPECTATIONS */")
        # lines.extend(self._stub_expectation_setup_lines(
        #     backend,
        #     expected_calls,
        #     values,
        #     stub_symbols or {},
        #     indent,
        # ))

        for key, value in sorted(values.items()):
            local_name = self._fixture_local_key_name(key)
            if local_name and self._is_c_identifier(local_name):
                ctype = self._fixture_type_from_value(str(value))
                lines.append(f"{indent}{local_name} = {self._typed_value(ctype, str(value))};")

        for param in fn.parameters:
            value = str(values.get(param.name, TestVectorGenerator._nominal_value(param)))
            if param.kind == "pointer":
                fixture_name = pointer_fixtures.get(param.name, f"tm_{self._safe_identifier(param.name)}")
                arg_name = pointer_arg_names.get(param.name, f"tm_arg_{self._safe_identifier(param.name)}")
                if value in {"0", "NULL", "nullptr"}:
                    lines.append(f"{indent}{arg_name} = 0;")
                else:
                    lines.append(f"{indent}{arg_name} = &{fixture_name};")
                    fixture_type = self._pointer_fixture_type(param)
                    pointee_initial = self._explicit_pointer_initial_value(values, param.name)
                    if pointee_initial is None and param.name not in pointer_writes and value not in {"1", "true", "TRUE", "non_null", "NON_NULL"}:
                        pointee_initial = value
                    if pointee_initial is not None and self._can_initialize_fixture_from_literal(fixture_type, pointee_initial):
                        lines.append(f"{indent}{fixture_name} = {self._typed_value(fixture_type, pointee_initial)};")
                    for field_key, field_value in sorted(values.items()):
                        prefix = f"{param.name}."
                        if field_key.startswith(prefix):
                            field_name = field_key[len(prefix) :]
                            if self._is_expected_key(field_name):
                                continue
                            if type(field_value) in {int}:
                                field_value = f"{field_value}u"
                            elif type(field_value) in {float}:
                                field_value = f"{field_value}f"
                            field_type = self._field_type_for_path(fixture_type, field_name, struct_field_types or {})
                            lines.append(f"{indent}{fixture_name}.{field_name} = {self._typed_field_value(field_type, str(field_value))};")
            else:
                local_name = f"tm_{self._safe_identifier(param.name)}"
                local_type = self._param_local_type(param)
                lines.append(f"{indent}{local_name} = {self._typed_value(local_type, value)};")
        return lines

    @staticmethod
    def _suite_lifecycle_global_names(cases: list[TestCase], global_writes: set[str]) -> set[str]:
        names: set[str] = set(global_writes)
        for case in cases:
            names.update(TestBackendGenerator._expected_global_literals(case).keys())
            names.update(TestBackendGenerator._fixit_global_literals(None, case).keys())
            names.update(TestBackendGenerator._fixit_expected_global_literals(None, case).keys())
            values = {**TestBackendGenerator._fixit_input_literals(None, case), **(case.values or {})}
            for key in values:
                for prefix in (
                    "__global.",
                    "__set_global.",
                    "__input_global.",
                    "__setup_global.",
                    "setup.global.",
                    "__teardown_global.",
                    "__reset_global.",
                    "teardown.global.",
                    "reset.global.",
                ):
                    if key.startswith(prefix):
                        symbol = key[len(prefix) :]
                        if TestBackendGenerator._is_c_identifier(symbol):
                            names.add(symbol)
                        break
        return names

    @staticmethod
    def _explicit_pointer_initial_value(case: TestCase | dict, param_name: str) -> str | None:
        values = case if isinstance(case, dict) else (case.values or {})
        keys = (
            f"__input_pointer.{param_name}",
            f"__input_output.{param_name}",
            f"__initial_output.{param_name}",
            f"input.{param_name}",
            f"initial.{param_name}",
            f"{param_name}.__input",
            f"{param_name}.input",
            f"{param_name}.__initial",
            f"{param_name}.initial",
        )
        for key in keys:
            if key in values:
                return str(values[key])
        return None

    @staticmethod
    def _param_local_type(param: Parameter) -> str:
        ctype = TestBackendGenerator._clean_fixture_type(param.ctype or "uint8", defaults="uint8", mutable=True)
        if param.kind == "pointer":
            ctype = ctype.replace("*", "").strip()
        return ctype or "uint8"

    @staticmethod
    def _return_fixture_type(fn: FunctionInfo) -> str:
        return TestBackendGenerator._clean_fixture_type(fn.return_type or "uint8", defaults="uint8", mutable=True)

    @staticmethod
    def _pointer_argument_type(param: Parameter, fixture_type: str) -> str:
        ctype = TestBackendGenerator._clean_source_type(param.ctype or "", defaults="")
        if "*" in ctype:
            return ctype
        return f"{fixture_type} *"

    @staticmethod
    def _clean_source_type(ctype: str, defaults: str = "uint8") -> str:
        clean = " ".join((ctype or defaults).replace("\n", " ").split()).strip()
        clean = re.sub(r"\b(?:extern|static|inline|__inline|__inline__|register)\b", " ", clean)
        clean = re.sub(r"__attribute__\s*\(\(.*?\)\)", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean or defaults

    @staticmethod
    def _clean_fixture_type(ctype: str, defaults: str = "uint8", mutable: bool = True) -> str:
        clean = TestBackendGenerator._clean_source_type(ctype, defaults=defaults)
        if mutable:
            # Fixture variables are assigned in PRECONDITION/SetUp, so remove top-level qualifiers.
            # The underlying datatype still comes from the analyzed source/header.
            clean = re.sub(r"\b(?:const|volatile)\b", " ", clean)
            clean = re.sub(r"\s+", " ", clean).strip()
        return clean or defaults

    @staticmethod
    def _is_valid_fixture_type(ctype: str) -> bool:
        clean = " ".join((ctype or "").split()).strip()
        if not clean or ":" in clean or ";" in clean or "{" in clean or "}" in clean:
            return False
        forbidden = {
            "if", "else", "for", "while", "switch", "case", "default",
            "return", "break", "continue", "goto", "do", "sizeof",
        }
        tokens = [token for token in re.split(r"[\s*&]+", clean) if token]
        if not tokens or tokens[0] in forbidden:
            return False
        return all(re.fullmatch(r"[A-Za-z_]\w*|::|<|>|,", token) for token in tokens)

    @staticmethod
    def _default_expected_literal(ctype: str) -> str:
        clean = " ".join((ctype or "").replace("const", "").replace("volatile", "").split()).strip()
        if not clean:
            return "0"
        lowered = clean.lower()
        if "*" in clean:
            return "0"
        if lowered in {"bool", "_bool", "boolean"}:
            return "false"
        if TestBackendGenerator._is_probably_aggregate_type(clean):
            return "{}"
        return "0"

    @staticmethod
    def _typed_value(ctype: str, value: str) -> str:
        value = str(value).strip()
        if value in {"{}", "{0}"}:
            return value
        clean_type = " ".join((ctype or "").replace("const", "").replace("volatile", "").split()).strip()
        if not clean_type:
            return value or "{}"
        if value == "":
            return "{}"
        return f"({clean_type})({value})"

    @staticmethod
    def _is_void_return_type(return_type: str) -> bool:
        # Empty return_type means the analyzer could not recover the type. Treat it as
        # non-void so the harness still declares/initializes actual_return/expected_return.
        clean = " ".join((return_type or "").replace("static", "").split()).strip()
        clean = StubHarnessEngine._actual_return_type(clean)
        clean = " ".join((clean or "").split()).strip()
        return clean == "void"

    @staticmethod
    def _assert_equal_lines(backend: str, expected: str, actual: str, indent: str = "    ") -> list[str]:
        if backend == "CppUTest":
            return [f"{indent}CHECK_EQUAL({expected}, {actual});"]
        return [f"{indent}EXPECT_EQ({expected}, {actual});"]

    @staticmethod
    def _assert_observable_equal_lines(
        backend: str,
        expected: str,
        actual: str,
        ctype: str = "",
        struct_field_types: dict[str, dict[str, str]] | None = None,
        indent: str = "    ",
    ) -> list[str]:
        # Use value equality for scalar-like C types and byte equality for aggregate outputs.
        # Aggregate byte compare keeps generated tests buildable for C structs without operator==.
        if struct_field_types is not None:
            is_aggregate = TestBackendGenerator._struct_lookup_type(ctype) in struct_field_types
        else:
            is_aggregate = TestBackendGenerator._is_probably_aggregate_type(ctype)
        if is_aggregate:
            if backend == "CppUTest":
                return [f"{indent}MEMCMP_EQUAL(&{expected}, &{actual}, sizeof({expected}));"]
            return [f"{indent}EXPECT_EQ(0, memcmp(&{expected}, &{actual}, sizeof({expected})));"]
        return TestBackendGenerator._assert_equal_lines(backend, expected, actual, indent)

    @staticmethod
    def _is_probably_aggregate_type(ctype: str) -> bool:
        clean = " ".join((ctype or "").replace("const", "").replace("volatile", "").split()).strip()
        if not clean or "*" in clean:
            return False
        lowered = clean.lower()
        if lowered.startswith(("struct ", "union ")):
            return True
        
        if clean in SCALAR_TOKENS or lowered in SCALAR_TOKENS:
            return False
        # Unknown typedef names may be scalar, but AUTOSAR/C output parameters are often struct typedefs.
        # Be conservative for whole-output comparison; field-level checks still use value equality.
        return bool(re.fullmatch(r"[A-Za-z_]\w*", clean))

    @classmethod
    def _infer_case_expected_literals_from_ast_or_source(
        cls,
        fn: FunctionInfo,
        case: TestCase,
        global_writes: set[str] | None = None,
    ) -> dict[str, dict[str, str] | str]:
        """Infer reviewable expected values from a stored AST when available.

        The generator never invents semantic evidence. This helper handles only
        simple deterministic forms such as `return 0`, `return E_OK`,
        `if (x == 1) return A; else return B`, `g_state = 2`, `g_state++`,
        `*out = 3`, and `out->field = value`.  It first tries any AST node
        attached to FunctionInfo, then uses the function body text as a fallback
        for analyzers that do not persist AST nodes yet.
        """
        global_writes = set(global_writes or set())
        values = cls._case_symbol_values(case)
        body = cls._strip_comments_and_strings(getattr(fn, "body", "") or "")
        local_values = cls._constant_local_value_map(body, values)
        eval_values = {**local_values, **values}

        inferred_return = cls._infer_return_from_stored_ast(fn, body, eval_values)
        if not inferred_return:
            inferred_return = cls._infer_return_from_body(body, eval_values)

        inferred_globals = cls._infer_global_expected_from_stored_ast(fn, body, global_writes, eval_values)
        if not inferred_globals:
            inferred_globals = cls._infer_global_expected_from_body(body, global_writes, eval_values)

        inferred_outputs = cls._infer_output_expected_from_body(body, fn, eval_values)
        result: dict[str, dict[str, str] | str] = {
            "globals": inferred_globals,
            "outputs": inferred_outputs,
        }
        if inferred_return:
            result["return"] = inferred_return
        return result

    @classmethod
    def _case_symbol_values(cls, case: TestCase) -> dict[str, str]:
        result: dict[str, str] = {}
        result.update(cls._fixit_input_literals(None, case))
        for key, value in (getattr(case, "values", {}) or {}).items():
            text_value = str(value)
            if cls._is_c_identifier(key) and not key.startswith("__"):
                result[key] = text_value
                continue
            for prefix in (
                "__global.", "__set_global.", "__input_global.", "__setup_global.", "setup.global.",
                "__local.", "local.", "__var.", "var.", "__setup_local.", "setup.local.",
            ):
                if key.startswith(prefix):
                    name = key[len(prefix):]
                    if cls._is_c_identifier(name):
                        result[name] = text_value
                    break
        return result

    @classmethod
    def _constant_local_value_map(cls, body: str, values: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        declaration = re.compile(
            r"\b(?:const\s+|volatile\s+|static\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|"
            r"char\s+|int\s+|float\s+|double\s+|bool\s+|uint\d+_t\s+|int\d+_t\s+|size_t\s+|[A-Za-z_]\w+_t\s+|[A-Za-z_]\w+\s+)+[*\s]*"
            r"(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<expr>[^;]+);"
        )
        for match in declaration.finditer(body):
            name = match.group("name")
            literal = cls._resolve_expected_expression(match.group("expr"), {**values, **result})
            if literal:
                result[name] = literal
        return result

    @classmethod
    def _infer_return_from_stored_ast(cls, fn: FunctionInfo, body: str, values: dict[str, str]) -> str:
        ast_node = cls._stored_function_ast(fn)
        if not ast_node:
            return ""
        # Clang range offsets are usually relative to the full parsed source, not
        # FunctionInfo.body. Use the persisted source text when the analyzer stored it.
        source_text = str(getattr(fn, "ast_source_text", "") or getattr(fn, "source_text", "") or body)
        returns: list[str] = []
        for node in cls._walk_ast(ast_node):
            if not isinstance(node, dict) or node.get("kind") != "ReturnStmt":
                continue
            expr = cls._ast_node_text(node, source_text)
            expr = re.sub(r"^return\s+", "", expr).rstrip("; ")
            if not expr:
                expr = cls._ast_return_expr_text(node, source_text)
            literal = cls._resolve_expected_expression(expr, values)
            if literal:
                returns.append(literal)
        return returns[0] if returns and len(set(returns)) == 1 else ""

    @classmethod
    def _infer_return_from_body(cls, body: str, values: dict[str, str]) -> str:
        selected = cls._select_if_else_return(body, values)
        if selected:
            return selected
        selected_local = cls._select_local_return_assignment(body, values)
        if selected_local:
            return selected_local
        returns: list[str] = []
        for match in re.finditer(r"\breturn\s+([^;]+);", body):
            literal = cls._resolve_expected_expression(match.group(1), values)
            if literal:
                returns.append(literal)
        return returns[0] if returns and len(set(returns)) == 1 else ""

    @classmethod
    def _select_if_else_return(cls, body: str, values: dict[str, str]) -> str:
        pattern = re.compile(
            r"if\s*\((?P<cond>[^(){};]+)\)\s*(?:\{\s*)?return\s+(?P<then>[^;{}]+);\s*(?:\}\s*)?"
            r"(?:else\s*(?:\{\s*)?return\s+(?P<else>[^;{}]+);)?",
            re.S,
        )
        for match in pattern.finditer(body):
            verdict = cls._eval_simple_condition(match.group("cond"), values)
            if verdict is True:
                literal = cls._resolve_expected_expression(match.group("then"), values)
                if literal:
                    return literal
            if verdict is False and match.group("else") is not None:
                literal = cls._resolve_expected_expression(match.group("else"), values)
                if literal:
                    return literal
        return ""

    @classmethod
    def _select_local_return_assignment(cls, body: str, values: dict[str, str]) -> str:
        """Resolve simple `ret = ...; ... return ret;` idioms path-sensitively."""
        return_matches = list(re.finditer(r"\breturn\s+([A-Za-z_]\w*)\s*;", body))
        if not return_matches:
            return ""
        ret_match = return_matches[-1]
        local_name = ret_match.group(1)
        # Upper-case identifiers are more likely direct macro/enum returns than locals.
        if local_name.isupper():
            return ""
        prefix_body = body[: ret_match.start()]
        selected = cls._select_if_else_assignment_with_prefix(prefix_body, local_name, values)
        if selected:
            return selected
        # Fall back to the last straight-line assignment before `return ret;`.
        return cls._last_assignment_before(prefix_body, local_name, len(prefix_body), values)

    @classmethod
    def _select_if_else_assignment_with_prefix(cls, body: str, name: str, values: dict[str, str]) -> str:
        escaped = re.escape(name)
        pattern = re.compile(
            rf"if\s*\((?P<cond>[^(){{}};]+)\)\s*"
            rf"(?P<then_block>\{{(?P<then_body>.*?)\}}|(?P<then_stmt>(?:(?!else).)*?;))\s*"
            rf"(?:else\s*(?P<else_block>\{{(?P<else_body>.*?)\}}|(?P<else_stmt>.*?;)))?",
            re.S,
        )
        for match in pattern.finditer(body):
            then_text = match.group("then_body") if match.group("then_body") is not None else (match.group("then_stmt") or "")
            else_text = match.group("else_body") if match.group("else_body") is not None else (match.group("else_stmt") or "")
            then_value = cls._last_assignment_before(then_text, name, len(then_text), values)
            else_value = cls._last_assignment_before(else_text, name, len(else_text), values) if else_text else ""
            if not then_value and not else_value:
                continue
            verdict = cls._eval_simple_condition(match.group("cond"), values)
            if verdict is True and then_value:
                return then_value
            if verdict is False:
                if else_value:
                    return else_value
                prefix_value = cls._last_assignment_before(body, name, match.start(), values)
                if prefix_value:
                    return prefix_value
        return ""

    @classmethod
    def _last_assignment_before(cls, body: str, name: str, pos: int, values: dict[str, str]) -> str:
        escaped = re.escape(name)
        text = body[:pos]
        assignments: list[str] = []
        # Declaration initializer: Std_ReturnType ret = E_NOT_OK;
        decl_pattern = re.compile(
            rf"\b(?:const\s+|volatile\s+|static\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|"
            rf"char\s+|int\s+|float\s+|double\s+|bool\s+|uint\d+_t\s+|int\d+_t\s+|size_t\s+|[A-Za-z_]\w+_t\s+|[A-Za-z_]\w+\s+)+[*\s]*"
            rf"\b{escaped}\b\s*=\s*([^;]+);"
        )
        for match in decl_pattern.finditer(text):
            literal = cls._resolve_expected_expression(match.group(1), values)
            if literal:
                assignments.append(literal)
        for match in re.finditer(rf"(?<![.>])\b{escaped}\b\s*=\s*([^;]+);", text):
            literal = cls._resolve_expected_expression(match.group(1), values)
            if literal:
                assignments.append(literal)
        return assignments[-1] if assignments else ""

    @classmethod
    def _infer_global_expected_from_stored_ast(
        cls, fn: FunctionInfo, body: str, global_writes: set[str], values: dict[str, str]
    ) -> dict[str, str]:
        # AST nodes in current Clang JSON do not always retain exact assignment
        # text in FunctionInfo. Use AST presence as a trigger, then reuse the
        # same expression resolver on the source body for deterministic cases.
        if not cls._stored_function_ast(fn):
            return {}
        return cls._infer_global_expected_from_body(body, global_writes, values)

    @classmethod
    def _infer_global_expected_from_body(cls, body: str, global_writes: set[str], values: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for name in sorted(global_writes):
            if not cls._is_c_identifier(name):
                continue
            escaped = re.escape(name)
            conditional = cls._select_if_else_assignment(body, name, values)
            if conditional:
                result[name] = conditional
                continue
            assignments: list[str] = []
            for match in re.finditer(rf"(?<![.>])\b{escaped}\b\s*=\s*([^;]+);", body):
                literal = cls._resolve_expected_expression(match.group(1), values)
                if literal:
                    assignments.append(literal)
            for match in re.finditer(rf"(?<![.>])\b{escaped}\b\s*(\+\+|--);|(?<![.>])(\+\+|--)\s*\b{escaped}\b", body):
                base = values.get(name, "0")
                numeric = cls._literal_to_number(base)
                if numeric is None:
                    continue
                op = match.group(1) or match.group(2)
                assignments.append(str(int(numeric + (1 if op == "++" else -1))))
            if assignments and len(set(assignments)) == 1:
                result[name] = assignments[-1]
        return result

    @classmethod
    def _select_if_else_assignment(cls, body: str, name: str, values: dict[str, str]) -> str:
        escaped = re.escape(name)
        pattern = re.compile(
            rf"if\s*\((?P<cond>[^(){{}};]+)\)\s*(?:\{{\s*)?(?<![.>])\b{escaped}\b\s*=\s*(?P<then>[^;{{}}]+);\s*(?:\}}\s*)?"
            rf"(?:else\s*(?:\{{\s*)?(?<![.>])\b{escaped}\b\s*=\s*(?P<else>[^;{{}}]+);)?",
            re.S,
        )
        for match in pattern.finditer(body):
            verdict = cls._eval_simple_condition(match.group("cond"), values)
            if verdict is True:
                return cls._resolve_expected_expression(match.group("then"), values)
            if verdict is False and match.group("else") is not None:
                return cls._resolve_expected_expression(match.group("else"), values)
        return ""

    @classmethod
    def _infer_output_expected_from_body(cls, body: str, fn: FunctionInfo, values: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for param in getattr(fn, "parameters", []) or []:
            if getattr(param, "kind", "") != "pointer":
                continue
            name = re.escape(param.name)
            for match in re.finditer(rf"\*\s*{name}\s*=\s*([^;]+);", body):
                literal = cls._resolve_expected_expression(match.group(1), values)
                if literal:
                    result[param.name] = literal
            for match in re.finditer(rf"\b{name}\s*->\s*([A-Za-z_]\w*)\s*=\s*([^;]+);", body):
                literal = cls._resolve_expected_expression(match.group(2), values)
                if literal:
                    result[f"{param.name}.{match.group(1)}"] = literal
            for match in re.finditer(rf"\b{name}\s*\[\s*0\s*\]\s*=\s*([^;]+);", body):
                literal = cls._resolve_expected_expression(match.group(1), values)
                if literal:
                    result[param.name] = literal
        return result

    @classmethod
    def _resolve_expected_expression(cls, expr: str, values: dict[str, str]) -> str:
        expr = (expr or "").strip().strip("() ")
        expr = re.sub(r"/\*.*?\*/", " ", expr, flags=re.S)
        expr = re.sub(r"//.*", " ", expr).strip()
        literal = cls._cpp_expected_literal(expr)
        if literal:
            if cls._is_c_identifier(literal):
                if literal in values:
                    return cls._cpp_expected_literal(values[literal]) or str(values[literal])
                # Keep probable enum/macro constants, but do not emit undeclared
                # internal locals such as `ret` or `status` into the test body.
                if literal.isupper() or re.match(r"(?:E_|STD_|RTE_|DEM_|DCM_|COM_|CAN_|LIN_)", literal):
                    return literal
                return ""
            return literal
        if cls._is_c_identifier(expr) and expr in values:
            return cls._cpp_expected_literal(values[expr]) or str(values[expr])
        # Handle simple arithmetic with testcase symbols, e.g. `x + 1`.
        resolved = expr
        for name, value in sorted(values.items(), key=lambda item: len(item[0]), reverse=True):
            if cls._is_c_identifier(name) and cls._cpp_expected_literal(str(value)):
                resolved = re.sub(rf"\b{re.escape(name)}\b", str(value), resolved)
        if re.fullmatch(r"[0-9xXa-fA-FuUlL+\-*/%()\s]+", resolved):
            try:
                cleaned = re.sub(r"(?<=\d)[uUlL]+\b", "", resolved)
                return str(int(eval(cleaned, {"__builtins__": {}}, {})))
            except Exception:
                return ""
        return ""

    @classmethod
    def _eval_simple_condition(cls, cond: str, values: dict[str, str]) -> bool | None:
        return ConditionEvaluator().evaluate(cond, values)

    @staticmethod
    def _literal_to_number(value: str) -> float | None:
        value = str(value or "").strip().strip("()")
        value = re.sub(r"(?<=\d)[uUlL]+\b", "", value)
        if value in {"true", "TRUE"}:
            return 1
        if value in {"false", "FALSE", "NULL", "nullptr"}:
            return 0
        try:
            return int(value, 0)
        except Exception:
            try:
                return float(value)
            except Exception:
                return None

    @classmethod
    def _stored_function_ast(cls, fn: FunctionInfo) -> dict | None:
        for attr in ("ast_node", "clang_ast_node", "function_ast", "ast", "body_ast"):
            node = getattr(fn, attr, None)
            if isinstance(node, dict):
                return node
        metadata = getattr(fn, "metadata", None)
        if isinstance(metadata, dict):
            for key in ("ast_node", "clang_ast_node", "function_ast", "ast", "body_ast"):
                node = metadata.get(key)
                if isinstance(node, dict):
                    return node
        return None

    @classmethod
    def _walk_ast(cls, node: dict):
        if isinstance(node, dict):
            yield node
            for child in node.get("inner", []) or []:
                if isinstance(child, dict):
                    yield from cls._walk_ast(child)

    @staticmethod
    def _ast_node_text(node: dict, source_text: str) -> str:
        if not isinstance(node, dict):
            return ""
        begin = ((node.get("range") or {}).get("begin") or {}).get("offset")
        end = ((node.get("range") or {}).get("end") or {}).get("offset")
        if isinstance(begin, int) and isinstance(end, int) and 0 <= begin <= end < len(source_text):
            return source_text[begin : end + 1].strip()
        return str(node.get("text") or node.get("spelling") or "")

    @classmethod
    def _ast_return_expr_text(cls, node: dict, source_text: str) -> str:
        for child in node.get("inner", []) or []:
            if isinstance(child, dict):
                text = cls._ast_node_text(child, source_text)
                if text:
                    return text
        return ""

    @staticmethod
    def _first_suite_expected_global_literal(cases: list[TestCase], inferred: dict[str, dict], symbol: str) -> str:
        for case in cases:
            explicit = TestBackendGenerator._expected_global_literals(case).get(symbol, "")
            if explicit:
                return explicit
            item = inferred.get(case.test_id, {}) if isinstance(inferred, dict) else {}
            value = (item.get("globals") or {}).get(symbol, "") if isinstance(item, dict) else ""
            if value:
                return str(value)
        return "0"

    @staticmethod
    def _global_variable_type_map(unit_sources: list[Path] | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for source in unit_sources or []:
            if not source.exists() or not source.is_file():
                continue
            try:
                text = source.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            sanitized = TestBackendGenerator._strip_comments_and_strings(text)
            sanitized = re.sub(r"^\s*#.*$", "", sanitized, flags=re.MULTILINE)
            depth = 0
            statement: list[str] = []
            for ch in sanitized:
                if ch == "{":
                    depth += 1
                    statement.clear()
                    continue
                if ch == "}":
                    depth = max(0, depth - 1)
                    statement.clear()
                    continue
                if depth != 0:
                    continue
                statement.append(ch)
                if ch == ";":
                    raw = "".join(statement).strip()
                    statement.clear()
                    parsed = TestBackendGenerator._parse_global_declaration(raw)
                    if parsed:
                        name, ctype = parsed
                        result.setdefault(name, ctype)
        return result

    def _struct_field_type_map(self, source: Path, unit_sources: list[Path] | None) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for path in self._type_map_sources(source, unit_sources):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            sanitized = self._strip_comments_and_strings(text)
            for match in re.finditer(
                r"typedef\s+(?:struct|union)\s*(?P<tag>[A-Za-z_]\w*)?\s*\{(?P<body>.*?)\}\s*(?P<alias>[A-Za-z_]\w*)\s*;",
                sanitized,
                re.S,
            ):
                fields = self._parse_struct_fields(match.group("body"))
                if not fields:
                    continue
                alias = match.group("alias")
                tag = match.group("tag") or ""
                result.setdefault(alias, fields)
                if tag:
                    result.setdefault(tag, fields)
                    result.setdefault(f"struct {tag}", fields)
            for match in re.finditer(r"\b(?:struct|union)\s+(?P<tag>[A-Za-z_]\w*)\s*\{(?P<body>.*?)\}\s*;", sanitized, re.S):
                fields = self._parse_struct_fields(match.group("body"))
                if fields:
                    tag = match.group("tag")
                    result.setdefault(tag, fields)
                    result.setdefault(f"struct {tag}", fields)
        return result

    def _type_map_sources(self, source: Path | str, unit_sources: list[Path | str] | None) -> list[Path]:
        roots = [self._as_path(source).expanduser(), *self._as_path_list(unit_sources)]
        result: list[Path] = []
        seen: set[str] = set()
        queue: list[tuple[Path, int]] = [(path, 0) for path in roots if path]
        while queue:
            path, depth = queue.pop(0)
            resolved = self._safe_resolve(path)
            key = str(resolved).lower()
            if key in seen or not path.exists() or not path.is_file():
                continue
            seen.add(key)
            result.append(path)
            if depth >= 3:
                continue
            for include in self._extract_includes(path):
                resolved_include = self._resolve_include(include, path.parent, roots)
                if resolved_include is not None:
                    queue.append((resolved_include, depth + 1))
        return result

    @staticmethod
    def _parse_struct_fields(body: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        statement: list[str] = []
        depth = 0
        for ch in body or "":
            if ch in "({[":
                depth += 1
            elif ch in ")}]":
                depth = max(0, depth - 1)
            statement.append(ch)
            if ch == ";" and depth == 0:
                raw = "".join(statement).strip()
                statement.clear()
                if ":" in raw:
                    raw = raw.split(":", 1)[0].strip() + ";"
                parsed = TestBackendGenerator._parse_global_declaration(raw)
                if parsed:
                    name, ctype = parsed
                    fields.setdefault(name, ctype)
        return fields

    @classmethod
    def _field_type_for_path(cls, parent_type: str, field_path: str, struct_field_types: dict[str, dict[str, str]]) -> str:
        current_type = cls._struct_lookup_type(parent_type)
        field_type = ""
        for field in (field_path or "").split("."):
            fields = struct_field_types.get(current_type) or struct_field_types.get(current_type.replace("struct ", ""))
            if not fields:
                return ""
            field_type = fields.get(field, "")
            if not field_type:
                return ""
            current_type = cls._struct_lookup_type(field_type)
        return field_type

    @staticmethod
    def _struct_lookup_type(ctype: str) -> str:
        clean = TestBackendGenerator._clean_fixture_type(ctype, defaults="", mutable=True)
        clean = clean.replace("*", " ")
        clean = re.sub(r"\b(?:const|volatile)\b", " ", clean)
        clean = " ".join(clean.split()).strip()
        return clean

    @staticmethod
    def _typed_field_value(ctype: str, value: str) -> str:
        return TestBackendGenerator._typed_value(ctype, value) if ctype else str(value).strip()

    @staticmethod
    def _is_enum_or_macro_identifier(name: str) -> bool:
        clean = str(name or "").strip()
        if not TestBackendGenerator._is_c_identifier(clean):
            return False
        # Only all-caps identifiers are safe to emit as external constants.
        # Mixed-case items such as Rte_VehicleSpeed are usually implementation
        # globals and may not be declared in the harness translation unit.
        if not clean.isupper():
            return False
        return bool(re.match(r"(?:E_|STD_|RTE_|DEM_|DCM_|COM_|CAN_|LIN_|NM_|PDUR_|PDU_|BSW_|ECUM_)", clean) or clean.isupper())

    @staticmethod
    def _harness_expression_for_expected(value: str, values: dict[str, str], param_by_name: dict[str, Parameter]) -> str:
        expr = str(value or "").strip()
        if not expr:
            return "0"
        if not TestBackendGenerator._is_c_identifier(expr):
            return expr
        if expr in values:
            candidate = str(values.get(expr, "")).strip()
            if not candidate or candidate == expr:
                return "0"
            literal = TestBackendGenerator._cpp_expected_literal(candidate)
            if literal and not TestBackendGenerator._is_c_identifier(literal):
                return literal
            if candidate in param_by_name:
                return f"tm_{TestBackendGenerator._safe_identifier(candidate)}"
            if TestBackendGenerator._is_enum_or_macro_identifier(candidate):
                return candidate
            if not TestBackendGenerator._is_c_identifier(candidate):
                # Keep simple literal/expression values only when they do not
                # contain unknown identifiers.
                if TestBackendGenerator._is_safe_expected_expression_for_harness(candidate, values, param_by_name):
                    return candidate
            return "0"
        if expr in param_by_name:
            return f"tm_{TestBackendGenerator._safe_identifier(expr)}"
        if TestBackendGenerator._is_enum_or_macro_identifier(expr):
            return expr
        return "0"

    @staticmethod
    def _is_safe_expected_expression_for_harness(value: str, values: dict[str, str], param_by_name: dict[str, Parameter]) -> bool:
        """Return True when an expected expression can compile in the harness.

        Implementation-local names from the source file, for example `ret`,
        `status`, or `result`, are not visible in generated GTest/CppUTest
        code.  Enum constants and macros are allowed only when they look like
        real constants.  Mixed-case globals such as Rte_VehicleSpeed are unsafe
        unless they are parameters or resolved to a literal in testcase values.
        """
        expr = str(value or "").strip()
        if not expr:
            return False
        literal = TestBackendGenerator._cpp_expected_literal(expr)
        if literal and not TestBackendGenerator._is_c_identifier(literal):
            return True
        if expr in param_by_name:
            return True
        if expr in values:
            candidate = str(values.get(expr, "")).strip()
            if not candidate or candidate == expr:
                return False
            return TestBackendGenerator._is_safe_expected_expression_for_harness(candidate, values, param_by_name)
        if TestBackendGenerator._is_c_identifier(expr):
            return TestBackendGenerator._is_enum_or_macro_identifier(expr)
        # Allow simple expressions composed from literals, enum-like constants,
        # case values, and parameters. Reject unknown identifiers.
        identifiers = re.findall(r"\b[A-Za-z_]\w*\b", expr)
        allowed_keywords = {"true", "false", "TRUE", "FALSE", "NULL", "nullptr", "sizeof"}
        for ident in identifiers:
            if ident in allowed_keywords or ident in param_by_name:
                continue
            if ident in values:
                candidate = str(values.get(ident, "")).strip()
                if candidate and candidate != ident and TestBackendGenerator._is_safe_expected_expression_for_harness(candidate, values, param_by_name):
                    continue
                return False
            if TestBackendGenerator._is_enum_or_macro_identifier(ident):
                continue
            return False
        return True

    @staticmethod
    def _can_initialize_fixture_from_literal(ctype: str, value: str) -> bool:
        literal = str(value or "").strip()
        if not literal:
            return False
        if literal.startswith("&") or "fixture_" in literal:
            return False
        if TestBackendGenerator._is_probably_aggregate_type(ctype):
            return literal.startswith("{")
        return True

    @staticmethod
    def _parse_global_declaration(statement: str) -> tuple[str, str] | None:
        if re.search(r"\bstatic\b", statement or ""):
            return None
        statement = re.sub(r"\b(?:extern|static)\b", " ", statement).strip()
        if "(" in statement or not statement.endswith(";"):
            return None
        statement = statement[:-1].strip()
        if "=" in statement:
            statement = statement.split("=", 1)[0].strip()
        match = re.match(r"(?P<type>.+?)(?P<ptr>\*\s*)?(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]+\])?$", statement)
        if not match:
            return None
        name = match.group("name")
        ctype = (match.group("type") + (" *" if match.group("ptr") else "")).strip()
        if not TestBackendGenerator._is_c_identifier(name):
            return None
        bad_starts = {"return", "if", "for", "while", "switch", "case", "typedef"}
        if ctype.split()[:1] and ctype.split()[0] in bad_starts:
            return None
        return name, TestBackendGenerator._clean_fixture_type(ctype, defaults="uint8", mutable=True)

    @staticmethod
    def _static_global_variable_names(unit_sources: list[Path] | None) -> set[str]:
        result: set[str] = set()
        for source in unit_sources or []:
            if not source.exists() or not source.is_file():
                continue
            try:
                text = source.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            sanitized = TestBackendGenerator._strip_comments_and_strings(text)
            sanitized = re.sub(r"^\s*#.*$", "", sanitized, flags=re.MULTILINE)
            depth = 0
            statement: list[str] = []
            for ch in sanitized:
                if ch == "{":
                    depth += 1
                    statement.clear()
                    continue
                if ch == "}":
                    depth = max(0, depth - 1)
                    statement.clear()
                    continue
                if depth != 0:
                    continue
                statement.append(ch)
                if ch == ";":
                    raw = "".join(statement).strip()
                    statement.clear()
                    if not re.search(r"\bstatic\b", raw):
                        continue
                    parsed = TestBackendGenerator._parse_static_global_declaration(raw)
                    if parsed:
                        result.add(parsed)
        return result

    @staticmethod
    def _parse_static_global_declaration(statement: str) -> str | None:
        if "(" in statement or not statement.endswith(";"):
            return None
        statement = statement[:-1].strip()
        if "=" in statement:
            statement = statement.split("=", 1)[0].strip()
        match = re.match(r"(?P<type>.+?)(?P<ptr>\*\s*)?(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]+\])?$", statement)
        if not match:
            return None
        name = match.group("name")
        return name if TestBackendGenerator._is_c_identifier(name) else None

    @classmethod
    def _merge_expectation_sources(cls, base: dict | None, override: dict | None) -> dict:
        """Merge expected-value sources; later sources have higher confidence.
        Priority is: explicit testcase metadata > FixItHint > AST/source fallback.
        Explicit testcase metadata is still applied later by _expected_*_literal().
        This merge is used for non-explicit sources, so FixItHint overrides the
        weaker AST/body heuristic when both produce an expected value.
        """
        result: dict = {"globals": {}, "outputs": {}}
        for item in (base or {}, override or {}):
            if not isinstance(item, dict):
                continue
            if item.get("return"):
                result["return"] = str(item.get("return"))
            result.setdefault("globals", {}).update({str(k): str(v) for k, v in (item.get("globals") or {}).items() if v is not None})
            result.setdefault("outputs", {}).update({cls._normalize_observable_name(str(k)): str(v) for k, v in (item.get("outputs") or {}).items() if v is not None})
        return result
    @classmethod
    def _fixit_case_expected_literals(cls, fn: FunctionInfo | None, case: TestCase) -> dict:
        result: dict = {"globals": {}, "outputs": {}}
        return_value = cls._fixit_expected_return_literal(fn, case)
        if return_value:
            result["return"] = return_value
        result["outputs"].update(cls._fixit_expected_output_literals(fn, case))
        result["globals"].update(cls._fixit_expected_global_literals(fn, case))
        return result
    @classmethod
    def _fixit_expected_return_literal(cls, fn: FunctionInfo | None, case: TestCase) -> str:
        for hint in cls._fixit_hints_for(fn, case, case=case, fn=fn):
            kind, target, value = cls._fixit_kind_target_value(hint)
            key = f"{kind} {target}".lower()
            if "return" not in key:
                parsed = cls._parse_fixit_text_for_expectation(value).get("return", "")
                if parsed:
                    return parsed
                continue
            if any(token in key for token in ("expected", "expect", "return")):
                literal = cls._cpp_expected_literal(value) or cls._parse_fixit_text_for_expectation(value).get("return", "")
                if literal:
                    return literal
        return ""
    @classmethod
    def _fixit_expected_output_literals(cls, fn: FunctionInfo | None, case: TestCase) -> dict[str, str]:
        result: dict[str, str] = {}
        for hint in cls._fixit_hints_for(fn, case, case=case, fn=fn):
            kind, target, value = cls._fixit_kind_target_value(hint)
            text_parsed = cls._parse_fixit_text_for_expectation(value)
            result.update(text_parsed.get("outputs", {}))
            key = f"{kind} {target}".lower()
            if not target or not any(token in key for token in ("output", "out", "parameter", "param", "pointee")):
                continue
            target = cls._strip_fixit_target_prefix(target, ("expected_output", "expect_output", "output", "out", "param", "parameter", "pointee"))
            if not target or target == "return" or target.startswith("global"):
                continue
            literal = cls._cpp_expected_literal(value)
            if literal:
                result[cls._normalize_observable_name(target)] = literal
        return result
    @classmethod
    def _fixit_expected_global_literals(cls, fn: FunctionInfo | None, case: TestCase) -> dict[str, str]:
        result: dict[str, str] = {}
        for hint in cls._fixit_hints_for(fn, case, case=case, fn=fn):
            kind, target, value = cls._fixit_kind_target_value(hint)
            text_parsed = cls._parse_fixit_text_for_expectation(value)
            result.update(text_parsed.get("globals", {}))
            key = f"{kind} {target}".lower()
            if not any(token in key for token in ("global", "state")):
                continue
            target = cls._strip_fixit_target_prefix(target, ("expected_global", "expect_global", "expected_state", "global", "state"))
            if target and cls._is_c_identifier(target):
                literal = cls._cpp_expected_literal(value)
                if literal:
                    result[target] = literal
        if fn is not None:
            result = cls._filter_global_literals(fn, result)
        return result
    @classmethod
    def _fixit_input_literals(cls, fn: FunctionInfo | None, case: TestCase) -> dict[str, str]:
        """Use FixItHint-like metadata to seed PRECONDITION values.
        Supported hint targets include input.x, param.x, local.tmp, global.g,
        stub.ReadSensor, and plain parameter/local/global names. Explicit
        case.values still override these hints.
        """
        result: dict[str, str] = {}
        for hint in cls._fixit_hints_for(fn, case, case=case, fn=fn):
            kind, target, value = cls._fixit_kind_target_value(hint)
            if not value:
                continue
            parsed = cls._parse_fixit_text_for_precondition(value)
            result.update(parsed)
            key = f"{kind} {target}".lower()
            target = target.strip()
            literal = cls._cpp_expected_literal(value) or value.strip()
            if not literal:
                continue
            if any(token in key for token in ("expected", "expect", "return", "output")):
                continue
            if target.startswith(("input.", "param.", "parameter.")):
                name = target.split(".", 1)[1]
                if cls._is_c_identifier(name):
                    result[name] = literal
            elif target.startswith(("local.", "var.")):
                name = target.split(".", 1)[1]
                if cls._is_c_identifier(name):
                    result[f"__local.{name}"] = literal
            elif target.startswith(("global.", "state.")):
                name = target.split(".", 1)[1]
                if cls._is_c_identifier(name):
                    result[f"__global.{name}"] = literal
            elif target.startswith("stub."):
                name = target.split(".", 1)[1]
                if cls._is_c_identifier(name):
                    result[f"__stub_return.{name}"] = literal
            elif cls._is_c_identifier(target) and any(token in key for token in ("input", "precondition", "setup", "value")):
                result[target] = literal
        return result
    @classmethod
    def _fixit_local_literals(cls, fn: FunctionInfo | None, case: TestCase) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in cls._fixit_input_literals(fn, case).items():
            name = cls._fixture_local_key_name(key)
            if name and cls._is_c_identifier(name):
                result[name] = str(value)
        return result
    @classmethod
    def _fixit_global_literals(cls, fn: FunctionInfo | None, case: TestCase) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in cls._fixit_input_literals(fn, case).items():
            for prefix in ("__global.", "__set_global.", "__input_global.", "__setup_global.", "setup.global."):
                if key.startswith(prefix):
                    name = key[len(prefix):]
                    if cls._is_c_identifier(name):
                        result[name] = str(value)
                    break
        return result
    @classmethod
    def _fixit_hints_for(cls, *owners, case: TestCase | None = None, fn: FunctionInfo | None = None) -> list[dict | str]:
        hints: list[dict | str] = []
        seen_ids: set[int] = set()
        for owner in owners:
            for hint in cls._collect_fixit_hints(owner):
                if isinstance(hint, (dict, list, tuple, str)):
                    hint_id = id(hint)
                    if hint_id in seen_ids:
                        continue
                    seen_ids.add(hint_id)
                if cls._fixit_hint_matches(hint, case, fn):
                    hints.append(hint)
        return hints
    @classmethod
    def _collect_fixit_hints(cls, owner) -> list[dict | str]:
        if owner is None:
            return []
        payloads: list = []
        if isinstance(owner, dict):
            payloads.append(owner)
        for attr in (
            "fixit_hints", "fixits", "fix_it_hints", "fixItHints", "fixit", "fix_it",
            "hints", "test_hints", "generation_hints", "expected_hints", "analysis_hints",
        ):
            if hasattr(owner, attr):
                payloads.append(getattr(owner, attr))
        for attr in ("metadata", "meta", "analysis", "extra", "extras"):
            meta = getattr(owner, attr, None) if not isinstance(owner, dict) else owner.get(attr)
            if isinstance(meta, dict):
                for key in (
                    "fixit_hints", "fixits", "fix_it_hints", "fixItHints", "fixit", "fix_it",
                    "hints", "test_hints", "generation_hints", "expected_hints", "analysis_hints",
                ):
                    if key in meta:
                        payloads.append(meta[key])
        hints: list[dict | str] = []
        for payload in payloads:
            hints.extend(cls._flatten_fixit_payload(payload))
        return hints
    @classmethod
    def _flatten_fixit_payload(cls, payload) -> list[dict | str]:
        if payload is None:
            return []
        if isinstance(payload, str):
            text = payload.strip()
            if not text:
                return []
            try:
                decoded = json.loads(text)
                return cls._flatten_fixit_payload(decoded)
            except Exception:
                return [text]
        if isinstance(payload, (list, tuple, set)):
            result: list[dict | str] = []
            for item in payload:
                result.extend(cls._flatten_fixit_payload(item))
            return result
        if isinstance(payload, dict):
            result: list[dict | str] = [payload]
            for group_key, semantic in (
                ("return", "expected_return"), ("expected_return", "expected_return"),
                ("outputs", "expected_output"), ("expected_outputs", "expected_output"),
                ("globals", "expected_global"), ("expected_globals", "expected_global"),
                ("inputs", "input"), ("preconditions", "input"), ("locals", "local"),
            ):
                value = payload.get(group_key)
                if isinstance(value, dict):
                    for target, literal in value.items():
                        result.append({"kind": semantic, "target": str(target), "value": literal})
                elif isinstance(value, (str, int, float)) and group_key in {"return", "expected_return"}:
                    result.append({"kind": semantic, "target": "return", "value": value})
            nested = payload.get("hints") or payload.get("fixits") or payload.get("fixit_hints")
            if nested is not payload:
                result.extend(cls._flatten_fixit_payload(nested))
            return result
        return []
    @classmethod
    def _fixit_hint_matches(cls, hint, case: TestCase | None, fn: FunctionInfo | None) -> bool:
        if not isinstance(hint, dict):
            return True
        if case is not None:
            wanted = {str(getattr(case, "test_id", "") or ""), str(getattr(case, "id", "") or ""), str(getattr(case, "name", "") or "")}
            wanted.discard("")
            for key in ("test_id", "case_id", "test", "test_name", "scenario", "id"):
                value = str(hint.get(key, "") or "")
                if value and wanted and value not in wanted:
                    return False
        if fn is not None:
            fn_name = str(getattr(fn, "name", "") or "")
            for key in ("function", "function_name", "fn", "symbol"):
                value = str(hint.get(key, "") or "")
                if value and fn_name and value != fn_name:
                    if key in {"function", "function_name", "fn"}:
                        return False
        condition = str(hint.get("condition") or hint.get("path_condition") or "").strip()
        if condition and case is not None:
            values = cls._case_symbol_values(case)
            verdict = cls._eval_simple_condition(condition, values)
            # A conditional oracle is unsafe if the current vector does not prove
            # that path. Drop it instead of applying THEN expectations to ELSE
            # vectors or vice versa.
            if verdict is not True:
                return False
        return True
    @classmethod
    def _fixit_kind_target_value(cls, hint) -> tuple[str, str, str]:
        if isinstance(hint, str):
            return "text", "", hint.strip()
        if not isinstance(hint, dict):
            return "", "", ""
        kind = str(hint.get("kind") or hint.get("type") or hint.get("category") or hint.get("action") or "").strip()
        target = str(
            hint.get("target") or hint.get("observable") or hint.get("path") or hint.get("symbol") or
            hint.get("name") or hint.get("field") or ""
        ).strip()
        value = hint.get("value")
        if value is None:
            for key in ("expected", "literal", "expr", "expression", "replacementText", "replacement", "newText", "text", "message"):
                if key in hint and hint[key] is not None:
                    value = hint[key]
                    break
        return kind, target, str(value or "").strip()
    @classmethod
    def _parse_fixit_text_for_expectation(cls, text: str) -> dict:
        result: dict = {"globals": {}, "outputs": {}}
        text = str(text or "").strip()
        for pattern in (
            r"Expected\s+return\s*[:=]\s*([^;,\n]+)",
            r"expect(?:ed)?[_\s-]*return\s*[:=]\s*([^;,\n]+)",
            r"return\s*(?:=>|:=)\s*([^;,\n]+)",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                literal = cls._cpp_expected_literal(match.group(1))
                if literal:
                    result["return"] = literal
                    break
        for pattern in (
            r"Expected\s+(?:output|out|output\s+parameter)\s+([A-Za-z_]\w*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)?)\s*[:=]\s*([^;,\n]+)",
            r"(?:expected[_\s-]*output|output)\s+([A-Za-z_]\w*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)?)\s*(?:=>|:=|=|:)\s*([^;,\n]+)",
        ):
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                literal = cls._cpp_expected_literal(match.group(2))
                if literal:
                    result["outputs"][cls._normalize_observable_name(match.group(1))] = literal
        for pattern in (
            r"Expected\s+(?:global|state)\s+([A-Za-z_]\w*)\s*[:=]\s*([^;,\n]+)",
            r"(?:expected[_\s-]*(?:global|state)|global|state)\s+([A-Za-z_]\w*)\s*(?:=>|:=|=|:)\s*([^;,\n]+)",
        ):
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                literal = cls._cpp_expected_literal(match.group(2))
                if literal:
                    result["globals"][match.group(1)] = literal
        return result
    @classmethod
    def _parse_fixit_text_for_precondition(cls, text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        text = str(text or "").strip()
        for pattern in (
            r"(?:input|param|parameter)\s+([A-Za-z_]\w*)\s*(?:=>|:=|=|:)\s*([^;,\n]+)",
            r"(?:local|var)\s+([A-Za-z_]\w*)\s*(?:=>|:=|=|:)\s*([^;,\n]+)",
            r"(?:global|state)\s+([A-Za-z_]\w*)\s*(?:=>|:=|=|:)\s*([^;,\n]+)",
            r"stub\s+([A-Za-z_]\w*)\s*(?:=>|:=|=|:)\s*([^;,\n]+)",
        ):
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                literal = cls._cpp_expected_literal(match.group(2))
                if not literal:
                    continue
                prefix = pattern.split("\\s+", 1)[0]
                lower_prefix = prefix.lower()
                name = match.group(1)
                if "local" in lower_prefix or "var" in lower_prefix:
                    result[f"__local.{name}"] = literal
                elif "global" in lower_prefix or "state" in lower_prefix:
                    result[f"__global.{name}"] = literal
                elif "stub" in lower_prefix:
                    result[f"__stub_return.{name}"] = literal
                else:
                    result[name] = literal
        return result
    @staticmethod
    def _strip_fixit_target_prefix(target: str, prefixes: tuple[str, ...]) -> str:
        target = str(target or "").strip().replace("->", ".")
        for prefix in prefixes:
            if target == prefix:
                return ""
            if target.startswith(prefix + "."):
                return target[len(prefix) + 1:]
        return target
    @staticmethod
    def _missing_expected_lines(backend: str, message: str, indent: str = "    ") -> list[str]:
        # Missing expectations are reported as review comments only.
        safe = message.replace("*/", "* /")
        return [f"{indent}/* TODO expected behavior: {safe} */"]

    @classmethod
    def _expected_return_literal(cls, case_or_expected) -> str:
        if hasattr(case_or_expected, "values"):
            values = getattr(case_or_expected, "values", {}) or {}
            for key in ("__expected_return", "__expect_return", "expected_return", "return.expected"):
                if key in values:
                    literal = cls._cpp_expected_literal(str(values[key]))
                    if literal:
                        return literal
            literal = cls._fixit_expected_return_literal(None, case_or_expected)
            if literal:
                return literal
            expected = str(getattr(case_or_expected, "expected", "") or "")
        else:
            expected = str(case_or_expected or "")
        for pattern in [
            r"Expected\s+return\s*[:=]\s*([^;,\n]+)",
            r"Return\s+value\s*[:=]\s*([^;,\n]+)",
            r"returns?\s+([^;,\n]+)",
        ]:
            match = re.search(pattern, expected, flags=re.IGNORECASE)
            if match:
                literal = cls._cpp_expected_literal(match.group(1))
                if literal:
                    return literal
        return ""

    @classmethod
    def _expected_output_literals(cls, case: TestCase) -> dict[str, str]:
        result: dict[str, str] = cls._fixit_expected_output_literals(None, case)
        values = case.values or {}
        prefixes = (
            "__expected_output.",
            "__expect_output.",
            "__output_expected.",
            "expected.output.",
        )
        for key, value in values.items():
            output_name = ""
            for prefix in prefixes:
                if key.startswith(prefix):
                    output_name = key[len(prefix) :]
                    break
            if not output_name:
                for suffix in (".__expected", ".expected", ".__expect"):
                    if key.endswith(suffix):
                        output_name = key[: -len(suffix)]
                        break
            if output_name:
                literal = cls._cpp_expected_literal(str(value))
                if literal:
                    result[cls._normalize_observable_name(output_name)] = literal
        expected = str(case.expected or "")
        text_patterns = [
            r"Expected\s+(?:output|out|output\s+parameter)\s+([A-Za-z_]\w*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)?)\s*[:=]\s*([^;,\n]+)",
            r"(?:output|out)\s+([A-Za-z_]\w*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)?)\s*(?:after\s+call)?\s*[:=]\s*([^;,\n]+)",
        ]
        for pattern in text_patterns:
            for match in re.finditer(pattern, expected, flags=re.IGNORECASE):
                literal = cls._cpp_expected_literal(match.group(2))
                if literal:
                    result[cls._normalize_observable_name(match.group(1))] = literal
        return result

    @classmethod
    def _expected_global_literals(cls, case: TestCase) -> dict[str, str]:
        result: dict[str, str] = cls._fixit_expected_global_literals(None, case)
        values = case.values or {}
        prefixes = (
            "__expected_global.",
            "__expect_global.",
            "__expected_state.",
            "expected.global.",
            "expected.state.",
        )
        for key, value in values.items():
            name = ""
            for prefix in prefixes:
                if key.startswith(prefix):
                    name = key[len(prefix) :]
                    break
            if not name:
                for suffix in (".__expected_global", ".expected_global", ".__expected_state", ".expected_state"):
                    if key.endswith(suffix):
                        name = key[: -len(suffix)]
                        break
            if name and cls._is_c_identifier(name):
                literal = cls._cpp_expected_literal(str(value))
                if literal:
                    result[name] = literal
        expected = str(case.expected or "")
        for pattern in [
            r"Expected\s+(?:global|state)\s+([A-Za-z_]\w*)\s*[:=]\s*([^;,\n]+)",
            r"(?:global|state)\s+([A-Za-z_]\w*)\s*(?:after\s+call)?\s*[:=]\s*([^;,\n]+)",
        ]:
            for match in re.finditer(pattern, expected, flags=re.IGNORECASE):
                literal = cls._cpp_expected_literal(match.group(2))
                if literal:
                    result[match.group(1)] = literal
        return result

    @staticmethod
    def _normalize_observable_name(name: str) -> str:
        return re.sub(r"\s*(?:->|\.)\s*", ".", name.strip())

    @staticmethod
    def _is_expected_key(key: str) -> bool:
        return key.startswith("__") or key in {"expected", "__expected", "__expect"} or key.endswith((".__expected", ".expected", ".__expect"))

    @staticmethod
    def _output_actual_expression(output_name: str, pointer_fixtures: dict[str, str]) -> str:
        normalized = TestBackendGenerator._normalize_observable_name(output_name)
        root, _, suffix = normalized.partition(".")
        fixture = pointer_fixtures.get(root)
        if not fixture:
            return ""
        return fixture if not suffix else f"{fixture}.{suffix}"

    @staticmethod
    def _output_expected_expression(output_name: str, expected_output_fixtures: dict[str, str]) -> str:
        normalized = TestBackendGenerator._normalize_observable_name(output_name)
        root, _, suffix = normalized.partition(".")
        fixture = expected_output_fixtures.get(root)
        if not fixture:
            return ""
        return fixture if not suffix else f"{fixture}.{suffix}"

    @classmethod
    def _written_pointer_observables(cls, fn: FunctionInfo) -> dict[str, set[str]]:
        body = cls._strip_comments_and_strings(fn.body)
        result: dict[str, set[str]] = {}
        for param in fn.parameters:
            if param.kind != "pointer":
                continue
            name = re.escape(param.name)
            suffixes: set[str] = set()
            if re.search(rf"\*\s*{name}\s*(?:=|\+=|-=|\*=|/=|%=|&=|\|=|\^=)", body):
                suffixes.add("")
            for match in re.finditer(rf"\b{name}\s*->\s*([A-Za-z_]\w*)\s*(?:=|\+=|-=|\*=|/=|%=|&=|\|=|\^=)", body):
                suffixes.add("." + match.group(1))
            if re.search(rf"\b{name}\s*\[\s*0\s*\]\s*(?:=|\+=|-=|\*=|/=|%=|&=|\|=|\^=)", body):
                suffixes.add("")
            if suffixes:
                result[param.name] = suffixes
        return result

    @classmethod
    def _written_global_names(cls, fn: FunctionInfo) -> set[str]:
        body = cls._strip_comments_and_strings(fn.body)
        locals_ = cls._function_internal_local_names(fn)
        params = {param.name for param in fn.parameters}
        ignored = {fn.name, *locals_, *params, "return", "sizeof", "true", "false", "NULL", "nullptr"}
        names: set[str] = set()
        op = r"(?:\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=|(?<![=!<>])=(?!=))"
        for match in re.finditer(rf"(?<![.>])\b([A-Za-z_]\w*)\b\s*{op}", body):
            name = match.group(1)
            prefix = body[max(0, match.start(1) - 8) : match.start(1)]
            if re.search(r"(?:->|\.)\s*$", prefix):
                continue
            if name not in ignored and cls._is_c_identifier(name):
                names.add(name)
        for match in re.finditer(r"(?:\+\+|--)\s*(?<![.>])\b([A-Za-z_]\w*)\b", body):
            name = match.group(1)
            prefix = body[max(0, match.start(1) - 8) : match.start(1)]
            if re.search(r"(?:->|\.)\s*$", prefix):
                continue
            if name not in ignored and cls._is_c_identifier(name):
                names.add(name)
        return names

    @staticmethod
    def _local_declaration_candidates(body: str) -> list[str]:
        """Return simple statement candidates that may be C local declarations.

        The previous regex scanned across arbitrary tokens and could mistake
        `if (...)` or `return ret;` for declarations. This routine keeps parsing
        statement-local and conservative: split on semicolons, keep only the tail
        after the nearest block delimiter, then let `_parse_local_declaration`
        reject control/call/assignment statements.
        """
        candidates: list[str] = []
        for raw in (body or "").split(";")[:-1]:
            stmt = raw.rsplit("{", 1)[-1].rsplit("}", 1)[-1].strip()
            if stmt:
                candidates.append(stmt)
        return candidates

    @staticmethod
    def _parse_local_declaration(stmt: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for spec in TestBackendGenerator._parse_local_declaration_specs(stmt):
            name = str(spec.get("name") or "")
            ctype = str(spec.get("ctype") or "")
            if name and ctype:
                result.setdefault(name, ctype)
        return result

    @staticmethod
    def _parse_local_declaration_specs(stmt: str) -> list[dict[str, str]]:
        """Parse non-static C local declarations into fixture specs.

        The harness may need implementation-local names for expected expressions
        produced by AST/fixit engines.  Static locals are deliberately excluded:
        they are persistent UUT state, not per-test fixture variables.
        """
        stmt = " ".join((stmt or "").replace("\n", " ").split()).strip()
        if not stmt:
            return []
        first_word = stmt.split(None, 1)[0].rstrip(":")
        forbidden_starts = {
            "if", "else", "for", "while", "switch", "case", "default",
            "return", "break", "continue", "goto", "do", "sizeof",
        }
        if first_word in forbidden_starts:
            return []
        if re.match(r"^(?:case\b.*|default)\s*:", stmt):
            return []
        if ":" in stmt.split("=", 1)[0]:
            return []
        if stmt.startswith(("*", "&", "++", "--")):
            return []
        head_for_call_check = stmt.split("=", 1)[0]
        if "(" in head_for_call_check or ")" in head_for_call_check:
            return []
        if re.search(r"(==|!=|<=|>=|&&|\|\||->|\.|[<>])", head_for_call_check):
            return []

        declarators = TestBackendGenerator._split_top_level_commas(stmt)
        if not declarators:
            return []
        first_decl = declarators[0].split("=", 1)[0].strip()
        first_match = re.search(
            r"(?P<prefix>.*?)(?P<name>[A-Za-z_]\w*)\s*(?P<array>(?:\[[^\]]*\]\s*)*)$",
            first_decl,
        )
        if not first_match:
            return []
        type_part = first_match.group("prefix").strip()
        if not type_part:
            return []
        if re.search(r"\b(?:static|STATIC)\b", type_part):
            return []
        type_tokens = [token for token in re.split(r"\s+", type_part.replace("*", " ").strip()) if token]
        if not type_tokens or type_tokens[0] in forbidden_starts:
            return []
        if len(type_tokens) == 1 and type_tokens[0] in {"true", "false", "NULL", "nullptr"}:
            return []
        base_type = TestBackendGenerator._clean_fixture_type(type_part.replace("*", " "), defaults="uint8", mutable=True)
        if not base_type:
            return []

        result: list[dict[str, str]] = []
        seen: set[str] = set()
        first_name = first_match.group("name")
        for index, raw_item in enumerate(declarators):
            item = raw_item.split("=", 1)[0].strip()
            name_match = re.search(
                r"(?P<prefix>.*?)(?P<name>[A-Za-z_]\w*)\s*(?P<array>(?:\[[^\]]*\]\s*)*)$",
                item,
            )
            if not name_match:
                continue
            name = name_match.group("name")
            if not TestBackendGenerator._is_c_identifier(name):
                continue
            prefix = name_match.group("prefix") or ""
            if index == 0 and name != first_name:
                continue
            pointer = "*" in prefix
            ctype = (base_type.rstrip(" *") + (" *" if pointer else "")).strip()
            array_suffix = " ".join((name_match.group("array") or "").split())
            if ctype and ctype.split()[0] not in forbidden_starts and name not in seen:
                result.append({"name": name, "ctype": ctype, "array": array_suffix})
                seen.add(name)
        return result

    @staticmethod
    def _local_variable_fixture_specs(fn: FunctionInfo | None) -> list[dict[str, str]]:
        if fn is None:
            return []
        body = TestBackendGenerator._strip_comments_and_strings(fn.body)
        specs: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(name: str, ctype: str, array: str = "") -> None:
            if not TestBackendGenerator._is_c_identifier(name) or name in seen:
                return
            safe_type = TestBackendGenerator._clean_fixture_type(ctype, defaults="uint8", mutable=True)
            if not TestBackendGenerator._is_valid_fixture_type(safe_type):
                safe_type = "uint8"
            specs.append({"name": name, "ctype": safe_type, "array": array or ""})
            seen.add(name)

        for statement in TestBackendGenerator._local_declaration_candidates(body):
            for spec in TestBackendGenerator._parse_local_declaration_specs(statement):
                add(str(spec.get("name") or ""), str(spec.get("ctype") or "uint8"), str(spec.get("array") or ""))

        for match in re.finditer(r"\bfor\s*\(\s*(?P<init>[^;]+);", body):
            for spec in TestBackendGenerator._parse_local_declaration_specs(match.group("init")):
                add(str(spec.get("name") or ""), str(spec.get("ctype") or "uint8"), str(spec.get("array") or ""))

        macro_decl = re.compile(
            r"\b(?P<static>static|STATIC)?\s*"
            r"(?P<macro>VAR|CONST|P2VAR|P2CONST|CONSTP2VAR)\s*\((?P<type>[^,()]+)(?:,[^()]*)?\)\s*"
            r"(?P<name>[A-Za-z_]\w*)\s*(?P<array>(?:\[[^\]]*\]\s*)*)\s*(?:=|;|\[)?"
        )
        for match in macro_decl.finditer(body):
            if match.group("static"):
                continue
            name = match.group("name")
            ctype = TestBackendGenerator._clean_fixture_type(match.group("type"), defaults="uint8", mutable=True)
            if match.group("macro") in {"P2VAR", "P2CONST", "CONSTP2VAR"}:
                ctype = ctype.rstrip(" *") + " *"
            add(name, ctype, " ".join((match.group("array") or "").split()))
        return specs

    @staticmethod
    def _local_variable_type_map(fn: FunctionInfo | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for spec in TestBackendGenerator._local_variable_fixture_specs(fn):
            name = str(spec.get("name") or "")
            ctype = str(spec.get("ctype") or "")
            if name and ctype:
                result.setdefault(name, ctype)
        return result

    @staticmethod
    def _split_top_level_commas(text: str) -> list[str]:
        parts: list[str] = []
        depth = 0
        current: list[str] = []
        for ch in text:
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
    def _local_variable_names(fn: FunctionInfo) -> set[str]:
        # Keep this aligned with fixture materialization: non-static locals only.
        return set(TestBackendGenerator._local_variable_type_map(fn).keys())
    @classmethod
    def _function_internal_local_names(cls, fn: FunctionInfo | None) -> set[str]:
        if fn is None:
            return set()
        locals_ = set(cls._local_variable_names(fn))
        locals_.update(cls._local_variable_type_map(fn).keys())
        body = cls._strip_comments_and_strings(getattr(fn, "body", "") or "")
        for match in re.finditer(r"\breturn\s+([A-Za-z_]\w*)\s*;", body):
            returned = match.group(1)
            if returned in locals_ or returned.lower() in {"ret", "retval", "returnvalue", "result", "status"}:
                locals_.add(returned)
        return locals_
    @staticmethod
    def _looks_like_global_name(name: str) -> bool:
        return bool(re.match(r"(?:g_|s_|m_|G_|S_|M_|global_|Global|g[A-Z]).+", name or ""))
    @staticmethod
    def _function_global_variable_type_map(fn: FunctionInfo | None) -> dict[str, str]:
        if fn is None:
            return {}
        payload = getattr(fn, "global_variable_types", None) or {}
        if isinstance(payload, dict):
            return {str(k): str(v) for k, v in payload.items()}
        symbols = getattr(fn, "global_symbols", None) or set()
        if isinstance(symbols, (set, list, tuple)):
            return {str(item): "uint8" for item in symbols}
        return {}
    @classmethod
    def _filter_global_names(
        cls,
        fn: FunctionInfo | None,
        names: set[str],
        global_type_map: dict[str, str] | None = None,
        blocked_global_names: set[str] | None = None,
    ) -> set[str]:
        locals_ = cls._function_internal_local_names(fn)
        params = {getattr(param, "name", "") for param in getattr(fn, "parameters", []) or []} if fn is not None else set()
        known_globals = set((global_type_map or {}).keys()) | set(cls._function_global_variable_type_map(fn).keys())
        blocked_global_names = blocked_global_names or set()
        result: set[str] = set()
        for name in names:
            if not name or not cls._is_c_identifier(name):
                continue
            if name in blocked_global_names or name in locals_ or name in params:
                continue
            if known_globals:
                if name in known_globals:
                    result.add(name)
            elif cls._looks_like_global_name(name):
                result.add(name)
        return result
    @classmethod
    def _filter_global_literals(
        cls,
        fn: FunctionInfo | None,
        values: dict[str, str],
        global_type_map: dict[str, str] | None = None,
        blocked_global_names: set[str] | None = None,
    ) -> dict[str, str]:
        allowed = cls._filter_global_names(fn, set(values.keys()), global_type_map, blocked_global_names)
        return {name: value for name, value in values.items() if name in allowed}

    @staticmethod
    def _strip_comments_and_strings(text: str) -> str:
        text = re.sub(r"/\*.*?\*/", " ", text or "", flags=re.S)
        text = re.sub(r"//.*", " ", text)
        text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
        text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
        return text

    @staticmethod
    def _cpp_expected_literal(value: str) -> str:
        value = (value or "").strip()
        value = re.sub(r"\s*(?:\.|;)$", "", value).strip()
        value = value.strip("` ")
        if not value or value.lower() in {"void", "n/a", "na", "none", "unknown", "todo"}:
            return ""
        if re.fullmatch(r"[-+]?0[xX][0-9A-Fa-f]+[uUlL]*", value):
            return value
        if re.fullmatch(r"[-+]?\d+(?:[uUlL]*)", value):
            return value
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?[fFlL]?", value):
            return value
        if re.fullmatch(r"[-+]?\d+[eE][-+]?\d+[fFlL]?", value):
            return value
        if value in {"true", "false", "TRUE", "FALSE", "NULL", "nullptr"}:
            return value
        if re.fullmatch(r"'.'", value) or re.fullmatch(r"'\\.'", value):
            return value
        if re.fullmatch(r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)?", value):
            return value
        return ""

    @staticmethod
    def _is_c_identifier(name: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z_]\w*", name or ""))

    @staticmethod
    def _safe_identifier(name: str) -> str:
        safe = re.sub(r"\W+", "_", name).strip("_")
        if not safe or safe[0].isdigit():
            safe = "stub_" + safe
        return safe

    @staticmethod
    def _test_group_lines(
        backend: str,
        group_name: str,
        case: TestCase,
        fixture_declarations: list[str] | None = None,
        setup_reset_lines: list[str] | None = None,
        teardown_reset_lines: list[str] | None = None,
    ) -> list[str]:
        fixture_declarations = fixture_declarations or []
        setup_reset_lines = setup_reset_lines or ["TestingMate_ResetStubs();"]
        teardown_reset_lines = teardown_reset_lines or ["TestingMate_ResetStubs();"]
        declaration_lines = [f"    {line}" for line in fixture_declarations]
        setup_comment = TestBackendGenerator._lifecycle_comment_lines(case, "        ")
        teardown_comment = TestBackendGenerator._teardown_comment_lines(case, "        ")
        setup_body = [f"        {line}" for line in setup_reset_lines]
        teardown_body = [f"        {line}" for line in teardown_reset_lines]
        if backend == "CppUTest":
            return [
                f"TEST_GROUP({group_name})",
                "{",
                *declaration_lines,
                "",
                "    void setup()",
                "    {",
                *setup_comment,
                *setup_body,
                "    }",
                "",
                "    void teardown()",
                "    {",
                *teardown_comment,
                *teardown_body,
                "    }",
                "};",
                "",
            ]
        return [
            f"class {group_name} : public ::testing::Test",
            "{",
            "protected:",
            *declaration_lines,
            "",
            "    void SetUp() override",
            "    {",
            *setup_comment,
            *setup_body,
            "    }",
            "",
            "    void TearDown() override",
            "    {",
            *teardown_comment,
            *teardown_body,
            "    }",
            "};",
            "",
        ]

    @staticmethod
    def _lifecycle_comment_lines(case: TestCase, indent: str = "") -> list[str]:
        fields = [
            ("Preconditions", case.preconditions),
            ("Setup", case.setup),
        ]
        lines = [f"{indent}/*"]
        for label, value in fields:
            value = TestBackendGenerator._oracle_free_comment_text(label, value)
            lines.append(f"{indent} * {label}: {value}")
        lines.append(f"{indent} */")
        return lines

    @staticmethod
    def _oracle_free_comment_text(label: str, value: str) -> str:
        text = str(value or "")
        text = re.sub(r";\s*harness state:\s*[^.]*\bexpected[_A-Za-z0-9.]*\s*=[^.]*", "", text)
        text = re.sub(r"\b(?:expected|expect)[_.](?:return|output|global|state)[A-Za-z0-9_.]*\s*=\s*[^;,.]+[;,.]?\s*", "", text)
        text = text.replace("3. Capture return value and modified outputs. 4. Check", "3. Check")
        text = text.replace("Capture return value and modified outputs.", "")
        if label == "Pass/Fail Criteria" and "expected results" in text:
            return "Pass when no unexpected safety-state, diagnostic, memory, or assertion failure occurs."
        return " ".join(text.split())

    @staticmethod
    def _teardown_comment_lines(case: TestCase, indent: str = "") -> list[str]:
        return [
            f"{indent}/*",
            f"{indent} * Teardown: {case.teardown}",
            f"{indent} */",
        ]

    @staticmethod
    def _case_comment_lines(case: TestCase, indent: str = "") -> list[str]:
        fields = [
            ("Description", case.description),
            ("Test Steps", case.test_steps),
            ("Pass/Fail Criteria", case.pass_fail_criteria),
            ("Requirement", case.requirement_ref),
            ("Safety Goal", case.safety_goal_ref),
            ("Safety Integrity", case.asil),
            # ("Process Target", case.aspice_level),
            ("Trace Tags", case.trace_tags),
        ]
        lines = [f"{indent}/*"]
        for label, value in fields:
            lines.append(f"{indent} * {label}: {value}")
        lines.append(f"{indent} */")
        return lines

    @staticmethod
    def _cpp_literal_for_param(param: Parameter, value: str) -> str:
        ctype = " ".join(param.ctype.replace("const", "").split()).strip()
        if ctype.endswith("Type") and re.match(r"^-?\d+", value):
            return f"({ctype}){value}"
        return value

    @staticmethod
    def _pointer_fixture_type(param: Parameter) -> str:
        ctype = TestBackendGenerator._clean_fixture_type(param.ctype or "uint8", defaults="uint8", mutable=True)
        if "*" in ctype:
            ctype = ctype.replace("*", " ")
        return " ".join(ctype.split()).strip() or "uint8"

    def _cmake_include_dirs(self, workspace_roots: list[Path | str] | None = None) -> str:
        """CMake include directories for the isolated backend workspace.

        Do not leak original project include paths into generated CMake. The
        compiler should see only the current generated stub header folder.
        """
        roots: list[Path | str] = []
        seen: set[str] = set()
        for raw_root in (workspace_roots or [Path("stub/include")]):
            root = self._as_path(raw_root).expanduser()
            key = str(root).replace("\\", "/").lower()
            if key in seen:
                continue
            seen.add(key)
            roots.append(root)
        return " ".join(self._cmake_path(item) for item in roots)

    def _relative_cmake_paths(self, paths: list[Path], base_dir: Path) -> list[str]:
        result: list[str] = []
        for path in paths:
            try:
                relative = path.relative_to(base_dir)
            except ValueError:
                relative = path
            result.append(self._cmake_path(relative))
        return result

    def _cmake_source_paths(self, paths: list[Path | str]) -> list[str]:
        result: list[str] = []
        for raw_path in paths:
            path = self._as_path(raw_path).expanduser()
            # Preserve workspace-relative paths such as src/AppSwc.c. Only
            # absolute external paths are resolved.
            if path.is_absolute() and path.exists():
                result.append(self._cmake_path(self._safe_resolve(path)))
            else:
                result.append(self._cmake_path(path))
        return result

    @classmethod
    def _runner_target_source(cls, source: Path | str) -> str:
        source_path = cls._as_path(source).expanduser()
        if source_path.suffix.lower() in C_SOURCE_EXTENSIONS:
            return f"src/{source_path.name}".replace("\\", "/")
        return str(source_path).replace("\\", "/")

    def _include_roots(self, source: Path | str, unit_sources: list[Path | str]) -> list[Path]:
        source_path = self._as_path(source).expanduser()
        unit_paths = self._as_path_list(unit_sources)
        roots: list[Path] = []
        for candidate in [
            source_path.parent,
            *(path.parent for path in unit_paths),
            *self.include_dirs,
            *self._workspace_include_discovery_roots(source_path, unit_paths),
        ]:
            if candidate and candidate.exists() and candidate.is_dir():
                roots.append(self._safe_resolve(candidate))
        return self._unique_paths(roots)

    def _matching_include_root(self, path: Path | str, unit_sources: list[Path | str]) -> Path | None:
        path = self._as_path(path).expanduser()
        unit_paths = self._as_path_list(unit_sources)
        candidates = [
            *self.include_dirs,
            *(source.parent for source in unit_paths),
            *self._workspace_include_discovery_roots(path, unit_paths),
        ]
        for root in sorted((item for item in candidates if item.exists()), key=lambda item: len(str(self._safe_resolve(item))), reverse=True):
            try:
                self._safe_resolve(path).relative_to(self._safe_resolve(root))
                return root
            except ValueError:
                continue
        return None

    @classmethod
    def _workspace_include_discovery_roots(cls, source: Path | str, unit_sources: list[Path | str] | None = None) -> list[Path]:
        roots: list[Path] = []
        for raw_source in [source, *(unit_sources or [])]:
            path = cls._as_path(raw_source).expanduser()
            start_dir = path.parent if path.suffix else path
            if start_dir.exists() and start_dir.is_dir():
                roots.append(start_dir)
            current = start_dir
            visited: set[Path] = set()
            while current and current not in visited:
                visited.add(current)
                if current.exists() and current.is_dir() and current.name.lower() in {"include", "includes", "inc", "header", "headers"}:
                    roots.append(current)
                    break
                found_child: Path | None = None
                if current.exists() and current.is_dir():
                    for child in current.iterdir():
                        if child.is_dir() and child.name.lower() in {"include", "includes", "inc", "header", "headers"}:
                            found_child = child
                            break
                if found_child is not None:
                    roots.append(found_child)
                    break
                parent = current.parent
                if parent == current:
                    break
                current = parent
        return cls._unique_paths([root for root in roots if root.exists() and root.is_dir()])

    @classmethod
    def _unique_paths(cls, paths: list[Path]) -> list[Path]:
        result: list[Path] = []
        seen: set[Path] = set()
        for path in paths:
            try:
                key = cls._safe_resolve(path)
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return result

    @staticmethod
    def _cmake_path(path: Path | str) -> str:
        text = str(path).replace("\\", "/")
        if text == ".":
            return "."
        return '"' + text.replace('"', '\\"') + '"'

    def _extract_includes(self, path: Path) -> list[str]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [match.group(1).strip() for match in self.include_pattern.finditer(text)]

    def _resolve_include(self, include_name: str, current_dir: Path | str, unit_sources: list[Path | str]) -> Path | None:
        current_dir = self._as_path(current_dir).expanduser()
        candidates = [current_dir / include_name]
        candidates.extend(root / include_name for root in self._include_roots(current_dir, unit_sources))
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def _guess_header(self, source: Path | str, unit_sources: list[Path | str]) -> str:
        source = self._as_path(source).expanduser()
        unit_sources = self._as_path_list(unit_sources)
        candidate = source.with_suffix(".h")
        if candidate.exists():
            return candidate.name
        preferred = f"{source.stem}.h"
        for root in self._include_roots(source, unit_sources):
            direct = root / preferred
            if direct.exists():
                return preferred
        source_includes = self._extract_includes(source)
        for include_name in source_includes:
            if Path(include_name).name == preferred and self._resolve_include(include_name, source.parent, unit_sources):
                return include_name
        for include_name in source_includes:
            suffix = Path(include_name).suffix.lower()
            if suffix in {".h", ".hpp"} and self._resolve_include(include_name, source.parent, unit_sources):
                return include_name
        if source.suffix.lower() in C_SOURCE_EXTENSIONS:
            # Last-resort direct-source harness: include the copied local source,
            # not the original project path. This keeps generated CMake include
            # directories scoped to stub/include/.
            return f"src/{source.name}"
        return source.name

    @staticmethod
    def _cmake_runner(target: str, target_source: str | None = None, enable_mcdc: bool = False) -> str:
        import textwrap

        def bash_dq(value: str) -> str:
            return (
                (value or "")
                .replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("$", "\\$")
                .replace("`", "\\`")
            )

        raw_target_source = target_source or ""
        if re.match(r"^[A-Za-z]:[\\/]", raw_target_source):
            target_source_msys = windows_to_msys_path(raw_target_source)
        else:
            target_source_msys = raw_target_source.replace("\\", "/")

        script = r'''
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

target="@@TARGET@@"
target_source="@@TARGET_SOURCE@@"
native_mcdc="@@NATIVE_MCDC@@"
skip_coverage="${TESTING_MATE_SKIP_COVERAGE:-0}"
gtest_filter="${TESTING_MATE_GTEST_FILTER:-}"
cpputest_filters="${TESTING_MATE_CPPUTEST_FILTERS:-}"
cpputest_group_filter="${TESTING_MATE_CPPUTEST_GROUP:-}"
cpputest_name_filter="${TESTING_MATE_CPPUTEST_NAME:-}"
clean_build="${TESTING_MATE_CLEAN_BUILD:-0}"

report_base="${target}"
report_base="${report_base%_cpputest}"
report_base="${report_base%_gtest}"

mkdir -p "$SCRIPT_DIR/.tmp" "$SCRIPT_DIR/.home"
export TMPDIR="$SCRIPT_DIR/.tmp"
export TMP="$SCRIPT_DIR/.tmp"
export TEMP="$SCRIPT_DIR/.tmp"
export HOME="$SCRIPT_DIR/.home"

unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH LD_LIBRARY_PATH || true

install_msys2_packages() {
  if ! command -v pacman >/dev/null 2>&1; then
    echo "MSYS2 pacman not found; cannot install missing package(s): $*"
    exit 127
  fi

  echo "Installing missing MSYS2 package(s): $*"
  pacman -S --needed --noconfirm "$@"
}

ensure_package() {
  package_name="$1"

  if ! command -v pacman >/dev/null 2>&1; then
    return 0
  fi

  if pacman -Q "$package_name" >/dev/null 2>&1; then
    return 0
  fi

  install_msys2_packages "$package_name"
}

ensure_tool() {
  tool_name="$1"
  shift

  if command -v "$tool_name" >/dev/null 2>&1; then
    return 0
  fi

  for package_name in "$@"; do
    install_msys2_packages "$package_name"

    if command -v "$tool_name" >/dev/null 2>&1; then
      return 0
    fi
  done

  echo "Required tool not found: ${tool_name}"
  exit 127
}

coverage_backend="${TESTING_MATE_COVERAGE_BACKEND:-llvm}"

case "$coverage_backend" in
  legacy_lcov|lcov)
    coverage_backend="legacy_lcov"
    ;;
  *)
    coverage_backend="clang_llvmcov"
    ;;
esac

choose_tool_prefix() {
  if [ -n "${TESTING_MATE_MSYS_PREFIX:-}" ]; then
    tool_prefix="${TESTING_MATE_MSYS_PREFIX}"
    return 0
  fi

  if [ "$coverage_backend" = "clang_llvmcov" ]; then
    if [ -d "/clang64/bin" ]; then
      tool_prefix="/clang64"
    elif [ -d "/mingw64/bin" ]; then
      tool_prefix="/mingw64"
    else
      echo "No MSYS2 /clang64 or /mingw64 prefix found."
      exit 127
    fi
  else
    if [ -d "/mingw64/bin" ]; then
      tool_prefix="/mingw64"
    elif [ -d "/clang64/bin" ]; then
      tool_prefix="/clang64"
    else
      echo "No MSYS2 /mingw64 or /clang64 prefix found."
      exit 127
    fi
  fi
}

choose_tool_prefix

export PATH="${tool_prefix}/bin:/usr/bin:$PATH"
export CMAKE_PREFIX_PATH="${tool_prefix}"

case "$tool_prefix" in
  /clang64*)
    pkg_prefix="mingw-w64-clang-x86_64"
    ignore_prefix="/mingw64"
    ;;
  /mingw64*)
    pkg_prefix="mingw-w64-x86_64"
    ignore_prefix="/clang64"
    ;;
  *)
    pkg_prefix="mingw-w64-x86_64"
    ignore_prefix=""
    ;;
esac

cmake_pkg="${pkg_prefix}-cmake"
ninja_pkg="${pkg_prefix}-ninja"
llvm_pkg="${pkg_prefix}-llvm"
clang_pkg="${pkg_prefix}-clang"
compiler_rt_pkg="${pkg_prefix}-compiler-rt"
gcc_pkg="${pkg_prefix}-gcc"
gtest_pkg="${pkg_prefix}-gtest"
cpputest_pkg="${pkg_prefix}-cpputest"

if [ "$coverage_backend" = "clang_llvmcov" ]; then
  export CC="${TESTING_MATE_CC:-${tool_prefix}/bin/clang.exe}"
  export CXX="${TESTING_MATE_CXX:-${tool_prefix}/bin/clang++.exe}"
else
  export CC="${TESTING_MATE_CC:-${tool_prefix}/bin/gcc.exe}"
  export CXX="${TESTING_MATE_CXX:-${tool_prefix}/bin/g++.exe}"
fi

echo "shell: ${MSYSTEM:-unknown}"
echo "tool prefix: ${tool_prefix}"
echo "coverage backend: ${coverage_backend}"
echo "native MC/DC: ${native_mcdc}"
echo "target: ${target}"
echo "coverage target source: ${target_source}"
echo "CC: ${CC}"
echo "CXX: ${CXX}"

ensure_tool cmake "$cmake_pkg"
ensure_tool ninja "$ninja_pkg"

if [ "$coverage_backend" = "clang_llvmcov" ]; then
  ensure_package "$clang_pkg"
  ensure_package "$compiler_rt_pkg"
  ensure_tool llvm-profdata "$llvm_pkg"
  ensure_tool llvm-cov "$llvm_pkg"
else
  ensure_package "$gcc_pkg"
  ensure_tool lcov lcov
  ensure_tool genhtml lcov
  ensure_tool perl perl
fi

case "$target" in
  *_gtest)
    ensure_package "$gtest_pkg"
    ;;
  *_cpputest)
    ensure_package "$cpputest_pkg"
    ;;
esac

echo "resolved cmake: $(command -v cmake)"
echo "resolved ninja: $(command -v ninja)"
echo "resolved llvm-cov: $(command -v llvm-cov || true)"
echo "resolved llvm-profdata: $(command -v llvm-profdata || true)"

if [ "$coverage_backend" = "clang_llvmcov" ]; then
  echo "clang version:"
  "$CXX" --version || true

  resource_dir="$("$CXX" -print-resource-dir 2>/dev/null || true)"
  runtime_dir="$("$CXX" -print-runtime-dir 2>/dev/null || true)"

  echo "clang resource dir: ${resource_dir}"
  echo "clang runtime dir: ${runtime_dir}"

  shopt -s nullglob
  profile_runtime_candidates=(
    "${runtime_dir}"/libclang_rt.profile*.a
    "${resource_dir}"/lib/windows/libclang_rt.profile*.a
    "${resource_dir}"/lib/x86_64-w64-windows-gnu/libclang_rt.profile*.a
  )

  profile_runtime_found=0
  for profile_runtime in "${profile_runtime_candidates[@]}"; do
    if [ -f "$profile_runtime" ]; then
      echo "found profile runtime: ${profile_runtime}"
      profile_runtime_found=1
      break
    fi
  done

  if [ "$profile_runtime_found" -eq 0 ]; then
    echo "Missing Clang profile runtime library."
    echo "Install matching compiler-rt package:"
    echo "  pacman -S --needed ${compiler_rt_pkg}"
    exit 1
  fi
fi

build_dir="build-clang-cov"
if [ "$coverage_backend" = "legacy_lcov" ]; then
  build_dir="build-lcov"
fi

if [ -e "$build_dir" ] && [ "$clean_build" = "1" ]; then
  if ! rm -rf "$build_dir"; then
    build_dir="build_${target}_$(date +%Y%m%d%H%M%S)_$$"
    echo "Existing build directory is locked; using fresh build directory: ${build_dir}"
  fi
fi

mkdir -p "$build_dir"

cmake_extra_args=()

if [ "$coverage_backend" = "clang_llvmcov" ]; then
  cmake_extra_args+=(
    "-DTESTING_MATE_LLVM_COVERAGE=ON"
    "-DTESTING_MATE_LLVM_MCDC=${native_mcdc}"
    "-DTESTING_MATE_LEGACY_LCOV=OFF"
  )
else
  cmake_extra_args+=(
    "-DTESTING_MATE_LLVM_COVERAGE=OFF"
    "-DTESTING_MATE_LLVM_MCDC=OFF"
    "-DTESTING_MATE_LEGACY_LCOV=ON"
  )
fi

cmake_configure_args=(
  -S .
  -B "$build_dir"
  -G Ninja
  "-DCMAKE_C_COMPILER=${CC}"
  "-DCMAKE_CXX_COMPILER=${CXX}"
  "-DCMAKE_PREFIX_PATH=${tool_prefix}"
  "-DCMAKE_MAKE_PROGRAM=$(command -v ninja)"
)

if [ -n "$ignore_prefix" ]; then
  cmake_configure_args+=("-DCMAKE_IGNORE_PREFIX_PATH=${ignore_prefix}")
fi

cmake_configure_args+=("${cmake_extra_args[@]}")

export LLVM_PROFILE_FILE="$SCRIPT_DIR/${build_dir}/${target}-%p.profraw"

cmake "${cmake_configure_args[@]}"
cmake --build "$build_dir"

exe_path=""
if [ -x "${build_dir}/${target}.exe" ]; then
  exe_path="${build_dir}/${target}.exe"
elif [ -x "${build_dir}/${target}" ]; then
  exe_path="${build_dir}/${target}"
fi

# Keep coverage evidence scoped to the current execution only.  Fallback
# discovery/filtered runs produce .profraw files too; delete old profiles before
# every run so the final full-suite coverage pass is not polluted by stale data.
if [ "$coverage_backend" = "clang_llvmcov" ]; then
  rm -f "${build_dir}"/*.profraw coverage.profdata coverage.json coverage.llvm.json coverage.summary.txt llvm_report.txt || true
fi

test_status=0
if [ -n "$gtest_filter" ] && [[ "$target" == *_gtest ]]; then
  if [ -z "$exe_path" ]; then
    echo "Cannot run GoogleTest filter because executable was not found."
    exit 1
  fi
  echo "Running GoogleTest filter: ${gtest_filter}"
  "$exe_path" --gtest_filter="$gtest_filter" || test_status=$?
elif [ -n "$cpputest_filters" ] && [[ "$target" == *_cpputest ]]; then
  if [ -z "$exe_path" ]; then
    echo "Cannot run CppUTest filter because executable was not found."
    exit 1
  fi
  echo "Running CppUTest failed-case filter list: ${cpputest_filters}"
  IFS=';' read -r -a cpputest_filter_entries <<< "$cpputest_filters"
  for cpputest_entry in "${cpputest_filter_entries[@]}"; do
    if [ -z "$cpputest_entry" ]; then
      continue
    fi
    if [[ "$cpputest_entry" != *"::"* ]]; then
      echo "Skipping malformed CppUTest filter entry: ${cpputest_entry}"
      test_status=1
      continue
    fi
    cpputest_group="${cpputest_entry%%::*}"
    cpputest_name="${cpputest_entry#*::}"
    if [ -z "$cpputest_group" ] || [ -z "$cpputest_name" ]; then
      echo "Skipping incomplete CppUTest filter entry: ${cpputest_entry}"
      test_status=1
      continue
    fi
    echo "Running CppUTest filter: -g ${cpputest_group} -n ${cpputest_name}"
    "$exe_path" -g "$cpputest_group" -n "$cpputest_name" || test_status=$?
  done
elif { [ -n "$cpputest_group_filter" ] || [ -n "$cpputest_name_filter" ]; } && [[ "$target" == *_cpputest ]]; then
  if [ -z "$exe_path" ]; then
    echo "Cannot run CppUTest filter because executable was not found."
    exit 1
  fi
  cpputest_args=()
  if [ -n "$cpputest_group_filter" ]; then
    cpputest_args+=("-g" "$cpputest_group_filter")
  fi
  if [ -n "$cpputest_name_filter" ]; then
    cpputest_args+=("-n" "$cpputest_name_filter")
  fi
  echo "Running CppUTest filter: ${cpputest_args[*]}"
  "$exe_path" "${cpputest_args[@]}" || test_status=$?
else
  ctest --test-dir "$build_dir" --output-on-failure || test_status=$?
fi

case "$skip_coverage" in
  1|true|TRUE|yes|YES|on|ON)
    echo "Skipping coverage generation for fallback/discovery run."
    exit "$test_status"
    ;;
esac

if [ "$coverage_backend" = "clang_llvmcov" ]; then
  if [ -z "$exe_path" ]; then
    echo "Cannot find test executable for llvm-cov."
    exit 1
  fi

  shopt -s nullglob
  profraw_files=("${build_dir}"/*.profraw)

  if [ "${#profraw_files[@]}" -eq 0 ]; then
    echo "No .profraw files were produced."
    echo "Check that -fprofile-instr-generate was applied and the test binary actually ran."
    exit 1
  fi

  llvm-profdata merge --sparse "${profraw_files[@]}" -o coverage.profdata

  llvm_source_args=()
  if [ -n "$target_source" ] && [ -f "$target_source" ]; then
    llvm_source_args+=("$target_source")
  elif [ -n "$target_source" ]; then
    echo "Warning: target source does not exist from MSYS2 shell: ${target_source}"
    echo "LLVM report will run without source filter."
  fi

  report_opts=("-show-branch-summary")

  if [ "$native_mcdc" = "ON" ]; then
    if llvm-cov report --help 2>&1 | grep -q -- "show-mcdc-summary"; then
      report_opts+=("-show-mcdc-summary")
    else
      echo "Warning: this llvm-cov does not support -show-mcdc-summary."
    fi
  fi

  llvm-cov report "$exe_path" \
    -instr-profile=coverage.profdata \
    "${report_opts[@]}" \
    "${llvm_source_args[@]}" \
    > llvm_report.txt

  cp llvm_report.txt coverage.summary.txt
  cat llvm_report.txt

  show_opts=(
    "-format=html"
    "-output-dir=llvm_html"
    "-show-line-counts-or-regions"
    "-show-branches=count"
  )

  if [ "$native_mcdc" = "ON" ]; then
    if llvm-cov show --help 2>&1 | grep -q -- "show-mcdc"; then
      show_opts+=("-show-mcdc")
    fi

    if llvm-cov show --help 2>&1 | grep -q -- "show-mcdc-non-executed-vectors"; then
      show_opts+=("-show-mcdc-non-executed-vectors")
    else
      echo "Info: this llvm-cov does not support -show-mcdc-non-executed-vectors."
    fi
  fi

  llvm-cov show "$exe_path" \
    -instr-profile=coverage.profdata \
    "${show_opts[@]}" \
    "${llvm_source_args[@]}"

  rm -rf "${report_base}_llvmcov"
  cp -R llvm_html "${report_base}_llvmcov"

  llvm-cov export "$exe_path" \
    -instr-profile=coverage.profdata \
    -format=text \
    "${llvm_source_args[@]}" \
    > coverage.json

  cp coverage.json coverage.llvm.json

  echo "Generated LLVM coverage:"
  echo "  llvm_report.txt"
  echo "  coverage.json"
  echo "  llvm_html/index.html"
  echo "  ${report_base}_llvmcov/index.html"
else
  if command -v lcov >/dev/null 2>&1 && command -v genhtml >/dev/null 2>&1; then
    rm -f coverage.raw.info coverage.info coverage.info.tmp

    lcov \
      --capture \
      --directory "$build_dir" \
      --base-directory . \
      --rc lcov_branch_coverage=1 \
      --rc branch_coverage=1 \
      --output-file coverage.raw.info

    if [ -n "$target_source" ]; then
      export TESTMATE_TARGET_SOURCE="$target_source"

      perl -0777 -ne 'BEGIN {
        $target = lc($ENV{"TESTMATE_TARGET_SOURCE"});
        $target =~ s!\\!/!g;
        $target =~ s!^\./!!;
      }
      for $rec (split(/end_of_record\n?/, $_)) {
        next unless $rec =~ /\S/;
        if ($rec =~ /^SF:(.+)$/m) {
          $sf = lc($1);
          $sf =~ s!\\!/!g;
          $sf =~ s!^\./!!;
          if ($sf eq $target || $sf =~ /\Q$target\E$/) {
            print $rec;
            print "end_of_record\n";
          }
        }
      }' coverage.raw.info > coverage.info.tmp

      if grep -q "^SF:" coverage.info.tmp; then
        mv coverage.info.tmp coverage.info
        echo "Filtered LCOV to target source: ${target_source}"
      else
        echo "No LCOV records found for target source ${target_source}; available records:"
        sed -n "s#^SF:##p" coverage.raw.info | head -80
        rm -f coverage.info coverage.info.tmp
      fi
    else
      cp coverage.raw.info coverage.info
    fi

    if [ -f coverage.info ]; then
      genhtml \
        coverage.info \
        --branch-coverage \
        --title "${report_base} legacy target-only" \
        --output-directory "${report_base}_lcov"

      echo "Generated legacy C0/C1 coverage:"
      echo "  coverage.info"
      echo "  ${report_base}_lcov/index.html"
    fi
  else
    echo "lcov/genhtml not found; install lcov before using legacy C0/C1 coverage."
  fi
fi

exit "$test_status"
'''

        return (
            textwrap.dedent(script)
            .lstrip()
            .replace("@@TARGET@@", bash_dq(target))
            .replace("@@TARGET_SOURCE@@", bash_dq(target_source_msys))
            .replace("@@NATIVE_MCDC@@", "ON" if enable_mcdc else "OFF")
        )

class TestBackendRunner:
    @staticmethod
    def gtest_failed_filter_from_result(run_result: RunResult, max_cases: int = 80) -> str:
        text = TestOracleFallback.backend_execution_text(run_result)
        if not text.strip():
            return ""
        ordered: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r"(?m)^\[\s*FAILED\s*\]\s+([^\s(]+)", text):
            name = match.group(1).strip()
            if not name or name.isdigit() or "." not in name:
                continue
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        if not ordered or len(ordered) > max_cases:
            return ""
        return ":".join(ordered)

    @staticmethod
    def cpputest_failed_filter_from_result(run_result: RunResult, max_cases: int = 80) -> str:
        text = TestOracleFallback.backend_execution_text(run_result)
        if not text.strip():
            return ""
        ordered: list[str] = []
        seen: set[str] = set()
        patterns = [
            r"Failure\s+in\s+TEST\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
            r"(?m)^\s*TEST\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)\s*(?:failed|failure|error)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I):
                group = match.group(1).strip()
                name = match.group(2).strip()
                if not group or not name or name.isdigit():
                    continue
                entry = f"{group}::{name}"
                if entry not in seen:
                    seen.add(entry)
                    ordered.append(entry)
        if not ordered or len(ordered) > max_cases:
            return ""
        return ";".join(ordered)

    @classmethod
    def failed_filter_from_result(cls, backend: str, run_result: RunResult, max_cases: int = 80) -> str:
        if backend == "GoogleTest":
            return cls.gtest_failed_filter_from_result(run_result, max_cases)
        if backend == "CppUTest":
            return cls.cpputest_failed_filter_from_result(run_result, max_cases)
        return ""

    @staticmethod
    def _as_path(value: Path | str) -> Path:
        return value if isinstance(value, Path) else Path(str(value))

    @staticmethod
    def output_limit() -> int:
        raw = os.environ.get("TESTING_MATE_RUN_OUTPUT_LIMIT", "2000000")
        try:
            return max(0, int(raw))
        except ValueError:
            return 2000000

    @classmethod
    def trim_output(cls, text: str) -> str:
        limit = cls.output_limit()
        text = str(text or "")
        return text if limit <= 0 or len(text) <= limit else text[-limit:]

    def run(
        self,
        backend: str,
        out_dir: Path | str,
        source: Path | str,
        *,
        test_filter: str | None = None,
        skip_coverage: bool = False,
        clean_build: bool = False,
    ) -> RunResult:
        out_dir = self._as_path(out_dir).expanduser()
        source = self._as_path(source).expanduser()
        env_extra: dict[str, str] = {}
        if skip_coverage:
            env_extra["TESTING_MATE_SKIP_COVERAGE"] = "1"
        if clean_build:
            env_extra["TESTING_MATE_CLEAN_BUILD"] = "1"
        if test_filter:
            if backend == "GoogleTest":
                env_extra["TESTING_MATE_GTEST_FILTER"] = str(test_filter)
            elif backend == "CppUTest":
                filter_text = str(test_filter)
                if "::" in filter_text or ";" in filter_text:
                    env_extra["TESTING_MATE_CPPUTEST_FILTERS"] = filter_text
                else:
                    env_extra["TESTING_MATE_CPPUTEST_NAME"] = filter_text
        if backend == "CppUTest":
            script = out_dir / f"{source.stem}_cpputest" / "run_cpputest.sh"
            return self._run_msys_script(script, backend, env_extra=env_extra)
        if backend == "GoogleTest":
            script = out_dir / f"{source.stem}_gtest" / "run_gtest.sh"
            return self._run_msys_script(script, backend, env_extra=env_extra)
        return RunResult(backend=backend, command="", returncode=None, skipped_reason="Unknown backend")

    def _run_msys_script(self, script: Path | str, backend: str, env_extra: dict[str, str] | None = None) -> RunResult:
        script = self._as_path(script).expanduser()
        bash = msys_bash_path()
        command_label = f"MSYS2 bash {script}"
        if not script.exists():
            return RunResult(backend=backend, command=command_label, returncode=None, skipped_reason="Runner script was not generated")
        if not bash:
            return RunResult(backend=backend, command=command_label, returncode=None, skipped_reason="MSYS2 bash was not found")
        env_extra = dict(env_extra or {})
        direct_filtered_run = any(key in env_extra for key in {
            "TESTING_MATE_GTEST_FILTER",
            "TESTING_MATE_CPPUTEST_FILTERS",
            "TESTING_MATE_CPPUTEST_GROUP",
            "TESTING_MATE_CPPUTEST_NAME",
        })
        self._cleanup_ctest_temp_logs(script.parent)
        run_started_at = time.time()
        msys_script = windows_to_msys_path(script)
        if bash.lower().endswith("bash.exe"):
            work_dir = windows_to_msys_path(script.parent)
            mingw_dir = windows_to_msys_path(local_mingw_bin())
            result = self._run_command([bash, "-lc", f"export MSYSTEM=MINGW64; export HOME='{work_dir}/.home'; export TMPDIR='{work_dir}/.tmp'; export PATH='{mingw_dir}':/usr/bin:$PATH; bash '{msys_script}'"], backend, command_label, env_extra=env_extra)
        else:
            result = self._run_command([bash, "-defterm", "-no-start", "-mingw64", "-c", f"bash '{msys_script}'"], backend, command_label, env_extra=env_extra)
        if direct_filtered_run:
            # Direct --gtest_filter / CppUTest -g/-n runs do not refresh CTest's
            # LastTest.log.  Do not append stale full-suite evidence to the
            # current filtered retry output.
            return result
        return self._attach_ctest_lasttest(result, script.parent, run_started_at=run_started_at)

    def _run_command(self, cmd: list[str], backend: str, command_label: str, env_extra: dict[str, str] | None = None) -> RunResult:
        try:
            timeout = int(os.environ.get("TESTING_MATE_RUN_TIMEOUT", "900"))
            env = toolchain_env()
            if env_extra:
                env.update({str(key): str(value) for key, value in env_extra.items()})
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env)
            return RunResult(backend=backend, command=command_label, returncode=result.returncode, stdout=self.trim_output(result.stdout), stderr=self.trim_output(result.stderr))
        except Exception as exc:
            return RunResult(backend=backend, command=command_label, returncode=None, skipped_reason=str(exc))

    @staticmethod
    def _cleanup_ctest_temp_logs(test_dir: Path | str) -> None:
        """Remove stale CTest temp logs before a new backend execution."""
        base = Path(str(test_dir))
        temporary_dirs = [base / "build-clang-cov" / "Testing" / "Temporary", base / "build-lcov" / "Testing" / "Temporary"]
        for temp_dir in temporary_dirs:
            for name in ("LastTest.log", "LastTestsFailed.log", "CTestCostData.txt"):
                try:
                    path = temp_dir / name
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass

    @classmethod
    def _attach_ctest_lasttest(cls, run_result: RunResult, test_dir: Path | str, run_started_at: float | None = None) -> RunResult:
        """Append fresh CTest LastTest.log as parse evidence.

        CTest logs are only valid for the current run. Filtered direct test
        reruns and stale temp files must not be mixed into reconciliation.
        """
        base = Path(str(test_dir))
        try:
            candidates = list(base.rglob("LastTest.log"))
        except OSError:
            candidates = []
        if not candidates:
            return run_result
        latest = max(candidates, key=lambda item: item.stat().st_mtime if item.exists() else 0)
        try:
            latest_mtime = latest.stat().st_mtime
        except OSError:
            return run_result
        if run_started_at is not None and latest_mtime + 1.0 < float(run_started_at):
            # Stale log from an older full-suite run. Ignore it.
            return run_result
        try:
            log_text = latest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return run_result
        if not log_text.strip():
            return run_result
        stdout = str(run_result.stdout or "")
        if log_text[:1000] in stdout:
            return run_result
        run_result.stdout = cls.trim_output(stdout + "\n\n=== CTest LastTest.log ===\n" + log_text)
        return run_result
    
    def extract_mcdc_metrics(self, out_dir: Path | str, source: Path | str, backend: str) -> dict[str, float | None]:
        out_dir = self._as_path(out_dir).expanduser()
        source = self._as_path(source).expanduser()
        """Extract MC/DC metrics from test execution coverage outputs.
        
        Looks for llvm-cov generated files:
        - coverage.summary.txt (text summary with percentages)
        - coverage.llvm.json (structured coverage data)
        
        Returns dict with mc_dc, lines, branches percentages or empty dict if not found.
        """
        collector = MCDCCoverageCollector()
        
        # Try to find coverage output directory
        test_dirs = [
            out_dir / f"{source.stem}_cpputest",
            out_dir / f"{source.stem}_gtest",
        ]
        
        for test_dir in test_dirs:
            if not test_dir.exists():
                continue
            
            summary_file = test_dir / "llvm_report.txt"
            if not summary_file.exists():
                summary_file = test_dir / "coverage.summary.txt"
            if summary_file.exists():
                return collector.extract_mcdc_from_summary(summary_file)
        
        return {}
