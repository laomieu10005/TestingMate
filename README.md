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
5. Set `Retry` if you want Testing Mate to patch expected values or mock expectations after a failed backend run. The value is capped at `5`.
6. Enable `Run after generate` when the MSYS2/MinGW toolchain is available.
7. Click `Generate`.
8. Use `Open Report` to view the latest generated HTML report.

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
  -> optional expected-value/mock fallback retry loop
  -> clang/llvm-cov source-based coverage collection
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
- Reads executed C0/C1 from LLVM source-based line and branch coverage when available.
- Falls back to generated-vector C1 evaluation when backend branch counters are absent.
- If no branch/decision obligations exist, C1 is marked complete by definition.

`MCDC`:

- Generates MC/DC-oriented vectors.
- Uses LLVM native MC/DC from `-fcoverage-mcdc` and `llvm-cov`.
- Does not use LCOV/gcov branch counters or generated vectors as official MC/DC coverage.
- If LLVM coverage exists but no MC/DC records are exported, MC/DC is shown as `N/A` with a reason.
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

Mock behavior:

- CppUTest stubs use `CppUTestExt/MockSupport.h`.
- GoogleTest stubs expose a generated GMock harness in `stub/include/stubs.h`.
- Mock parameters and return values follow the best available function declaration. When no declaration is visible, Testing Mate emits a reviewed fallback stub type so the harness remains buildable.
- Generated expectations are path-aware when call guards can be evaluated. A mock is expected only for test vectors that should reach the guarded call.

After a failed backend run, Testing Mate can apply a small fallback loop:

- Update expected values from parsed expected/actual assertion failures.
- Remove redundant mock expectations that were never fulfilled.
- Add missing expectations for unexpected mock calls.
- Rerun the backend, up to the GUI `Retry` value, capped at `5`.

## MSYS2 / MinGW Toolchain

Testing Mate uses the default MSYS2 MinGW64 installation:

```text
C:\msys64\mingw64
```

Generated backend runners are MSYS2 bash scripts. They put `C:\msys64\mingw64\bin` and `C:\msys64\usr\bin` on the runner `PATH`.

If `C:\msys64\mingw64` is not found, the GUI opens a folder picker and asks for the MSYS2 `mingw64` folder. Select a folder such as:

```text
C:\msys64\mingw64
```

If required tools are missing, generated runners try to install them with MSYS2 `pacman` and retry:
pacman -S --needed mingw-w64-x86_64-clang mingw-w64-x86_64-llvm mingw-w64-x86_64-compiler-rt
pacman -S --needed mingw-w64-clang-x86_64-toolchain mingw-w64-clang-x86_64-cmake mingw-w64-clang-x86_64-ninja
- `mingw-w64-x86_64-cmake`
- `mingw-w64-x86_64-ninja`
- `mingw-w64-x86_64-clang`
- `mingw-w64-x86_64-llvm`
mingw-w64-x86_64-compiler-rt
- `mingw-w64-x86_64-lcov` or `lcov` only for explicit legacy lcov mode

If `pacman` is not available, the run fails with a clear error. The GUI stops the current operation and shows an error popup.

Backend command timeout defaults to 900 seconds. Override it with:

```powershell
$env:TESTING_MATE_RUN_TIMEOUT = "1800"
```

## Coverage Collection

Default backend coverage uses Clang/LLVM source-based coverage:

- `coverage.profdata`
- `llvm_report.txt`
- `coverage.json`
- `llvm_html/index.html`

The generated CMake harness builds with:

```text
-fprofile-instr-generate
-fcoverage-mapping
-fcoverage-mcdc
```

The runner uses:

- `llvm-profdata merge -sparse`
- `llvm-cov report -show-branch-summary -show-mcdc-summary`
- `llvm-cov show -format=html -show-branches=count -show-mcdc`
- `llvm-cov export -format=text`

Legacy lcov can still be requested for C0/C1 only:

```bash
export TESTING_MATE_COVERAGE_BACKEND=legacy_lcov
```

LCOV/gcov branch counters are never used for MC/DC. Coverage records are filtered to the selected target source when possible, so generated tests and stubs do not inflate production coverage metrics.

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
- `<source>_gtest\coverage.json` or `<source>_cpputest\coverage.json`: LLVM coverage export after backend run
- `<source>_gtest\llvm_html\index.html` or `<source>_cpputest\llvm_html\index.html`: LLVM source coverage report after backend run

