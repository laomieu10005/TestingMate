from testing_mate.models import * 
from testing_mate.reporter import *

class CoverageGapPlanner:
    def plan(self, feedback: CoverageFeedback, functions: list[FunctionInfo], max_gaps: int = 12) -> list[CoverageGap]:
        gaps: list[CoverageGap] = []
        function_by_name = {fn.name: fn for fn in functions}
        for item in feedback.uncovered_items:
            gap = self._gap_from_text(item, functions, function_by_name)
            if gap:
                gaps.append(gap)
        if not gaps:
            gaps.extend(self._metric_fallback_gaps(feedback, functions))
        unique: list[CoverageGap] = []
        seen: set[tuple[str, int | None, str, str]] = set()
        for gap in gaps:
            key = (gap.function, gap.line, gap.metric, gap.condition or gap.text)
            if key in seen:
                continue
            seen.add(key)
            unique.append(gap)
            if len(unique) >= max_gaps:
                break
        return unique

    def _gap_from_text(self, text: str, functions: list[FunctionInfo], function_by_name: dict[str, FunctionInfo]) -> CoverageGap | None:
        lowered = text.lower()
        metric = "MC/DC" if "mc/dc" in lowered or "mcdc" in lowered else "C1" if "branch" in lowered or "decision" in lowered or "c1" in lowered else "C0"
        line = self._line_from_text(text)
        fn = self._function_from_text(text, functions, function_by_name, line)
        if not fn:
            return None
        condition = self._condition_for_gap(fn, text, line)
        return CoverageGap(function=fn.name, line=line, metric=metric, text=text[:500], condition=condition)

    @staticmethod
    def _line_from_text(text: str) -> int | None:
        match = re.search(r"\bline\s+(\d+)\b", text, flags=re.I)
        if match:
            return int(match.group(1))
        match = re.search(r"\b(?:DA|BRDA):(\d+)", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _function_from_text(text: str, functions: list[FunctionInfo], function_by_name: dict[str, FunctionInfo], line: int | None) -> FunctionInfo | None:
        for name, fn in function_by_name.items():
            if re.search(rf"\b{re.escape(name)}\b", text):
                return fn
        if line is not None:
            for fn in functions:
                if fn.start_line <= line <= fn.end_line:
                    return fn
        return None

    @staticmethod
    def _condition_for_gap(fn: FunctionInfo, text: str, line: int | None) -> str:
        if line is not None:
            same_line = [condition for condition in fn.conditions if condition.line == line]
            if same_line:
                return same_line[0].text
        for condition in fn.conditions:
            if condition.text and condition.text in text:
                return condition.text
            for decision in condition.decisions:
                if decision and decision in text:
                    return condition.text
        return ""

    @staticmethod
    def _metric_fallback_gaps(feedback: CoverageFeedback, functions: list[FunctionInfo]) -> list[CoverageGap]:
        gaps: list[CoverageGap] = []
        weak_metrics = [metric for metric, value in feedback.metrics.items() if value is None or value < 100.0]
        if not weak_metrics:
            weak_metrics = ["C1"]
        for metric in weak_metrics:
            for fn in functions:
                if fn.conditions:
                    condition = fn.conditions[0]
                    gaps.append(
                        CoverageGap(
                            function=fn.name,
                            line=condition.line,
                            metric=metric,
                            text=f"{metric} target still below requested coverage for {fn.name}.",
                            condition=condition.text,
                        )
                    )
        return gaps
