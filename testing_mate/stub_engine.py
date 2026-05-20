# from __future__ import annotations

# from dataclasses import asdict, dataclass, field
# from pathlib import Path
# import json
# import re
# import shutil

# from testing_mate.constants import C_KEYWORDS, C_SOURCE_EXTENSIONS, HEADER_SOURCE_EXTENSIONS
# from testing_mate.code_database import CodeDatabase
# from testing_mate.models import FunctionInfo, TestCase


# @dataclass
# class StubSymbol:
#     name: str
#     return_type: str = "int"
#     params: str = ""
#     reason: str = "external call"
#     source: str = "analysis"
#     from_prototype: bool = False
#     call_count_name: str = ""
#     return_control_name: str = ""


# @dataclass
# class StubHarnessResult:
#     backend: str
#     stub_symbols: list[StubSymbol] = field(default_factory=list)
#     generated_paths: list[Path] = field(default_factory=list)
#     source_files: list[Path] = field(default_factory=list)
#     header_files: list[Path] = field(default_factory=list)

#     @property
#     def symbols(self) -> list[str]:
#         return [symbol.name for symbol in self.stub_symbols]


# class StubHarnessEngine:
#     """Create backend-specific stub harness artifacts for external C dependencies.

#     The engine is intentionally conservative. It generates weak, reviewable stubs for
#     external function calls that are referenced by the unit under test but not defined
#     by the selected/auto-linked source files. The stubs unblock quick smoke builds;
#     expected behavior still belongs in reviewed project-specific mocks.
#     """

#     call_pattern = re.compile(r"(?<![.#>])\b([A-Za-z_]\w*)\s*\(")
#     function_definition_pattern = re.compile(
#         r"(?m)^\s*[A-Za-z_]\w*[\w\s\*]*?(?:\s|\*)([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{"
#     )
#     prototype_pattern = re.compile(
#         r"(?m)^\s*(?:extern\s+)?(?P<rtype>(?:const\s+|volatile\s+|signed\s+|unsigned\s+|long\s+|short\s+|struct\s+|enum\s+|union\s+|[A-Za-z_]\w*\s+|\*)+?)"
#         r"\s*(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^;{}]*)\)\s*;"
#     )
#     include_pattern = re.compile(r'^\s*#\s*include\s+[<"]([^>"]+)[>"]', re.MULTILINE)

#     ignored_calls = {
#         *C_KEYWORDS,
#         "main",
#         "return",
#         "sizeof",
#         "alignof",
#         "offsetof",
#         "static_assert",
#         "assert",
#         "printf",
#         "fprintf",
#         "sprintf",
#         "snprintf",
#         "puts",
#         "putchar",
#         "memcpy",
#         "memmove",
#         "memset",
#         "memcmp",
#         "strlen",
#         "strcmp",
#         "strncmp",
#         "strcpy",
#         "strncpy",
#         "malloc",
#         "calloc",
#         "realloc",
#         "free",
#         "abs",
#         "labs",
#         "fabs",
#         "fabsf",
#         "min",
#         "max",
#     }

#     def discover(
#         self,
#         source: Path,
#         functions: list[FunctionInfo],
#         cases: list[TestCase],
#         unit_sources: list[Path] | None = None,
#         include_dirs: list[Path | str] | None = None,
#         code_database: CodeDatabase | None = None,
#     ) -> list[StubSymbol]:
#         unit_sources = unit_sources or [source]
#         include_dirs = [Path(item).expanduser() for item in (include_dirs or []) if item]
#         defined = self._defined_function_names([*unit_sources, *self._analysis_sources_from_database(code_database)]) | {fn.name for fn in functions}
#         if code_database is not None:
#             defined.update(record.name for record in code_database.functions)
#         prototypes = self._prototype_map(source, unit_sources, include_dirs)
#         calls = self._referenced_calls(functions)
#         calls.update(self._case_harness_calls(cases))
#         controlled_returns = self._case_stub_return_calls(cases)
#         calls.update(controlled_returns)
#         symbols: list[StubSymbol] = []
#         for name in sorted(calls):
#             if (name in defined and name not in controlled_returns) or self._should_ignore_call(name):
#                 continue
#             rtype, params, from_prototype = prototypes.get(name, ("int", "", False))
#             safe = self._safe_identifier(name)
#             symbol = StubSymbol(
#                 name=name,
#                 return_type=self._clean_return_type(rtype),
#                 params=self._definition_params(params),
#                 reason="external call referenced by target source",
#                 source=str(source),
#                 from_prototype=from_prototype,
#                 call_count_name=f"TestingMate_{safe}_CallCount",
#                 return_control_name=f"TestingMate_SetStubReturn_{safe}",
#             )
#             symbols.append(symbol)
#         return symbols

#     def write_for_backend(
#         self,
#         backend: str,
#         out_dir: Path,
#         source: Path,
#         functions: list[FunctionInfo],
#         cases: list[TestCase],
#         unit_sources: list[Path] | None = None,
#         include_dirs: list[Path | str] | None = None,
#         code_database: CodeDatabase | None = None,
#     ) -> StubHarnessResult:
#         out_dir.mkdir(parents=True, exist_ok=True)
#         include_dirs_expanded = [Path(item).expanduser() for item in (include_dirs or []) if item]
#         symbols = self.discover(source, functions, cases, unit_sources, include_dirs_expanded, code_database)
#         if backend == "CppUTest":
#             return self._write_cmake_backend("CppUTest", out_dir, source, symbols, include_dirs_expanded)
#         if backend == "GoogleTest":
#             return self._write_cmake_backend("GoogleTest", out_dir, source, symbols, include_dirs_expanded)
#         return StubHarnessResult(backend=backend, stub_symbols=symbols)

