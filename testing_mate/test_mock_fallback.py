import tempfile
import unittest
from pathlib import Path

from testing_mate.generator import TestMockFallback
from testing_mate.models import RunResult


class MockFallbackTests(unittest.TestCase):
    def test_cpputest_unfulfilled_expectation_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            source = out_dir / "SafetySwc.c"
            test_dir = out_dir / "SafetySwc_cpputest"
            test_dir.mkdir(parents=True)
            test_source = test_dir / "SafetySwc_cpputest_test.cpp"
            test_source.write_text(
                """
TEST(SafetySwc_McdcBrakeAssist, SafetySwc_McdcBrakeAssist_mc_dc_001)
{
    /* PRECONDITION */
    mock().expectOneCall("Dem_SetEventStatus").ignoreOtherParameters();
    CHECK(TestingMate_Dem_SetEventStatus_CallCount() > 0U);
    /* ACTIONS */
    SafetySwc_McdcBrakeAssist();
}
""".lstrip(),
                encoding="utf-8",
            )
            run_result = RunResult(
                backend="CppUTest",
                command="run_cpputest",
                returncode=1,
                stdout="""
TEST(SafetySwc_McdcBrakeAssist, SafetySwc_McdcBrakeAssist_mc_dc_001)
    Mock Failure: Expected call WAS NOT fulfilled.
    EXPECTED calls that WERE NOT fulfilled:
        Dem_SetEventStatus -> all parameters ignored (expected 1 call, called 0 times)
""",
            )

            updated, notes = TestMockFallback.apply("CppUTest", out_dir, source, run_result)

            patched = test_source.read_text(encoding="utf-8")
            self.assertEqual(updated, 1)
            self.assertTrue(any("removed redundant expectation" in note for note in notes))
            self.assertNotIn('expectOneCall("Dem_SetEventStatus")', patched)
            self.assertNotIn("TestingMate_Dem_SetEventStatus_CallCount()", patched)

    def test_gtest_unexpected_mock_call_is_added_with_typed_default_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            source = out_dir / "GatewaySwc.c"
            test_dir = out_dir / "GatewaySwc_gtest"
            stub_include = test_dir / "stub" / "include"
            stub_include.mkdir(parents=True)
            (stub_include / "stubs.h").write_text(
                "class TestingMateGMockHarness {\n"
                " public:\n"
                "  MOCK_METHOD(uint8, Dem_SetEventStatus, (uint16, uint8), ());\n"
                "};\n",
                encoding="utf-8",
            )
            test_source = test_dir / "GatewaySwc_gtest_test.cpp"
            test_source.write_text(
                """
TEST(GatewaySwc, GatewaySwc_case_001)
{
    /* PRECONDITION */
    /* ACTIONS */
    GatewaySwc_Main();
}
""".lstrip(),
                encoding="utf-8",
            )
            run_result = RunResult(
                backend="GoogleTest",
                command="run_gtest",
                returncode=1,
                stdout="""
[ RUN      ] GatewaySwc.GatewaySwc_case_001
Unexpected mock function call - returning default value.
    Function call: Dem_SetEventStatus(1, 2)
""",
            )

            updated, notes = TestMockFallback.apply("GoogleTest", out_dir, source, run_result)

            patched = test_source.read_text(encoding="utf-8")
            self.assertEqual(updated, 1)
            self.assertTrue(any("added missing expectation" in note for note in notes))
            self.assertIn(
                "EXPECT_CALL(TestingMate_Mock(), Dem_SetEventStatus(::testing::_, ::testing::_)).WillOnce(::testing::Return(0));",
                patched,
            )


if __name__ == "__main__":
    unittest.main()
