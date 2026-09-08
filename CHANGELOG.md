# 更新日志 (Changelog)

## [Unreleased] - 2026-09-08

### ✨ 新增功能 (Added)

#### 认证用户自定义成员标签
- `group_users` 新增 `member_tags` 字段（JSON 字符串数组），可为每位认证用户设置自定义标签（如 VIP / 核心 / 管理）
- 认证用户管理页支持编辑、列表展示、按标签搜索
- 列表模板与频道推送模板支持占位符 `{标签}`（渲染为 `#VIP #核心`）
- 用户导入/导出 XLSX 增加「标签」列；数据库迁移兼容旧库

### 🐛 问题修复 (Fixed)

#### 自动点赞换图标后不生效
- 根因：Telegram `setMessageReaction` 只接受官方「消息回应」白名单表情（如 `❤`/`👍`/`🔥`），任意图标、贴纸或自定义 emoji 会被 API 拒绝；旧默认值 `❤️`（带 FE0F）与部分粘贴变体也会导致失败，且失败被静默吞掉
- 新增 `normalize_like_emoji`：自动把 `❤️` 等变体映射为 API 合法形式，并拒绝无效图标
- 保存群设置时校验点赞表情；设置页增加白名单快捷选择与说明
- `do_like` 记录 API 失败日志，便于排查

## [Unreleased] - 2026-09-06

### 🔧 优化改进 (Improved)

#### 列表统一为「定时消息」展示风格
- 列表以紧凑元数据列为主（ID / 类型 badge / 状态 / 时间 / 操作），不再整段展开正文
- 长文本（关键词、内容、详情、描述）使用与定时消息「备注」相同的短预览 + 悬停查看全文
- 名称+副信息、目标聊天等保留双行元数据栈（与定时消息「目标」列一致）
- 频道模板 / 转发规则 / 优惠券 / 统计等表格结构对齐定时消息列表
- **认证用户列表**对齐：独立 ID 列、紧凑用户名栈、资料字段短预览、过期时间 `mm-dd HH:MM:SS`、外置批量操作栏、空状态行

## [Unreleased] - 2026-01-26

### ✨ 新增功能 (Added)

#### 邀请活动群内公告功能（Invitation Activity Group Announcement）
- 新增 `announce_in_group` 字段到 `invitation_activity` 表
- 支持在群内公告邀请成功消息，提升活动参与度
- 添加了用户界面开关，管理员可以选择是否启用群内公告
- 完整的数据库迁移脚本支持，使用 `IF NOT EXISTS` 确保迁移安全
- 后端代码具有完善的错误处理，兼容尚未迁移的数据库
- 使用安全的属性访问模式（`getattr`），防止属性不存在时报错

### 🔧 优化改进 (Improved)
- 优化邀请活动设置页面，新增"群内公告邀请成功"选项
- 改进数据库迁移的容错性，自动检测列是否存在
- 增强前端表单，支持保存 `announce_in_group` 配置

## [Unreleased] - 2026-01-05

### ✨ 新增功能 (Added)

#### 积分自动回复功能（Points-based Auto-Reply）
- 实现了积分自动回复系统，用户可通过消耗积分获取高级内容
- 自动检查用户积分余额，不足时显示友好的错误提示
- 积分扣除与内容发送采用事务处理，确保数据一致性
- 完整的交易日志记录，支持审计和追溯
- 扣除积分后自动通知用户剩余余额
- 异常情况下自动回滚，防止积分丢失

#### 抽奖消息跟踪系统（Lottery Message Tracking）
- 新增 `LotteryMessageCount` 数据模型，实时跟踪用户在抽奖期间的消息数
- 支持两种抽奖类型的消息跟踪：
  - **消息计数抽奖**：根据消息数量加权随机选择获奖者（发送越多，中奖概率越高）
  - **消息排名抽奖**：根据实际消息数量选择前 N 名活跃用户
- 仅在抽奖活动期间（start_time 到 end_time）跟踪消息
- 自动更新消息计数和时间戳
- 获奖者选择基于真实活动数据，确保公平性

#### 同步消息日志功能
- 新增 `SyncMessageLog` 数据模型，用于跟踪所有同步的消息
- 创建了同步消息日志页面，支持查看历史同步记录
- 显示消息发送者、类型、内容预览、目标群组和同步状态
- 完整的分页支持（10、20、50、100 条/页）
- 空状态提示和快速配置入口

