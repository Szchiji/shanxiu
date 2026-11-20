"""Telegram HTML formatters for lottery notices, lists, and results.

Dynamic text is escaped. Chunks never split tags, entities, or anchors.
Names come from award snapshots or already-stored local profiles — this
module never calls Telegram.
"""
from __future__ import annotations

import html
import json
import re

from app.models import GroupMember, GroupUser

PART_LIMIT = 3000
NAME_LIMIT = 64
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{5,32}$')


def utf16_len(text):
    return len((text or '').encode('utf-16-le')) // 2


def escape(text):
    return html.escape(str(text or ''), quote=True)


def normalize_display_name(value):
    name = ' '.join(str(value or '').replace('\u00a0', ' ').split())
    if not name:
        return None
    if len(name) > NAME_LIMIT:
        name = name[:NAME_LIMIT].rstrip() + '…'
    return name


def normalize_username(value):
    username = str(value or '').strip().lstrip('@')
    return username if USERNAME_RE.fullmatch(username) else None


def profile_from_parts(display_name=None, username=None, first_name=None, last_name=None):
    if not display_name:
        display_name = ' '.join(part for part in (first_name, last_name) if part)
    name = normalize_display_name(display_name)
    handle = normalize_username(username)
    profile = {}
    if name:
        profile['display_name'] = name
    if handle:
        profile['username'] = handle
    return profile


def profile_from_tg_user(user):
    if user is None:
        return {}
    return profile_from_parts(
        first_name=getattr(user, 'first_name', None),
        last_name=getattr(user, 'last_name', None),
        username=getattr(user, 'username', None),
    )


def merge_profiles(*profiles):
    merged = {}
    for profile in profiles:
        if not profile:
            continue
        if profile.get('display_name') and not merged.get('display_name'):
            merged['display_name'] = profile['display_name']
        if profile.get('username') and not merged.get('username'):
            merged['username'] = profile['username']
    return merged


def _profile_from_userdata(raw):
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        return {}
    return profile_from_parts(
        display_name=data.get('name') or data.get('display_name'),
        first_name=data.get('first_name'),
        last_name=data.get('last_name'),
        username=data.get('username') or data.get('user_username'),
    )


def local_profiles(group_id, user_ids):
    """Read already-stored names for this group. Never writes and never calls Telegram."""
    ids = sorted({int(uid) for uid in user_ids or []})
    if not group_id or not ids:
        return {}
    found = {uid: {} for uid in ids}
    members = GroupMember.query.filter(GroupMember.group_id == group_id, GroupMember.user_id.in_(ids)).all()
    for member in members:
        found[int(member.user_id)] = profile_from_parts(
            first_name=member.first_name, last_name=member.last_name, username=member.username)
    users = GroupUser.query.filter(GroupUser.group_id == group_id, GroupUser.tg_id.in_(ids)).all()
    for user in users:
        found[int(user.tg_id)] = merge_profiles(found.get(int(user.tg_id)), _profile_from_userdata(user.profile_data))
    return {uid: profile for uid, profile in found.items() if profile}


def split_escaped(escaped, limit=PART_LIMIT):
    parts, buf, index, length = [], '', 0, len(escaped)
    while index < length:
        if escaped[index] == '&':
            end = escaped.find(';', index)
            token = escaped[index:end + 1] if end != -1 else escaped[index]
            index = end + 1 if end != -1 else index + 1
        else:
            token = escaped[index]
            index += 1
        if buf and utf16_len(buf + token) > limit:
            parts.append(buf)
            buf = token
        else:
            buf += token
    if buf:
        parts.append(buf)
    return parts or ['']


def wrap_tag(tag, escaped, limit=PART_LIMIT):
    open_tag, close_tag = f'<{tag}>', f'</{tag}>'
    budget = max(1, limit - utf16_len(open_tag) - utf16_len(close_tag))
    return [open_tag + part + close_tag for part in split_escaped(escaped, budget)]


