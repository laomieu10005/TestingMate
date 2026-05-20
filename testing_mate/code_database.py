from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from testing_mate.constants import C_KEYWORDS
from testing_mate.models import FunctionInfo, TestCase


@dataclass
class CodeFunctionRecord:
    name: str
    return_type: str
    parameters: list[dict]
    start_line: int
    end_line: int
    conditions: list[dict]
    body_excerpt: str
    calls: list[str] = field(default_factory=list)
    external_calls: list[str] = field(default_factory=list)
    local_variables: list[str] = field(default_factory=list)
    globals_read: list[str] = field(default_factory=list)
    globals_written: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    return_statements: list[dict] = field(default_factory=list)
    execution_paths: list[dict] = field(default_factory=list)
    data_dependencies: list[dict] = field(default_factory=list)
    mock_requirements: list[dict] = field(default_factory=list)
    pointer_fields: dict[str, str] = field(default_factory=dict)
    data_flow_graph: dict = field(default_factory=dict)


@dataclass
class CodeDatabase:
    version: str
    source_file: str
    source_path: str
    analysis_source_path: str
    analyzer: str
    generated_at: str
    functions: list[CodeFunctionRecord]

    @classmethod
    def from_functions(
        cls,
        source: Path,
        analysis_source: Path,
        functions: list[FunctionInfo],
        analyzer: str = "clang_enhanced",
    ) -> "CodeDatabase":
        function_names = {fn.name for fn in functions}
        records = []
        for fn in functions:
            calls = function_calls(fn, function_names)
            external = external_calls(fn, function_names)
            locals_ = local_variables(fn)
            globals_written = global_writes(fn, locals_, function_names)
            globals_read = global_reads(fn, locals_, globals_written, function_names)
            returns = return_statements(fn)
            paths = execution_paths(fn, function_names, max_paths=16)
            records.append(CodeFunctionRecord(
                name=fn.name,
                return_type=fn.return_type,
                parameters=[asdict(param) for param in fn.parameters],
                start_line=fn.start_line,
                end_line=fn.end_line,
                conditions=[asdict(condition) for condition in fn.conditions],
                body_excerpt=body_excerpt(fn.body),
                calls=calls,
                external_calls=external,
                local_variables=locals_,
                globals_read=globals_read,
                globals_written=globals_written,
                branches=[condition.text for condition in fn.conditions],
                return_statements=returns,
                execution_paths=paths,
                data_dependencies=data_dependencies(fn, locals_, function_names),
                mock_requirements=mock_requirements(external),
                pointer_fields=pointer_field_reads(fn),
                data_flow_graph=data_flow_graph(fn, function_names),
            ))
        return cls(
            version="1.0",
            source_file=source.name,
            source_path=str(source),
            analysis_source_path=str(analysis_source),
            analyzer=analyzer,
            generated_at=datetime.now().isoformat(timespec="seconds"),
            functions=records,
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "source_file": self.source_file,
            "source_path": self.source_path,
            "analysis_source_path": self.analysis_source_path,
            "analyzer": self.analyzer,
            "generated_at": self.generated_at,
            "functions": [asdict(function) for function in self.functions],
        }

    def function_chunk(self, function_name: str, existing_cases: list[TestCase] | None = None) -> dict:
        records = [record for record in self.functions if record.name == function_name]
        payload = {
            "source_file": self.source_file,
            "analyzer": self.analyzer,
            "functions": [asdict(record) for record in records],
        }
        if existing_cases is not None:
            payload["recent_engine_cases"] = [
                {
                    "test_id": case.test_id,
                    "coverage": case.coverage,
                    "values": case.values,
                    "rationale": case.rationale[:220],
                }
                for case in existing_cases
                if case.function == function_name
            ][-6:]
        return payload


def body_excerpt(body: str, limit: int = 1600) -> str:
    clean = re.sub(r"\s+", " ", body).strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit] + "..."