#### 用户信息查询命令
- 新增 `/userinfo` 机器人命令，管理员专用
- 支持回复或转发用户消息查看详细信息
- 显示用户基本信息：ID、用户名、姓氏、是否机器人
- 显示群组信息：个人资料、到期时间、封禁状态、签到记录
- 显示积分信息：当前积分、总获得、总消耗（使用数据库聚合优化）
- 显示 Telegram 群组权限：群主、管理员、普通成员等状态

#### 统一分页系统
- 创建了统一的分页组件 (`pagination.html`)，可在所有列表页面重复使用
- 支持更大的每页显示数量：10、20、50、**100**
- 统一的翻页按钮样式，带有现代化的渐变效果和动画
- 响应式设计，在移动设备和桌面设备上都有良好体验

#### 群管理机器人功能 (Group Management Bot)
添加了完整的群管理机器人命令，仅限群组管理员使用：

- `/kick` - 踢出群成员（可重新加入）
- `/ban` - 永久封禁群成员
- `/unban` - 解除用户封禁
- `/mute [分钟]` - 禁言用户指定时间（默认60分钟）
- `/unmute` - 解除用户禁言
- `/pin` - 置顶消息
- `/unpin` - 取消置顶（不回复消息则取消所有置顶）
- `/warn [原因]` - 警告用户
- `/userinfo` - 查看用户详细信息（新增）

所有命令都需要回复目标用户的消息使用，并自动检查操作者是否为群组管理员。

### 🎨 UI/UX 改进 (Improved)

#### 同步消息模块 UI 升级
- 同步设置页面新增"查看日志"按钮，快速访问历史记录
- 同步日志页面新增"同步设置"按钮，快速返回配置
- 侧边栏导航分离显示"同步设置"和"同步日志"
- 响应式设计优化，移动端显示更友好
- 页面头部按钮支持自动换行和间距调整

#### 移动端全面优化
- **同步消息设置页面**：
  - 响应式页面头部（1rem → 1.5rem 标题字体）
  - 优化表单控件字体大小（0.85rem 移动端）
  - 按钮尺寸自适应（0.85rem → 0.95rem）
- **同步消息日志页面**：
  - 隐藏移动端不重要列（发送者、类型）
  - 内容预览宽度自适应（150px → 300px）
  - 表格字体大小优化（0.85rem → 0.95rem）
  - 卡片圆角和阴影自适应

#### 分页 UI 升级
- **现代化按钮样式**：使用渐变色和阴影效果
- **流畅动画**：悬停时按钮会有上升动画和颜色变化
- **一致的设计语言**：所有列表页面（用户、自动回复、定时消息、启动消息、同步日志）使用统一的分页组件
- **禁用状态**：清晰的禁用状态样式，用户体验更好

#### 设置页面增强
- 新增"群管理机器人命令"板块，展示所有可用的管理命令
- 使用彩色渐变卡片展示每个命令，视觉效果更佳
- 详细说明每个命令的功能和用法
- 新增 `/userinfo` 命令文档

### 🔧 技术改进 (Changed)

#### 后端优化
- 所有分页路由支持 100 条/页的选项
- 统一的参数验证逻辑
- 更好的代码复用
- **积分计算优化**：使用 SQL 聚合函数（CASE + SUM）替代内存循环，显著提升性能
- **异常处理改进**：使用 `except Exception` 替代裸 `except`，避免捕获系统级异常

#### 数据库优化
- 新增 `SyncMessageLog` 模型，支持同步消息追踪
- 添加索引：`synced_at` 字段索引，优化时间范围查询
- 外键关系：`source_group_id` 关联 `bot_groups` 表

#### 前端优化
- 移除重复的 JavaScript 函数
- 使用 Jinja2 宏 (macro) 实现组件复用
- 减少代码冗余，提高可维护性

### 📄 文件变更 (Files Changed)

**新增文件：**
- `app/modules/core/templates/pagination.html` - 统一分页组件
- `app/modules/core/templates/sync_message_logs.html` - 同步消息日志页面

