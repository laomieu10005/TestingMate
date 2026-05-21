# LLM Usage Guide for Testing Mate

## Overview

Testing Mate uses LLMs (via Ollama) as an **optional refinement layer** after deterministic analysis, not as the primary analyzer. This approach ensures reliability while leveraging AI for coverage gap closure and test enhancement.

## Core Philosophy

**Deterministic First, LLM Second**
- Primary analysis uses Clang AST + internal analyzer
- LLM receives small, focused chunks with specific context
- LLM output is validated and merged with existing cases
- Generated tests remain audit-ready and traceable

## Current LLM Integration Architecture

### 1. **OllamaClient** (Lines 432-531)
The HTTP client that communicates with local Ollama instance.

**Key Features:**
- JSON-only responses (enforced via `format: "json"`)
- Configurable timeout, context window, and prediction length
- Automatic JSON extraction from markdown-wrapped responses
- Error handling for timeouts, HTTP errors, and connection failures

**Environment Variables:**
```bash
TESTMATE_OLLAMA_HOST=http://localhost:11434
TESTMATE_OLLAMA_TIMEOUT=45              # seconds
TESTMATE_OLLAMA_NUM_CTX=4096            # context window
TESTMATE_OLLAMA_NUM_PREDICT=512         # max tokens
TESTMATE_OLLAMA_AUTO_PULL=false         # auto-pull model
```

### 2. **OllamaTestRefiner** (Lines 533-1100+)
The prompt engineering and test case generation engine.

**Three Main Methods:**

#### a) `refine()` - Initial Generation Enhancement
- Used when no coverage report exists yet
- Generates additional test vectors per function
- Limited to small chunks (max 3 test cases per request)
- Focuses on function parameters and conditions

#### b) `refine_gap()` - Coverage Gap Closure
- Used when coverage report shows gaps
- Targets specific uncovered lines/branches/MC/DC conditions
- Includes code database context and pointer field hints
- Most precise and effective mode

#### c) `refine_oracle()` - Expected Value Generation
- Generates realistic expected outputs for non-void functions
- Validates outputs are C-literals, enums, or valid casts
- Tagged as "ORACLE:" for human review
- **NEW STAGE** - Enable via `TESTMATE_LLM_STAGES`

#### d) `refine_negative()` - Security/Robustness Testing
- Generates negative test cases (NULL, overflow, invalid enums)
- Aligned with ISO/SAE 21434 security testing
- Tagged as "SECURITY:" for audit trail
- **NEW STAGE** - Enable via `TESTMATE_LLM_STAGES`

### 3. **Multi-Stage Refinement Pipeline** (Lines 2667-2815)

The `refine_with_ollama()` function orchestrates multiple enhancement stages:

```python
# Controlled by environment variable
TESTMATE_LLM_STAGES=coverage_gap,oracle,negative
```

**Stage Execution Order:**
1. **coverage_gap** (default) - Close coverage gaps from report
2. **oracle** (optional) - Generate expected values
3. **negative** (optional) - Generate security/robustness tests

**Stage Control:**
```bash
# Default: only coverage gap closure
TESTMATE_LLM_STAGES=coverage_gap

# Enable all stages
TESTMATE_LLM_STAGES=coverage_gap,oracle,negative

# Only oracle generation
TESTMATE_LLM_STAGES=oracle
```

### 4. **CoverageGapPlanner** (Lines 345-429)
Analyzes coverage feedback to identify specific gaps.

**Gap Identification:**
- Parses uncovered items from lcov/llvm-cov reports
- Extracts function name, line number, metric (C0/C1/MC/DC)
- Matches gaps to function conditions
- Prioritizes and deduplicates gaps

## Effective Usage Strategies

### Strategy 1: Coverage-Driven Refinement (Recommended)

**Workflow:**
```powershell
# 1. Initial generation without LLM
python TestingMate.py
# - Select source files
# - Choose coverage mode (C0/C1 or MC/DC)
# - Enable "Run after generate"
# - Click "Analyze & Generate"

# 2. Review initial coverage report
# - Check coverage metrics
# - Identify uncovered branches/conditions

# 3. Refine with LLM targeting gaps
# - Select the generated HTML report
# - Enable "Use local Ollama"
# - Set model (e.g., qwen2.5-coder:3b)
# - Click "Refine From Report & Run"
```

**Why This Works:**
- LLM receives concrete coverage gaps, not abstract requirements
- Prompts include actual uncovered line numbers and conditions
- Code database provides function context and allowed symbols
- Iterative refinement with validation

