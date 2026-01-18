# Shanxiu - Telegram群组管理机器人系统

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.x-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-3.1.2-green.svg)](https://flask.palletsprojects.com/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-21.11.1-blue.svg)](https://python-telegram-bot.org/)

一个功能完备的Telegram群组管理机器人平台，提供Web管理界面和Telegram Bot功能，帮助群组管理员高效管理社群。

## ✨ 核心特性

- 🎯 **全面的用户管理**: 用户导入导出、过期管理、封禁系统
- 💬 **智能内容管理**: 自动回复、定时消息、自定义启动消息
- 🛡️ **强大的垃圾防护**: 内容过滤、频率限制、自动惩罚
- 🎁 **完整的积分系统**: 打卡奖励、消息积分、积分竞拍
- 🎲 **丰富的社交功能**: 群抽奖、投票、答题游戏、红包
- 📊 **数据统计分析**: 消息统计、用户活跃度、改名监控
- 🤖 **多机器人支持**: Bot克隆、独立配置管理
- 🔄 **自动化运营**: 定时开关群、消息同步、强制订阅

## 📚 文档

- **[详细设计方案](DESIGN_DOCUMENT.md)** - 完整的系统架构和技术设计文档
- **[功能实现指南](BOT_FEATURES_IMPLEMENTATION.md)** - 机器人功能实现详解
- **[实现总结](IMPLEMENTATION_SUMMARY_CN.md)** - 中文版实现总结
- **[测试指南](TESTING_GUIDE.md)** - 测试步骤和验证方法
- **[更新日志](CHANGELOG.md)** - 版本更新记录

## 🚀 快速开始

### 环境要求

- Python 3.x
- PostgreSQL / SQLite
- Telegram Bot Token

### 安装步骤

1. **克隆仓库**
```bash
git clone https://github.com/Szchiji/shanxiu.git
cd shanxiu
```

2. **安装依赖**
```bash
pip install -r requirements.txt
```

3. **配置环境变量**
```bash
# 创建 .env 文件
TG_BOT_TOKEN=your_bot_token_here
DATABASE_URL=postgresql://user:pass@host:port/dbname
SECRET_KEY=your_secret_key_here
ADMIN_ID=your_telegram_id
```

4. **启动应用**
```bash
python run.py
```

### 部署到Railway

1. Fork本仓库
2. 在Railway创建新项目
3. 连接GitHub仓库
4. 配置环境变量
5. 部署完成！

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────┐
│              用户界面层                      │
│  Web管理界面  │  Telegram客户端             │
└──────┬───────────────┬───────────────────────┘
       │               │
       ▼               ▼
┌─────────────────────────────────────────────┐
│              应用层                          │
│  Flask Web服务  │  Telegram Bot处理器       │
└──────┬───────────────┬───────────────────────┘
       │               │
       ▼               ▼
┌─────────────────────────────────────────────┐
│           数据层 (PostgreSQL/SQLite)         │
└─────────────────────────────────────────────┘
       ▲
       │
┌──────┴──────────────────────────────────────┐
│          后台任务层 (Job Queue)              │
└─────────────────────────────────────────────┘
```

详细架构设计请参阅 [设计文档](DESIGN_DOCUMENT.md#2-系统架构设计)。

## 🎯 主要功能模块

### 1. 用户管理
- 批量导入/导出 (Excel)
- 过期时间管理
- 自动封禁系统
- 用户资料自定义字段

### 2. 内容管理
- 关键词自动回复
- 定时消息发送
- 自定义 /start 消息
- 媒体支持 (文本/图片/视频)

### 3. 群组设置
- 进群验证
- 欢迎消息
- 退群封禁
- 垃圾消息防护
- 定时开关群

### 4. 积分系统
- 打卡签到奖励
- 发言积分
- 邀请奖励
- 积分竞拍

### 5. 社交互动
- 群抽奖 (发言数/排行榜)
- 群投票
- 答题游戏
- 红包功能

### 6. 高级功能
- 邀请活动追踪
- 强制频道订阅
- 成员等级系统
- 用户改名监控
- 关键词过滤
- 跨群消息同步

### 7. 自动化
- 自动删除系统消息
- 取消频道消息置顶
- 群组底部按钮
- 后台定时任务

### 8. 多实例
- Bot克隆
- 独立Token管理
- 分权管理

## 📊 数据库设计

系统使用30+张表支持完整功能，核心表包括：

- **BotGroup**: 群组基础信息和配置
- **GroupUser**: 群组用户和权限
- **UserPoints**: 用户积分系统
- **AutoReply**: 自动回复规则
- **ScheduledMessage**: 定时消息
- **GroupLottery**: 群抽奖
- **PointsAuction**: 积分竞拍

详细数据库设计请参阅 [设计文档](DESIGN_DOCUMENT.md#4-数据库设计)。

## 🔐 安全特性

- ✅ JWT身份认证 (7天有效期)
- ✅ Session安全 (HttpOnly, Secure, SameSite)
- ✅ SQL注入防护 (ORM参数化查询)
- ✅ XSS防护 (HTML转义)
- ✅ 敏感信息保护 (环境变量)
- ✅ 权限分级 (群主/管理员/用户)
- ✅ API速率限制

## 🔧 技术栈

- **后端框架**: Flask 3.1.2
- **ORM**: SQLAlchemy 2.0.45
- **数据库**: PostgreSQL / SQLite
- **Bot SDK**: python-telegram-bot 21.11.1
- **Web服务器**: Gunicorn 23.0.0
- **认证**: PyJWT 2.10.1
- **时区**: pytz 2025.2
- **Excel**: openpyxl 3.1.5

## 📝 API文档

系统提供完整的RESTful API：

- **用户管理**: `/api/save_user`, `/api/delete_user`, `/api/bulk_import_users`
- **群组设置**: `/api/save_settings`, `/api/save_fields`
- **内容管理**: `/api/save_auto_reply`, `/api/save_scheduled_message`
- **积分系统**: `/api/save_points_rule`, `/api/save_points_auction`
- **社交功能**: `/api/save_group_lottery`, `/api/save_group_vote`

详细API文档请参阅 [设计文档](DESIGN_DOCUMENT.md#5-api设计)。

## 🤖 Bot命令

### 用户命令
- `/start` - 启动机器人
- `/menu` - 显示菜单按钮
- `/userinfo` - 查看用户信息
- `/auction` - 查看竞拍
- `/bid <id> <金额>` - 竞拍出价

### 管理员命令
- `/kick @user` - 踢出用户
- `/ban @user` - 封禁用户
- `/mute @user` - 禁言用户
- `/warn @user` - 警告用户
- `/pin` - 置顶消息

详细命令文档请参阅 [设计文档](DESIGN_DOCUMENT.md#6-机器人命令与处理器设计)。

## 🎨 后台任务

系统运行7个后台定时任务：

| 任务 | 频率 | 功能 |
|------|------|------|
| 过期用户检查 | 1小时 | 自动禁言过期用户 |
| 定时消息 | 1分钟 | 发送定时消息 |
| 定时开关群 | 1分钟 | 按时间表开关群 |
| 订阅验证 | 1小时 | 验证频道订阅 |
| 等级更新 | 30分钟 | 更新用户等级 |
| 抽奖开奖 | 5分钟 | 执行到期抽奖 |
| 不活跃检查 | 24小时 | 检测不活跃用户 |

## 📈 性能优化

- ✅ 数据库索引优化
- ✅ 批量查询处理
- ✅ 配置缓存机制
- ✅ 异步并发处理
- ✅ API速率控制
- ✅ 连接池管理

详细优化策略请参阅 [设计文档](DESIGN_DOCUMENT.md#10-性能优化)。

## 🔄 扩展性

- ✅ 模块化设计
- ✅ 配置驱动
- ✅ 多机器人支持
- ✅ 插件系统 (计划中)
- ✅ API版本控制

## 📊 监控与日志

- ✅ 分级日志系统 (INFO/WARNING/ERROR/CRITICAL)
- ✅ 性能监控
- ✅ 数据库查询监控
- ✅ 错误追踪
- ✅ 功能使用统计

## 🤝 贡献

欢迎提交Issue和Pull Request！

## 📄 许可证

[MIT License](LICENSE)

## 📧 联系方式

- GitHub: [@Szchiji](https://github.com/Szchiji)
- Issue: [提交问题](https://github.com/Szchiji/shanxiu/issues)

## 🙏 致谢

感谢所有贡献者和使用者的支持！

---

**⭐ 如果这个项目对你有帮助，请给个Star支持一下！**
