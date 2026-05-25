# Testing Mate v5

Testing Mate is a Windows-friendly Python GUI for analyzing C projects, generating coverage-oriented unit test candidates, building GoogleTest or CppUTest harnesses, running those harnesses under MSYS2/MinGW, and collecting audit-ready reports.

The tool is designed for deterministic analysis first. Local LLM refinement is optional and runs after an initial report or selected report file exists.

## Quick Start

Run the GUI from the repository root:

```powershell
python .\TestingMate.py
```

The package entry point opens the same GUI:

```powershell
python -m testing_mate
```

Recommended first run:

1. Click `Compile DB` and select `compile_commands.json`, or use `Add Source` to add files or a folder.
2. Select coverage mode: `C0/C1` or `MCDC`.
3. Select level: `UT`, `IT`, or `ST`.
4. Select backend: `GoogleTest` or `CppUTest`.
5. Enable `Run after generate` when the MSYS2/MinGW toolchain is available.
6. Click `Generate`.
7. Use `Open Report` to view the latest generated HTML report.

## Current Pipeline

```text
C source/header inputs or compile_commands.json
  -> optional preprocessing
  -> Clang AST analysis
  -> internal analyzer fallback when Clang cannot parse
  -> code database generation
  -> path/vector/testcase generation
  -> GoogleTest or CppUTest harness generation
  -> optional backend build and execution
  -> lcov or llvm-cov coverage collection
  -> Testing Mate HTML/JSON/CSV/markdown reports
  -> optional Ollama report-driven refinement
  -> optional rerun and refreshed report
```

## Input Model

Testing Mate accepts:

- Source files: `.c`, `.cc`, `.cpp`, `.cxx`
- Header files: `.h`, `.hh`, `.hpp`, `.hxx`
- Source folders, scanned recursively
- `compile_commands.json` from CMake/Ninja or another build integration

Folder scanning skips common generated/build folders such as `.git`, `build`, `out`, `dist`, `generated`, `gen`, `node_modules`, and `__pycache__`.

For production use, prefer `compile_commands.json`. It lets Testing Mate import real source files, include paths, defines, and compiler context instead of relying on manual GUI fields.

## Analysis Engine

For each function, the engine tries to extract:

- Function name, return type, parameters, and source line range
- Local variables and globals read/written when available
- Function calls and external call hints
- Conditions from `if`, loops, switches, and decision expressions
- Return paths and execution-path hints
- Data-dependency hints
- Mock/stub requirements

Primary analysis uses Clang AST JSON. If `clang` is unavailable or parsing fails, Testing Mate falls back to the internal analyzer and records that in `<source>_code_database.json`.

## Coverage Modes

`C0/C1`:

- Generates statement and branch-oriented test vectors.
- Reads executed C0/C1 from backend coverage when available.
- Falls back to generated-vector C1 evaluation when backend branch counters are absent.
- If no branch/decision obligations exist, C1 is marked complete by definition.

`MCDC`:

- Generates MC/DC-oriented vectors.
- Uses `llvm-cov` MC/DC information when the clang coverage backend is selected and available.
- Falls back to Testing Mate generated-vector MC/DC analysis when backend MC/DC proof is unavailable.
- If no branch/condition obligations exist, MC/DC is marked complete by definition.

Generated-vector coverage is useful engineering evidence, but executed backend coverage is the stronger evidence. Review expected values and safety-critical oracles before relying on generated cases for sign-off.

## Test Levels

`UT`:

- Unit-level tests for selected functions.
- Main target for GoogleTest and CppUTest harnesses.

`IT`:

- Adds integration-oriented cases where calls, interfaces, pointer fields, and data-flow hints are detected.
- Useful for AUTOSAR-style module interaction evidence.

`ST`:

- System-level evidence classification in generated metadata and reports.
- It does not replace a system test environment.

## ISO 26262 Profile

Enable `ISO 26262 method profile` to generate additional method-oriented cases based on the selected ASIL level:

- `QM`
- `ASIL A`
- `ASIL B`
- `ASIL C`
- `ASIL D`

The profile is generic. It is driven by analyzed function facts and policy tables, not by sample-project names. Executable methods include requirements-based tests, interface tests, fault-injection candidates, equivalence classes, boundary values, error guessing, C0, C1, and MC/DC-oriented vectors. Non-executable methods remain audit/review evidence.

