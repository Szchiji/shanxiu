# 数据库迁移指南 / Database Migration Guide

## 概述 / Overview

本文档描述了应用所需的数据库结构变更以及如何应用这些变更。

This document describes the database schema changes required by the application and how to apply them.

## 自动迁移 / Automatic Migration

应用启动时会自动检查并添加缺失的数据库列。但是，建议在部署新版本前手动运行迁移脚本。

The application automatically checks and adds missing database columns on startup. However, it's recommended to manually run the migration script before deploying new versions.

### 方法 1：使用迁移脚本 / Method 1: Using Migration Script

```bash
python migrate_database.py
```

这个脚本会：
- 检查每个必需的列是否存在
- 只添加缺失的列（幂等操作）
- 提供详细的执行日志

The script will:
- Check if each required column exists
- Only add missing columns (idempotent operation)
- Provide detailed execution logs

### 方法 2：应用启动时自动修复 / Method 2: Auto-fix on Application Start

```bash
python run.py
```

应用的 `fix_database_schema()` 函数会在启动时自动添加缺失的列。

The application's `fix_database_schema()` function automatically adds missing columns on startup.

## 必需的数据库变更 / Required Database Changes

### 1. bot_groups.members_last_sync

**用途 / Purpose**: 追踪群成员最后同步时间 / Track last group member synchronization time

**添加时间 / Added**: 2025-01

**SQL 命令 / SQL Command**:
```sql
ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS members_last_sync TIMESTAMP NULL;
```

**影响的功能 / Affected Features**:
- 群成员同步接口 `/core/api/sync_group_members`
- 群成员列表页面 `/core/group/<id>/members`
- 定时任务中的群成员查询

**错误信息（如果未迁移）/ Error Message (if not migrated)**:
```
psycopg2.errors.UndefinedColumn: column bot_groups.members_last_sync does not exist
```

### 2. group_users.is_muted_permanent

**用途 / Purpose**: 标记用户是否被永久禁言 / Mark if user is permanently muted

**SQL 命令 / SQL Command**:
```sql
ALTER TABLE group_users ADD COLUMN IF NOT EXISTS is_muted_permanent BOOLEAN DEFAULT FALSE;
```

### 3. group_users.mute_reason

**用途 / Purpose**: 记录禁言原因 / Record mute reason

**SQL 命令 / SQL Command**:
```sql
ALTER TABLE group_users ADD COLUMN IF NOT EXISTS mute_reason VARCHAR(255);
```

### 4. invitation_activity.announce_in_group

**用途 / Purpose**: 控制邀请活动是否在群内公告 / Control whether invitation activity is announced in group

**SQL 命令 / SQL Command**:
```sql
ALTER TABLE invitation_activity ADD COLUMN IF NOT EXISTS announce_in_group BOOLEAN DEFAULT FALSE;
```

## 手动迁移步骤 / Manual Migration Steps

如果自动迁移失败，可以手动执行以下步骤：

If automatic migration fails, you can manually execute the following steps:

### PostgreSQL

```sql
-- 连接到数据库 / Connect to database
psql $DATABASE_URL

-- 执行迁移 / Execute migrations
ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS members_last_sync TIMESTAMP NULL;
ALTER TABLE group_users ADD COLUMN IF NOT EXISTS is_muted_permanent BOOLEAN DEFAULT FALSE;
ALTER TABLE group_users ADD COLUMN IF NOT EXISTS mute_reason VARCHAR(255);
ALTER TABLE invitation_activity ADD COLUMN IF NOT EXISTS announce_in_group BOOLEAN DEFAULT FALSE;

-- 验证列已添加 / Verify columns added
\d bot_groups
\d group_users
\d invitation_activity
```

### SQLite