#     def _write_cmake_backend(self, backend: str, out_dir: Path, source: Path, symbols: list[StubSymbol], include_dirs: list[Path] | None = None) -> StubHarnessResult:
#         stub_src_dir = out_dir / "stub" / "src"
#         stub_include_dir = out_dir / "stub" / "include"
#         stub_src_dir.mkdir(parents=True, exist_ok=True)
#         stub_include_dir.mkdir(parents=True, exist_ok=True)
#         c_path = stub_src_dir / "testing_mate_stubs.c"
#         h_path = stub_include_dir / "testing_mate_stubs.h"
#         manifest = stub_src_dir / "testing_mate_stub_manifest.json"
#         readme = stub_src_dir / "README_Stubs.md"
#         c_path.write_text(self._stub_c_source(symbols), encoding="utf-8")
#         h_path.write_text(self._stub_header(symbols), encoding="utf-8")
#         manifest.write_text(self._manifest_json(backend, source, symbols), encoding="utf-8")
#         readme.write_text(self._stub_readme(backend, source, symbols), encoding="utf-8")
        
#         generated_paths = [c_path, h_path, manifest, readme]
#         copied_includes = self._recursively_find_and_copy_includes(source, stub_include_dir, include_dirs or [])
        
#         return StubHarnessResult(
#             backend=backend,
#             stub_symbols=symbols,
#             generated_paths=[*generated_paths, *copied_includes],
#             source_files=[c_path],
#             header_files=[h_path, *copied_includes],
#         )

#     def _referenced_calls(self, functions: list[FunctionInfo]) -> set[str]:
#         calls: set[str] = set()
#         for fn in functions:
#             body = self._strip_comments_and_strings(fn.body)
#             for match in self.call_pattern.finditer(body):
#                 calls.add(match.group(1))
#         return calls

#     def _case_harness_calls(self, cases: list[TestCase]) -> set[str]:
#         calls: set[str] = set()
#         for case in cases:
#             calls.update(self._case_stub_return_calls([case]))
#         return calls

#     @staticmethod
#     def _case_stub_return_calls(cases: list[TestCase]) -> set[str]:
#         calls: set[str] = set()
#         for case in cases:
#             for key in case.values:
#                 if key.startswith("__stub_return."):
#                     name = key.split(".", 1)[1]
#                     if name:
#                         calls.add(name)
#         return calls

#     def _defined_function_names(self, unit_sources: list[Path]) -> set[str]:
#         result: set[str] = set()
#         for source in unit_sources:
#             if source.suffix.lower() not in C_SOURCE_EXTENSIONS and source.suffix.lower() not in HEADER_SOURCE_EXTENSIONS:
#                 continue
#             try:
#                 text = source.read_text(encoding="utf-8", errors="replace")
#             except OSError:
#                 continue
#             text = self._strip_comments_and_strings(text)
#             result.update(match.group(1) for match in self.function_definition_pattern.finditer(text))
#         return result

#     @staticmethod
#     def _analysis_sources_from_database(code_database: CodeDatabase | None) -> list[Path]:
#         if code_database is None:
#             return []
#         paths: list[Path] = []
#         for raw in (code_database.source_path, code_database.analysis_source_path):
#             if not raw:
#                 continue
#             path = Path(raw).expanduser()
#             if path.exists() and path.is_file():
#                 paths.append(path)
#         return paths

#     def _prototype_map(
#         self,
#         source: Path,
#         unit_sources: list[Path],
#         include_dirs: list[Path],
#     ) -> dict[str, tuple[str, str, bool]]:
#         candidates: list[Path] = []
#         for path in [source, source.with_suffix(".h"), *unit_sources, *(src.with_suffix(".h") for src in unit_sources)]:
#             if path.exists() and path.is_file():
#                 candidates.append(path)
#         for include_dir in include_dirs:
#             if include_dir.exists() and include_dir.is_dir():
#                 for suffix in HEADER_SOURCE_EXTENSIONS:
#                     candidates.extend(include_dir.rglob(f"*{suffix}"))
#         result: dict[str, tuple[str, str, bool]] = {}
#         seen: set[Path] = set()
#         for path in candidates[:300]:
#             resolved = path.resolve()
#             if resolved in seen:
#                 continue
#             seen.add(resolved)
#             try:
#                 text = path.read_text(encoding="utf-8", errors="replace")
#             except OSError:
#                 continue
#             text = self._strip_comments_and_strings(text)
#             for match in self.prototype_pattern.finditer(text):
#                 name = match.group("name")
#                 rtype = match.group("rtype")
#                 params = match.group("params")
#                 if self._looks_like_statement_match(rtype, params):
#                     continue
#                 if name not in result and not self._should_ignore_call(name):
#                     result[name] = (rtype, params, True)
#         return result

#     @staticmethod
#     def _looks_like_statement_match(return_type: str, params: str) -> bool:
#         first = (return_type or "").strip().split()[:1]
#         if first and first[0] in {"return", "case", "if", "while", "for", "switch"}:
#             return True
#         for raw in StubHarnessEngine._split_params(params or ""):
#             item = raw.strip()
#             if item and re.match(r"[-+]?\d", item):
#                 return True
#         return False

#     def _stub_c_source(self, symbols: list[StubSymbol]) -> str:
#         lines = [
#             "/* Generated by Testing Mate stub harness engine. */",
#             "/* Review every stub before using the result as safety evidence. */",
#             "#include <stddef.h>",
#             "#include <stdint.h>",
#             "#include <stdbool.h>",
#             "",
#             "#if defined(__GNUC__) || defined(__clang__)",
#             "#define TESTING_MATE_WEAK __attribute__((weak))",
#             "#else",
#             "#define TESTING_MATE_WEAK",
#             "#endif",
#             "",
#         ]
#         lines.extend([
#             "void TestingMate_StubsPresent(void)",
#             "{",
#             "    /* Link anchor for generated stub harness. */",
#             "}",
#             "",
#         ])
#         if not symbols:
#             return "\n".join(lines)
#         for symbol in symbols:
#             safe = self._safe_identifier(symbol.name)
#             params = symbol.params or ""
#             lines.extend(
#                 [
#                     f"static unsigned g_TestingMate_{safe}_calls;",
#                     f"static long long g_TestingMate_{safe}_return_value;",
#                     f"unsigned {symbol.call_count_name}(void)",
#                     "{",
#                     f"    return g_TestingMate_{safe}_calls;",
#                     "}",
#                     f"void {symbol.return_control_name}(long long value)",
#                     "{",
#                     f"    g_TestingMate_{safe}_return_value = value;",
#                     "}",
#                     f"{symbol.return_type} TESTING_MATE_WEAK {symbol.name}({params})",
#                     "{",
#                     f"    ++g_TestingMate_{safe}_calls;",
#                 ]
#             )
#             lines.extend(self._unused_param_lines(params))
#             lines.extend(self._return_lines(symbol.return_type, safe))
#             lines.extend(["}", ""])
#         return "\n".join(lines)

