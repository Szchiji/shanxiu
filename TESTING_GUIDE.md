# 测试指南 (Testing Guide)

## 快速测试清单 (Quick Testing Checklist)

### 1. 分页功能测试 (Pagination Testing)

#### 用户列表页面 (Users Page)
1. 访问 `/core/group/{group_id}/users`
2. 检查项目：
   - [ ] 分页组件显示正常
   - [ ] 可以选择 10, 20, 50, 100 每页数量
   - [ ] 上一页/下一页按钮工作正常
   - [ ] 当前页码显示正确
   - [ ] 总记录数显示正确
   - [ ] 悬停按钮有动画效果
   - [ ] 第一页时"上一页"按钮禁用
   - [ ] 最后一页时"下一页"按钮禁用

#### 自动回复页面 (Auto Replies Page)
1. 访问 `/core/group/{group_id}/auto_replies`
2. 检查项目：
   - [ ] 分页组件与用户列表页面样式一致
   - [ ] 所有分页功能正常工作
   - [ ] 选择不同每页数量后跳转到第一页

#### 定时消息页面 (Scheduled Messages Page)
1. 访问 `/core/group/{group_id}/scheduled_messages`
2. 检查项目：
   - [ ] 分页组件显示正常
   - [ ] 客户端分页功能正常（无需刷新页面）
   - [ ] 样式与其他页面一致

#### 启动消息页面 (Start Messages Page)
1. 访问 `/core/group/{group_id}/start_messages`
2. 检查项目：
   - [ ] 分页组件与其他页面一致
   - [ ] 所有分页功能正常工作

### 2. 移动端测试 (Mobile Testing)

#### Chrome DevTools 移动模拟
1. 打开 Chrome DevTools (F12)
2. 点击 Toggle Device Toolbar (Ctrl+Shift+M)
3. 选择移动设备 (iPhone, iPad, Android)
4. 测试项目：
   - [ ] 分页组件在小屏幕上布局正常
   - [ ] 按钮大小适合触摸操作
   - [ ] 文字清晰可读
   - [ ] 下拉选择框易于操作
   - [ ] 可以选择 100 每页查看更多内容

#### 不同设备尺寸
测试以下设备尺寸：
- [ ] iPhone SE (375x667)
- [ ] iPhone 12 Pro (390x844)
- [ ] iPad (768x1024)
- [ ] Galaxy S20 (360x800)
- [ ] Desktop (1920x1080)

### 3. 群管理机器人测试 (Bot Commands Testing)

⚠️ **重要：所有命令需要在 Telegram 群组中测试，且需要管理员权限**

#### 准备工作
1. 确保机器人在测试群组中
2. 给机器人管理员权限
3. 使用管理员账号进行测试

#### 测试步骤

##### /kick 命令
1. 回复一个测试用户的消息
2. 发送 `/kick`
3. 检查：
   - [ ] 用户被踢出群组
   - [ ] 收到成功提示消息
   - [ ] 用户可以重新加入

##### /ban 命令
1. 回复测试用户的消息
2. 发送 `/ban`
3. 检查：
   - [ ] 用户被永久封禁
   - [ ] 收到成功提示消息
   - [ ] 用户无法重新加入

##### /unban 命令
1. 回复被封禁用户的消息
2. 发送 `/unban`
3. 检查：
   - [ ] 用户被解封
   - [ ] 收到成功提示消息
   - [ ] 用户可以重新加入

##### /mute 命令
1. 回复测试用户的消息
2. 发送 `/mute 5` (禁言5分钟)
3. 检查：
   - [ ] 用户被禁言
   - [ ] 收到提示消息显示禁言时长
   - [ ] 用户无法发送消息
   - [ ] 5分钟后自动解除

##### /unmute 命令
1. 回复被禁言用户的消息
2. 发送 `/unmute`
3. 检查：
   - [ ] 用户解除禁言
   - [ ] 收到成功提示消息
   - [ ] 用户可以发送消息

##### /pin 命令
1. 回复要置顶的消息
2. 发送 `/pin`
3. 检查：
   - [ ] 消息被置顶
   - [ ] 收到成功提示消息

