"""群升级为超级群后，把库里保存的旧 chat_id 改成新 ID，并在发送失败时重试一次。"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from flask import has_app_context
from sqlalchemy.exc import IntegrityError
from telegram.error import ChatMigrated

from app import db
from app.models import (
    BotGroup,
    ChannelDiscussionPost,
    ChannelFirstComment,
    ChannelForwardRule,
    ChannelMessageStats,
    ForcedChannelSubscription,
    GroupLottery,
    ScheduledMessage,
    SyncGroupMessages,
    SyncMessageLog,
    UserbotSyncRule,
)

logger = logging.getLogger(__name__)


class MigrationRetryFailed(Exception):
    """已写入新群 ID，但对新 ID 的补发仍然失败。"""

    def __init__(self, new_chat_id, original):
        self.new_chat_id = normalize_chat_id(new_chat_id)
        self.original = original
        super().__init__(str(original))


def normalize_chat_id(value) -> Optional[str]:
    """把 Telegram chat_id 统一成去空白的字符串。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def chat_id_variants(value) -> set[str]:
    """同一 chat_id 可能以 '-10001' 或整型转字符串形式入库，查询时都匹配。"""
    text = normalize_chat_id(value)
    if not text:
        return set()
    variants = {text}
    try:
        variants.add(str(int(text)))
    except (TypeError, ValueError):
        pass
    return variants


def apply_chat_migration(old_chat_id, new_chat_id, clone_id=None, group=None) -> dict:
    """把旧 chat_id 的 BotGroup 记录及其它保存副本改成新 ID。

    若同 clone 下已有另一个群占用新 ID，不改写该 BotGroup.chat_id，避免唯一约束冲突，
    但仍会尽量更新其它表里指向旧 ID 的字段。

    返回 dict：ok / reason / old_chat_id / new_chat_id / updated_group_ids /
    existing_group_id / copies / message。
    """
    old_id = normalize_chat_id(old_chat_id)
    new_id = normalize_chat_id(new_chat_id)
    result = {
        'ok': False,
        'reason': None,
        'old_chat_id': old_id,
        'new_chat_id': new_id,
        'updated_group_ids': [],
        'existing_group_id': None,
        'copies': {},
        'message': '',
    }
    if not old_id or not new_id:
        result['reason'] = 'invalid'
        result['message'] = '群升级更新失败：旧/新 chat_id 无效'
        print(f'⚠️ {result["message"]}', flush=True)
        return result
    if chat_id_variants(old_id) & chat_id_variants(new_id):
        result['ok'] = True
        result['reason'] = 'same_id'
        result['message'] = f'群升级跳过：新旧 chat_id 相同（{old_id}）'
        return result

    old_variants = chat_id_variants(old_id)
    try:
        updated_ids, collision_id = _update_bot_groups(old_variants, new_id, group=group)
        copies = _update_stored_copies(old_variants, new_id)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        result['reason'] = 'integrity'
        result['message'] = f'群升级写入失败（唯一约束）：{exc}'
        print(f'⚠️ {result["message"]}', flush=True)
        logger.exception('apply_chat_migration integrity error old=%s new=%s', old_id, new_id)
        return result
    except Exception as exc:
        db.session.rollback()
        result['reason'] = 'error'
        result['message'] = f'群升级写入失败：{exc}'
        print(f'⚠️ {result["message"]}', flush=True)
        logger.exception('apply_chat_migration failed old=%s new=%s', old_id, new_id)
        return result

    result['updated_group_ids'] = updated_ids
    result['existing_group_id'] = collision_id
    result['copies'] = copies
    if collision_id is not None:
        result['reason'] = 'collision'
        result['message'] = (
            f'群升级冲突：新 chat_id {new_id} 已被群 {collision_id} 占用，'
            f'未改写冲突的 BotGroup；其它引用已尽量更新（{copies}）'
        )
        print(f'⚠️ {result["message"]}', flush=True)
        logger.warning(result['message'])
        return result

    result['ok'] = True
    result['message'] = (
        f'群已升级为超级群：{old_id} → {new_id}，'
        f'已更新 BotGroup {updated_ids} 及其它引用 {copies}'
    )
    print(f'✅ {result["message"]}', flush=True)
    return result


def _clone_matches(existing: BotGroup, source: BotGroup) -> bool:
    return existing.id != source.id and existing.clone_id == source.clone_id


def _find_collision(new_id: str, source: BotGroup) -> Optional[BotGroup]:
    candidates = BotGroup.query.filter(BotGroup.chat_id.in_(chat_id_variants(new_id))).all()
    for existing in candidates:
        if _clone_matches(existing, source):
            return existing
    return None