### Strategy 2: Multi-Stage Enhancement

**Enable Multiple Stages:**
```powershell
# Set environment variable before running
$env:TESTMATE_LLM_STAGES = "coverage_gap,oracle,negative"
python TestingMate.py
```

**Use Cases:**
- **coverage_gap**: Close C0/C1/MC/DC gaps from backend coverage
- **oracle**: Generate expected values for assertion-heavy tests
- **negative**: Add robustness/security test cases for safety-critical code

**Limits:**
```bash
TESTMATE_LLM_MAX_FUNCTIONS=8    # Max functions per stage
TESTMATE_LLM_MAX_GAPS=3         # Max gaps per iteration
```

### Strategy 3: Iterative Refinement

**Multiple Rounds:**
```python
# In GUI: set "Max AI Rounds" (default: 1)
# Or programmatically:
options.max_ai_rounds = 3
```

**Iteration Behavior:**
- Round 1: Address top 3 coverage gaps
- Round 2: Re-analyze, address remaining gaps
- Round 3: Final refinement
- Stops early if no new cases added or target reached

### Strategy 4: Model Selection

**Recommended Models:**

| Model | Size | Speed | Quality | Use Case |
|-------|------|-------|---------|----------|
| `qwen2.5-coder:3b` | Small | Fast | Good | Quick iterations, simple C |
| `qwen2.5-coder:7b` | Medium | Moderate | Better | Complex logic, MC/DC |
| `deepseek-coder:6.7b` | Medium | Moderate | Better | Pointer-heavy code |
| `codellama:13b` | Large | Slow | Best | Safety-critical, ASIL D |

**Installation:**
```bash
ollama pull qwen2.5-coder:3b
ollama pull qwen2.5-coder:7b
ollama pull deepseek-coder:6.7b
```

## Prompt Engineering Insights

### Gap-Focused Prompts (Most Effective)

**What the LLM Receives:**
```json
{
  "task": "Generate additional GoogleTest test vectors from this coverage feedback chunk only.",
  "request_type": "coverage_gap_chunk",
  "chunk_id": "source.c:function_name:line_42:C1",
  "target_gap": {
    "function": "calculate_speed",
    "line": 42,
    "metric": "C1",
    "condition": "speed > MAX_SPEED",
    "text": "Uncovered C1: calculate_speed line 42 missing false outcome"
  },
  "coverage_feedback_chunk": {
    "source": "lcov",
    "metrics": {"C0": 85.5, "C1": 72.3},
    "uncovered_items": ["BRDA:42,0,1,0 - false branch not taken"]
  },
  "function": {
    "name": "calculate_speed",
    "return_type": "int",
    "parameters": [{"name": "speed", "type": "int"}],
    "nearby_conditions": [...],
    "allowed_pointer_fields": ["config.max_speed", "state.current"]
  },
  "code_database_chunk": {
    "functions": [...],
    "calls": [...],
    "variables": [...]
  },
  "recent_existing_cases_for_function": [
    {"values": {"speed": "100"}, "rationale": "Test normal speed"}
  ],
  "dedupe_against": ["speed=100", "speed=0"],
  "acceptance_rules": [
    "Only use listed function names and parameters",
    "Only use allowed_pointer_fields for pointer member access",
    "Use C literals only (no project-specific enums unless in code_database)",
    "Generate at most 4 test_cases"
  ]
}
```

**Key Design Decisions:**
1. **Chunked Requests**: One gap at a time, not entire project
2. **Explicit Constraints**: Only allowed symbols, no invention
3. **Deduplication**: Signatures of existing cases provided
4. **Concrete Evidence**: Actual line numbers and conditions
5. **Code Database**: Source of truth for function shape

### Validation and Acceptance Rules

**LLM Output Validation:**
```python
# Function name must match input
if case.function not in allowed_functions:
    reject()

# Parameters must be declared
if param not in function.parameters:
    reject()

# Pointer fields must be in allowed list
if field not in allowed_pointer_fields:
    reject()

# Values must be C literals
if not is_c_literal(value):
    reject()
```

**Accepted Response Formats:**
```json
// Format 1: Direct array
{"test_cases": [...]}

// Format 2: Wrapped object
{"coverage_enhancement": {"test_cases": [...]}}

// Format 3: Alternative key
{"cases": [...]}
{"vectors": [...]}
```

## Best Practices

