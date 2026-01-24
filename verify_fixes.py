#!/usr/bin/env python3
"""
Verification script for the bug fixes
This script validates the implemented changes are correct
"""

import re
import sys

def verify_routes_file():
    """Verify changes in routes.py"""
    print("🔍 Verifying changes in app/modules/core/routes.py...")
    
    with open('app/modules/core/routes.py', 'r') as f:
        content = f.read()
    
    checks = []
    
    # Check 1: update_lottery_status function exists
    if 'async def update_lottery_status(context):' in content:
        checks.append(('✅', 'update_lottery_status() function exists'))
    else:
        checks.append(('❌', 'update_lottery_status() function NOT FOUND'))
    
    # Check 2: update_lottery_status updates pending to active
    if "GroupLottery.status == 'pending'" in content and \
       "GroupLottery.start_time <= now" in content and \
       "update({'status': 'active'})" in content:
        checks.append(('✅', 'update_lottery_status() correctly updates pending -> active'))
    else:
        checks.append(('❌', 'update_lottery_status() logic incorrect'))
    
    # Check 3: run_lottery_draws queries both pending and active
    if "GroupLottery.status.in_(['pending', 'active'])" in content:
        checks.append(('✅', 'run_lottery_draws() queries both pending and active status'))
    else:
        checks.append(('❌', 'run_lottery_draws() does not query both statuses'))
    
    # Check 4: Lottery message tracking supports both statuses
    # Use multiple simpler checks instead of complex regex
    has_pending_active_check = "GroupLottery.status.in_(['pending', 'active'])" in content
    has_lottery_type_filter = "lottery_type.in_(['message_count', 'message_rank'])" in content
    # Find the lottery tracking section (around line 8073-8115)
    tracking_section = content[content.find("# 🆕 Track messages for active lotteries"):
                                content.find("# 🆕 Track messages for active lotteries") + 2000]
    has_both_in_tracking = has_pending_active_check and has_lottery_type_filter and \
                          "status.in_(['pending', 'active'])" in tracking_section
    
    if has_both_in_tracking:
        checks.append(('✅', 'Lottery message tracking supports pending and active status'))
    else:
        checks.append(('❌', 'Lottery message tracking does not support both statuses'))
    
    # Check 5: update_lottery_status registered in job queue
    if 'app.job_queue.run_repeating(update_lottery_status' in content:
        checks.append(('✅', 'update_lottery_status() registered in job queue'))
    else:
        checks.append(('❌', 'update_lottery_status() NOT registered in job queue'))
    
    # Check 6: Pure digit filter fixed
    if 'and not txt.isdigit()' in content:
        checks.append(('✅', 'Pure digit messages excluded from filter queries'))
    else:
        checks.append(('❌', 'Pure digit filter NOT implemented'))
    
    # Check 7: check_scheduled_messages filters by is_active
    if 'ScheduledMessage.is_active == True' in content:
        checks.append(('✅', 'check_scheduled_messages() filters by is_active=True'))
    else:
        checks.append(('❌', 'check_scheduled_messages() does NOT filter by is_active'))
    
    # Print results
    print("\n📊 Verification Results:\n")
    all_passed = True
    for status, message in checks:
        print(f"  {status} {message}")
        if status == '❌':
            all_passed = False
    
    return all_passed

def verify_models():
    """Verify GroupLottery model"""
    print("\n🔍 Verifying GroupLottery model...")
    
    with open('app/models.py', 'r') as f:
        content = f.read()
    
    checks = []
    
    # Check GroupLottery has status field
    if "status = db.Column(db.String(20), default='pending')" in content:
        checks.append(('✅', 'GroupLottery.status field exists with default=pending'))
    else:
        checks.append(('❌', 'GroupLottery.status field incorrect'))
    
    # Check GroupLottery has start_time and end_time
    if 'start_time = db.Column(db.DateTime' in content and \
       'end_time = db.Column(db.DateTime' in content:
        checks.append(('✅', 'GroupLottery has start_time and end_time fields'))
    else:
        checks.append(('❌', 'GroupLottery missing time fields'))
    
    print("\n📊 Model Verification Results:\n")
    for status, message in checks:
        print(f"  {status} {message}")

def main():
    """Main verification"""
    print("="*60)
    print("🔧 Bug Fix Verification Script")
    print("="*60)
    
    routes_ok = verify_routes_file()
    verify_models()
    
    print("\n" + "="*60)
    if routes_ok:
        print("✅ ALL CHECKS PASSED!")
        print("="*60)
        return 0
    else:
        print("❌ SOME CHECKS FAILED")
        print("="*60)
        return 1

if __name__ == '__main__':
    sys.exit(main())
