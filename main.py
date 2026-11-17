import hashlib
import hmac
import logging
import os
import random
import time
from collections import OrderedDict
from urllib.parse import quote

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
    timeout=8,
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

MEDIA_CACHE_MAX = int(os.getenv("MEDIA_CACHE_MB", "150")) * 1024 * 1024
_media_cache: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_media_size = 0


def _media_put(url: str, body: bytes, ctype: str) -> None:
    global _media_size
    if len(body) > MEDIA_CACHE_MAX // 10:  # skip very large files
        return
    if url in _media_cache:
        _media_size -= len(_media_cache.pop(url)[0])
    _media_cache[url] = (body, ctype)
    _media_size += len(body)
    while _media_size > MEDIA_CACHE_MAX and _media_cache:  # evict least recently used
        _, (old, _) = _media_cache.popitem(last=False)
        _media_size -= len(old)


def proxied(url: str) -> str:
    """Serve media via this service so Telegram doesn't hit the CDN directly."""
    sig = hmac.new(SECRET.encode(), url.encode(), hashlib.sha256).hexdigest()[:20]
    return f"{PUBLIC_URL}/m/{sig}?u={quote(url, safe='')}"


async def media(request: web.Request):
    url = request.query.get("u", "")
    sig = request.match_info["sig"]
    good = hmac.new(SECRET.encode(), url.encode(), hashlib.sha256).hexdigest()[:20]
    if not url.startswith("https://") or not hmac.compare_digest(sig, good):
        return web.Response(status=403)
    hit = _media_cache.get(url)
    if hit:
        _media_cache.move_to_end(url)
        return web.Response(
            body=hit[0], content_type=hit[1], headers={"Cache-Control": "public, max-age=86400"}
        )
    try:
        r = await client.get(url, headers={"Referer": SITE + "/"})
    except httpx.HTTPError:
        return web.Response(status=502)
    if r.status_code != 200:
        log.warning("media HTTP %s for %s", r.status_code, url[:80])
        return web.Response(status=502)
    ctype = r.headers.get("content-type", "application/octet-stream").split(";")[0]
    _media_put(url, r.content, ctype)
    return web.Response(
        body=r.content,
        content_type=ctype,
        headers={"Cache-Control": "public, max-age=86400"},
    )


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
    items = list(await fetch_page(q.query.strip(), page))
    random.shuffle(items)
    log.info("inline %r page %s -> %d items", q.query, page, len(items))
    results = [
        InlineQueryResultMpeg4Gif(
            id=hashlib.md5(mp4.encode()).hexdigest(),
            mpeg4_url=proxied(mp4),
            thumbnail_url=proxied(thumb),
        )
        for mp4, thumb in items[:MAX_RESULTS]
    ]
    # Non-empty next_offset => Telegram requests the next page as the user scrolls.
    next_offset = str(page + 1) if results else ""
    await q.answer(results, cache_time=0, is_personal=False, next_offset=next_offset)


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
    app.router.add_get("/m/{sig}", media)
    SimpleRequestHandler(dp, bot, secret_token=SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


if __name__ == "__main__":
    main()
