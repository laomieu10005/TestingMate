from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from testing_mate.code_database import CodeDatabase
from testing_mate.constants import C_SOURCE_EXTENSIONS
from testing_mate.mc_coverage import MCDCAnalyzer, ExecutedVector, MCDCReport
from testing_mate.models import FunctionInfo, Parameter, RunResult, TestCase, Condition
from testing_mate.stub_engine import StubHarnessEngine
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
        if backend == "CppUTest":
            return self._write_cpputest(source, functions, cases, unit_sources or [source], code_database)
        if backend == "GoogleTest":
            return self._write_gtest(source, functions, cases, unit_sources or [source], code_database)
        return []

    def _write_cpputest(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        unit_sources: list[Path],
        code_database: CodeDatabase | None = None,
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
        test_file.write_text(self._cpp_test_source("CppUTest", source, functions, cases, harness_sources), encoding="utf-8")
        cmake_file.write_text(
            "\n".join(
                [
                    "cmake_minimum_required(VERSION 3.16)",
                    f"project({source.stem}_cpputest C CXX)",
                    "set(CMAKE_CXX_STANDARD 11)",
                    "enable_testing()",
                    "include(CheckCCompilerFlag)",
                    "include(CheckCXXCompilerFlag)",
                    "if(CMAKE_C_COMPILER_ID MATCHES \"GNU\")",
                    "  add_compile_options(--coverage -O0 -g)",
                    "  add_link_options(--coverage)",
                    "  check_c_compiler_flag(\"-fcondition-coverage\" TESTING_MATE_C_HAS_CONDITION_COVERAGE)",
                    "  check_cxx_compiler_flag(\"-fcondition-coverage\" TESTING_MATE_CXX_HAS_CONDITION_COVERAGE)",
                    "  if(TESTING_MATE_C_HAS_CONDITION_COVERAGE)",
                    "    add_compile_options($<$<COMPILE_LANGUAGE:C>:-fcondition-coverage>)",
                    "  endif()",
                    "  if(TESTING_MATE_CXX_HAS_CONDITION_COVERAGE)",
                    "    add_compile_options($<$<COMPILE_LANGUAGE:CXX>:-fcondition-coverage>)",
                    "  endif()",
                    "elseif(CMAKE_C_COMPILER_ID MATCHES \"Clang\")",
                    "  add_compile_options(-O0 -g -fprofile-instr-generate -fcoverage-mapping -fcoverage-mcdc)",
                    "  add_link_options(-fprofile-instr-generate -fcoverage-mapping -fcoverage-mcdc)",
                    "endif()",
                    "if(MINGW)",
                    "  add_compile_options($<$<COMPILE_LANGUAGE:CXX>:-Wa,-mbig-obj>)",
                    "endif()",
                    "find_package(CppUTest REQUIRED)",
                    f"add_executable({source.stem}_cpputest {test_file.name} {c_sources})",
                    f"target_include_directories({source.stem}_cpputest PRIVATE {include_dirs})",
                    f"target_compile_options({source.stem}_cpputest PRIVATE $<$<COMPILE_LANGUAGE:C>:-include> $<$<COMPILE_LANGUAGE:C>:testing_mate_stubs.h>)",
                    f"target_link_libraries({source.stem}_cpputest CppUTest CppUTestExt)",
                    f"add_test(NAME {source.stem}_cpputest COMMAND {source.stem}_cpputest)",
                ]
            ),
            encoding="utf-8",
        )
        runner.write_text(self._cmake_runner(source.stem + "_cpputest", f"src/{source.name}"), encoding="utf-8")
        return [*copied, *stub_result.generated_paths, test_file, cmake_file, runner]

    def _write_gtest(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        unit_sources: list[Path],
        code_database: CodeDatabase | None = None,
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
        test_file.write_text(self._cpp_test_source("GoogleTest", source, functions, cases, harness_sources), encoding="utf-8")
        cmake_file.write_text(
            "\n".join(
                [
                    "cmake_minimum_required(VERSION 3.16)",
                    f"project({source.stem}_gtest C CXX)",
                    "set(CMAKE_CXX_STANDARD 14)",
                    "enable_testing()",
                    "include(CheckCCompilerFlag)",
                    "include(CheckCXXCompilerFlag)",
                    "if(CMAKE_C_COMPILER_ID MATCHES \"GNU\")",
                    "  add_compile_options(--coverage -O0 -g)",
                    "  add_link_options(--coverage)",
                    "  check_c_compiler_flag(\"-fcondition-coverage\" TESTING_MATE_C_HAS_CONDITION_COVERAGE)",
                    "  check_cxx_compiler_flag(\"-fcondition-coverage\" TESTING_MATE_CXX_HAS_CONDITION_COVERAGE)",
                    "  if(TESTING_MATE_C_HAS_CONDITION_COVERAGE)",
                    "    add_compile_options($<$<COMPILE_LANGUAGE:C>:-fcondition-coverage>)",
                    "  endif()",
                    "  if(TESTING_MATE_CXX_HAS_CONDITION_COVERAGE)",
                    "    add_compile_options($<$<COMPILE_LANGUAGE:CXX>:-fcondition-coverage>)",
                    "  endif()",
                    "elseif(CMAKE_C_COMPILER_ID MATCHES \"Clang\")",
                    "  add_compile_options(-O0 -g -fprofile-instr-generate -fcoverage-mapping -fcoverage-mcdc)",
                    "  add_link_options(-fprofile-instr-generate -fcoverage-mapping -fcoverage-mcdc)",
                    "endif()",
                    "if(MINGW)",
                    "  add_compile_options($<$<COMPILE_LANGUAGE:CXX>:-Wa,-mbig-obj>)",
                    "endif()",
                    "find_package(GTest REQUIRED)",
                    f"add_executable({source.stem}_gtest {test_file.name} {c_sources})",
                    f"target_include_directories({source.stem}_gtest PRIVATE {include_dirs})",
                    f"target_compile_options({source.stem}_gtest PRIVATE $<$<COMPILE_LANGUAGE:C>:-include> $<$<COMPILE_LANGUAGE:C>:testing_mate_stubs.h>)",
                    f"target_link_libraries({source.stem}_gtest GTest::gtest GTest::gtest_main)",
                    f"add_test(NAME {source.stem}_gtest COMMAND {source.stem}_gtest)",
                ]
            ),
            encoding="utf-8",
        )
        runner.write_text(self._cmake_runner(source.stem + "_gtest", f"src/{source.name}"), encoding="utf-8")
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

    def _cpp_test_source(self, backend: str, source: Path, functions: list[FunctionInfo], cases: list[TestCase], unit_sources: list[Path]) -> str:
        include = self._guess_header(source, unit_sources)
        lines = []
        if backend == "CppUTest":
            lines.extend(['#include "CppUTest/CommandLineTestRunner.h"', '#include "CppUTest/TestHarness.h"', '#include <string.h>', "", 'extern "C" {', f'#include "{include}"', '#include "testing_mate_stubs.h"', "}", ""])
        else:
            lines.extend(["#include <gtest/gtest.h>", "#include <string.h>", "", 'extern "C" {', f'#include "{include}"', '#include "testing_mate_stubs.h"', "}", ""])
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
            global_writes = self._written_global_names(fn)
            suite_globals = self._suite_lifecycle_global_names(fn_cases, global_writes)

            fixture_declarations: list[str] = []
            declared_fixture_names: set[str] = set()

            def add_fixture_declaration(c_type: str, name: str) -> None:
                # Every variable assigned in setup/teardown/PRECONDITION/ACTION must be
                # declared once in the test group before any generated assignment uses it.
                safe_name = self._safe_identifier(name)
                if not self._is_c_identifier(safe_name) or safe_name in declared_fixture_names:
                    return
                fixture_declarations.append(f"{c_type} {safe_name};")
                declared_fixture_names.add(safe_name)

            setup_reset_lines: list[str] = ["TestingMate_ResetStubs();"]
            teardown_reset_lines: list[str] = ["TestingMate_ResetStubs();"]
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
            param_by_name = {param.name: param for param in fn.parameters}

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

            suite_required_outputs = self._suite_required_outputs(fn_cases, pointer_writes, pointer_fixtures)
            expected_output_fixtures: dict[str, str] = {}
            for output_name in sorted(suite_required_outputs):
                root = output_name.split(".", 1)[0]
                if root not in pointer_fixtures or root in expected_output_fixtures:
                    continue
                param = param_by_name.get(root)
                if param is None:
                    continue
                expected_name = f"expected_{self._safe_identifier(root)}"
                expected_type = self._pointer_fixture_type(param)
                expected_output_fixtures[root] = expected_name
                add_fixture_declaration(expected_type, expected_name)
                setup_reset_lines.append(f"{expected_name} = {{}};")
                teardown_reset_lines.append(f"{expected_name} = {{}};")

            for local_name, local_type in self._suite_local_fixture_declarations(fn_cases, fn).items():
                add_fixture_declaration(local_type, local_name)
                setup_reset_lines.append(f"{local_name} = {{}};")
                teardown_reset_lines.append(f"{local_name} = {{}};")

            for symbol in sorted(suite_globals):
                if self._is_c_identifier(symbol):
                    reset_value = self._suite_global_reset_value(fn_cases, symbol)
                    setup_reset_lines.append(f"{symbol} = {reset_value};")
                    teardown_reset_lines.append(f"{symbol} = {reset_value};")

            lines.extend(self._test_group_lines(backend, suite_name, fn_cases[0], fixture_declarations, setup_reset_lines, teardown_reset_lines))

            for case in fn_cases:
                test_name = re.sub(r"[^A-Za-z0-9_]", "_", case.test_id)
                if backend == "CppUTest":
                    lines.append(f"TEST({suite_name}, {test_name})")
                else:
                    lines.append(f"TEST_F({suite_name}, {test_name})")
                lines.append("{")
                lines.extend(self._case_comment_lines(case, "    "))

                required_outputs = self._case_required_outputs(case, pointer_writes, pointer_fixtures)
                precondition_lines = self._case_precondition_lines(case, fn, pointer_writes, pointer_fixtures, pointer_arg_names, "    ")
                precondition_lines.extend(
                    self._case_expected_precondition_lines(
                        case,
                        return_type,
                        expected_return_fixture_name,
                        required_outputs,
                        expected_output_fixtures,
                        param_by_name,
                        "    ",
                    )
                )
                lines.append("    /* PRECONDITION */")
                lines.extend(precondition_lines if precondition_lines else ["    /* No explicit precondition values were generated. */"])

                check_lines: list[str] = []
                missing_lines: list[str] = []

                lines.append("    /* ACTIONS */")
                if self._is_void_return_type(fn.return_type):
                    lines.append(f"    {call};")
                else:
                    lines.append(f"    {return_fixture_name} = {{}};")
                    lines.append(f"    {return_fixture_name} = {call};")
                    check_lines.extend(
                        self._assert_observable_equal_lines(
                            backend,
                            expected_return_fixture_name,
                            return_fixture_name,
                            return_type,
                        )
                    )

                for output_name in sorted(required_outputs):
                    actual_expr = self._output_actual_expression(output_name, pointer_fixtures)
                    expected_expr = self._output_expected_expression(output_name, expected_output_fixtures)
                    if not actual_expr or not expected_expr:
                        continue
                    root, _, suffix = self._normalize_observable_name(output_name).partition(".")
                    output_type = self._pointer_fixture_type(param_by_name[root]) if root in param_by_name else ""
                    if suffix:
                        check_lines.extend(self._assert_equal_lines(backend, expected_expr, actual_expr))
                    else:
                        check_lines.extend(self._assert_observable_equal_lines(backend, expected_expr, actual_expr, output_type))

                expected_globals = self._expected_global_literals(case)
                required_globals = set(global_writes) | set(expected_globals)
                for global_name in sorted(required_globals):
                    expected_value = expected_globals.get(global_name, "")
                    if expected_value:
                        check_lines.extend(self._assert_equal_lines(backend, expected_value, global_name))
                    else:
                        missing_lines.extend(self._missing_expected_lines(backend, f"Missing expected global/state value for {global_name} in {fn.name}."))

                lines.append("    /* EXPECTED BEHAVIORS */")
                if check_lines:
                    lines.extend(check_lines)
                if missing_lines:
                    lines.extend(missing_lines)
                if not check_lines and not missing_lines:
                    lines.extend(self._missing_expected_lines(backend, f"No concrete observable expected behavior was generated for {fn.name}. Add expected return/output/global checks."))
                lines.append(f"    /* {case.rationale} */")
                lines.append("}")
                lines.append("")
        if backend == "CppUTest":
            lines.extend(["int main(int argc, char **argv)", "{", "    return CommandLineTestRunner::RunAllTests(argc, argv);", "}", ""])
        return "\n".join(lines)

    @staticmethod
    def _state_setup_lines(case: TestCase, indent: str = "") -> list[str]:
        values = case.values or {}
        lines: list[str] = []
        for key, value in sorted(values.items()):
            if key.startswith("__stub_return."):
                symbol = key.split(".", 1)[1]
                safe = TestBackendGenerator._safe_identifier(symbol)
                lines.append(f"{indent}TestingMate_SetStubReturn_{safe}((long long)({value}));")
                continue
            for prefix in ("__global.", "__set_global.", "__input_global.", "__setup_global.", "setup.global."):
                if key.startswith(prefix):
                    symbol = key[len(prefix) :]
                    if TestBackendGenerator._is_c_identifier(symbol):
                        lines.append(f"{indent}{symbol} = {value};")
                    break
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
        locals_: dict[str, str] = {}
        source_local_types = TestBackendGenerator._local_variable_type_map(fn) if fn is not None else {}
        for case in cases:
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
        indent: str = "",
    ) -> list[str]:
        lines: list[str] = []
        if expected_return_name:
            expected_return = self._expected_return_literal(case)
            if not expected_return:
                expected_return = self._default_expected_literal(return_type)
            lines.append(f"{indent}{expected_return_name} = {self._typed_value(return_type, expected_return)};")

        expected_outputs = self._expected_output_literals(case)
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
                lines.append(f"{indent}{expected_fixture}.{suffix} = {expected_value};")
            else:
                lines.append(f"{indent}{expected_fixture} = {self._typed_value(output_type, expected_value)};")
        return lines

    def _case_precondition_lines(
        self,
        case: TestCase,
        fn: FunctionInfo,
        pointer_writes: dict[str, set[str]],
        pointer_fixtures: dict[str, str],
        pointer_arg_names: dict[str, str],
        indent: str = "",
    ) -> list[str]:
        values = case.values or {}
        lines: list[str] = []
        lines.extend(self._state_setup_lines(case, indent))

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
                    pointee_initial = self._explicit_pointer_initial_value(case, param.name)
                    if pointee_initial is None and param.name not in pointer_writes and value not in {"1", "true", "TRUE", "non_null", "NON_NULL"}:
                        pointee_initial = value
                    if pointee_initial is not None:
                        lines.append(f"{indent}{fixture_name} = {self._typed_value(fixture_type, pointee_initial)};")
                    for field_key, field_value in sorted(values.items()):
                        prefix = f"{param.name}."
                        if field_key.startswith(prefix):
                            field_name = field_key[len(prefix) :]
                            if self._is_expected_key(field_name):
                                continue
                            lines.append(f"{indent}{fixture_name}.{field_name} = {field_value};")
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
            values = case.values or {}
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
    def _explicit_pointer_initial_value(case: TestCase, param_name: str) -> str | None:
        values = case.values or {}
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
        ctype = TestBackendGenerator._clean_fixture_type(param.ctype or "int", default="int", mutable=True)
        if param.kind == "pointer":
            ctype = ctype.replace("*", "").strip()
        return ctype or "int"

    @staticmethod
    def _return_fixture_type(fn: FunctionInfo) -> str:
        return TestBackendGenerator._clean_fixture_type(fn.return_type or "int", default="int", mutable=True)

    @staticmethod
    def _pointer_argument_type(param: Parameter, fixture_type: str) -> str:
        ctype = TestBackendGenerator._clean_source_type(param.ctype or "", default="")
        if "*" in ctype:
            return ctype
        return f"{fixture_type} *"

    @staticmethod
    def _clean_source_type(ctype: str, default: str = "int") -> str:
        clean = " ".join((ctype or default).replace("\n", " ").split()).strip()
        clean = re.sub(r"\b(?:extern|static|inline|__inline|__inline__|register)\b", " ", clean)
        clean = re.sub(r"__attribute__\s*\(\(.*?\)\)", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean or default

    @staticmethod
    def _clean_fixture_type(ctype: str, default: str = "int", mutable: bool = True) -> str:
        clean = TestBackendGenerator._clean_source_type(ctype, default=default)
        if mutable:
            # Fixture variables are assigned in PRECONDITION/SetUp, so remove top-level qualifiers.
            # The underlying datatype still comes from the analyzed source/header.
            clean = re.sub(r"\b(?:const|volatile)\b", " ", clean)
            clean = re.sub(r"\s+", " ", clean).strip()
        return clean or default

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
        clean = " ".join((return_type or "void").replace("static", "").split()).strip()
        return clean == "void"

    @staticmethod
    def _assert_equal_lines(backend: str, expected: str, actual: str, indent: str = "    ") -> list[str]:
        if backend == "CppUTest":
            return [f"{indent}CHECK_EQUAL({expected}, {actual});"]
        return [f"{indent}EXPECT_EQ({expected}, {actual});"]

    @staticmethod
    def _assert_observable_equal_lines(backend: str, expected: str, actual: str, ctype: str = "", indent: str = "    ") -> list[str]:
        # Use value equality for scalar-like C types and byte equality for aggregate outputs.
        # Aggregate byte compare keeps generated tests buildable for C structs without operator==.
        if TestBackendGenerator._is_probably_aggregate_type(ctype):
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
        scalar_tokens = {
            "void", "char", "signed char", "unsigned char",
            "short", "short int", "signed short", "signed short int", "unsigned short", "unsigned short int",
            "int", "signed", "signed int", "unsigned", "unsigned int",
            "long", "long int", "signed long", "signed long int", "unsigned long", "unsigned long int",
            "long long", "long long int", "signed long long", "signed long long int",
            "unsigned long long", "unsigned long long int",
            "float", "double", "long double", "bool", "_bool",
            "uint8_t", "uint16_t", "uint32_t", "uint64_t",
            "int8_t", "int16_t", "int32_t", "int64_t",
            "size_t", "ssize_t", "Std_ReturnType", "boolean",
        }
        if clean in scalar_tokens or lowered in scalar_tokens:
            return False
        # Unknown typedef names may be scalar, but AUTOSAR/C output parameters are often struct typedefs.
        # Be conservative for whole-output comparison; field-level checks still use value equality.
        return bool(re.fullmatch(r"[A-Za-z_]\w*", clean))

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
        result: dict[str, str] = {}
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
        result: dict[str, str] = {}
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
        locals_ = cls._local_variable_names(fn)
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
    def _local_variable_type_map(fn: FunctionInfo | None) -> dict[str, str]:
        if fn is None:
            return {}
        body = TestBackendGenerator._strip_comments_and_strings(fn.body)
        result: dict[str, str] = {}
        declaration = re.compile(
            r"\b(?P<type>(?:const\s+|volatile\s+|static\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|"
            r"char\s+|int\s+|float\s+|double\s+|bool\s+|uint\d+_t\s+|int\d+_t\s+|size_t\s+|[A-Za-z_]\w+_t\s+|[A-Za-z_]\w+\s+)+[*\s]*)"
            r"(?P<decls>[^;]+);"
        )
        for match in declaration.finditer(body):
            raw_type = TestBackendGenerator._clean_fixture_type(match.group("type"), default="int", mutable=True)
            for item in TestBackendGenerator._split_top_level_commas(match.group("decls")):
                item = item.strip()
                if not item:
                    continue
                item = item.split("=", 1)[0].strip()
                name_match = re.search(r"(?:\*\s*)?([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)?$", item)
                if not name_match:
                    continue
                name = name_match.group(1)
                if not TestBackendGenerator._is_c_identifier(name):
                    continue
                pointer_prefix = "*" if "*" in item[: name_match.start(1)] else ""
                result.setdefault(name, (raw_type + (" *" if pointer_prefix else "")).strip())
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
        names: set[str] = set()
        declaration = re.compile(
            r"\b(?:const\s+|volatile\s+|static\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|"
            r"char\s+|int\s+|float\s+|double\s+|bool\s+|uint\d+_t\s+|int\d+_t\s+|size_t\s+|[A-Za-z_]\w+_t\s+)+"
            r"[*\s]*([A-Za-z_]\w*)\s*(?:=|;|,|\[)"
        )
        for match in declaration.finditer(body):
            names.add(match.group(1))
        return names

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
            lines.append(f"{indent} * {label}: {value}")
        lines.append(f"{indent} */")
        return lines

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
            ("Expected Output", case.expected),
            ("Pass/Fail Criteria", case.pass_fail_criteria),
            ("Requirement", case.requirement_ref),
            ("Safety Goal", case.safety_goal_ref),
            ("Safety Integrity", case.asil),
            ("Process Target", case.aspice_level),
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
        ctype = TestBackendGenerator._clean_fixture_type(param.ctype or "int", default="int", mutable=True)
        if "*" in ctype:
            ctype = ctype.replace("*", " ")
        return " ".join(ctype.split()).strip() or "int"

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
    def _cmake_runner(target: str, target_source: str | None = None) -> str:
        target_source = (target_source or "").replace('"', '\\"')
        local_mingw = windows_to_msys_path(local_mingw_bin()).replace('"', '\\"')
        return "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
                'cd "$SCRIPT_DIR"',
                f'tool_mingw_bin="{local_mingw}"',
                'if [ -d "$tool_mingw_bin" ]; then',
                '  export PATH="$tool_mingw_bin:/mingw64/bin:/ucrt64/bin:/clang64/bin:/usr/bin:$PATH"',
                '  echo "Using tool MinGW: $tool_mingw_bin"',
                'else',
                '  export PATH="/mingw64/bin:/ucrt64/bin:/clang64/bin:/usr/bin:$PATH"',
                '  echo "Tool MinGW not found at $tool_mingw_bin; using system MSYS2 PATH"',
                'fi',
                'mkdir -p "$SCRIPT_DIR/.tmp" "$SCRIPT_DIR/.home"',
                'export TMPDIR="$SCRIPT_DIR/.tmp"',
                'export TMP="$SCRIPT_DIR/.tmp"',
                'export TEMP="$SCRIPT_DIR/.tmp"',
                'export HOME="$SCRIPT_DIR/.home"',
                'install_msys2_packages() {',
                '  if ! command -v pacman >/dev/null 2>&1; then',
                '    echo "MSYS2 pacman not found; cannot install missing package(s): $*"',
                '    exit 127',
                '  fi',
                '  echo "Installing missing MSYS2 package(s): $*"',
                '  pacman -Sy --needed --noconfirm "$@"',
                '}',
                'ensure_tool() {',
                '  tool_name="$1"',
                '  shift',
                '  if command -v "$tool_name" >/dev/null 2>&1; then',
                '    return 0',
                '  fi',
                '  for package_name in "$@"; do',
                '    if install_msys2_packages "$package_name"; then',
                '      if command -v "$tool_name" >/dev/null 2>&1; then',
                '        return 0',
                '      fi',
                '    fi',
                '  done',
                '  if [ "$#" -eq 0 ]; then',
                '    echo "No package mapping is available for missing tool: ${tool_name}"',
                '  else',
                '    echo "Required tool still not found after install attempt: ${tool_name}"',
                '  fi',
                '  exit 127',
                '}',
                'coverage_backend="${TESTING_MATE_COVERAGE_BACKEND:-gcc_lcov}"',
                'case "$coverage_backend" in',
                '  clang|clang_llvmcov|llvmcov)',
                '    export CC="${TESTING_MATE_CC:-clang}"',
                '    export CXX="${TESTING_MATE_CXX:-clang++}"',
                '    coverage_backend="clang_llvmcov"',
                '    ;;',
                '  *)',
                '    export CC="${TESTING_MATE_CC:-gcc}"',
                '    export CXX="${TESTING_MATE_CXX:-g++}"',
                '    coverage_backend="gcc_lcov"',
                '    ;;',
                'esac',
                'echo "shell: ${MSYSTEM:-unknown}"',
                'echo "coverage backend: ${coverage_backend}"',
                'echo "cc: ${CC} -> $(command -v "$CC" || true)"',
                'echo "cxx: ${CXX} -> $(command -v "$CXX" || true)"',
                'echo "cmake: $(command -v cmake || true)"',
                'echo "ninja: $(command -v ninja || true)"',
                'if [ "$coverage_backend" = "clang_llvmcov" ]; then',
                '  compiler_package="mingw-w64-x86_64-clang"',
                'else',
                '  compiler_package="mingw-w64-x86_64-gcc"',
                'fi',
                'ensure_tool cmake mingw-w64-x86_64-cmake',
                'ensure_tool ninja mingw-w64-x86_64-ninja',
                'ensure_tool "$CC" "$compiler_package"',
                'ensure_tool "$CXX" "$compiler_package"',
                'echo "resolved cc: ${CC} -> $(command -v "$CC")"',
                'echo "resolved cxx: ${CXX} -> $(command -v "$CXX")"',
                'echo "resolved cmake: $(command -v cmake)"',
                'echo "resolved ninja: $(command -v ninja)"',
                f'report_base="{target}"',
                'report_base="${report_base%_cpputest}"',
                'report_base="${report_base%_gtest}"',
                f'target_source="{target_source}"',
                'echo "coverage target source: ${target_source}"',
                f'target="{target}"',
                'build_dir="build"',
                'if [ -e "$build_dir" ]; then',
                '  if ! rm -rf "$build_dir"; then',
                '    build_dir="build_${target}_$(date +%Y%m%d%H%M%S)_$$"',
                '    echo "Existing build directory is locked; using fresh build directory: ${build_dir}"',
                '  fi',
                'fi',
                'mkdir -p "$build_dir"',
                'export LLVM_PROFILE_FILE="$SCRIPT_DIR/${build_dir}/${target}-%p.profraw"',
                'cmake -S . -B "$build_dir" -G Ninja -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" -DCMAKE_MAKE_PROGRAM="$(command -v ninja)"',
                'cmake --build "$build_dir"',
                'ctest --test-dir "$build_dir" --output-on-failure',
                'exe_path=""',
                'if [ -x "${build_dir}/${target}.exe" ]; then exe_path="${build_dir}/${target}.exe"; fi',
                'if [ -z "$exe_path" ] && [ -x "${build_dir}/${target}" ]; then exe_path="${build_dir}/${target}"; fi',
                'if [ -n "$exe_path" ]; then "$exe_path"; fi',
                'if [ "$coverage_backend" = "clang_llvmcov" ]; then',
                '  if [ -z "$exe_path" ]; then echo "Cannot find test executable for llvm-cov"; exit 1; fi',
                '  ensure_tool llvm-profdata mingw-w64-x86_64-llvm',
                '  ensure_tool llvm-cov mingw-w64-x86_64-llvm',
                '  shopt -s nullglob',
                '  profraw_files=("${build_dir}"/*.profraw)',
                '  if [ "${#profraw_files[@]}" -eq 0 ]; then',
                '    echo "No .profraw files were produced. Check that -fprofile-instr-generate was applied."',
                '    exit 1',
                '  fi',
                '  llvm-profdata merge --sparse "${profraw_files[@]}" -o coverage.profdata',
                '  llvm_source_args=()',
                '  if [ -n "$target_source" ] && [ -f "$target_source" ]; then',
                '    llvm_source_args+=("$target_source")',
                '  fi',
                '  llvm-cov report "$exe_path" -instr-profile=coverage.profdata -show-branch-summary -show-mcdc-summary -show-mcdc-non-executed-vectors "${llvm_source_args[@]}" > coverage.summary.txt',
                '  cat coverage.summary.txt',
                '  llvm-cov show "$exe_path" -instr-profile=coverage.profdata -show-branches=count -show-mcdc -format=html -output-dir="${report_base}_llvmcov" "${llvm_source_args[@]}"',
                '  llvm-cov export -format=text -instr-profile=coverage.profdata "$exe_path" "${llvm_source_args[@]}" > coverage.llvm.json',
                '  if command -v llvm2lcov >/dev/null 2>&1; then',
                '    ensure_tool genhtml mingw-w64-x86_64-lcov lcov',
                '    ensure_tool perl perl',
                '    llvm2lcov --branch --mcdc -o coverage.raw.info coverage.llvm.json',
                '    if [ -n "$target_source" ]; then',
                '      export TESTMATE_TARGET_SOURCE="$target_source"',
                "      perl -0777 -ne 'BEGIN { $target = lc($ENV{\"TESTMATE_TARGET_SOURCE\"}); $target =~ s!\\\\!/!g; $target =~ s!^\\./!!; } for $rec (split(/end_of_record\\n?/, $_)) { next unless $rec =~ /\\S/; if ($rec =~ /^SF:(.+)$/m) { $sf = lc($1); $sf =~ s!\\\\!/!g; $sf =~ s!^\\./!!; if ($sf eq $target || $sf =~ /\\Q$target\\E$/) { print $rec; print \"end_of_record\\n\"; } } }' coverage.raw.info > coverage.info.tmp",
                '      if grep -q "^SF:" coverage.info.tmp; then',
                '        mv coverage.info.tmp coverage.info',
                '        echo "Filtered LCOV to target source basename: ${target_source}"',
                '      else',
                '        echo "No LCOV records found for target source ${target_source}; available source records were:"',
                '        sed -n "s#^SF:##p" coverage.raw.info | head -80',
                '        rm -f coverage.info coverage.info.tmp',
                '      fi',
                '    else',
                '      cp coverage.raw.info coverage.info',
                '    fi',
                '    if [ -f coverage.info ] && command -v genhtml >/dev/null 2>&1; then',
                '      genhtml coverage.info --branch-coverage --mcdc --title "${report_base} target-only" --output-directory "${report_base}_lcov"',
                '    fi',
                '  else',
                '    echo "llvm2lcov not found; generated coverage.summary.txt, coverage.llvm.json and ${report_base}_llvmcov/index.html instead."',
                '  fi',
                'else',
                '  ensure_tool lcov mingw-w64-x86_64-lcov lcov',
                '  ensure_tool genhtml mingw-w64-x86_64-lcov lcov',
                '  ensure_tool perl perl',
                '  if command -v lcov >/dev/null 2>&1 && command -v genhtml >/dev/null 2>&1; then',
                '    rm -f coverage.raw.info coverage.info',
                '    if lcov --help 2>&1 | grep -q -- "--mcdc"; then',
                '      lcov --capture --directory "$build_dir" --base-directory . --branch-coverage --mcdc --output-file coverage.raw.info',
                '      genhtml_mcdc_opt="--mcdc"',
                '    else',
                '      lcov --capture --directory "$build_dir" --base-directory . --rc lcov_branch_coverage=1 --rc branch_coverage=1 --output-file coverage.raw.info',
                '      genhtml_mcdc_opt=""',
                '    fi',
                '    if [ -n "$target_source" ]; then',
                '      export TESTMATE_TARGET_SOURCE="$target_source"',
                "      perl -0777 -ne 'BEGIN { $target = lc($ENV{\"TESTMATE_TARGET_SOURCE\"}); $target =~ s!\\\\!/!g; $target =~ s!^\\./!!; } for $rec (split(/end_of_record\\n?/, $_)) { next unless $rec =~ /\\S/; if ($rec =~ /^SF:(.+)$/m) { $sf = lc($1); $sf =~ s!\\\\!/!g; $sf =~ s!^\\./!!; if ($sf eq $target || $sf =~ /\\Q$target\\E$/) { print $rec; print \"end_of_record\\n\"; } } }' coverage.raw.info > coverage.info.tmp",
                '      if grep -q "^SF:" coverage.info.tmp; then',
                '        mv coverage.info.tmp coverage.info',
                '        echo "Filtered LCOV to target source basename: ${target_source}"',
                '      else',
                '        echo "No LCOV records found for target source ${target_source}; available source records were:"',
                '        sed -n "s#^SF:##p" coverage.raw.info | head -80',
                '        rm -f coverage.info coverage.info.tmp',
                '      fi',
                '    else',
                '      cp coverage.raw.info coverage.info',
                '    fi',
                '    if [ -f coverage.info ]; then',
                '      genhtml coverage.info --branch-coverage ${genhtml_mcdc_opt} --title "${report_base} target-only" --output-directory "${report_base}_lcov"',
                '      echo "Generated target-only coverage.info and ${report_base}_lcov/index.html with lcov/genhtml"',
                '    fi',
                '  else',
                "    echo 'lcov/genhtml not found; install lcov before using backend coverage. Testing Mate will keep generated-vector evidence only.'",
                '  fi',
                'fi',
                "",
            ]
        )




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
            return self._run_command([bash, "-lc", f"export MSYSTEM=MINGW64; export HOME='{work_dir}/.home'; export TMPDIR='{work_dir}/.tmp'; export PATH='{mingw_dir}':/mingw64/bin:/usr/bin:$PATH; bash '{msys_script}'"], backend, command_label)
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
            
            summary_file = test_dir / "coverage.summary.txt"
            if summary_file.exists():
                return collector.extract_mcdc_from_summary(summary_file)
        
        return {}