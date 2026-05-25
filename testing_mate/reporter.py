from __future__ import annotations

from testing_mate.models import CoverageFeedback

from pathlib import Path
import html
import json
import re
from typing import Any


class MCDCDetailedParser:
    """Parse detailed MC/DC metrics from llvm-cov outputs."""
    
    @staticmethod
    @staticmethod
    def parse_mcdc_summary_detailed(text: str) -> dict[str, Any]:
        """Extract useful MC/DC detail from llvm-cov text output.

        `llvm-cov report` usually exposes only the TOTAL table. `llvm-cov show`
        may expose decision/vector detail depending on the LLVM version and flags.
        This parser intentionally accepts both formats and never invents MC/DC:
        if the LLVM output does not contain MC/DC data, `overall_percent` stays None.
        """
        result: dict[str, Any] = {
            "overall_percent": None,
            "per_decision": [],
            "uncovered_decisions": [],
            "raw_mcdc_lines": [],
        }

        normalized_lines = [" ".join(line.strip().split()) for line in text.splitlines()]
        normalized_lines = [line for line in normalized_lines if line]

        # llvm-cov report table: the TOTAL row normally ends with MC/DC percentage
        # when -show-mcdc-summary is enabled.
        for line in normalized_lines:
            if not line.startswith("TOTAL"):
                continue
            percentages = [float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*%", line)]
            if percentages:
                result["overall_percent"] = percentages[-1]

        # Keep source/show lines that mention MC/DC, decision, condition, or vector so
        # the HTML report can display raw evidence without guessing unavailable fields.
        for line in normalized_lines:
            lowered = line.lower()
            if any(token in lowered for token in ("mcdc", "mc/dc", "decision", "condition", "vector")):
                result["raw_mcdc_lines"].append(line[:500])

                line_no = None
                line_match = re.search(r"\bline\s+(\d+)\b", line, re.IGNORECASE)
                if line_match:
                    line_no = int(line_match.group(1))

                # Common detail style: N/M conditions or covered/total wording.
                fraction_match = re.search(r"\b(\d+)\s*/\s*(\d+)\b", line)
                if fraction_match and int(fraction_match.group(2)) > 0:
                    covered = int(fraction_match.group(1))
                    total = int(fraction_match.group(2))
                    entry = {
                        "line": line_no,
                        "text": line,
                        "covered_conditions": covered,
                        "total_conditions": total,
                        "percent": (covered / total) * 100.0,
                    }
                    result["per_decision"].append(entry)
                    if covered < total:
                        result["uncovered_decisions"].append({
                            "line": line_no,
                            "reason": line,
                        })
                    continue

                if any(token in lowered for token in ("not covered", "uncovered", "false", "missing")):
                    result["uncovered_decisions"].append({
                        "line": line_no,
                        "reason": line,
                    })

        # Avoid returning thousands of lines from a large source report.
        result["raw_mcdc_lines"] = result["raw_mcdc_lines"][:200]
        result["per_decision"] = result["per_decision"][:200]
        result["uncovered_decisions"] = result["uncovered_decisions"][:200]
        return result


class CoverageReportParser:
    coverage_pattern = re.compile(r"(?i)\b(?:coverage|statement|line|branch|mcdc|mc/dc|decision)\b[^\n%]{0,80}?(\d+(?:\.\d+)?)\s*%")
    metric_patterns = {
        "C0": re.compile(r"(?i)\b(?:c0|statement|statements|line|lines)\b[^\n%]{0,80}?(\d+(?:\.\d+)?)\s*%"),
        "C1": re.compile(r"(?i)\b(?:c1|branch|branches|decision|decisions)\b[^\n%]{0,80}?(\d+(?:\.\d+)?)\s*%"),
        "MC/DC": re.compile(r"(?i)\b(?:mc/dc|mcdc|modified\s+condition/decision)\b[^\n%]{0,80}?(\d+(?:\.\d+)?)\s*%"),
    }

    def parse_file(self, path: Path, target_files: list[str | Path] | None = None) -> CoverageFeedback:
        text = path.read_text(encoding="utf-8", errors="replace")
        suffix = path.suffix.lower()
        if suffix in {".info", ".lcov"}:
            parsed = self._parse_lcov_info(text, str(path), target_files)
            if parsed:
                return parsed
        if suffix == ".json":
            parsed = self._parse_llvm_cov_json(text, str(path), target_files)
            if parsed:
                return parsed
        return self.parse_text(text, str(path), target_files)

    def parse_text(self, text: str, source: str = "manual input", target_files: list[str | Path] | None = None) -> CoverageFeedback:
        if re.search(r"(?m)^(?:SF|LF|LH|BRF|BRH|DA|BRDA|MRF|MRH|MCDC):", text):
            parsed = self._parse_lcov_info(text, source, target_files)
            if parsed:
                return parsed
        parsed_json = self._parse_llvm_cov_json(text, source, target_files)
        if parsed_json:
            return parsed_json

        normalized = self._normalize_text(text)
        metrics = self._extract_metrics(normalized)
        self._merge_metrics(metrics, self._extract_llvm_cov_report_metrics(normalized))
        percent = self._best_percent(normalized, metrics)
        uncovered: list[str] = []
        failures: list[str] = []
        for raw_line in normalized.splitlines():
            line = " ".join(raw_line.strip().split())
            if not line:
                continue
            lowered = line.lower()
            if any(token in lowered for token in ("uncovered", "not covered", "missing", "partial", "mcdc", "mc/dc")):
                uncovered.append(line[:500])
            if any(token in lowered for token in ("fail", "error", "exception", "assert", "expected", "actual")):
                failures.append(line[:500])
        notes = self._metric_notes(metrics, source)
        if metrics.get("MC/DC") is not None:
            notes["MC/DC"] = "From llvm-cov textual MC/DC summary."
        return CoverageFeedback(
            source=source,
            raw_text=normalized[:12000],
            coverage_percent=percent,
            metrics=metrics,
            metric_notes=notes,
            uncovered_items=uncovered[:80],
            failures=failures[:80],
        )

    def _best_percent(self, text: str, metrics: dict[str, float | None] | None = None) -> float | None:
        values = [value for value in (metrics or {}).values() if value is not None]
        if values:
            return min(values)
        matches = [float(match.group(1)) for match in self.coverage_pattern.finditer(text)]
        if not matches:
            generic = re.findall(r"(\d+(?:\.\d+)?)\s*%", text)
            matches = [float(item) for item in generic[:20]]
        return min(matches) if matches else None

    def _extract_metrics(self, text: str) -> dict[str, float | None]:
        metrics: dict[str, float | None] = {"C0": None, "C1": None, "MC/DC": None}
        for metric, pattern in self.metric_patterns.items():
            matches = [float(match.group(1)) for match in pattern.finditer(text)]
            if matches:
                metrics[metric] = min(matches)
        return metrics

    @staticmethod
    def _merge_metrics(target: dict[str, float | None], source: dict[str, float | None]) -> None:
        for key, value in source.items():
            if value is not None:
                target[key] = value

    @staticmethod
    def _normalize_text(text: str) -> str:
        if "<" in text and ">" in text:
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", "\n", text)
            text = html.unescape(text)
        return text

    @staticmethod
    def _percent(hit: int, found: int) -> float | None:
        if found <= 0:
            return None
        return (hit / found) * 100.0

    def _parse_lcov_info(self, text: str, source: str, target_files: list[str | Path] | None = None) -> CoverageFeedback | None:
        current_file = ""
        include_current_file = True
        filtered_lines: list[str] = []
        targets = self._normalize_targets(target_files)
        line_hits: dict[tuple[str, int], int] = {}
        branch_hits: dict[tuple[str, int, str, str], int] = {}
        fallback_lines_found = fallback_lines_hit = 0
        fallback_branches_found = fallback_branches_hit = 0

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("SF:"):
                current_file = line[3:]
                include_current_file = self._path_matches_targets(current_file, targets)
                if include_current_file:
                    filtered_lines.append(raw_line)
                continue
            if not include_current_file:
                continue
            filtered_lines.append(raw_line)
            if line.startswith("LF:"):
                fallback_lines_found += self._lcov_hit_count(line[3:])
            elif line.startswith("LH:"):
                fallback_lines_hit += self._lcov_hit_count(line[3:])
            elif line.startswith("BRF:"):
                fallback_branches_found += self._lcov_hit_count(line[4:])
            elif line.startswith("BRH:"):
                fallback_branches_hit += self._lcov_hit_count(line[4:])
            elif line.startswith("DA:"):
                parts = line[3:].split(",")
                if len(parts) >= 2 and parts[0].isdigit():
                    key = (current_file, int(parts[0]))
                    line_hits[key] = line_hits.get(key, 0) + self._lcov_hit_count(parts[1])
            elif line.startswith("BRDA:"):
                parts = line[5:].split(",")
                if len(parts) >= 4 and parts[0].isdigit():
                    key = (current_file, int(parts[0]), parts[1], parts[2])
                    branch_hits[key] = branch_hits.get(key, 0) + self._lcov_hit_count(parts[3])

        lines_found = len(line_hits) if line_hits else fallback_lines_found
        lines_hit = sum(1 for count in line_hits.values() if count > 0) if line_hits else fallback_lines_hit
        branches_found = len(branch_hits) if branch_hits else fallback_branches_found
        branches_hit = sum(1 for count in branch_hits.values() if count > 0) if branch_hits else fallback_branches_hit

        if not any((lines_found, branches_found)):
            return None

        uncovered: list[str] = []
        for (path, line_no), count in sorted(line_hits.items(), key=lambda item: (item[0][0].lower(), item[0][1])):
            if count <= 0:
                uncovered.append(f"Uncovered C0 line {line_no} in {path}")
        for (path, line_no, block, branch), count in sorted(branch_hits.items(), key=lambda item: (item[0][0].lower(), item[0][1], item[0][2], item[0][3])):
            if count <= 0:
                uncovered.append(f"Uncovered C1 branch line {line_no} block {block} branch {branch} in {path}")
        metrics = {
            "C0": self._percent(lines_hit, lines_found),
            "C1": self._percent(branches_hit, branches_found),
            "MC/DC": None,
        }
        notes = self._metric_notes(metrics, source)
        target_note = self._target_note(targets)
        notes["C0"] = f"From lcov LF/LH counters{target_note}: {lines_hit}/{lines_found} lines hit." if lines_found else f"lcov did not emit line counters{target_note}."
        notes["C1"] = f"From lcov BRF/BRH counters{target_note}: {branches_hit}/{branches_found} branches hit." if branches_found else f"lcov did not emit branch counters{target_note}."
        notes["MC/DC"] = "MC/DC unavailable: LCOV/gcov branch counters do not contain executed condition vectors."
        raw_text = "\n".join(filtered_lines) if targets else text
        return CoverageFeedback(
            source=source,
            raw_text=raw_text[:12000],
            coverage_percent=self._best_percent(raw_text, metrics),
            metrics=metrics,
            metric_notes=notes,
            uncovered_items=uncovered[:80],
            failures=[],
        )

    def _parse_mcdc_record(self, payload: str, current_file: str) -> tuple[tuple[str, int, str, str, str, str], int] | None:
        parts = [part.strip() for part in payload.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            return None
        line_no = int(parts[0])
        group_size = parts[1] if len(parts) > 1 else ""
        sense = parts[2] if len(parts) > 2 else ""
        taken = parts[3] if len(parts) > 3 else ""
        index = parts[4] if len(parts) > 4 else str(len(parts))
        expression = ",".join(parts[5:]).strip() if len(parts) > 5 else ""
        return (current_file, line_no, group_size, sense, index, expression), self._lcov_mcdc_hit_count(taken)

    @staticmethod
    def _lcov_mcdc_hit_count(raw: str) -> int:
        value = str(raw).strip().lower()
        if value in {"", "-", "0", "false", "f", "no", "not_covered", "uncovered"}:
            return 0
        if value in {"1", "true", "t", "yes", "covered"}:
            return 1
        try:
            return 1 if float(value) > 0 else 0
        except ValueError:
            return 0

    @staticmethod
    def _lcov_hit_count(raw: str) -> int:
        value = str(raw).strip()
        if value in {"", "-"}:
            return 0
        try:
            return int(float(value))
        except ValueError:
            return 0

    def _parse_llvm_cov_json(self, text: str, source: str, target_files: list[str | Path] | None = None) -> CoverageFeedback | None:
        stripped = text.lstrip()
        if not stripped.startswith(("{", "[")):
            return None
        try:
            payload = json.loads(text)
        except Exception:
            return None

        targets = self._normalize_targets(target_files)
        totals = self._extract_llvm_json_totals(payload, targets)
        if not totals:
            return None

        metrics = {
            "C0": self._percent_from_total(totals.get("lines")),
            "C1": self._percent_from_total(totals.get("branches")),
            "MC/DC": self._percent_from_total(totals.get("mcdc")),
        }
        notes = self._metric_notes(metrics, source)
        target_note = self._target_note(targets)
        for key, label in (("C0", "lines"), ("C1", "branches"), ("MC/DC", "MC/DC conditions")):
            total = totals.get({"C0": "lines", "C1": "branches", "MC/DC": "mcdc"}[key])
            if total:
                notes[key] = f"From llvm-cov JSON{target_note}: {int(total.get('covered', 0))}/{int(total.get('count', 0))} {label} covered."
        if metrics.get("MC/DC") is None:
            notes["MC/DC"] = "LLVM coverage exists, but no MC/DC records were found. Rebuild target sources with -fcoverage-mcdc and rerun tests."

        uncovered = self._extract_llvm_json_uncovered_items(payload, targets)

        return CoverageFeedback(
            source=source,
            raw_text=text[:12000],
            coverage_percent=self._best_percent(text, metrics),
            metrics=metrics,
            metric_notes=notes,
            uncovered_items=uncovered[:120],
            failures=[],
        )

    def _extract_llvm_json_uncovered_items(self, payload: object, targets: list[str]) -> list[str]:
        """Best-effort gap list from llvm-cov export JSON.

        This is intentionally conservative. It reports line/branch/MC/DC evidence that
        llvm-cov explicitly marks as not executed or not fully covered; it does not
        derive MC/DC from LCOV-style branch counters.
        """
        items: list[str] = []
        data = payload.get("data") if isinstance(payload, dict) else None
        if not (isinstance(data, list) and data and isinstance(data[0], dict)):
            return items

        files = data[0].get("files")
        if not isinstance(files, list):
            return items

        for file_item in files:
            if not isinstance(file_item, dict):
                continue
            filename = str(file_item.get("filename") or file_item.get("name") or "")
            if filename and not self._path_matches_targets(filename, targets):
                continue

            # Segments are commonly [line, col, count, has_count, is_region_entry, ...].
            zero_lines: set[int] = set()
            segments = file_item.get("segments")
            if isinstance(segments, list):
                for segment in segments:
                    if isinstance(segment, list) and len(segment) >= 4:
                        try:
                            line_no = int(segment[0])
                            count = int(float(segment[2]))
                            has_count = bool(segment[3])
                        except (TypeError, ValueError):
                            continue
                        if has_count and count <= 0:
                            zero_lines.add(line_no)
                    elif isinstance(segment, dict):
                        line_no = self._first_number(segment, ("line", "line_no", "line_number"))
                        count = self._first_number(segment, ("count", "execution_count", "covered"))
                        if line_no is not None and count is not None and count <= 0:
                            zero_lines.add(int(line_no))
            for line_no in sorted(zero_lines)[:80]:
                items.append(f"Uncovered C0 line {line_no} in {filename}")

            branches = file_item.get("branches")
            if isinstance(branches, list):
                for branch in branches[:160]:
                    if isinstance(branch, list) and len(branch) >= 4:
                        try:
                            line_no = int(branch[0])
                            true_count = int(float(branch[2]))
                            false_count = int(float(branch[3]))
                        except (TypeError, ValueError):
                            continue
                        if true_count <= 0:
                            items.append(f"Uncovered C1 true branch line {line_no} in {filename}")
                        if false_count <= 0:
                            items.append(f"Uncovered C1 false branch line {line_no} in {filename}")
                    elif isinstance(branch, dict):
                        line_no = self._first_number(branch, ("line", "line_no", "line_number"))
                        count = self._first_number(branch, ("count", "covered", "executed"))
                        false_count = self._first_number(branch, ("false_count", "false", "not_taken"))
                        true_count = self._first_number(branch, ("true_count", "true", "taken"))
                        if line_no is None:
                            continue
                        if count is not None and count <= 0:
                            items.append(f"Uncovered C1 branch line {int(line_no)} in {filename}")
                        if true_count is not None and true_count <= 0:
                            items.append(f"Uncovered C1 true branch line {int(line_no)} in {filename}")
                        if false_count is not None and false_count <= 0:
                            items.append(f"Uncovered C1 false branch line {int(line_no)} in {filename}")

            mcdc_records = file_item.get("mcdc_records")
            if isinstance(mcdc_records, list):
                for record in mcdc_records[:160]:
                    if isinstance(record, dict):
                        line_no = self._first_number(record, ("line", "line_no", "line_number"))
                        percent = self._first_number(record, ("percent", "coverage", "ratio"))
                        covered = self._first_number(record, ("covered", "hit", "executed"))
                        count = self._first_number(record, ("count", "total", "num"))
                        if percent is not None and percent < 100.0:
                            line_text = f" line {int(line_no)}" if line_no is not None else ""
                            items.append(f"Uncovered MC/DC obligation{line_text} in {filename}: {percent:.2f}%")
                        elif count is not None and covered is not None and covered < count:
                            line_text = f" line {int(line_no)}" if line_no is not None else ""
                            items.append(f"Uncovered MC/DC obligation{line_text} in {filename}: {int(covered)}/{int(count)} covered")
                    elif isinstance(record, list) and len(record) >= 3:
                        try:
                            line_no = int(record[0])
                        except (TypeError, ValueError):
                            line_no = None
                        text = ",".join(str(part) for part in record)
                        if any(token in text.lower() for token in ("false", "uncovered", "not covered")):
                            line_text = f" line {line_no}" if line_no is not None else ""
                            items.append(f"Uncovered MC/DC obligation{line_text} in {filename}")
        # Stable de-duplication.
        unique: list[str] = []
        seen: set[str] = set()
        for item in items:
            if item not in seen:
                unique.append(item)
                seen.add(item)
        return unique
    def _extract_llvm_json_totals(self, payload: object, targets: list[str]) -> dict[str, dict[str, float]]:
        totals = {"lines": {"count": 0.0, "covered": 0.0}, "branches": {"count": 0.0, "covered": 0.0}, "mcdc": {"count": 0.0, "covered": 0.0}}

        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            block = data[0]
            files = block.get("files")
            if isinstance(files, list):
                for file_item in files:
                    if not isinstance(file_item, dict):
                        continue
                    filename = str(file_item.get("filename") or file_item.get("name") or "")
                    if filename and not self._path_matches_targets(filename, targets):
                        continue
                    summary = file_item.get("summary")
                    if not isinstance(summary, dict):
                        continue
                    for json_key, kind in (("lines", "lines"), ("branches", "branches"), ("mcdc", "mcdc")):
                        metric = summary.get(json_key)
                        if isinstance(metric, dict):
                            count = self._first_number(metric, ("count", "num", "found", "total"))
                            covered = self._first_number(metric, ("covered", "hit", "executed"))
                            percent = self._first_number(metric, ("percent", "coverage", "ratio"))
                            if count is None:
                                if covered is not None and percent not in (None, 0):
                                    count = covered * 100.0 / percent
                                else:
                                    notcovered = self._first_number(metric, ("notcovered", "not_covered", "missed"))
                                    if covered is not None and notcovered is not None:
                                        count = covered + notcovered
                            if count is None:
                                continue
                            if covered is None:
                                covered = count * percent / 100.0 if percent is not None else 0.0
                            totals[kind]["count"] += float(count)
                            totals[kind]["covered"] += float(covered)
                return {key: value for key, value in totals.items() if value["count"] > 0}

        def add_metric(kind: str, node: object) -> None:
            if not isinstance(node, dict):
                return
            count = self._first_number(node, ("count", "num", "found", "total"))
            covered = self._first_number(node, ("covered", "hit", "executed"))
            percent = self._first_number(node, ("percent", "coverage", "ratio"))
            if count is None and covered is not None and percent not in (None, 0):
                count = covered * 100.0 / percent
            if count is None:
                return
            if covered is None and percent is not None:
                covered = count * percent / 100.0
            if covered is None:
                covered = 0.0
            totals[kind]["count"] += float(count)
            totals[kind]["covered"] += float(covered)

        def visit(node: object, path_allowed: bool = True) -> None:
            if isinstance(node, dict):
                file_name = str(node.get("filename") or node.get("name") or node.get("file") or "")
                current_allowed = path_allowed
                if file_name and ("/" in file_name or "\\" in file_name or Path(file_name).suffix):
                    current_allowed = self._path_matches_targets(file_name, targets)
                for key, value in node.items():
                    lowered = str(key).lower()
                    if current_allowed:
                        if lowered in {"lines", "line"} and isinstance(value, dict):
                            add_metric("lines", value)
                        elif lowered in {"branches", "branch"} and isinstance(value, dict):
                            add_metric("branches", value)
                        elif "mcdc" in lowered and isinstance(value, dict):
                            add_metric("mcdc", value)
                    visit(value, current_allowed)
            elif isinstance(node, list):
                for item in node:
                    visit(item, path_allowed)

        visit(payload)
        return {key: value for key, value in totals.items() if value["count"] > 0}

    @staticmethod
    def _first_number(node: dict, names: tuple[str, ...]) -> float | None:
        for name in names:
            for key, value in node.items():
                if str(key).lower() == name:
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        return None
        return None

    @staticmethod
    def _percent_from_total(total: dict[str, float] | None) -> float | None:
        if not total:
            return None
        count = int(total.get("count", 0))
        covered = int(total.get("covered", 0))
        if count <= 0:
            return None
        return (covered / count) * 100.0

    @staticmethod
    def _extract_llvm_cov_report_metrics(text: str) -> dict[str, float | None]:
        metrics: dict[str, float | None] = {"C0": None, "C1": None, "MC/DC": None}
        for raw_line in text.splitlines():
            line = " ".join(raw_line.strip().split())
            if not line.startswith("TOTAL"):
                continue
            percentages = [float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*%", line)]
            if len(percentages) >= 5:
                metrics["C0"] = percentages[-3]
                metrics["C1"] = percentages[-2]
                metrics["MC/DC"] = percentages[-1]
            elif len(percentages) >= 3:
                metrics["C0"] = percentages[-3]
                metrics["C1"] = percentages[-2]
                metrics["MC/DC"] = percentages[-1]
        return metrics

    @staticmethod
    def _normalize_targets(target_files: list[str | Path] | None) -> list[str]:
        targets: list[str] = []
        for target in target_files or []:
            text = str(target).replace("\\", "/").strip()
            if not text:
                continue
            targets.append(text.rstrip("/"))
        return targets

    @staticmethod
    def _path_matches_targets(path: str, targets: list[str]) -> bool:
        if not targets:
            return True
        normalized = str(path).replace("\\", "/").rstrip("/")
        lowered = normalized.lower()
        path_parts = [part for part in lowered.split("/") if part]
        for target in targets:
            target_norm = str(target).replace("\\", "/").rstrip("/").lower()
            target_parts = [part for part in target_norm.split("/") if part]
            if lowered == target_norm:
                return True
            if target_norm and lowered.endswith("/" + target_norm):
                return True
            if len(target_parts) >= 2 and len(path_parts) >= 2 and path_parts[-2:] == target_parts[-2:]:
                return True
            if len(target_parts) == 1 and len(path_parts) == 1 and path_parts[-1] == target_parts[-1]:
                return True
        return False

    @staticmethod
    def _target_note(targets: list[str]) -> str:
        if not targets:
            return ""
        names = ", ".join(Path(target).name for target in targets)
        return f" for target file(s) only [{names}]"

    @staticmethod
    def _metric_notes(metrics: dict[str, float | None], source: str) -> dict[str, str]:
        notes: dict[str, str] = {}
        for metric, value in metrics.items():
            if value is None:
                notes[metric] = f"{metric} was not provided by {source}; Testing Mate will list generated obligations but cannot prove this metric without backend evidence."
        return notes
