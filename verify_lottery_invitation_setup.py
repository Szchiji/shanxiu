#!/usr/bin/env python3
"""
验证抽奖和邀请功能的配置
Verify lottery and invitation feature setup
"""
import os
import sys

def check_handlers_registration():
    """检查处理器是否正确注册"""
    print("\n📋 检查处理器注册...")
    
    with open('app/modules/core/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()
        
    checks = []
    
    # Check if new_chat_members handler is registered
    if 'MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_member)' in content:
        checks.append(('✅', 'NEW_CHAT_MEMBERS handler is registered'))
    else:
        checks.append(('❌', 'NEW_CHAT_MEMBERS handler is NOT registered'))
    
    # Check if run_lottery_draws is scheduled
    if 'app.job_queue.run_repeating(run_lottery_draws, interval=300' in content:
        checks.append(('✅', 'run_lottery_draws() is scheduled to run every 300 seconds (5 minutes)'))
    else:
        checks.append(('❌', 'run_lottery_draws() is NOT scheduled'))
    
    # Check if update_lottery_status is scheduled
    if 'app.job_queue.run_repeating(update_lottery_status, interval=60' in content:
        checks.append(('✅', 'update_lottery_status() is scheduled to run every 60 seconds'))
    else:
        checks.append(('❌', 'update_lottery_status() is NOT scheduled'))
    
    return checks

def check_logging_implementation():
    """检查日志实现"""
    print("\n📋 检查日志实现...")
    
    with open('app/modules/core/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()
        
    checks = []
    
    # Check logging in run_lottery_draws
    if 'logging.info(f"🎲 [抽奖任务]' in content:
        checks.append(('✅', 'run_lottery_draws() has logging statements'))
    else:
        checks.append(('❌', 'run_lottery_draws() missing logging'))
    
    # Check logging in handle_new_chat_member
    if 'logging.info(f"👥 [入群事件]' in content or 'logging.info(f"🎁 [邀请活动]' in content:
        checks.append(('✅', 'handle_new_chat_member() has logging statements'))
    else:
        checks.append(('❌', 'handle_new_chat_member() missing logging'))
    
    # Check logging in message counting
    if 'logging.debug(f"🎲 [消息计数]' in content:
        checks.append(('✅', 'Message counting has debug logging'))
    else:
        checks.append(('❌', 'Message counting missing logging'))
    
    # Check error logging
    if 'logging.error(f"❌' in content and 'traceback.format_exc()' in content:
        checks.append(('✅', 'Error logging with traceback is implemented'))
    else:
        checks.append(('❌', 'Error logging is incomplete'))
    
    return checks

def check_invitation_tracking():
    """检查邀请追踪逻辑"""
    print("\n📋 检查邀请追踪逻辑...")
    
    with open('app/modules/core/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()
        
    checks = []
    
    # Check InvitationActivity query
    if 'InvitationActivity.query.filter_by(group_id=group.id, enabled=True)' in content:
        checks.append(('✅', 'InvitationActivity is queried correctly'))
    else:
        checks.append(('❌', 'InvitationActivity query is missing'))
    
    # Check InvitationRecord creation
    if 'InvitationRecord(' in content and 'db.session.add(invitation_record)' in content:
        checks.append(('✅', 'InvitationRecord is created and saved'))
    else:
        checks.append(('❌', 'InvitationRecord creation is missing'))
    
    # Check points award
    if 'user_points.points_balance += invitation_activity.reward_points' in content:
        checks.append(('✅', 'Points are awarded to inviter'))
    else:
        checks.append(('❌', 'Points award logic is missing'))
    
    # Check group announcement
    if 'announce_in_group' in content and 'await context.bot.send_message' in content:
        checks.append(('✅', 'Group announcement is implemented'))
    else:
        checks.append(('❌', 'Group announcement is missing'))
    
    return checks

def check_lottery_message_counting():
    """检查抽奖消息计数"""
    print("\n📋 检查抽奖消息计数...")
    
    with open('app/modules/core/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()
        
    checks = []
    
    # Check active lottery query
    if "GroupLottery.query.filter_by" in content and "status='active'" in content:
        checks.append(('✅', 'Active lotteries are queried'))
    else:
        checks.append(('❌', 'Active lottery query is missing'))
    
    # Check LotteryMessageCount creation
    if 'LotteryMessageCount(' in content and 'msg_count.message_count += 1' in content:
        checks.append(('✅', 'Message counting is implemented'))
    else:
        checks.append(('❌', 'Message counting is missing'))
    
    # Check time window validation
    if 'lottery.start_time <= now <= lottery.end_time' in content:
        checks.append(('✅', 'Time window validation is implemented'))
    else:
        checks.append(('❌', 'Time window validation is missing'))
    
    return checks

def check_logging_configuration():
    """检查日志配置"""
    print("\n📋 检查日志配置...")
    
    checks = []
    
    if os.path.exists('run.py'):
        with open('run.py', 'r', encoding='utf-8') as f:
            content = f.read()
            
        if 'import logging' in content:
            checks.append(('✅', 'logging module is imported in run.py'))
        else:
            checks.append(('❌', 'logging module is NOT imported in run.py'))
            
        if 'logging.basicConfig' in content:
            checks.append(('✅', 'logging is configured in run.py'))
        else:
            checks.append(('❌', 'logging is NOT configured in run.py'))
    else:
        checks.append(('❌', 'run.py not found'))
    
    return checks

def main():
    print("=" * 70)
    print("🔍 抽奖和邀请功能验证")
    print("   Lottery and Invitation Feature Verification")
    print("=" * 70)
    
    all_checks = []
    
    # Run all checks
    all_checks.extend(check_handlers_registration())
    all_checks.extend(check_logging_implementation())
    all_checks.extend(check_invitation_tracking())
    all_checks.extend(check_lottery_message_counting())
    all_checks.extend(check_logging_configuration())
    
    # Print results
    print("\n" + "=" * 70)
    print("📊 验证结果 / Verification Results")
    print("=" * 70)
    
    passed = 0
    failed = 0
    
    for status, message in all_checks:
        print(f"{status} {message}")
        if status == '✅':
            passed += 1
        else:
            failed += 1
    
    print("\n" + "=" * 70)
    print(f"总计 Total: {passed + failed}")
    print(f"通过 Passed: {passed}")
    print(f"失败 Failed: {failed}")
    print("=" * 70)
    
    if failed == 0:
        print("\n✅ 所有检查通过！All checks passed!")
        return 0
    else:
        print(f"\n⚠️  有 {failed} 个检查失败。{failed} check(s) failed.")
        return 1

if __name__ == '__main__':
    sys.exit(main())
