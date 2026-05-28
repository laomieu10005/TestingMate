from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from testing_mate.models import CoverageGap, FunctionInfo
from testing_mate.vector_generator import TestVectorGenerator


def run_angr_basic(
    fn: FunctionInfo,
    compile_db: Any,
    unsolvable_edges: List[Any],
    cov_db: Any,
    timeout: int = 120,
) -> Tuple[List[Dict[str, Any]], List[Any]]:
    """Target unresolved paths with angr when possible, else concolic fallback.

    A full angr run requires a prepared binary and symbol/target addresses. The
    GUI flow usually reaches this layer with source-level targets only, so this
    implementation first tries real angr metadata, then emits deterministic
    concolic vectors for the unresolved source constraints.
    """
    real_tests, unresolved = _try_angr(fn, compile_db, unsolvable_edges, timeout, deep=False)
    fallback_tests = _source_level_concolic(fn, unresolved, "ANGR_BASIC")
    remaining = [target for target in unresolved if not _target_has_decision(target)]
    return [*real_tests, *fallback_tests], remaining


def _try_angr(fn: FunctionInfo, compile_db: Any, targets: list[Any], timeout: int, deep: bool) -> tuple[list[dict[str, Any]], list[Any]]:
    binary = _binary_path(compile_db)
    if not binary:
        return [], list(targets)
    try:
        import angr  # type: ignore
        import claripy  # type: ignore
    except Exception:
        return [], list(targets)

    address = _function_address(compile_db, fn.name)
    if address is None:
        return [], list(targets)
    address_targets = [target for target in targets if _target_address(target) is not None]
    if not address_targets:
        return [], list(targets)

    tests: list[dict[str, Any]] = []
    unresolved: list[Any] = []
    try:
        project = angr.Project(str(binary), auto_load_libs=False)
    except Exception:
        return [], list(targets)

    for target in targets:
        target_addr = _target_address(target)
        if target_addr is None:
            unresolved.append(target)
            continue
        try:
            sym_args = []
            arg_vars = []
            for param in getattr(fn, "parameters", []) or []:
                bits = _bits_for_ctype(param.ctype)
                sym = claripy.BVS(param.name, bits)
                sym_args.append(sym)
                arg_vars.append((param, sym))
            state = project.factory.call_state(address, *sym_args)
            simgr = project.factory.simulation_manager(state)
            if deep:
                try:
                    simgr.use_technique(angr.exploration_techniques.DFS())
                    cfg = project.analyses.CFGFast()
                    simgr.use_technique(angr.exploration_techniques.LoopSeer(cfg=cfg, bound=3))
                except Exception:
                    pass
            simgr.explore(find=target_addr, n=1)
            if not simgr.found:
                unresolved.append(target)
                continue
            found = simgr.found[0]
            values: dict[str, str] = {}
            for param, sym in arg_vars:
                try:
                    numeric = int(found.solver.eval(sym))
                except Exception:
                    continue
                if param.enum_values:
                    values[param.name] = param.enum_values[numeric % len(param.enum_values)]
                else:
                    values[param.name] = str(numeric)
            tests.append(
                {
                    "test_id": f"{'ANGR_DEEP' if deep else 'ANGR_BASIC'}_{fn.name}_{len(tests) + 1:04d}",
                    "function": fn.name,
                    "coverage": "C0/C1",
                    "values": values,
                    "description": f"angr reached target {hex(target_addr)}",
                    "rationale": f"angr {'deep' if deep else 'basic'} symbolic execution reached {hex(target_addr)}.",
                    "__layer": "angr_deep" if deep else "angr_basic",
                }
            )
        except Exception:
            unresolved.append(target)
    return tests, unresolved


