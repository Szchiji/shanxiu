# Scheduled Message Race Condition Fix

## 问题描述 (Problem Description)

用户报告：即使定时消息已经从后台管理界面删除或设置为非激活状态 (`is_active=False`)，消息仍然会发送到群组中。

**English:** Users reported that scheduled messages were still being sent to groups even after being deleted or set to inactive (`is_active=False`) from the admin interface.

## 根本原因 (Root Cause)

**竞态条件 (Race Condition):**

在查询激活消息和实际发送之间存在竞态条件：

1. **初始查询** - 系统查询所有 `is_active=True` 的消息并加入发送列表
2. **管理员操作** - 管理员在此期间将消息设为停用或删除
3. **异步发送** - 系统从列表中异步发送消息，没有重新验证状态
4. **结果** - 已停用的消息仍然被发送

**English:** A race condition existed between querying active messages and actually sending them:
1. System queries all `is_active=True` messages and adds them to send list
2. Admin deactivates or deletes a message during this time
3. System sends messages from the list asynchronously without re-verification
4. Result: Deactivated messages are still sent

## 解决方案 (Solution)

### 多层保护机制 (Multi-layered Protection)

#### 1. 初始查询过滤 (Initial Query Filtering) ✓
```python
scheduled_messages = ScheduledMessage.query.options(
    joinedload(ScheduledMessage.group)
).filter(
    ScheduledMessage.is_active == True  # Filter active messages
).all()
```

#### 2. 会话缓存刷新 (Session Cache Refresh) ✓
```python
db.session.expire_all()  # Refresh cached data
```

#### 3. **新增：发送前验证 (NEW: Pre-send Verification)** ✓
```python
def _verify_still_active(msg_id):
    with global_flask_app.app_context():
        scheduled_msg = ScheduledMessage.query.get(msg_id)
        return scheduled_msg is not None and scheduled_msg.is_active

# Before sending each message
is_still_active = await asyncio.get_running_loop().run_in_executor(
    None, _verify_still_active, msg_data['id']
)

if not is_still_active:
    print(f"⏭️ 跳过消息 {msg_data['id']}：已被停用或删除", flush=True)
    continue
```

**说明:** 在每条消息发送前重新验证其状态，如果已被停用或删除则跳过。

**English:** Re-verifies message status before sending each message. Skips if deactivated or deleted.

#### 4. **新增：更新保护 (NEW: Update Protection)** ✓
```python
def _update_sent(msg_id, sent_msg_id):
    with global_flask_app.app_context():
        scheduled_msg = ScheduledMessage.query.get(msg_id)
        # Only update messages that are still active
        if scheduled_msg and scheduled_msg.is_active:
            scheduled_msg.last_sent_at = get_beijing_now()
            scheduled_msg.last_message_id = sent_msg_id
            db.session.commit()
```

**说明:** 只更新仍然激活的消息的发送时间和消息ID。

**English:** Only updates send time and message ID for messages that are still active.

## 代码变更 (Code Changes)

### 修改的文件 (Modified Files)

#### `app/modules/core/routes.py`

**位置 1 (Location 1):** Lines 3947-3963
- 添加 `_verify_still_active()` 辅助函数
- 在消息发送循环中添加状态验证
- Added `_verify_still_active()` helper function
- Added status verification in message sending loop

**位置 2 (Location 2):** Line 4021
- 在 `_update_sent()` 中添加 `is_active` 检查
- Added `is_active` check in `_update_sent()`

**总计修改 (Total Changes):** +15 行代码 / +15 lines of code

### 新增测试文件 (New Test Files)

#### 1. `test_scheduled_message_inactive.py`
全面测试套件，包含4个测试场景：
- ✅ 激活消息过滤测试
- ✅ 管理界面查询行为测试
- ✅ 消息删除场景测试
- ✅ 会话刷新测试

Comprehensive test suite with 4 test scenarios:
- ✅ Active message filtering test
- ✅ Admin UI query behavior test
- ✅ Message deletion scenario test
- ✅ Session refresh test

