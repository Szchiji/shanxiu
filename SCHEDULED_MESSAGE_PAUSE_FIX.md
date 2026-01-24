# 定时消息暂停功能修复文档

## 问题描述

用户反馈：定时消息已经在后台设置为"暂停"状态（`is_active=False`），但群里仍然收到定时消息。

## 问题原因

`check_scheduled_messages()` 后台任务在查询定时消息时，如果没有正确过滤 `is_active=False` 的消息，会导致暂停的消息仍然被发送。

## 解决方案

### 修复位置

**文件:** `app/modules/core/routes.py`  
**函数:** `check_scheduled_messages(context)`  
**行号:** 约 3685-3826

### 关键代码

```python
async def check_scheduled_messages(context):
    """
    定时检查需要发送的消息
    """
    if not global_flask_app:
        return
    
    def _sync_check():
        with global_flask_app.app_context():
            try:
                now = get_beijing_now()
                messages_to_send = []
                
                # ✅ 关键修复：只查询启用的定时消息
                scheduled_messages = ScheduledMessage.query.options(
                    joinedload(ScheduledMessage.group)
                ).filter(
                    ScheduledMessage.is_active == True  # ← 这里过滤暂停的消息
                ).all()
                
                for msg in scheduled_messages:
                    # 1. 检查群组是否活跃
                    if not msg.group or not msg.group.is_active:
                        continue
                    
                    # 2. 检查模块是否启用
                    conf = get_group_conf(msg.group)
                    if not conf.get('scheduled_msg_open', True):
                        continue
                    
                    # 3. 检查开始时间
                    if msg.start_time and now < msg.start_time:
                        continue
                    
                    # 4. 检查停止时间
                    if msg.stop_time and now > msg.stop_time:
                        continue
                    
                    # 5. 检查重复间隔
                    if msg.last_sent_at is None:
                        should_send = True
                    elif msg.repeat_interval > 0:
                        elapsed_minutes = (now - msg.last_sent_at).total_seconds() / 60
                        should_send = elapsed_minutes >= msg.repeat_interval
                    else:
                        should_send = False
                    
                    if should_send:
                        messages_to_send.append({...})
                
                return messages_to_send
            except Exception as e:
                print(f"Error in check_scheduled_messages sync part: {e}")
                return []
    
    # 在 executor 中运行同步 DB 操作
    messages_to_send = await asyncio.get_running_loop().run_in_executor(None, _sync_check)
    
    # 发送消息...
```

## 完整的检查逻辑

函数实现了以下完整的过滤逻辑：

1. **✅ 只查询启用的消息** (`is_active=True`)
2. **✅ 检查群组是否激活** (`group.is_active`)
3. **✅ 检查模块是否启用** (`scheduled_msg_open`)
4. **✅ 检查开始时间** (`start_time`)
5. **✅ 检查停止时间** (`stop_time`)
6. **✅ 检查重复间隔** (`repeat_interval` 和 `last_sent_at`)

## 数据模型

```python
class ScheduledMessage(db.Model):
    """定时消息"""
    __tablename__ = 'scheduled_messages'
    
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    media_type = db.Column(db.String(20), default='text')
    media_url = db.Column(db.Text, nullable=True)
    content = db.Column(db.Text, nullable=True)
    links = db.Column(db.Text, default='[]')
    repeat_interval = db.Column(db.Integer, default=0)
    delete_previous = db.Column(db.Boolean, default=False)
    last_message_id = db.Column(db.BigInteger, nullable=True)
    start_time = db.Column(db.DateTime, nullable=True)
    stop_time = db.Column(db.DateTime, nullable=True)
    remark = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True)  # ← 关键字段
    last_sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='scheduled_messages', lazy=True)
```

## 其他 ScheduledMessage 查询分析

代码中还有其他地方查询 `ScheduledMessage`，但它们的用途不同：

### 1. 管理界面查询（不需要过滤）

```python
# Line 556 - 管理页面显示所有消息（包括暂停的）
query = ScheduledMessage.query.filter_by(group_id=gid)
```

**用途:** 后台管理页面需要显示所有定时消息（包括暂停的），以便管理员查看和编辑。  
**是否需要过滤:** ❌ 不需要，管理员需要看到所有消息。