def pack_html(fragments, limit=PART_LIMIT):
    atoms = []
    for fragment in fragments:
        if fragment is None or fragment == '':
            continue
        if fragment == '\n':
            atoms.append('\n')
            continue
        if '<' in fragment:
            if utf16_len(fragment) > limit:
                # Tagged atoms are kept whole when possible; oversize tags are not expected.
                atoms.append(fragment)
            else:
                atoms.append(fragment)
        else:
            atoms.extend(split_escaped(fragment, limit))
    parts, current = [], ''
    for atom in atoms:
        candidate = current + atom
        if current and utf16_len(candidate) > limit:
            parts.append(current)
            current = atom
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts or ['']


def user_mention_html(user_id, profile=None, *, show_id=True):
    """Clickable name/@username. Results also append a visible ID; join notices never do."""
    uid = int(user_id)
    profile = profile or {}
    name = normalize_display_name(profile.get('display_name'))
    username = normalize_username(profile.get('username'))
    if name:
        label = escape(name)
        suffix = ' @' + escape(username) if username else ''
    elif username:
        label, suffix = '@' + escape(username), ''
    else:
        label, suffix = '一位成员', ''
    if show_id:
        suffix += f'（{uid}）'
    return f'<a href="tg://user?id={uid}">{label}</a>{suffix}'


TYPE_EMOJI = {
    'message_count': '💬',
    'message_rank': '🏅',
    'random': '🎲',
    'points': '💎',
}
PRIZE_MARKS = ('🥇', '🥈', '🥉')


def prize_mark(index):
    return PRIZE_MARKS[index] if 0 <= index < len(PRIZE_MARKS) else '🎁'


def type_line(lottery, labels):
    kind = labels.get(lottery.lottery_type, '抽奖')
    return f'{TYPE_EMOJI.get(lottery.lottery_type, "🎁")} {kind}'


def _times(lottery):
    lines = []
    if lottery.start_time:
        lines.append('🕒 开始  ' + lottery.start_time.strftime('%Y-%m-%d %H:%M'))
    if lottery.end_time:
        lines.append('⏰ 开奖  ' + lottery.end_time.strftime('%Y-%m-%d %H:%M') + '（北京时间）')
    return lines


def _rules(lottery, total):
    kind = lottery.lottery_type
    if kind == 'message_count':
        rules = [f'本期至少发言 {lottery.min_messages or 10} 条，按发言数加权抽取 {total} 人；发言越多机会越高，但不保证中奖。']
    elif kind == 'message_rank':
        rules = [f'按本期发言从高到低录取 {total} 人，同分按用户 ID 升序；奖品按排名从高到低分配。']
    elif kind == 'points':
        rules = [
            f'点击下方按钮报名，每人扣 {lottery.points_cost} 积分，仅一次；从报名者中随机抽取 {total} 人，无需发言。',
            '正常未中奖不退分；取消活动或开奖时已离群/封禁者退还报名积分。',
        ]
    else:
        rules = [f'从本期发过言的成员中随机抽取 {total} 人，每人机会相同。']
    rules.append('同一人只中一份。奖品按从上到下的顺序分配，人数不足时按此顺序分配。')
    rules.append('开奖时须仍在群内，不要求认证。')
    return rules


def _prize_block(prizes, total):
    fragments = ['🏆 <b>奖品</b>']
    for index, prize in enumerate(prizes or []):
        fragments += ['\n', f'{prize_mark(index)} ', *split_escaped(escape(prize['name'])), f'  ×{int(prize["winner_count"])}']
    if total:
        fragments += ['\n', f'👥 共 {total} 人']
    return fragments


