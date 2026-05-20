"""
app/bot/handlers/commands.py
-----------------------------
Bot command handlers for group moderation and query commands.

Functions migrated from app.modules.core.routes:
  cmd_kick, cmd_ban, cmd_unban, cmd_mute, cmd_unmute,
  cmd_pin, cmd_unpin, cmd_warn, cmd_userinfo,
  cmd_rank, cmd_invite_rank, cmd_active, cmd_clones

Backward-compat re-exports are kept in routes.py so existing code
continues to work without changes.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import func

from app.bot import state as _state
from app.utils import (
    is_user_admin_in_group,
    is_user_chat_owner,
    get_muted_permissions,
    get_unrestricted_permissions,
    log_admin_action,
    get_beijing_now,
    safe_int,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _full_name(user) -> str:
    """Return the display name of a Telegram user object."""
    name = user.first_name or ''
    if user.last_name:
        name = f"{name} {user.last_name}"
    return name.strip()


# ---------------------------------------------------------------------------
# Moderation commands
# ---------------------------------------------------------------------------

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """踢出群成员 /kick"""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    if not await is_user_admin_in_group(context.bot, chat.id, user.id):
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要踢出的用户消息")
        return

    target = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(chat.id, target.id)
        await context.bot.unban_chat_member(chat.id, target.id)
        await update.message.reply_text(f"✅ 已将 {target.first_name} 踢出群组")

        if _state.global_flask_app:
            def _log():
                with _state.global_flask_app.app_context():
                    from app.models import BotGroup
                    group = BotGroup.query.filter_by(
                        chat_id=str(chat.id),
                        clone_id=context.application.bot_data.get('clone_id'),
                    ).first()
                    if group:
                        log_admin_action(
                            group.id, user.id, _full_name(user),
                            'kick', target.id, _full_name(target),
                        )
            await asyncio.get_running_loop().run_in_executor(None, _log)
    except Exception as exc:
        await update.message.reply_text(f"❌ 操作失败: {exc}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """封禁群成员 /ban"""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    if not await is_user_admin_in_group(context.bot, chat.id, user.id):
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要封禁的用户消息")
        return

    target = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(chat.id, target.id)
        await update.message.reply_text(f"✅ 已将 {target.first_name} 封禁")

        if _state.global_flask_app:
            def _log():
                with _state.global_flask_app.app_context():
                    from app.models import BotGroup
                    group = BotGroup.query.filter_by(
                        chat_id=str(chat.id),
                        clone_id=context.application.bot_data.get('clone_id'),
                    ).first()
                    if group:
                        log_admin_action(
                            group.id, user.id, _full_name(user),
                            'ban', target.id, _full_name(target),
                        )
            await asyncio.get_running_loop().run_in_executor(None, _log)
    except Exception as exc:
        await update.message.reply_text(f"❌ 操作失败: {exc}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """解封群成员 /unban"""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    if not await is_user_admin_in_group(context.bot, chat.id, user.id):
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要解封的用户消息")
        return

    target = update.message.reply_to_message.from_user
    try:
        await context.bot.unban_chat_member(chat.id, target.id)
        await update.message.reply_text(f"✅ 已将 {target.first_name} 解封")
    except Exception as exc:
        await update.message.reply_text(f"❌ 操作失败: {exc}")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """禁言群成员 /mute [时间(分钟)]"""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    if not await is_user_admin_in_group(context.bot, chat.id, user.id):
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要禁言的用户消息")
        return

    target = update.message.reply_to_message.from_user

    duration = 60
    if context.args:
        try:
            duration = int(context.args[0])
            if duration <= 0:
                duration = 60
        except ValueError:
            duration = 60

    try:
        until_date = datetime.now() + timedelta(minutes=duration)
        await context.bot.restrict_chat_member(
            chat.id, target.id, get_muted_permissions(), until_date=until_date
        )
        await update.message.reply_text(f"✅ 已将 {target.first_name} 禁言 {duration} 分钟")
    except Exception as exc:
        await update.message.reply_text(f"❌ 操作失败: {exc}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """解除禁言 /unmute"""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    if not await is_user_admin_in_group(context.bot, chat.id, user.id):
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要解除禁言的用户消息")
        return

    target = update.message.reply_to_message.from_user

    if await is_user_chat_owner(context.bot, chat.id, target.id):
        await update.message.reply_text(
            f"⏭️ 无法解除群主 {target.first_name} 的禁言 - 群主权限无需解除禁言"
        )
        return

    try:
        await context.bot.restrict_chat_member(
            chat.id, target.id, get_unrestricted_permissions()
        )
        await update.message.reply_text(f"✅ 已解除 {target.first_name} 的禁言")
    except Exception as exc:
        await update.message.reply_text(f"❌ 操作失败: {exc}")


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """置顶消息 /pin"""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    if not await is_user_admin_in_group(context.bot, chat.id, user.id):
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要置顶的消息")
        return

    try:
        await context.bot.pin_chat_message(chat.id, update.message.reply_to_message.message_id)
        await update.message.reply_text("✅ 消息已置顶")
    except Exception as exc:
        await update.message.reply_text(f"❌ 操作失败: {exc}")


async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消置顶 /unpin"""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    if not await is_user_admin_in_group(context.bot, chat.id, user.id):
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return

    try:
        if update.message.reply_to_message:
            await context.bot.unpin_chat_message(chat.id, update.message.reply_to_message.message_id)
        else:
            await context.bot.unpin_all_chat_messages(chat.id)
        await update.message.reply_text("✅ 已取消置顶")
    except Exception as exc:
        await update.message.reply_text(f"❌ 操作失败: {exc}")


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """警告用户 /warn [原因]"""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    if not await is_user_admin_in_group(context.bot, chat.id, user.id):
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要警告的用户消息")
        return

    target = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "违反群规"

    await update.message.reply_text(
        f"⚠️ 警告\n\n用户: {target.first_name}\n原因: {reason}\n\n请遵守群规，避免再次违规！"
    )