## Backends

Testing Mate currently generates runnable harnesses for:

- `GoogleTest`
- `CppUTest`

Each backend writes a CMake project with:

- Copied target source/header files
- Project-linked source files needed by the selected harness
- Generated test file
- Generated stub source and header under `stub/src` and `stub/include`
- `CMakeLists.txt`
- `run_gtest.sh` or `run_cpputest.sh`

Generated stubs are based on declarations found from headers and analyzed call requirements when available. Stub files are excluded from target coverage intent; the report is focused on production source coverage.

## MSYS2 / MinGW Toolchain

Testing Mate looks for tools in this order:

1. The repository-local tool directory: `mingw64\bin`
2. `C:\msys64\mingw64\bin`
3. `C:\msys64\ucrt64\bin`
4. `C:\msys64\clang64\bin`
5. `C:\msys64\usr\bin`
6. Extra paths from `TESTMATE_TOOLCHAIN_PATH`

To add another tool path:

```powershell
$env:TESTMATE_TOOLCHAIN_PATH = "D:\tools\msys64\mingw64\bin"
python .\TestingMate.py
```

Generated backend runners are MSYS2 bash scripts. They use the repository-local `mingw64\bin` first when it exists.

If required tools are missing, generated runners try to install them with MSYS2 `pacman` and retry:

- `mingw-w64-x86_64-cmake`
- `mingw-w64-x86_64-ninja`
- `mingw-w64-x86_64-gcc`
- `mingw-w64-x86_64-clang`
- `mingw-w64-x86_64-llvm`
- `mingw-w64-x86_64-lcov` or `lcov`
- `perl`

If `pacman` is not available, the run fails with a clear error. The GUI stops the current operation and shows an error popup.

Backend command timeout defaults to 900 seconds. Override it with:

```powershell
$env:TESTING_MATE_RUN_TIMEOUT = "1800"
```

## Coverage Collection

Default backend coverage uses GCC plus `lcov`/`genhtml`:

- `coverage.raw.info`
- target-filtered `coverage.info`
- `<source>_lcov/index.html`

Clang coverage can be selected through runner environment variables:

```bash
export TESTING_MATE_COVERAGE_BACKEND=clang_llvmcov
```

Clang coverage produces:

- `coverage.profdata`
- `coverage.summary.txt`
- `coverage.llvm.json`
- `<source>_llvmcov/index.html`
- optional `coverage.info` when `llvm2lcov` is available

Coverage records are filtered to the selected target source when possible, so generated tests and stubs do not inflate production coverage metrics.

## Generated Artifacts

For each selected source file, Testing Mate writes artifacts under:

```text
TestMate_out\<source-or-project>\<backend>\
```

Important files:

