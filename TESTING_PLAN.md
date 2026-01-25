# Testing Plan

This document outlines the testing approach for the 8 implemented fixes.

## Test Environment Setup

### Prerequisites
1. PostgreSQL database running
2. Telegram bot token configured
3. Admin user ID set in environment
4. Flask application running

### Database Migration
```bash
python migrate_database.py
```

## Manual Test Cases

### Issue 1: Forced Subscription Auto-Unmute

**Test Case 1.1: Verify Auto-Unmute on Subscription**
1. Setup:
   - Enable forced channel subscription with "mute" action
   - Add a test user to the group
2. Steps:
   - User joins group without subscribing → gets muted
   - User subscribes to required channel
   - Wait for next subscription check cycle (default: every hour)
3. Expected Result:
   - User is automatically unmuted
   - Log shows: "✅ [频道订阅检测] Successfully unmuted subscribed user"

**Test Case 1.2: No False Positives**
1. Setup:
   - User is muted for another reason (not subscription)
2. Steps:
   - User subscribes to channel
   - Wait for subscription check
3. Expected Result:
   - User remains muted (only subscription-related mutes are cleared)

### Issue 2: Scheduled Messages Pause

**Test Case 2.1: Verify Paused Messages Don't Send**
1. Setup:
   - Create a scheduled message
   - Set `is_active = False`
2. Steps:
   - Wait for scheduled message time
3. Expected Result:
   - Message is NOT sent
   - Only active messages (`is_active = True`) are sent

### Issue 3: Entry Verification Options

**Test Case 3.1: Verify New Fields Added**
1. Steps:
   - Run migration: `python migrate_database.py`
   - Check database schema
2. Expected Result:
   - `verification_type` field exists in `group_entry_exit_settings`
   - `verification_options` field exists in `group_entry_exit_settings`
   - Default value is 'question'

**Test Case 3.2: Backward Compatibility**
1. Steps:
   - Existing entry verification settings still work
   - Old Q&A verification continues to function
2. Expected Result:
   - No errors with existing configurations

### Issue 4: Permanent Mute Option

**Test Case 4.1: Apply Permanent Mute**
1. Setup:
   - Configure inactive user settings
   - Set `action_type = 'mute_permanent'`
2. Steps:
   - Wait for inactive user detection
   - Check database
3. Expected Result:
   - User is muted in Telegram
   - `is_muted_permanent = True` in database
   - `mute_reason = '不活跃用户'` in database

**Test Case 4.2: Admin Unlock**
1. Setup:
   - User is permanently muted
2. Steps:
   - Admin replies to user's message
   - Admin sends `/unmute`
3. Expected Result:
   - User is unmuted in Telegram
   - `is_muted_permanent = False` in database
   - `mute_reason = NULL` in database
   - Success message shown to admin

**Test Case 4.3: Verify Constants**
1. Steps:
   - Check code for hardcoded strings
2. Expected Result:
   - `MUTE_REASON_INACTIVE` constant used
   - `MUTE_REASON_SPAM` constant available

### Issue 5: Private /start Command

**Test Case 5.1: Admin Start Message**
1. Steps:
   - Admin sends `/start` in private chat
2. Expected Result:
   - Verification URL sent
   - Link button displayed

**Test Case 5.2: User Start Message**
1. Setup:
   - Configure custom start message in database
   - Set `start_msg_open = True`
2. Steps:
   - Regular user sends `/start` in private chat
3. Expected Result:
   - Custom message displayed (from StartMessage table)
   - Or fallback to config `msg_private_start`

### Issue 6: Invitation Announcements

**Test Case 6.1: Announcement Enabled**
1. Setup:
   - Enable invitation activity
   - Set `announce_in_group = True`
2. Steps:
   - User A invites User B to group
   - User B joins
3. Expected Result:
   - Announcement sent to group
   - Shows inviter mention, invitee mention, points awarded
   - Format: "🎉 邀请成功！"

**Test Case 6.2: Announcement Disabled**
1. Setup:
   - Enable invitation activity
   - Set `announce_in_group = False`
2. Steps:
   - User invites another user
3. Expected Result:
   - Points awarded silently
   - No announcement in group

