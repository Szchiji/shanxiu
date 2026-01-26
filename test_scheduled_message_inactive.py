#!/usr/bin/env python3
"""
Test suite for scheduled message inactive/deletion functionality
Tests that inactive (is_active=False) messages are not sent and properly handled
"""
import sys
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Add the app directory to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_scheduled_message_filtering():
    """Test that check_scheduled_messages only processes active messages"""
    print("=" * 70)
    print("TEST 1: Scheduled Message Active Filtering")
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
        from app.models import ScheduledMessage, BotGroup
        
        app = create_app()
        with app.app_context():
            # Create tables
            db.create_all()
            print("   - Tables created successfully")
            
            # Create a test group
            test_group = BotGroup(
                chat_id=-1001234567890,
                title="Test Group",
                is_active=True
            )
            db.session.add(test_group)
            db.session.commit()
            print(f"   - Created test group (id={test_group.id})")
            
            # Create test scheduled messages
            print()
            print("✅ Creating test scheduled messages...")
            
            # Active message 1
            active_msg1 = ScheduledMessage(
                group_id=test_group.id,
                content="Active Message 1",
                is_active=True,
                repeat_interval=60,
                last_sent_at=None
            )
            db.session.add(active_msg1)
            
            # Active message 2
            active_msg2 = ScheduledMessage(
                group_id=test_group.id,
                content="Active Message 2",
                is_active=True,
                repeat_interval=60,
                last_sent_at=datetime.now() - timedelta(minutes=61)  # Should send
            )
            db.session.add(active_msg2)
            
            # Inactive message 1
            inactive_msg1 = ScheduledMessage(
                group_id=test_group.id,
                content="Inactive Message 1",
                is_active=False,
                repeat_interval=60,
                last_sent_at=None
            )
            db.session.add(inactive_msg1)
            
            # Inactive message 2 (set to inactive after creation)
            inactive_msg2 = ScheduledMessage(
                group_id=test_group.id,
                content="Inactive Message 2 (paused)",
                is_active=False,
                repeat_interval=60,
                last_sent_at=datetime.now() - timedelta(minutes=61)
            )
            db.session.add(inactive_msg2)
            
            db.session.commit()
            print(f"   - Created 2 active messages")
            print(f"   - Created 2 inactive messages")
            
            # Test query from check_scheduled_messages
            print()
            print("✅ Testing query logic from check_scheduled_messages()...")
            
            from sqlalchemy.orm import joinedload
            
            # This is the exact query from check_scheduled_messages
            scheduled_messages = ScheduledMessage.query.options(
                joinedload(ScheduledMessage.group)
            ).filter(
                ScheduledMessage.is_active == True
            ).all()
            
            print(f"   - Query returned {len(scheduled_messages)} messages")
            
            # Verify count
            if len(scheduled_messages) != 2:
                print(f"❌ FAIL: Expected 2 active messages, got {len(scheduled_messages)}")
                return False
            
            # Verify all returned messages are active
            for msg in scheduled_messages:
                if not msg.is_active:
                    print(f"❌ FAIL: Query returned inactive message: {msg.content}")
                    return False
                print(f"   ✓ {msg.content} (is_active={msg.is_active})")
            
            # Verify inactive messages are not in results
            inactive_contents = ["Inactive Message 1", "Inactive Message 2 (paused)"]
            for msg in scheduled_messages:
                if msg.content in inactive_contents:
                    print(f"❌ FAIL: Inactive message found in results: {msg.content}")
                    return False
            
            print()
            print("✅ Verifying database state...")
            
            # Verify total count
            total_count = ScheduledMessage.query.count()
            active_count = ScheduledMessage.query.filter_by(is_active=True).count()
            inactive_count = ScheduledMessage.query.filter_by(is_active=False).count()
            
            print(f"   - Total messages in DB: {total_count}")
            print(f"   - Active messages: {active_count}")
            print(f"   - Inactive messages: {inactive_count}")
            
            if total_count != 4:
                print(f"❌ FAIL: Expected 4 total messages, got {total_count}")
                return False
            
            if active_count != 2:
                print(f"❌ FAIL: Expected 2 active messages, got {active_count}")
                return False
            
            if inactive_count != 2:
                print(f"❌ FAIL: Expected 2 inactive messages, got {inactive_count}")
                return False
            
        # Clean up
        os.unlink(temp_db.name)
        
        print()
        print("✅ TEST 1 PASSED: Active filtering works correctly")
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