##### /unpin 命令
1. 回复已置顶的消息，发送 `/unpin`
2. 或直接发送 `/unpin` (取消所有置顶)
3. 检查：
   - [ ] 消息取消置顶
   - [ ] 收到成功提示消息

##### /warn 命令
1. 回复测试用户的消息
2. 发送 `/warn 违反群规`
3. 检查：
   - [ ] 收到警告消息
   - [ ] 消息显示用户和原因
   - [ ] 格式正确

#### 权限测试
1. 使用非管理员账号
2. 尝试执行任意命令
3. 检查：
   - [ ] 收到权限不足提示
   - [ ] 命令未执行

#### 错误处理测试
1. 发送命令但不回复消息
2. 检查：
   - [ ] 收到使用说明提示
   - [ ] 命令未执行

### 4. 设置页面测试 (Settings Page Testing)

1. 访问 `/core/group/{group_id}/settings`
2. 滚动到"群管理机器人命令"板块
3. 检查：
   - [ ] 所有8个命令卡片显示正常
   - [ ] 每个卡片显示命令名称
   - [ ] 每个卡片显示功能说明
   - [ ] 每个卡片显示用法示例
   - [ ] 渐变背景颜色正确显示
   - [ ] 移动端布局正常

### 5. 浏览器兼容性测试 (Browser Compatibility)

测试以下浏览器：
- [ ] Chrome (最新版)
- [ ] Firefox (最新版)
- [ ] Safari (最新版)
- [ ] Edge (最新版)
- [ ] Chrome Mobile (Android)
- [ ] Safari Mobile (iOS)

### 6. 性能测试 (Performance Testing)

#### 大数据量测试
1. 创建 500+ 用户记录
2. 测试分页：
   - [ ] 加载速度正常（<2秒）
   - [ ] 翻页响应快速
   - [ ] 选择100每页加载正常
   - [ ] 无明显卡顿

#### 网络测试
1. 使用 Chrome DevTools 模拟慢速网络
2. 测试：
   - [ ] Fast 3G 下功能正常
   - [ ] Slow 3G 下可用
   - [ ] 离线时显示合适提示

## 自动化测试脚本 (Automated Test Scripts)

### Python 语法检查
```bash
cd /home/runner/work/shanxiu/shanxiu
python -m py_compile app/modules/core/routes.py
```

### 模板验证
```bash
python -c "
from jinja2 import Environment, FileSystemLoader
import os
template_dir = 'app/modules/core/templates'
env = Environment(loader=FileSystemLoader(template_dir))
for tmpl in ['pagination.html', 'users.html', 'auto_replies.html', 'start_messages.html', 'scheduled_messages.html', 'settings.html']:
    env.get_template(tmpl)
print('✅ All templates valid')
"
```

### HTML 验证
```bash
python -c "
from html.parser import HTMLParser
with open('UI_UPGRADE_SHOWCASE.html', 'r') as f:
    HTMLParser().feed(f.read())
print('✅ HTML valid')
"
```

## 问题报告 (Issue Reporting)

如果发现问题，请记录以下信息：

1. **问题描述**：简要说明问题
2. **重现步骤**：如何触发问题
3. **预期行为**：应该发生什么
4. **实际行为**：实际发生了什么
5. **环境信息**：
   - 浏览器和版本
   - 设备类型
   - 屏幕尺寸
   - 操作系统
6. **截图**：如果可能，提供截图

## 测试完成标准 (Test Completion Criteria)

✅ **所有测试通过条件：**
- [ ] 所有分页功能测试通过
- [ ] 移动端所有设备尺寸测试通过
- [ ] 所有8个机器人命令测试通过
- [ ] 权限检查正常工作
- [ ] 所有主流浏览器测试通过
- [ ] 性能测试满足要求
- [ ] 无严重或阻塞性bug

---

📝 **测试记录模板**

日期：_____________
测试人：_____________
环境：_____________

| 测试项 | 状态 | 备注 |
|--------|------|------|
| 分页功能 | ☐ Pass ☐ Fail | |
| 移动端 | ☐ Pass ☐ Fail | |
| Bot命令 | ☐ Pass ☐ Fail | |
| 浏览器兼容 | ☐ Pass ☐ Fail | |
| 性能 | ☐ Pass ☐ Fail | |

总体评价：☐ 通过 ☐ 不通过
