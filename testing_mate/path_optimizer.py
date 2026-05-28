from typing import Any, Dict, List

WEIGHT_UNCOVERED_LINE = 10
WEIGHT_UNCOVERED_BRANCH = 5
WEIGHT_MCDC_MISSING = 20


def block_weight(block: Dict[str, Any], cov_db: Any) -> int:
    w = 1
    for ln in block.get('lines', []):
        if cov_db.is_line_uncovered(ln):
            w += WEIGHT_UNCOVERED_LINE
    if cov_db.has_uncovered_branch(block.get('id')):
        w += WEIGHT_UNCOVERED_BRANCH
    if cov_db.has_mcdc_missing(block.get('id')):
        w += WEIGHT_MCDC_MISSING
    return w


def select_targets(cfg: Dict[str, Any], cov_db: Any, k: int = 5) -> List[int]:
    weights = {b['id']: block_weight(b, cov_db) for b in cfg.get('blocks', [])}
    # sort blocks by weight desc
    sorted_blocks = sorted(weights.items(), key=lambda x: x[1], reverse=True)
    return [bid for bid, _ in sorted_blocks[:k]]
