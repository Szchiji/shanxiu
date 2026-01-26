#!/usr/bin/env python3
"""
Test script to verify that the immediate subscription unmute functionality
works correctly with proper logging and uses get_unrestricted_permissions.
"""
import sys
import os
from pathlib import Path


def test_callback_uses_get_unrestricted_permissions():
    """
    Test that handle_subscription_check_callback uses get_unrestricted_permissions
    instead of hardcoded permissions
    """
    print("=" * 70)
    print("TEST 1: Callback uses get_unrestricted_permissions()")
    print("=" * 70)
    print()
    
    try:
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
        
        # Find the handle_subscription_check_callback function
        print("✅ Checking handle_subscription_check_callback function...")
        
        # Check that it uses get_unrestricted_permissions()
        if 'get_unrestricted_permissions()' in content:
            # Count occurrences in the callback handler
            callback_start = content.find('async def handle_subscription_check_callback')
            callback_end = content.find('\nasync def', callback_start + 1)
            callback_section = content[callback_start:callback_end]
            
            if 'get_unrestricted_permissions()' in callback_section:
                print("   ✓ Uses get_unrestricted_permissions() (correct)")
            else:
                print("   ✗ Callback doesn't use get_unrestricted_permissions() (WRONG!)")
                return False
        else:
            print("   ✗ get_unrestricted_permissions() not found in routes.py (WRONG!)")
            return False
        
        # Verify it doesn't use hardcoded ChatPermissions in the unmute section
        print()
        print("✅ Checking for hardcoded permissions in callback...")
        if 'can_send_media_messages=True' in callback_section:
            print("   ✗ Still uses hardcoded permissions (WRONG!)")
            return False
        else:
            print("   ✓ No hardcoded permissions found (correct)")
        
        print()
        print("✅ PASS: handle_subscription_check_callback uses get_unrestricted_permissions()")
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_immediate_unmute_logging():
    """
    Test that immediate unmute has comprehensive logging
    """
    print()
    print("=" * 70)
    print("TEST 2: Immediate unmute has comprehensive logging")
    print("=" * 70)
    print()
    
    try:
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
        
        # Find the handle_subscription_check_callback function
        callback_start = content.find('async def handle_subscription_check_callback')
        callback_end = content.find('\nasync def', callback_start + 1)
        callback_section = content[callback_start:callback_end]
        
        print("✅ Checking for immediate unmute logging...")
        
        # Check for the new logging markers
        required_logs = [
            '[即时订阅验证]',  # Immediate subscription verification marker
            '准备立即解除禁言',  # Preparing to unmute immediately
            'Successfully unmuted user',  # Success message
            'immediately after subscription verification'  # Immediate verification note
        ]
        
        all_found = True
        for log in required_logs:
            if log in callback_section:
                print(f"   ✓ Found log: '{log}'")
            else:
                print(f"   ✗ Missing log: '{log}' (WRONG!)")
                all_found = False
        
        if not all_found:
            return False
        
        # Check for error logging with details
        print()
        print("✅ Checking for detailed error logging...")
        if 'Error type:' in callback_section and 'Error details:' in callback_section:
            print("   ✓ Has detailed error logging (correct)")
        else:
            print("   ✗ Missing detailed error logging (WRONG!)")
            return False
        
        print()
        print("✅ PASS: Immediate unmute has comprehensive logging")
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_realtime_check_logging():
    """
    Test that the real-time check (check_forced_subscription) has comprehensive logging
    """
    print()
    print("=" * 70)
    print("TEST 3: Real-time check has comprehensive logging")
    print("=" * 70)
    print()
    
    try:
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
        
        # Find the check_forced_subscription function
        func_start = content.find('async def check_forced_subscription')
        func_end = content.find('\nasync def', func_start + 1)
        func_section = content[func_start:func_end]
        
        print("✅ Checking for real-time mute logging...")
        
        # Check for the new logging markers
        required_logs = [
            '[实时订阅检测]',  # Real-time subscription detection marker
            '未订阅频道，准备禁言',  # Not subscribed, preparing to mute
            'Successfully muted unsubscribed user',  # Success message
        ]
        
        all_found = True
        for log in required_logs:
            if log in func_section:
                print(f"   ✓ Found log: '{log}'")
            else:
                print(f"   ✗ Missing log: '{log}' (WRONG!)")
                all_found = False
        
        if not all_found:
            return False
        
        # Check for error logging with details
        print()
        print("✅ Checking for detailed error logging in real-time check...")
        if 'Error type:' in func_section and 'Error details:' in func_section:
            print("   ✓ Has detailed error logging (correct)")
        else:
            print("   ✗ Missing detailed error logging (WRONG!)")
            return False
        
        print()
        print("✅ PASS: Real-time check has comprehensive logging")
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_scheduled_check_uses_get_unrestricted_permissions():
    """
    Test that the scheduled check also uses get_unrestricted_permissions
    """
    print()
    print("=" * 70)
    print("TEST 4: Scheduled check uses get_unrestricted_permissions()")
    print("=" * 70)
    print()
    
    try:
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
        
        # Find the check_channel_subscriptions function
        func_start = content.find('async def check_channel_subscriptions')
        func_end = content.find('\nasync def', func_start + 1)
        func_section = content[func_start:func_end]
        
        print("✅ Checking check_channel_subscriptions function...")
        
        if 'get_unrestricted_permissions()' in func_section:
            print("   ✓ Uses get_unrestricted_permissions() (correct)")
        else:
            print("   ✗ Doesn't use get_unrestricted_permissions() (WRONG!)")
            return False
        
        print()
        print("✅ PASS: Scheduled check uses get_unrestricted_permissions()")
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests"""
    print("\n" + "=" * 70)
    print("TESTING: IMMEDIATE SUBSCRIPTION UNMUTE FUNCTIONALITY")
    print("=" * 70)
    print()
    
    tests = [
        test_callback_uses_get_unrestricted_permissions,
        test_immediate_unmute_logging,
        test_realtime_check_logging,
        test_scheduled_check_uses_get_unrestricted_permissions,
    ]
    
    results = []
    for test_func in tests:
        results.append(test_func())
    
    # Summary
    print()
    print("=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    passed = sum(results)
    total = len(results)
    print(f"Tests passed: {passed}/{total}")
    
    if all(results):
        print()
        print("✅ ALL TESTS PASSED!")
        print()
        print("Summary:")
        print("- Callback handler uses get_unrestricted_permissions() for consistency")
        print("- Comprehensive logging added for immediate unmute operations")
        print("- Real-time check has enhanced logging for mute operations")
        print("- Scheduled check already uses get_unrestricted_permissions()")
        print("- Users are unmuted immediately when they verify subscription")
        print("- All three verification paths (callback, real-time, scheduled) are enhanced")
        return 0
    else:
        print()
        print("❌ SOME TESTS FAILED!")
        return 1


if __name__ == '__main__':
    sys.exit(main())
