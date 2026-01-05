# 群底部按钮推送功能 (Group Bottom Button Push Feature)

## 功能说明 (Feature Description)

这个功能允许管理员在后台可视化地编辑群底部按钮和排序后，通过点击推送到群里，使其作为群组底部的持久化菜单键盘。

This feature allows administrators to visually edit group bottom buttons and their order in the backend, then push them to the group with one click to display as a persistent menu keyboard at the bottom of the group chat.

## 使用方法 (How to Use)

### 步骤 1: 配置按钮 (Step 1: Configure Buttons)

1. 登录管理后台
2. 导航到群组管理页面
3. 选择"群底部按钮"模块
4. 添加/编辑/排序按钮：
   - **按钮文本**: 显示在键盘上的文字
   - **按钮URL**: 点击按钮打开的链接（可选）
   - **回调数据**: 内联按钮的回调数据（可选）
   - **触发关键词**: 用户发送此关键词时自动显示按钮（可选）
   - **显示顺序**: 控制按钮的显示顺序
   - **启用/禁用**: 控制按钮是否激活

### 步骤 2: 推送到群组 (Step 2: Push to Group)

1. 在按钮列表页面，点击右上角的 **"推送到群组"** 按钮
2. 确认推送操作
3. 系统将自动发送带有菜单键盘的消息到群组
4. 所有群成员将看到持久化的菜单键盘

### 步骤 3: 用户使用 (Step 3: User Usage)

群成员可以：
- 点击底部菜单键盘上的按钮
- 使用 `/menu` 或 `/buttons` 命令显示菜单
- 发送配置的触发关键词显示特定按钮

## 功能特点 (Features)

### ✅ 可视化编辑 (Visual Editing)
- 直观的界面设计
- 拖拽排序（通过上移/下移按钮）
- 实时预览按钮配置

### ✅ 一键推送 (One-Click Push)
- 点击即可推送到群组
- 无需手动在 Telegram 中配置
- 自动同步所有激活的按钮

### ✅ 持久化显示 (Persistent Display)
- 菜单键盘持久显示在聊天底部
- 所有群成员都能看到
- 支持多行按钮布局

### ✅ 灵活配置 (Flexible Configuration)
- 支持 URL 链接
- 支持内联回调
- 支持关键词触发
- 支持启用/禁用控制

## 技术实现 (Technical Implementation)

### 前端 (Frontend)
- **UI**: Bootstrap 5 样式的管理界面
- **JavaScript**: 异步 API 调用，实时反馈
- **确认对话框**: 防止误操作

### 后端 (Backend)
- **API 端点**: `/core/api/push_group_bottom_buttons`
- **权限验证**: 需要管理员登录
- **异步处理**: 使用 `asyncio.run_coroutine_threadsafe`
- **错误处理**: 完善的错误捕获和提示

### Telegram Bot
- **ReplyKeyboardMarkup**: 持久化菜单键盘
- **KeyboardButton**: 按钮文本
- **resize_keyboard**: 自适应键盘大小
- **one_time_keyboard=False**: 持久显示

## 示例场景 (Example Scenarios)

### 场景 1: 群组导航菜单
配置按钮：
- 📋 群规
- 👥 成员列表
- 📊 积分查询
- 💰 抽奖活动
- 🎯 每日签到

### 场景 2: 快速链接菜单
配置按钮：
- 🌐 官方网站
- 📱 下载APP
- 💬 客服支持
- 📢 最新公告
- ⚙️ 设置

### 场景 3: 功能入口菜单
配置按钮：
- 🎮 互动游戏
- 🎁 领取红包
- 🗳️ 参与投票
- 🏆 竞拍专区
- 📈 数据统计

## API 说明 (API Documentation)

### 推送按钮到群组

**端点**: `POST /core/api/push_group_bottom_buttons`

**请求体**:
```json
{
  "group_id": 123
}
```

**响应**:
```json
{
  "status": "ok"
}
```

或错误响应:
```json
{
  "status": "error",
  "msg": "错误信息"
}
```

**权限**: 需要管理员登录

**超时**: 10秒

## 错误处理 (Error Handling)

### 常见错误及解决方案

| 错误信息 | 原因 | 解决方案 |
|---------|------|---------|
| "Auth required" | 未登录 | 请先登录管理后台 |
| "Group not found" | 群组不存在 | 检查群组ID是否正确 |
| "没有可推送的按钮" | 没有激活的按钮 | 至少添加并启用一个按钮 |
| "Bot未就绪，请稍后再试" | Bot未初始化 | 等待Bot启动完成 |
| "推送失败，请检查bot权限" | Bot权限不足 | 确保Bot在群组中且有发送消息权限 |

## 安全性 (Security)

### ✅ 已通过安全检查
- CodeQL 扫描: 0 个安全警报
- 身份验证: 必须登录才能推送
- 权限控制: 只有管理员可以操作
- 输入验证: 所有参数都经过验证
- 错误处理: 完善的异常捕获

## 兼容性 (Compatibility)

- ✅ Python 3.12+
- ✅ Flask 3.0+
- ✅ python-telegram-bot 20.7+
- ✅ PostgreSQL / SQLite
- ✅ 所有现代浏览器

## 版本历史 (Version History)

### v1.0.0 (2026-01-05)
- ✨ 初始版本发布
- ✨ 支持可视化编辑按钮
- ✨ 支持一键推送到群组
- ✨ 支持持久化菜单键盘
- ✨ 完整的文档和错误处理

## 相关功能 (Related Features)

- `/menu` 命令 - 在群组中显示菜单键盘
- `/buttons` 命令 - `/menu` 的别名
- 关键词触发 - 用户发送关键词自动显示按钮
- 自动回复集成 - 自动回复消息自动附带按钮
- /start 消息集成 - /start 消息自动附带按钮

## 支持 (Support)

如有问题或建议，请通过以下方式联系：
- GitHub Issues: https://github.com/Szchiji/shanxiu/issues
- 项目文档: 查看 BOT_FEATURES_IMPLEMENTATION.md

---

**最后更新**: 2026-01-05  
**作者**: Szchiji  
**许可证**: 根据项目许可证
