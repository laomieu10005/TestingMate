from pathlib import Path
import json
import os
import re
import shlex
import shutil
import subprocess

from testing_mate.constants import C_KEYWORDS, DEFAULT_COMPILER, C_SOURCE_EXTENSIONS, HEADER_SOURCE_EXTENSIONS, SOURCE_EXTENSIONS, EXCLUDED_SOURCE_DIRS

from testing_mate.models import *
from testing_mate.toolchain import toolchain_env


def validate_toolchain(compiler: str = DEFAULT_COMPILER, log_fn=None) -> tuple[bool, str]:
    """Validate that required compiler and tools are available.
    
    Phase D optimization: early toolchain validation prevents downstream failures.
    Returns (is_valid, message) where message contains status/warnings.
    """
    log_fn = log_fn or (lambda _: None)
    env = toolchain_env()
    compiler_path = shutil.which(compiler, path=env.get("PATH"))
    
    status_lines = []
    if compiler_path:
        status_lines.append(f"✓ Compiler ({compiler}): {compiler_path}")
    else:
        status_lines.append(f"✗ Compiler ({compiler}): NOT FOUND")
        status_lines.append("  Hint: Install GCC/Clang or set TESTMATE_TOOLCHAIN_PATH")
        return False, "\n".join(status_lines)
    
    # Check for supporting tools
    cmake = shutil.which("cmake", path=env.get("PATH"))
    ninja = shutil.which("ninja", path=env.get("PATH"))
    
    if cmake:
        status_lines.append(f"✓ CMake: {cmake}")
    else:
        status_lines.append("⚠ CMake: NOT FOUND (needed for CppUTest/GoogleTest generation)")
    
    if ninja:
        status_lines.append(f"✓ Ninja: {ninja}")
    else:
        status_lines.append("⚠ Ninja: NOT FOUND (CMake will use default generator)")
    
    return True, "\n".join(status_lines)



def _unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


class ProjectScanner:
    """Discover source files from a project folder without depending on a build system."""

    @staticmethod
    def discover_sources(
        root: Path,
        extensions: set[str] | None = None,
        include_headers: bool = True,
        excluded_dirs: set[str] | None = None,
    ) -> list[Path]:
        root = root.expanduser()
        extensions = extensions or (SOURCE_EXTENSIONS if include_headers else C_SOURCE_EXTENSIONS)
        excluded_dirs = {item.lower() for item in (excluded_dirs or EXCLUDED_SOURCE_DIRS)}
        if not root.exists():
            return []
        if root.is_file():
            return [root.resolve()] if root.suffix.lower() in extensions else []
        result: list[Path] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in extensions:
                continue
            if any(part.lower() in excluded_dirs for part in path.parts):
                continue
            result.append(path)
        return _unique_paths(sorted(result, key=lambda item: str(item).lower()))


