"""Bounded, single-flight, non-blocking avatar cache; no database access here."""
import asyncio
import threading
import time
from collections import OrderedDict

PLACEHOLDER = b'''<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64" rx="32" fill="#e2e8f0"/><circle cx="32" cy="24" r="11" fill="#94a3b8"/><path d="M12 58a20 20 0 0 1 40 0" fill="#94a3b8"/></svg>'''
TIMEOUT = 3
SUCCESS_TTL = 300
FAILURE_TTL = 45
MAX_ENTRIES = 256
MAX_INFLIGHT = 16
MAX_BYTES = 2 * 1024 * 1024


class PhotoCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.inflight = set()

    def lookup_or_claim(self, key):
        with self.lock:
            now = time.monotonic()
            for old, value in list(self.entries.items()):
                if value[0] <= now:
                    self.entries.pop(old, None)
            hit = self.entries.get(key)
            if hit:
                self.entries.move_to_end(key)
                return ('ready', hit[1]) if hit[1] else ('missing', None)
            if key in self.inflight:
                return 'pending', None
            if len(self.inflight) >= MAX_INFLIGHT:
                return 'busy', None
            self.inflight.add(key)
            return 'fetch', None

    def complete(self, key, data):
        with self.lock:
            self.inflight.discard(key)
            self.entries[key] = (time.monotonic() + (SUCCESS_TTL if data else FAILURE_TTL), data)
            self.entries.move_to_end(key)
            while len(self.entries) > MAX_ENTRIES:
                self.entries.popitem(last=False)


cache = PhotoCache()


async def fetch_photo(bot, entity_id, entity_type):
    async def fetch():
        if entity_type == 'group':
            chat = await bot.get_chat(entity_id)
            if not chat.photo:
                return None
            file_id = chat.photo.small_file_id
        else:
            photos = await bot.get_user_profile_photos(int(entity_id), limit=1)
            if not photos or not photos.total_count:
                return None
            file_id = photos.photos[0][0].file_id
        file = await bot.get_file(file_id)
        if file.file_size and file.file_size > MAX_BYTES:
            return None
        data = bytes(await file.download_as_bytearray())
        return data if len(data) <= MAX_BYTES else None
    return await asyncio.wait_for(fetch(), timeout=TIMEOUT)


async def load(key, bot, entity_id, entity_type):
    data = None
    try:
        data = await fetch_photo(bot, entity_id, entity_type)
    except (Exception, asyncio.CancelledError):
        # No exception strings: Telegram URLs can contain credentials.
        pass
    finally:
        cache.complete(key, data)
