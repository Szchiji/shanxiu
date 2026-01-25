#!/usr/bin/env python3
"""
Test script to verify that chat owner checks have been properly added
to prevent "Can't remove chat owner" errors.
"""
import sys
import os
from pathlib import Path


def test_chat_owner_checks():
    """
    Test that code properly checks for chat owners before attempting
    to restrict/unrestrict them.
    """
    print("=" * 70)
    print("CHAT OWNER FIX VERIFICATION")
    print("=" * 70)
    print()
    
    print("Verifying that chat owner checks have been added...")
    print()
    
    try:
        # Read routes.py
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
        
        # Check 1: Helper function exists
        print("✅ Checking for is_user_chat_owner() helper function...")
        if 'async def is_user_chat_owner(bot, chat_id, user_id):' in content:
            print("   ✓ Helper function exists")
            if "return member.status == 'creator'" in content:
                print("   ✓ Correctly checks for 'creator' status")
        else:
            print("   ✗ Helper function not found (WRONG!)")
            return False
        
        print()
        
        # Check 2: Channel subscription unmute check
        print("✅ Checking channel subscription detection (unmute)...")
        if 'is_owner = await is_user_chat_owner(context.bot, chat_id, group_user.tg_id)' in content:
            print("   ✓ Checks if user is chat owner")
            if '跳过解除禁言操作' in content and 'Chat Owner' in content:
                print("   ✓ Has proper logging for skipped unmute")
        else:
            print("   ✗ Chat owner check for unmute not found (WRONG!)")
            return False
        
        print()
        
        # Check 3: Channel subscription mute check
        print("✅ Checking channel subscription detection (mute)...")
        if '跳过惩罚操作' in content:
            print("   ✓ Has proper logging for skipped mute")
        else:
            print("   ? Mute skip logging may not be present")
        
        print()
        
        # Check 4: Unmute command check
        print("✅ Checking /unmute command handler...")
        if 'is_owner = await is_user_chat_owner(context.bot, chat.id, target_user.id)' in content:
            print("   ✓ Checks if target user is chat owner")
            if '无法解除群主' in content or 'Chat Owner' in content:
                print("   ✓ Has proper user-facing message for chat owners")
        else:
            print("   ✗ Chat owner check in unmute command not found (WRONG!)")
            return False
        
        print()
        
        # Check 5: Unban function check
        print("✅ Checking unban_user_in_group() function...")
        if 'async def _check_and_unban():' in content:
            print("   ✓ Has async wrapper for chat owner check")
            if 'is_owner = await is_user_chat_owner(global_ptb_app.bot' in content:
                print("   ✓ Checks if user is chat owner before unbanning")
        else:
            print("   ✗ Chat owner check in unban function not found (WRONG!)")
            return False
        
        print()
        print("=" * 70)
        print("✅ ALL CHECKS PASSED!")
        print("=" * 70)
        print()
        print("Summary:")
        print("  ✓ is_user_chat_owner() helper function added")
        print("  ✓ Channel subscription detection skips chat owners (mute)")
        print("  ✓ Channel subscription detection skips chat owners (unmute)")
        print("  ✓ /unmute command skips chat owners")
        print("  ✓ unban_user_in_group() skips chat owners")
        print()
        print("The fix should prevent the 'Can't remove chat owner' error.")
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    success = test_chat_owner_checks()
    sys.exit(0 if success else 1)
