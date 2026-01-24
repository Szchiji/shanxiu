#!/usr/bin/env python3
"""
静态代码分析 - 验证定时消息暂停功能
Static Code Analysis - Verify Scheduled Message Pause Functionality

This script analyzes the source code to verify that the is_active filter
is correctly implemented in check_scheduled_messages().
"""

import re
import sys

def verify_code():
    """静态分析代码实现"""
    
    print("=" * 70)
    print("定时消息暂停功能静态代码验证")
    print("Scheduled Message Pause Feature - Static Code Verification")
    print("=" * 70)
    print()
    
    # 读取源文件
    file_path = "app/modules/core/routes.py"
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"❌ 无法读取文件: {e}")
        return False
    
    print(f"✓ 文件路径: {file_path}")
    print(f"✓ 文件大小: {len(content)} 字符")
    print()
    
    # 查找 check_scheduled_messages 函数
    func_pattern = r'async def check_scheduled_messages\(context\):'
    func_match = re.search(func_pattern, content)
    
    if not func_match:
        print("❌ 未找到 check_scheduled_messages 函数")
        return False
    
    print("✓ 找到函数: check_scheduled_messages()")
    func_start = func_match.start()
    print(f"  位置: 第 {content[:func_start].count(chr(10)) + 1} 行")
    print()
    
    # 提取函数内容（简化版，找到下一个顶层函数）
    next_func_pattern = r'\nasync def |^\ndef '
    remaining_content = content[func_start + 100:]
    next_func_match = re.search(next_func_pattern, remaining_content)
    
    if next_func_match:
        func_end = func_start + 100 + next_func_match.start()
    else:
        func_end = len(content)
    
    func_content = content[func_start:func_end]
    
    # 检查 is_active 过滤
    checks = []
    
    # 1. 检查 ScheduledMessage.query 是否存在
    if 'ScheduledMessage.query' in func_content:
        checks.append(('✓', 'ScheduledMessage.query 查询存在'))
    else:
        checks.append(('❌', 'ScheduledMessage.query 查询不存在'))
    
    # 2. 检查 is_active == True 过滤
    is_active_patterns = [
        r'is_active\s*==\s*True',
        r'is_active\s*=\s*True',
        r'filter_by\([^)]*is_active\s*=\s*True',
        r'filter\([^)]*is_active\s*==\s*True'
    ]
    
    found_filter = False
    for pattern in is_active_patterns:
        if re.search(pattern, func_content):
            found_filter = True
            checks.append(('✓', f'找到 is_active 过滤: {pattern}'))
            break
    
    if not found_filter:
        checks.append(('❌', 'is_active 过滤条件缺失'))
    
    # 3. 检查查询前后的代码结构
    if 'scheduled_messages = ScheduledMessage.query' in func_content:
        checks.append(('✓', '查询赋值给 scheduled_messages 变量'))
    else:
        checks.append(('⚠️', '查询可能使用不同的变量名'))
    
    # 4. 检查 joinedload 优化
    if 'joinedload' in func_content:
        checks.append(('✓', '使用 joinedload 优化查询'))
    else:
        checks.append(('⚠️', '未使用 joinedload 优化'))
    
    # 5. 检查群组 is_active 验证
    if 'group.is_active' in func_content or 'msg.group.is_active' in func_content:
        checks.append(('✓', '验证群组是否活跃'))
    else:
        checks.append(('⚠️', '未验证群组活跃状态'))
    
    # 6. 检查 start_time 验证
    if 'start_time' in func_content and 'now' in func_content:
        checks.append(('✓', '验证开始时间'))
    else:
        checks.append(('⚠️', '未验证开始时间'))
    
    # 7. 检查 stop_time 验证
    if 'stop_time' in func_content and 'now' in func_content:
        checks.append(('✓', '验证停止时间'))
    else:
        checks.append(('⚠️', '未验证停止时间'))
    
    # 8. 检查 repeat_interval 验证
    if 'repeat_interval' in func_content:
        checks.append(('✓', '验证重复间隔'))
    else:
        checks.append(('⚠️', '未验证重复间隔'))
    
    # 显示检查结果
    print("-" * 70)
    print("检查项目:")
    print("-" * 70)
    
    pass_count = 0
    fail_count = 0
    warn_count = 0
    
    for status, desc in checks:
        print(f"{status} {desc}")
        if status == '✓':
            pass_count += 1
        elif status == '❌':
            fail_count += 1
        else:
            warn_count += 1
    
    print()
    print("-" * 70)
    print(f"统计: ✓ {pass_count}  ⚠️ {warn_count}  ❌ {fail_count}")
    print("-" * 70)
    print()
    
    # 提取关键代码片段
    print("关键代码片段:")
    print("-" * 70)
    
    # 查找包含 is_active 的查询
    query_pattern = r'ScheduledMessage\.query[^\n]*(?:\n[^\n]*){0,10}\.all\(\)'
    query_matches = re.finditer(query_pattern, func_content, re.MULTILINE)
    
    for i, match in enumerate(query_matches, 1):
        snippet = match.group(0)
        # 清理缩进
        lines = snippet.split('\n')
        min_indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
        cleaned_lines = [line[min_indent:] if len(line) > min_indent else line for line in lines]
        cleaned_snippet = '\n'.join(cleaned_lines)
        
        print(f"\n查询 #{i}:")
        print(cleaned_snippet)
    
    print("-" * 70)
    print()
    
    # 最终判断
    critical_checks = [
        'ScheduledMessage.query 查询存在' in str(checks),
        found_filter,
    ]
    
    all_critical_pass = all(critical_checks)
    
    if all_critical_pass and fail_count == 0:
        print("=" * 70)
        print("🎉 验证通过！代码实现正确。")
        print("=" * 70)
        print()
        print("✓ check_scheduled_messages() 函数正确实现了 is_active 过滤")
        print("✓ 暂停的定时消息 (is_active=False) 不会被发送")
        print("✓ 只有启用的定时消息 (is_active=True) 会被处理")
        print()
        return True
    else:
        print("=" * 70)
        print("❌ 验证失败！代码可能存在问题。")
        print("=" * 70)
        print()
        if not found_filter:
            print("❌ 关键问题: 缺少 is_active 过滤条件")
            print("   需要在 ScheduledMessage.query 中添加:")
            print("   .filter(ScheduledMessage.is_active == True)")
        print()
        return False

if __name__ == "__main__":
    print()
    success = verify_code()
    print()
    sys.exit(0 if success else 1)
