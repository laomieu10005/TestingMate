from __future__ import annotations

from dataclasses import dataclass, field


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