#### 2. `test_scheduled_message_race_condition.py`
竞态条件保护验证测试：
- ✅ 验证发送前验证逻辑
- ✅ 验证更新保护逻辑
- ✅ 验证所有保护层

Race condition protection verification test:
- ✅ Verifies pre-send verification logic
- ✅ Verifies update protection logic
- ✅ Verifies all protection layers

## 测试结果 (Test Results)

### 新增测试 (New Tests)
```
✅ test_scheduled_message_inactive.py - ALL 4 TESTS PASSED
✅ test_scheduled_message_race_condition.py - TEST PASSED
```

### 现有测试 (Existing Tests)
```
✅ test_fixes.py - ALL 3 TESTS PASSED
✅ Code compilation verified
```

### 安全扫描 (Security Scan)
```
✅ CodeQL scan - 0 vulnerabilities found
```

## 预期行为 (Expected Behavior)

### 场景 1: 激活消息 (Scenario 1: Active Messages)
- **状态:** `is_active=True`
- **行为:** 按计划发送 ✅
- **Behavior:** Sent as scheduled ✅

### 场景 2: 停用消息 (Scenario 2: Inactive Messages)
- **状态:** `is_active=False`
- **行为:** 不会被查询到，不会发送 ✅
- **Behavior:** Not queried, not sent ✅

### 场景 3: 发送期间停用 (Scenario 3: Deactivated During Send)
- **操作:** 管理员在查询后、发送前停用消息
- **行为:** 发送前验证检测到状态变化，跳过发送 ✅
- **Behavior:** Pre-send verification detects status change, skips sending ✅

### 场景 4: 已删除消息 (Scenario 4: Deleted Messages)
- **操作:** 管理员从数据库删除消息
- **行为:** 发送前验证检测到消息不存在，跳过发送 ✅
- **Behavior:** Pre-send verification detects message doesn't exist, skips sending ✅

## 性能影响 (Performance Impact)

- **额外数据库查询:** 每条待发送消息增加1次查询
- **优化措施:** 
  - 查询非常轻量（仅检查存在性和 `is_active` 状态）
  - 函数定义移到循环外部以提高性能
  - 使用异步执行器避免阻塞

**English:**
- **Additional DB queries:** 1 additional query per message to be sent
- **Optimizations:**
  - Query is very lightweight (only checks existence and `is_active` status)
  - Function definition moved outside loop for better performance
  - Uses async executor to avoid blocking

## 代码审查反馈 (Code Review Feedback)

所有代码审查意见已处理：
- ✅ 将 `_verify_still_active` 函数移到循环外部
- ✅ 修复测试中的 SQL 注入问题（使用参数化查询）
- ✅ 使测试语言无关（移除中文字符串依赖）

All code review feedback addressed:
- ✅ Moved `_verify_still_active` function outside loop
- ✅ Fixed SQL injection in test (used parameterized query)
- ✅ Made test language-agnostic (removed Chinese string dependency)

## 总结 (Summary)

此修复通过在消息发送前添加额外的验证步骤，成功解决了定时消息竞态条件问题。现在即使管理员在消息查询后、发送前停用或删除消息，系统也会正确跳过这些消息，确保只有真正激活的消息才会被发送。

**English:** This fix successfully resolves the scheduled message race condition by adding an additional verification step before sending. Now even if an admin deactivates or deletes a message after it's queried but before it's sent, the system correctly skips those messages, ensuring only truly active messages are sent.

## 兼容性 (Compatibility)

- ✅ 向后兼容 (Backward compatible)
- ✅ 不影响现有功能 (No impact on existing features)
- ✅ 所有现有测试通过 (All existing tests pass)
- ✅ 无安全漏洞 (No security vulnerabilities)

---

**修复日期 (Fix Date):** 2026-01-26
**测试状态 (Test Status):** ✅ ALL TESTS PASSED
**安全扫描 (Security Scan):** ✅ NO VULNERABILITIES