#     def _stub_header(self, symbols: list[StubSymbol]) -> str:
#         lines = [
#             "#ifndef TESTING_MATE_STUBS_H",
#             "#define TESTING_MATE_STUBS_H",
#             "",
#             "#ifdef __cplusplus",
#             "extern \"C\" {",
#             "#endif",
#             "",
#             "void TestingMate_StubsPresent(void);",
#         ]
#         for symbol in symbols:
#             lines.append(f"unsigned {symbol.call_count_name}(void);")
#             lines.append(f"void {symbol.return_control_name}(long long value);")
#             if not symbol.from_prototype:
#                 # Fallback declaration for target C files that call an external symbol
#                 # without a visible prototype. Known project prototypes are not repeated
#                 # here to avoid conflicts with typed production headers.
#                 lines.append("#ifdef __cplusplus")
#                 lines.append(f"int {symbol.name}(...);")
#                 lines.append("#else")
#                 lines.append(f"int {symbol.name}();")
#                 lines.append("#endif")
#         lines.extend(
#             [
#                 "",
#                 "#ifdef __cplusplus",
#                 "}",
#                 "#endif",
#                 "",
#                 "#endif /* TESTING_MATE_STUBS_H */",
#                 "",
#             ]
#         )
#         return "\n".join(lines)

#     def _manifest_json(self, backend: str, source: Path, symbols: list[StubSymbol]) -> str:
#         payload = {
#             "version": "1.0",
#             "backend": backend,
#             "source": str(source),
#             "stub_count": len(symbols),
#             "symbols": [asdict(symbol) for symbol in symbols],
#         }
#         return json.dumps(payload, indent=2)

#     def _stub_readme(self, backend: str, source: Path, symbols: list[StubSymbol]) -> str:
#         lines = [
#             "# Testing Mate Stub Harness",
#             "",
#             f"- Backend: `{backend}`",
#             f"- Unit under test: `{source.name}`",
#             f"- Generated stub count: `{len(symbols)}`",
#             "",
#             "The generated stubs are weak build-unblockers. Replace them with reviewed mocks/fakes before claiming test evidence.",
#             "The main coverage report remains target-only; these stub files are not intended to contribute to unit coverage.",
#             "",
#             "## Symbols",
#             "",
#         ]
#         if not symbols:
#             lines.append("No external function calls were discovered.")
#         else:
#             lines.append("| Symbol | Return | Reason | Call-count accessor |")
#             lines.append("|---|---|---|---|")
#             for symbol in symbols:
#                 lines.append(f"| `{symbol.name}` | `{symbol.return_type}` | {symbol.reason} | `{symbol.call_count_name}()` |")
#         lines.append("")
#         return "\n".join(lines)

#     def _extract_includes(self, source: Path) -> set[str]:
#         """Extract all #include directives from source files."""
#         includes: set[str] = set()
#         if not source.exists():
#             return includes
#         try:
#             text = source.read_text(encoding="utf-8", errors="replace")
#         except OSError:
#             return includes
#         for match in self.include_pattern.finditer(text):
#             includes.add(match.group(1))
#         return includes

#     def _recursively_find_and_copy_includes(
#         self,
#         source: Path,
#         dest_include_dir: Path,
#         include_dirs: list[Path],
#     ) -> list[Path]:
#         """Recursively search include directories for missing includes and copy them.
        
#         Returns list of copied include file paths.
#         """
#         copied_files: list[Path] = []
#         if not include_dirs:
#             return copied_files
        
#         # Extract includes from the source file
#         includes = self._extract_includes(source)
#         if not includes:
#             return copied_files
        
#         # Track already copied files to avoid duplicates
#         copied_set: set[str] = set()
        
#         # For each include, search recursively in all include directories
#         for include_name in includes:
#             if include_name in copied_set:
#                 continue
            
#             # Try to find the include file in the include directories
#             found = False
#             for include_dir in include_dirs:
#                 if not include_dir.exists() or not include_dir.is_dir():
#                     continue
                
#                 # Search recursively for the include file
#                 for candidate in include_dir.rglob(include_name):
#                     if candidate.is_file():
#                         try:
#                             # Determine destination path preserving relative structure
#                             rel_path = candidate.relative_to(include_dir)
#                             dest_file = dest_include_dir / rel_path
#                             dest_file.parent.mkdir(parents=True, exist_ok=True)
#                             shutil.copy2(candidate, dest_file)
#                             copied_files.append(dest_file)
#                             copied_set.add(include_name)
#                             found = True
#                             break
#                         except (OSError, ValueError) as e:
#                             continue
                
#                 if found:
#                     break
        
#         return copied_files

#     @staticmethod
#     def _return_lines(return_type: str, safe: str = "") -> list[str]:
#         clean = " ".join(return_type.replace("TESTING_MATE_WEAK", "").split()).strip()
#         lowered = clean.lower().replace("const ", "").replace("volatile ", "")
#         value = f"g_TestingMate_{safe}_return_value" if safe else "0"
#         if lowered == "void":
#             return ["    return;"]
#         if "*" in clean:
#             return [f"    return ({clean})(intptr_t){value};"]
#         if lowered.startswith("struct ") or lowered.startswith("union "):
#             return [f"    return ({clean}){{0}};"]
#         if any(token in lowered for token in ("bool", "boolean")):
#             return [f"    return {value} != 0;"]
#         return [f"    return ({clean}){value};"]

