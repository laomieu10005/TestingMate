"""Testing Mate GUI package."""

from testing_mate.generator import TestBackendGenerator, TestBackendRunner, backend_harness_path, format_run_result
from testing_mate.boolean_expression import BooleanExpressionEvaluator
from testing_mate.clang_analyzer import ClangEnhancedAnalyzer
from testing_mate.code_database import CodeDatabase, CodeFunctionRecord
from testing_mate.analysis import AstCache
from testing_mate.generation_workflow import ClassificationResult, Complexity, TestGenerationWorkflow, VerificationObjective, classify
from testing_mate.mc_coverage import ExecutedVector, MCDCAnalyzer, MCDCCoverageEngine, MCDCReport
from testing_mate.boolean_expression import ConditionEvaluator
from testing_mate.vector_generator import TestVectorGenerator

__version__ = "4.3-dev"

__all__ = [
    "BooleanExpressionEvaluator",
    "AstCache",
    "ClangEnhancedAnalyzer",
    "CodeDatabase",
    "CodeFunctionRecord",
    "ClassificationResult",
    "Complexity",
    "ConditionEvaluator",
    "ExecutedVector",
    "MCDCAnalyzer",
    "MCDCCoverageEngine",
    "MCDCReport",
    "TestBackendGenerator",
    "TestBackendRunner",
    "TestGenerationWorkflow",
    "TestVectorGenerator",
    "VerificationObjective",
    "__version__",
    "backend_harness_path",
    "classify",
    "format_run_result",
]
