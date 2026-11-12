"""
Group/channel configuration backup export and import.

Version 2 expands coverage beyond the original six sections (group config,
profile fields, auto replies, scheduled messages, points rules, member
levels).  Import is backward-compatible: missing keys are skipped and
never crash the restore.  Secrets (tokens, sessions, api_hash, etc.) are
stripped from the exported JSON. Certified users and a balance/ledger snapshot
are included only with the explicit include_users_points flag.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from typing import Any, Iterable, Optional


BACKUP_VERSION = 2

# Shown in the admin UI and embedded in the export file.
BACKUP_INCLUDED_LABELS = [
    '群/频道配置与资料字段（功能配置、其他设置里的开关和文案）',
    '自动回复、定时消息、/start 消息',
    '积分规则、积分回复、积分商城/兑换、竞拍',
    '成员等级',
    '关键词过滤、进退群设置、垃圾防护、强制订阅、不活跃用户',
    '定时开关群、邀请活动（不含邀请记录）',
    '群底部按钮',
    '群/频道同步规则（不含同步日志）',
    '抽奖、投票、问答（不含参与记录和中奖运营数据）',
    '频道转发规则、消息模板、频道帖按钮、优惠券配置',
    '插件开关',
]

BACKUP_EXCLUDED_LABELS = [
    '认证用户名单、群成员同步表、积分余额与流水、兑换记录',
    '同步日志、邀请记录、警告记录、管理操作日志、消息统计',
    '投票/问答/红包的参与记录，抽奖中奖名单与发言计数',
    'Bot Token、小号 session、api_hash 等机密信息',
]

SECRET_KEYS = {
    'token',
    'bot_token',
    'api_hash',
    'api_hash_enc',
    'api_id',
    'session',
    'session_enc',
    'session_string',
    'password',
    'secret',
    'webhook_secret',
    'phone',
    'phone_number',
    'phone_masked',
}


def _is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    if lowered in SECRET_KEYS:
        return True
    # Catch botToken / apiHash style keys without matching "announce_channel_id".
    compact = lowered.replace('_', '').replace('-', '')
    return compact in {
        'token', 'bottoken', 'apihash', 'apihashenc', 'apiid',
        'session', 'sessionenc', 'sessionstring', 'webhooksecret',
    }


def sanitize_secrets(value: Any) -> Any:
    """Recursively drop keys that look like credentials."""
    if isinstance(value, dict):
        return {
            k: sanitize_secrets(v)
            for k, v in value.items()
            if not _is_secret_key(k)
        }
    if isinstance(value, list):
        return [sanitize_secrets(v) for v in value]
    return value


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> Optional[time]:
    if not value:
        return None
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()
    text = str(value).strip()
    if not text:
        return None
    try:
        return time.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _json_load(raw: Any, default):
    if raw is None or raw == '':
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, type(default)) or default == parsed else parsed
    except (TypeError, ValueError):
        return default


def _json_dump(value: Any, default='[]') -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return default


def _safe_dict(item: Any) -> Optional[dict]:
    return item if isinstance(item, dict) else None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_group_config(group_id: int, *, include_users_points: bool = False) -> dict:
    """Build a sanitised JSON-serialisable backup for *group_id*."""
    from app.models import (
        AutoReply, BotGroup, ChannelCoupon, ChannelForwardRule,
        ChannelMessageTemplate, ChannelPostButtonConfig, ForcedChannelSubscription,
        GroupBottomButton, GroupEntryExitSettings, GroupLottery,
        GroupPluginSettings, GroupVote, InactiveUserSettings, InvitationActivity,
        KeywordFilter, MemberLevel, OtherSettings, PointsAuction, PointsAutoReply,
        PointsExchangeItem, PointsRule, QuizGame, ScheduledMessage, SpamProtection,
        StartMessage, SyncGroupMessages, TimedGroupControl,
    )

    group = BotGroup.query.get(group_id)
    if not group:
        raise ValueError('群组不存在')

    config = {
        'backup_version': BACKUP_VERSION,
        'includes': list(BACKUP_INCLUDED_LABELS),
        'excludes': list(BACKUP_EXCLUDED_LABELS),
        'group_info': {
            'title': group.title,
            'chat_id': group.chat_id,
            'type': group.type,
        },
        'config': _json_load(group.config, {}),
        'fields_config': _json_load(group.fields_config, []),
        'auto_replies': _export_auto_replies(group_id),
        'scheduled_messages': _export_scheduled_messages(group_id),
        'start_messages': [
            {
                'message_type': sm.message_type,
                'media_type': sm.media_type,
                'media_url': sm.media_url,
                'content': sm.content,
                'links': _json_load(sm.links, []),
                'is_active': sm.is_active,
            }
            for sm in StartMessage.query.filter_by(group_id=group_id).all()
        ],
        'points_rules': [
            {
                'rule_name': pr.rule_name,
                'rule_type': pr.rule_type,
                'points_amount': pr.points_amount,
                'max_daily_points': pr.max_daily_points,
                'is_active': pr.is_active,
            }
            for pr in PointsRule.query.filter_by(group_id=group_id).all()
        ],
        'member_levels': [
            {
                'level_name': ml.level_name,
                'required_points': ml.required_points,
                'permissions': _json_load(ml.permissions, {}),
                'badge_emoji': ml.badge_emoji,
            }
            for ml in MemberLevel.query.filter_by(group_id=group_id).all()
        ],
        'keyword_filters': [
            {
                'keyword': kf.keyword,
                'filter_type': kf.filter_type,
                'match_type': kf.match_type,
                'action': kf.action,
                'is_active': kf.is_active,
            }
            for kf in KeywordFilter.query.filter_by(group_id=group_id).all()
        ],
        'entry_exit_settings': _export_singleton(
            GroupEntryExitSettings.query.filter_by(group_id=group_id).first(),
            (
                'entry_verification_enabled', 'verification_type',
                'verification_question', 'verification_answer',
                'verification_options', 'verification_timeout',
                'welcome_enabled', 'welcome_message', 'welcome_media_type',
                'welcome_media_url', 'exit_ban_enabled', 'exit_ban_duration',
            ),
        ),
        'spam_protection': _export_singleton(
            SpamProtection.query.filter_by(group_id=group_id).first(),
            (
                'enabled', 'max_messages_per_minute', 'block_links',
                'block_forwards', 'block_stickers', 'punishment_type',
                'punishment_duration', 'whitelist_users',
            ),
        ),
        'forced_subscription': _export_forced_sub(group_id),
        'inactive_user_settings': _export_inactive(group_id),
        'timed_group_control': _export_timed_control(group_id),
        'invitation_activity': _export_invitation(group_id),
        'points_auto_replies': [
            {
                'trigger_keyword': row.trigger_keyword,
                'points_cost': row.points_cost,
                'content': row.content,
                'media_type': row.media_type,
                'media_url': row.media_url,
                'media_urls': _json_load(row.media_urls, []),
                'is_active': row.is_active,
            }
            for row in PointsAutoReply.query.filter_by(group_id=group_id).all()
        ],
        'points_exchange_items': [
            {
                'item_name': row.item_name,
                'item_description': row.item_description,
                'points_cost': row.points_cost,
                'stock': row.stock,
                'redemption_info': row.redemption_info,
                'is_active': row.is_active,
            }
            for row in PointsExchangeItem.query.filter_by(group_id=group_id).all()
        ],
        'points_auctions': [
            {
                'item_name': row.item_name,
                'item_description': row.item_description,
                'starting_price': row.starting_price,
                'auction_start': _iso(row.auction_start),
                'auction_end': _iso(row.auction_end),
                'status': row.status,
            }
            for row in PointsAuction.query.filter_by(group_id=group_id).all()
        ],
        'group_bottom_buttons': [
            {
                'button_text': row.button_text,
                'button_url': row.button_url,
                'button_callback': row.button_callback,
                'trigger_keyword': row.trigger_keyword,
                'input_field_placeholder': row.input_field_placeholder,
                'button_order': row.button_order,
                'row_position': row.row_position,
                'is_active': row.is_active,
            }
            for row in GroupBottomButton.query.filter_by(group_id=group_id).all()
        ],
        'sync_rules': [
            {
                'target_group_id': row.target_group_id,
                'target_title': row.target_title,
                'enabled': row.enabled,
                'sync_media': row.sync_media,
                'sync_forwards': row.sync_forwards,
                'include_sender_prefix': row.include_sender_prefix,
                'sender_prefix_style': row.sender_prefix_style,
                'filter_keywords': _json_load(row.filter_keywords, []),
                'sync_own_bot_posts': row.sync_own_bot_posts,
            }
            for row in SyncGroupMessages.query.filter_by(source_group_id=group_id).all()
        ],
        'lotteries': [
            {
                'lottery_name': row.lottery_name,
                'lottery_type': row.lottery_type,
                'prize_description': row.prize_description,
                'min_messages': row.min_messages,
                'top_n_winners': row.top_n_winners,
                'start_time': _iso(row.start_time),
                'end_time': _iso(row.end_time),
                'announce_channel_id': row.announce_channel_id,
                'status': row.status,
                'pool_source': getattr(row, 'pool_source', None) or 'certified',
            }
            for row in GroupLottery.query.filter_by(group_id=group_id).all()
        ],
        'votes': [
            {
                'title': row.title,
                'description': row.description,
                'options': _json_load(row.options, []),
                'vote_type': row.vote_type,
                'max_choices': row.max_choices,
                'is_anonymous': row.is_anonymous,
                'allow_revote': row.allow_revote,
                'start_time': _iso(row.start_time),
                'end_time': _iso(row.end_time),
                'status': row.status,
            }
            for row in GroupVote.query.filter_by(group_id=group_id).all()
        ],
        'quizzes': [
            {
                'question': row.question,
                'answers': _json_load(row.answers, []),
                'correct_answer_index': row.correct_answer_index,
                'explanation': row.explanation,
                'points_reward': row.points_reward,
                'time_limit': row.time_limit,
                'difficulty': row.difficulty,
                'category': row.category,
                'is_active': row.is_active,
            }
            for row in QuizGame.query.filter_by(group_id=group_id).all()
        ],
        'channel_forward_rules': [
            {
                'rule_name': row.rule_name,
                'source_keywords': row.source_keywords,
                'target_chat_id': row.target_chat_id,
                'target_thread_id': row.target_thread_id,
                'forward_mode': row.forward_mode,
                'is_active': row.is_active,
            }
            for row in ChannelForwardRule.query.filter_by(group_id=group_id).all()
        ],
        'channel_message_templates': [
            {
                'template_name': row.template_name,
                'trigger_keywords': row.trigger_keywords,
                'media_type': row.media_type,
                'media_url': row.media_url,
                'content': row.content,
                'links': _json_load(row.links, []),
                'send_as_reply': row.send_as_reply,
                'is_active': row.is_active,
            }
            for row in ChannelMessageTemplate.query.filter_by(group_id=group_id).all()
        ],
        'channel_post_buttons': _export_post_buttons(group_id),
        'channel_coupons': [
            {
                'coupon_name': row.coupon_name,
                'coupon_code': row.coupon_code,
                'button_text': row.button_text,
                'button_url': row.button_url,
                'trigger_keywords': row.trigger_keywords,
                'sort_order': row.sort_order,
                'is_active': row.is_active,
            }
            for row in ChannelCoupon.query.filter_by(group_id=group_id).all()
        ],
        'other_settings': _export_other_settings(group_id),
        'plugin_settings': [
            {
                'plugin_name': row.plugin_name,
                'enabled': row.enabled,
            }
            for row in GroupPluginSettings.query.filter_by(group_id=group_id).all()
        ],
        'export_time': datetime.now().isoformat(),
    }
    if include_users_points:
        config['users_points'] = _export_users_points(group_id)
        config['includes'].append('认证用户、积分余额与流水（完整快照）')
        config['excludes'][0] = '群成员同步表、兑换记录'
    return sanitize_secrets(config)


def _export_auto_replies(group_id: int) -> list:
    from app.models import AutoReply
    rows = []
    for ar in AutoReply.query.filter_by(group_id=group_id).all():
        rows.append({
            'trigger_keyword': ar.trigger_keyword,
            'media_type': ar.media_type,
            'media_url': ar.media_url,
            'media_urls': _json_load(getattr(ar, 'media_urls', None), []),
            'content': ar.content,
            'links': _json_load(ar.links, []),
            'delete_after': ar.delete_after,
            'remark': ar.remark,
            'is_active': ar.is_active,
        })
    return rows


def _export_scheduled_messages(group_id: int) -> list:
    from app.models import ScheduledMessage
    rows = []
    for sm in ScheduledMessage.query.filter_by(group_id=group_id).all():
        rows.append({
            'media_type': sm.media_type,
            'media_url': sm.media_url,
            'media_urls': _json_load(sm.media_urls, []),
            'content': sm.content,
            'links': _json_load(sm.links, []),
            'repeat_interval': sm.repeat_interval,
            'delete_previous': sm.delete_previous,
            'start_time': _iso(sm.start_time),
            'stop_time': _iso(sm.stop_time),
            'remark': sm.remark,
            'auto_pin': getattr(sm, 'auto_pin', False),
            'message_thread_id': getattr(sm, 'message_thread_id', None),
            'target_type': getattr(sm, 'target_type', 'group'),
            'target_channel_id': getattr(sm, 'target_channel_id', None),
            'is_active': sm.is_active,
        })
    return rows


def _export_singleton(row, fields: Iterable[str]) -> Optional[dict]:
    if not row:
        return None
    data = {}
    for name in fields:
        value = getattr(row, name, None)
        if name in ('whitelist_users', 'verification_options') or isinstance(value, (dict, list)):
            # keep JSON text as parsed structure when it is stored as text
            if isinstance(value, str):
                data[name] = _json_load(value, value)
            else:
                data[name] = value
        elif isinstance(value, (datetime, date, time)):
            data[name] = _iso(value)
        else:
            data[name] = value
    return data


def _export_forced_sub(group_id: int) -> Optional[dict]:
    from app.models import ForcedChannelSubscription
    row = ForcedChannelSubscription.query.filter_by(group_id=group_id).first()
    if not row:
        return None
    # reward_given_user_ids / last_checked_at are operational — omit.
    return {
        'enabled': row.enabled,
        'channel_id': row.channel_id,
        'channel_username': row.channel_username,
        'check_interval': row.check_interval,
        'unsubscribe_action': row.unsubscribe_action,
        'verification_message': row.verification_message,
        'reward_points': row.reward_points,
    }


def _export_inactive(group_id: int) -> Optional[dict]:
    from app.models import InactiveUserSettings
    row = InactiveUserSettings.query.filter_by(group_id=group_id).first()
    if not row:
        return None
    return {
        'enabled': row.enabled,
        'inactivity_days': row.inactivity_days,
        'action_type': row.action_type,
        'check_interval': row.check_interval,
        'warning_enabled': row.warning_enabled,
        'warning_days': row.warning_days,
        'warning_message': row.warning_message,
    }


def _export_timed_control(group_id: int) -> Optional[dict]:
    from app.models import TimedGroupControl
    row = TimedGroupControl.query.filter_by(group_id=group_id).first()
    if not row:
        return None
    return {
        'enabled': row.enabled,
        'open_time': _iso(row.open_time),
        'close_time': _iso(row.close_time),
        'timezone': row.timezone,
        'close_message': row.close_message,
        'open_message': row.open_message,
    }


def _export_invitation(group_id: int) -> Optional[dict]:
    from app.models import InvitationActivity
    row = InvitationActivity.query.filter_by(group_id=group_id).first()
    if not row:
        return None
    return {
        'enabled': row.enabled,
        'reward_points': row.reward_points,
        'minimum_invites': row.minimum_invites,
        'activity_start': _iso(row.activity_start),
        'activity_end': _iso(row.activity_end),
        'description': row.description,
        'announce_in_group': row.announce_in_group,
        'link_keyword': row.link_keyword,
    }


def _export_post_buttons(group_id: int) -> Optional[dict]:
    from app.models import ChannelPostButtonConfig
    row = ChannelPostButtonConfig.query.filter_by(group_id=group_id).first()
    if not row:
        return None
    return {
        'enabled': row.enabled,
        'links': _json_load(row.links, []),
    }


def _export_other_settings(group_id: int) -> Optional[dict]:
    from app.models import OtherSettings
    row = OtherSettings.query.filter_by(group_id=group_id).first()
    if not row:
        return None
    return {
        'auto_delete_join_msg': row.auto_delete_join_msg,
        'auto_delete_leave_msg': row.auto_delete_leave_msg,
        'auto_delete_promote_msg': row.auto_delete_promote_msg,
        'auto_delete_pin_msg': row.auto_delete_pin_msg,
        'auto_delete_channel_pin_msg': row.auto_delete_channel_pin_msg,
        'cancel_channel_pin': row.cancel_channel_pin,
        'auto_delete_channel_discussion_msg': row.auto_delete_channel_discussion_msg,
        'channel_auto_buttons': row.channel_auto_buttons,
        'channel_discussion_keyword_filter': row.channel_discussion_keyword_filter,
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_group_config(group_id: int, raw_config: Any) -> dict:
    """Import a backup (new or legacy). Missing sections are skipped.

    Returns a summary dict.  Raises ValueError for totally invalid payloads.
    """
    from app import db
    from app.models import BotGroup

    if not isinstance(raw_config, dict):
        raise ValueError('配置格式错误')

    group = BotGroup.query.get(group_id)
    if not group:
        raise ValueError('群组不存在')

    config = sanitize_secrets(raw_config)
    # Validate before any writes; absent section must never clear operational data.
    if 'users_points' in config:
        _validate_users_points(config['users_points'])
    imported = []

    if isinstance(config.get('config'), dict):
        group.config = json.dumps(config['config'], ensure_ascii=False)
        imported.append('config')

    if isinstance(config.get('fields_config'), list):
        group.fields_config = json.dumps(config['fields_config'], ensure_ascii=False)
        imported.append('fields_config')

    _import_auto_replies(group_id, config, imported)
    _import_scheduled_messages(group_id, config, imported)
    _import_points_rules(group_id, config, imported)
    _import_member_levels(group_id, config, imported)
    _import_start_messages(group_id, config, imported)
    _import_keyword_filters(group_id, config, imported)
    _import_entry_exit(group_id, config, imported)
    _import_spam(group_id, config, imported)
    _import_forced_sub(group_id, config, imported)
    _import_inactive(group_id, config, imported)
    _import_timed_control(group_id, config, imported)
    _import_invitation(group_id, config, imported)
    _import_points_auto_replies(group_id, config, imported)
    _import_exchange_items(group_id, config, imported)
    _import_auctions(group_id, config, imported)
    _import_bottom_buttons(group_id, config, imported)
    _import_sync_rules(group_id, config, imported)
    _import_lotteries(group_id, config, imported)
    _import_votes(group_id, config, imported)
    _import_quizzes(group_id, config, imported)
    _import_forward_rules(group_id, config, imported)
    _import_templates(group_id, config, imported)
    _import_post_buttons(group_id, config, imported)
    _import_coupons(group_id, config, imported)
    _import_other_settings(group_id, config, imported)
    _import_plugin_settings(group_id, config, imported)

    if 'users_points' in config:
        _import_users_points(group_id, config['users_points'])
        imported.append('users_points')
    db.session.commit()
    return {'imported': imported}


def _iter_dicts(config: dict, key: str):
    value = config.get(key)
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            yield item


def _get_or_create(model, group_id: int, **filters):
    from app import db
    row = model.query.filter_by(group_id=group_id, **filters).first()
    if not row:
        row = model(group_id=group_id, **filters)
        db.session.add(row)
    return row


def _import_auto_replies(group_id, config, imported):
    from app.models import AutoReply
    items = list(_iter_dicts(config, 'auto_replies'))
    if not items:
        return
    from app import db
    for data in items:
        keyword = data.get('trigger_keyword', '')
        existing = AutoReply.query.filter_by(group_id=group_id, trigger_keyword=keyword).first()
        if existing:
            continue
        db.session.add(AutoReply(
            group_id=group_id,
            trigger_keyword=keyword,
            media_type=data.get('media_type', 'text'),
            media_url=data.get('media_url'),
            media_urls=_json_dump(data.get('media_urls', [])),
            content=data.get('content'),
            links=_json_dump(data.get('links', [])),
            delete_after=data.get('delete_after', 0),
            remark=data.get('remark'),
            is_active=data.get('is_active', True),
        ))
    imported.append('auto_replies')


def _import_scheduled_messages(group_id, config, imported):
    from app import db
    from app.models import ScheduledMessage
    from app.utils import get_beijing_now
    items = list(_iter_dicts(config, 'scheduled_messages'))
    if not items:
        return
    # Keep historical behaviour: always append scheduled messages.
    # compute_aligned_next_send_at lives in routes; set next_send_at simply.
    now = get_beijing_now()
    for data in items:
        media_urls_val = data.get('media_urls')
        if isinstance(media_urls_val, list):
            media_urls_json = json.dumps(media_urls_val, ensure_ascii=False)
        elif isinstance(media_urls_val, str) and media_urls_val.strip():
            media_urls_json = media_urls_val
        elif data.get('media_url'):
            media_urls_json = json.dumps([data.get('media_url')])
        else:
            media_urls_json = '[]'
        sm = ScheduledMessage(
            group_id=group_id,
            media_type=data.get('media_type', 'text'),
            media_url=data.get('media_url'),
            media_urls=media_urls_json,
            content=data.get('content'),
            links=_json_dump(data.get('links', [])),
            repeat_interval=data.get('repeat_interval', 0) or 0,
            delete_previous=data.get('delete_previous', False),
            start_time=_parse_dt(data.get('start_time')),
            stop_time=_parse_dt(data.get('stop_time')),
            remark=data.get('remark'),
            auto_pin=data.get('auto_pin', False),
            message_thread_id=data.get('message_thread_id'),
            target_type=data.get('target_type', 'group'),
            target_channel_id=data.get('target_channel_id'),
            is_active=data.get('is_active', True),
        )
        if sm.start_time and sm.is_active:
            if sm.start_time > now:
                sm.next_send_at = sm.start_time
        db.session.add(sm)
    imported.append('scheduled_messages')


def _import_points_rules(group_id, config, imported):
    from app import db
    from app.models import PointsRule
    items = list(_iter_dicts(config, 'points_rules'))
    if not items:
        return
    for data in items:
        name = data.get('rule_name', '')
        existing = PointsRule.query.filter_by(group_id=group_id, rule_name=name).first()
        if existing:
            continue
        db.session.add(PointsRule(
            group_id=group_id,
            rule_name=name,
            rule_type=data.get('rule_type', 'custom'),
            points_amount=data.get('points_amount', 1),
            max_daily_points=data.get('max_daily_points'),
            is_active=data.get('is_active', True),
        ))
    imported.append('points_rules')


def _import_member_levels(group_id, config, imported):
    from app import db
    from app.models import MemberLevel
    items = list(_iter_dicts(config, 'member_levels'))
    if not items:
        return
    for data in items:
        name = data.get('level_name', '')
        existing = MemberLevel.query.filter_by(group_id=group_id, level_name=name).first()
        if existing:
            continue
        db.session.add(MemberLevel(
            group_id=group_id,
            level_name=name,
            required_points=data.get('required_points', 0),
            permissions=_json_dump(data.get('permissions', {}), '{}'),
            badge_emoji=data.get('badge_emoji'),
        ))
    imported.append('member_levels')


def _import_start_messages(group_id, config, imported):
    from app.models import StartMessage
    items = list(_iter_dicts(config, 'start_messages'))
    if not items:
        return
    for data in items:
        mtype = data.get('message_type', 'user')
        row = _get_or_create(StartMessage, group_id, message_type=mtype)
        row.media_type = data.get('media_type', 'text')
        row.media_url = data.get('media_url')
        row.content = data.get('content')
        row.links = _json_dump(data.get('links', []))
        row.is_active = data.get('is_active', True)
    imported.append('start_messages')


def _import_keyword_filters(group_id, config, imported):
    from app.models import KeywordFilter
    items = list(_iter_dicts(config, 'keyword_filters'))
    if not items:
        return
    for data in items:
        keyword = data.get('keyword', '')
        ftype = data.get('filter_type', 'blacklist')
        row = _get_or_create(KeywordFilter, group_id, keyword=keyword, filter_type=ftype)
        row.match_type = data.get('match_type', 'contains')
        row.action = data.get('action', 'delete')
        row.is_active = data.get('is_active', True)
    imported.append('keyword_filters')


def _apply_fields(row, data: dict, mapping: dict):
    """mapping: dest_field -> (key, default, transform or None)."""
    for field, spec in mapping.items():
        key, default, transform = spec
        if key not in data:
            if getattr(row, field, None) is None and default is not None:
                setattr(row, field, default)
            continue
        value = data.get(key, default)
        if transform:
            value = transform(value)
        setattr(row, field, value)


def _import_entry_exit(group_id, config, imported):
    from app.models import GroupEntryExitSettings
    data = _safe_dict(config.get('entry_exit_settings'))
    if not data:
        return
    row = _get_or_create(GroupEntryExitSettings, group_id)
    for field in (
        'entry_verification_enabled', 'verification_type', 'verification_question',
        'verification_answer', 'verification_timeout', 'welcome_enabled',
        'welcome_message', 'welcome_media_type', 'welcome_media_url',
        'exit_ban_enabled', 'exit_ban_duration',
    ):
        if field in data:
            setattr(row, field, data[field])
    if 'verification_options' in data:
        row.verification_options = _json_dump(data['verification_options'], '[]') if not isinstance(data['verification_options'], str) else data['verification_options']
    imported.append('entry_exit_settings')


def _import_spam(group_id, config, imported):
    from app.models import SpamProtection
    data = _safe_dict(config.get('spam_protection'))
    if not data:
        return
    row = _get_or_create(SpamProtection, group_id)
    for field in (
        'enabled', 'max_messages_per_minute', 'block_links', 'block_forwards',
        'block_stickers', 'punishment_type', 'punishment_duration',
    ):
        if field in data:
            setattr(row, field, data[field])
    if 'whitelist_users' in data:
        val = data['whitelist_users']
        row.whitelist_users = val if isinstance(val, str) else _json_dump(val, '[]')
    imported.append('spam_protection')


def _import_forced_sub(group_id, config, imported):
    from app.models import ForcedChannelSubscription
    data = _safe_dict(config.get('forced_subscription'))
    if not data:
        return
    row = _get_or_create(ForcedChannelSubscription, group_id)
    for field in (
        'enabled', 'channel_id', 'channel_username', 'check_interval',
        'unsubscribe_action', 'verification_message', 'reward_points',
    ):
        if field in data:
            setattr(row, field, data[field])
    imported.append('forced_subscription')


def _import_inactive(group_id, config, imported):
    from app.models import InactiveUserSettings
    data = _safe_dict(config.get('inactive_user_settings'))
    if not data:
        return
    row = _get_or_create(InactiveUserSettings, group_id)
    for field in (
        'enabled', 'inactivity_days', 'action_type', 'check_interval',
        'warning_enabled', 'warning_days', 'warning_message',
    ):
        if field in data:
            setattr(row, field, data[field])
    imported.append('inactive_user_settings')


def _import_timed_control(group_id, config, imported):
    from app.models import TimedGroupControl
    data = _safe_dict(config.get('timed_group_control'))
    if not data:
        return
    row = _get_or_create(TimedGroupControl, group_id)
    if 'enabled' in data:
        row.enabled = data['enabled']
    if 'open_time' in data:
        row.open_time = _parse_time(data['open_time'])
    if 'close_time' in data:
        row.close_time = _parse_time(data['close_time'])
    for field in ('timezone', 'close_message', 'open_message'):
        if field in data:
            setattr(row, field, data[field])
    imported.append('timed_group_control')


def _import_invitation(group_id, config, imported):
    from app.models import InvitationActivity
    data = _safe_dict(config.get('invitation_activity'))
    if not data:
        return
    row = _get_or_create(InvitationActivity, group_id)
    for field in (
        'enabled', 'reward_points', 'minimum_invites', 'description',
        'announce_in_group', 'link_keyword',
    ):
        if field in data:
            setattr(row, field, data[field])
    if 'activity_start' in data:
        row.activity_start = _parse_dt(data['activity_start'])
    if 'activity_end' in data:
        row.activity_end = _parse_dt(data['activity_end'])
    imported.append('invitation_activity')


def _import_points_auto_replies(group_id, config, imported):
    from app import db
    from app.models import PointsAutoReply
    items = list(_iter_dicts(config, 'points_auto_replies'))
    if not items:
        return
    for data in items:
        keyword = data.get('trigger_keyword', '')
        existing = PointsAutoReply.query.filter_by(group_id=group_id, trigger_keyword=keyword).first()
        if existing:
            continue
        db.session.add(PointsAutoReply(
            group_id=group_id,
            trigger_keyword=keyword,
            points_cost=data.get('points_cost', 0),
            content=data.get('content'),
            media_type=data.get('media_type', 'text'),
            media_url=data.get('media_url'),
            media_urls=_json_dump(data.get('media_urls', [])),
            is_active=data.get('is_active', True),
        ))
    imported.append('points_auto_replies')


def _import_exchange_items(group_id, config, imported):
    from app import db
    from app.models import PointsExchangeItem
    items = list(_iter_dicts(config, 'points_exchange_items'))
    if not items:
        return
    for data in items:
        name = data.get('item_name', '')
        existing = PointsExchangeItem.query.filter_by(group_id=group_id, item_name=name).first()
        if existing:
            continue
        db.session.add(PointsExchangeItem(
            group_id=group_id,
            item_name=name,
            item_description=data.get('item_description'),
            points_cost=data.get('points_cost', 100),
            stock=data.get('stock'),
            redemption_info=data.get('redemption_info'),
            is_active=data.get('is_active', True),
        ))
    imported.append('points_exchange_items')


def _import_auctions(group_id, config, imported):
    from app import db
    from app.models import PointsAuction
    items = list(_iter_dicts(config, 'points_auctions'))
    if not items:
        return
    for data in items:
        name = data.get('item_name', '')
        existing = PointsAuction.query.filter_by(group_id=group_id, item_name=name).first()
        if existing:
            continue
        db.session.add(PointsAuction(
            group_id=group_id,
            item_name=name,
            item_description=data.get('item_description'),
            starting_price=data.get('starting_price', 100),
            current_bid=0,
            current_bidder_id=None,
            auction_start=_parse_dt(data.get('auction_start')),
            auction_end=_parse_dt(data.get('auction_end')),
            status=data.get('status', 'pending'),
        ))
    imported.append('points_auctions')


def _import_bottom_buttons(group_id, config, imported):
    from app import db
    from app.models import GroupBottomButton
    items = list(_iter_dicts(config, 'group_bottom_buttons'))
    if not items:
        return
    for data in items:
        text = data.get('button_text', '')
        row_pos = data.get('row_position', 0)
        existing = GroupBottomButton.query.filter_by(
            group_id=group_id, button_text=text, row_position=row_pos
        ).first()
        if existing:
            continue
        db.session.add(GroupBottomButton(
            group_id=group_id,
            button_text=text,
            button_url=data.get('button_url'),
            button_callback=data.get('button_callback'),
            trigger_keyword=data.get('trigger_keyword'),
            input_field_placeholder=data.get('input_field_placeholder'),
            button_order=data.get('button_order', 0),
            row_position=row_pos,
            is_active=data.get('is_active', True),
        ))
    imported.append('group_bottom_buttons')


def _import_sync_rules(group_id, config, imported):
    from app.models import SyncGroupMessages
    items = list(_iter_dicts(config, 'sync_rules'))
    if not items:
        return
    for data in items:
        target = data.get('target_group_id')
        if not target:
            continue
        row = SyncGroupMessages.query.filter_by(
            source_group_id=group_id, target_group_id=str(target)
        ).first()
        if not row:
            row = SyncGroupMessages(source_group_id=group_id, target_group_id=str(target))
            from app import db
            db.session.add(row)
        row.target_title = data.get('target_title')
        row.enabled = data.get('enabled', False)
        row.sync_media = data.get('sync_media', True)
        row.sync_forwards = data.get('sync_forwards', True)
        row.include_sender_prefix = data.get('include_sender_prefix', False)
        row.sender_prefix_style = data.get('sender_prefix_style', 'newline')
        row.filter_keywords = (
            data['filter_keywords']
            if isinstance(data.get('filter_keywords'), str)
            else _json_dump(data.get('filter_keywords', []))
        )
        if 'sync_own_bot_posts' in data:
            row.sync_own_bot_posts = data['sync_own_bot_posts']
    imported.append('sync_rules')


def _import_lotteries(group_id, config, imported):
    from app import db
    from app.models import GroupLottery
    from app.services.lottery_service import normalize_pool_source
    items = list(_iter_dicts(config, 'lotteries'))
    if not items:
        return
    for data in items:
        name = data.get('lottery_name', '')
        existing = GroupLottery.query.filter_by(group_id=group_id, lottery_name=name).first()
        if existing:
            continue
        lottery = GroupLottery(
            group_id=group_id,
            lottery_name=name or '抽奖',
            lottery_type=data.get('lottery_type', 'message_count'),
            prize_description=data.get('prize_description'),
            min_messages=data.get('min_messages', 10),
            top_n_winners=data.get('top_n_winners', 3),
            start_time=_parse_dt(data.get('start_time')),
            end_time=_parse_dt(data.get('end_time')),
            announce_channel_id=data.get('announce_channel_id'),
            status=data.get('status', 'pending'),
            winner_ids='[]',
        )
        lottery.pool_source = normalize_pool_source(data.get('pool_source'))
        db.session.add(lottery)
    imported.append('lotteries')


def _import_votes(group_id, config, imported):
    from app import db
    from app.models import GroupVote
    items = list(_iter_dicts(config, 'votes'))
    if not items:
        return
    for data in items:
        title = data.get('title', '')
        existing = GroupVote.query.filter_by(group_id=group_id, title=title).first()
        if existing:
            continue
        db.session.add(GroupVote(
            group_id=group_id,
            title=title or '投票',
            description=data.get('description'),
            options=_json_dump(data.get('options', [])),
            vote_type=data.get('vote_type', 'single'),
            max_choices=data.get('max_choices', 1),
            is_anonymous=data.get('is_anonymous', False),
            allow_revote=data.get('allow_revote', True),
            start_time=_parse_dt(data.get('start_time')),
            end_time=_parse_dt(data.get('end_time')),
            status=data.get('status', 'pending'),
        ))
    imported.append('votes')


def _import_quizzes(group_id, config, imported):
    from app import db
    from app.models import QuizGame
    items = list(_iter_dicts(config, 'quizzes'))
    if not items:
        return
    for data in items:
        question = data.get('question', '')
        existing = QuizGame.query.filter_by(group_id=group_id, question=question).first()
        if existing:
            continue
        idx = data.get('correct_answer_index', 0)
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = 0
        db.session.add(QuizGame(
            group_id=group_id,
            question=question or '问题',
            answers=_json_dump(data.get('answers', [])),
            correct_answer_index=idx,
            explanation=data.get('explanation'),
            points_reward=data.get('points_reward', 10),
            time_limit=data.get('time_limit', 60),
            difficulty=data.get('difficulty', 'medium'),
            category=data.get('category'),
            is_active=data.get('is_active', True),
        ))
    imported.append('quizzes')


def _import_forward_rules(group_id, config, imported):
    from app import db
    from app.models import ChannelForwardRule
    items = list(_iter_dicts(config, 'channel_forward_rules'))
    if not items:
        return
    for data in items:
        name = data.get('rule_name', '')
        existing = ChannelForwardRule.query.filter_by(group_id=group_id, rule_name=name).first()
        if existing:
            continue
        target = data.get('target_chat_id') or ''
        db.session.add(ChannelForwardRule(
            group_id=group_id,
            rule_name=name or '规则',
            source_keywords=data.get('source_keywords'),
            target_chat_id=target,
            target_thread_id=data.get('target_thread_id'),
            forward_mode=data.get('forward_mode', 'copy'),
            is_active=data.get('is_active', True),
        ))
    imported.append('channel_forward_rules')


def _import_templates(group_id, config, imported):
    from app import db
    from app.models import ChannelMessageTemplate
    items = list(_iter_dicts(config, 'channel_message_templates'))
    if not items:
        return
    for data in items:
        name = data.get('template_name', '')
        existing = ChannelMessageTemplate.query.filter_by(group_id=group_id, template_name=name).first()
        if existing:
            continue
        db.session.add(ChannelMessageTemplate(
            group_id=group_id,
            template_name=name or '模板',
            trigger_keywords=data.get('trigger_keywords'),
            media_type=data.get('media_type', 'text'),
            media_url=data.get('media_url'),
            content=data.get('content'),
            links=_json_dump(data.get('links', [])),
            send_as_reply=data.get('send_as_reply', True),
            is_active=data.get('is_active', True),
        ))
    imported.append('channel_message_templates')


def _import_post_buttons(group_id, config, imported):
    from app.models import ChannelPostButtonConfig
    data = _safe_dict(config.get('channel_post_buttons'))
    if not data:
        return
    row = _get_or_create(ChannelPostButtonConfig, group_id)
    if 'enabled' in data:
        row.enabled = data['enabled']
    if 'links' in data:
        row.links = data['links'] if isinstance(data['links'], str) else _json_dump(data['links'], '[]')
    imported.append('channel_post_buttons')


def _import_coupons(group_id, config, imported):
    from app import db
    from app.models import ChannelCoupon
    items = list(_iter_dicts(config, 'channel_coupons'))
    if not items:
        return
    for data in items:
        name = data.get('coupon_name', '')
        existing = ChannelCoupon.query.filter_by(group_id=group_id, coupon_name=name).first()
        if existing:
            continue
        db.session.add(ChannelCoupon(
            group_id=group_id,
            coupon_name=name or '优惠券',
            coupon_code=data.get('coupon_code'),
            button_text=data.get('button_text'),
            button_url=data.get('button_url'),
            trigger_keywords=data.get('trigger_keywords'),
            sort_order=data.get('sort_order', 0),
            is_active=data.get('is_active', True),
        ))
    imported.append('channel_coupons')


def _import_other_settings(group_id, config, imported):
    from app.models import OtherSettings
    data = _safe_dict(config.get('other_settings'))
    if not data:
        return
    row = _get_or_create(OtherSettings, group_id)
    for field in (
        'auto_delete_join_msg', 'auto_delete_leave_msg', 'auto_delete_promote_msg',
        'auto_delete_pin_msg', 'auto_delete_channel_pin_msg', 'cancel_channel_pin',
        'auto_delete_channel_discussion_msg', 'channel_auto_buttons',
        'channel_discussion_keyword_filter',
    ):
        if field in data:
            setattr(row, field, data[field])
    imported.append('other_settings')


def _import_plugin_settings(group_id, config, imported):
    from app.models import GroupPluginSettings
    items = list(_iter_dicts(config, 'plugin_settings'))
    if not items:
        return
    for data in items:
        name = data.get('plugin_name')
        if not name:
            continue
        row = _get_or_create(GroupPluginSettings, group_id, plugin_name=name)
        row.enabled = bool(data.get('enabled', True))
    imported.append('plugin_settings')


# Explicit allowlists: never serialise model __dict__ or auth sessions.
_CERTIFIED_FIELDS = (
    'tg_id', 'profile_data', 'member_tags', 'expiration_date', 'is_banned',
    'is_muted_permanent', 'mute_reason', 'checkin_time', 'last_activity',
    'join_source', 'created_at',
)
_USER_DATES = {'expiration_date', 'checkin_time', 'last_activity', 'created_at'}


def _export_users_points(group_id):
    from app import db
    from app.models import GroupUser, UserPoints, PointsLog
    from sqlalchemy.orm import Session
    # A separate read-only snapshot keeps balances and ledger consistent even
    # while points are awarded. PostgreSQL READ COMMITTED is not sufficient.
    isolation = 'REPEATABLE READ' if db.engine.dialect.name == 'postgresql' else 'SERIALIZABLE'
    with Session(bind=db.engine.execution_options(isolation_level=isolation)) as snapshot:
        users = []
        for row in snapshot.query(GroupUser).filter_by(group_id=group_id).all():
            data = _export_singleton(row, _CERTIFIED_FIELDS)
            data['profile_data'] = _json_load(row.profile_data, {})
            data['member_tags'] = _json_load(row.member_tags, [])
            users.append(data)
        balances = [
            {'user_id': r.user_id, 'points_balance': r.points_balance,
             'level_name': r.current_level.level_name if r.current_level else None,
             'updated_at': _iso(r.updated_at)}
            for r in snapshot.query(UserPoints).filter_by(group_id=group_id).all()
        ]
        logs = [_export_singleton(r, ('user_id', 'points_change', 'reason', 'balance_after', 'created_at'))
                for r in snapshot.query(PointsLog).filter_by(group_id=group_id).order_by(PointsLog.id).all()]
        return {'certified_users': users, 'points_balances': balances, 'points_logs': logs}


def _validate_users_points(data):
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), list)
        for key in ('certified_users', 'points_balances', 'points_logs')
    ):
        raise ValueError('认证用户和积分快照不完整，未导入')
    for key, id_key in (('certified_users', 'tg_id'), ('points_balances', 'user_id'), ('points_logs', 'user_id')):
        seen = set()
        for row in data[key]:
            if not isinstance(row, dict) or type(row.get(id_key)) is not int or row[id_key] <= 0:
                raise ValueError('认证用户和积分快照的用户 ID 无效')
            if key != 'points_logs' and row[id_key] in seen:
                raise ValueError('认证用户和积分快照有重复用户')
            seen.add(row[id_key])
            fields = ('points_balance',) if key == 'points_balances' else (
                ('points_change', 'balance_after') if key == 'points_logs' else ())
            if any(type(row.get(f)) is not int for f in fields):
                raise ValueError('积分快照的数值无效')


def _import_users_points(group_id, data):
    """Explicit complete section replaces only this group's three collections.

    Missing section leaves them untouched; an explicit empty snapshot clears
    them. No sessions, redemption/invitation records or Telegram side effects.
    Local primary keys and cached Telegram tag/subscription state are not copied.
    """
    from app import db
    from app.models import GroupUser, UserPoints, PointsLog, MemberLevel
    for model in (GroupUser, UserPoints, PointsLog):
        model.query.filter_by(group_id=group_id).delete(synchronize_session='fetch')
    for item in data['certified_users']:
        fields = {k: item[k] for k in _CERTIFIED_FIELDS if k in item}
        for key in _USER_DATES & fields.keys():
            fields[key] = _parse_dt(fields[key])
        for key, default in (('profile_data', '{}'), ('member_tags', '[]')):
            if key in fields:
                fields[key] = _json_dump(fields[key], default)
        db.session.add(GroupUser(group_id=group_id, **fields))
    for item in data['points_balances']:
        level = MemberLevel.query.filter_by(group_id=group_id, level_name=item.get('level_name')).first() if item.get('level_name') else None
        db.session.add(UserPoints(group_id=group_id, user_id=item['user_id'],
                                  points_balance=item['points_balance'],
                                  current_level_id=level.id if level else None,
                                  updated_at=_parse_dt(item.get('updated_at'))))
    for item in data['points_logs']:
        db.session.add(PointsLog(group_id=group_id, user_id=item['user_id'],
                                points_change=item['points_change'], balance_after=item['balance_after'],
                                reason=item.get('reason'), created_at=_parse_dt(item.get('created_at'))))
