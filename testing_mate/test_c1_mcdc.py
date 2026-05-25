import unittest
from itertools import combinations

from testing_mate.c1_engine import C1CoverageEngine
from testing_mate.mc_coverage import MCDCCoverageEngine
from testing_mate.models import Condition, FunctionInfo, Parameter, TestCase


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


if __name__ == "__main__":
    unittest.main()