#     @staticmethod
#     def _unused_param_lines(params: str) -> list[str]:
#         if not params.strip() or params.strip() == "void":
#             return []
#         result = []
#         for index, raw in enumerate(StubHarnessEngine._split_params(params)):
#             name = StubHarnessEngine._param_name(raw, index)
#             if name:
#                 result.append(f"    (void){name};")
#         return result

#     @staticmethod
#     def _definition_params(params: str) -> str:
#         params = " ".join((params or "").replace("\n", " ").split()).strip()
#         if not params or params == "void":
#             return ""
#         if "..." in params:
#             return ""
#         converted: list[str] = []
#         for index, raw in enumerate(StubHarnessEngine._split_params(params)):
#             raw = raw.strip()
#             if not raw:
#                 continue
#             if StubHarnessEngine._param_name(raw, index):
#                 converted.append(raw)
#             else:
#                 converted.append(f"{raw} p{index}")
#         return ", ".join(converted)

#     @staticmethod
#     def _param_name(param: str, index: int) -> str:
#         param = param.strip()
#         if not param or param == "void":
#             return ""
#         array_match = re.search(r"\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])\s*$", param)
#         if array_match:
#             return array_match.group(1)
#         match = re.search(r"(?:\*|\s)([A-Za-z_]\w*)\s*$", param)
#         if not match:
#             return ""
#         name = match.group(1)
#         if name in {"char", "short", "int", "long", "float", "double", "void", "signed", "unsigned", "const", "volatile", "struct", "enum", "union"}:
#             return ""
#         return name

#     @staticmethod
#     def _split_params(params: str) -> list[str]:
#         parts: list[str] = []
#         depth = 0
#         current: list[str] = []
#         for ch in params:
#             if ch in "([{" :
#                 depth += 1
#             elif ch in ")]}" :
#                 depth = max(0, depth - 1)
#             if ch == "," and depth == 0:
#                 parts.append("".join(current).strip())
#                 current = []
#             else:
#                 current.append(ch)
#         tail = "".join(current).strip()
#         if tail:
#             parts.append(tail)
#         return parts

#     @staticmethod
#     def _clean_return_type(return_type: str) -> str:
#         clean = " ".join((return_type or "int").replace("\n", " ").split()).strip()
#         clean = re.sub(r"\b(static|extern|inline|__inline|__inline__)\b", "", clean)
#         clean = " ".join(clean.split()).strip()
#         return clean or "int"

#     def _should_ignore_call(self, name: str) -> bool:
#         if name in self.ignored_calls:
#             return True
#         if name.startswith("__"):
#             return True
#         # Most all-uppercase function-like tokens are macros/constants, not link symbols.
#         if name.isupper() and not name.startswith(("RTE_", "Rte_", "DEM_", "Dem_", "COM_", "Com_", "CAN_", "Can_")):
#             return True
#         return False

#     @staticmethod
#     def _safe_identifier(name: str) -> str:
#         safe = re.sub(r"\W+", "_", name).strip("_")
#         if not safe or safe[0].isdigit():
#             safe = "stub_" + safe
#         return safe

#     @staticmethod
#     def _strip_comments_and_strings(text: str) -> str:
#         text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
#         text = re.sub(r"//.*", " ", text)
#         text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
#         text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
#         return text


from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import re
import shutil

from testing_mate.constants import C_KEYWORDS, C_SOURCE_EXTENSIONS, HEADER_SOURCE_EXTENSIONS
from testing_mate.code_database import CodeDatabase
from testing_mate.models import FunctionInfo, TestCase


@dataclass
class HeaderPrototype:
    name: str
    return_type: str
    params: str
    declaration: str
    header_path: str = ""
    header_include: str = ""


@dataclass
class StubSymbol:
    name: str
    return_type: str = "int"
    params: str = "void"
    reason: str = "external call"
    source: str = "analysis"
    from_prototype: bool = False
    prototype: str = ""
    header_path: str = ""
    header_include: str = ""
    call_count_name: str = ""
    return_control_name: str = ""


@dataclass
class StubHarnessResult:
    backend: str
    stub_symbols: list[StubSymbol] = field(default_factory=list)
    generated_paths: list[Path] = field(default_factory=list)
    source_files: list[Path] = field(default_factory=list)
    header_files: list[Path] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        return [symbol.name for symbol in self.stub_symbols]


