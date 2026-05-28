from __future__ import annotations

from typing import Any, Dict, List, Tuple

from testing_mate.models import FunctionInfo
from testing_mate.layers.layer_angr_basic import _source_level_concolic, _try_angr


def run_angr_deep(
    fn: FunctionInfo,
    compile_db: Any,
    unreachable_targets: List[Any],
    cov_db: Any,
    timeout: int = 300,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deep symbolic layer with optional angr DFS/LoopSeer and source fallback."""
    real_tests, unresolved = _try_angr(fn, compile_db, unreachable_targets, timeout, deep=True)
    fallback_tests = _source_level_concolic(fn, unresolved, "ANGR_DEEP")
    solved_labels = {
        str(test.get("rationale") or test.get("description") or "")
        for test in [*real_tests, *fallback_tests]
    }
    final_gaps = []
    if not real_tests and not fallback_tests:
        for target in unresolved:
            final_gaps.append({"target": target, "reason": "No angr binary metadata and no source-level constraint could be materialized."})
    else:
        for target in unresolved:
            label = str(target)
            if not any(label[:40] in solved for solved in solved_labels):
                final_gaps.append({"target": target, "reason": "Deep layer emitted fallback vectors; backend rerun must confirm closure."})
    return [*real_tests, *fallback_tests], final_gaps
