"""Pure selection helpers. Eligibility and freezing live in lottery_workflow.

There is deliberately no authentication-table/whole-group fallback. The legacy
pool_source schema/backup key is accepted only as a deprecated ignored field.
"""
from __future__ import annotations

import secrets


def normalize_pool_source(value=None):
    return 'speakers'


def pick_winners_random(pool, count):
    pool = sorted(set(int(uid) for uid in pool))
    return secrets.SystemRandom().sample(pool, min(count, len(pool))) if pool and count > 0 else []


def pick_winner_weighted(participants):
    pool = {}
    for uid, weight in participants:
        if weight > 0:
            pool[int(uid)] = pool.get(int(uid), 0) + int(weight)
    total = sum(pool.values())
    if not total:
        return None
    pick = secrets.randbelow(total)
    for uid, weight in sorted(pool.items()):
        if pick < weight:
            return uid
        pick -= weight


def pick_winners_top_n(participants, count):
    if count <= 0:
        return []
    pool = {}
    for uid, weight in participants:
        pool[int(uid)] = max(weight, pool.get(int(uid), weight))
    return [uid for uid,_ in sorted(pool.items(), key=lambda x: (-x[1], x[0]))[:count]]


def validate_lottery_draw(lottery_type, participants, top_n_winners=1):
    if lottery_type not in ('message_count', 'message_rank', 'random', 'points'):
        return False, f'未知的抽奖类型: {lottery_type}'
    if lottery_type != 'message_count' and not 1 <= top_n_winners <= 100:
        return False, '获奖人数必须在1—100之间'
    if not participants and lottery_type in ('message_count', 'message_rank'):
        return False, '没有符合条件的参与者'
    return True, ''


def run_lottery_draw(lottery_type, participants, top_n_winners=1, fallback_pool=None):
    # Retain the obsolete parameter for callers of the pure helper, but NEVER
    # substitute an unrelated pool when no eligible participant exists.
    if lottery_type in ('random', 'points'):
        return pick_winners_random([uid for uid,_ in participants], top_n_winners)
    if lottery_type == 'message_count':
        uid = pick_winner_weighted(participants)
        return [] if uid is None else [uid]
    if lottery_type == 'message_rank':
        return pick_winners_top_n(participants, top_n_winners)
    return []