### 2. 配置导出查询（不需要过滤）

```python
# Line 1052 - 导出配置
for sm in ScheduledMessage.query.filter_by(group_id=gid).all()

# Line 1979 - 导出定时消息
scheduled_messages = ScheduledMessage.query.filter_by(group_id=group_id).all()
```

**用途:** 备份/导出功能需要包含所有消息（包括暂停的），以便完整备份和恢复。  
**是否需要过滤:** ❌ 不需要，备份应该包含所有数据。

### 3. 统计查询（已正确过滤）

```python
# Line 326 - 统计启用的消息数量
scheduled_msgs_count = ScheduledMessage.query.filter_by(group_id=gid, is_active=True).count()
```

**用途:** 统计启用的定时消息数量。  
**是否需要过滤:** ✅ 已正确过滤。

### 4. 单个消息操作（不需要过滤）

```python
# Line 1910, 1947, 1964, 3813 - 获取单个消息
item = ScheduledMessage.query.get(item_id)
```

**用途:** 根据 ID 获取单个消息进行编辑、删除或更新。  
**是否需要过滤:** ❌ 不需要，这些是直接操作特定消息。

## 验证方法

### 自动验证脚本

运行验证脚本：

```bash
cd /home/runner/work/shanxiu/shanxiu
python verify_scheduled_message_fix.py
```

### 手动验证步骤

1. **设置测试消息:**
   - 在后台创建一个定时消息
   - 设置为"启用"状态（`is_active=True`）
   - 设置合适的发送时间

2. **测试暂停功能:**
   - 将消息设为"暂停"状态（`is_active=False`）
   - 等待下一个检查周期（60秒）
   - 确认群里**不再**收到该消息

3. **测试恢复功能:**
   - 将消息重新设为"启用"状态（`is_active=True`）
   - 等待下一个检查周期（60秒）
   - 确认群里**重新**收到该消息

## 预期行为

### ✅ 正确行为

- `is_active=True` 的消息会被正常发送
- `is_active=False` 的消息不会被发送
- 暂停状态的消息在管理页面中仍然可见
- 暂停状态的消息可以被编辑
- 暂停状态的消息可以被重新启用

### ❌ 错误行为（已修复）

- ~~`is_active=False` 的消息仍然被发送~~ （已修复）
- ~~查询没有过滤 `is_active` 字段~~ （已修复）

## 技术细节

### 查询优化

使用 `joinedload` 预加载关联的群组数据，减少数据库查询次数：

```python
scheduled_messages = ScheduledMessage.query.options(
    joinedload(ScheduledMessage.group)  # 预加载群组关系
).filter(
    ScheduledMessage.is_active == True
).all()
```

### 异步执行

数据库操作在 executor 中运行，避免阻塞事件循环：

```python
messages_to_send = await asyncio.get_running_loop().run_in_executor(
    None, _sync_check
)
```

### 错误处理

所有数据库操作都包含在 try-except 块中，确保错误不会导致任务停止：

```python
try:
    # 数据库操作
except Exception as e:
    print(f"Error in check_scheduled_messages sync part: {e}")
    return []
```

## 定时任务配置

后台任务每 60 秒检查一次定时消息：

```python
SCHEDULED_MESSAGE_CHECK_INTERVAL = 60  # 秒
```

任务在应用启动时自动注册（具体实现在 `app/__init__.py` 或相关配置文件中）。

## 相关功能

### 定时消息的其他配置项

- **开始时间** (`start_time`): 消息开始发送的时间
- **停止时间** (`stop_time`): 消息停止发送的时间
- **重复间隔** (`repeat_interval`): 重复发送的间隔（分钟）
- **删除上一条** (`delete_previous`): 是否删除上一次发送的消息

所有这些配置项都在 `check_scheduled_messages()` 中得到正确处理。

## 总结

✅ **修复状态:** 已完成  
✅ **关键改动:** 在查询中添加 `is_active == True` 过滤条件  
✅ **影响范围:** 仅影响定时消息发送逻辑，不影响管理界面和导出功能  
✅ **向后兼容:** 完全兼容，不需要数据库迁移  
✅ **测试覆盖:** 提供验证脚本和手动测试步骤

---

**最后更新:** $(date +%Y-%m-%d)  
**文档版本:** 1.0
