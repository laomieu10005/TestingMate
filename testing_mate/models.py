from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Parameter:
    name: str
    ctype: str
    kind: str = "scalar"
    enum_values: list[str] = field(default_factory=list)


@dataclass
class Condition:
    text: str
    line: int
    decisions: list[str] = field(default_factory=list)
    depth: int = 0
    analysis_text: str = ""
    analysis_decisions: list[str] = field(default_factory=list)
    analysis_line: int | None = None


@dataclass
class FunctionInfo:
    name: str
    return_type: str
    parameters: list[Parameter]
    start_line: int
    end_line: int
    body: str
    conditions: list[Condition] = field(default_factory=list)


@dataclass
class TestCase:
    test_id: str
    unit: str
    function: str
    coverage: str
    level: str
    values: dict[str, str]
    expected: str
    rationale: str
    artifact_status: str = "candidate"
    oracle_status: str = "needs_review"
    execution_status: str = "not_run"
    description: str = ""
    preconditions: str = ""
    setup: str = ""
    test_steps: str = ""
    teardown: str = ""
    pass_fail_criteria: str = ""
    requirement_ref: str = ""
    safety_goal_ref: str = ""
    asil: str = "ASIL-D"
    aspice_level: str = "ASPICE L2"
    trace_tags: str = ""


@dataclass
class CoverageFeedback:
    source: str
    raw_text: str
    coverage_percent: float | None = None
    metrics: dict[str, float | None] = field(default_factory=dict)
    metric_notes: dict[str, str] = field(default_factory=dict)
    uncovered_items: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    execution_layers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ExecutionContext:
    """Backend execution context used by the layered escalation engine.

    The context deliberately carries project/build data instead of letting
    escalation depend on GUI state. This makes the layer engine callable from
    GUI workflows, CLI workflows, and focused regression tests.
    """

    source_path: Path
    backend: str
    out_dir: Path
    include_dirs: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    compiler: str = ""
    coverage: str = "MC/DC"
    level: str = "UT"
    asil_level: str = "ASIL D"
    target_coverage_percent: float = 100.0
    unit_sources: list[Path] = field(default_factory=list)
    toolchain_options: dict[str, Any] = field(default_factory=dict)
    run_after_generate: bool = True

    def __post_init__(self) -> None:
        # Some GUI/project JSON paths arrive as plain strings.  Normalize once
        # here so GAP-09 backend execution can safely call pathlib APIs such as
        # .resolve(), .exists(), .parent, and .suffix.
        self.source_path = self.source_path if isinstance(self.source_path, Path) else Path(str(self.source_path))
        self.out_dir = self.out_dir if isinstance(self.out_dir, Path) else Path(str(self.out_dir))
        self.unit_sources = [item if isinstance(item, Path) else Path(str(item)) for item in (self.unit_sources or []) if item]
        self.include_dirs = [str(item) for item in (self.include_dirs or []) if item]
        self.defines = [str(item) for item in (self.defines or []) if item]
        self.backend = str(self.backend)
        self.compiler = str(self.compiler or "")
        self.coverage = str(self.coverage or "MC/DC")
        self.level = str(self.level or "UT")
        self.asil_level = str(self.asil_level or "ASIL D")


@dataclass
class CoverageGap:
    function: str
    line: int | None
    metric: str
    text: str
    condition: str = ""


@dataclass
class RunResult:
    backend: str
    command: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    skipped_reason: str = ""
