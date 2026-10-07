"""
tests/unit/test_sync_service.py
-------------------------------
Unit tests for cross-group sync helpers (keyword blacklist, anti-loop).
"""

from types import SimpleNamespace

from app.services.sync_service import (
    is_same_source_target,
    keyword_blocks_sync,
    should_skip_bot_sender,
    sync_filter_text,
)


class TestKeywordBlocksSync:
    def test_empty_never_blocks(self):
        assert keyword_blocks_sync(None, '["广告"]') is False
        assert keyword_blocks_sync('hello', None) is False
        assert keyword_blocks_sync('hello', '[]') is False

    def test_blacklist_hit(self):
        assert keyword_blocks_sync('这是广告内容', '["广告", "垃圾"]') is True

    def test_blacklist_miss(self):
        assert keyword_blocks_sync('正常消息', '["广告"]') is False

    def test_invalid_json_does_not_block(self):
        assert keyword_blocks_sync('广告', 'not-json') is False
        assert keyword_blocks_sync('广告', '{"a":1}') is False


class TestAntiLoop:
    def test_skip_bot_sender(self):
        assert should_skip_bot_sender(SimpleNamespace(is_bot=True)) is True
        assert should_skip_bot_sender(SimpleNamespace(is_bot=False)) is False
        assert should_skip_bot_sender(None) is False

    def test_same_source_target(self):
        assert is_same_source_target(-100123, '-100123') is True
        assert is_same_source_target(-100123, '-100999') is False
        assert is_same_source_target(None, '-100123') is False


class TestSyncFilterText:
    def test_prefers_text_over_caption(self):
        msg = SimpleNamespace(text='hello', caption='cap')
        assert sync_filter_text(msg) == 'hello'

    def test_falls_back_to_caption(self):
        msg = SimpleNamespace(text=None, caption='photo caption')
        assert sync_filter_text(msg) == 'photo caption'

    def test_none_when_empty(self):
        msg = SimpleNamespace(text=None, caption=None)
        assert sync_filter_text(msg) is None