**修改文件：**
- `app/models.py` - 添加 `SyncMessageLog` 模型
- `app/modules/core/routes.py` - 添加同步日志路由、`/userinfo` 命令、积分计算优化
- `app/modules/core/templates/base.html` - 更新侧边栏导航（同步设置/日志分离）
- `app/modules/core/templates/sync_group_messages.html` - 添加"查看日志"按钮，移动端优化
- `app/modules/core/templates/settings.html` - 添加 `/userinfo` 命令文档
- `app/modules/core/templates/users.html` - 使用新的分页组件
- `app/modules/core/templates/auto_replies.html` - 使用新的分页组件
- `app/modules/core/templates/start_messages.html` - 使用新的分页组件
- `app/modules/core/templates/scheduled_messages.html` - 更新分页样式

---

## 使用说明 (Usage)

### 如何查看同步消息日志

1. 在侧边栏导航中点击"同步日志"
2. 或在"同步设置"页面点击"查看日志"按钮
3. 日志显示所有同步记录，包括：
   - 同步时间
   - 发送者信息
   - 消息类型（文本、图片、视频等）
   - 内容预览
   - 目标群组
   - 同步状态（成功、失败、过滤）

### 如何使用 /userinfo 命令

1. 确保机器人在群组中拥有管理员权限
2. **方式一**：回复目标用户的消息，然后发送 `/userinfo`
3. **方式二**：将目标用户的消息转发给机器人，然后发送 `/userinfo`
4. 机器人会返回详细的用户信息，包括：
   - 基本信息（用户名、ID、姓名）
   - 群组信息（个人资料、到期时间、封禁状态）
   - 积分信息（当前积分、总获得、总消耗）
   - 群组权限（群主、管理员、普通成员等）

### 管理员如何使用群管理命令

1. 确保机器人在群组中拥有管理员权限
2. 回复目标用户的任意消息
3. 发送相应的命令，例如：
   ```
   /kick          # 踢出用户
   /mute 30       # 禁言30分钟
   /warn 违反群规  # 警告用户
   /userinfo      # 查看用户详细信息
   ```

### 如何查看所有命令

在群组设置页面中，找到"群管理机器人命令"板块，查看所有可用命令及其详细说明。

---

## 技术细节 (Technical Details)

### 同步消息日志模型

```python
class SyncMessageLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    target_group_id = db.Column(db.String(50), nullable=False)
    source_message_id = db.Column(db.BigInteger, nullable=True)
    target_message_id = db.Column(db.BigInteger, nullable=True)
    user_id = db.Column(db.BigInteger, nullable=True)
    username = db.Column(db.String(255), nullable=True)
    message_type = db.Column(db.String(20), default='text')
    content_preview = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='success')
    error_message = db.Column(db.Text, nullable=True)
    synced_at = db.Column(db.DateTime, default=datetime.now, index=True)
```

### 积分计算优化

使用 SQL 聚合函数进行高效计算：

```python
from sqlalchemy import func, case

result = db.session.query(
    func.sum(case((PointsLog.points_change > 0, PointsLog.points_change), else_=0)).label('earned'),
    func.sum(case((PointsLog.points_change < 0, func.abs(PointsLog.points_change)), else_=0)).label('spent')
).filter(
    PointsLog.group_id == group.id,
    PointsLog.user_id == target_user.id
).first()
```

### 分页组件 API

```jinja2
{% from 'pagination.html' import pagination %}
{{ pagination(current_page, total_pages, per_page, total_items) }}
```

参数说明：
- `current_page`: 当前页码
- `total_pages`: 总页数
- `per_page`: 每页显示数量
- `total_items`: 总记录数

### 支持的每页数量
- 10 条/页
- 20 条/页（默认）
- 50 条/页
- 100 条/页（新增）

---

## 测试检查清单 (Testing Checklist)

- [x] Python 语法检查通过
- [x] Jinja2 模板语法验证通过
- [x] 所有列表页面使用统一分页组件
- [x] 分页按钮样式统一
- [x] 响应式设计在移动端正常工作
- [x] 群管理命令已注册
- [x] 管理员权限检查逻辑正确
- [ ] 在真实群组环境中测试管理命令
- [ ] 在真实环境中测试分页功能
- [ ] 移动设备浏览器测试

---

## 后续优化建议 (Future Improvements)

1. **性能优化**
   - 考虑为大数据量添加缓存机制
   - 优化数据库查询

2. **功能增强**
   - 添加批量操作功能
   - 添加操作日志记录
   - 群管理命令添加权限配置

3. **用户体验**
   - 添加键盘快捷键支持
   - 实现无刷新分页（AJAX）
   - 添加页面跳转输入框

4. **移动端优化**
   - 考虑添加下拉刷新
   - 优化触摸操作体验
   - PWA 支持
