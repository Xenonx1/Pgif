import hashlib
import logging
import os
import time

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import InlineQuery, InlineQueryResultMpeg4Gif, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("phgifbot")

TOKEN = os.environ["BOT_TOKEN"]
SITE = os.getenv("SITE_URL", "https://www.pornhub.com").rstrip("/")
PUBLIC_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.environ["PUBLIC_URL"]).rstrip("/")
PROXY = os.getenv("PROXY_URL") or None  # optional, e.g. http://user:pass@host:port
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
WEBHOOK_PATH = f"/webhook/{SECRET}"
CACHE_TTL = 600
MAX_RESULTS = 50  # Telegram limit per inline answer

bot = Bot(TOKEN)
dp = Dispatcher()

client = httpx.AsyncClient(
    timeout=15,
    follow_redirects=True,
    proxy=PROXY,
    headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    },
    cookies={"accessAgeDisclaimerPH": "1", "accessPH": "1", "platform": "pc"},
)

_cache: dict = {}


def parse(html: str) -> list[tuple[str, str]]:
    """Return [(mp4_url, thumb_url)] from a gifs listing page."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for el in soup.select("[data-mp4]"):
        mp4 = el.get("data-mp4")
        if not mp4 or mp4 in seen:
            continue
        seen.add(mp4)
        thumb = el.get("data-poster") or el.get("poster")
        if not thumb:
            li = el.find_parent("li")
            img = li.find("img") if li else None
            thumb = (img.get("data-src") or img.get("src")) if img else None
        out.append((mp4, thumb or mp4))
    return out


async def fetch_page(query: str, page: int) -> list[tuple[str, str]]:
    key = (query, page)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    if query:
        url, params = f"{SITE}/gifs/search", {"search": query, "page": page}
    else:
        url, params = f"{SITE}/gifs", {"page": page}
    try:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            log.warning("HTTP %s for %s page %s", r.status_code, query, page)
            return []
        items = parse(r.text)
    except httpx.HTTPError as e:
        log.warning("fetch failed: %s", e)
        return []
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.time(), items)
    return items


@dp.inline_query()
async def on_inline(q: InlineQuery):
    page = int(q.offset) if q.offset.isdigit() else 1
    items = await fetch_page(q.query.strip(), page)
    results = [
        InlineQueryResultMpeg4Gif(
            id=hashlib.md5(mp4.encode()).hexdigest(),
            mpeg4_url=mp4,
            thumbnail_url=thumb,
        )
        for mp4, thumb in items[:MAX_RESULTS]
    ]
    # Non-empty next_offset => Telegram requests the next page as the user scrolls.
    next_offset = str(page + 1) if results else ""
    await q.answer(results, cache_time=300, is_personal=False, next_offset=next_offset)


@dp.message(CommandStart())
async def on_start(m: Message):
    await m.answer(
        "18+ only. Use me inline in any chat: type @" + (await bot.me()).username + " <search>.\n"
        "Leave the query empty for trending. Scroll for more."
    )


async def health(_):
    return web.Response(text="ok")


async def on_startup(_):
    await bot.set_webhook(
        PUBLIC_URL + WEBHOOK_PATH,
        secret_token=SECRET,
        allowed_updates=["inline_query", "message"],
        drop_pending_updates=True,
    )


async def on_cleanup(_):
    await client.aclose()
    await bot.session.close()


def main():
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dp, bot, secret_token=SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


if __name__ == "__main__":
    main()
