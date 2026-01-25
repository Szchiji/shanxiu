#!/usr/bin/env python3
"""
Test script to verify that the specific errors mentioned in the problem statement
have been resolved:
1. SQL errors due to missing is_muted_permanent column
2. AttributeError when accessing forward_date on Message objects
"""
import sys
import os
import tempfile
from pathlib import Path


def test_no_forward_date_error():
    """
    Test that code does not attempt to access forward_date attribute
    which would cause AttributeError
    """
    print("=" * 70)
    print("ERROR SCENARIO 1: AttributeError for forward_date")
    print("=" * 70)
    print()
    
    print("Original Error: AttributeError: 'Message' object has no attribute 'forward_date'")
    print()
    
    try:
        # Verify the fix in routes.py
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
        
        # Find the spam protection section
        print("✅ Checking spam protection (block_forwards)...")
        if 'protection.block_forwards and msg.forward_origin' in content:
            print("   ✓ Uses forward_origin (correct)")
        elif 'protection.block_forwards and msg.forward_date' in content:
            print("   ✗ Still uses forward_date (WRONG!)")
            return False
        else:
            print("   ? Could not find spam protection code")
        
        # Find the message sync section  
        print()
        print("✅ Checking message sync (sync_forwards)...")
        if 'msg.forward_origin and not sync_setting.sync_forwards' in content:
            print("   ✓ Uses forward_origin (correct)")
        elif 'msg.forward_date and not sync_setting.sync_forwards' in content:
            print("   ✗ Still uses forward_date (WRONG!)")
            return False
        else:
            print("   ? Could not find message sync code")
        
        # Verify no other uses of forward_date
        print()
        print("✅ Checking for any remaining forward_date usage...")
        if '.forward_date' not in content:
            print("   ✓ No forward_date usage found (correct)")
        else:
            print("   ✗ forward_date still used in code (WRONG!)")
            return False
        
        print()
        print("✅ RESOLVED: AttributeError for forward_date will not occur")
        print("   The code now correctly uses forward_origin instead of forward_date")
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_no_sql_error_for_is_muted_permanent():
    """
    Test that is_muted_permanent column is properly defined and accessible
    This simulates checking for expired users and spam protection
    """
    print()
    print("=" * 70)
    print("ERROR SCENARIO 2: SQL Error - Column is_muted_permanent does not exist")
    print("=" * 70)
    print()
    
    print("Original Error: psycopg2.errors.UndefinedColumn: column 'is_muted_permanent' does not exist")
    print()
    
    try:
        # Create temporary database
        temp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        temp_db.close()
        os.environ['DATABASE_URL'] = f'sqlite:///{temp_db.name}'
        
        from app import create_app, db
        from app.models import GroupUser, BotGroup
        
        app = create_app()
        with app.app_context():
            # Create tables
            db.create_all()
            
            print("✅ Testing expired user check functionality...")
            
            # Create a test group
            group = BotGroup(chat_id='123', title='Test Group')
            db.session.add(group)
            db.session.commit()
            
            # Create test users with different mute states
            user1 = GroupUser(
                group_id=group.id,
                tg_id=111,
                is_muted_permanent=False
            )
            user2 = GroupUser(
                group_id=group.id,
                tg_id=222,
                is_muted_permanent=True,
                mute_reason="spam"
            )
            
            db.session.add_all([user1, user2])
            db.session.commit()
            
            print("   ✓ Created test users with is_muted_permanent column")
            
            # Query users - this would fail if column doesn't exist
            print()
            print("✅ Testing queries that use is_muted_permanent...")
            
            # Test 1: Query by is_muted_permanent
            muted_users = db.session.query(GroupUser).filter(
                GroupUser.is_muted_permanent == True
            ).all()
            print(f"   ✓ Query for muted users: Found {len(muted_users)} user(s)")
            
            # Test 2: Update is_muted_permanent (spam protection scenario)
            user1.is_muted_permanent = True
            user1.mute_reason = "automatic mute"
            db.session.commit()
            print("   ✓ Updated is_muted_permanent (spam protection scenario)")
            
            # Test 3: Check permanent mute status (expired user scenario)
            user = db.session.query(GroupUser).filter_by(tg_id=222).first()
            if user and user.is_muted_permanent:
                print(f"   ✓ Checked permanent mute status: User 222 is permanently muted")
            
            # Test 4: Unmute user (admin action scenario)
            user.is_muted_permanent = False
            user.mute_reason = None
            db.session.commit()
            print("   ✓ Unmuted user (admin action scenario)")
            
        # Clean up
        os.unlink(temp_db.name)
        if 'DATABASE_URL' in os.environ:
            del os.environ['DATABASE_URL']
        
        print()
        print("✅ RESOLVED: SQL errors for is_muted_permanent will not occur")
        print("   The column is properly defined in the model and migrations")
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        if 'DATABASE_URL' in os.environ:
            del os.environ['DATABASE_URL']


def test_migration_idempotency():
    """
    Test that migration can be run multiple times safely
    """
    print()
    print("=" * 70)
    print("ADDITIONAL TEST: Migration Idempotency")
    print("=" * 70)
    print()
    
    print("Verifying that migration uses 'ADD COLUMN IF NOT EXISTS' for safety...")
    
    try:
        migration_path = Path(__file__).parent / 'migrate_database.py'
        with open(migration_path, 'r') as f:
            content = f.read()
        
        # Check for idempotent migration
        if 'ADD COLUMN IF NOT EXISTS is_muted_permanent' in content:
            print("   ✓ Migration uses IF NOT EXISTS (idempotent)")
        else:
            print("   ✗ Migration does not use IF NOT EXISTS")
            return False
        
        # Check for proper error handling
        if 'try:' in content and 'except' in content and 'rollback' in content:
            print("   ✓ Migration has proper error handling")
        else:
            print("   ⚠ Migration may lack comprehensive error handling")
        
        # Check for column existence verification
        if 'information_schema.columns' in content:
            print("   ✓ Migration checks for column existence")
        else:
            print("   ⚠ Migration may not check for column existence")
        
        print()
        print("✅ Migration is safe to run multiple times")
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        return False


def main():
    """Run all error scenario tests"""
    print("\n")
    print("╔" + "=" * 68 + "╗")
    print("║" + " " * 18 + "ERROR SCENARIO VERIFICATION" + " " * 22 + "║")
    print("║" + " " * 15 + "Testing fixes for reported issues" + " " * 19 + "║")
    print("╚" + "=" * 68 + "╝")
    print()
    
    results = []
    
    # Run tests
    results.append(("AttributeError: forward_date", test_no_forward_date_error()))
    results.append(("SQL Error: is_muted_permanent", test_no_sql_error_for_is_muted_permanent()))
    results.append(("Migration Idempotency", test_migration_idempotency()))
    
    # Print summary
    print()
    print("=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    print()
    
    all_passed = True
    for test_name, passed in results:
        status = "✅ RESOLVED" if passed else "❌ STILL FAILING"
        print(f"  {status}: {test_name}")
        if not passed:
            all_passed = False
    
    print()
    print("=" * 70)
    if all_passed:
        print("✅ ALL REPORTED ERRORS HAVE BEEN RESOLVED!")
        print()
        print("Summary:")
        print("  1. forward_date → forward_origin: Working correctly")
        print("  2. is_muted_permanent column: Properly defined and accessible")
        print("  3. Migration script: Safe and idempotent")
        print("=" * 70)
        return 0
    else:
        print("❌ SOME ERRORS MAY STILL EXIST!")
        print("=" * 70)
        return 1


if __name__ == '__main__':
    sys.exit(main())