def _source_level_concolic(fn: FunctionInfo, targets: list[Any], prefix: str) -> list[dict[str, Any]]:
    generator = TestVectorGenerator()
    tests: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for target in targets:
        vectors = _vectors_for_target(generator, fn, target)
        for vector in vectors:
            signature = tuple(sorted((str(key), str(value)) for key, value in vector.items()))
            if signature in seen:
                continue
            seen.add(signature)
            tests.append(
                {
                    "test_id": f"{prefix}_{fn.name}_{len(tests) + 1:04d}",
                    "function": fn.name,
                    "coverage": str(_target_metric(target) or "C0/C1"),
                    "values": vector,
                    "description": f"Source-level concolic fallback for {_target_label(target)}",
                    "rationale": f"{prefix} targeted unresolved source constraint {_target_label(target)}.",
                    "__layer": prefix.lower(),
                }
            )
    return tests


def _vectors_for_target(generator: TestVectorGenerator, fn: FunctionInfo, target: Any) -> list[dict[str, str]]:
    decision = _target_decision(target)
    desired = _target_desired(target)
    line = _target_line(target)
    base = generator._base_vector_for(fn)
    if decision:
        vec = dict(base)
        condition = _condition_for_line(fn, line)
        if condition is not None and line is not None:
            for previous in fn.conditions:
                if previous.line < line:
                    generator._drive_condition(vec, previous, previous.depth < condition.depth)
        generator._drive_decision(vec, decision, desired)
        return [vec]
    if line is not None:
        gap = CoverageGap(function=fn.name, line=line, metric=str(_target_metric(target) or "C0"), text=str(target))
        return [case.values for case in generator.generate_for_coverage_gaps(fn.name, [fn], [], [gap], "UT", "C0/C1", max_cases_per_gap=3)]
    return generator._path_optimized_vectors(fn, base)[:6]


def _target_has_decision(target: Any) -> bool:
    return bool(_target_decision(target))


def _target_decision(target: Any) -> str:
    if isinstance(target, dict):
        return str(target.get("decision") or target.get("cond_text") or target.get("condition") or "").strip()
    return ""


def _target_desired(target: Any) -> bool:
    if isinstance(target, dict):
        return bool(target.get("desired", target.get("kind") != "false_branch"))
    return True


def _target_line(target: Any) -> int | None:
    if isinstance(target, dict):
        raw = target.get("line")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
        loc = str(target.get("cond_loc") or target.get("location") or "")
        match = re.search(r":(\d+)(?::\d+)?$", loc)
        if match:
            return int(match.group(1))
    return None


def _target_metric(target: Any) -> str:
    return str(target.get("metric") or "") if isinstance(target, dict) else ""


def _target_label(target: Any) -> str:
    return _target_decision(target) or (f"line {_target_line(target)}" if _target_line(target) is not None else str(target)[:80])


def _condition_for_line(fn: FunctionInfo, line: int | None) -> Any:
    if line is None:
        return None
    same = [condition for condition in fn.conditions if condition.line == line or getattr(condition, "analysis_line", None) == line]
    if same:
        return same[0]
    previous = [condition for condition in fn.conditions if condition.line < line]
    return max(previous, key=lambda condition: condition.line) if previous else None


def _binary_path(compile_db: Any) -> Path | None:
    if isinstance(compile_db, dict):
        raw = compile_db.get("binary") or compile_db.get("binary_path") or compile_db.get("executable")
        if raw and Path(str(raw)).exists():
            return Path(str(raw))
    return None


def _function_address(compile_db: Any, fn_name: str) -> int | None:
    if not isinstance(compile_db, dict):
        return None
    addresses = compile_db.get("function_addresses") or {}
    raw = addresses.get(fn_name) if isinstance(addresses, dict) else None
    if raw is None:
        raw = compile_db.get("entry_address")
    try:
        return int(str(raw), 0)
    except (TypeError, ValueError):
        return None


def _target_address(target: Any) -> int | None:
    if not isinstance(target, dict):
        return None
    raw = target.get("target_addr") or target.get("address") or target.get("addr")
    try:
        return int(str(raw), 0)
    except (TypeError, ValueError):
        return None


def _bits_for_ctype(ctype: str) -> int:
    text = str(ctype).lower()
    if "64" in text:
        return 64
    if "16" in text:
        return 16
    if "8" in text or "char" in text or "bool" in text or "boolean" in text:
        return 8
    return 32