### 1. **Start Without LLM**
Always run deterministic analysis first:
- Establishes baseline coverage
- Generates code database
- Creates initial test vectors
- Provides concrete gaps for LLM

### 2. **Use Appropriate Context Windows**
```bash
# For simple functions (< 10 conditions)
TESTMATE_OLLAMA_NUM_CTX=2048

# For complex functions (10-20 conditions)
TESTMATE_OLLAMA_NUM_CTX=4096

# For very complex functions (20+ conditions)
TESTMATE_OLLAMA_NUM_CTX=8192
```

### 3. **Limit Scope to Avoid Hallucination**
```bash
# Process fewer functions more thoroughly
TESTMATE_LLM_MAX_FUNCTIONS=5
TESTMATE_LLM_MAX_GAPS=2

# Multiple focused rounds better than one large round
max_ai_rounds=3
```

### 4. **Review LLM-Generated Cases**
All LLM cases are tagged:
- Check `rationale` field for reasoning
- Verify `expected` values (may be placeholders)
- Review pointer field accesses
- Validate against requirements

### 5. **Combine with Manual Editing**
```powershell
# 1. Generate with LLM
# 2. Review generated test file
# 3. Edit manually if needed
# 4. Use "Rerun" to execute edited tests
```

### 6. **Monitor LLM Performance**
Check logs for:
- Cases added per round
- Rejection reasons
- Timeout issues
- Coverage improvement

## Troubleshooting

### Issue: LLM Timeouts
```bash
# Increase timeout
TESTMATE_OLLAMA_TIMEOUT=90

# Reduce context/prediction
TESTMATE_OLLAMA_NUM_CTX=2048
TESTMATE_OLLAMA_NUM_PREDICT=256

# Use smaller model
ollama pull qwen2.5-coder:3b
```

### Issue: No Cases Generated
**Possible Causes:**
1. All gaps already covered
2. LLM output rejected (invalid symbols)
3. Deduplication filtered all cases
4. Model too small for complexity

**Solutions:**
- Check logs for rejection reasons
- Increase `TESTMATE_LLM_MAX_GAPS`
- Use larger model
- Simplify function (split complex functions)

### Issue: Invalid Generated Code
**Validation Failures:**
- Unknown function names → Check code database
- Invalid parameters → LLM hallucinated
- Unknown enums → Not in allowed symbols
- Syntax errors → Model too small

**Solutions:**
- Use code database (compile_commands.json)
- Increase context window
- Use better model (7b+ for complex code)
- Add more context in code database

### Issue: Poor Coverage Improvement
**Reasons:**
1. Gaps require complex state setup
2. Pointer/struct initialization needed
3. Hardware/external dependencies
4. Coupled conditions (MC/DC)

**Solutions:**
- Enable oracle stage for expected values
- Enable negative stage for edge cases
- Manual stub editing
- Split into smaller functions

## Advanced Configuration

### Custom Ollama Host
```bash
# Remote Ollama instance
TESTMATE_OLLAMA_HOST=http://192.168.1.100:11434

# Different port
TESTMATE_OLLAMA_HOST=http://localhost:8080
```

### Performance Tuning
```bash
# Fast iteration (lower quality)
TESTMATE_OLLAMA_NUM_CTX=2048
TESTMATE_OLLAMA_NUM_PREDICT=256
TESTMATE_OLLAMA_TIMEOUT=30

# High quality (slower)
TESTMATE_OLLAMA_NUM_CTX=8192
TESTMATE_OLLAMA_NUM_PREDICT=1024
TESTMATE_OLLAMA_TIMEOUT=120
```

### Stage-Specific Workflows

#### Coverage Gap Only (Default)
```bash
TESTMATE_LLM_STAGES=coverage_gap
TESTMATE_LLM_MAX_GAPS=5
# Focus: Close C0/C1/MC/DC gaps
```

#### Oracle Generation Focus
```bash
TESTMATE_LLM_STAGES=oracle
TESTMATE_LLM_MAX_FUNCTIONS=10
# Focus: Generate expected values for assertions
```

#### Security Testing Focus
```bash
TESTMATE_LLM_STAGES=negative
TESTMATE_LLM_MAX_FUNCTIONS=8
# Focus: NULL, overflow, invalid enum, boundary tests
```

#### Full Enhancement Pipeline
```bash
TESTMATE_LLM_STAGES=coverage_gap,oracle,negative
TESTMATE_LLM_MAX_FUNCTIONS=6
TESTMATE_LLM_MAX_GAPS=3
# Focus: Comprehensive test suite with all enhancements
```