- `<source>_code_database.json`: analyzer facts, calls, variables, paths, dependencies, and mock requirements
- `<source>_analysis.json`: functions, parameters, conditions, and generated cases
- `<source>_enhancement_request.json`: compact handoff for LLM or later optimization
- `<source>_<backend>_cases.csv`: audit-friendly testcase export
- `<source>_summary.md`: generated summary and coverage metrics
- `<source>.html`: main interactive report
- `<source>_<backend>_audit_checklist.md`: review and closure checklist
- `<source>_gtest\` or `<source>_cpputest\`: backend CMake harness

When multiple files are selected, Testing Mate also writes:

```text
all_files_report.html
```

This report collects the final status for all processed files.

## Main GUI Actions

`Analyze & Generate`:

- Runs analysis and generation.
- Optionally builds/runs the selected backend when `Run after generate` is enabled.
- Stops immediately on build/test/execution failure and shows an error popup.

`Rerun`:

- Reuses the existing generated backend environment.
- Reruns manually edited or previously generated testcases.
- Recollects backend coverage and refreshes reports.
- Stops on the first test or execution failure.

`Refine From Report & Run`:

- Requires a selected report or coverage/failure file.
- Runs Ollama refinement from that report.
- Regenerates backend artifacts.
- Runs the backend and refreshes coverage reports.

`Open Report`:

- Opens the latest available HTML report beside the main action buttons.

## Ollama Refinement

Ollama is optional. Install and start it separately:

```powershell
ollama pull qwen2.5-coder:3b
ollama serve
```

Testing Mate does not use the LLM as the first analyzer. The LLM receives small report-driven chunks only after deterministic generation has produced cases and coverage evidence. Prompts include:

- One target function
- The matching code database facts
- Recent generated cases
- Specific uncovered line/branch/MC/DC/failure evidence
- Allowed parameter and pointer-field names

The generated `<source>_enhancement_request.json` includes an `ollama_handoff` section with ready-to-send chunks. Each chunk carries a `chunk_id`, `target_gap`, compact `coverage_feedback_chunk`, dedupe signatures, acceptance rules, and the expected response contract.

Accepted LLM output is strict JSON. `test_cases`, `cases`, or `vectors` arrays are accepted, including replies wrapped inside a `coverage_enhancement` object. Cases that reference unknown functions or invalid symbols are rejected.

## Reports and Metrics

HTML reports include:

- Source and backend identity
- Function list and condition list
- C0, C1, and MC/DC metric cards
- Metric notes describing whether data came from executed coverage or generated-vector analysis
- Uncovered items and backend failures
- Test cases run, using backend output when available
- Links to backend coverage artifacts
- Captured run stdout/stderr

The CSV, JSON, markdown, and HTML outputs are intended to support review, traceability, and ASPICE/ISO 26262 evidence workflows. Generated expected values may be placeholders when the tool cannot safely infer a real oracle.

## Useful Environment Variables

`TESTMATE_TOOLCHAIN_PATH`:

- Adds custom toolchain paths to command execution.

`TESTING_MATE_COVERAGE_BACKEND`:

- `gcc_lcov` by default.
- `clang`, `clang_llvmcov`, or `llvmcov` to use clang/llvm-cov flow.

`TESTING_MATE_CC` and `TESTING_MATE_CXX`:

- Override compiler names inside generated runners.

`TESTING_MATE_RUN_TIMEOUT`:

- Overrides backend command timeout in seconds.

`TESTMATE_LLM_STAGES`:

- Optional future-facing control for LLM enhancement stages.

## Limitations

Testing Mate generates a strong first pass, not a final safety certification package. Review is required for:

- Exact expected-result oracles
- Pointers, arrays, structs, unions, and hardware registers
- Complex macros and build-specific include behavior
- External globals and side effects
- Function pointers and callback-heavy designs
- Coupled MC/DC decisions
- Integration behavior across real RTE/BSW/MCAL layers
- C++ parsing, which is accepted but still experimental compared with C

### Recent fixes

- Improved MC/DC independence-pair search: the analyzer now attempts a relaxed
  masked-pair acceptance when strict unique-cause pairs are not available,
  using evidence from available vectors to identify masked atomic conditions.
  This reduces false negatives in MC/DC calculation while keeping strict
  proof as the primary criterion. See `testing_mate/test_c1_mcdc.py` for
  regression tests covering C1/MC/DC relationships.

Path generation is intentionally prioritized rather than exhaustive. The tool focuses on return paths, error paths, boundary paths, branches with calls, global writes, interface paths, diagnostic paths, and uncovered paths to avoid path explosion.

## Troubleshooting

`Clang analysis skipped`:

- Install clang in MSYS2, or keep using the internal analyzer fallback.
- Check include paths and defines from `compile_commands.json`.

`MSYS2 bash was not found`:

- Install MSYS2, or place a usable MSYS2 toolchain under `mingw64\bin`.
- Ensure `C:\msys64\usr\bin\bash.exe` exists or set `TESTMATE_TOOLCHAIN_PATH`.

Missing `gcc`, `clang`, `lcov`, `genhtml`, `cmake`, or `ninja`:

- Regenerate the backend runner and run again. The runner will try to install missing packages with `pacman`.
- If `pacman` is unavailable, install the package manually in MSYS2.

Coverage lines look wrong:

- Regenerate and rerun so the latest target-source filtering is used.
- Check that the selected source file matches the copied source inside the backend harness.
- Prefer `compile_commands.json` when macros/includes change line mapping.

Generated tests compile but fail:

- Check placeholders in expected values.
- Inspect generated stubs under `stub/src` and `stub/include`.
- Use `Rerun` after manual testcase or stub edits.

Large generated harness fails on MinGW:

- ASIL D or large MC/DC projects can create large C++ objects.
- Use a smaller target subset or split files when needed.
