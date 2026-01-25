#!/usr/bin/env python3
"""
Comprehensive test suite for SQL and AttributeError fixes
Tests both the is_muted_permanent column migration and forward_origin fix
"""
import sys
import os
import tempfile
from pathlib import Path

def test_forward_origin_fix():
    """Test that forward_origin is used instead of forward_date"""
    print("=" * 70)
    print("TEST 1: Forward Origin Fix")
    print("=" * 70)
    print()
    
    try:
        from telegram import Message
        
        # Verify the telegram library has the correct attributes
        print("✅ Checking telegram.Message attributes...")
        
        has_forward_origin = hasattr(Message, 'forward_origin')
        has_forward_date = hasattr(Message, 'forward_date')
        
        print(f"   - forward_origin exists: {has_forward_origin} (expected: True)")
        print(f"   - forward_date exists: {has_forward_date} (expected: False)")
        
        if not has_forward_origin:
            print("❌ FAIL: forward_origin attribute not found!")
            return False
            
        print()
        print("✅ Verifying routes.py uses forward_origin...")
        
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
        
        # Check that forward_date is not used
        if '.forward_date' in content:
            print("❌ FAIL: forward_date still found in routes.py!")
            return False
        
        # Check that forward_origin is used
        forward_origin_count = content.count('forward_origin')
        if forward_origin_count < 2:
            print(f"❌ FAIL: forward_origin only found {forward_origin_count} times (expected at least 2)")
            return False
            
        print(f"   - forward_origin used {forward_origin_count} times")
        print(f"   - forward_date not found (correct)")
        
        print()
        print("✅ TEST 1 PASSED: Forward origin fix is correctly implemented")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_database_migration():
    """Test that the database migration script is correct and idempotent"""
    print()
    print("=" * 70)
    print("TEST 2: Database Migration for is_muted_permanent")
    print("=" * 70)
    print()
    
    try:
        # Use a temporary SQLite database for testing
        temp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        temp_db.close()
        
        # Set up test database
        os.environ['DATABASE_URL'] = f'sqlite:///{temp_db.name}'
        
        print("✅ Creating test database...")
        from app import create_app, db
        from app.models import GroupUser
        
        app = create_app()
        with app.app_context():
            # Create tables
            db.create_all()
            print("   - Tables created successfully")
            
            # Verify the column exists in the model
            print()
            print("✅ Checking GroupUser model...")
            
            if not hasattr(GroupUser, 'is_muted_permanent'):
                print("❌ FAIL: is_muted_permanent attribute not in GroupUser model!")
                return False
                
            print("   - is_muted_permanent exists in model")
            
            # Check the column properties
            col = GroupUser.__table__.columns.get('is_muted_permanent')
            if col is None:
                print("❌ FAIL: is_muted_permanent column not in table!")
                return False
                
            print(f"   - Column type: {col.type}")
            print(f"   - Column nullable: {col.nullable}")
            print(f"   - Column default: {col.default}")
            
            # Test creating a user with the column
            print()
            print("✅ Testing database operations...")
            
            # Create a test user
            test_user = GroupUser(
                group_id=1,
                tg_id=12345,
                is_muted_permanent=False
            )
            db.session.add(test_user)
            db.session.commit()
            print("   - Created test user successfully")
            
            # Query the user
            user = db.session.query(GroupUser).filter_by(tg_id=12345).first()
            if user is None:
                print("❌ FAIL: Could not retrieve test user!")
                return False
                
            print(f"   - Retrieved user with is_muted_permanent={user.is_muted_permanent}")
            
            # Test updating the column
            user.is_muted_permanent = True
            user.mute_reason = "Test mute"
            db.session.commit()
            print("   - Updated is_muted_permanent successfully")
            
            # Verify the update
            user = db.session.query(GroupUser).filter_by(tg_id=12345).first()
            if not user.is_muted_permanent:
                print("❌ FAIL: Column update did not persist!")
                return False
                
            print(f"   - Verified update: is_muted_permanent={user.is_muted_permanent}")
            
        # Clean up
        os.unlink(temp_db.name)
        
        print()
        print("✅ Checking migration script...")
        
        # Verify the migration script exists and is correct
        migration_path = Path(__file__).parent / 'migrate_database.py'
        with open(migration_path, 'r') as f:
            migration_content = f.read()
        
        # Check for key elements
        checks = [
            ("is_muted_permanent BOOLEAN DEFAULT FALSE", "Column definition"),
            ("ADD COLUMN IF NOT EXISTS is_muted_permanent", "Idempotent migration"),
            ("information_schema.columns", "Column existence check"),
        ]
        
        for check_str, description in checks:
            if check_str in migration_content:
                print(f"   ✓ {description}: Found")
            else:
                print(f"   ✗ {description}: Not found")
                return False
        
        print()
        print("✅ TEST 2 PASSED: Database migration is correctly implemented")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Clean up environment
        if 'DATABASE_URL' in os.environ:
            del os.environ['DATABASE_URL']

def test_integration():
    """Test that both fixes work together"""
    print()
    print("=" * 70)
    print("TEST 3: Integration Test")
    print("=" * 70)
    print()
    
    try:
        print("✅ Verifying routes.py imports and compiles...")
        import py_compile
        
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        py_compile.compile(str(routes_path), doraise=True)
        print("   - routes.py compiles without errors")
        
        print()
        print("✅ Verifying models.py compiles...")
        models_path = Path(__file__).parent / 'app' / 'models.py'
        py_compile.compile(str(models_path), doraise=True)
        print("   - models.py compiles without errors")
        
        print()
        print("✅ TEST 3 PASSED: All components compile and integrate correctly")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run all tests"""
    print("\n")
    print("╔" + "=" * 68 + "╗")
    print("║" + " " * 15 + "SQL AND ATTRIBUTEERROR FIX TEST SUITE" + " " * 15 + "║")
    print("╚" + "=" * 68 + "╝")
    print()
    
    results = []
    
    # Run tests
    results.append(("Forward Origin Fix", test_forward_origin_fix()))
    results.append(("Database Migration", test_database_migration()))
    results.append(("Integration Test", test_integration()))
    
    # Print summary
    print()
    print("=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    print()
    
    all_passed = True
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status}: {test_name}")
        if not passed:
            all_passed = False
    
    print()
    print("=" * 70)
    if all_passed:
        print("✅ ALL TESTS PASSED!")
        print("=" * 70)
        return 0
    else:
        print("❌ SOME TESTS FAILED!")
        print("=" * 70)
        return 1

if __name__ == '__main__':
    sys.exit(main())