def _update_bot_groups(old_variants: set[str], new_id: str, group=None):
    updated_ids = []
    collision_id = None
    groups = []
    if group is not None and getattr(group, 'id', None):
        loaded = db.session.get(BotGroup, group.id)
        if loaded is not None:
            groups.append(loaded)
    # 同一 Telegram 群升级对所有机器人生效，把仍挂着旧 ID 的记录一并改掉
    for row in BotGroup.query.filter(BotGroup.chat_id.in_(old_variants)).all():
        if all(row.id != g.id for g in groups):
            groups.append(row)

    for row in groups:
        if normalize_chat_id(row.chat_id) not in old_variants:
            continue
        collision = _find_collision(new_id, row)
        if collision is not None:
            collision_id = collision.id
            print(
                f'⚠️ 群升级冲突：BotGroup {row.id}（{row.chat_id}）无法改为 {new_id}，'
                f'已被 BotGroup {collision.id}（clone_id={collision.clone_id}）占用',
                flush=True,
            )
            continue
        row.chat_id = new_id
        if (row.type or '') == 'group':
            row.type = 'supergroup'
        updated_ids.append(row.id)
    return updated_ids, collision_id


def _count_and_replace(model, column, old_variants: set[str], new_id: str, unique_check=None) -> int:
    changed = 0
    rows = model.query.filter(column.in_(old_variants)).all()
    for row in rows:
        if unique_check and unique_check(row, new_id):
            continue
        setattr(row, column.key, new_id)
        changed += 1
    return changed


def _sync_target_conflict(row: SyncGroupMessages, new_id: str) -> bool:
    exists = SyncGroupMessages.query.filter_by(
        source_group_id=row.source_group_id, target_group_id=new_id
    ).filter(SyncGroupMessages.id != row.id).first()
    if exists:
        row.enabled = False
        print(
            f'⚠️ 同步目标升级冲突：source={row.source_group_id} 已有目标 {new_id}，'
            f'跳过行 id={row.id}（旧目标 {row.target_group_id}）并停用该行',
            flush=True,
        )
        return True
    return False


def _first_comment_conflict(row: ChannelFirstComment, new_id: str) -> bool:
    exists = ChannelFirstComment.query.filter_by(
        group_id=row.group_id, discussion_chat_id=new_id, post_key=row.post_key
    ).filter(ChannelFirstComment.id != row.id).first()
    if exists:
        print(
            f'⚠️ 首条评论去重记录升级冲突：group={row.group_id} post_key={row.post_key} '
            f'已有 discussion_chat_id={new_id}，跳过行 id={row.id}',
            flush=True,
        )
        return True
    return False


def _update_userbot_rules(old_variants: set[str], new_id: str) -> int:
    changed = 0
    rules = UserbotSyncRule.query.all()
    for rule in rules:
        dirty = False
        if normalize_chat_id(rule.source_chat_id) in old_variants:
            rule.source_chat_id = new_id
            dirty = True
        try:
            targets = json.loads(rule.target_chat_ids or '[]')
        except (TypeError, ValueError, json.JSONDecodeError):
            targets = []
        if not isinstance(targets, list):
            targets = []
        new_targets = []
        seen = set()
        replaced = False
        for item in targets:
            value = new_id if normalize_chat_id(item) in old_variants else item
            if normalize_chat_id(item) in old_variants:
                replaced = True
            key = normalize_chat_id(value)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            new_targets.append(str(value) if value is not None else value)
        if replaced:
            rule.target_chat_ids = json.dumps(new_targets, ensure_ascii=False)
            dirty = True
        if dirty:
            changed += 1
    return changed


