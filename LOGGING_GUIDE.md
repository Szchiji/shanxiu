# 抽奖和邀请功能日志说明
# Lottery and Invitation Feature Logging Guide

## 日志格式 / Log Format

所有日志使用以下格式：
All logs use the following format:

```
YYYY-MM-DD HH:MM:SS - module.name - LEVEL - message
```

## 日志级别 / Log Levels

- **INFO**: 正常操作信息 / Normal operation information
- **DEBUG**: 详细调试信息 / Detailed debugging information  
- **WARNING**: 警告信息 / Warning messages
- **ERROR**: 错误信息 / Error messages

## 抽奖功能日志 / Lottery Feature Logs

### 1. 抽奖任务执行 / Lottery Task Execution

**标签**: `🎲 [抽奖任务]`

#### 成功场景 / Success Scenarios

```
INFO - 🎲 [抽奖任务] 发现 2 个需要开奖的抽奖活动: [1, 2]
INFO - 🎲 [抽奖任务] 开始处理抽奖 '春节活动' (ID: 1, 类型: message_count, 群组: 测试群)
INFO - 🎲 [抽奖任务] 消息计数抽奖: 找到 15 个参与者
INFO - 🎲 [抽奖任务] 选中获奖者: 用户 123456789 (消息数: 50)
INFO - ✅ [抽奖任务] 抽奖 '春节活动' (ID: 1) 已结束，获奖者: [123456789]
INFO - 📢 [抽奖任务] 成功在群组 -1001234567890 发送获奖公告
```

#### 警告场景 / Warning Scenarios

```
WARNING - 🎲 [抽奖任务] global_flask_app 未初始化
WARNING - 🎲 [抽奖任务] 抽奖 ID 999 未找到
WARNING - 🎲 [抽奖任务] 群组 ID 888 未找到
WARNING - 🎲 [抽奖任务] 无消息记录，使用随机备选方案
```

#### 错误场景 / Error Scenarios

```
ERROR - ❌ [抽奖任务] 发送获奖公告失败 (群组: -1001234567890): Forbidden: bot was kicked from the supergroup chat
ERROR - ❌ [抽奖任务] 处理抽奖 1 时出错: division by zero
ERROR - ❌ [抽奖任务] run_lottery_draws 执行失败: Database connection lost
```

### 2. 抽奖状态更新 / Lottery Status Updates

**标签**: `🎲 [状态更新]`

```
INFO - ✅ [状态更新] 已更新 3 个抽奖状态: pending -> active
INFO - ✅ [状态更新] 抽奖 '周末活动' (ID: 5) 已激活
WARNING - 🎲 [状态更新] 抽奖 '过期活动' (ID: 6) 已过期，跳过激活
ERROR - ❌ [状态更新] update_lottery_status 执行失败: ...
```

### 3. 消息计数追踪 / Message Count Tracking

**标签**: `🎲 [消息计数]`

```
DEBUG - 🎲 [消息计数] 用户 123456789 在群组 测试群 发送消息，追踪 2 个活动抽奖
DEBUG - 🎲 [消息计数] 为用户 123456789 创建抽奖 '春节活动' (ID: 1) 的消息计数记录
DEBUG - 🎲 [消息计数] 用户 123456789 抽奖 '春节活动' 消息计数: 15
ERROR - ❌ [消息计数] 提交抽奖追踪数据失败: ...
```

## 邀请功能日志 / Invitation Feature Logs

**标签**: `👥 [入群事件]`, `🎁 [邀请活动]`

### 1. 用户加入群组 / User Joins Group

```
INFO - 👥 [入群事件] 检测到新成员加入群组 测试群 (ID: -1001234567890)
INFO - 👥 [入群事件] 处理新成员: 张三 (ID: 123456789)
INFO - 👥 [入群事件] 跳过机器人: BotName
WARNING - 👥 [入群事件] 群组未激活或未找到: -1001234567890
INFO - 👥 [入群事件] 未检测到邀请者或自行加入
```

### 2. 邀请追踪 / Invitation Tracking

```
INFO - 🎁 [邀请活动] 检测到邀请者: 987654321
INFO - 🎁 [邀请活动] 邀请活动已启用 (奖励: 10 积分)
INFO - 🎁 [邀请活动] 创建新的邀请记录: 987654321 -> 123456789
INFO - ✅ [邀请活动] 邀请记录成功: 987654321 邀请了 123456789 (张三), 积分: 100 -> 110
INFO - ✅ [邀请活动] 数据库提交成功
INFO - 📢 [邀请活动] 成功在群组 -1001234567890 发送邀请公告
```

### 3. 警告和错误 / Warnings and Errors

```
INFO - 🎁 [邀请活动] 邀请记录已存在，跳过 (被邀请者: 123456789)
INFO - 🎁 [邀请活动] 群组 测试群 未启用邀请活动
ERROR - ❌ [邀请活动] 数据库提交失败: IntegrityError
ERROR - ❌ [邀请活动] 发送邀请公告失败: ...
ERROR - ❌ [入群事件] handle_new_chat_member 执行失败: ...
```

## 如何启用调试日志 / How to Enable Debug Logs

在 `run.py` 中修改日志级别：
Modify the log level in `run.py`:

```python
# 启用所有 DEBUG 日志 / Enable all DEBUG logs
logging.basicConfig(level=logging.DEBUG, ...)

# 或只针对特定模块 / Or only for specific modules
logging.getLogger('app.modules.core.routes').setLevel(logging.DEBUG)
```

## 常见问题诊断 / Common Issue Diagnostics

### 问题1: 抽奖未自动开奖
**Problem 1: Lottery not automatically drawn**

查找日志中的：
Look for in logs:

```
🎲 [抽奖任务] 发现 X 个需要开奖的抽奖活动
```

- 如果找不到此日志 → 检查 `run_lottery_draws()` 是否正常运行
- If not found → Check if `run_lottery_draws()` is running
- 如果找到但无后续处理日志 → 检查错误日志
- If found but no follow-up → Check error logs

### 问题2: 邀请活动不生效
**Problem 2: Invitation activity not working**

查找日志中的：
Look for in logs:

```
👥 [入群事件] 检测到新成员加入群组
🎁 [邀请活动] 检测到邀请者
```

- 如果找不到入群日志 → 检查 `handle_new_chat_member()` 是否注册
- If join log not found → Check if `handle_new_chat_member()` is registered
- 如果找到入群但无邀请日志 → 检查邀请活动是否启用
- If join found but no invitation log → Check if invitation activity is enabled

### 问题3: 消息计数不准确
**Problem 3: Message count inaccurate**

启用 DEBUG 级别日志查看：
Enable DEBUG level to see:

```
🎲 [消息计数] 用户 X 抽奖 Y 消息计数: Z
```

检查：
Check:
- 时间窗口是否正确
- Time window is correct
- 抽奖状态是否为 'active'
- Lottery status is 'active'
- 数据库提交是否成功
- Database commit succeeds

## 性能影响 / Performance Impact

- **INFO 级别**: 最小影响 / Minimal impact
- **DEBUG 级别**: 轻微影响（在高流量群组中不推荐24/7开启）
- **DEBUG level**: Slight impact (not recommended 24/7 in high-traffic groups)

## 建议 / Recommendations

1. 生产环境使用 INFO 级别
   Use INFO level in production

2. 调试时临时启用 DEBUG 级别
   Temporarily enable DEBUG when debugging

3. 定期检查 ERROR 日志
   Regularly check ERROR logs

4. 保存日志到文件用于长期分析
   Save logs to file for long-term analysis