class CompileCommandsDatabase:
    """Small parser for CMake/Ninja compile_commands.json files."""

    def __init__(self, path: Path, entries: list[dict]):
        self.path = path.expanduser().resolve()
        self.entries = entries

    @classmethod
    def load(cls, path: Path) -> "CompileCommandsDatabase":
        path = path.expanduser().resolve()
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(payload, list):
            raise ValueError("compile_commands.json must contain a list of compile commands")
        return cls(path, [entry for entry in payload if isinstance(entry, dict)])

    def source_files(self) -> list[Path]:
        files: list[Path] = []
        for entry in self.entries:
            raw_file = entry.get("file")
            if not raw_file:
                continue
            directory = Path(str(entry.get("directory") or self.path.parent)).expanduser()
            path = Path(str(raw_file))
            if not path.is_absolute():
                path = directory / path
            if path.suffix.lower() in SOURCE_EXTENSIONS and path.exists():
                files.append(path)
        return _unique_paths(files)

    def include_dirs(self) -> list[str]:
        includes: list[Path] = []
        for entry in self.entries:
            directory = Path(str(entry.get("directory") or self.path.parent)).expanduser()
            args = self._args(entry)
            index = 0
            while index < len(args):
                arg = args[index]
                value: str | None = None
                if arg == "-I" and index + 1 < len(args):
                    value = args[index + 1]
                    index += 1
                elif arg.startswith("-I") and len(arg) > 2:
                    value = arg[2:]
                elif arg == "-isystem" and index + 1 < len(args):
                    value = args[index + 1]
                    index += 1
                elif arg.startswith("/I") and len(arg) > 2:
                    value = arg[2:]
                if value:
                    inc = Path(value.strip('"'))
                    if not inc.is_absolute():
                        inc = directory / inc
                    if inc.exists():
                        includes.append(inc)
                index += 1
        return [str(path) for path in _unique_paths(includes)]

    def defines(self) -> list[str]:
        defines: list[str] = []
        seen: set[str] = set()
        for entry in self.entries:
            args = self._args(entry)
            index = 0
            while index < len(args):
                arg = args[index]
                value: str | None = None
                if arg == "-D" and index + 1 < len(args):
                    value = args[index + 1]
                    index += 1
                elif arg.startswith("-D") and len(arg) > 2:
                    value = arg[2:]
                elif arg.startswith("/D") and len(arg) > 2:
                    value = arg[2:]
                if value:
                    value = value.strip('"')
                    if value and value not in seen:
                        seen.add(value)
                        defines.append(value)
                index += 1
        return defines

    def compiler(self) -> str | None:
        for entry in self.entries:
            args = self._args(entry)
            if args:
                return args[0]
        return None

    @staticmethod
    def _args(entry: dict) -> list[str]:
        args = entry.get("arguments")
        if isinstance(args, list):
            return [str(item) for item in args]
        command = str(entry.get("command") or "")
        if not command.strip():
            return []
        try:
            return shlex.split(command, posix=False)
        except ValueError:
            return command.split()