```sql
-- 连接到数据库 / Connect to database
sqlite3 bot.db

-- 执行迁移 / Execute migrations
ALTER TABLE bot_groups ADD COLUMN members_last_sync TIMESTAMP NULL;
ALTER TABLE group_users ADD COLUMN is_muted_permanent BOOLEAN DEFAULT 0;
ALTER TABLE group_users ADD COLUMN mute_reason VARCHAR(255);
ALTER TABLE invitation_activity ADD COLUMN announce_in_group BOOLEAN DEFAULT 0;

-- 验证列已添加 / Verify columns added
.schema bot_groups
.schema group_users
.schema invitation_activity
```

## 验证迁移 / Verify Migration

运行以下命令验证迁移成功：

Run the following command to verify successful migration:

```bash
python validate_schema.py
```

或者启动应用并检查日志：

Or start the application and check the logs:

```bash
python run.py
```

查找以下成功消息：
Look for these success messages:

```
✅ 数据库表初始化完成
✅ 数据库结构检查完成
```

## 回滚 / Rollback

如果需要回滚迁移（不推荐）：

If you need to rollback migrations (not recommended):

```sql
-- PostgreSQL
ALTER TABLE bot_groups DROP COLUMN IF EXISTS members_last_sync;
ALTER TABLE group_users DROP COLUMN IF EXISTS is_muted_permanent;
ALTER TABLE group_users DROP COLUMN IF EXISTS mute_reason;
ALTER TABLE invitation_activity DROP COLUMN IF EXISTS announce_in_group;

-- SQLite 不支持 DROP COLUMN，需要重建表
-- SQLite doesn't support DROP COLUMN, need to rebuild table
```

## 故障排除 / Troubleshooting

### 问题 1：权限不足 / Issue 1: Insufficient Permissions

**错误 / Error**:
```
permission denied for table bot_groups
```

**解决方案 / Solution**:
确保数据库用户有 ALTER TABLE 权限
Ensure database user has ALTER TABLE permission

```sql
GRANT ALTER ON ALL TABLES IN SCHEMA public TO your_user;
```

### 问题 2：表不存在 / Issue 2: Table Doesn't Exist

**错误 / Error**:
```
relation "bot_groups" does not exist
```

**解决方案 / Solution**:
首先运行 `db.create_all()` 创建所有表
First run `db.create_all()` to create all tables

```bash
python -c "from app import create_app, db; app = create_app(); app.app_context().push(); db.create_all()"
```

### 问题 3：列已存在 / Issue 3: Column Already Exists

**错误 / Error**:
```
column "members_last_sync" of relation "bot_groups" already exists
```

**解决方案 / Solution**:
这不是错误！使用 `IF NOT EXISTS` 的 SQL 命令是幂等的。如果看到这个错误，说明列已经存在，可以忽略。

This is not an error! SQL commands with `IF NOT EXISTS` are idempotent. If you see this error, it means the column already exists and can be ignored.

## 生产环境最佳实践 / Production Best Practices

1. **备份数据库 / Backup Database**
   ```bash
   pg_dump $DATABASE_URL > backup_$(date +%Y%m%d_%H%M%S).sql
   ```

2. **在维护窗口执行 / Execute During Maintenance Window**
   - 通知用户系统维护
   - Notify users of system maintenance

3. **先在测试环境验证 / Verify in Test Environment First**
   - 在生产环境前先在测试环境运行
   - Run in test environment before production

4. **监控应用日志 / Monitor Application Logs**
   - 检查迁移后是否有错误
   - Check for errors after migration

5. **准备回滚计划 / Prepare Rollback Plan**
   - 保留旧版本代码
   - Keep old version code ready

## 版本历史 / Version History

- **2025-01**: 添加 `members_last_sync` 字段以修复群成员同步问题
  - Added `members_last_sync` field to fix group member sync issue
- **2025-12**: 添加 `is_muted_permanent`, `mute_reason` 字段
  - Added `is_muted_permanent`, `mute_reason` fields
- **2025-11**: 添加 `announce_in_group` 字段
  - Added `announce_in_group` field