## Integration with ISO 26262 Workflow

### ASIL-Appropriate LLM Usage

**ASIL A/B:**
- LLM for initial test generation
- Manual review of critical paths
- Focus on coverage_gap stage

**ASIL C:**
- LLM for coverage gap closure only
- Mandatory manual review of all LLM cases
- Enable oracle stage for assertion validation
- Document LLM-generated cases in audit trail

**ASIL D:**
- LLM as suggestion tool only
- All LLM cases require expert review
- Enable all stages for comprehensive coverage
- Manual verification of expected values
- Traceability: LLM cases tagged in rationale

### Audit Trail
All LLM-generated cases include:
- `rationale`: Why this case was generated
- `coverage_intent`: Which gap it targets
- Stage tags: "ORACLE:", "SECURITY:" prefixes
- Iteration number in case metadata

## Example Workflows

### Workflow 1: Quick Coverage Boost
```powershell
# Start Ollama
ollama serve

# Run Testing Mate with LLM
python TestingMate.py
# GUI: Enable "Use local Ollama"
# GUI: Model = "qwen2.5-coder:3b"
# GUI: Click "Analyze & Generate"
# Result: Deterministic + LLM-enhanced tests in one pass
```

### Workflow 2: Iterative Refinement
```powershell
# Round 1: Deterministic only
python TestingMate.py  # Disable "Use local Ollama"

# Review coverage report
# Identify gaps

# Round 2: LLM refinement
# GUI: Select generated HTML report
# GUI: Enable "Use local Ollama"
# GUI: Click "Refine From Report & Run"

# Round 3: Manual editing
# Edit generated test file
# GUI: Click "Rerun"
```

### Workflow 3: Multi-Stage Enhancement
```powershell
# Configure stages
$env:TESTMATE_LLM_STAGES = "coverage_gap,oracle,negative"
$env:TESTMATE_LLM_MAX_FUNCTIONS = "8"

# Run with all stages
python TestingMate.py
# Result: Coverage gaps + oracles + security tests
```

### Workflow 4: Safety-Critical (ASIL D)
```powershell
# Use larger model for better quality
ollama pull qwen2.5-coder:7b

# Conservative limits
$env:TESTMATE_LLM_MAX_FUNCTIONS = "5"
$env:TESTMATE_LLM_MAX_GAPS = "2"
$env:TESTMATE_OLLAMA_NUM_CTX = "8192"

# Enable all stages for comprehensive coverage
$env:TESTMATE_LLM_STAGES = "coverage_gap,oracle,negative"

python TestingMate.py
# GUI: Model = "qwen2.5-coder:7b"
# GUI: Enable "Use local Ollama"
# GUI: Max AI Rounds = 3

# Manual review of all LLM-generated cases
# Verify expected values
# Validate security test cases
# Document in audit checklist
```

## Metrics and Evaluation

### Success Indicators
- **Coverage Increase**: 5-15% typical improvement
- **Cases Added**: 10-50 per refinement round
- **Acceptance Rate**: 60-80% of LLM cases accepted
- **Iteration Efficiency**: Diminishing returns after round 2-3

### Quality Metrics
- **Compilation Rate**: >95% (validated symbols)
- **Execution Rate**: >90% (valid test logic)
- **Deduplication Rate**: 20-40% (expected overlap)
- **Manual Edit Rate**: <30% (good prompts)

## Conclusion

**Key Takeaways:**
1. **LLM is a refinement tool**, not the primary analyzer
2. **Coverage-driven prompts** are most effective
3. **Small, focused chunks** prevent hallucination
4. **Validation and constraints** ensure quality
5. **Multi-stage pipeline** enables comprehensive testing
6. **Human review** remains essential for safety-critical code

**When to Use LLM:**
- ✅ Closing coverage gaps from real reports
- ✅ Generating test variations for MC/DC
- ✅ Creating negative/security test cases
- ✅ Filling in expected values (with review)
- ✅ Exploring edge cases

**When NOT to Use LLM:**
- ❌ As primary code analyzer
- ❌ For safety-critical oracles without review
- ❌ When deterministic analysis is sufficient
- ❌ For hardware-dependent tests
- ❌ Without code database context

**Remember:** Testing Mate's strength is deterministic analysis. LLM enhancement is optional and should complement, not replace, the core analysis engine.
