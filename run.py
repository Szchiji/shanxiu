from app import create_app, db
import threading
import asyncio
import os
import sys
import time
from sqlalchemy import text

app = create_app()

def init_database(app):
    """Initialize database tables synchronously on startup."""
    with app.app_context():
        try:
            db.create_all()
            print("✅ 数据库表初始化完成", flush=True)
        except Exception as e:
            print(f"❌ 数据库初始化失败: {e}", flush=True)
            raise

def fix_database_schema(app):
    """Add missing columns to existing tables (synchronous, runs before server starts)."""
    with app.app_context():
        # List of ALTER TABLE statements to execute
        alter_statements = [
            "ALTER TABLE bot_groups ADD COLUMN last_query_msg_id INTEGER",
            "ALTER TABLE group_users ADD COLUMN expiration_date TIMESTAMP",
            "ALTER TABLE group_users ADD COLUMN is_banned BOOLEAN DEFAULT FALSE",
            "ALTER TABLE group_users ADD COLUMN last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
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
            # Bot clones table columns
            "ALTER TABLE bot_clones ADD COLUMN owner_user_id BIGINT",
            "ALTER TABLE bot_clones ADD COLUMN admin_user_ids TEXT DEFAULT '[]'",
        ]
        
        # Execute each statement in its own transaction to handle PostgreSQL properly
        for stmt in alter_statements:
            try:
                with db.engine.connect() as conn:
                    conn.execute(text(stmt))
                    conn.commit()
            except Exception:
                # Column/index likely already exists, which is fine
                pass
        
        print("✅ 数据库结构检查完成", flush=True)

def run_flask():
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)

def start_bot_process_forever(flask_app):
    """
    启动一个永不退出的事件循环，供 Webhook 使用
    """
    time.sleep(3)
    from app.modules.core.routes import run_bot
    
    print("🤖 启动机器人后台循环...", flush=True)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # 1. 初始化 (传入 Flask App 实例)
    loop.run_until_complete(run_bot(flask_app))
    
    # 2. ⚡ 核心：让 Loop 永远跑下去，活着等待 Flask 的投喂
    print("✅ 机器人循环已启动，正在监听 Webhook 任务...", flush=True)
    loop.run_forever()

if __name__ == '__main__':
    domain = os.getenv('RAILWAY_PUBLIC_DOMAIN')
    mode = "Webhook" if domain else "Polling"
    print(f"🚀 系统启动中 ({mode} 模式)...", flush=True)

    # 1. 初始化数据库表 (同步执行，确保表存在后再启动服务)
    init_database(app)
    
    # 2. 数据库修复 (同步执行，确保列存在后再启动 Web 服务)
    fix_database_schema(app)
    
    # 3. 启动 Web (Flask)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # 4. 启动机器人 (在独立线程中跑 loop_forever)
    # ⚡️ 修复点：将 app 传入机器人线程
    bot_thread = threading.Thread(target=start_bot_process_forever, args=(app,), daemon=True)
    bot_thread.start()
    
    # 5. 主线程死循环保活
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sys.exit(0)