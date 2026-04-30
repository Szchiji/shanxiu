"""
Database migration script.
Run this script to add missing columns to existing tables before starting the application.

Usage:
    python migrate_database.py
"""
from app import create_app, db
from sqlalchemy import text
import sys

app = create_app()


def run_migrations():
    """Add missing columns to existing tables."""
    alter_statements = [
        "ALTER TABLE bot_groups ADD COLUMN last_query_msg_id INTEGER",
        "ALTER TABLE group_users ADD COLUMN expiration_date TIMESTAMP",
        "ALTER TABLE group_users ADD COLUMN is_banned BOOLEAN DEFAULT FALSE",
        "ALTER TABLE group_users ADD COLUMN IF NOT EXISTS is_muted_permanent BOOLEAN DEFAULT FALSE",
        "ALTER TABLE group_users ADD COLUMN IF NOT EXISTS mute_reason VARCHAR(255)",
        "ALTER TABLE group_users ADD COLUMN last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE group_users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        # Auto replies table columns
        "ALTER TABLE auto_replies ADD COLUMN group_id INTEGER REFERENCES bot_groups(id)",
        "CREATE INDEX IF NOT EXISTS ix_auto_replies_group_id ON auto_replies(group_id)",
        "ALTER TABLE auto_replies ADD COLUMN trigger_keyword VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE auto_replies ADD COLUMN media_type VARCHAR(20) DEFAULT 'text'",
        "ALTER TABLE auto_replies ADD COLUMN media_url TEXT",
        "ALTER TABLE auto_replies ADD COLUMN content TEXT",
        "ALTER TABLE auto_replies ADD COLUMN links TEXT DEFAULT '[]'",
        "ALTER TABLE auto_replies ADD COLUMN delete_after INTEGER DEFAULT 0",
        "ALTER TABLE auto_replies ADD COLUMN remark TEXT",
        "ALTER TABLE auto_replies ADD COLUMN is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE auto_replies ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE auto_replies ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        # Scheduled messages table columns
        "ALTER TABLE scheduled_messages ADD COLUMN group_id INTEGER REFERENCES bot_groups(id)",
        "CREATE INDEX IF NOT EXISTS ix_scheduled_messages_group_id ON scheduled_messages(group_id)",
        "ALTER TABLE scheduled_messages ADD COLUMN media_type VARCHAR(20) DEFAULT 'text'",
        "ALTER TABLE scheduled_messages ADD COLUMN media_url TEXT",
        "ALTER TABLE scheduled_messages ADD COLUMN content TEXT",
        "ALTER TABLE scheduled_messages ADD COLUMN links TEXT DEFAULT '[]'",
        "ALTER TABLE scheduled_messages ADD COLUMN repeat_interval INTEGER DEFAULT 0",
        "ALTER TABLE scheduled_messages ADD COLUMN delete_previous BOOLEAN DEFAULT FALSE",
        "ALTER TABLE scheduled_messages ADD COLUMN last_message_id BIGINT",
        "ALTER TABLE scheduled_messages ADD COLUMN start_time TIMESTAMP",
        "ALTER TABLE scheduled_messages ADD COLUMN stop_time TIMESTAMP",
        "ALTER TABLE scheduled_messages ADD COLUMN remark TEXT",
        "ALTER TABLE scheduled_messages ADD COLUMN auto_pin BOOLEAN DEFAULT FALSE",
        "ALTER TABLE scheduled_messages ADD COLUMN IF NOT EXISTS message_thread_id INTEGER NULL",
        "ALTER TABLE scheduled_messages ADD COLUMN is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE scheduled_messages ADD COLUMN last_sent_at TIMESTAMP",
        "ALTER TABLE scheduled_messages ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE scheduled_messages ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        # Start messages table columns
        "ALTER TABLE start_messages ADD COLUMN group_id INTEGER REFERENCES bot_groups(id)",
        "CREATE INDEX IF NOT EXISTS ix_start_messages_group_id ON start_messages(group_id)",
        "ALTER TABLE start_messages ADD COLUMN message_type VARCHAR(20) DEFAULT 'user'",
        "ALTER TABLE start_messages ADD COLUMN media_type VARCHAR(20) DEFAULT 'text'",
        "ALTER TABLE start_messages ADD COLUMN media_url TEXT",
        "ALTER TABLE start_messages ADD COLUMN content TEXT",
        "ALTER TABLE start_messages ADD COLUMN links TEXT DEFAULT '[]'",
        "ALTER TABLE start_messages ADD COLUMN is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE start_messages ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE start_messages ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        # Group bottom button table columns
        "ALTER TABLE group_bottom_button ADD COLUMN trigger_keyword VARCHAR(255) NULL",
        "ALTER TABLE group_bottom_button ADD COLUMN row_position INTEGER DEFAULT 0",
        "ALTER TABLE group_bottom_button ADD COLUMN input_field_placeholder VARCHAR(255) NULL",
        # Bot clones table columns
        "ALTER TABLE bot_clones ADD COLUMN owner_user_id BIGINT",
        "ALTER TABLE bot_clones ADD COLUMN admin_user_ids TEXT DEFAULT '[]'",
        # User points table columns
        "ALTER TABLE user_points ADD COLUMN current_level_id INTEGER REFERENCES member_level(id)",
        # GroupMember: Set joined_at for existing records if NULL
        "UPDATE group_members SET joined_at = COALESCE(created_at, synced_at) WHERE joined_at IS NULL",
        # BotGroup: Add members_last_sync column
        "ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS members_last_sync TIMESTAMP NULL",
        # BotGroup: Add clone_id to track which bot (main or clone) owns each group
        "ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS clone_id INTEGER NULL",
        # AuthSession: Add clone_id to track which clone a login session belongs to
        "ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS clone_id INTEGER NULL",
        # AuthSession: Add user_name to display logged-in user identity
        "ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS user_name VARCHAR(255) NULL",
        # BotGroup: Drop old unique index on chat_id and add composite unique constraint
        "ALTER TABLE bot_groups DROP CONSTRAINT IF EXISTS bot_groups_chat_id_key",
        "ALTER TABLE bot_groups DROP INDEX IF EXISTS ix_bot_groups_chat_id",
        "CREATE UNIQUE INDEX IF NOT EXISTS _bot_group_chat_clone_uc ON bot_groups(chat_id, clone_id)",
        # PointsExchangeItem / PointsExchangeRecord tables (created via db.create_all, listed here for clarity)
        "CREATE TABLE IF NOT EXISTS points_exchange_items (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER REFERENCES bot_groups(id), item_name VARCHAR(255) NOT NULL, item_description TEXT, points_cost INTEGER NOT NULL DEFAULT 100, stock INTEGER, redemption_info TEXT, is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE INDEX IF NOT EXISTS ix_points_exchange_items_group_id ON points_exchange_items(group_id)",
        "CREATE TABLE IF NOT EXISTS points_exchange_records (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER REFERENCES bot_groups(id), user_id BIGINT, item_id INTEGER REFERENCES points_exchange_items(id), points_spent INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE INDEX IF NOT EXISTS ix_points_exchange_records_group_id ON points_exchange_records(group_id)",
        "CREATE INDEX IF NOT EXISTS ix_points_exchange_records_user_id ON points_exchange_records(user_id)",
        # PointsExchangeItem: Add announcement_msg_id to track per-item channel announcement
        "ALTER TABLE points_exchange_items ADD COLUMN IF NOT EXISTS announcement_msg_id BIGINT NULL",
        # system_config: Ensure id column exists (older deployments may have created the table without it)
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS id SERIAL",
    ]

    with app.app_context():
        # Ensure all tables exist first
        db.create_all()
        print("✅ 数据库表初始化完成", flush=True)

        success_count = 0
        skip_count = 0
        for stmt in alter_statements:
            try:
                with db.engine.connect() as conn:
                    conn.execute(text(stmt))
                    conn.commit()
                success_count += 1
            except Exception:
                # Column/index likely already exists, which is fine
                skip_count += 1

        print(
            f"✅ 数据库迁移完成 (执行: {success_count}, 跳过: {skip_count})",
            flush=True,
        )


if __name__ == '__main__':
    try:
        run_migrations()
    except Exception as e:
        print(f"❌ 数据库迁移失败: {e}", flush=True)
        sys.exit(1)
