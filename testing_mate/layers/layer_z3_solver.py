from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from testing_mate.models import CoverageFeedback, FunctionInfo
from testing_mate.vector_generator import TestVectorGenerator


def run_z3_solver(fn: FunctionInfo, pruned_cfg: Any, cov_db: Any) -> Tuple[List[Dict[str, Any]], List[Any]]:
    """Solve scalar decision targets with Z3 and report genuinely unsolved targets.

    The layer is intentionally dependency-tolerant: when z3-solver is absent or a
    condition is outside the scalar fragment, it falls back to Testing Mate's local
    decision driver and passes unsolved targets to later layers.
    """
    generator = TestVectorGenerator()
    tests: list[dict[str, Any]] = []
    unsolvable: list[Any] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    targets = _decision_targets(fn, cov_db, pruned_cfg)

    for target in targets:
        decision = str(target.get("decision") or "")
        desired = bool(target.get("desired", True))
        if not decision:
            continue
        vector = _solve_with_z3(fn, decision, desired)
        if vector is None:
            vector = _drive_without_z3(fn, decision, desired)
        if not vector:
            unsolvable.append(target)
            continue
        signature = tuple(sorted((str(key), str(value)) for key, value in vector.items()))
        if signature in seen:
            continue
        seen.add(signature)
        safe_decision = re.sub(r"[^A-Za-z0-9]+", "_", decision).strip("_")[:40] or "decision"
        tests.append(
            {
                "test_id": f"Z3_{fn.name}_{len(tests) + 1:04d}",
                "function": fn.name,
                "coverage": str(target.get("metric") or "C0/C1"),
                "values": vector,
                "description": f"Z3 scalar solve for {decision} -> {desired}",
                "rationale": f"Z3 layer targeted {safe_decision} as {desired}.",
                "__layer": "z3",
                "__target_line": str(target.get("line") or ""),
            }
        )

    return tests, unsolvable


