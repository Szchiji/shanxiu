#!/usr/bin/env python3
"""
Database Migration Script
Adds new fields for issues #3, #4, and #6

Note: This script uses PostgreSQL-specific syntax (information_schema).
The application uses PostgreSQL as indicated by psycopg2-binary in requirements.txt.
"""
import os
import sys
from sqlalchemy import text

# Add the app directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from app import create_app, db

def migrate_database():
    """Run database migrations to add new fields"""
    app = create_app()
    
    with app.app_context():
        print("🔄 Starting database migration...")
        
        migrations = []
        
        # Issue #3: Entry Verification - Add flexible verification options
        migrations.append({
            'name': 'Add verification_type to group_entry_exit_settings',
            'sql': "ALTER TABLE group_entry_exit_settings ADD COLUMN IF NOT EXISTS verification_type VARCHAR(20) DEFAULT 'question'",
            'check': "SELECT column_name FROM information_schema.columns WHERE table_name='group_entry_exit_settings' AND column_name='verification_type'"
        })
        
        migrations.append({
            'name': 'Add verification_options to group_entry_exit_settings',
            'sql': "ALTER TABLE group_entry_exit_settings ADD COLUMN IF NOT EXISTS verification_options TEXT",
            'check': "SELECT column_name FROM information_schema.columns WHERE table_name='group_entry_exit_settings' AND column_name='verification_options'"
        })
        
        # Issue #4: Spam & Inactive User - Add permanent mute tracking
        migrations.append({
            'name': 'Add is_muted_permanent to group_users',
            'sql': "ALTER TABLE group_users ADD COLUMN IF NOT EXISTS is_muted_permanent BOOLEAN DEFAULT FALSE",
            'check': "SELECT column_name FROM information_schema.columns WHERE table_name='group_users' AND column_name='is_muted_permanent'"
        })
        
        migrations.append({
            'name': 'Add mute_reason to group_users',
            'sql': "ALTER TABLE group_users ADD COLUMN IF NOT EXISTS mute_reason VARCHAR(255)",
            'check': "SELECT column_name FROM information_schema.columns WHERE table_name='group_users' AND column_name='mute_reason'"
        })
        
        # Issue #6: Invitation Tracking - Add group announcements
        migrations.append({
            'name': 'Add announce_in_group to invitation_activity',
            'sql': "ALTER TABLE invitation_activity ADD COLUMN IF NOT EXISTS announce_in_group BOOLEAN DEFAULT FALSE",
            'check': "SELECT column_name FROM information_schema.columns WHERE table_name='invitation_activity' AND column_name='announce_in_group'"
        })
        
        # Run migrations
        for migration in migrations:
            try:
                # Check if column already exists
                result = db.session.execute(text(migration['check'])).fetchone()
                if result:
                    print(f"⏭️  Skipping: {migration['name']} (already exists)")
                else:
                    # Run migration
                    print(f"🔄 Running: {migration['name']}")
                    db.session.execute(text(migration['sql']))
                    db.session.commit()
                    print(f"✅ Completed: {migration['name']}")
            except Exception as e:
                print(f"❌ Error in {migration['name']}: {e}")
                db.session.rollback()
                # Continue with other migrations
        
        print("\n✅ Database migration completed!")
        print("\nSummary:")
        print("- Added verification_type and verification_options to group_entry_exit_settings")
        print("- Added is_muted_permanent and mute_reason to group_users")
        print("- Added announce_in_group to invitation_activity")

if __name__ == '__main__':
    migrate_database()