def _update_stored_copies(old_variants: set[str], new_id: str) -> dict:
    copies = {
        'scheduled_message_target_channel_id': _count_and_replace(
            ScheduledMessage, ScheduledMessage.target_channel_id, old_variants, new_id
        ),
        'sync_group_messages_target_group_id': _count_and_replace(
            SyncGroupMessages, SyncGroupMessages.target_group_id, old_variants, new_id,
            unique_check=_sync_target_conflict,
        ),
        'sync_message_log_target_group_id': _count_and_replace(
            SyncMessageLog, SyncMessageLog.target_group_id, old_variants, new_id
        ),
        'channel_forward_rule_target_chat_id': _count_and_replace(
            ChannelForwardRule, ChannelForwardRule.target_chat_id, old_variants, new_id
        ),
        'channel_discussion_post_discussion_chat_id': _count_and_replace(
            ChannelDiscussionPost, ChannelDiscussionPost.discussion_chat_id, old_variants, new_id
        ),
        'channel_first_comment_discussion_chat_id': _count_and_replace(
            ChannelFirstComment, ChannelFirstComment.discussion_chat_id, old_variants, new_id,
            unique_check=_first_comment_conflict,
        ),
        'channel_message_stats_channel_chat_id': _count_and_replace(
            ChannelMessageStats, ChannelMessageStats.channel_chat_id, old_variants, new_id
        ),
        'channel_message_stats_discussion_chat_id': _count_and_replace(
            ChannelMessageStats, ChannelMessageStats.discussion_chat_id, old_variants, new_id
        ),
        'forced_channel_subscription_channel_id': _count_and_replace(
            ForcedChannelSubscription, ForcedChannelSubscription.channel_id, old_variants, new_id
        ),
        'group_lottery_announce_channel_id': _count_and_replace(
            GroupLottery, GroupLottery.announce_channel_id, old_variants, new_id
        ),
        'userbot_sync_rule': _update_userbot_rules(old_variants, new_id),
    }
    return copies


def persist_chat_migration(old_chat_id, new_chat_id, clone_id=None, group=None, flask_app=None) -> dict:
    """在有 Flask 应用上下文时写入迁移；bot 事件循环里需传入 flask_app。"""
    def _apply():
        return apply_chat_migration(old_chat_id, new_chat_id, clone_id=clone_id, group=group)

    if has_app_context():
        return _apply()
    if flask_app is not None:
        with flask_app.app_context():
            return _apply()
    message = f'无法写入群升级后的新 chat_id（{old_chat_id} → {new_chat_id}）：缺少 Flask 应用上下文'
    print(f'⚠️ {message}', flush=True)
    return {
        'ok': False,
        'reason': 'no_app_context',
        'old_chat_id': normalize_chat_id(old_chat_id),
        'new_chat_id': normalize_chat_id(new_chat_id),
        'updated_group_ids': [],
        'existing_group_id': None,
        'copies': {},
        'message': message,
    }


async def call_with_chat_migration_retry(
    send_func: Callable[[Any], Any],
    chat_id,
    *,
    clone_id=None,
    group=None,
    flask_app=None,
):
    """调用 send_func(chat_id)；遇到 ChatMigrated 则写入新 ID 并对新 ID 再发一次。

    成功返回 (result, used_chat_id, migrated)。
    补发仍失败时抛出 MigrationRetryFailed（带 new_chat_id）。
    """
    try:
        return await send_func(chat_id), normalize_chat_id(chat_id), False
    except ChatMigrated as exc:
        new_id = exc.new_chat_id
        persist_chat_migration(chat_id, new_id, clone_id=clone_id, group=group, flask_app=flask_app)
        try:
            return await send_func(new_id), normalize_chat_id(new_id), True
        except Exception as retry_exc:
            raise MigrationRetryFailed(new_id, retry_exc) from retry_exc


def extract_migration_ids(message) -> tuple[Optional[str], Optional[str]]:
    """从升级服务消息中取出 (旧 chat_id, 新 chat_id)。"""
    if message is None:
        return None, None
    migrate_to = getattr(message, 'migrate_to_chat_id', None)
    migrate_from = getattr(message, 'migrate_from_chat_id', None)
    chat = getattr(message, 'chat', None)
    chat_id = getattr(message, 'chat_id', None) or (getattr(chat, 'id', None) if chat is not None else None)
    if migrate_to:
        return normalize_chat_id(chat_id), normalize_chat_id(migrate_to)
    if migrate_from:
        return normalize_chat_id(migrate_from), normalize_chat_id(chat_id)
    return None, None


async def handle_chat_migrated(update, context):
    """主号/克隆收到群升级服务消息时，把库里的旧 chat_id 改成新 ID。"""
    message = update.effective_message if update is not None else None
    old_id, new_id = extract_migration_ids(message)
    if not old_id or not new_id:
        return
    bot_data = {}
    if context is not None:
        application = getattr(context, 'application', None)
        bot_data = getattr(application, 'bot_data', None) or getattr(context, 'bot_data', {}) or {}
    flask_app = bot_data.get('flask_app')
    clone_id = bot_data.get('clone_id')
    result = persist_chat_migration(old_id, new_id, clone_id=clone_id, flask_app=flask_app)
    if not result.get('ok'):
        print(f'⚠️ 处理群升级服务消息失败：{result.get("message")}', flush=True)