def _decision_targets(fn: FunctionInfo, cov_db: Any, pruned_cfg: Any) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    feedback = cov_db if isinstance(cov_db, CoverageFeedback) else None
    uncovered_items = list(getattr(feedback, "uncovered_items", []) or [])
    for item in uncovered_items:
        line = _line_from_text(item)
        desired = _desired_from_text(item)
        condition = _condition_for_line(fn, line, item)
        if condition is None:
            continue
        decisions = TestVectorGenerator._condition_decisions(condition)
        target_decision = _quoted_decision(item)
        if target_decision:
            decisions = [target_decision, *[decision for decision in decisions if decision != target_decision]]
        for decision in decisions:
            targets.append({"decision": decision, "desired": desired, "line": line, "metric": _metric_from_text(item)})

    if not targets:
        for condition in getattr(fn, "conditions", []) or []:
            for decision in TestVectorGenerator._condition_decisions(condition):
                targets.append({"decision": decision, "desired": True, "line": condition.line, "metric": "C1"})
                targets.append({"decision": decision, "desired": False, "line": condition.line, "metric": "C1"})

    for edge in _cfg_edges(pruned_cfg):
        decision = str(edge.get("cond_text") or edge.get("condition") or "").strip()
        if decision:
            targets.append({"decision": decision, "desired": edge.get("kind") != "false_branch", "line": _edge_line(edge), "metric": "C1"})

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, bool, int | None]] = set()
    for target in targets:
        key = (str(target.get("decision") or ""), bool(target.get("desired", True)), target.get("line"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique[:80]


def _solve_with_z3(fn: FunctionInfo, decision: str, desired: bool) -> dict[str, str] | None:
    try:
        import z3  # type: ignore
    except Exception:
        return None

    base = TestVectorGenerator()._base_vector_for(fn)
    variables: dict[str, Any] = {}
    solver = z3.Solver()
    for param in getattr(fn, "parameters", []) or []:
        if param.kind == "pointer":
            continue
        if param.enum_values:
            var = z3.Int(param.name)
            solver.add(var >= 0, var < len(param.enum_values))
        else:
            var = z3.Int(param.name)
            lower, upper = _bounds_for_ctype(param.ctype)
            if lower is not None:
                solver.add(var >= lower)
            if upper is not None:
                solver.add(var <= upper)
        variables[param.name] = var

    expr = _atom_to_z3(decision, variables, fn, z3)
    if expr is None:
        return None
    solver.add(expr if desired else z3.Not(expr))
    if solver.check() != z3.sat:
        return None

    model = solver.model()
    vector = dict(base)
    for param in getattr(fn, "parameters", []) or []:
        var = variables.get(param.name)
        if var is None:
            continue
        value = model.eval(var, model_completion=True)
        try:
            numeric = int(value.as_long())
        except Exception:
            continue
        if param.enum_values:
            numeric = max(0, min(len(param.enum_values) - 1, numeric))
            vector[param.name] = param.enum_values[numeric]
        elif _is_boolean_type(param.ctype):
            vector[param.name] = "TRUE" if numeric != 0 else "FALSE"
        else:
            vector[param.name] = str(numeric)
    return vector


def _drive_without_z3(fn: FunctionInfo, decision: str, desired: bool) -> dict[str, str] | None:
    generator = TestVectorGenerator()
    vector = generator._base_vector_for(fn)
    before = dict(vector)
    generator._drive_decision(vector, decision, desired)
    return vector if vector != before else None


def _atom_to_z3(decision: str, variables: dict[str, Any], fn: FunctionInfo, z3: Any) -> Any:
    clean = TestVectorGenerator._strip_wrapping_parentheses(str(decision or "").strip())
    clean = re.sub(r"\([A-Za-z_]\w+\)\s*", "", clean)
    negated = re.fullmatch(r"!\s*([A-Za-z_]\w*)", clean)
    if negated and negated.group(1) in variables:
        return variables[negated.group(1)] == 0
    bare = re.fullmatch(r"([A-Za-z_]\w*)", clean)
    if bare and bare.group(1) in variables:
        return variables[bare.group(1)] != 0

    operand = r"(?:[A-Za-z_]\w*|[-+]?\d+(?:\.\d+)?[uUlL]*|TRUE|FALSE|true|false)"
    match = re.fullmatch(rf"\s*(?P<left>{operand})\s*(?P<op>==|!=|>=|<=|>|<)\s*(?P<right>{operand})\s*", clean)
    if not match:
        return None
    left = _operand_to_z3(match.group("left"), variables, fn)
    right = _operand_to_z3(match.group("right"), variables, fn)
    if left is None or right is None:
        return None
    op = match.group("op")
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    return None


def _operand_to_z3(token: str, variables: dict[str, Any], fn: FunctionInfo) -> Any:
    clean = str(token).strip()
    if clean in variables:
        return variables[clean]
    upper = clean.upper()
    if upper == "TRUE":
        return 1
    if upper == "FALSE":
        return 0
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?[uUlL]*", clean):
        return int(float(re.sub(r"[uUlL]+$", "", clean)))
    for param in getattr(fn, "parameters", []) or []:
        if clean in param.enum_values:
            return param.enum_values.index(clean)
    if re.fullmatch(r"[A-Z][A-Za-z0-9_]+", clean):
        return TestVectorGenerator._symbolic_compare_anchor(clean)
    return None


def _bounds_for_ctype(ctype: str) -> tuple[int | None, int | None]:
    text = str(ctype).lower()
    
    # Boolean types: 0-1
    if "bool" in text or "boolean" in text:
        return 0, 1
    
    # Floating-point types: no practical bounds for Z3
    if any(ftype in text for ftype in ("float", "double", "real")):
        return None, None
    
    # Unsigned integer types
    if "uint8" in text:
        return 0, 255
    if "uint16" in text:
        return 0, 65535
    if "uint32" in text:
        return 0, 4294967295
    if "uint64" in text:
        return 0, 18446744073709551615
    
    # Signed integer types
    if "int8" in text or (re.search(r"\bint8_t\b", text)):
        return -128, 127
    if "int16" in text or (re.search(r"\bint16_t\b", text)):
        return -32768, 32767
    if "int32" in text or (re.search(r"\bint32_t\b", text)):
        return -2147483648, 2147483647
    if "int64" in text or (re.search(r"\bint64_t\b", text)):
        return -9223372036854775808, 9223372036854775807
    
    # Generic "signed" without size: use int32 bounds as default
    if "signed" in text and not "unsigned" in text:
        return -2147483648, 2147483647
    
    # Generic "unsigned" without size: use uint32 bounds as default
    if "unsigned" in text:
        return 0, 4294967295
    
    # Default: no constraints
    return None, None


def _is_boolean_type(ctype: str) -> bool:
    text = str(ctype).lower()
    return "bool" in text or "boolean" in text


def _condition_for_line(fn: FunctionInfo, line: int | None, text: str) -> Any:
    if line is not None:
        same = [condition for condition in fn.conditions if condition.line == line or getattr(condition, "analysis_line", None) == line]
        if same:
            return same[0]
        previous = [condition for condition in fn.conditions if condition.line < line]
        if previous:
            return max(previous, key=lambda condition: condition.line)
    lowered = str(text or "").lower()
    for condition in fn.conditions:
        for candidate in TestVectorGenerator._condition_texts(condition):
            if candidate.lower() in lowered:
                return condition
    return None


def _line_from_text(text: str) -> int | None:
    match = re.search(r"\bline\s+(\d+)\b", str(text), flags=re.I)
    return int(match.group(1)) if match else None


def _quoted_decision(text: str) -> str:
    match = re.search(r"`([^`]+)`", str(text or ""))
    return match.group(1).strip() if match else ""


def _desired_from_text(text: str) -> bool:
    lowered = str(text or "").lower()
    if "missing false" in lowered or "false branch" in lowered:
        return False
    return True


def _metric_from_text(text: str) -> str:
    lowered = str(text or "").lower()
    if "mc/dc" in lowered or "mcdc" in lowered:
        return "MC/DC"
    if "c1" in lowered or "branch" in lowered or "decision" in lowered:
        return "C1"
    return "C0"


def _cfg_edges(pruned_cfg: Any) -> list[dict[str, Any]]:
    if isinstance(pruned_cfg, dict) and isinstance(pruned_cfg.get("edges"), list):
        return [edge for edge in pruned_cfg["edges"] if isinstance(edge, dict)]
    return []


def _edge_line(edge: dict[str, Any]) -> int | None:
    text = str(edge.get("cond_loc") or edge.get("location") or "")
    match = re.search(r":(\d+)(?::\d+)?$", text)
    return int(match.group(1)) if match else None