When multiple files are selected, Testing Mate also writes:

```text
all_files_report.html
```

This report collects the final status for all processed files.

## Main GUI Actions

`Analyze & Generate`:

- Runs analysis and generation.
- Optionally builds/runs the selected backend when `Run after generate` is enabled.
- Applies the expected-value/mock fallback retry loop when a backend run fails and `Retry` is greater than `0`.
- Stops on build/test/execution failure that cannot be patched by the retry loop and shows an error popup.

`Rerun`:

- Reuses the existing generated backend environment.
- Reruns manually edited or previously generated testcases.
- Recollects backend coverage and refreshes reports.
- Applies the same expected-value/mock fallback retry loop when enabled.
- Stops on the first test or execution failure that cannot be patched by the retry loop.

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

The GUI `Timeout` field beside `Use local Ollama` controls the per-request Ollama timeout in seconds. The minimum is `5`. Environment variables such as `TESTMATE_OLLAMA_NUM_CTX`, `TESTMATE_OLLAMA_NUM_PREDICT`, and `TESTMATE_LLM_MAX_GAPS` can still be used to reduce prompt size or output length for large projects.

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

Metric interpretation:

- `Report Coverage` is the best available executed report percentage.
- `C0` comes from LLVM line coverage.
- `C1` comes from LLVM branch coverage.
- `MC/DC` comes only from LLVM native MC/DC records produced by `-fcoverage-mcdc`.
- Legacy lcov mode can provide C0/C1 only; MC/DC is `N/A` because LCOV/gcov branch counters do not contain executed condition vectors.

## Useful Environment Variables

`TESTING_MATE_COVERAGE_BACKEND`:

- `llvm` by default.
- `legacy_lcov` to use old lcov C0/C1 fallback. MC/DC remains `N/A` in legacy lcov mode.

`TESTING_MATE_CC` and `TESTING_MATE_CXX`:

- Override compiler names inside generated runners.

`TESTING_MATE_RUN_TIMEOUT`:

- Overrides backend command timeout in seconds.

`TESTMATE_OLLAMA_TIMEOUT`:

- Overrides the local Ollama request timeout. When set, it takes precedence over the GUI `Timeout` field.

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

- Reported MC/DC now comes only from LLVM native MC/DC counters. If the backend coverage artifact has only lcov line/branch counters, MC/DC is `N/A`.
- Backend retry now handles both expected-value mismatches and mock expectation failures. It can remove unreachable expectations, add missing expectations from backend mock logs, and rerun up to the GUI retry cap.
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

- Install MSYS2 with MinGW64 support.
- Ensure `C:\msys64\mingw64\bin` and `C:\msys64\usr\bin\bash.exe` exist.
- If MSYS2 is installed somewhere else, run the GUI action again and select the correct `mingw64` folder when prompted.

Missing `clang`, `llvm-cov`, `llvm-profdata`, `cmake`, or `ninja`:

- Regenerate the backend runner and run again. The runner will try to install missing packages with `pacman`.
- If `pacman` is unavailable, install the package manually in MSYS2.

Coverage lines look wrong:

- Regenerate and rerun so the latest target-source filtering is used.
- Check that the selected source file matches the copied source inside the backend harness.
- Prefer `compile_commands.json` when macros/includes change line mapping.

MC/DC looks too high:

- Check the metric note under the MC/DC card.
- Executed MC/DC requires LLVM coverage artifacts with MC/DC records.
- Standard gcov/lcov line and branch reports do not prove MC/DC and are shown as `N/A` for MC/DC.
- If LLVM coverage exists but MC/DC is `N/A`, rebuild and rerun so target sources are compiled with `-fcoverage-mcdc`.

Generated tests compile but fail:

- Check placeholders in expected values.
- Inspect generated stubs under `stub/src` and `stub/include`.
- Use `Rerun` after manual testcase or stub edits.

Mock failures remain after retry:

- Inspect the generated `stub/include/stubs.h` declaration for the mocked function signature.
- Check whether the real call is guarded by conditions that are not satisfied by the testcase.
- Reduce `Retry` to `0` when you want to inspect the first raw backend failure without automatic patching.

Large generated harness fails on MinGW:

- ASIL D or large MC/DC projects can create large C++ objects.
- Use a smaller target subset or split files when needed.