async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查询用户详细信息 /userinfo"""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    if not await is_user_admin_in_group(context.bot, chat.id, user.id):
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return

    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        target = getattr(update.message, 'forward_from', None)

    if not target:
        await update.message.reply_text("❌ 请回复或转发用户的消息来查看详情")
        return

    def _get_user_info():
        with _state.global_flask_app.app_context():
            from app import db
            from app.models import BotGroup, GroupUser, UserPoints, PointsLog, MemberLevel
            from sqlalchemy import func, case

            group = BotGroup.query.filter_by(
                chat_id=str(chat.id),
                clone_id=context.application.bot_data.get('clone_id'),
            ).first()
            if not group:
                return None, None, None, 0, 0, None

            group_user = GroupUser.query.filter_by(
                group_id=group.id, tg_id=target.id
            ).first()

            user_points = None
            total_earned = 0
            total_spent = 0
            try:
                user_points = UserPoints.query.filter_by(
                    group_id=group.id, user_id=target.id
                ).first()
                result = db.session.query(
                    func.sum(
                        case((PointsLog.points_change > 0, PointsLog.points_change), else_=0)
                    ).label('earned'),
                    func.sum(
                        case((PointsLog.points_change < 0, func.abs(PointsLog.points_change)), else_=0)
                    ).label('spent'),
                ).filter(
                    PointsLog.group_id == group.id,
                    PointsLog.user_id == target.id,
                ).first()
                if result:
                    total_earned = result.earned or 0
                    total_spent = result.spent or 0
            except Exception as exc:
                print(f"Error getting user points in cmd_userinfo: {exc}")

            # Resolve member level badge
            member_level = None
            if user_points:
                try:
                    levels = MemberLevel.query.filter_by(
                        group_id=group.id
                    ).order_by(MemberLevel.required_points.desc()).all()
                    for lvl in levels:
                        if user_points.points_balance >= lvl.required_points:
                            member_level = lvl
                            break
                except Exception as exc:
                    print(f"Error getting member level in cmd_userinfo: {exc}")

            return group_user, user_points, group, total_earned, total_spent, member_level

    group_user, user_pts, group, total_earned, total_spent, member_level = \
        await asyncio.get_running_loop().run_in_executor(None, _get_user_info)

    lines = ["👤 用户详细信息\n", "━━━━━━━━━━━━━━━━"]
    lines.append(f"📛 用户名: {target.first_name or '-'}")
    if target.last_name:
        lines.append(f"   姓氏: {target.last_name}")
    if target.username:
        lines.append(f"🔗 Username: @{target.username}")
    lines.append(f"🆔 用户ID: <code>{target.id}</code>")
    lines.append(f"🤖 机器人: {'是' if target.is_bot else '否'}")

    if group_user:
        lines.append("\n📊 群组信息\n━━━━━━━━━━━━━━━━")
        try:
            profile = json.loads(group_user.profile_data or '{}')
            for k, v in profile.items():
                lines.append(f"   {k}: {v}")
        except Exception:
            pass
        if group_user.expiration_date:
            lines.append(f"⏰ 到期时间: {group_user.expiration_date.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"🚫 封禁状态: {'已封禁' if group_user.is_banned else '正常'}")
        if group_user.checkin_time:
            lines.append(f"✅ 最后签到: {group_user.checkin_time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"🟢 在线状态: {'在线' if group_user.online else '离线'}")

    if user_pts:
        lines.append("\n💰 积分信息\n━━━━━━━━━━━━━━━━")
        lines.append(f"💎 当前积分: {user_pts.points_balance}")
        if total_earned or total_spent:
            lines.append(f"📈 总获得: {total_earned}")
            lines.append(f"📉 总消耗: {total_spent}")
        if member_level:
            badge = member_level.badge_emoji or "🎖️"
            lines.append(f"{badge} 会员等级: {member_level.level_name}")

    try:
        member = await context.bot.get_chat_member(chat.id, target.id)
        lines.append("\n👥 群组权限\n━━━━━━━━━━━━━━━━")
        status_labels = {
            'creator': '群主', 'administrator': '管理员',
            'member': '普通成员', 'restricted': '受限用户',
            'left': '已退出', 'kicked': '已被移除',
        }
        lines.append(f"📌 身份: {member.status} ({status_labels.get(member.status, '')})")
    except Exception as exc:
        print(f"Error getting member info in cmd_userinfo: {exc}")

    await update.message.reply_html("\n".join(lines))


# ---------------------------------------------------------------------------
# Ranking commands
# ---------------------------------------------------------------------------

async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """积分排行榜 /rank [数量]"""
    chat = update.effective_chat

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    limit = 10
    if context.args:
        try:
            limit = int(context.args[0])
            if limit < 1 or limit > 50:
                limit = 10
        except ValueError:
            pass

    if not _state.global_flask_app:
        return

    def _get_rankings():
        with _state.global_flask_app.app_context():
            from app import db
            from app.models import BotGroup, GroupUser, UserPoints, MemberLevel

            group = BotGroup.query.filter_by(
                chat_id=str(chat.id),
                clone_id=context.application.bot_data.get('clone_id'),
            ).first()
            if not group:
                return None, "群组不存在"

            rows = db.session.query(UserPoints, GroupUser).join(
                GroupUser,
                db.and_(
                    UserPoints.group_id == GroupUser.group_id,
                    UserPoints.user_id == GroupUser.tg_id,
                ),
            ).filter(
                UserPoints.group_id == group.id,
                UserPoints.points_balance > 0,
            ).order_by(
                UserPoints.points_balance.desc()
            ).limit(limit).all()

            results = []
            for up, gu in rows:
                badge = ''
                if up.current_level_id:
                    lvl = MemberLevel.query.get(up.current_level_id)
                    if lvl and lvl.badge_emoji:
                        badge = lvl.badge_emoji
                try:
                    profile = json.loads(gu.profile_data) if gu.profile_data else {}
                    name = profile.get('name', f'User_{up.user_id}')
                except Exception:
                    name = f'User_{up.user_id}'
                results.append({'name': name, 'points': up.points_balance, 'badge': badge})
            return results, None

    results, error = await asyncio.get_running_loop().run_in_executor(None, _get_rankings)

    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    if not results:
        await update.message.reply_text("📊 暂无积分排行数据")
        return

    medals = ["🥇", "🥈", "🥉"]
    msg = f"🏆 <b>积分排行榜 TOP {len(results)}</b>\n\n"
    for idx, r in enumerate(results):
        icon = medals[idx] if idx < 3 else f"{idx + 1}."
        badge = r['badge'] + ' ' if r['badge'] else ''
        msg += f"{icon} {badge}{r['name']} - {r['points']} 积分\n"

    await update.message.reply_text(msg, parse_mode='HTML')


async def cmd_invite_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """邀请排行榜 /invite_rank [数量]"""
    chat = update.effective_chat

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    limit = 10
    if context.args:
        try:
            limit = int(context.args[0])
            if limit < 1 or limit > 50:
                limit = 10
        except ValueError:
            pass

    if not _state.global_flask_app:
        return

    def _get_invite_rankings():
        with _state.global_flask_app.app_context():
            from app import db
            from app.models import BotGroup, GroupUser, InvitationRecord

            group = BotGroup.query.filter_by(
                chat_id=str(chat.id),
                clone_id=context.application.bot_data.get('clone_id'),
            ).first()
            if not group:
                return None, "群组不存在"

            rows = db.session.query(
                InvitationRecord.inviter_id,
                func.count(InvitationRecord.id).label('invite_count'),
                func.sum(InvitationRecord.points_awarded).label('total_points'),
            ).filter(
                InvitationRecord.group_id == group.id
            ).group_by(
                InvitationRecord.inviter_id
            ).order_by(
                func.count(InvitationRecord.id).desc()
            ).limit(limit).all()

            results = []
            for inviter_id, invite_count, total_points in rows:
                gu = GroupUser.query.filter_by(
                    group_id=group.id, tg_id=inviter_id
                ).first()
                name = f'User_{inviter_id}'
                if gu:
                    try:
                        profile = json.loads(gu.profile_data) if gu.profile_data else {}
                        name = profile.get('name', name)
                    except Exception:
                        pass
                results.append({
                    'name': name,
                    'invite_count': invite_count,
                    'total_points': total_points or 0,
                })
            return results, None

    results, error = await asyncio.get_running_loop().run_in_executor(None, _get_invite_rankings)

    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    if not results:
        await update.message.reply_text("📊 暂无邀请排行数据")
        return

    medals = ["🥇", "🥈", "🥉"]
    msg = f"🏆 <b>邀请排行榜 TOP {len(results)}</b>\n\n"
    for idx, r in enumerate(results):
        icon = medals[idx] if idx < 3 else f"{idx + 1}."
        msg += f"{icon} {r['name']} - 邀请 {r['invite_count']} 人 ({r['total_points']} 积分)\n"

    await update.message.reply_text(msg, parse_mode='HTML')


async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """活跃排行榜 /active [week|month]"""
    chat = update.effective_chat

    if chat.type not in ('group', 'supergroup'):
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return

    period = 'week'
    if context.args and context.args[0].lower() in ('week', 'month'):
        period = context.args[0].lower()

    if not _state.global_flask_app:
        return

    def _get_active_rankings():
        with _state.global_flask_app.app_context():
            from app import db
            from app.models import BotGroup, GroupUser, MessageStatistics

            group = BotGroup.query.filter_by(
                chat_id=str(chat.id),
                clone_id=context.application.bot_data.get('clone_id'),
            ).first()
            if not group:
                return None, "群组不存在"

            now = get_beijing_now()
            if period == 'week':
                start_date = (now - timedelta(days=7)).date()
                period_label = "本周"
            else:
                start_date = (now - timedelta(days=30)).date()
                period_label = "本月"

            rows = db.session.query(
                MessageStatistics.user_id,
                func.sum(MessageStatistics.message_count).label('total_messages'),
                GroupUser,
            ).join(
                GroupUser,
                db.and_(
                    MessageStatistics.group_id == GroupUser.group_id,
                    MessageStatistics.user_id == GroupUser.tg_id,
                ),
            ).filter(
                MessageStatistics.group_id == group.id,
                MessageStatistics.date >= start_date,
            ).group_by(
                MessageStatistics.user_id, GroupUser.id
            ).order_by(
                db.text('total_messages DESC')
            ).limit(20).all()

            results = []
            for user_id, total_messages, gu in rows:
                try:
                    profile = json.loads(gu.profile_data) if gu.profile_data else {}
                    name = profile.get('name', f'User_{user_id}')
                except Exception:
                    name = f'User_{user_id}'
                results.append({'name': name, 'messages': int(total_messages)})
            return {'results': results, 'period_label': period_label}, None

    data, error = await asyncio.get_running_loop().run_in_executor(None, _get_active_rankings)

    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    if not data['results']:
        await update.message.reply_text(f"📊 暂无{data['period_label']}活跃数据")
        return

    medals = ["🥇", "🥈", "🥉"]
    msg = f"📈 <b>{data['period_label']}活跃排行榜 TOP {len(data['results'])}</b>\n\n"
    for idx, r in enumerate(data['results']):
        icon = medals[idx] if idx < 3 else f"{idx + 1}."
        msg += f"{icon} {r['name']} - {r['messages']} 条消息\n"

    await update.message.reply_text(msg, parse_mode='HTML')


# ---------------------------------------------------------------------------
# Clone management command (admin only, private chat)
# ---------------------------------------------------------------------------

async def cmd_clones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看克隆机器人列表 /clones — 管理员私聊专用"""
    user_id = update.effective_user.id
    chat = update.effective_chat
    admin_id = safe_int(os.getenv('ADMIN_ID', 0))

    if chat.type != 'private':
        await update.message.reply_text("❌ 此命令只能在私聊中使用")
        return

    if user_id != admin_id:
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return

    if not _state.global_flask_app:
        await update.message.reply_text("❌ 系统未就绪")
        return

    def _get_clones():
        with _state.global_flask_app.app_context():
            from app.models import BotClone
            clones = BotClone.query.order_by(BotClone.created_at.desc()).all()
            return [{
                'id': c.id,
                'clone_name': c.clone_name,
                'owner_user_id': c.owner_user_id,
                'is_active': c.is_active,
                'expiration_date': (
                    c.expiration_date.strftime('%Y-%m-%d %H:%M:%S')
                    if c.expiration_date else '无期限'
                ),
                'description': c.description or '无描述',
            } for c in clones]

    clones = await asyncio.get_running_loop().run_in_executor(None, _get_clones)

    if not clones:
        await update.message.reply_text("📝 当前没有克隆机器人")
        return

    message = "🤖 <b>克隆机器人列表</b>\n\n"
    for clone in clones:
        status = "✅ 活跃" if clone['is_active'] else "❌ 停用"
        message += (
            f"<b>ID:</b> {clone['id']}\n"
            f"<b>名称:</b> {clone['clone_name']}\n"
            f"<b>状态:</b> {status}\n"
            f"<b>拥有者ID:</b> {clone['owner_user_id'] or '无'}\n"
            f"<b>有效期:</b> {clone['expiration_date']}\n"
            f"<b>描述:</b> {clone['description']}\n"
            "─────────────────\n"
        )

    domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', 'localhost:5000')
    message += f"\n💡 <b>提示:</b> 在管理后台可以管理克隆机器人\n访问: {domain}/core/bot_clones"

    await update.message.reply_html(message)
