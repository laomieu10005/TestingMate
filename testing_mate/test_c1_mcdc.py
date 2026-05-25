import unittest
from itertools import combinations

from testing_mate.c1_engine import C1CoverageEngine
from testing_mate.mcdc_engine import MCDCCoverageEngine
from testing_mate.models import Condition, CoverageFeedback, FunctionInfo, Parameter, TestCase
from testing_mate.reporter import CoverageReportParser
from testing_mate.utilities import collect_backend_feedback, merge_generated_coverage_feedback


def make_function():
    condition = Condition(text="a && b", line=10, decisions=["a", "b"])
    return FunctionInfo(
        name="fn",
        return_type="void",
        parameters=[Parameter(name="a", ctype="int"), Parameter(name="b", ctype="int")],
        start_line=1,
        end_line=20,
        body="",
        conditions=[condition],
    )


def make_case(test_id, a, b):
    return TestCase(
        test_id=test_id,
        unit="",
        function="fn",
        coverage="C1",
        level="",
        values={"a": str(int(a)), "b": str(int(b))},
        expected="",
        rationale="",
    )


class CoverageMetricsTests(unittest.TestCase):
    def test_c1_atomic_decisions_are_counted_per_atom(self):
        function = make_function()
        cases = [
            make_case("t1", True, False),
            make_case("t2", False, True),
            make_case("t3", True, True),
        ]

        feedback = C1CoverageEngine().analyze([function], cases)
        self.assertEqual(feedback.metrics.get("C1"), 100.0)
        self.assertIn("atomic decision `a`", feedback.raw_text)
        self.assertIn("atomic decision `b`", feedback.raw_text)

    def test_mcdc_pairs_require_independent_effect_for_atomic_decisions(self):
        function = make_function()
        cases = [
            make_case("t1", True, False),
            make_case("t2", False, True),
            make_case("t3", True, True),
        ]

        feedback = MCDCCoverageEngine().analyze([function], cases)
        self.assertEqual(feedback.metrics.get("MC/DC"), 100.0)

    def test_mcdc_never_exceeds_c1_for_any_case_subset(self):
        function = make_function()
        all_cases = [
            make_case("t1", False, False),
            make_case("t2", True, False),
            make_case("t3", False, True),
            make_case("t4", True, True),
        ]

        for size in range(1, len(all_cases) + 1):
            for subset in combinations(all_cases, size):
                c1_feedback = C1CoverageEngine().analyze([function], list(subset))
                mc_feedback = MCDCCoverageEngine().analyze([function], list(subset))
                c1 = c1_feedback.metrics.get("C1") or 0.0
                mc = mc_feedback.metrics.get("MC/DC") or 0.0
                self.assertLessEqual(
                    mc,
                    c1 + 1e-9,
                    f"MC/DC {mc} should not exceed C1 {c1} for subset {[c.values for c in subset]}",
                )

    def test_generated_mcdc_does_not_fill_official_report_without_llvm_mcdc(self):
        function = make_function()
        cases = [
            make_case("t1", False, False),
            make_case("t2", True, False),
            make_case("t3", False, True),
            make_case("t4", True, True),
        ]
        executed = CoverageFeedback(
            source="lcov tracefile",
            raw_text="TN:\nSF:sample.c\nLF:10\nLH:5\nBRF:4\nBRH:2\nend_of_record\n",
            coverage_percent=50.0,
            metrics={"C0": 50.0, "C1": 50.0, "MC/DC": None},
            metric_notes={"MC/DC": "lcov did not emit MC/DC counters."},
        )

        feedback = merge_generated_coverage_feedback(executed, [function], cases, "MC/DC")

        self.assertIsNone(feedback.metrics.get("MC/DC"))
        self.assertIn("LLVM native MC/DC records were not found", feedback.metric_notes.get("MC/DC", ""))

    def test_backend_collection_prefers_llvm_summary_mcdc_over_lcov_missing_mcdc(self):
        with self.subTest("GoogleTest llvm summary is collected before lcov"):
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as tmp:
                out_dir = Path(tmp)
                source = out_dir / "GatewaySwc.c"
                base = out_dir / "GatewaySwc_gtest"
                base.mkdir(parents=True)
                (base / "coverage.summary.txt").write_text(
                    "Filename Regions Missed Regions Cover Functions Missed Functions Executed Lines Missed Lines Cover Branches Missed Branches Cover MC/DC Cover\n"
                    "TOTAL 10 0 100.00% 1 0 100.00% 20 0 100.00% 8 2 75.00% 5 1 80.00%\n",
                    encoding="utf-8",
                )
                (base / "coverage.info").write_text(
                    "TN:\nSF:GatewaySwc.c\nLF:10\nLH:5\nBRF:4\nBRH:2\nend_of_record\n",
                    encoding="utf-8",
                )

                feedback = collect_backend_feedback(CoverageReportParser(), out_dir, source, "GoogleTest")

                self.assertIsNotNone(feedback)
                self.assertEqual(feedback.metrics.get("MC/DC"), 80.0)
                self.assertIn("llvm-cov", feedback.metric_notes.get("MC/DC", ""))

    def test_lcov_mcdc_records_are_not_used_as_official_mcdc(self):
        text = (
            "TN:\n"
            "SF:GatewaySwc.c\n"
            "LF:2\n"
            "LH:2\n"
            "BRF:2\n"
            "BRH:2\n"
            "MRF:2\n"
            "MRH:2\n"
            "MCDC:10,2,0,1,0,a && b\n"
            "end_of_record\n"
        )

        feedback = CoverageReportParser().parse_text(text, "coverage.info")

        self.assertEqual(feedback.metrics.get("C0"), 100.0)
        self.assertEqual(feedback.metrics.get("C1"), 100.0)
        self.assertIsNone(feedback.metrics.get("MC/DC"))
        self.assertIn("LCOV/gcov branch counters", feedback.metric_notes.get("MC/DC", ""))

    def test_llvm_json_mcdc_is_parsed_as_native_coverage(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coverage.json"
            path.write_text(
                json.dumps(
                    {
                        "data": [
                            {
                                "files": [
                                    {
                                        "filename": "GatewaySwc.c",
                                        "summary": {
                                            "lines": {"count": 10, "covered": 9, "percent": 90.0},
                                            "branches": {"count": 8, "covered": 6, "percent": 75.0},
                                            "mcdc": {"count": 5, "covered": 4, "percent": 80.0},
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            feedback = CoverageReportParser().parse_file(path, target_files=["GatewaySwc.c"])

            self.assertEqual(feedback.metrics.get("C0"), 90.0)
            self.assertEqual(feedback.metrics.get("C1"), 75.0)
            self.assertEqual(feedback.metrics.get("MC/DC"), 80.0)
            self.assertIn("llvm-cov JSON", feedback.metric_notes.get("MC/DC", ""))

    def test_llvm_json_without_mcdc_returns_native_missing_note(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coverage.json"
            path.write_text(
                json.dumps(
                    {
                        "data": [
                            {
                                "files": [
                                    {
                                        "filename": "GatewaySwc.c",
                                        "summary": {
                                            "lines": {"count": 10, "covered": 9, "percent": 90.0},
                                            "branches": {"count": 8, "covered": 6, "percent": 75.0},
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            feedback = CoverageReportParser().parse_file(path, target_files=["GatewaySwc.c"])

            self.assertEqual(feedback.metrics.get("C0"), 90.0)
            self.assertEqual(feedback.metrics.get("C1"), 75.0)
            self.assertIsNone(feedback.metrics.get("MC/DC"))
            self.assertIn("no mc/dc records were found", feedback.metric_notes.get("MC/DC", "").lower())

    def test_llvm_export_json_list_root_parses_metrics(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coverage.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "files": [
                                {
                                    "filename": "GatewaySwc.c",
                                    "summary": {
                                        "lines": {"count": 10, "covered": 9, "percent": 90.0},
                                        "branches": {"count": 8, "covered": 6, "percent": 75.0},
                                        "mcdc": {"count": 5, "covered": 4, "percent": 80.0},
                                    },
                                }
                            ]
                        }
                    ]
                ),
                encoding="utf-8",
            )

            feedback = CoverageReportParser().parse_file(path, target_files=["GatewaySwc.c"])

            self.assertEqual(feedback.metrics.get("C0"), 90.0)
            self.assertEqual(feedback.metrics.get("C1"), 75.0)
            self.assertEqual(feedback.metrics.get("MC/DC"), 80.0)
            self.assertIn("llvm-cov JSON", feedback.metric_notes.get("MC/DC", ""))


if __name__ == "__main__":
    unittest.main()
