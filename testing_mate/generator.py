from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from testing_mate.code_database import CodeDatabase
from testing_mate.constants import C_SOURCE_EXTENSIONS, SCALAR_TOKENS
from testing_mate.mcdc_engine import MCDCCoverageCollector
from testing_mate.models import FunctionInfo, Parameter, RunResult, TestCase, Condition
from testing_mate.boolean_expression import ConditionEvaluator
from testing_mate.stub_engine import StubHarnessEngine, StubSymbol
from testing_mate.toolchain import local_mingw_bin, msys_bash_path, toolchain_env, windows_to_msys_path
from testing_mate.vector_generator import TestVectorGenerator


def backend_harness_path(out_dir: Path, source: Path, backend: str) -> Path:
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
    def apply(cls, backend: str, out_dir: Path, source: Path, run_result: RunResult) -> tuple[int, list[str]]:
        if run_result.returncode in (0, None):
            return 0, []
        test_source = cls._test_source_path(backend, out_dir, source)
        if not test_source.exists():
            return 0, [f"Oracle fallback skipped: test source not found: {test_source}"]
        try:
            text = test_source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return 0, [f"Oracle fallback skipped: cannot read {test_source}: {exc}"]

        mismatches = cls.expected_actual_mismatches(run_result)
        if not mismatches:
            return 0, ["Oracle fallback found no parseable expected/actual equality failures."]

        type_map = cls._fixture_type_map(text)
        updated = 0
        notes: list[str] = []
        for mismatch in mismatches:
            literal = cls._literal_from_observed_value(mismatch.get("actual_value", ""))
            expected = mismatch.get("expected", "")
            test_name = mismatch.get("test", "")
            if not literal or not expected or not test_name:
                continue
            next_text, changed = cls._patch_expected_assignment(text, test_name, expected, literal, type_map)
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
    def expected_actual_mismatches(cls, run_result: RunResult) -> list[dict[str, str]]:
        text = "\n".join(part for part in (run_result.stdout, run_result.stderr) if part)
        if not text.strip():
            return []
        return cls._gtest_mismatches(text) + cls._cpputest_mismatches(text)

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
        text = "\n".join(part for part in (run_result.stdout, run_result.stderr) if part)
        for line in text.splitlines():
            clean = " ".join(line.strip().split())
            if clean and any(token in clean.lower() for token in ("failed", "failure", "error", "expected", "actual")):
                summaries.append(clean[:500])
            if len(summaries) >= limit:
                break
        return summaries

    @staticmethod
    def _test_source_path(backend: str, out_dir: Path, source: Path) -> Path:
        if backend == "CppUTest":
            return out_dir / f"{source.stem}_cpputest" / f"{source.stem}_cpputest_test.cpp"
        return out_dir / f"{source.stem}_gtest" / f"{source.stem}_gtest_test.cpp"

    @staticmethod
    def _gtest_mismatches(text: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        current_test = ""
        lines = text.splitlines()
        index = 0
        while index < len(lines):
            run_match = re.search(r"^\[\s*RUN\s*\]\s+(.+)$", lines[index])
            if run_match:
                current_test = run_match.group(1).strip().rsplit(".", 1)[-1]
            if "Expected equality of these values:" in lines[index]:
                parsed = TestOracleFallback._parse_gtest_equality_block(lines[index + 1 : index + 8])
                if parsed:
                    parsed["test"] = current_test
                    result.append(parsed)
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
    def _cpputest_mismatches(text: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        current_test = ""
        for line in text.splitlines():
            test_match = re.search(r"TEST\(([^,]+),\s*([^)]+)\)", line)
            if test_match:
                current_test = test_match.group(2).strip()
            match = re.search(r"expected\s*<([^>]+)>.*(?:but was|actual)\s*<([^>]+)>", line, re.I)
            if match:
                result.append({
                    "test": current_test,
                    "expected": "expected_return",
                    "expected_value": match.group(1).strip(),
                    "actual": "actual_return",
                    "actual_value": match.group(2).strip(),
                })
        return result

    @staticmethod
    def _fixture_type_map(text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for match in re.finditer(r"(?m)^\s*(?P<type>[A-Za-z_]\w*(?:\s+[*A-Za-z_]\w*)*)\s+(?P<name>[A-Za-z_]\w*)\s*;", text):
            result[match.group("name")] = " ".join(match.group("type").split())
        return result

    @staticmethod
    def _patch_expected_assignment(
        text: str,
        test_name: str,
        expected: str,
        literal: str,
        type_map: dict[str, str],
    ) -> tuple[str, bool]:
        span = TestOracleFallback._test_block_span(text, test_name)
        if span is None:
            return text, False
        start, end = span
        block = text[start:end]
        expected_root = expected.split(".", 1)[0]
        ctype = type_map.get(expected_root, "")
        typed = TestBackendGenerator._typed_value(ctype, literal) if ctype else literal
        pattern = re.compile(rf"(?m)^(\s*{re.escape(expected)}\s*=\s*)([^;]+)(;)")
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
    def _literal_from_observed_value(value: str) -> str:
        clean = value.strip()
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


class TestMockFallback:
    """Patch generated mock expectations from backend mock-failure evidence."""

    @classmethod
    def apply(cls, backend: str, out_dir: Path, source: Path, run_result: RunResult) -> tuple[int, list[str]]:
        if run_result.returncode in (0, None):
            return 0, []
        test_source = TestOracleFallback._test_source_path(backend, out_dir, source)
        if not test_source.exists():
            return 0, [f"Mock fallback skipped: test source not found: {test_source}"]
        try:
            text = test_source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return 0, [f"Mock fallback skipped: cannot read {test_source}: {exc}"]

        failures = cls.mock_failures(run_result)
        if not failures:
            return 0, ["Mock fallback found no parseable mock failures."]

        mock_info = cls._mock_info_map(test_source)
        updated = 0
        notes: list[str] = []
        for failure in failures:
            test_name = failure.get("test", "")
            function = failure.get("function", "")
            action = failure.get("action", "")
            if not test_name or not function or action not in {"add", "remove"}:
                continue
            if action == "remove":
                text, changed = cls._remove_mock_expectation(text, test_name, function)
                if changed:
                    updated += 1
                    notes.append(f"Mock fallback removed redundant expectation: {test_name} -> {function}.")
                else:
                    notes.append(f"Mock fallback could not remove expectation: {test_name} -> {function}.")
            else:
                text, changed = cls._add_mock_expectation(text, backend, test_name, function, mock_info)
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
    def mock_failures(run_result: RunResult) -> list[dict[str, str]]:
        text = "\n".join(part for part in (run_result.stdout, run_result.stderr) if part)
        if not text.strip():
            return []
        failures: list[dict[str, str]] = []
        current_test = ""
        cpputest_unfulfilled = False
        unexpected_context = False
        lines = text.splitlines()
        for index, line in enumerate(lines):
            test_match = re.search(r"\bTEST(?:_F)?\s*\(\s*[^,]+,\s*([^)]+)\)", line)
            if test_match:
                current_test = test_match.group(1).strip()
            run_match = re.search(r"^\[\s*RUN\s*\]\s+(.+)$", line)
            if run_match:
                current_test = run_match.group(1).strip().rsplit(".", 1)[-1]

            if "EXPECTED calls that WERE NOT fulfilled" in line:
                cpputest_unfulfilled = True
                unexpected_context = False
                continue
            if "EXPECTED calls that WERE fulfilled" in line:
                cpputest_unfulfilled = False
                continue
            if cpputest_unfulfilled:
                call_match = re.search(r"^\s*([A-Za-z_]\w*)\s*->", line)
                if call_match and current_test:
                    failures.append({"test": current_test, "function": call_match.group(1), "action": "remove"})
                continue

            lower = line.lower()
            unexpected_direct = re.search(
                r"unexpected\s+(?:mock\s+)?(?:function\s+)?call(?:\s+to\s+function)?\s*:?\s*\"?([A-Za-z_]\w*)",
                line,
                re.I,
            )
            if unexpected_direct and current_test:
                failures.append({"test": current_test, "function": unexpected_direct.group(1), "action": "add"})
                continue
            if "unexpected mock function call" in lower or ("mock failure" in lower and "unexpected" in lower):
                unexpected_context = True
            if unexpected_context:
                call_match = re.search(r"(?:Function call:\s*|^\s*)([A-Za-z_]\w*)\s*(?:\(|->)", line)
                if call_match and current_test and call_match.group(1) not in {"Mock", "Function"}:
                    failures.append({"test": current_test, "function": call_match.group(1), "action": "add"})
                    unexpected_context = False
                    continue
                if index + 1 < len(lines):
                    next_match = re.search(r"^\s*([A-Za-z_]\w*)\s*(?:\(|->)", lines[index + 1])
                    if next_match and current_test:
                        failures.append({"test": current_test, "function": next_match.group(1), "action": "add"})
                        unexpected_context = False
                        continue

            if "actual: never called" in lower:
                window = "\n".join(lines[max(0, index - 8): index + 1])
                for call_match in re.finditer(r"EXPECT_CALL\([^,]+,\s*([A-Za-z_]\w*)\s*\(", window):
                    if current_test:
                        failures.append({"test": current_test, "function": call_match.group(1), "action": "remove"})

        unique: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in failures:
            key = (item["test"], item["function"], item["action"])
            if key not in seen:
                unique.append(item)
                seen.add(key)
        return unique

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
    ) -> tuple[str, bool]:
        span = TestOracleFallback._test_block_span(text, test_name)
        if span is None:
            return text, False
        start, end = span
        block = text[start:end]
        safe = TestBackendGenerator._safe_identifier(function)
        if f'expectOneCall("{function}")' in block or f"EXPECT_CALL(TestingMate_Mock(), {safe}(" in block:
            return text, False
        info = mock_info.get(safe, {})
        if backend == "CppUTest":
            line = f'    mock().expectOneCall("{function}").ignoreOtherParameters();\n'
        else:
            arity = int(info.get("arity") or 0)
            args = ", ".join("::testing::_" for _ in range(arity))
            ret = info.get("return_type", "void")
            if ret == "void":
                line = f"    EXPECT_CALL(TestingMate_Mock(), {safe}({args}));\n"
            else:
                line = f"    EXPECT_CALL(TestingMate_Mock(), {safe}({args})).WillOnce(::testing::Return({cls_default_return(ret)}));\n"
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
            result[match.group(2)] = {"return_type": " ".join(match.group(1).split()), "arity": str(arity)}
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
    include_pattern = re.compile(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", re.M)

    def __init__(self, out_dir: Path, include_dirs: list[str] | None = None):
        self.out_dir = out_dir
        self.include_dirs = [Path(item).expanduser() for item in (include_dirs or []) if item]
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def write_backend(
        self,
        backend: str,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        level: str,
        unit_sources: list[Path] | None = None,
        code_database: CodeDatabase | None = None,
    ) -> list[Path]:
        # Keep native LLVM MC/DC instrumentation strictly tied to the selected
        # coverage objective. C0/C1 generation must not compile or report with
        # -fcoverage-mcdc, even when the clang/llvm-cov backend is used.
        enable_mcdc = self._cases_request_mcdc(cases)
        if backend == "CppUTest":
            return self._write_cpputest(source, functions, cases, unit_sources or [source], code_database, enable_mcdc)
        if backend == "GoogleTest":
            return self._write_gtest(source, functions, cases, unit_sources or [source], code_database, enable_mcdc)
        return []

    @staticmethod
    def _cases_request_mcdc(cases: list[TestCase]) -> bool:
        return any(str(case.coverage).strip().upper() in {"MC/DC", "MCDC"} for case in cases)

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
        harness_sources = [source]
        copied = self._copy_target_source(source, source_env_dir)
        test_file = test_dir / f"{source.stem}_cpputest_test.cpp"
        cmake_file = test_dir / "CMakeLists.txt"
        runner = test_dir / "run_cpputest.sh"
        link_sources = self._link_unit_sources(harness_sources)
        stub_result = StubHarnessEngine().write_for_backend("CppUTest", test_dir, source, functions, cases, harness_sources, self.include_dirs, code_database)
        c_sources = " ".join([*(f"src/{path.name}" for path in link_sources), *self._relative_cmake_paths(stub_result.source_files, test_dir)])
        include_dirs = self._cmake_include_dirs(source, harness_sources, [Path("src"), Path("stub/include")])
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
        runner.write_text(self._cmake_runner(source.stem + "_cpputest", f"src/{source.name}", enable_mcdc), encoding="utf-8")
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
        harness_sources = [source]
        copied = self._copy_target_source(source, source_env_dir)
        test_file = test_dir / f"{source.stem}_gtest_test.cpp"
        cmake_file = test_dir / "CMakeLists.txt"
        runner = test_dir / "run_gtest.sh"
        link_sources = self._link_unit_sources(harness_sources)
        stub_result = StubHarnessEngine().write_for_backend("GoogleTest", test_dir, source, functions, cases, harness_sources, self.include_dirs, code_database)
        c_sources = " ".join([*(f"src/{path.name}" for path in link_sources), *self._relative_cmake_paths(stub_result.source_files, test_dir)])
        include_dirs = self._cmake_include_dirs(source, harness_sources, [Path("src"), Path("stub/include")])
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
        runner.write_text(self._cmake_runner(source.stem + "_gtest", f"src/{source.name}", enable_mcdc), encoding="utf-8")
        return [*copied, *stub_result.generated_paths, test_file, cmake_file, runner]

    def _copy_target_source(self, source: Path, test_dir: Path) -> list[Path]:
        copied: list[Path] = []
        if not source.exists() or not source.is_file():
            return copied
        test_dir.mkdir(parents=True, exist_ok=True)
        target = test_dir / source.name
        shutil.copy2(source, target)
        copied.append(target)
        return copied

    def _copy_unit_files(self, sources: list[Path], test_dir: Path) -> list[Path]:
        copied: list[Path] = []
        seen: set[Path] = set()
        queue: list[Path] = []
        for source in sources:
            for path in (source, source.with_suffix(".h")):
                self._copy_project_file(path, test_dir, copied, seen, queue, preserve_from=None)
        index = 0
        while index < len(queue):
            path = queue[index]
            index += 1
            for include_name in self._extract_includes(path):
                resolved = self._resolve_include(include_name, path.parent, sources)
                if resolved:
                    self._copy_project_file(resolved, test_dir, copied, seen, queue, preserve_from=self._matching_include_root(resolved, sources))
        return copied

    def _link_unit_sources(self, sources: list[Path]) -> list[Path]:
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
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        if preserve_from:
            try:
                relative = resolved.relative_to(preserve_from.resolve())
            except ValueError:
                relative = Path(path.name)
        else:
            relative = Path(path.name)
        target = test_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(target)
        if path.suffix.lower() in {".c", ".h", ".hpp"}:
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
        for case in cases:
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
            global_writes = self._filter_global_names(fn, self._written_global_names(fn), global_type_map)
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
                inferred["globals"] = self._filter_global_literals(fn, inferred.get("globals") or {}, global_type_map)
                inferred_global_names.update((inferred.get("globals") or {}).keys())
                inferred_output_names.update((inferred.get("outputs") or {}).keys())
            global_writes = self._filter_global_names(fn, set(global_writes) | inferred_global_names, global_type_map)
            suite_globals = self._filter_global_names(fn, self._suite_lifecycle_global_names(fn_cases, global_writes) | inferred_global_names, global_type_map)

            fixture_declarations: list[str] = []
            declared_fixture_names: set[str] = set()

            def add_fixture_declaration(c_type: str, name: str) -> None:
                # Every variable assigned in setup/teardown/PRECONDITION/ACTION must be
                # declared once in the test group before any generated assignment uses it.
                safe_name = self._safe_identifier(name)
                if not self._is_c_identifier(safe_name) or safe_name in declared_fixture_names:
                    return
                safe_type = self._clean_fixture_type(c_type, defaults="uint8", mutable=True)
                if not self._is_valid_fixture_type(safe_type):
                    safe_type = "uint8"
                fixture_declarations.append(f"{safe_type} {safe_name};")
                declared_fixture_names.add(safe_name)

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
            if not self._is_void_return_type(fn.return_type):
                return_fixture_name = "actual_return"
                return_type = self._return_fixture_type(fn)
                add_fixture_declaration(return_type, return_fixture_name)
                setup_reset_lines.append(f"{return_fixture_name} = {{}};")
                teardown_reset_lines.append(f"{return_fixture_name} = {{}};")

            for local_name, local_type in self._suite_local_fixture_declarations(fn_cases, fn).items():
                add_fixture_declaration(local_type, local_name)
                setup_reset_lines.append(f"{local_name} = {{}};")
                teardown_reset_lines.append(f"{local_name} = {{}};")

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
                )
                lines.append("    /* PRECONDITION */")
                lines.extend(precondition_lines if precondition_lines else ["    /* No explicit precondition values were generated. */"])
                manual_precondition = self._manual_code_lines(case, "__manual_precondition_code", "    ")
                if manual_precondition:
                    lines.append("    /* MANUAL PRECONDITION CODE */")
                    lines.extend(manual_precondition)

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
                    self._expected_stub_call_names(case, stubbed_calls),
                    "    ",
                )
                if expected_call_lines:
                    lines.append("    /* EXPECTED CALLS */")
                    lines.extend(expected_call_lines)
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
    def _state_setup_lines(case: TestCase | dict, indent: str = "") -> list[str]:
        values = case if isinstance(case, dict) else (case.values or {})
        lines: list[str] = []
        for key, value in sorted(values.items()):
            for prefix in ("__global.", "__set_global.", "__input_global.", "__setup_global.", "setup.global."):
                if key.startswith(prefix):
                    symbol = key[len(prefix) :]
                    if TestBackendGenerator._is_c_identifier(symbol):
                        lines.append(f"{indent}{symbol} = {value};")
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
            symbol = stub_symbols.get(name)
            safe = TestBackendGenerator._safe_identifier(name)
            value = values.get(f"__stub_return.{name}", "0")
            expected_args = TestBackendGenerator._expected_call_arg_values(name, values)
            if backend == "CppUTest":
                param_expectations = TestBackendGenerator._cpputest_expected_param_chain(symbol, expected_args)
                ignore_params = ".ignoreOtherParameters()" if symbol and TestBackendGenerator._has_mock_params(symbol.params) and not param_expectations else ""
                if symbol and TestBackendGenerator._is_void_return_type(symbol.return_type):
                    lines.append(f"{indent}mock().expectOneCall(\"{name}\"){param_expectations}{ignore_params};")
                else:
                    return_type = TestBackendGenerator._mock_return_type(symbol.return_type if symbol else "")
                    lines.append(f"{indent}mock().expectOneCall(\"{name}\"){param_expectations}{ignore_params}.andReturnValue({TestBackendGenerator._typed_value(return_type, str(value))});")
            else:
                args = TestBackendGenerator._gmock_argument_matchers(symbol.params if symbol else "void", expected_args)
                if symbol and TestBackendGenerator._is_void_return_type(symbol.return_type):
                    lines.append(f"{indent}EXPECT_CALL(TestingMate_Mock(), {safe}({args}));")
                else:
                    return_type = TestBackendGenerator._mock_return_type(symbol.return_type if symbol else "")
                    typed_value = TestBackendGenerator._typed_value(return_type, str(value)) if symbol else str(value)
                    lines.append(
                        f"{indent}EXPECT_CALL(TestingMate_Mock(), {safe}({args})).WillOnce(::testing::Return({typed_value}));"
                    )
        return lines

    @staticmethod
    def _expected_stub_call_names(case: TestCase, stubbed_calls: set[str]) -> list[str]:
        values = case.values or {}
        names: list[str] = []

        def add(raw: object) -> None:
            for item in re.split(r"[,;\s]+", str(raw or "")):
                clean = item.strip()
                if clean in stubbed_calls and clean not in names:
                    names.append(clean)

        for key, value in sorted(values.items()):
            if key in {"__expected_calls", "__expect_calls", "expected_calls", "expect_calls"}:
                add(value)
            elif key.startswith("__expected_call."):
                add(key.split(".", 1)[1])
            elif key.startswith("__stub_return."):
                add(key.split(".", 1)[1])
        return names

    @staticmethod
    def _gmock_argument_matchers(params: str, expected_args: dict[int, str] | None = None) -> str:
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
        matchers: list[str] = []
        for index, item in enumerate(item for item in items if item and item != "void"):
            value = expected_args.get(index)
            if value is None:
                matchers.append("::testing::_")
                continue
            name = StubHarnessEngine._param_name(item, index)
            ctype = StubHarnessEngine._param_type(item, name) if name else item
            if "*" in ctype and str(value).strip() not in {"0", "NULL", "nullptr"} and not str(value).strip().startswith("&"):
                matchers.append("::testing::_")
                continue
            matchers.append(f"::testing::Eq({TestBackendGenerator._typed_value(ctype, value)})")
        return ", ".join(matchers)

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
        """Return every function-local variable that must exist as a fixture member.

        Earlier versions only declared locals when a testcase or FixItHint referenced
        them. That still allowed generated setup/precondition/oracle logic to touch a
        function-local symbol before the fixture had declared it. Keep the source
        function's local variables as first-class fixture members so all generated
        assignments/check expressions have declarations before use.

        Scope precedence is still enforced elsewhere: parameter > local > global.
        Declaring these members does not make them expected globals; it only prevents
        undeclared test-harness symbols when a heuristic/hint legitimately refers to a
        function-local scratch value.
        """
        locals_: dict[str, str] = {}
        source_local_types = TestBackendGenerator._local_variable_type_map(fn) if fn is not None else {}

        # Always declare every parsed function-local variable first, using the original
        # source/header type when available. Testcase-specific local hints below can add
        # extra review variables, but they must not override source-derived types.
        for name, ctype in source_local_types.items():
            if name and TestBackendGenerator._is_c_identifier(name):
                locals_.setdefault(name, ctype or  "uint8")
        for case in cases:
            for name, value in TestBackendGenerator._fixit_local_literals(fn, case).items():
                if name and TestBackendGenerator._is_c_identifier(name) and name not in locals_:
                    locals_[name] = source_local_types.get(name) or TestBackendGenerator._fixture_type_from_value(str(value))
            for key, value in (case.values or {}).items():
                name = TestBackendGenerator._fixture_local_key_name(key)
                if name and TestBackendGenerator._is_c_identifier(name) and name not in locals_:
                    locals_[name] = source_local_types.get(name) or TestBackendGenerator._fixture_type_from_value(str(value))
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
            expected_return = self._expected_return_literal(case)
            if not expected_return:
                expected_return = str(inferred_expectations.get("return") or "")
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
            global_type = global_type_map.get(global_name) or self._fixture_type_from_value(str(expected_value))
            lines.append(f"{indent}{expected_fixture} = {self._typed_value(global_type, str(expected_value))};")
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
    ) -> list[str]:
        values = {**self._fixit_input_literals(fn, case), **(case.values or {})}
        lines: list[str] = []
        lines.extend(self._state_setup_lines(values, indent))
        explicit_calls = self._expected_stub_call_names(case, stubbed_calls or set())
        lines.extend(self._stub_expectation_setup_lines(
            backend,
            explicit_calls,
            values,
            stub_symbols or {},
            indent,
        ))

        for key, value in sorted(values.items()):
            local_name = self._fixture_local_key_name(key)
            if local_name and self._is_c_identifier(local_name):
                lines.append(f"{indent}{local_name} = {value};")

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

    def _type_map_sources(self, source: Path, unit_sources: list[Path] | None) -> list[Path]:
        roots = [source, *(unit_sources or [])]
        result: list[Path] = []
        seen: set[str] = set()
        queue: list[tuple[Path, int]] = [(path, 0) for path in roots if path]
        while queue:
            path, depth = queue.pop(0)
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
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
    def _harness_expression_for_expected(value: str, values: dict[str, str], param_by_name: dict[str, Parameter]) -> str:
        expr = str(value or "").strip()
        if not TestBackendGenerator._is_c_identifier(expr):
            return expr
        if expr in values:
            return str(values[expr])
        if expr in param_by_name:
            return f"tm_{TestBackendGenerator._safe_identifier(expr)}"
        return expr

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
        stmt = " ".join((stmt or "").replace("\n", " ").split()).strip()
        if not stmt:
            return {}
        first_word = stmt.split(None, 1)[0]
        first_word = first_word.rstrip(":")
        forbidden_starts = {
            "if", "else", "for", "while", "switch", "case", "default",
            "return", "break", "continue", "goto", "do", "sizeof",
        }
        if first_word in forbidden_starts:
            return {}
        if re.match(r"^(?:case\b.*|default)\s*:", stmt):
            return {}
        if ":" in stmt.split("=", 1)[0]:
            return {}
        if stmt.startswith(("*", "&", "++", "--")):
            return {}
        # Function calls/control headers are not declarations. Casts in initializers
        # are allowed because we only inspect the declarator head before '=' below.
        head_for_call_check = stmt.split("=", 1)[0]
        if "(" in head_for_call_check or ")" in head_for_call_check:
            return {}
        if re.search(r"(==|!=|<=|>=|&&|\|\||->|\.)", head_for_call_check):
            return {}

        declarators = TestBackendGenerator._split_top_level_commas(stmt)
        if not declarators:
            return {}
        first_decl = declarators[0].split("=", 1)[0].strip()
        first_match = re.search(r"(?P<prefix>.*?)(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)?$", first_decl)
        if not first_match:
            return {}
        type_part = first_match.group("prefix").strip()
        if not type_part:
            return {}
        # A declaration needs a plausible type, not just an assignment target.
        type_tokens = [token for token in re.split(r"\s+", type_part.replace("*", " ").strip()) if token]
        if not type_tokens or type_tokens[0] in forbidden_starts:
            return {}
        if len(type_tokens) == 1 and type_tokens[0] in {"true", "false", "NULL", "nullptr"}:
            return {}
        base_type = TestBackendGenerator._clean_fixture_type(type_part.replace("*", " "), defaults="uint8", mutable=True)
        if not base_type:
            return {}

        result: dict[str, str] = {}
        first_name = first_match.group("name")
        for index, raw_item in enumerate(declarators):
            item = raw_item.split("=", 1)[0].strip()
            name_match = re.search(r"(?P<prefix>.*?)(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)?$", item)
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
            if ctype and ctype.split()[0] not in forbidden_starts:
                result.setdefault(name, ctype)
        return result

    @staticmethod
    def _local_variable_type_map(fn: FunctionInfo | None) -> dict[str, str]:
        if fn is None:
            return {}
        body = TestBackendGenerator._strip_comments_and_strings(fn.body)
        result: dict[str, str] = {}
        for statement in TestBackendGenerator._local_declaration_candidates(body):
            result.update(TestBackendGenerator._parse_local_declaration(statement))
        macro_decl = re.compile(
            r"\b(?P<macro>VAR|CONST|P2VAR|P2CONST|CONSTP2VAR)\s*\((?P<type>[^,()]+)(?:,[^()]*)?\)\s*"
            r"(?P<name>[A-Za-z_]\w*)\s*(?:=|;|\[)"
        )
        for match in macro_decl.finditer(body):
            name = match.group("name")
            if TestBackendGenerator._is_c_identifier(name):
                ctype = TestBackendGenerator._clean_fixture_type(match.group("type"), defaults="uint8", mutable=True)
                if match.group("macro") in {"P2VAR", "P2CONST", "CONSTP2VAR"}:
                    ctype = ctype.rstrip(" *") + " *"
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
        body = TestBackendGenerator._strip_comments_and_strings(fn.body)
        names: set[str] = set(TestBackendGenerator._local_variable_type_map(fn).keys())
        macro_decl = re.compile(
            r"\b(?:VAR|CONST|P2VAR|P2CONST|CONSTP2VAR|P2FUNC)\s*\([^;()]*\)\s*"
            r"(?P<name>[A-Za-z_]\w*)\s*(?:=|;|\[)"
        )
        for match in macro_decl.finditer(body):
            names.add(match.group("name"))
        return names
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
    def _filter_global_names(cls, fn: FunctionInfo | None, names: set[str], global_type_map: dict[str, str] | None = None) -> set[str]:
        locals_ = cls._function_internal_local_names(fn)
        params = {getattr(param, "name", "") for param in getattr(fn, "parameters", []) or []} if fn is not None else set()
        known_globals = set((global_type_map or {}).keys()) | set(cls._function_global_variable_type_map(fn).keys())
        result: set[str] = set()
        for name in names:
            if not name or not cls._is_c_identifier(name):
                continue
            if name in locals_ or name in params:
                continue
            if known_globals:
                if name in known_globals or cls._looks_like_global_name(name):
                    result.add(name)
            elif cls._looks_like_global_name(name):
                result.add(name)
        return result
    @classmethod
    def _filter_global_literals(cls, fn: FunctionInfo | None, values: dict[str, str], global_type_map: dict[str, str] | None = None) -> dict[str, str]:
        allowed = cls._filter_global_names(fn, set(values.keys()), global_type_map)
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

    def _cmake_include_dirs(self, source: Path, unit_sources: list[Path], extra_roots: list[Path] | None = None) -> str:
        roots: list[Path | str] = ["."]
        seen = {"."}
        for root in [*(extra_roots or []), *self._include_roots(source, unit_sources)]:
            key = str(root.resolve() if root.exists() else root)
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

    def _include_roots(self, source: Path, unit_sources: list[Path]) -> list[Path]:
        roots: list[Path] = []
        for candidate in [source.parent, *(path.parent for path in unit_sources), *self.include_dirs]:
            if candidate and candidate.exists() and candidate.is_dir():
                roots.append(candidate.resolve())
        return roots

    def _matching_include_root(self, path: Path, unit_sources: list[Path]) -> Path | None:
        candidates = [*self.include_dirs, *(source.parent for source in unit_sources)]
        for root in sorted((item for item in candidates if item.exists()), key=lambda item: len(str(item.resolve())), reverse=True):
            try:
                path.resolve().relative_to(root.resolve())
                return root
            except ValueError:
                continue
        return None

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

    def _resolve_include(self, include_name: str, current_dir: Path, unit_sources: list[Path]) -> Path | None:
        candidates = [current_dir / include_name]
        candidates.extend(root / include_name for root in self._include_roots(current_dir, unit_sources))
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def _guess_header(self, source: Path, unit_sources: list[Path]) -> str:
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

