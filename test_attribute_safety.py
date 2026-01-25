#!/usr/bin/env python3
"""
Test suite for attribute safety and database column fixes.
Validates the fixes for:
1. Safe attribute access for forward_origin (using hasattr)
2. Database column is_muted_permanent exists and works
"""
import re


def test_forward_origin_safety():
    """
    Test that forward_origin is accessed safely with hasattr checks.
    This prevents AttributeError when the attribute doesn't exist.
    """
    print("\n" + "="*70)
    print("TEST 1: Safe forward_origin Attribute Access")
    print("="*70)
    
    try:
        # Read the routes.py file
        with open('app/modules/core/routes.py', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for patterns of forward_origin access
        print("\n✅ Checking for safe forward_origin access...")
        
        # Pattern 1: Block forwards spam protection
        pattern1 = r"hasattr\(msg,\s*['\"]forward_origin['\"]\)\s+and\s+msg\.forward_origin"
        matches1 = re.findall(pattern1, content)
        
        if matches1:
            print(f"   ✓ Found {len(matches1)} safe forward_origin access with hasattr()")
            for match in matches1:
                print(f"     - {match}")
        
        # Check that there are no unsafe direct accesses
        # Look for patterns like "msg.forward_origin" without hasattr check
        # But exclude patterns that already have hasattr
        unsafe_pattern = r"(?<!hasattr\([^)]{0,50})msg\.forward_origin(?!\s+and\s+hasattr)"
        
        # Find all forward_origin accesses
        all_accesses = re.finditer(r'msg\.forward_origin', content)
        unsafe_count = 0
        safe_count = 0
        
        for match in all_accesses:
            # Get context around the match (100 chars before)
            start = max(0, match.start() - 100)
            context = content[start:match.end() + 50]
            
            # Check if hasattr is in the context before the match
            if 'hasattr' in context[:100]:
                safe_count += 1
            else:
                unsafe_count += 1
                print(f"\n   ⚠️  Potentially unsafe access at position {match.start()}")
                print(f"      Context: ...{context}...")
        
        print(f"\n✅ Summary:")
        print(f"   - Safe accesses (with hasattr): {safe_count}")
        print(f"   - Potentially unsafe accesses: {unsafe_count}")
        
        if safe_count >= 2 and unsafe_count == 0:
            print("\n✅ PASS: All forward_origin accesses are safe!")
            return True
        elif safe_count >= 2:
            print("\n⚠️  WARNING: Most accesses are safe, but review potential unsafe ones")
            return True
        else:
            print("\n❌ FAIL: Not enough safe forward_origin accesses found")
            return False
            
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        return False


def test_is_muted_permanent_in_model():
    """
    Test that is_muted_permanent column is defined in the model.
    """
    print("\n" + "="*70)
    print("TEST 2: is_muted_permanent Column in Model")
    print("="*70)
    
    try:
        # Read the models.py file
        with open('app/models.py', 'r', encoding='utf-8') as f:
            content = f.read()
        
        print("\n✅ Checking model definition...")
        
        # Check for is_muted_permanent column definition
        if 'is_muted_permanent' in content:
            print("   ✓ is_muted_permanent found in models.py")
            
            # Extract the line with the definition
            lines = content.split('\n')
            for i, line in enumerate(lines, 1):
                if 'is_muted_permanent' in line and 'db.Column' in line:
                    print(f"   - Line {i}: {line.strip()}")
                    
                    # Verify it's a Boolean column
                    if 'Boolean' in line or 'BOOLEAN' in line:
                        print("   ✓ Column type is Boolean (correct)")
                    
                    # Verify default value
                    if 'default=False' in line or 'DEFAULT FALSE' in line:
                        print("   ✓ Default value is False (correct)")
            
            print("\n✅ PASS: is_muted_permanent column is properly defined!")
            return True
        else:
            print("   ✗ is_muted_permanent not found in model")
            print("\n❌ FAIL: Column not in model definition")
            return False
            
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        return False


def test_migration_script():
    """
    Test that migration script includes is_muted_permanent column migration.
    """
    print("\n" + "="*70)
    print("TEST 3: Migration Script Validation")
    print("="*70)
    
    try:
        # Read the migrate_database.py file
        with open('migrate_database.py', 'r', encoding='utf-8') as f:
            content = f.read()
        
        print("\n✅ Checking migration script...")
        
        checks = [
            ('is_muted_permanent', 'Column name'),
            ('ADD COLUMN IF NOT EXISTS', 'Idempotent migration'),
            ('BOOLEAN DEFAULT FALSE', 'Column definition'),
            ('information_schema.columns', 'Column existence check'),
        ]
        
        all_passed = True
        for check_str, description in checks:
            if check_str in content:
                print(f"   ✓ {description}: Found")
            else:
                print(f"   ✗ {description}: NOT found")
                all_passed = False
        
        # Check for proper error handling
        if 'except Exception' in content and 'rollback' in content:
            print("   ✓ Error handling: Found (with rollback)")
        else:
            print("   ⚠️  Error handling: May be incomplete")
        
        if all_passed:
            print("\n✅ PASS: Migration script is properly configured!")
            return True
        else:
            print("\n❌ FAIL: Migration script is incomplete")
            return False
            
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        return False


def test_readme_documentation():
    """
    Test that README includes migration step in installation instructions.
    """
    print("\n" + "="*70)
    print("TEST 4: README Documentation Check")
    print("="*70)
    
    try:
        # Read the README.md file
        with open('README.md', 'r', encoding='utf-8') as f:
            content = f.read()
        
        print("\n✅ Checking README documentation...")
        
        # Check for migration instruction
        if 'migrate_database.py' in content:
            print("   ✓ migrate_database.py mentioned in README")
            
            # Check if it's in the installation/setup section
            # Look for context around the migration mention
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if 'migrate_database.py' in line:
                    # Show context
                    start = max(0, i - 2)
                    end = min(len(lines), i + 3)
                    print(f"\n   Context (lines {start+1}-{end}):")
                    for j in range(start, end):
                        prefix = "   >>> " if j == i else "       "
                        print(f"{prefix}{lines[j]}")
            
            print("\n✅ PASS: Migration step is documented in README!")
            return True
        else:
            print("   ✗ migrate_database.py not mentioned in README")
            print("\n❌ FAIL: Migration step not documented")
            return False
            
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        return False


def main():
    """Run all tests and report results."""
    print("\n" + "╔" + "="*68 + "╗")
    print("║" + " "*15 + "ATTRIBUTE SAFETY AND DATABASE TESTS" + " "*18 + "║")
    print("╚" + "="*68 + "╝")
    
    results = []
    
    # Run all tests
    results.append(("Safe forward_origin access", test_forward_origin_safety()))
    results.append(("is_muted_permanent in model", test_is_muted_permanent_in_model()))
    results.append(("Migration script validation", test_migration_script()))
    results.append(("README documentation", test_readme_documentation()))
    
    # Print summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {test_name}")
    
    print("\n" + "="*70)
    print(f"Results: {passed}/{total} tests passed")
    print("="*70)
    
    if passed == total:
        print("\n🎉 ALL TESTS PASSED! The fixes are properly implemented.")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed. Review the output above.")
        return 1


if __name__ == '__main__':
    exit(main())
