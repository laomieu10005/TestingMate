#!/usr/bin/env python
"""
Diagnostic script to test TestVectorGenerator.generate() method.
Run this to identify any errors.
"""

import sys
import traceback
from pathlib import Path

# Add TestingMate to path
sys.path.insert(0, str(Path(__file__).parent))

def test_generate():
    """Test the generate method with various configurations."""
    
    print("=" * 70)
    print("DIAGNOSTIC TEST: TestVectorGenerator.generate()")
    print("=" * 70)
    
    try:
        # Test 1: Import check
        print("\n[TEST 1] Importing modules...")
        from testing_mate.vector_generator import TestVectorGenerator
        from testing_mate.models import FunctionInfo, Parameter, Condition
        print("  ✓ Imports successful")
        
        # Test 2: Initialization
        print("\n[TEST 2] Initializing TestVectorGenerator...")
        generator = TestVectorGenerator()
        print("  ✓ Generator initialized")
        
        # Test 3: Create simple function
        print("\n[TEST 3] Creating test function...")
        fn = FunctionInfo(
            name="calculate",
            return_type="int",
            parameters=[
                Parameter("a", "int", "scalar", set()),
                Parameter("b", "int", "scalar", set()),
            ],
            start_line=1,
            end_line=10,
            body="if (a > b) return a; return b;",
            conditions=[Condition("a > b", ["a", "b"], 1)]
        )
        print(f"  ✓ Function created: {fn.name}")
        
        # Test 4: Generate with minimal parameters
        print("\n[TEST 4] Calling generate() with minimal parameters...")
        cases = generator.generate(
            source_name="module.c",
            functions=[fn],
            level="UT",
            coverage="C0"
        )
        print(f"  ✓ Generated {len(cases)} test case(s)")
        
        # Test 5: Generate with ISO enabled
        print("\n[TEST 5] Calling generate() with iso_enabled=True...")
        cases = generator.generate(
            source_name="module.c",
            functions=[fn],
            level="UT",
            coverage="C1",
            iso_enabled=True
        )
        print(f"  ✓ Generated {len(cases)} test case(s)")
        
        # Test 6: Generate IT level
        print("\n[TEST 6] Calling generate() with level='IT'...")
        cases = generator.generate(
            source_name="module.c",
            functions=[fn],
            level="IT",
            coverage="C0"
        )
        print(f"  ✓ Generated {len(cases)} test case(s)")
        
        # Test 7: MC/DC coverage
        print("\n[TEST 7] Calling generate() with MC/DC coverage...")
        cases = generator.generate(
            source_name="module.c",
            functions=[fn],
            level="UT",
            coverage="MC/DC"
        )
        print(f"  ✓ Generated {len(cases)} test case(s)")
        
        print("\n" + "=" * 70)
        print("ALL TESTS PASSED")
        print("=" * 70)
        return True
        
    except Exception as e:
        print("\n" + "=" * 70)
        print("ERROR DETECTED")
        print("=" * 70)
        print(f"\nError Type: {type(e).__name__}")
        print(f"Error Message: {str(e)}")
        print("\nFull Traceback:")
        print("-" * 70)
        traceback.print_exc()
        print("-" * 70)
        return False


if __name__ == "__main__":
    success = test_generate()
    sys.exit(0 if success else 1)
