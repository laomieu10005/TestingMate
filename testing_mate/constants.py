APP_NAME = "Testing Mate v5"
DEFAULT_COMPILER = "gcc"

# LLM Enhancement Stages (environment variable: TESTMATE_LLM_STAGES)
# Default stages: coverage_gap (existing), oracle (new), negative (new)
# Can be overridden via env var: coverage_gap,oracle,negative,compliance
# - coverage_gap: Close code coverage gaps detected from report feedback
# - oracle: Generate expected values (C-literal/enum/cast) for test oracles  
# - negative: Generate negative/security test cases (robustness/boundary/invalid-enum)
# - compliance: Generate compliance-specific test cases (ISO 26262/21434)
DEFAULT_LLM_STAGES = ["coverage_gap"]

# Source input support. C++ is accepted as experimental by the current regex analyzer;
# use compile_commands.json or clang integration for production-grade C++ parsing.
C_SOURCE_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx"}
HEADER_SOURCE_EXTENSIONS = {".h", ".hh", ".hpp", ".hxx"}
SOURCE_EXTENSIONS = C_SOURCE_EXTENSIONS | HEADER_SOURCE_EXTENSIONS
EXCLUDED_SOURCE_DIRS = {
    ".git", ".svn", ".hg", ".idea", ".vscode",
    "build", "out", "dist", "cmake-build-debug", "cmake-build-release",
    "node_modules", "__pycache__", "generated", "gen",
}

C_KEYWORDS = {
    "if",
    "else",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "do",
    "case",
    "break",
    "continue",
    "typedef",
    "struct",
    "union",
    "enum",
    "static",
    "extern",
    "const",
    "inline",
    "volatile",
}

DEFAULT_ENHANCEMENT_STAGES = ["coverage_gap", "oracle", "redundancy", "negative", "compliance"]

ISO_RECOMMENDATIONS: dict[str, dict[str, str]] = {
    "ASIL A": {"1j": "++", "1k": "++", "1l": "+", "1n": "+", "T8a": "++", "T8b": "+", "T8c": "+", "T8d": "+", "C0": "++", "C1": "+", "MC/DC": "+"},
    "ASIL B": {"1j": "++", "1k": "++", "1l": "+", "1n": "+", "T8a": "++", "T8b": "++", "T8c": "++", "T8d": "+", "C0": "++", "C1": "++", "MC/DC": "+"},
    "ASIL C": {"1j": "++", "1k": "++", "1l": "+", "1n": "++", "T8a": "++", "T8b": "++", "T8c": "++", "T8d": "+", "C0": "+", "C1": "++", "MC/DC": "+"},
    "ASIL D": {"1j": "++", "1k": "++", "1l": "++", "1n": "++", "T8a": "++", "T8b": "++", "T8c": "++", "T8d": "+", "C0": "+", "C1": "++", "MC/DC": "++"},
    "QM": {"1j": "+", "1k": "+", "T8a": "+", "T8b": "+", "T8c": "+", "T8d": "+", "C0": "+"},
}

ISO_EXECUTABLE_METHODS: dict[str, tuple[str, str]] = {
    "1j": ("Table 7", "Requirements-based test"),
    "1k": ("Table 7", "Interface test"),
    "1l": ("Table 7", "Fault injection test"),
    "1n": ("Table 7", "Back-to-back comparison test"),
    "T8a": ("Table 8", "Analysis of requirements"),
    "T8b": ("Table 8", "Generation and analysis of equivalence classes"),
    "T8c": ("Table 8", "Analysis of boundary values"),
    "T8d": ("Table 8", "Error guessing based on knowledge or experience"),
    "C0": ("Table 9", "Statement coverage"),
    "C1": ("Table 9", "Branch coverage"),
    "MC/DC": ("Table 9", "MC/DC"),
}