class CPreprocessor:
    def __init__(self, compiler: str = DEFAULT_COMPILER, include_dirs: list[str] | None = None, defines: list[str] | None = None, stub_dir: Path | None = None):
        self.compiler = compiler.strip() or DEFAULT_COMPILER
        self.include_dirs = include_dirs or []
        self.defines = defines or []
        self.stub_dir = Path(stub_dir).expanduser() if stub_dir else None
        self._preprocess_cache: dict[str, Path] = {}  # Cache by source path
        self._missing_symbols: dict[str, set[str]] = {}  # Track missing symbols per file
    
    @staticmethod
    def _cache_key(source: Path, include_dirs: list[str], defines: list[str]) -> str:
        """Generate a cache key based on source hash and parameters.
        
        Phase D optimization: preprocess caching reduces redundant gcc calls.
        """
        import hashlib
        try:
            content_hash = hashlib.md5(source.read_bytes()).hexdigest()
        except OSError:
            content_hash = "unknown"
        includes_str = "|".join(sorted(include_dirs))
        defines_str = "|".join(sorted(defines))
        combined = f"{source.stem}:{content_hash}:{includes_str}:{defines_str}"
        return hashlib.md5(combined.encode()).hexdigest()

    def preprocess_file(self, source: Path, out_dir: Path) -> tuple[Path, str]:
        # Check cache first (Phase D: preprocess caching)
        cache_key = self._cache_key(source, self.include_dirs, self.defines)
        if cache_key in self._preprocess_cache:
            cached_path = self._preprocess_cache[cache_key]
            if cached_path.exists():
                return cached_path, f"Cached preprocessed: {source.name}"
        
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{source.stem}.preprocessed.c"
        env = toolchain_env()
        
        # Build include directories, prioritizing stub_dir if available
        include_dirs = self.include_dirs.copy()
        if self.stub_dir and self.stub_dir.exists():
            stub_include = self.stub_dir / "stub" / "include"
            if stub_include.exists():
                include_dirs.insert(0, str(stub_include))
        
        compiler_path = shutil.which(self.compiler, path=env.get("PATH"))
        if compiler_path:
            cmd = [compiler_path, "-E", "-P", str(source)]
            for inc in include_dirs:
                if inc:
                    cmd.extend(["-I", inc])
            for define in self.defines:
                if define:
                    cmd.append(f"-D{define}")
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False, env=env)
                if result.returncode == 0 and result.stdout.strip():
                    processed_text = result.stdout
                    # Detect missing symbols and try to add stubs
                    processed_text = self._inject_missing_stubs(processed_text, source)
                    target.write_text(processed_text, encoding="utf-8", errors="replace")
                    self._preprocess_cache[cache_key] = target
                    return target, f"Preprocessed with {compiler_path}: {source.name}"
                fallback = self._fallback(source.read_text(encoding="utf-8", errors="replace"))
                fallback = self._inject_missing_stubs(fallback, source)
                target.write_text(fallback, encoding="utf-8")
                return target, f"{self.compiler} preprocessing failed for {source.name}; used internal cleanup. {result.stderr.strip()[:240]}"
            except Exception as exc:
                fallback = self._fallback(source.read_text(encoding="utf-8", errors="replace"))
                fallback = self._inject_missing_stubs(fallback, source)
                target.write_text(fallback, encoding="utf-8")
                return target, f"{self.compiler} preprocessing error for {source.name}; used internal cleanup. {exc}"

        fallback = self._fallback(source.read_text(encoding="utf-8", errors="replace"))
        fallback = self._inject_missing_stubs(fallback, source)
        target.write_text(fallback, encoding="utf-8")
        return target, f"{self.compiler} not found; used internal cleanup for {source.name}"

    def _inject_missing_stubs(self, text: str, source: Path) -> str:
        """Detect missing symbols and inject stubs if available from stub folder.
        
        Attempts to find missing function/variable definitions in the stub directory
        and injects their stubs into the preprocessed output.
        """
        if not self.stub_dir or not self.stub_dir.exists():
            return text
        
        stub_manifest = self.stub_dir / "stub" / "src" / "testing_mate_stub_manifest.json"
        if not stub_manifest.exists():
            return text
        
        try:
            manifest_data = json.loads(stub_manifest.read_text(encoding="utf-8"))
            stub_symbols = {sym["name"]: sym for sym in manifest_data.get("symbols", [])}
        except Exception:
            return text
        
        # Extract all referenced identifiers from the text
        referenced = self._extract_identifiers(text)
        
        # Find which symbols are referenced but not defined
        defined = self._extract_defined_symbols(text)
        missing = referenced - defined
        
        # Filter to only symbols we have stubs for
        available_stubs = {name: sym for name, sym in stub_symbols.items() if name in missing}
        
        if not available_stubs:
            return text
        
        # Create stub declarations for missing symbols
        stub_declarations = self._generate_stub_declarations(available_stubs)
        
        # Inject at the beginning of the file (after any preserved directives)
        lines = text.split("\n")
        insert_index = 0
        for i, line in enumerate(lines):
            if line.strip() and not line.strip().startswith("#"):
                insert_index = i
                break
        
        result_lines = lines[:insert_index] + ["", "/* TestingMate: Injected stub declarations */"] + stub_declarations + [""] + lines[insert_index:]
        return "\n".join(result_lines)

    @staticmethod
    def _extract_identifiers(text: str) -> set[str]:
        """Extract all identifiers (potential symbol references) from code."""
        # Match valid C identifiers (function calls, variable references)
        pattern = r"\b([A-Za-z_]\w*)\s*(?:\(|;|=|\+|-|\*|/|%|&|\||\^|!|<|>|,|\.|-[>])"
        identifiers = set()
        for match in re.finditer(pattern, text):
            identifiers.add(match.group(1))
        return identifiers

    @staticmethod
    def _extract_defined_symbols(text: str) -> set[str]:
        """Extract all symbols that are defined (functions and variables)."""
        defined = set()
        
        # Find function definitions
        func_pattern = r"(?:^|\n)\s*(?:static\s+)?(?:inline\s+)?(?:\w+[\s\*]*)+\s+([A-Za-z_]\w*)\s*\("
        for match in re.finditer(func_pattern, text, re.MULTILINE):
            defined.add(match.group(1))
        
        # Find variable declarations
        var_pattern = r"(?:^|\n)\s*(?:static\s+)?(?:const\s+)?(?:volatile\s+)?(?:\w+[\s\*]*)+\s+([A-Za-z_]\w*)\s*(?:;|=|,)"
        for match in re.finditer(var_pattern, text, re.MULTILINE):
            defined.add(match.group(1))
        
        return defined

    @staticmethod
    def _generate_stub_declarations(stub_symbols: dict[str, dict]) -> list[str]:
        """Generate stub declarations for missing symbols."""
        declarations = []
        
        for name, sym in sorted(stub_symbols.items()):
            return_type = sym.get("return_type", "int").strip()
            params = sym.get("params", "").strip()
            
            # Generate a simple stub function or variable
            if "(" in params or params == "":
                # It's a function-like symbol
                param_list = params if params else "void"
                declarations.append(f"{return_type} {name}({param_list}) {{ return 0; }}")
            else:
                # It might be a variable
                declarations.append(f"extern {return_type} {name};")
        
        return declarations

    def retrieve_and_copy_stub_headers(self, include_name: str, dest_dir: Path) -> bool:
        """Retrieve a header file from stub folder and copy to destination.
        
        Args:
            include_name: Name or path of the include file (e.g., 'config.h' or 'sys/types.h')
            dest_dir: Destination directory
            
        Returns:
            True if successfully copied, False otherwise
        """
        if not self.stub_dir or not self.stub_dir.exists():
            return False
        
        stub_include = self.stub_dir / "stub" / "include"
        if not stub_include.exists():
            return False
        
        # Search for the header file recursively
        for candidate in stub_include.rglob("*"):
            if candidate.is_file() and candidate.name == include_name.split("/")[-1]:
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    rel_path = candidate.relative_to(stub_include)
                    dest_file = dest_dir / rel_path
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate, dest_file)
                    return True
                except (OSError, ValueError):
                    continue
        
        return False

    @staticmethod
    def _fallback(text: str) -> str:
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"//.*", "", text)
        text = re.sub(r"^\s*#\s*include.*$", "", text, flags=re.M)
        text = re.sub(r"^\s*#\s*(pragma|line).*$", "", text, flags=re.M)
        simple_defines: dict[str, str] = {}
        for match in re.finditer(r"^\s*#\s*define\s+([A-Za-z_]\w*)\s+([^\n()/]+)\s*$", text, flags=re.M):
            simple_defines[match.group(1)] = match.group(2).strip()
        text = re.sub(r"^\s*#.*$", "", text, flags=re.M)
        for name, value in simple_defines.items():
            text = re.sub(rf"\b{re.escape(name)}\b", value, text)
        return text