class StubHarnessEngine:
    """Create backend-specific stub harness artifacts for external C dependencies.

    Stub signatures are resolved from visible C header declarations first. Only when
    no declaration can be found does the engine fall back to an old-style ``int``
    declaration. This prevents broken AUTOSAR stubs such as ``int Dem_SetEventStatus()``
    when the header actually declares ``FUNC(void, DEM_CODE) Dem_SetEventStatus(...)``.
    """

    call_pattern = re.compile(r"(?<![.#>])\b([A-Za-z_]\w*)\s*\(")
    function_definition_pattern = re.compile(
        r"(?m)^\s*[A-Za-z_]\w*[\w\s\*]*?(?:\s|\*)([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{"
    )
    include_pattern = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]', re.MULTILINE)

    ignored_calls = {
        *C_KEYWORDS,
        "main",
        "return",
        "sizeof",
        "alignof",
        "offsetof",
        "static_assert",
        "assert",
        "printf",
        "fprintf",
        "sprintf",
        "snprintf",
        "puts",
        "putchar",
        "memcpy",
        "memmove",
        "memset",
        "memcmp",
        "strlen",
        "strcmp",
        "strncmp",
        "strcpy",
        "strncpy",
        "malloc",
        "calloc",
        "realloc",
        "free",
        "abs",
        "labs",
        "fabs",
        "fabsf",
        "min",
        "max",
    }

    def discover(
        self,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        unit_sources: list[Path] | None = None,
        include_dirs: list[Path | str] | None = None,
        code_database: CodeDatabase | None = None,
    ) -> list[StubSymbol]:
        unit_sources = unit_sources or [source]
        include_dirs = [Path(item).expanduser() for item in (include_dirs or []) if item]

        defined = self._defined_function_names(
            [*unit_sources, *self._analysis_sources_from_database(code_database)]
        ) | {fn.name for fn in functions}
        if code_database is not None:
            defined.update(record.name for record in code_database.functions)

        prototypes = self._prototype_map(source, unit_sources, include_dirs)
        calls = self._referenced_calls(functions)
        calls.update(self._case_harness_calls(cases))
        controlled_returns = self._case_stub_return_calls(cases)
        calls.update(controlled_returns)

        symbols: list[StubSymbol] = []
        for name in sorted(calls):
            if (name in defined and name not in controlled_returns) or self._should_ignore_call(name):
                continue

            proto = prototypes.get(name)
            if proto is not None:
                rtype = proto.return_type
                params = proto.params
                from_prototype = True
                reason = f"external call declared in header {Path(proto.header_path).name}"
                prototype_text = proto.declaration
                header_path = proto.header_path
                header_include = proto.header_include
                symbol_source = proto.header_path or str(source)
            else:
                rtype = "int"
                params = "void"
                from_prototype = False
                reason = "external call without visible prototype; fallback stub generated"
                prototype_text = ""
                header_path = ""
                header_include = ""
                symbol_source = str(source)

            safe = self._safe_identifier(name)
            symbol = StubSymbol(
                name=name,
                return_type=self._clean_return_type(rtype),
                params=self._definition_params(params),
                reason=reason,
                source=symbol_source,
                from_prototype=from_prototype,
                prototype=prototype_text,
                header_path=header_path,
                header_include=header_include,
                call_count_name=f"TestingMate_{safe}_CallCount",
                return_control_name=f"TestingMate_SetStubReturn_{safe}",
            )
            symbols.append(symbol)
        return symbols

    def write_for_backend(
        self,
        backend: str,
        out_dir: Path,
        source: Path,
        functions: list[FunctionInfo],
        cases: list[TestCase],
        unit_sources: list[Path] | None = None,
        include_dirs: list[Path | str] | None = None,
        code_database: CodeDatabase | None = None,
    ) -> StubHarnessResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        include_dirs_expanded = [Path(item).expanduser() for item in (include_dirs or []) if item]
        unit_sources_expanded = unit_sources or [source]
        symbols = self.discover(
            source,
            functions,
            cases,
            unit_sources_expanded,
            include_dirs_expanded,
            code_database,
        )
        if backend == "CppUTest":
            return self._write_cmake_backend(
                "CppUTest", out_dir, source, symbols, include_dirs_expanded, unit_sources_expanded
            )
        if backend == "GoogleTest":
            return self._write_cmake_backend(
                "GoogleTest", out_dir, source, symbols, include_dirs_expanded, unit_sources_expanded
            )
        return StubHarnessResult(backend=backend, stub_symbols=symbols)

    def _write_cmake_backend(
        self,
        backend: str,
        out_dir: Path,
        source: Path,
        symbols: list[StubSymbol],
        include_dirs: list[Path] | None = None,
        unit_sources: list[Path] | None = None,
    ) -> StubHarnessResult:
        stub_src_dir = out_dir / "stub" / "src"
        stub_include_dir = out_dir / "stub" / "include"
        stub_src_dir.mkdir(parents=True, exist_ok=True)
        stub_include_dir.mkdir(parents=True, exist_ok=True)

        c_path = stub_src_dir / "testing_mate_stubs.c"
        h_path = stub_include_dir / "testing_mate_stubs.h"
        manifest = stub_src_dir / "testing_mate_stub_manifest.json"
        readme = stub_src_dir / "README_Stubs.md"

        copied_includes = self._copy_required_headers(
            source,
            stub_include_dir,
            include_dirs or [],
            unit_sources or [source],
            symbols,
        )

        c_path.write_text(self._stub_c_source(symbols), encoding="utf-8")
        h_path.write_text(self._stub_header(symbols), encoding="utf-8")
        manifest.write_text(self._manifest_json(backend, source, symbols), encoding="utf-8")
        readme.write_text(self._stub_readme(backend, source, symbols), encoding="utf-8")

        generated_paths = [c_path, h_path, manifest, readme]
        return StubHarnessResult(
            backend=backend,
            stub_symbols=symbols,
            generated_paths=[*generated_paths, *copied_includes],
            source_files=[c_path],
            header_files=[h_path, *copied_includes],
        )

    def _referenced_calls(self, functions: list[FunctionInfo]) -> set[str]:
        calls: set[str] = set()
        for fn in functions:
            body = self._strip_comments_and_strings(fn.body)
            for match in self.call_pattern.finditer(body):
                calls.add(match.group(1))
        return calls

    def _case_harness_calls(self, cases: list[TestCase]) -> set[str]:
        calls: set[str] = set()
        for case in cases:
            calls.update(self._case_stub_return_calls([case]))
        return calls

    @staticmethod
    def _case_stub_return_calls(cases: list[TestCase]) -> set[str]:
        calls: set[str] = set()
        for case in cases:
            for key in case.values:
                if key.startswith("__stub_return."):
                    name = key.split(".", 1)[1]
                    if name:
                        calls.add(name)
        return calls

    def _defined_function_names(self, unit_sources: list[Path]) -> set[str]:
        result: set[str] = set()
        for source in unit_sources:
            if source.suffix.lower() not in C_SOURCE_EXTENSIONS and source.suffix.lower() not in HEADER_SOURCE_EXTENSIONS:
                continue
            try:
                text = source.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            text = self._strip_comments_and_strings(text)
            result.update(match.group(1) for match in self.function_definition_pattern.finditer(text))
        return result

    @staticmethod
    def _analysis_sources_from_database(code_database: CodeDatabase | None) -> list[Path]:
        if code_database is None:
            return []
        paths: list[Path] = []
        for raw in (code_database.source_path, code_database.analysis_source_path):
            if not raw:
                continue
            path = Path(raw).expanduser()
            if path.exists() and path.is_file():
                paths.append(path)
        return paths

    def _prototype_map(
        self,
        source: Path,
        unit_sources: list[Path],
        include_dirs: list[Path],
    ) -> dict[str, HeaderPrototype]:
        candidates = self._prototype_candidate_files(source, unit_sources, include_dirs)
        result: dict[str, HeaderPrototype] = {}
        seen: set[Path] = set()

        for path in candidates[:1000]:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.exists() or not resolved.is_file():
                continue
            seen.add(resolved)

            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for proto in self._parse_header_prototypes(text, path, include_dirs, source.parent):
                if proto.name not in result and not self._should_ignore_call(proto.name):
                    result[proto.name] = proto
        return result

    def _prototype_candidate_files(
        self,
        source: Path,
        unit_sources: list[Path],
        include_dirs: list[Path],
    ) -> list[Path]:
        candidates: list[Path] = []

        # 1. The unit source and its sibling header are highest priority.
        for path in [source, source.with_suffix(".h"), *unit_sources, *(src.with_suffix(".h") for src in unit_sources)]:
            if path.exists() and path.is_file():
                candidates.append(path)

        # 2. Headers actually reachable through #include chains from the unit.
        candidates.extend(self._collect_reachable_headers([source, *unit_sources], include_dirs))

        # 3. Fallback: all project headers in include dirs, for cases where the test
        # harness injects calls via __stub_return.X without the source including X.h.
        for include_dir in include_dirs:
            if include_dir.exists() and include_dir.is_dir():
                for suffix in HEADER_SOURCE_EXTENSIONS:
                    candidates.extend(include_dir.rglob(f"*{suffix}"))

        deduped: list[Path] = []
        seen: set[Path] = set()
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved not in seen:
                seen.add(resolved)
                deduped.append(path)
        return deduped

    def _parse_header_prototypes(
        self,
        text: str,
        path: Path,
        include_dirs: list[Path],
        source_parent: Path,
    ) -> list[HeaderPrototype]:
        sanitized = self._strip_comments_and_strings(text)
        sanitized = re.sub(r"^\s*#.*$", "", sanitized, flags=re.MULTILINE)

        prototypes: list[HeaderPrototype] = []
        for statement in self._iter_top_level_statements(sanitized):
            proto = self._parse_prototype_statement(statement)
            if proto is None:
                continue
            proto.header_path = str(path)
            proto.header_include = self._header_include_name(path, include_dirs, source_parent)
            prototypes.append(proto)
        return prototypes

    @staticmethod
    def _iter_top_level_statements(text: str) -> list[str]:
        statements: list[str] = []
        current: list[str] = []
        paren = 0
        brace = 0
        bracket = 0

        for ch in text:
            current.append(ch)
            if ch == "(":
                paren += 1
            elif ch == ")":
                paren = max(0, paren - 1)
            elif ch == "{":
                brace += 1
            elif ch == "}":
                brace = max(0, brace - 1)
            elif ch == "[":
                bracket += 1
            elif ch == "]":
                bracket = max(0, bracket - 1)

            if ch == ";" and paren == 0 and brace == 0 and bracket == 0:
                statements.append("".join(current))
                current = []

        return statements

    def _parse_prototype_statement(self, statement: str) -> HeaderPrototype | None:
        stmt = " ".join(statement.strip().rstrip(";").split())
        if not stmt or "(" not in stmt:
            return None
        if re.search(r"\btypedef\b", stmt):
            return None
        if "{" in stmt or "}" in stmt or "=" in stmt:
            return None

        found = self._find_function_param_open(stmt)
        if found is None:
            return None
        open_idx, close_idx, name_start, name = found
        return_type = stmt[:name_start].strip()
        params = stmt[open_idx + 1 : close_idx].strip()
        after = stmt[close_idx + 1 :].strip()

        if not return_type:
            return None
        if after and not after.startswith("__attribute__"):
            return None
        if "(*" in stmt[:open_idx]:
            return None

        return_type = self._clean_return_type(return_type)
        if self._looks_like_statement_match(return_type, params):
            return None

        return HeaderPrototype(
            name=name,
            return_type=return_type,
            params=params,
            declaration=stmt + ";",
        )

    @staticmethod
    def _find_function_param_open(stmt: str) -> tuple[int, int, int, str] | None:
        depth = 0
        for index, ch in enumerate(stmt):
            if ch == "(":
                if depth == 0:
                    ident = StubHarnessEngine._identifier_before(stmt, index)
                    if ident is not None:
                        name_start, name = ident
                        before_name = stmt[:name_start].strip()
                        if before_name and name not in {"FUNC", "P2VAR", "P2CONST", "CONSTP2VAR", "P2FUNC"}:
                            close_idx = StubHarnessEngine._matching_paren(stmt, index)
                            if close_idx is not None:
                                return index, close_idx, name_start, name
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
        return None

    @staticmethod
    def _identifier_before(text: str, index: int) -> tuple[int, str] | None:
        pos = index - 1
        while pos >= 0 and text[pos].isspace():
            pos -= 1
        end = pos + 1
        while pos >= 0 and (text[pos].isalnum() or text[pos] == "_"):
            pos -= 1
        start = pos + 1
        if start >= end:
            return None
        name = text[start:end]
        if not re.match(r"^[A-Za-z_]\w*$", name):
            return None
        return start, name

    @staticmethod
    def _matching_paren(text: str, open_index: int) -> int | None:
        depth = 0
        for index in range(open_index, len(text)):
            ch = text[index]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return index
        return None

    @staticmethod
    def _looks_like_statement_match(return_type: str, params: str) -> bool:
        first = (return_type or "").strip().split()[:1]
        if first and first[0] in {"return", "case", "if", "while", "for", "switch"}:
            return True
        for raw in StubHarnessEngine._split_params(params or ""):
            item = raw.strip()
            if item and re.match(r"[-+]?\d", item):
                return True
        return False

    def _stub_c_source(self, symbols: list[StubSymbol]) -> str:
        production_includes = []
        for symbol in symbols:
            if symbol.header_include and symbol.header_include not in production_includes:
                production_includes.append(symbol.header_include)

        lines = [
            "/* Generated by Testing Mate stub harness engine. */",
            "/* Review every stub before using the result as safety evidence. */",
            "#include <stddef.h>",
            "#include <stdint.h>",
            "#include <stdbool.h>",
            "#include \"testing_mate_stubs.h\"",
        ]
        for header in production_includes:
            lines.append(f"#include \"{header}\"")
        lines.extend(
            [
                "",
                "#if defined(__GNUC__) || defined(__clang__)",
                "#define TESTING_MATE_WEAK __attribute__((weak))",
                "#else",
                "#define TESTING_MATE_WEAK",
                "#endif",
                "",
                "void TestingMate_StubsPresent(void)",
                "{",
                "    /* Link anchor for generated stub harness. */",
                "}",
                "",
            ]
        )
        if not symbols:
            return "\n".join(lines)

        for symbol in symbols:
            safe = self._safe_identifier(symbol.name)
            params = symbol.params or "void"
            definition_prefix = self._definition_return_prefix(symbol.return_type)
            lines.extend(
                [
                    f"static unsigned g_TestingMate_{safe}_calls;",
                    f"static long long g_TestingMate_{safe}_return_value;",
                    f"unsigned {symbol.call_count_name}(void)",
                    "{",
                    f"    return g_TestingMate_{safe}_calls;",
                    "}",
                    f"void {symbol.return_control_name}(long long value)",
                    "{",
                    f"    g_TestingMate_{safe}_return_value = value;",
                    "}",
                    f"{definition_prefix} TESTING_MATE_WEAK {symbol.name}({params})",
                    "{",
                    f"    ++g_TestingMate_{safe}_calls;",
                ]
            )
            lines.extend(self._unused_param_lines(params))
            lines.extend(self._return_lines(symbol.return_type, safe))
            lines.extend(["}", ""])
        return "\n".join(lines)

    def _stub_header(self, symbols: list[StubSymbol]) -> str:
        lines = [
            "#ifndef TESTING_MATE_STUBS_H",
            "#define TESTING_MATE_STUBS_H",
            "",
            "#ifdef __cplusplus",
            "extern \"C\" {",
            "#endif",
            "",
            "void TestingMate_StubsPresent(void);",
        ]
        for symbol in symbols:
            lines.append(f"unsigned {symbol.call_count_name}(void);")
            lines.append(f"void {symbol.return_control_name}(long long value);")
            if not symbol.from_prototype:
                lines.append("#ifdef __cplusplus")
                lines.append(f"int {symbol.name}(...);")
                lines.append("#else")
                lines.append(f"int {symbol.name}();")
                lines.append("#endif")
        lines.extend(
            [
                "",
                "#ifdef __cplusplus",
                "}",
                "#endif",
                "",
                "#endif /* TESTING_MATE_STUBS_H */",
                "",
            ]
        )
        return "\n".join(lines)

    def _manifest_json(self, backend: str, source: Path, symbols: list[StubSymbol]) -> str:
        payload = {
            "version": "1.1",
            "backend": backend,
            "source": str(source),
            "stub_count": len(symbols),
            "symbols": [asdict(symbol) for symbol in symbols],
        }
        return json.dumps(payload, indent=2)

    def _stub_readme(self, backend: str, source: Path, symbols: list[StubSymbol]) -> str:
        lines = [
            "# Testing Mate Stub Harness",
            "",
            f"- Backend: `{backend}`",
            f"- Unit under test: `{source.name}`",
            f"- Generated stub count: `{len(symbols)}`",
            "",
            "The generated stubs are weak build-unblockers. Replace them with reviewed mocks/fakes before claiming test evidence.",
            "When a header declaration is available, the stub implementation uses that exact signature shape instead of guessing `int`.",
            "The main coverage report remains target-only; these stub files are not intended to contribute to unit coverage.",
            "",
            "## Symbols",
            "",
        ]
        if not symbols:
            lines.append("No external function calls were discovered.")
        else:
            lines.append("| Symbol | Return | Params | Header | Reason | Call-count accessor |")
            lines.append("|---|---|---|---|---|---|")
            for symbol in symbols:
                header = symbol.header_include or "fallback"
                lines.append(
                    f"| `{symbol.name}` | `{symbol.return_type}` | `{symbol.params}` | `{header}` | {symbol.reason} | `{symbol.call_count_name}()` |"
                )
        lines.append("")
        return "\n".join(lines)

    def _extract_includes(self, source: Path) -> set[str]:
        includes: set[str] = set()
        if not source.exists():
            return includes
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return includes
        for match in self.include_pattern.finditer(text):
            includes.add(match.group(2))
        return includes

    def _collect_reachable_headers(
        self,
        seed_files: list[Path],
        include_dirs: list[Path],
        max_headers: int = 1000,
    ) -> list[Path]:
        queue: list[Path] = []
        for seed in seed_files:
            if seed.exists() and seed.is_file():
                queue.append(seed)
                sibling_header = seed.with_suffix(".h")
                if sibling_header.exists() and sibling_header.is_file():
                    queue.append(sibling_header)

        unit_dirs = [seed.parent for seed in seed_files if seed.exists()]
        seen: set[Path] = set()
        headers: list[Path] = []

        while queue and len(headers) < max_headers:
            current = queue.pop(0)
            try:
                resolved_current = current.resolve()
            except OSError:
                continue
            if resolved_current in seen:
                continue
            seen.add(resolved_current)

            for include_name in self._extract_includes(current):
                header = self._resolve_include(include_name, current, include_dirs, unit_dirs)
                if header is None:
                    continue
                try:
                    resolved_header = header.resolve()
                except OSError:
                    continue
                if resolved_header not in seen and header not in headers:
                    headers.append(header)
                    queue.append(header)

        return headers

    def _resolve_include(
        self,
        include_name: str,
        from_file: Path,
        include_dirs: list[Path],
        unit_dirs: list[Path] | None = None,
    ) -> Path | None:
        search_roots = [from_file.parent, *(unit_dirs or []), *include_dirs]
        for root in search_roots:
            candidate = root / include_name
            if candidate.exists() and candidate.is_file():
                return candidate

        # Fallback basename search for projects that pass only a high-level include dir.
        include_basename = Path(include_name).name
        for root in include_dirs:
            if not root.exists() or not root.is_dir():
                continue
            for candidate in root.rglob(include_basename):
                if candidate.is_file():
                    return candidate
        return None

    def _copy_required_headers(
        self,
        source: Path,
        dest_include_dir: Path,
        include_dirs: list[Path],
        unit_sources: list[Path],
        symbols: list[StubSymbol],
    ) -> list[Path]:
        copied_files: list[Path] = []
        seed_files = [source, *unit_sources]
        for symbol in symbols:
            if symbol.header_path:
                header = Path(symbol.header_path)
                if header.exists() and header.is_file():
                    seed_files.append(header)

        headers = self._collect_reachable_headers(seed_files, include_dirs)
        for symbol in symbols:
            if symbol.header_path:
                header = Path(symbol.header_path)
                if header.exists() and header.is_file() and header not in headers:
                    headers.append(header)

        copied_set: set[Path] = set()
        for header in headers:
            try:
                resolved = header.resolve()
            except OSError:
                continue
            if resolved in copied_set:
                continue
            copied_set.add(resolved)

            rel_path = self._header_relative_path(header, include_dirs, source.parent)
            dest_file = dest_include_dir / rel_path
            try:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(header, dest_file)
                copied_files.append(dest_file)
            except OSError:
                continue
        return copied_files

    @staticmethod
    def _header_relative_path(header: Path, include_dirs: list[Path], source_parent: Path) -> Path:
        for root in [*include_dirs, source_parent]:
            try:
                return header.resolve().relative_to(root.resolve())
            except (OSError, ValueError):
                continue
        return Path(header.name)

    @staticmethod
    def _header_include_name(header: Path, include_dirs: list[Path], source_parent: Path) -> str:
        return StubHarnessEngine._header_relative_path(header, include_dirs, source_parent).as_posix()

    @staticmethod
    def _definition_return_prefix(return_type: str) -> str:
        clean = " ".join((return_type or "int").split()).strip()
        return clean or "int"

    @staticmethod
    def _actual_return_type(return_type: str) -> str:
        clean = " ".join((return_type or "int").split()).strip()
        func_match = re.match(r"FUNC\s*\(\s*([^,]+)\s*,", clean)
        if func_match:
            return func_match.group(1).strip()
        return clean

    @staticmethod
    def _return_lines(return_type: str, safe: str = "") -> list[str]:
        actual = StubHarnessEngine._actual_return_type(return_type)
        clean = " ".join(actual.replace("TESTING_MATE_WEAK", "").split()).strip()
        lowered = clean.lower().replace("const ", "").replace("volatile ", "")
        value = f"g_TestingMate_{safe}_return_value" if safe else "0"
        if lowered == "void":
            return ["    return;"]
        if "*" in clean:
            return [f"    return ({clean})(intptr_t){value};"]
        if lowered.startswith("struct ") or lowered.startswith("union "):
            return [f"    return ({clean}){{0}};"]
        if any(token in lowered for token in ("bool", "boolean")):
            return [f"    return {value} != 0;"]
        return [f"    return ({clean}){value};"]

    @staticmethod
    def _unused_param_lines(params: str) -> list[str]:
        if not params.strip() or params.strip() == "void":
            return []
        result = []
        for index, raw in enumerate(StubHarnessEngine._split_params(params)):
            name = StubHarnessEngine._param_name(raw, index)
            if name:
                result.append(f"    (void){name};")
        return result

    @staticmethod
    def _definition_params(params: str) -> str:
        params = " ".join((params or "").replace("\n", " ").split()).strip()
        if not params or params == "void":
            return "void"
        if "..." in params:
            return "void"
        converted: list[str] = []
        for index, raw in enumerate(StubHarnessEngine._split_params(params)):
            raw = raw.strip()
            if not raw:
                continue
            if StubHarnessEngine._param_name(raw, index):
                converted.append(raw)
            else:
                converted.append(f"{raw} p{index}")
        return ", ".join(converted) if converted else "void"

    @staticmethod
    def _param_name(param: str, index: int) -> str:
        param = param.strip()
        if not param or param == "void":
            return ""
        array_match = re.search(r"\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])\s*$", param)
        if array_match:
            return array_match.group(1)
        match = re.search(r"(?:\*|\s)([A-Za-z_]\w*)\s*$", param)
        if not match:
            return ""
        name = match.group(1)
        if name in {"char", "short", "int", "long", "float", "double", "void", "signed", "unsigned", "const", "volatile", "struct", "enum", "union"}:
            return ""
        return name

    @staticmethod
    def _split_params(params: str) -> list[str]:
        parts: list[str] = []
        depth = 0
        current: list[str] = []
        for ch in params:
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
    def _clean_return_type(return_type: str) -> str:
        clean = " ".join((return_type or "int").replace("\n", " ").split()).strip()
        clean = re.sub(r"\b(extern|static|inline|__inline|__inline__)\b", "", clean)
        clean = " ".join(clean.split()).strip()
        return clean or "int"

    def _should_ignore_call(self, name: str) -> bool:
        if name in self.ignored_calls:
            return True
        if name.startswith("__"):
            return True
        # Most all-uppercase function-like tokens are macros/constants, not link symbols.
        if name.isupper() and not name.startswith(("RTE_", "Rte_", "DEM_", "Dem_", "COM_", "Com_", "CAN_", "Can_")):
            return True
        return False

    @staticmethod
    def _safe_identifier(name: str) -> str:
        safe = re.sub(r"\W+", "_", name).strip("_")
        if not safe or safe[0].isdigit():
            safe = "stub_" + safe
        return safe

    @staticmethod
    def _strip_comments_and_strings(text: str) -> str:
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        text = re.sub(r"//.*", " ", text)
        text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
        text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
        return text
