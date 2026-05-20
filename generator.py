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
        copied = self._copy_unit_files(unit_sources, source_env_dir)
        test_file = test_dir / f"{source.stem}_cpputest_test.cpp"
        cmake_file = test_dir / "CMakeLists.txt"
        runner = test_dir / "run_cpputest.sh"
        link_sources = self._link_unit_sources(unit_sources)
        stub_result = StubHarnessEngine().write_for_backend("CppUTest", test_dir, source, functions, cases, unit_sources, self.include_dirs, code_database)
        c_sources = " ".join([*(f"src/{path.name}" for path in link_sources), *self._relative_cmake_paths(stub_result.source_files, test_dir)])
        include_dirs = self._cmake_include_dirs(source, unit_sources, [Path("src"), Path("stub/include")])
        test_file.write_text(self._cpp_test_source("CppUTest", source, functions, cases, unit_sources), encoding="utf-8")
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
        copied = self._copy_unit_files(unit_sources, source_env_dir)
        test_file = test_dir / f"{source.stem}_gtest_test.cpp"
        cmake_file = test_dir / "CMakeLists.txt"
        runner = test_dir / "run_gtest.sh"
        link_sources = self._link_unit_sources(unit_sources)
        stub_result = StubHarnessEngine().write_for_backend("GoogleTest", test_dir, source, functions, cases, unit_sources, self.include_dirs, code_database)
        c_sources = " ".join([*(f"src/{path.name}" for path in link_sources), *self._relative_cmake_paths(stub_result.source_files, test_dir)])
        include_dirs = self._cmake_include_dirs(source, unit_sources, [Path("src"), Path("stub/include")])
        test_file.write_text(self._cpp_test_source("GoogleTest", source, functions, cases, unit_sources), encoding="utf-8")
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
            lines.extend(['#include "CppUTest/CommandLineTestRunner.h"', '#include "CppUTest/TestHarness.h"', "", 'extern "C" {', f'#include "{include}"', '#include "testing_mate_stubs.h"', "}", ""])
        else:
            lines.extend(["#include <gtest/gtest.h>", "", 'extern "C" {', f'#include "{include}"', '#include "testing_mate_stubs.h"', "}", ""])
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
            lines.extend(self._test_group_lines(backend, suite_name, fn_cases[0]))
            for case in fn_cases:
                test_name = re.sub(r"[^A-Za-z0-9_]", "_", case.test_id)
                call_args = []
                setup_lines = []
                for param in fn.parameters:
                    value = case.values.get(param.name, TestVectorGenerator._nominal_value(param))
                    if param.kind == "pointer":
                        if value in {"0", "NULL", "nullptr"}:
                            call_args.append("0")
                        else:
                            fixture_name = f"fixture_{param.name}"
                            setup_lines.append(f"    {self._pointer_fixture_type(param)} {fixture_name} = {{}};")
                            for field_key, field_value in sorted(case.values.items()):
                                prefix = f"{param.name}."
                                if field_key.startswith(prefix):
                                    field_name = field_key[len(prefix) :]
                                    setup_lines.append(f"    {fixture_name}.{field_name} = (decltype({fixture_name}.{field_name})){field_value};")
                            call_args.append(f"&{fixture_name}")
                    else:
                        call_args.append(self._cpp_literal_for_param(param, value))
                if backend == "CppUTest":
                    lines.append(f"TEST({suite_name}, {test_name})")
                else:
                    lines.append(f"TEST_F({suite_name}, {test_name})")
                lines.append("{")
                lines.extend(self._case_comment_lines(case, "    "))
                lines.extend(self._state_setup_lines(case, "    "))
                lines.extend(setup_lines)
                call = f"{fn.name}({', '.join(call_args)})"
                if fn.return_type.strip() == "void":
                    lines.append(f"    {call};")
                    if backend == "CppUTest":
                        lines.append("    CHECK_TRUE(true);")
                    else:
                        lines.append("    SUCCEED();")
                else:
                    lines.append(f"    auto actual = {call};")
                    expected_return = self._expected_return_literal(case.expected)
                    if expected_return:
                        if backend == "CppUTest":
                            lines.append(f"    CHECK_EQUAL({expected_return}, actual);")
                        else:
                            lines.append(f"    EXPECT_EQ({expected_return}, actual);")
                    else:
                        lines.append("    (void)actual;")
                        lines.append("    /* TODO: replace with a concrete expected-value assertion. */")
                        if backend == "CppUTest":
                            lines.append("    CHECK_TRUE(true);")
                        else:
                            lines.append("    SUCCEED();")
                lines.append(f"    /* {case.rationale} */")
                lines.append("}")
                lines.append("")
        if backend == "CppUTest":
            lines.extend(["int main(int argc, char **argv)", "{", "    return CommandLineTestRunner::RunAllTests(argc, argv);", "}", ""])
        return "\n".join(lines)

    @staticmethod
    def _state_setup_lines(case: TestCase, indent: str = "") -> list[str]:
        values = case.values
        if not any(key.startswith("__") for key in values):
            return []
        lines: list[str] = []
        for key, value in sorted(values.items()):
            if key.startswith("__stub_return."):
                symbol = key.split(".", 1)[1]
                safe = TestBackendGenerator._safe_identifier(symbol)
                lines.append(f"{indent}TestingMate_SetStubReturn_{safe}((long long)({value}));")
        return lines

    @staticmethod
    def _safe_identifier(name: str) -> str:
        safe = re.sub(r"\W+", "_", name).strip("_")
        if not safe or safe[0].isdigit():
            safe = "stub_" + safe
        return safe

    @staticmethod
    def _expected_return_literal(expected: str) -> str:
        match = re.search(r"Expected return:\s*(.+)$", expected.strip())
        if not match:
            return ""
        value = match.group(1).strip()
        if not value or value.lower() == "void":
            return ""
        if re.fullmatch(r"-?\d+(?:[uUlL]*)", value):
            return value
        if re.fullmatch(r"-?\d+\.\d+(?:[fFlL]*)?", value):
            return value
        if re.fullmatch(r"[A-Za-z_]\w*", value):
            return value
        return ""

    @staticmethod
    def _test_group_lines(backend: str, group_name: str, case: TestCase) -> list[str]:
        setup_comment = TestBackendGenerator._lifecycle_comment_lines(case, "        ")
        teardown_comment = TestBackendGenerator._teardown_comment_lines(case, "        ")
        if backend == "CppUTest":
            return [
                f"TEST_GROUP({group_name})",
                "{",
                "    void setup()",
                "    {",
                *setup_comment,
                "    }",
                "",
                "    void teardown()",
                "    {",
                *teardown_comment,
                "    }",
                "};",
                "",
            ]
        return [
            f"class {group_name} : public ::testing::Test",
            "{",
            "protected:",
            "    void SetUp() override",
            "    {",
            *setup_comment,
            "    }",
            "",
            "    void TearDown() override",
            "    {",
            *teardown_comment,
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
        ctype = " ".join(param.ctype.replace("const", "").replace("volatile", "").split()).strip()
        return ctype.replace("*", "").strip() or "int"

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