def clean_code(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//.*", " ", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
    return text


def function_calls(fn: FunctionInfo, callable_names: set[str] | None = None) -> list[str]:
    callable_names = callable_names or set()
    names: set[str] = set()
    for match in re.finditer(r"(?<![.#>])\b([A-Za-z_]\w*)\s*\(", clean_code(fn.body)):
        name = match.group(1)
        if name == fn.name or name in C_KEYWORDS or name in {"if", "for", "while", "switch", "return", "sizeof"}:
            continue
        names.add(name)
    internal = sorted(name for name in names if name in callable_names)
    external = sorted(name for name in names if name not in callable_names)
    return [*internal, *external]


def internal_calls(fn: FunctionInfo, callable_names: set[str]) -> list[str]:
    return [name for name in function_calls(fn, callable_names) if name in callable_names and name != fn.name]


def external_calls(fn: FunctionInfo, callable_names: set[str]) -> list[str]:
    return [name for name in function_calls(fn, callable_names) if name not in callable_names]


def local_variables(fn: FunctionInfo) -> list[str]:
    names: set[str] = set()
    declaration = re.compile(
        r"\b(?:const\s+|volatile\s+|static\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|"
        r"char\s+|int\s+|float\s+|double\s+|bool\s+|uint\d+_t\s+|int\d+_t\s+|size_t\s+|[A-Za-z_]\w+_t\s+)+"
        r"[*\s]*([A-Za-z_]\w*)\s*(?:=|;|,|\[)"
    )
    for match in declaration.finditer(clean_code(fn.body)):
        name = match.group(1)
        if name not in C_KEYWORDS:
            names.add(name)
    return sorted(names)


def assignment_targets(fn: FunctionInfo) -> list[str]:
    targets: list[str] = []
    text = clean_code(fn.body)
    for match in re.finditer(r"(?<![=!<>])\b([A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*)\s*(?:=|\+=|-=|\*=|/=|%=)", text):
        target = re.sub(r"\s*(?:->|\.)\s*", ".", match.group(1))
        targets.append(target)
    return targets


def global_writes(fn: FunctionInfo, locals_: list[str], callable_names: set[str]) -> list[str]:
    params = {param.name for param in fn.parameters}
    local_set = set(locals_)
    result: set[str] = set()
    for target in assignment_targets(fn):
        root = target.split(".", 1)[0]
        if root not in params and root not in local_set and root not in callable_names and root != fn.name:
            result.add(target)
    return sorted(result)


def identifiers(text: str) -> set[str]:
    return {match.group(0) for match in re.finditer(r"\b[A-Za-z_]\w*\b", clean_code(text))}


def global_reads(fn: FunctionInfo, locals_: list[str], globals_written: list[str], callable_names: set[str]) -> list[str]:
    params = {param.name for param in fn.parameters}
    local_set = set(locals_)
    calls = set(function_calls(fn, callable_names))
    written_roots = {item.split(".", 1)[0] for item in globals_written}
    result: set[str] = set()
    for name in identifiers(fn.body):
        if name in C_KEYWORDS or name in params or name in local_set or name in calls or name in callable_names or name == fn.name:
            continue
        if name.startswith("__") or name[0].isdigit():
            continue
        if name in written_roots:
            continue
        result.add(name)
    return sorted(result)


def return_statements(fn: FunctionInfo) -> list[dict]:
    returns: list[dict] = []
    for index, match in enumerate(re.finditer(r"\breturn\s+([^;]*);", fn.body), start=1):
        expr = " ".join(match.group(1).split()) or "void"
        line = fn.start_line + fn.body[: match.start()].count("\n")
        returns.append({"id": f"R{index}", "line": line, "expression": expr})
    return returns


def invert_condition_text(text: str) -> str:
    clean = text.strip()
    replacements = [
        (r"(.+?)\s*==\s*(.+)", r"\1 != \2"),
        (r"(.+?)\s*!=\s*(.+)", r"\1 == \2"),
        (r"(.+?)\s*>\s*(.+)", r"\1 <= \2"),
        (r"(.+?)\s*<\s*(.+)", r"\1 >= \2"),
        (r"(.+?)\s*>=\s*(.+)", r"\1 < \2"),
        (r"(.+?)\s*<=\s*(.+)", r"\1 > \2"),
    ]
    for pattern, replacement in replacements:
        if re.fullmatch(pattern, clean):
            return re.sub(pattern, replacement, clean).strip()
    if clean.startswith("!"):
        return clean[1:].strip()
    return f"!({clean})"


def path_constraints_to_text(constraints: list[dict]) -> list[str]:
    result: list[str] = []
    for item in constraints:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        result.append(text if item.get("truth", True) else invert_condition_text(text))
    return result


def execution_paths(fn: FunctionInfo, callable_names: set[str], max_paths: int = 16) -> list[dict]:
    paths: list[dict] = []
    returns = return_statements(fn)
    calls = function_calls(fn, callable_names)
    writes = global_writes(fn, local_variables(fn), callable_names)
    ordered_conditions = sorted(fn.conditions, key=lambda condition: (condition.line, condition.depth))
    previous_conditions: list[dict] = []

    for index, ret in enumerate(returns, start=1):
        return_line = int(ret.get("line") or fn.start_line)
        relevant = [condition for condition in ordered_conditions if condition.line <= return_line]
        constraints: list[dict] = []
        for condition in relevant:
            truth = True
            if condition.depth == 0 and index > 1 and condition not in relevant[-2:]:
                truth = False
            constraints.append({"text": condition.text, "truth": truth, "line": condition.line})
        if not constraints and previous_conditions:
            constraints = list(previous_conditions)
        previous_conditions = constraints
        path_calls = calls_before_line(fn, calls, return_line)
        path_writes = [item for item in writes if item in fn.body]
        paths.append(
            {
                "id": f"P{index}",
                "kind": "return",
                "priority": path_priority(constraints, path_calls, path_writes, str(ret.get("expression") or "")),
                "conditions": path_constraints_to_text(constraints),
                "constraints": constraints,
                "expected_calls": path_calls,
                "expected_global_writes": path_writes,
                "expected_return": ret.get("expression"),
                "return_line": return_line,
            }
        )

    if returns and ordered_conditions:
        final_return = returns[-1]
        final_line = int(final_return.get("line") or 0)
        paths = [
            path for path in paths
            if not (
                path.get("return_line") == final_line
                and path.get("expected_return") == final_return.get("expression")
                and path.get("constraints")
            )
        ]
        paths.extend(fallthrough_return_paths(fn, ordered_conditions, final_return, callable_names))

    for condition in ordered_conditions:
        for truth in (True, False):
            constraints = [{"text": condition.text, "truth": truth, "line": condition.line}]
            if any(path.get("conditions") == path_constraints_to_text(constraints) for path in paths):
                continue
            paths.append(
                {
                    "id": f"P{len(paths) + 1}",
                    "kind": "branch",
                    "priority": path_priority(constraints, [], [], ""),
                    "conditions": path_constraints_to_text(constraints),
                    "constraints": constraints,
                    "expected_calls": [],
                    "expected_global_writes": [],
                    "expected_return": None,
                    "return_line": None,
                }
            )

    if not paths:
        paths.append(
            {
                "id": "P1",
                "kind": "fallthrough",
                "priority": 1,
                "conditions": [],
                "constraints": [],
                "expected_calls": calls,
                "expected_global_writes": writes,
                "expected_return": returns[0]["expression"] if returns else None,
                "return_line": returns[0]["line"] if returns else None,
            }
        )
    return sorted(paths, key=lambda path: (-int(path.get("priority") or 0), str(path.get("id"))))[:max_paths]


def fallthrough_return_paths(fn: FunctionInfo, conditions: list, final_return: dict, callable_names: set[str]) -> list[dict]:
    if len(conditions) < 2:
        return []
    paths: list[dict] = []
    calls = function_calls(fn, callable_names)
    writes = global_writes(fn, local_variables(fn), callable_names)
    for index, condition in enumerate(conditions[1:], start=2):
        constraints: list[dict] = []
        parent = None
        if condition.depth > 0:
            for candidate in reversed(conditions[: index - 1]):
                if candidate.depth == condition.depth - 1:
                    parent = candidate
                    break
        for previous in conditions[: index - 1]:
            if parent is not None and previous is parent:
                constraints.append({"text": previous.text, "truth": True, "line": previous.line})
            elif previous.depth == 0 and condition_true_returns_before(fn, previous, condition.line):
                constraints.append({"text": previous.text, "truth": False, "line": previous.line})
        constraints.append({"text": condition.text, "truth": False, "line": condition.line})
        paths.append(
            {
                "id": f"P_final_{index}",
                "kind": "fallthrough-return",
                "priority": path_priority(constraints, [], writes, str(final_return.get("expression") or "")),
                "conditions": path_constraints_to_text(constraints),
                "constraints": constraints,
                "expected_calls": [],
                "expected_global_writes": writes,
                "expected_return": final_return.get("expression"),
                "return_line": final_return.get("line"),
            }
        )
    if not paths and calls:
        paths.append(
            {
                "id": "P_final",
                "kind": "fallthrough-return",
                "priority": path_priority([], calls, writes, str(final_return.get("expression") or "")),
                "conditions": [],
                "constraints": [],
                "expected_calls": calls,
                "expected_global_writes": writes,
                "expected_return": final_return.get("expression"),
                "return_line": final_return.get("line"),
            }
        )
    return paths


def condition_true_returns_before(fn: FunctionInfo, condition, next_line: int) -> bool:
    start = max(0, condition.line - fn.start_line)
    end = max(start + 1, next_line - fn.start_line)
    lines = fn.body.splitlines()
    text = "\n".join(lines[start:end])
    return bool(re.search(r"\breturn\b", text))


def calls_before_line(fn: FunctionInfo, calls: list[str], line: int) -> list[str]:
    result: list[str] = []
    for call in calls:
        for match in re.finditer(rf"\b{re.escape(call)}\s*\(", clean_code(fn.body)):
            call_line = fn.start_line + fn.body[: match.start()].count("\n")
            if call_line <= line and call not in result:
                result.append(call)
    return result


def path_priority(constraints: list[dict], calls: list[str], writes: list[str], expected_return: str) -> int:
    priority = 1 + min(len(constraints), 4)
    if calls:
        priority += 4
    if writes:
        priority += 3
    if expected_return.strip() not in {"", "0", "void", "true", "TRUE"}:
        priority += 2
    if any(not item.get("truth", True) for item in constraints):
        priority += 1
    return priority


def data_dependencies(fn: FunctionInfo, locals_: list[str], callable_names: set[str]) -> list[dict]:
    deps: list[dict] = []
    params = {param.name for param in fn.parameters}
    assignments = list(re.finditer(r"\b([A-Za-z_]\w*)\s*=\s*([^;]+);", clean_code(fn.body)))
    assigned_from_param: dict[str, set[str]] = {}
    for match in assignments:
        target, expr = match.group(1), match.group(2)
        for param in params:
            if re.search(rf"\b{re.escape(param)}\b", expr):
                assigned_from_param.setdefault(target, set()).add(param)
                deps.append({"from": param, "to": target, "kind": "definition", "expression": " ".join(expr.split())})
    for condition in fn.conditions:
        for param in params:
            if re.search(rf"\b{re.escape(param)}\b", condition.text):
                deps.append({"from": param, "to": condition.text, "kind": "branch", "line": condition.line})
    for ret in return_statements(fn):
        expr = str(ret.get("expression") or "")
        for param in params:
            if re.search(rf"\b{re.escape(param)}\b", expr):
                deps.append({"from": param, "to": "return", "kind": "return", "expression": expr, "line": ret.get("line")})
        for local in locals_:
            if re.search(rf"\b{re.escape(local)}\b", expr):
                for param in sorted(assigned_from_param.get(local, set())):
                    deps.append({"from": param, "via": local, "to": "return", "kind": "def-use-return", "expression": expr, "line": ret.get("line")})
    for call in function_calls(fn, callable_names):
        for param in params:
            pattern = rf"\b{re.escape(call)}\s*\([^;)]*\b{re.escape(param)}\b"
            if re.search(pattern, fn.body):
                deps.append({"from": param, "to": call, "kind": "call_argument"})
    return deps


def mock_requirements(calls: list[str]) -> list[dict]:
    return [
        {
            "function": call,
            "reason": "external call requires reviewed mock/stub behavior for isolated unit testing",
            "default_return": 0,
        }
        for call in calls
    ]


def pointer_field_reads(fn: FunctionInfo) -> dict[str, str]:
    fields: dict[str, str] = {}
    for param in fn.parameters:
        if param.kind != "pointer":
            continue
        pattern = rf"(?<![&.])\b{re.escape(param.name)}\s*->\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\b"
        for field in sorted(set(re.findall(pattern, fn.body))):
            clean = re.sub(r"\s+", "", field)
            fields[f"{param.name}.{clean}"] = "0"
    return fields


def data_flow_graph(fn: FunctionInfo, callable_names: set[str]) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_nodes: set[str] = set()

    def add_node(node_id: str, kind: str, label: str, **extra: str | int) -> None:
        if node_id in seen_nodes:
            return
        seen_nodes.add(node_id)
        nodes.append({"id": node_id, "kind": kind, "label": label, **extra})

    add_node(f"function:{fn.name}", "function", fn.name, line=fn.start_line)
    for param in fn.parameters:
        add_node(f"param:{param.name}", "parameter", param.name, ctype=param.ctype, param_kind=param.kind)
        edges.append({"from": f"param:{param.name}", "to": f"function:{fn.name}", "kind": "input"})

    for index, condition in enumerate(fn.conditions, start=1):
        condition_id = f"condition:{index}"
        add_node(condition_id, "condition", condition.text, line=condition.line)
        edges.append({"from": f"function:{fn.name}", "to": condition_id, "kind": "control"})
        for decision in condition.decisions or [condition.text]:
            for param in fn.parameters:
                if re.search(rf"\b{re.escape(param.name)}\b", decision):
                    edges.append({"from": f"param:{param.name}", "to": condition_id, "kind": "decision_read"})

    for call in internal_calls(fn, callable_names):
        call_id = f"call:{call}"
        add_node(call_id, "call", call)
        edges.append({"from": f"function:{fn.name}", "to": call_id, "kind": "calls"})

    for key in pointer_field_reads(fn):
        field_id = f"field:{key}"
        add_node(field_id, "pointer_field", key)
        param_name = key.split(".", 1)[0]
        edges.append({"from": f"param:{param_name}", "to": field_id, "kind": "field_read"})

    for match in re.finditer(r"\b([A-Za-z_]\w*(?:->|\.)[A-Za-z_]\w*)\s*=", fn.body):
        target = re.sub(r"\s*(?:->|\.)\s*", ".", match.group(1))
        target_id = f"write:{target}"
        add_node(target_id, "write", target)
        edges.append({"from": f"function:{fn.name}", "to": target_id, "kind": "writes"})

    for match in re.finditer(r"\breturn\s+([^;]+);", fn.body):
        return_id = f"return:{len([node for node in nodes if node['kind'] == 'return']) + 1}"
        expression = " ".join(match.group(1).split())
        add_node(return_id, "return", expression)
        edges.append({"from": f"function:{fn.name}", "to": return_id, "kind": "returns"})
        for param in fn.parameters:
            if re.search(rf"\b{re.escape(param.name)}\b", expression):
                edges.append({"from": f"param:{param.name}", "to": return_id, "kind": "return_read"})

    return {"nodes": nodes, "edges": edges}