class CAnalyzer:
    # Improved function header regex: handles static inline, extern, __attribute__, complex return types
    function_header = re.compile(
        r"(?P<rtype>(?:extern|static|inline|__attribute__\s*\([^)]*\)|\*|const|volatile|\w|\s)+?)\s+"
        r"(?P<name>[A-Za-z_]\w*)\s*"
        r"\((?P<params>[^;{}]*)\)\s*\{",
        re.M,
    )

    def analyze(self, source: Path, include_static_functions: bool = False, include_main: bool = False) -> list[FunctionInfo]:
        text = source.read_text(encoding="utf-8", errors="replace")
        enum_types = self._enum_types(text)
        line_starts = self._line_offsets(text)
        functions: list[FunctionInfo] = []
        for match in self.function_header.finditer(text):
            name = match.group("name")
            return_type = " ".join(match.group("rtype").split())
            # Normalize return type: remove redundant attributes
            return_type = re.sub(r"__attribute__\s*\([^)]*\)", "", return_type).strip()
            return_type = " ".join(return_type.split())
            
            # Skip keywords, compiler builtins, and prototypes/typedefs
            if name in C_KEYWORDS or name.startswith("__") or name.startswith("_"):
                continue
            if name == "main" and not include_main:
                continue
            if return_type.strip().startswith("static") and not include_static_functions:
                continue
            
            body_start = match.end() - 1
            body_end = self._matching_brace(text, body_start)
            if body_end == -1:
                continue
            body = text[body_start + 1 : body_end]
            start_line = self._line_for_offset(line_starts, match.start())
            end_line = self._line_for_offset(line_starts, body_end)
            fn = FunctionInfo(
                name=name,
                return_type=return_type,
                parameters=self._parse_parameters(match.group("params"), enum_types),
                start_line=start_line,
                end_line=end_line,
                body=body,
            )
            fn.conditions = self._conditions(body, start_line)
            functions.append(fn)
        return functions

    @staticmethod
    def _line_offsets(text: str) -> list[int]:
        offsets = [0]
        for match in re.finditer(r"\n", text):
            offsets.append(match.end())
        return offsets

    @staticmethod
    def _line_for_offset(offsets: list[int], offset: int) -> int:
        lo, hi = 0, len(offsets)
        while lo < hi:
            mid = (lo + hi) // 2
            if offsets[mid] <= offset:
                lo = mid + 1
            else:
                hi = mid
        return lo

    @staticmethod
    def _matching_brace(text: str, start: int) -> int:
        depth = 0
        i = start
        in_string = False
        in_char = False
        escape = False
        while i < len(text):
            ch = text[i]
            if escape:
                escape = False
            elif ch == "\\" and (in_string or in_char):
                escape = True
            elif ch == '"' and not in_char:
                in_string = not in_string
            elif ch == "'" and not in_string:
                in_char = not in_char
            elif not in_string and not in_char:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return i
            i += 1
        return -1

    @staticmethod
    def _split_top_level(text: str, separator: str = ",") -> list[str]:
        parts: list[str] = []
        depth = 0
        current: list[str] = []
        for ch in text:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            if ch == separator and depth == 0:
                parts.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        tail = "".join(current).strip()
        if tail:
            parts.append(tail)
        return parts

    def _parse_parameters(self, params: str, enum_types: dict[str, list[str]] | None = None) -> list[Parameter]:
        """Parse function parameters robustly, handling function pointers, arrays, and nested parentheses."""
        params = params.strip()
        if not params or params == "void":
            return []
        enum_types = enum_types or {}
        result: list[Parameter] = []
        
        # Split parameters at top-level commas only (respecting nested parentheses/brackets)
        for raw in self._split_top_level(params):
            clean = " ".join(raw.replace("\n", " ").split())
            # Remove default values
            clean = re.sub(r"\s*=\s*.*$", "", clean)
            
            # Handle function pointer parameters: type (*name)(...)
            func_ptr_match = re.search(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)\s*\([^)]*\)", clean)
            if func_ptr_match:
                name = func_ptr_match.group(1)
                result.append(Parameter(name=name, ctype="function_pointer", kind="pointer", enum_values=[]))
                continue
            
            # Extract parameter name (handle pointers and arrays)
            name_match = re.search(r"(\*?\s*[A-Za-z_]\w*)(\s*\[[^\]]*\])?\s*$", clean)
            if not name_match:
                continue
            
            # Get parameter name and extract type
            name = name_match.group(1).replace("*", "").strip()
            ctype = clean[: name_match.start(1)].strip()
            
            # Add pointer indicator to ctype if needed
            if "*" in clean[: name_match.end(1)]:
                ctype = f"{ctype} *".strip()
            
            # Determine parameter kind (pointer/array/scalar)
            kind = "pointer" if "*" in clean or "[" in clean else "scalar"
            
            # Get enum values if applicable
            enum_values = self._enum_values_for_type(ctype, enum_types)
            result.append(Parameter(name=name, ctype=ctype or "int", kind=kind, enum_values=enum_values))
        
        return result

    @classmethod
    def _enum_types(cls, text: str) -> dict[str, list[str]]:
        enum_types: dict[str, list[str]] = {}
        clean = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        clean = re.sub(r"//.*", "", clean)
        typedef_pattern = re.compile(
            r"typedef\s+enum\s*(?P<tag>[A-Za-z_]\w*)?\s*\{(?P<body>.*?)\}\s*(?P<alias>[A-Za-z_]\w*)\s*;",
            re.S,
        )
        for match in typedef_pattern.finditer(clean):
            values = cls._enum_constants(match.group("body"))
            if not values:
                continue
            alias = match.group("alias")
            enum_types[alias] = values
            tag = match.group("tag")
            if tag:
                enum_types[tag] = values
                enum_types[f"enum {tag}"] = values
        enum_pattern = re.compile(r"enum\s+(?P<tag>[A-Za-z_]\w*)\s*\{(?P<body>.*?)\}\s*;", re.S)
        for match in enum_pattern.finditer(clean):
            values = cls._enum_constants(match.group("body"))
            if values:
                enum_types[match.group("tag")] = values
                enum_types[f"enum {match.group('tag')}"] = values
        return enum_types

    @staticmethod
    def _enum_constants(body: str) -> list[str]:
        values: list[str] = []
        for item in CAnalyzer._split_top_level(body):
            token = item.split("=", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", token):
                values.append(token)
        return values

    @staticmethod
    def _enum_values_for_type(ctype: str, enum_types: dict[str, list[str]]) -> list[str]:
        normalized = re.sub(r"\b(const|volatile|static|extern)\b", " ", ctype)
        normalized = re.sub(r"\s+", " ", normalized.replace("*", " ")).strip()
        candidates = [normalized]
        if normalized.startswith("enum "):
            candidates.append(normalized[5:].strip())
        for candidate in candidates:
            values = enum_types.get(candidate)
            if values:
                return values
        return []

    def _conditions(self, body: str, start_line: int) -> list[Condition]:
        """Extract conditions from function body, handling if/while/for/switch statements."""
        conditions: list[Condition] = []
        
        # Handle if/while/for statements
        for match in re.finditer(r"\b(if|while|for)\s*\(", body):
            expr, end = self._extract_parenthesized(body, match.end() - 1)
            if not expr:
                continue
            
            # For for-loops, extract the condition part (middle)
            if match.group(1) == "for":
                pieces = self._split_top_level(expr, ";")
                expr = pieces[1].strip() if len(pieces) == 3 else expr
            
            line = start_line + body[: match.start()].count("\n")
            depth = self._block_depth(body, match.start())
            
            # Split decisions by && and ||, stripping wrapping parentheses from each
            decisions = [
                self._strip_wrapping_parentheses(piece.strip())
                for piece in re.split(r"&&|\|\|", expr)
                if piece.strip()
            ]
            decisions = [d for d in decisions if d]  # Remove empty decisions
            
            conditions.append(
                Condition(
                    text=expr.strip(),
                    line=line,
                    decisions=decisions or [expr.strip()],
                    depth=depth
                )
            )
        
        # Handle switch statements
        for match in re.finditer(r"\bswitch\s*\(", body):
            expr, _ = self._extract_parenthesized(body, match.end() - 1)
            if expr:
                line = start_line + body[: match.start()].count("\n")
                depth = self._block_depth(body, match.start())
                conditions.append(
                    Condition(
                        text=f"switch({expr.strip()})",
                        line=line,
                        decisions=[expr.strip()],
                        depth=depth
                    )
                )
        
        return conditions

    @staticmethod
    def _strip_wrapping_parentheses(text: str) -> str:
        """Unified helper to strip outer parentheses that semantically wrap the entire expression.
        
        Used consistently by detection and generation phases to avoid semantic mismatches.
        """
        clean = text.strip()
        changed = True
        while changed and clean.startswith("(") and clean.endswith(")"):
            changed = False
            depth = 0
            balanced_outer = True
            for index, ch in enumerate(clean):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0 and index != len(clean) - 1:
                        balanced_outer = False
                        break
            if balanced_outer:
                clean = clean[1:-1].strip()
                changed = True
        return clean
    
    @classmethod
    def _clean_decision_token(cls, text: str) -> str:
        """Clean a decision token by normalizing whitespace and stripping wrapping parentheses."""
        clean = text.strip()
        while clean.startswith("(") and clean.count("(") > clean.count(")"):
            clean = clean[1:].strip()
        while clean.endswith(")") and clean.count(")") > clean.count("("):
            clean = clean[:-1].strip()
        return cls._strip_wrapping_parentheses(clean)

    @staticmethod
    def _block_depth(text: str, offset: int) -> int:
        prefix = text[:offset]
        return max(0, prefix.count("{") - prefix.count("}"))

    @staticmethod
    def _extract_parenthesized(text: str, open_index: int) -> tuple[str, int]:
        depth = 0
        current: list[str] = []
        for idx in range(open_index, len(text)):
            ch = text[idx]
            if ch == "(":
                depth += 1
                if depth == 1:
                    continue
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return "".join(current), idx
            if depth >= 1:
                current.append(ch)
        return "", -1
