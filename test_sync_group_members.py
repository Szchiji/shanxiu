#!/usr/bin/env python3
"""
Test suite for sync_group_members functionality
Tests the API endpoint, async task, and database updates
"""
import sys
import os
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent))

def test_model_has_sync_field():
    """Test that BotGroup model has members_last_sync field"""
    print("=" * 70)
    print("TEST 1: BotGroup Model has members_last_sync field")
    print("=" * 70)
    print()
    
    try:
        from app.models import BotGroup
        
        print("✅ Checking BotGroup model...")
        
        if not hasattr(BotGroup, 'members_last_sync'):
            print("❌ FAIL: members_last_sync attribute not in BotGroup model!")
            return False
        
        print("   - members_last_sync exists in model")
        print()
        print("✅ TEST 1 PASSED")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_sync_task_returns_status():
    """Test that sync_group_members_task returns status tuple"""
    print()
    print("=" * 70)
    print("TEST 2: sync_group_members_task returns status")
    print("=" * 70)
    print()
    
    try:
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
        
        print("✅ Checking sync_group_members_task implementation...")
        
        # Check that the function returns a tuple
        if 'return (False,' not in content or 'return (True,' not in content:
            print("❌ FAIL: sync_group_members_task doesn't return status tuple!")
            return False
        
        print("   - Function returns status tuple")
        
        # Check that it updates members_last_sync
        if 'group.members_last_sync' not in content:
            print("❌ FAIL: sync_group_members_task doesn't update members_last_sync!")
            return False
        
        print("   - Function updates group.members_last_sync")
        print()
        print("✅ TEST 2 PASSED")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_api_waits_for_result():
    """Test that api_sync_group_members waits for task result"""
    print()
    print("=" * 70)
    print("TEST 3: API waits for async task result")
    print("=" * 70)
    print()
    
    try:
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
        
        print("✅ Checking api_sync_group_members implementation...")
        
        # Check that it uses future.result() to wait
        if 'future.result(timeout=' not in content:
            print("❌ FAIL: API doesn't wait for task result!")
            return False
        
        print("   - API waits for task result with timeout")
        
        # Check that it returns success/error based on task result
        if 'success, message, synced_count = future.result' not in content:
            print("❌ FAIL: API doesn't unpack task result tuple!")
            return False
        
        print("   - API unpacks and uses task result")
        
        # Check that it handles timeout
        if 'asyncio.TimeoutError' not in content or '同步超时' not in content:
            print("❌ FAIL: API doesn't handle timeout properly!")
            return False
        
        print("   - API handles timeout errors")
        print()
        print("✅ TEST 3 PASSED")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_api_response_format():
    """Test that API returns correct response format"""
    print()
    print("=" * 70)
    print("TEST 4: API response format matches frontend expectations")
    print("=" * 70)
    print()
    
    try:
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
        
        print("✅ Checking API response format...")
        
        # Extract the api_sync_group_members function
        func_start = content.find("def api_sync_group_members():")
        func_end = content.find("\n@", func_start + 1)
        if func_end == -1:
            func_end = content.find("\ndef ", func_start + 100)
        func_content = content[func_start:func_end]
        
        # Check that function uses 'status' and 'msg' in responses
        if "'status':" not in func_content and '"status":' not in func_content:
            print("❌ FAIL: Function doesn't use 'status' field!")
            return False
        
        if "'msg':" not in func_content and '"msg":' not in func_content:
            print("❌ FAIL: Function doesn't use 'msg' field!")
            return False
        
        # Count status usages (should have multiple - for ok and error)
        status_count = func_content.count("'status':") + func_content.count('"status":')
        msg_count = func_content.count("'msg':") + func_content.count('"msg":')
        
        print(f"   - Found {status_count} 'status' fields")
        print(f"   - Found {msg_count} 'msg' fields")
        
        # Check frontend template format
        template_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'templates' / 'group_members.html'
        with open(template_path, 'r') as f:
            template = f.read()
        
        if "data.status === 'ok'" not in template:
            print("❌ FAIL: Frontend doesn't check for 'ok' status!")
            return False
        
        print("   - Frontend checks for 'ok' status")
        print()
        print("✅ TEST 4 PASSED")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False

def run_all_tests():
    """Run all tests"""
    print("\n" + "=" * 70)
    print("SYNC GROUP MEMBERS FIX - TEST SUITE")
    print("=" * 70)
    print()
    
    results = []
    
    results.append(("BotGroup model field", test_model_has_sync_field()))
    results.append(("Sync task returns status", test_sync_task_returns_status()))
    results.append(("API waits for result", test_api_waits_for_result()))
    results.append(("API response format", test_api_response_format()))
    
    # Print summary
    print()
    print("=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")
    
    all_passed = all(passed for _, passed in results)
    
    print()
    if all_passed:
        print("🎉 ALL TESTS PASSED!")
        return 0
    else:
        print("❌ SOME TESTS FAILED")
        return 1

if __name__ == '__main__':
    exit_code = run_all_tests()
    sys.exit(exit_code)