def format_activity_notice(lottery, *, participant_count=None, labels=None):
    from app.services.lottery_workflow import LABELS, prizes_of
    labels = labels or LABELS
    prizes = prizes_of(lottery)
    total = sum(int(prize['winner_count']) for prize in prizes)
    fragments = ['🎁 ', *wrap_tag('b', escape(lottery.lottery_name))]
    fragments += ['\n', escape(type_line(lottery, labels)), '\n\n']
    fragments += _prize_block(prizes, total)
    fragments += ['\n\n', '📋 <b>规则</b>']
    for line in _rules(lottery, total):
        fragments += ['\n', *split_escaped(escape(line))]
    times = _times(lottery)
    if times:
        fragments.append('\n\n')
        for index, line in enumerate(times):
            if index:
                fragments.append('\n')
            fragments.append(escape(line))
    if participant_count is not None:
        if lottery.lottery_type == 'points':
            fragments += ['\n\n', escape(f'✅ 已报名 {int(participant_count)} 人')]
        else:
            fragments += ['\n\n', escape(f'✅ 已达到发言条件 {int(participant_count)} 人')]
    return pack_html(fragments)


def format_result_notice(lottery, winners=None, awards=None, *, profiles=None, labels=None):
    """Build HTML parts for a draw result. Never reassigns prizes for legacy rows."""
    from app.services.lottery_service import unfilled_prizes
    from app.services.lottery_workflow import LABELS, awards_of, prizes_of
    labels = labels or LABELS
    prizes = prizes_of(lottery)
    if awards is None:
        awards = awards_of(lottery)
    profiles = profiles or {}
    fragments = ['🎉 <b>开奖结果</b>\n🎁 ', *wrap_tag('b', escape(lottery.lottery_name))]
    fragments += ['\n', escape(type_line(lottery, labels))]
    times = _times(lottery)
    if times:
        fragments.append('\n')
        for index, line in enumerate(times):
            if index:
                fragments.append('\n')
            fragments.append(escape(line))

    def mention(award_or_uid, profile=None):
        if isinstance(award_or_uid, dict):
            uid = int(award_or_uid['user_id'])
            profile = merge_profiles({
                'display_name': award_or_uid.get('display_name'),
                'username': award_or_uid.get('username'),
            }, profile, profiles.get(uid))
        else:
            uid = int(award_or_uid)
            profile = merge_profiles(profile, profiles.get(uid))
        return user_mention_html(uid, profile)

    if awards:
        grouped = {}
        taken = {}
        for item in awards:
            index = int(item.get('prize_index', 0))
            grouped.setdefault(index, []).append(item)
            taken[index] = taken.get(index, 0) + 1
        for index, prize in enumerate(prizes):
            fragments += ['\n\n', f'{prize_mark(index)} ', *wrap_tag('b', escape(prize['name']))]
            for item in grouped.get(index, []):
                fragments += ['\n', mention(item)]
            missing = int(prize['winner_count']) - taken.get(index, 0)
            if missing > 0:
                fragments += ['\n', escape(f'📭 还缺 {missing} 人')]
        leftover_rows = unfilled_prizes(prizes, awards)
        if not prizes and leftover_rows:
            fragments += ['\n\n', escape('📭 还缺：' + '；'.join(f'{name} {count} 人' for name, count in leftover_rows))]
    elif winners:
        fragments += ['\n\n', '🏅 <b>获奖者</b>']
        for uid in winners:
            fragments += ['\n', mention(uid)]
        if lottery.prize_description:
            fragments += ['\n\n', '🏆 <b>奖品</b>\n', *split_escaped(escape(lottery.prize_description))]
    else:
        fragments += ['\n\n', escape('📭 无人符合条件，本期已经结束。')]
        if prizes:
            fragments += ['\n\n', *_prize_block(prizes, sum(int(p['winner_count']) for p in prizes))]
    if lottery.lottery_type == 'points':
        fragments += ['\n\n', escape('💎 未中奖不退分。已确认离群或封禁的报名积分已原额退还。')]
    return pack_html(fragments)


def format_join_success_notice(lottery_name, cost, user_id, profile=None):
    """Public, short join notice. Never shows a numeric user ID in visible text."""
    mention = user_mention_html(user_id, profile, show_id=False)
    title = ''.join(wrap_tag('b', escape(lottery_name or '抽奖')))
    return f'✅ {mention} 已报名参加 {title}，已扣 {int(cost)} 积分。'
