#!/usr/bin/env python3
"""
Schema Validation and Cleanup Utility
Validates that all required columns exist in the database and provides cleanup options.
"""
import os
import sys
from sqlalchemy import text

# Add the app directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from app import create_app, db

def validate_schema():
    """Validate that all required columns exist in the database"""
    app = create_app()
    
    with app.app_context():
        print("🔍 Validating database schema...")
        print()
        
        # Define expected columns
        expected_columns = [
            {
                'table': 'group_users',
                'column': 'is_muted_permanent',
                'type': 'boolean',
                'description': 'Track if user needs admin to unlock'
            },
            {
                'table': 'group_users',
                'column': 'mute_reason',
                'type': 'character varying',
                'description': 'Reason for permanent mute'
            },
            {
                'table': 'group_entry_exit_settings',
                'column': 'verification_type',
                'type': 'character varying',
                'description': 'Type of verification (question, button, etc.)'
            },
            {
                'table': 'group_entry_exit_settings',
                'column': 'verification_options',
                'type': 'text',
                'description': 'JSON options for verification'
            },
            {
                'table': 'invitation_activity',
                'column': 'announce_in_group',
                'type': 'boolean',
                'description': 'Whether to announce invitation in group'
            },
        ]
        
        all_valid = True
        missing_columns = []
        
        for col_spec in expected_columns:
            try:
                # Check if column exists
                query = text("""
                    SELECT column_name, data_type, column_default
                    FROM information_schema.columns 
                    WHERE table_name=:table_name 
                      AND column_name=:column_name
                      AND table_schema = current_schema()
                """)
                result = db.session.execute(
                    query, 
                    {'table_name': col_spec['table'], 'column_name': col_spec['column']}
                ).fetchone()
                
                if result:
                    print(f"✅ {col_spec['table']}.{col_spec['column']}")
                    print(f"   Type: {result[1]}")
                    if result[2]:
                        print(f"   Default: {result[2]}")
                    print(f"   Description: {col_spec['description']}")
                else:
                    print(f"❌ {col_spec['table']}.{col_spec['column']} - MISSING")
                    print(f"   Description: {col_spec['description']}")
                    all_valid = False
                    missing_columns.append(col_spec)
                print()
                    
            except Exception as e:
                print(f"⚠️  Error checking {col_spec['table']}.{col_spec['column']}: {e}")
                print()
        
        if all_valid:
            print("✅ All required columns are present in the database!")
            return True
        else:
            print("❌ Some columns are missing from the database.")
            print("\n📝 To fix this, run:")
            print("   python migrate_database.py")
            print("\nMissing columns:")
            for col in missing_columns:
                print(f"  - {col['table']}.{col['column']}")
            return False

def check_data_integrity():
    """Check for any data integrity issues"""
    app = create_app()
    
    with app.app_context():
        print("\n🔍 Checking data integrity...")
        print()
        
        try:
            # First check if the column exists before querying it
            query = text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='group_users' 
                  AND column_name='is_muted_permanent'
                  AND table_schema = current_schema()
            """)
            result = db.session.execute(query).fetchone()
            
            if not result:
                print("⚠️  Column is_muted_permanent does not exist yet.")
                print("   Run migrate_database.py first.")
                return
            
            # Check for NULL values in is_muted_permanent (should all have defaults)
            query = text("""
                SELECT COUNT(*) 
                FROM group_users 
                WHERE is_muted_permanent IS NULL
            """)
            result = db.session.execute(query).fetchone()
            null_count = result[0] if result else 0
            
            if null_count > 0:
                print(f"⚠️  Found {null_count} group_users with NULL is_muted_permanent")
                print("   These should be set to FALSE. Run cleanup to fix.")
            else:
                print("✅ No NULL values in is_muted_permanent")
            
            print()
            
            # Count total rows
            query = text("SELECT COUNT(*) FROM group_users")
            result = db.session.execute(query).fetchone()
            total = result[0] if result else 0
            print(f"📊 Total group_users: {total}")
            
            # Count permanently muted users
            query = text("SELECT COUNT(*) FROM group_users WHERE is_muted_permanent = TRUE")
            result = db.session.execute(query).fetchone()
            muted = result[0] if result else 0
            print(f"📊 Permanently muted users: {muted}")
            
        except Exception as e:
            print(f"⚠️  Could not check data integrity: {e}")
            print("   This is expected if the column doesn't exist yet.")

def cleanup_data():
    """Cleanup any data integrity issues"""
    app = create_app()
    
    with app.app_context():
        print("\n🧹 Cleaning up data integrity issues...")
        print()
        
        try:
            # First check if the column exists before trying to update it
            query = text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='group_users' 
                  AND column_name='is_muted_permanent'
                  AND table_schema = current_schema()
            """)
            result = db.session.execute(query).fetchone()
            
            if not result:
                print("⚠️  Column is_muted_permanent does not exist yet.")
                print("   Run migrate_database.py first before cleanup.")
                return
            
            # Fix NULL values in is_muted_permanent
            query = text("""
                UPDATE group_users 
                SET is_muted_permanent = FALSE 
                WHERE is_muted_permanent IS NULL
            """)
            result = db.session.execute(query)
            db.session.commit()
            
            if result.rowcount > 0:
                print(f"✅ Fixed {result.rowcount} NULL values in is_muted_permanent")
            else:
                print("✅ No NULL values to fix")
                
        except Exception as e:
            print(f"❌ Error during cleanup: {e}")
            db.session.rollback()

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Validate and cleanup database schema')
    parser.add_argument('--cleanup', action='store_true', help='Run data cleanup')
    args = parser.parse_args()
    
    # Always validate schema
    schema_valid = validate_schema()
    
    if schema_valid:
        # Check data integrity
        check_data_integrity()
        
        # Run cleanup if requested
        if args.cleanup:
            cleanup_data()
            print("\n🔍 Re-checking data integrity after cleanup...")
            check_data_integrity()
    
    print("\n✅ Validation complete!")
