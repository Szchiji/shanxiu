#!/usr/bin/env python3
"""
批量更新模板文件，将 alert() 替换为 toast 通知系统
"""

import re
import os
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / 'app' / 'modules' / 'core' / 'templates'

# 需要更新的模板列表
TEMPLATES_TO_UPDATE = [
    'bot_clones.html',
    'chat_settings.html',
    'entry_exit_settings.html',
    'fields.html',
    'forced_channel_subscription.html',
    'group_bottom_button.html',
    'group_lottery.html',
    'group_setting.html',
    'inactive_user_settings.html',
    'invitation_activity.html',
    'keyword_filter.html',
    'member_level.html',
    'points_auction.html',
    'points_auto_reply.html',
    'points_rules.html',
    'quiz_games.html',
    'settings.html',
    'spam_protection.html',
    'sync_group_messages.html',
    'system.html',
    'timed_group_control.html',
]

def update_template(filepath):
    """更新单个模板文件"""
    print(f"Processing {filepath.name}...")
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original_content = content
    
    # 1. 添加 import 语句（如果不存在）
    if '{% from "components.html"' not in content:
        # 在 {% extends "base.html" %} 后添加
        content = content.replace(
            '{% extends "base.html" %}',
            '{% extends "base.html" %}\n{% from "components.html" import toast_notification, loading_spinner %}'
        )
    
    # 2. 在 {% block content %} 后添加组件（如果不存在）
    if '{{ toast_notification() }}' not in content:
        content = content.replace(
            '{% block content %}',
            '{% block content %}\n{{ toast_notification() }}\n{{ loading_spinner() }}\n'
        )
    
    # 3. 替换 alert('✅ ... 为 showToast(..., 'success')
    content = re.sub(
        r"alert\('✅\s*([^']+)'\)",
        r"showToast('\1', 'success')",
        content
    )
    
    # 4. 替换 alert('❌ ... 为 showToast(..., 'error')
    content = re.sub(
        r"alert\('❌\s*([^']+)'\)",
        r"showToast('\1', 'error')",
        content
    )
    
    # 5. 替换 alert("✅ ... 为 showToast(..., 'success')
    content = re.sub(
        r'alert\("✅\s*([^"]+)"\)',
        r"showToast('\1', 'success')",
        content
    )
    
    # 6. 替换 alert("❌ ... 为 showToast(..., 'error')
    content = re.sub(
        r'alert\("❌\s*([^"]+)"\)',
        r"showToast('\1', 'error')",
        content
    )
    
    # 7. 在 fetch 调用前添加 showLoading（如果是保存操作）
    # 查找保存函数模式
    save_function_pattern = r'(function\s+save\w+\([^)]*\)\s*\{[^}]*fetch\()'
    if re.search(save_function_pattern, content):
        # 在 fetch 之前添加 showLoading
        content = re.sub(
            r'(function\s+save\w+\([^)]*\)\s*\{)(\s*)(const\s+data\s*=)',
            r"\1\2showLoading('保存中...');\2\3",
            content
        )
        
        # 在成功和失败的回调中添加 hideLoading
        content = re.sub(
            r'(\.then\(result\s*=>\s*\{[^}]*showToast\()',
            r'.then(result => {\n            hideLoading();\n            \1',
            content
        )
        
        content = re.sub(
            r'(\.catch\([^)]+\)\s*=>\s*\{[^}]*showToast\()',
            r'.catch(e => {\n            hideLoading();\n            \1',
            content
        )
    
    # 8. 修改 location.reload() 为延迟刷新（更好的用户体验）
    content = re.sub(
        r"(showToast\([^)]+,\s*'success'\);)\s*location\.reload\(\);",
        r"\1\n                setTimeout(() => location.reload(), 1000);",
        content
    )
    
    # 9. 添加错误日志
    content = re.sub(
        r"(\.catch\(e\s*=>\s*\{[^}]*showToast\([^)]+\);)",
        r"\1\n            console.error('操作失败:', e);",
        content
    )
    
    # 检查是否有变化
    if content != original_content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"  ✅ Updated {filepath.name}")
        return True
    else:
        print(f"  ⏭️  No changes needed for {filepath.name}")
        return False

def main():
    """主函数"""
    print("=" * 60)
    print("开始批量更新模板文件")
    print("=" * 60)
    print()
    
    updated_count = 0
    
    for template_name in TEMPLATES_TO_UPDATE:
        filepath = TEMPLATES_DIR / template_name
        if filepath.exists():
            if update_template(filepath):
                updated_count += 1
        else:
            print(f"⚠️  File not found: {template_name}")
    
    print()
    print("=" * 60)
    print(f"完成！共更新 {updated_count}/{len(TEMPLATES_TO_UPDATE)} 个文件")
    print("=" * 60)

if __name__ == '__main__':
    main()
