from typing import Any, Dict, List


def materialize(fn: Any, symex_inputs: List[Dict[str, Any]], existing_cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from testing_mate.vector_generator import TestVectorGenerator

    new_cases = []
    signatures = {
        TestVectorGenerator._vector_signature(fn.name, c.get("values") or {})
        for c in existing_cases
    }

    def _materialized_values(inputs):
        return {p.name: str(inputs.get(p.name, "")) for p in getattr(fn, "parameters", [])}

    for idx, inputs in enumerate(symex_inputs):
        values = _materialized_values(inputs)
        signature = TestVectorGenerator._vector_signature(fn.name, values)
        if signature in signatures:
            continue
        signatures.add(signature)
        case = {
            "test_id": f"SYMEX_{fn.name}_{idx:04d}",
            "function": fn.name,
            "description": f"SymEx input targeting uncovered path #{idx}",
            "values": values,
        }
        new_cases.append(case)
    return new_cases
