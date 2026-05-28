from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from testing_mate.analysis import AstCache
from testing_mate.c_backend import CAnalyzer, attach_fixit_hints
from testing_mate.constants import C_KEYWORDS
from testing_mate.mcdc_engine import MCDCAnalyzer
from testing_mate.models import Condition, FunctionInfo, Parameter


class ClangEnhancedAnalyzer:
    """Analyze C/C++ source with Clang AST JSON, falling back to CAnalyzer.

    The output intentionally stays compatible with the existing FunctionInfo
    model so vector generation can become more deterministic incrementally.
    
    Supports preprocessing step: can analyze preprocessed source files with
    resolved includes and injected stubs for better heuristic accuracy.
    """

    def __init__(
        self,
        include_dirs: list[str] | None = None,
        defines: list[str] | None = None,
        compiler: str = "clang",
        log: Callable[[str], None] | None = None,
        preprocessor=None,
    ):
        self.include_dirs = self._unique_strings(include_dirs or [])
        self.defines = defines or []
        self.compiler = compiler
        self.log = log or (lambda _text: None)
        self.fallback = CAnalyzer()
        self.last_analyzer = "unresolved"
        self.preprocessor = preprocessor  # Optional CPreprocessor instance
        self._preprocessed_cache: dict[str, Path] = {}  # Cache preprocessed files
        self.ast_cache = AstCache()

    def analyze(self, source: Path, include_static_functions: bool = False, include_main: bool = False) -> list[FunctionInfo]:
        try:
            original_text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            original_text = ""

        # Preprocess source file if preprocessor is available
        source_for_analysis = source
        preprocess_msg = ""
        if self.preprocessor:
            try:
                preprocessed_path, msg = self.preprocessor.preprocess_file(source, Path(source.parent) / ".preprocessed")
                source_for_analysis = preprocessed_path
                preprocess_msg = f" [Preprocessed: {msg}]"
                self._preprocessed_cache[str(source)] = preprocessed_path
            except Exception as exc:
                self.log(f"Preprocessing failed for {source.name}; proceeding with raw source: {exc}")
        
        clang = self._clang_executable()
        if not clang:
            self.log("Clang analysis skipped: clang executable was not found; using internal analyzer.")
            self.last_analyzer = "internal_fallback"
            functions = self.fallback.analyze(source_for_analysis, include_static_functions, include_main)
            self._apply_original_source_conditions(functions, original_text)
            for fn in functions:
                attach_fixit_hints(fn, source="clang_fallback")
            return functions

        try:
            ast = self._load_ast(clang, source_for_analysis)
            text = self.ast_cache.source_text(source_for_analysis)
            enum_types = self.fallback._enum_types(text)
            functions = self._functions_from_ast(ast, source_for_analysis, text, enum_types, include_static_functions, include_main)
            if functions:
                self._apply_original_source_conditions(functions, original_text)
                self.log(f"Clang analysis: extracted {len(functions)} functions from {source.name}.{preprocess_msg}")
                self.last_analyzer = "clang_ast_json"
                return functions
            self.log(f"Clang analysis found no function definitions; using internal analyzer.{preprocess_msg}")
        except Exception as exc:
            self.log(f"Clang analysis failed for {source.name}; using internal analyzer. {str(exc)[:240]}{preprocess_msg}")
        self.last_analyzer = "internal_fallback"
        functions = self.fallback.analyze(source_for_analysis, include_static_functions, include_main)
        self._apply_original_source_conditions(functions, original_text)
        for fn in functions:
            attach_fixit_hints(fn, source="clang_fallback")
        return functions

    def _apply_original_source_conditions(self, functions: list[FunctionInfo], source_text: str) -> None:
        if not source_text.strip():
            return
        source_index = self._source_function_index(source_text)
        for fn in functions:
            source_fn = source_index.get(fn.name)
            if source_fn is None:
                continue
            fn.start_line = source_fn.start_line
            fn.end_line = source_fn.end_line
            fn.body = source_fn.body
            if source_fn.conditions:
                analysis_conditions = list(fn.conditions)
                for index, source_condition in enumerate(source_fn.conditions):
                    if index >= len(analysis_conditions):
                        continue
                    analysis_condition = analysis_conditions[index]
                    if analysis_condition.text != source_condition.text:
                        source_condition.analysis_text = analysis_condition.text
                    source_condition.analysis_decisions = [
                        item for item in analysis_condition.decisions
                        if item and item not in source_condition.decisions
                    ]
                    source_condition.analysis_line = analysis_condition.line
                fn.conditions = source_fn.conditions

    def _source_function_index(self, source_text: str) -> dict[str, FunctionInfo]:
        result: dict[str, FunctionInfo] = {}
        line_starts = self.fallback._line_offsets(source_text)
        for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", source_text):
            name = match.group(1)
            if name in C_KEYWORDS or name in {"FUNC", "P2VAR", "P2CONST", "CONSTP2VAR", "P2FUNC"} or name in result:
                continue
            _params, close_paren = CAnalyzer._extract_parenthesized(source_text, match.end() - 1)
            if close_paren < 0:
                continue
            brace = self._next_nonspace_index(source_text, close_paren + 1)
            if brace is None or brace >= len(source_text) or source_text[brace] != "{":
                continue
            if self._looks_like_control_statement(source_text, match.start()):
                continue
            body_end = CAnalyzer._matching_brace(source_text, brace)
            if body_end < 0:
                continue
            body = source_text[brace + 1 : body_end]
            start_line = CAnalyzer._line_for_offset(line_starts, match.start())
            end_line = CAnalyzer._line_for_offset(line_starts, body_end)
            result[name] = FunctionInfo(
                name=name,
                return_type="",
                parameters=[],
                start_line=start_line,
                end_line=end_line,
                body=body,
                conditions=self.fallback._conditions(body, start_line),
            )
        return result

    @staticmethod
    def _next_nonspace_index(text: str, index: int) -> int | None:
        while index < len(text) and text[index].isspace():
            index += 1
        return index if index < len(text) else None

    @staticmethod
    def _looks_like_control_statement(text: str, name_start: int) -> bool:
        prefix = text[max(0, name_start - 32) : name_start]
        return bool(re.search(r"\b(if|for|while|switch|return|sizeof)\s*$", prefix))

    def _clang_executable(self) -> str | None:
        compiler_name = Path(self.compiler.strip().split()[0]).name.lower() if self.compiler.strip() else ""
        if "clang" in compiler_name:
            found = shutil.which(self.compiler.strip().split()[0])
            if found:
                return found
        return shutil.which("clang") or shutil.which("clang.exe")

    def set_preprocessor(self, preprocessor) -> None:
        """Set or update the preprocessor instance for use in analysis."""
        self.preprocessor = preprocessor

    def get_preprocessed_source(self, source: Path) -> Path | None:
        """Get the cached preprocessed path for a source file if available."""
        return self._preprocessed_cache.get(str(source))

    def apply_original_source_context(self, functions: list[FunctionInfo], source: Path) -> None:
        try:
            source_text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self._apply_original_source_conditions(functions, source_text)

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
    def _effective_include_dirs(self, source: Path) -> list[str]:
        return self._unique_strings([*self.include_dirs])
    def _attach_function_metadata(self, fn: FunctionInfo, node: dict, body_node: dict, text: str) -> None:
        """Preserve AST context and FixItHint-style analysis hints for vector generation."""
        try:
            fn.ast_node = node
            fn.ast_body_node = body_node
            fn.ast_source_text = text
        except Exception:
            pass
        attach_fixit_hints(fn, source="clang_ast")
    def _load_ast(self, clang: str, source: Path) -> dict:
        context = "|".join([clang, *self._effective_include_dirs(source), *self.defines])
        return self.ast_cache.ast_for(source, lambda path: self._load_ast_uncached(clang, path), context)

    def _load_ast_uncached(self, clang: str, source: Path) -> dict:
        # Determine if source is preprocessed based on naming or check if preprocessor was used
        is_preprocessed = ".preprocessed" in source.name or source.parent.name == ".preprocessed"
        
        cmd = [
            clang,
            "-x",
            "c++" if source.suffix.lower() in {".cc", ".cpp", ".cxx", ".hpp"} else "c",
            "-fsyntax-only",
            "-Xclang",
            "-ast-dump=json",
            "-ferror-limit=0",
        ]
        
        # For preprocessed files, skip include dirs as they are already resolved
        if not is_preprocessed:
            for include_dir in self._effective_include_dirs(source):
                cmd.extend(["-I", include_dir])
            for define in self.defines:
                cmd.append("-D" + define)
        else:
            # For preprocessed files, defines are already expanded, but keep minimal flags
            # to avoid re-expansion issues
            pass
        
        cmd.append(str(source))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0 and not result.stdout.strip():
            raise RuntimeError(result.stderr.strip() or f"clang exited with code {result.returncode}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"clang AST JSON could not be parsed: {exc}") from exc

    def _functions_from_ast(
        self,
        ast: dict,
        source: Path,
        text: str,
        enum_types: dict[str, list[str]],
        include_static_functions: bool,
        include_main: bool,
    ) -> list[FunctionInfo]:
        source_key = self._path_key(source)
        # For preprocessed files, also try the original source name if available
        source_name_key = source.stem.replace(".preprocessed", "")
        functions: list[FunctionInfo] = []
        seen: set[tuple[str, int]] = set()
        for node in self._walk(ast):
            if node.get("kind") != "FunctionDecl" or not node.get("isUsed", True):
                continue
            if not self._is_definition(node):
                continue
            name = str(node.get("name") or "")
            if not name or name in C_KEYWORDS or name.startswith("__") or name.startswith("_"):
                continue
            if name == "main" and not include_main:
                continue
            if node.get("storageClass") == "static" and not include_static_functions:
                continue
            if not self._node_belongs_to_source(node, source_key, source_name_key):
                continue

            body_node = self._first_child(node, "CompoundStmt")
            if body_node is None:
                continue
            start_line = self._line(node.get("loc") or node.get("range", {}).get("begin")) or 1
            end_line = self._line(node.get("range", {}).get("end")) or start_line
            key = (name, start_line)
            if key in seen:
                continue
            seen.add(key)

            body = self._body_text(text, body_node)
            return_type = self._return_type(node)
            fn = FunctionInfo(
                name=name,
                return_type=return_type,
                parameters=self._parameters(node, enum_types),
                start_line=start_line,
                end_line=end_line,
                body=body,
            )
            fn.conditions = self._conditions_from_ast(body_node, text, start_line)
            if not fn.conditions:
                fn.conditions = self.fallback._conditions(body, start_line)
            self._attach_function_metadata(fn, node, body_node, text)
            functions.append(fn)
        return functions

    def _parameters(self, function_node: dict, enum_types: dict[str, list[str]]) -> list[Parameter]:
        params: list[Parameter] = []
        for child in function_node.get("inner", []) or []:
            if child.get("kind") != "ParmVarDecl":
                continue
            name = str(child.get("name") or f"param{len(params) + 1}")
            ctype = str((child.get("type") or {}).get("qualType") or "int")
            kind = "pointer" if "*" in ctype or "[" in ctype or "(*)" in ctype else "scalar"
            enum_values = self.fallback._enum_values_for_type(ctype, enum_types)
            params.append(Parameter(name=name, ctype=ctype, kind=kind, enum_values=enum_values))
        return params

    def _conditions_from_ast(self, body_node: dict, text: str, function_start_line: int) -> list[Condition]:
        conditions: list[Condition] = []
        for node in self._walk(body_node):
            kind = node.get("kind")
            if kind not in {"IfStmt", "WhileStmt", "DoStmt", "ForStmt", "SwitchStmt"}:
                continue
            condition_node = self._condition_child(node, kind)
            condition_text = self._text_for_node(text, condition_node or node)
            condition_text = self._normalize_condition_text(condition_text, kind)
            if not condition_text:
                continue
            loc = condition_node.get("range", {}).get("begin") if condition_node else node.get("loc")
            line = self._line(loc) or self._line(node.get("range", {}).get("begin")) or function_start_line
            decisions = [
                atom.text
                for atom in MCDCAnalyzer().extract_atomic_conditions(
                    Condition(text=condition_text, line=line, depth=self._ast_depth(body_node, node))
                )
                if atom.text
            ]
            conditions.append(
                Condition(
                    text=condition_text,
                    line=line,
                    decisions=decisions or [condition_text],
                    depth=self._ast_depth(body_node, node),
                )
            )
        return conditions

    @staticmethod
    def _walk(node: dict) -> Iterable[dict]:
        yield node
        for child in node.get("inner", []) or []:
            if isinstance(child, dict):
                yield from ClangEnhancedAnalyzer._walk(child)

    @staticmethod
    def _first_child(node: dict, kind: str) -> dict | None:
        for child in node.get("inner", []) or []:
            if child.get("kind") == kind:
                return child
        return None

    @staticmethod
    def _condition_child(node: dict, kind: str) -> dict | None:
        children = [child for child in node.get("inner", []) or [] if isinstance(child, dict)]
        if kind == "ForStmt":
            for child in children:
                if child.get("kind") in {"BinaryOperator", "UnaryOperator", "ImplicitCastExpr", "DeclRefExpr", "CallExpr"}:
                    return child
            return None
        if kind == "SwitchStmt":
            return children[0] if children else None
        for child in children:
            if child.get("kind") not in {"CompoundStmt", "NullStmt", "ReturnStmt", "BreakStmt"}:
                return child
        return None

    @classmethod
    def _ast_depth(cls, root: dict, target: dict, depth: int = 0) -> int:
        if root is target:
            return depth
        for child in root.get("inner", []) or []:
            if not isinstance(child, dict):
                continue
            found = cls._ast_depth(child, target, depth + (1 if child.get("kind", "").endswith("Stmt") else 0))
            if found >= 0:
                return found
        return -1

    @staticmethod
    def _is_definition(node: dict) -> bool:
        if node.get("isThisDeclarationADefinition") is True:
            return True
        return any(child.get("kind") == "CompoundStmt" for child in node.get("inner", []) or [])

    @classmethod
    def _node_belongs_to_source(cls, node: dict, source_key: str, source_name_key: str = "") -> bool:
        for loc in (node.get("loc"), node.get("range", {}).get("begin"), node.get("range", {}).get("end")):
            file_name = cls._file_from_loc(loc)
            if file_name:
                file_key = cls._path_key(Path(file_name))
                # Check exact match or name-only match for preprocessed files
                if file_key == source_key or (source_name_key and source_name_key in file_key.lower()):
                    return True
        return not cls._file_from_loc(node.get("loc"))

    @staticmethod
    def _file_from_loc(loc: dict | None) -> str:
        if not isinstance(loc, dict):
            return ""
        return str(loc.get("file") or loc.get("includedFrom", {}).get("file") or "")

    @staticmethod
    def _line(loc: dict | None) -> int | None:
        if not isinstance(loc, dict):
            return None
        value = loc.get("line")
        return int(value) if isinstance(value, int) else None

    @staticmethod
    def _offset(loc: dict | None) -> int | None:
        if not isinstance(loc, dict):
            return None
        value = loc.get("offset")
        return int(value) if isinstance(value, int) else None

    @classmethod
    def _text_for_node(cls, text: str, node: dict | None) -> str:
        if not node:
            return ""
        begin = cls._offset(node.get("range", {}).get("begin"))
        end = cls._offset(node.get("range", {}).get("end"))
        if begin is None or end is None or begin < 0 or end < begin or end >= len(text):
            return ""
        end = cls._expand_range_end(text, end)
        return text[begin : end].strip()

    @staticmethod
    def _expand_range_end(text: str, end: int) -> int:
        if end >= len(text):
            return len(text)
        if text[end].isalnum() or text[end] == "_":
            while end < len(text) and (text[end].isalnum() or text[end] in "_."):
                end += 1
            return end
        return end + 1

    @classmethod
    def _body_text(cls, text: str, compound_node: dict) -> str:
        raw = cls._text_for_node(text, compound_node)
        if raw.startswith("{") and raw.endswith("}"):
            return raw[1:-1]
        return raw

    @staticmethod
    def _return_type(node: dict) -> str:
        qual_type = str((node.get("type") or {}).get("qualType") or "void")
        match = re.match(r"(?P<rtype>.+?)\s*\([^()]*\)\s*$", qual_type)
        if match:
            return " ".join(match.group("rtype").split())
        return " ".join(qual_type.split())

    @staticmethod
    def _normalize_condition_text(text: str, kind: str) -> str:
        clean = " ".join(text.split())
        if kind == "SwitchStmt":
            match = re.match(r"switch\s*\((.*)\)", clean)
            if match:
                return f"switch({match.group(1).strip()})"
        if kind in {"IfStmt", "WhileStmt"}:
            match = re.match(r"(?:if|while)\s*\((.*)\)", clean)
            if match:
                return match.group(1).strip()
        if kind == "ForStmt":
            match = re.match(r"for\s*\((.*)\)", clean)
            if match:
                pieces = CAnalyzer._split_top_level(match.group(1), ";")
                if len(pieces) == 3:
                    return pieces[1].strip()
        return clean

    @staticmethod
    def _path_key(path: Path) -> str:
        try:
            return str(Path(path).resolve()).replace("\\", "/").lower()
        except OSError:
            return str(path).replace("\\", "/").lower()
