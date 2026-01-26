#!/usr/bin/env python3
"""
Database migration script to add members_last_sync field to bot_groups table
"""
import sys
import os
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent))

from app import create_app, db
from sqlalchemy import text

def migrate():
    """Add members_last_sync column to bot_groups table"""
    print("=" * 70)
    print("Adding members_last_sync field to bot_groups table")
    print("=" * 70)
    
    app = create_app()
    
    with app.app_context():
        try:
            # Check if column already exists
            result = db.session.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='bot_groups' AND column_name='members_last_sync'"
            ))
            if result.fetchone():
                print("✅ Column 'members_last_sync' already exists, skipping migration")
                return True
            
            # Add the column
            print("Adding 'members_last_sync' column to bot_groups table...")
            db.session.execute(text(
                "ALTER TABLE bot_groups ADD COLUMN members_last_sync TIMESTAMP NULL"
            ))
            db.session.commit()
            print("✅ Successfully added 'members_last_sync' column")
            return True
            
        except Exception as e:
            db.session.rollback()
            print(f"❌ Migration failed: {e}")
            import traceback
            traceback.print_exc()
            return False

if __name__ == '__main__':
    success = migrate()
    sys.exit(0 if success else 1)