if [ -e "$build_dir" ]; then
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

test_status=0
ctest --test-dir "$build_dir" --output-on-failure || test_status=$?

exe_path=""
if [ -x "${build_dir}/${target}.exe" ]; then
  exe_path="${build_dir}/${target}.exe"
elif [ -x "${build_dir}/${target}" ]; then
  exe_path="${build_dir}/${target}"
fi

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
    def run(self, backend: str, out_dir: Path, source: Path) -> RunResult:
        if backend == "CppUTest":
            script = out_dir / f"{source.stem}_cpputest" / "run_cpputest.sh"
            return self._run_msys_script(script, backend)
        if backend == "GoogleTest":
            script = out_dir / f"{source.stem}_gtest" / "run_gtest.sh"
            return self._run_msys_script(script, backend)
        return RunResult(backend=backend, command="", returncode=None, skipped_reason="Unknown backend")

    def _run_msys_script(self, script: Path, backend: str) -> RunResult:
        bash = msys_bash_path()
        command_label = f"MSYS2 bash {script}"
        if not script.exists():
            return RunResult(backend=backend, command=command_label, returncode=None, skipped_reason="Runner script was not generated")
        if not bash:
            return RunResult(backend=backend, command=command_label, returncode=None, skipped_reason="MSYS2 bash was not found")
        msys_script = windows_to_msys_path(script)
        if bash.lower().endswith("bash.exe"):
            work_dir = windows_to_msys_path(script.parent)
            mingw_dir = windows_to_msys_path(local_mingw_bin())
            return self._run_command([bash, "-lc", f"export MSYSTEM=MINGW64; export HOME='{work_dir}/.home'; export TMPDIR='{work_dir}/.tmp'; export PATH='{mingw_dir}':/usr/bin:$PATH; bash '{msys_script}'"], backend, command_label)
        return self._run_command([bash, "-defterm", "-no-start", "-mingw64", "-c", f"bash '{msys_script}'"], backend, command_label)

    def _run_command(self, cmd: list[str], backend: str, command_label: str) -> RunResult:
        try:
            timeout = int(os.environ.get("TESTING_MATE_RUN_TIMEOUT", "900"))
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=toolchain_env())
            return RunResult(backend=backend, command=command_label, returncode=result.returncode, stdout=result.stdout[-12000:], stderr=result.stderr[-12000:])
        except Exception as exc:
            return RunResult(backend=backend, command=command_label, returncode=None, skipped_reason=str(exc))
    
    def extract_mcdc_metrics(self, out_dir: Path, source: Path, backend: str) -> dict[str, float | None]:
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
