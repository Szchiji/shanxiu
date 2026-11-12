"""
Helpers for the per-group「过期自动禁言」switch.

Historical behaviour (before the switch was wired): expired authenticated
users were always muted.  The admin checkbox was saved but never read, and
the settings page defaulted it to unchecked, so many saved configs contain
``auto_mute_expired: false`` even though production always muted.

Default for a missing key is therefore ON, and a one-time startup backfill
sets existing group configs to True so live behaviour does not suddenly stop.
After the backfill, unchecking the box and saving will actually disable
auto-mute for that group.
"""

from __future__ import annotations

import json
import logging

from app.models import BotGroup, SystemConfig

logger = logging.getLogger(__name__)

BACKFILL_FLAG = 'auto_mute_expired_backfill_v1'


def is_auto_mute_expired_enabled(conf) -> bool:
    """Return whether a group should auto-mute expired authenticated users.

    Missing / unknown configs default to True to match historical behaviour.
    """
    if not isinstance(conf, dict):
        return True
    if 'auto_mute_expired' not in conf:
        return True
    return bool(conf.get('auto_mute_expired'))


def parse_group_config_dict(raw_config):
    """Parse BotGroup.config JSON, unwrapping a nested ``{"config": ...}`` if present.

    Returns ``(root_dict, inner_conf, used_nested)``.  ``inner_conf`` is the
    dict that actually holds settings keys.
    """
    if not raw_config:
        return {}, {}, False
    try:
        data = json.loads(raw_config)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, {}, False
    if not isinstance(data, dict):
        return {}, {}, False
    if 'config' in data and isinstance(data.get('config'), dict):
        return data, data['config'], True
    return data, data, False


def backfill_auto_mute_expired_on(db_session) -> int:
    """One-time: set ``auto_mute_expired=True`` on every existing group config.

    Returns the number of groups updated.  Subsequent calls are no-ops once
    the SystemConfig flag is stored.
    """
    if SystemConfig.get_value(BACKFILL_FLAG) == '1':
        return 0

    updated = 0
    groups = BotGroup.query.all()
    for group in groups:
        root, conf, nested = parse_group_config_dict(group.config)
        if not isinstance(conf, dict):
            conf = {}
        conf['auto_mute_expired'] = True
        if nested:
            root['config'] = conf
            group.config = json.dumps(root, ensure_ascii=False)
        else:
            # ``root`` is ``conf`` when not nested; also handle empty/invalid
            group.config = json.dumps(conf, ensure_ascii=False)
        updated += 1

    SystemConfig.set_value(BACKFILL_FLAG, '1')
    db_session.commit()
    if updated:
        logger.info('Backfilled auto_mute_expired=True on %s groups', updated)
    return updated
