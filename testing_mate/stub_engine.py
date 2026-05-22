
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
class HeaderGlobal:
    name: str
    ctype: str
    declaration: str
    array_suffix: str = ""
    header_path: str = ""
    header_include: str = ""


@dataclass
class StubSymbol:
    name: str
    kind: str = "function"
    return_type: str = "uint8"
    params: str = "void"
    array_suffix: str = ""
    reason: str = "external call"
    source: str = "analysis"
    from_prototype: bool = False
    prototype: str = ""
    emit_declaration: bool = False
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
    no declaration can be found does the engine create a generated ``uint8`` fallback
    declaration. This prevents broken AUTOSAR stubs such as guessed ``int`` declarations
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

        analysis_sources = self._analysis_sources_from_database(code_database)
        primary_source_include = self._primary_source_include(source, unit_sources, include_dirs)

        prototypes = self._prototype_map(source, unit_sources, include_dirs, analysis_sources)
        dependency_prototypes = self._dependency_prototype_map(source, unit_sources, include_dirs)
        global_declarations = self._global_declaration_map(source, unit_sources, include_dirs, analysis_sources)
        dependency_globals = self._dependency_global_declaration_map(source, unit_sources, include_dirs)
        defined_globals = self._defined_global_names(unit_sources)
        calls = self._referenced_calls(functions)
        calls.update(self._case_harness_calls(cases))
        controlled_returns = self._case_stub_return_calls(cases) | self._function_fixit_stub_return_calls(functions)
        calls.update(controlled_returns)

        symbols: list[StubSymbol] = []
        for name in sorted(calls):
            if (name in defined and name not in controlled_returns) or self._should_ignore_call(name):
                continue

            proto = prototypes.get(name) or dependency_prototypes.get(name)
            if proto is not None:
                rtype = proto.return_type
                params = proto.params
                from_prototype = True
                declaration_source = "header" if self._is_header_path(proto.header_path) else "preprocessed source"
                reason = f"external call declared in {declaration_source} {Path(proto.header_path).name}"
                prototype_text = proto.declaration
                header_path = proto.header_path
                header_include = proto.header_include or primary_source_include
                symbol_source = proto.header_path or str(source)
                emit_declaration = not self._is_header_path(proto.header_path)
            else:
                rtype = "uint8"
                params = "..."
                from_prototype = False
                reason = "external call without visible header declaration; uint8 fallback stub generated"
                prototype_text = f"uint8 {name}(...);"
                header_path = ""
                header_include = ""
                symbol_source = str(source)
                emit_declaration = True

            safe = self._safe_identifier(name)
            symbol = StubSymbol(
                name=name,
                kind="function",
                return_type=self._clean_return_type(rtype),
                params=self._definition_params(params),
                reason=reason,
                source=symbol_source,
                from_prototype=from_prototype,
                prototype=prototype_text,
                emit_declaration=emit_declaration,
                header_path=header_path,
                header_include=header_include,
                call_count_name=f"TestingMate_{safe}_CallCount",
                return_control_name=f"TestingMate_SetStubReturn_{safe}",
            )
            symbols.append(symbol)

        referenced_globals = self._referenced_globals(functions, code_database) | set(dependency_globals)
        for name in sorted(referenced_globals):
            if name in defined_globals or name in C_KEYWORDS or name.startswith("__"):
                continue
            declaration = global_declarations.get(name)
            if declaration is None:
                continue
            symbol = StubSymbol(
                name=name,
                kind="global",
                return_type=self._clean_variable_type(declaration.ctype),
                params="",
                array_suffix=declaration.array_suffix,
                reason=f"global reference declared in header {Path(declaration.header_path).name}",
                source=declaration.header_path or str(source),
                from_prototype=True,
                prototype=declaration.declaration,
                emit_declaration=not self._is_header_path(declaration.header_path),
                header_path=declaration.header_path,
                header_include=declaration.header_include or primary_source_include,
                call_count_name="",
                return_control_name="",
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
        h_path = stub_include_dir / "stubs.h"
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
        h_path.write_text(self._stub_header(symbols, source, unit_sources or [source]), encoding="utf-8")
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

    @staticmethod
    def _referenced_globals(functions: list[FunctionInfo], code_database: CodeDatabase | None) -> set[str]:
        globals_: set[str] = set()
        if code_database is not None:
            for record in code_database.functions:
                globals_.update(item.split(".", 1)[0] for item in record.globals_read)
                globals_.update(item.split(".", 1)[0] for item in record.globals_written)
        local_names = {local for fn in functions for local in StubHarnessEngine._local_variable_names(fn)}
        param_names = {param.name for fn in functions for param in fn.parameters}
        call_names = {name for fn in functions for name in StubHarnessEngine._calls_in_body(fn)}
        function_names = {fn.name for fn in functions}
        for fn in functions:
            body = StubHarnessEngine._strip_comments_and_strings(fn.body)
            for name in re.findall(r"\b[A-Za-z_]\w*\b", body):
                if name in C_KEYWORDS or name in local_names or name in param_names or name in call_names or name in function_names:
                    continue
                globals_.add(name)
        return globals_

    @staticmethod
    def _calls_in_body(fn: FunctionInfo) -> set[str]:
        body = StubHarnessEngine._strip_comments_and_strings(fn.body)
        return {match.group(1) for match in StubHarnessEngine.call_pattern.finditer(body)}

    def _case_harness_calls(self, cases: list[TestCase]) -> set[str]:
        calls: set[str] = set()
        for case in cases:
            calls.update(self._case_stub_return_calls([case]))
        return calls

    @staticmethod
    def _function_fixit_stub_return_calls(functions: list[FunctionInfo]) -> set[str]:
        calls: set[str] = set()
        for fn in functions:
            for hint in getattr(fn, "fixit_hints", []) or []:
                if not isinstance(hint, dict):
                    continue
                kind = str(hint.get("kind") or "").lower().replace("-", "_")
                if kind not in {"stub", "stub_return", "mock_return"}:
                    continue
                name = str(hint.get("target") or hint.get("name") or hint.get("symbol") or "").strip()
                if name:
                    calls.add(name)
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

    def _dependency_prototype_map(
        self,
        source: Path,
        unit_sources: list[Path],
        include_dirs: list[Path],
    ) -> dict[str, HeaderPrototype]:
        result: dict[str, HeaderPrototype] = {}
        for path in self._dependency_header_candidates(source, unit_sources, include_dirs):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for proto in self._parse_header_prototypes(text, path, include_dirs, source.parent):
                if proto.name not in result and not self._should_ignore_call(proto.name):
                    result[proto.name] = proto
        return result

    def _dependency_global_declaration_map(
        self,
        source: Path,
        unit_sources: list[Path],
        include_dirs: list[Path],
    ) -> dict[str, HeaderGlobal]:
        result: dict[str, HeaderGlobal] = {}
        for path in self._dependency_header_candidates(source, unit_sources, include_dirs):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for declaration in self._parse_header_globals(text, path, include_dirs, source.parent):
                result.setdefault(declaration.name, declaration)
        return result

    def _dependency_header_candidates(
        self,
        source: Path,
        unit_sources: list[Path],
        include_dirs: list[Path],
    ) -> list[Path]:
        candidates: list[Path] = []
        for path in [source.with_suffix(".h"), *(src.with_suffix(".h") for src in unit_sources)]:
            if path.exists() and path.is_file():
                candidates.append(path)
        candidates.extend(self._collect_reachable_headers([source, *unit_sources], include_dirs))

        deduped: list[Path] = []
        seen: set[Path] = set()
        for path in candidates:
            if path.suffix.lower() not in HEADER_SOURCE_EXTENSIONS:
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            deduped.append(path)
        return deduped

    def _defined_global_names(self, unit_sources: list[Path]) -> set[str]:
        result: set[str] = set()
        for source in unit_sources:
            if source.suffix.lower() not in C_SOURCE_EXTENSIONS:
                continue
            try:
                text = source.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            sanitized = self._strip_comments_and_strings(text)
            sanitized = re.sub(r"^\s*#.*$", "", sanitized, flags=re.MULTILINE)
            for statement in self._iter_top_level_statements(sanitized):
                name = self._parse_global_name_statement(statement)
                if name:
                    result.add(name)
        return result

    @staticmethod
    def _local_variable_names(fn: FunctionInfo) -> set[str]:
        names: set[str] = set()
        declaration = re.compile(
            r"\b(?:const\s+|volatile\s+|static\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|"
            r"char\s+|int\s+|float\s+|double\s+|bool\s+|uint\d+_t\s+|int\d+_t\s+|size_t\s+|[A-Za-z_]\w+_t\s+)+"
            r"[*\s]*([A-Za-z_]\w*)\s*(?:=|;|,|\[)"
        )
        for match in declaration.finditer(StubHarnessEngine._strip_comments_and_strings(fn.body)):
            names.add(match.group(1))
        return names

    @staticmethod
    def _analysis_sources_from_database(code_database: CodeDatabase | None) -> list[Path]:
        if code_database is None:
            return []
        paths: list[Path] = []
        for raw in (code_database.analysis_source_path, code_database.source_path):
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
        declaration_sources: list[Path] | None = None,
    ) -> dict[str, HeaderPrototype]:
        candidates = [*(declaration_sources or []), *self._prototype_candidate_files(source, unit_sources, include_dirs)]
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

    def _global_declaration_map(
        self,
        source: Path,
        unit_sources: list[Path],
        include_dirs: list[Path],
        declaration_sources: list[Path] | None = None,
    ) -> dict[str, HeaderGlobal]:
        candidates = [*(declaration_sources or []), *self._prototype_candidate_files(source, unit_sources, include_dirs)]
        result: dict[str, HeaderGlobal] = {}
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
            for declaration in self._parse_header_globals(text, path, include_dirs, source.parent):
                if declaration.name not in result:
                    result[declaration.name] = declaration
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
            proto.header_include = self._header_include_name(path, include_dirs, source_parent) if self._is_header_path(path) else ""
            prototypes.append(proto)
        return prototypes

    def _parse_header_globals(
        self,
        text: str,
        path: Path,
        include_dirs: list[Path],
        source_parent: Path,
    ) -> list[HeaderGlobal]:
        sanitized = self._strip_comments_and_strings(text)
        sanitized = re.sub(r"^\s*#.*$", "", sanitized, flags=re.MULTILINE)

        globals_: list[HeaderGlobal] = []
        for statement in self._iter_top_level_statements(sanitized):
            declaration = self._parse_global_declaration_statement(statement)
            if declaration is None:
                continue
            declaration.header_path = str(path)
            declaration.header_include = self._header_include_name(path, include_dirs, source_parent) if self._is_header_path(path) else ""
            globals_.append(declaration)
        return globals_

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

    def _parse_global_declaration_statement(self, statement: str) -> HeaderGlobal | None:
        stmt = " ".join(statement.strip().rstrip(";").split())
        if not stmt or "(" in stmt and not re.search(r"\b(?:VAR|CONST|P2VAR|P2CONST)\s*\(", stmt):
            return None
        if re.search(r"\btypedef\b", stmt) or "{" in stmt or "}" in stmt:
            return None
        if "=" in stmt:
            return None
        if "extern" not in stmt and not re.match(r"^(?:VAR|CONST|P2VAR|P2CONST)\s*\(", stmt):
            return None

        stmt = re.sub(r"\b__attribute__\s*\(\(.*?\)\)", "", stmt).strip()
        var_match = re.match(
            r"(?P<ctype>.+?(?:\*|\s))(?P<name>[A-Za-z_]\w*)\s*(?P<array>(?:\[[^\]]*\]\s*)*)$",
            stmt,
        )
        if not var_match:
            return None
        name = var_match.group("name")
        if name in C_KEYWORDS:
            return None
        ctype = self._clean_variable_type(var_match.group("ctype"))
        if not ctype:
            return None
        return HeaderGlobal(
            name=name,
            ctype=ctype,
            declaration=stmt + ";",
            array_suffix="".join((var_match.group("array") or "").split()),
        )

    @staticmethod
    def _parse_global_name_statement(statement: str) -> str:
        stmt = " ".join(statement.strip().rstrip(";").split())
        if not stmt or "(" in stmt or re.search(r"\b(?:typedef|extern)\b", stmt):
            return ""
        stmt = stmt.split("=", 1)[0].strip()
        var_match = re.match(
            r"(?P<ctype>.+?(?:\*|\s))(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*$",
            stmt,
        )
        if not var_match:
            return ""
        name = var_match.group("name")
        return "" if name in C_KEYWORDS else name

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
        lines = [
            "/* Generated by Testing Mate stub harness engine. */",
            "/* Review every stub before using the result as safety evidence. */",
            "#include <stddef.h>",
            "#include <stdint.h>",
            "#include <stdbool.h>",
            "#include \"stubs.h\"",
        ]

        lines.extend(
            [
                "",
                "#if (defined(__GNUC__) || defined(__clang__)) && !defined(_WIN32) && !defined(__MINGW32__)",
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
            lines.extend([
                "void TestingMate_ResetStubs(void)",
                "{",
                "    /* No generated function stubs to reset. */",
                "}",
                "",
            ])
            return "\n".join(lines)

        function_symbols: list[StubSymbol] = []
        for symbol in symbols:
            if symbol.kind != "global":
                continue
            lines.append(self._global_definition_line(symbol))
            lines.append("")

        for symbol in symbols:
            if symbol.kind != "function":
                continue
            function_symbols.append(symbol)
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
        lines.extend([
            "void TestingMate_ResetStubs(void)",
            "{",
        ])
        if function_symbols:
            for symbol in function_symbols:
                safe = self._safe_identifier(symbol.name)
                lines.append(f"    g_TestingMate_{safe}_calls = 0U;")
                lines.append(f"    g_TestingMate_{safe}_return_value = 0;")
        else:
            lines.append("    /* No generated function stubs to reset. */")
        lines.extend(["}", ""])
        return "\n".join(lines)

    def _stub_header(self, symbols: list[StubSymbol], source: Path, unit_sources: list[Path]) -> str:
        production_includes: list[str] = []
        seen_headers: set[str] = set()

        for name, directive in self._collect_source_header_includes(source, unit_sources):
            if name not in seen_headers:
                production_includes.append(directive)
                seen_headers.add(name)

        for symbol in symbols:
            if symbol.header_include and symbol.header_include not in seen_headers:
                production_includes.append(f"#include \"{symbol.header_include}\"")
                seen_headers.add(symbol.header_include)
    
        lines = [
            "#ifndef STUBS_H",
            "#define STUBS_H\n",
            ]
        
        for header in production_includes:
            lines.append(header)

        if production_includes:
            lines.append("")
        if any(symbol.kind == "function" and symbol.emit_declaration for symbol in symbols):
            lines.extend(
                [
                    "#include <stdint.h>",
                    "#include <stdbool.h>",
                    "",
                ]
            )
        lines.extend(["#ifdef __cplusplus",
            "extern \"C\" {",
            "#endif",
            "",
            "void TestingMate_StubsPresent(void);",
            "void TestingMate_ResetStubs(void);",])
        
        for symbol in symbols:
            if symbol.kind == "global":
                ctype = self._clean_variable_type(symbol.return_type)
                lines.append(f"extern {ctype} {symbol.name}{symbol.array_suffix};")
                continue
            if symbol.kind != "function":
                continue
            lines.append(f"unsigned {symbol.call_count_name}(void);")
            lines.append(f"void {symbol.return_control_name}(long long value);")
            if symbol.emit_declaration and symbol.from_prototype and symbol.prototype:
                lines.append(symbol.prototype)
            elif symbol.emit_declaration:
                lines.append("#ifdef __cplusplus")
                lines.append(f"uint8 {symbol.name}(...);")
                lines.append("#else")
                lines.append(f"uint8 {symbol.name}();")
                lines.append("#endif")
        lines.extend(
            [
                "",
                "#ifdef __cplusplus",
                "}",
                "#endif",
                "",
                "#endif /* STUBS_H */",
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
            "When a header declaration is available, the stub implementation uses that exact signature shape instead of guessing.",
            "When a preprocessed analysis source is available, Testing Mate also mines it for external function and variable declarations.",
            "When no declaration is found, Testing Mate emits a generated `uint8 name(...)` fallback declaration and a buildable C stub.",
            "The main coverage report remains target-only; these stub files are not intended to contribute to unit coverage.",
            "",
            "## Symbols",
            "",
        ]
        if not symbols:
            lines.append("No external function or header-declared global references were discovered.")
        else:
            lines.append("| Symbol | Kind | Type/Return | Params | Header | Reason | Call-count accessor |")
            lines.append("|---|---|---|---|---|---|---|")
            for symbol in symbols:
                header = symbol.header_include or "fallback"
                accessor = f"`{symbol.call_count_name}()`" if symbol.call_count_name else ""
                lines.append(
                    f"| `{symbol.name}` | `{symbol.kind}` | `{symbol.return_type}` | `{symbol.params}` | `{header}` | {symbol.reason} | {accessor} |"
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

    def _extract_include_directives(self, source: Path) -> list[tuple[str, str]]:
        directives: list[tuple[str, str]] = []
        if not source.exists():
            return directives
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return directives
        for match in self.include_pattern.finditer(text):
            delim = match.group(1)
            name = match.group(2).strip()
            close = ">" if delim == "<" else '"'
            directives.append((name, f"#include {delim}{name}{close}"))
        return directives

    def _collect_source_header_includes(self, source: Path, unit_sources: list[Path]) -> list[tuple[str, str]]:
        includes: list[tuple[str, str]] = []
        seen_headers: set[str] = set()
        for path in [source, *unit_sources]:
            if not path.exists() or not path.is_file():
                continue
            for name, directive in self._extract_include_directives(path):
                if name not in seen_headers:
                    includes.append((name, directive))
                    seen_headers.add(name)
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
    def _is_header_path(path: str | Path) -> bool:
        text = str(path or "")
        return bool(text) and Path(text).suffix.lower() in HEADER_SOURCE_EXTENSIONS

    def _primary_source_include(self, source: Path, unit_sources: list[Path], include_dirs: list[Path]) -> str:
        unit_dirs = [item.parent for item in [source, *unit_sources] if item.exists()]
        for seed in [source, *unit_sources]:
            if not seed.exists() or not seed.is_file():
                continue
            try:
                text = seed.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in self.include_pattern.finditer(text):
                include_name = match.group(2).strip()
                resolved = self._resolve_include(include_name, seed, include_dirs, unit_dirs)
                if resolved is not None and self._is_header_path(resolved):
                    return include_name.replace("\\", "/")
        sibling = source.with_suffix(".h")
        if sibling.exists() and sibling.is_file():
            return self._header_include_name(sibling, include_dirs, source.parent)
        return ""

    @staticmethod
    def _definition_return_prefix(return_type: str) -> str:
        clean = " ".join((return_type or "int").split()).strip()
        return clean or "int"

    @staticmethod
    def _global_definition_line(symbol: StubSymbol) -> str:
        ctype = StubHarnessEngine._clean_variable_type(symbol.return_type)
        suffix = symbol.array_suffix or ""
        initializer = StubHarnessEngine._global_initializer(ctype, suffix)
        return f"TESTING_MATE_WEAK {ctype} {symbol.name}{suffix} = {initializer};"

    @staticmethod
    def _global_initializer(ctype: str, suffix: str = "") -> str:
        lowered = ctype.lower()
        if suffix or lowered.startswith("struct ") or lowered.startswith("union "):
            return "{0}"
        if "*" in ctype:
            return "0"
        if "bool" in lowered or "boolean" in lowered:
            return "0"
        return "0"

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

    @staticmethod
    def _clean_variable_type(ctype: str) -> str:
        clean = " ".join((ctype or "int").replace("\n", " ").split()).strip()
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