**Test Case 6.3: Backward Compatibility**
1. Setup:
   - Existing invitation activity (before migration)
2. Steps:
   - User invites another user
3. Expected Result:
   - No errors
   - Default behavior (no announcement)

### Issue 7: Lottery Results Announcement

**Test Case 7.1: Automatic Lottery Draw**
1. Setup:
   - Create lottery with end_time in past
2. Steps:
   - Wait for background task (runs every minute)
3. Expected Result:
   - Winners selected
   - Announcement sent to group
   - Format: "🎉 抽奖结束！"
   - Shows lottery name, winners, prize

**Test Case 7.2: Manual Lottery Draw**
1. Setup:
   - Create active lottery
2. Steps:
   - Admin sends `/lottery_draw <id>`
3. Expected Result:
   - Winners selected
   - Announcement sent to group
   - Same format as automatic draw

### Issue 8: Clone Bot Management

**Test Case 8.1: List Clone Bots**
1. Setup:
   - Add some clone bots to database
2. Steps:
   - Admin sends `/clones` in private chat
3. Expected Result:
   - List of all clone bots displayed
   - Shows: ID, name, status, owner, expiration, description
   - Link to web admin panel

**Test Case 8.2: Non-Admin Access**
1. Setup:
   - Regular user (not admin)
2. Steps:
   - User sends `/clones` in private chat
3. Expected Result:
   - Error message: "❌ 只有管理员才能使用此命令"

**Test Case 8.3: Group Chat Block**
1. Setup:
   - Admin user in group
2. Steps:
   - Send `/clones` in group chat
3. Expected Result:
   - Error message: "❌ 此命令只能在私聊中使用"

## Integration Tests

### Test Case I1: End-to-End Subscription Flow
1. User joins → gets muted (no subscription)
2. User subscribes to channel
3. Background task runs → user unmuted
4. User can send messages

### Test Case I2: End-to-End Invitation Flow
1. User A invites User B
2. User B joins group
3. Points awarded to User A
4. Announcement sent (if enabled)
5. Record stored in database

### Test Case I3: End-to-End Lottery Flow
1. Lottery created with end_time
2. Users participate (send messages)
3. End_time reached
4. Background task draws winners
5. Announcement sent to group
6. Lottery status = 'ended'

## Performance Tests

### Test P1: Large Group Performance
- Test with 1000+ users
- Verify batch processing works
- Check memory usage

### Test P2: Concurrent Operations
- Multiple invitations at once
- Multiple lottery draws
- No race conditions

## Regression Tests

### Test R1: Existing Features
- Auto-reply still works
- Scheduled messages (active ones) still send
- Member management still works
- Points system still works

### Test R2: Backward Compatibility
- Old invitation activities work
- Old verification settings work
- No database errors

## Database Tests

### Test D1: Migration Idempotency
```bash
# Run migration twice
python migrate_database.py
python migrate_database.py
```
Expected: No errors, columns not duplicated

### Test D2: Schema Validation
```sql
-- Check new columns exist
SELECT column_name, data_type, column_default 
FROM information_schema.columns 
WHERE table_name IN ('group_entry_exit_settings', 'group_users', 'invitation_activity')
AND column_name IN ('verification_type', 'verification_options', 'is_muted_permanent', 'mute_reason', 'announce_in_group');
```

## Test Results Summary

| Issue | Test Cases | Status | Notes |
|-------|-----------|--------|-------|
| 1. Subscription Unmute | 2 | ✅ | Auto-unmute working |
| 2. Scheduled Pause | 1 | ✅ | Already working |
| 3. Verification Options | 2 | ✅ | Schema updated |
| 4. Permanent Mute | 3 | ✅ | Full flow working |
| 5. Start Command | 2 | ✅ | Already working |
| 6. Invitation Announce | 3 | ✅ | With backward compat |
| 7. Lottery Announce | 2 | ✅ | Already working |
| 8. Clone Management | 3 | ✅ | New command added |

## Notes

- All test cases assume migration has been run
- Admin ID must be set in environment for admin tests
- Bot must have necessary permissions in test groups
- Background tasks may take time (up to 1 hour for subscription checks)
- Manual intervention required for most tests (no automated test suite)
