#!/usr/bin/env python3
"""
Quick test to verify forward_origin attribute exists and works correctly
"""
import sys

def test_telegram_forward_detection():
    """Test that forward_origin attribute exists in python-telegram-bot"""
    print("Testing telegram Message forward detection...")
    print()
    
    try:
        from telegram import Message
        
        # Check that forward_origin exists
        print("✅ Imported telegram.Message successfully")
        
        # Verify forward_origin attribute exists
        has_forward_origin = hasattr(Message, 'forward_origin')
        print(f"✅ Message has 'forward_origin' attribute: {has_forward_origin}")
        
        # Verify forward_date does NOT exist (old API)
        has_forward_date = hasattr(Message, 'forward_date')
        print(f"ℹ️  Message has 'forward_date' attribute: {has_forward_date} (expected: False)")
        
        if not has_forward_origin:
            print("❌ ERROR: forward_origin not found!")
            return False
            
        if has_forward_date:
            print("⚠️  WARNING: forward_date still exists (unexpected for v21+)")
        
        print()
        print("✅ Forward detection attributes are correct for python-telegram-bot v21+")
        return True
        
    except ImportError as e:
        print(f"❌ Error importing telegram module: {e}")
        print("   Make sure python-telegram-bot is installed:")
        print("   pip install python-telegram-bot[job-queue]==21.11.1")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False

def test_code_syntax():
    """Test that the fixed code compiles correctly"""
    print("\nTesting code syntax...")
    print()
    
    try:
        import py_compile
        import os
        
        routes_file = os.path.join(
            os.path.dirname(__file__), 
            'app', 'modules', 'core', 'routes.py'
        )
        
        if os.path.exists(routes_file):
            py_compile.compile(routes_file, doraise=True)
            print(f"✅ routes.py compiles successfully")
            
            # Check that forward_date is not in the file
            with open(routes_file, 'r') as f:
                content = f.read()
                if 'forward_date' in content:
                    print("❌ ERROR: forward_date still found in routes.py")
                    return False
                if 'forward_origin' in content:
                    print("✅ forward_origin found in routes.py (correct)")
                else:
                    print("⚠️  WARNING: forward_origin not found in routes.py")
            
            return True
        else:
            print(f"⚠️  routes.py not found at {routes_file}")
            return False
            
    except Exception as e:
        print(f"❌ Syntax error in routes.py: {e}")
        return False

if __name__ == '__main__':
    print("="*70)
    print("Forward Detection Fix Verification")
    print("="*70)
    print()
    
    test1_pass = test_telegram_forward_detection()
    test2_pass = test_code_syntax()
    
    print()
    print("="*70)
    if test1_pass and test2_pass:
        print("✅ All tests passed!")
        print("="*70)
        sys.exit(0)
    else:
        print("❌ Some tests failed!")
        print("="*70)
        sys.exit(1)