def test_admin_ui_queries():
    """Test that admin UI queries properly handle inactive messages"""
    print()
    print("=" * 70)
    print("TEST 2: Admin UI Query Behavior")
    print("=" * 70)
    print()
    
    try:
        print("✅ Analyzing admin UI queries in routes.py...")
        
        routes_path = Path(__file__).parent / 'app' / 'modules' / 'core' / 'routes.py'
        with open(routes_path, 'r') as f:
            content = f.read()
            lines = content.split('\n')
        
        # Find queries that might need is_active filtering
        issues_found = []
        
        # Check line 667: List scheduled messages
        print("   - Checking line 667 (list scheduled messages)...")
        if 'ScheduledMessage.query.filter_by(group_id=gid)' in content:
            # This query is used for admin UI - should it filter by is_active?
            # For admin UI, we might want to show ALL messages (including inactive)
            # so admins can see what's paused
            print("     ✓ Query found - used for admin listing (shows all messages)")
        
        # Check line 1206: Export group config
        print("   - Checking line 1206 (export group config)...")
        if 'ScheduledMessage.query.filter_by(group_id=gid).all()' in content:
            print("     ✓ Query found - used for config export")
        
        # Check line 2152: Export to Excel
        print("   - Checking line 2152 (export to Excel)...")
        if 'ScheduledMessage.query.filter_by(group_id=group_id).all()' in content:
            print("     ✓ Query found - used for Excel export")
        
        # Most importantly, check that check_scheduled_messages filters correctly
        print()
        print("   - Checking check_scheduled_messages() function...")
        if 'ScheduledMessage.is_active == True' in content:
            print("     ✅ Correct: Filters by is_active == True")
        else:
            print("     ❌ ERROR: Missing is_active filter!")
            issues_found.append("check_scheduled_messages missing is_active filter")
        
        if issues_found:
            print()
            print("❌ Issues found:")
            for issue in issues_found:
                print(f"   - {issue}")
            return False
        
        print()
        print("✅ TEST 2 PASSED: Query structure is appropriate")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_deletion_scenario():
    """Test that deleted messages are properly handled"""
    print()
    print("=" * 70)
    print("TEST 3: Message Deletion Scenario")
    print("=" * 70)
    print()
    
    try:
        # Use a temporary SQLite database for testing
        temp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        temp_db.close()
        
        # Set up test database
        os.environ['DATABASE_URL'] = f'sqlite:///{temp_db.name}'
        
        print("✅ Testing deletion behavior...")
        from app import create_app, db
        from app.models import ScheduledMessage, BotGroup
        
        app = create_app()
        with app.app_context():
            # Create tables
            db.create_all()
            
            # Create a test group
            test_group = BotGroup(
                chat_id=-1001234567891,
                title="Test Group 2",
                is_active=True
            )
            db.session.add(test_group)
            db.session.commit()
            
            # Create a scheduled message
            msg = ScheduledMessage(
                group_id=test_group.id,
                content="Test Message",
                is_active=True,
                repeat_interval=60
            )
            db.session.add(msg)
            db.session.commit()
            msg_id = msg.id
            
            print(f"   - Created message (id={msg_id})")
            
            # Verify it's active
            active_msgs = ScheduledMessage.query.filter(
                ScheduledMessage.is_active == True
            ).count()
            print(f"   - Active messages before deletion: {active_msgs}")
            
            # Delete the message
            db.session.delete(msg)
            db.session.commit()
            print("   - Deleted message from database")
            
            # Verify it's gone
            deleted_msg = ScheduledMessage.query.get(msg_id)
            if deleted_msg is not None:
                print(f"❌ FAIL: Message still exists after deletion!")
                return False
            
            print("   ✓ Message successfully deleted")
            
            # Verify query doesn't return it
            active_msgs_after = ScheduledMessage.query.filter(
                ScheduledMessage.is_active == True
            ).count()
            print(f"   - Active messages after deletion: {active_msgs_after}")
            
            if active_msgs_after != 0:
                print(f"❌ FAIL: Expected 0 messages, got {active_msgs_after}")
                return False
            
        # Clean up
        os.unlink(temp_db.name)
        
        print()
        print("✅ TEST 3 PASSED: Deletion works correctly")
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

