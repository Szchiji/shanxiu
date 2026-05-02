"""
app/plugins.py
--------------
Plugin registry and helper utilities for the plugin system.

Each feature module should expose a ``PLUGIN_META`` dict in its
``__init__.py``:

    PLUGIN_META = {
        'name': 'points',           # internal key stored in DB
        'display_name': '积分系统',
        'description': '用户积分、签到、排行榜',
        'default_enabled': True,
    }

Modules register themselves via :func:`register_plugin`.  The registry is
populated at application startup (or lazily on first access).
"""

from __future__ import annotations

from typing import Dict, List

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, dict] = {}


def register_plugin(meta: dict) -> None:
    """Add a plugin to the global registry.

    ``meta`` must contain at least ``'name'`` and ``'display_name'``.
    """
    name = meta.get('name')
    if not name:
        raise ValueError("Plugin meta must include 'name'")
    _REGISTRY[name] = {
        'display_name': meta.get('display_name', name),
        'description': meta.get('description', ''),
        'default_enabled': meta.get('default_enabled', True),
        **{k: v for k, v in meta.items() if k not in ('name',)},
    }


def get_all_plugins() -> List[dict]:
    """Return all registered plugins as a list, sorted by display_name."""
    return sorted(
        [{'name': n, **m} for n, m in _REGISTRY.items()],
        key=lambda x: x['display_name'],
    )


def is_plugin_registered(plugin_name: str) -> bool:
    return plugin_name in _REGISTRY


# ---------------------------------------------------------------------------
# DB helper (requires Flask app context)
# ---------------------------------------------------------------------------

def is_plugin_enabled(group_id: int, plugin_name: str) -> bool:
    """Return True if *plugin_name* is enabled for *group_id*.

    Falls back to True when no setting row exists (backward-compatible).
    Requires an active Flask application context.
    """
    from app.models import GroupPluginSettings
    return GroupPluginSettings.is_enabled(group_id, plugin_name)


# ---------------------------------------------------------------------------
# Built-in plugin definitions
# (registered here so they appear in the UI even before the modules are
# fully split out; update display_name/description as modules are extracted)
# ---------------------------------------------------------------------------

_BUILTIN_PLUGINS: List[dict] = [
    {
        'name': 'points',
        'display_name': '积分系统',
        'description': '用户积分、签到、排行榜、积分商城',
        'default_enabled': True,
    },
    {
        'name': 'lottery',
        'display_name': '群抽奖',
        'description': '发言计数抽奖和发言排行抽奖',
        'default_enabled': True,
    },
    {
        'name': 'spam',
        'display_name': '消息过滤',
        'description': '垃圾消息防护、关键词过滤、频率限制',
        'default_enabled': True,
    },
    {
        'name': 'invitation',
        'display_name': '邀请活动',
        'description': '邀请新成员获得积分奖励',
        'default_enabled': True,
    },
    {
        'name': 'games',
        'display_name': '互动游戏',
        'description': '投票、答题、红包',
        'default_enabled': True,
    },
    {
        'name': 'moderation',
        'display_name': '群管理',
        'description': '封禁、禁言、踢人、警告等管理命令',
        'default_enabled': True,
    },
    {
        'name': 'sync',
        'display_name': '消息同步',
        'description': '跨群组消息同步',
        'default_enabled': False,
    },
]

for _meta in _BUILTIN_PLUGINS:
    register_plugin(_meta)