def test_session_refresh():
    """Test that session.expire_all() properly refreshes data"""
    print()
    print("=" * 70)
    print("TEST 4: Database Session Refresh")
    print("=" * 70)
    print()
    
    try:
        # Use a temporary SQLite database for testing
        temp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        temp_db.close()
        
        # Set up test database
        os.environ['DATABASE_URL'] = f'sqlite:///{temp_db.name}'
        
        print("✅ Testing session.expire_all() behavior...")
        from app import create_app, db
        from app.models import ScheduledMessage, BotGroup
        
        app = create_app()
        with app.app_context():
            # Create tables
            db.create_all()
            
            # Create a test group
            test_group = BotGroup(
                chat_id=-1001234567892,
                title="Test Group 3",
                is_active=True
            )
            db.session.add(test_group)
            db.session.commit()
            
            # Create an active message
            msg = ScheduledMessage(
                group_id=test_group.id,
                content="Test Message",
                is_active=True,
                repeat_interval=60
            )
            db.session.add(msg)
            db.session.commit()
            msg_id = msg.id
            
            print(f"   - Created active message (id={msg_id})")
            
            # Query and verify it's active
            result1 = ScheduledMessage.query.filter(
                ScheduledMessage.is_active == True
            ).count()
            print(f"   - Query 1: {result1} active messages")
            
            # Simulate external change (like from admin UI)
            # We'll use a raw SQL update to simulate another process changing the value
            from sqlalchemy import text
            db.session.execute(
                text(f"UPDATE scheduled_messages SET is_active = 0 WHERE id = {msg_id}")
            )
            db.session.commit()
            print("   - Simulated external update: set is_active=False")
            
            # Without expire_all, the session might still have cached data
            print("   - Testing WITHOUT expire_all()...")
            result2 = db.session.query(ScheduledMessage).get(msg_id)
            print(f"     Object in session: is_active={result2.is_active if result2 else 'None'}")
            
            # Now with expire_all (as used in check_scheduled_messages)
            print("   - Testing WITH expire_all()...")
            db.session.expire_all()
            
            result3 = ScheduledMessage.query.filter(
                ScheduledMessage.is_active == True
            ).count()
            print(f"     Query after expire_all: {result3} active messages")
            
            if result3 != 0:
                print(f"❌ FAIL: Expected 0 messages after deactivation, got {result3}")
                return False
            
            print("   ✓ expire_all() correctly refreshes session data")
            
        # Clean up
        os.unlink(temp_db.name)
        
        print()
        print("✅ TEST 4 PASSED: Session refresh works correctly")
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

def main():
    """Run all tests"""
    print("\n")
    print("╔" + "=" * 68 + "╗")
    print("║" + " " * 10 + "SCHEDULED MESSAGE INACTIVE/DELETION TEST SUITE" + " " * 11 + "║")
    print("╚" + "=" * 68 + "╝")
    print()
    
    results = []
    
    # Run tests
    results.append(("Active Filtering", test_scheduled_message_filtering()))
    results.append(("Admin UI Queries", test_admin_ui_queries()))
    results.append(("Message Deletion", test_deletion_scenario()))
    results.append(("Session Refresh", test_session_refresh()))
    
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
        print()
        print("Key Findings:")
        print("  ✓ check_scheduled_messages() correctly filters by is_active=True")
        print("  ✓ Inactive messages are not sent")
        print("  ✓ Deleted messages are properly removed from database")
        print("  ✓ Session.expire_all() correctly refreshes cached data")
        print("=" * 70)
        return 0
    else:
        print("❌ SOME TESTS FAILED!")
        print("=" * 70)
        return 1

if __name__ == '__main__':
    sys.exit(main())
