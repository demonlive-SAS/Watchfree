#!/usr/bin/env python3
"""
Scrape VIZIO WatchFree+ live channels and build:
    Vizio.m3u      - IPTV playlist (every channel in group "Fast Vizio")
    Vizio.xml.gz   - XMLTV guide (gzipped)

The listing page (https://www.watchfreeplus.com/live-tv-channels) is a Next.js
app that lazy-loads its category rows, and stream URLs are not in the HTML.
So this script uses a headless browser to:
  1. scroll the listing page until every row has loaded,
  2. open each channel page, press "Watch Now", and sniff the .m3u8 request.

Usage:
    python vizio_scraper.py                      # full run
    python vizio_scraper.py --max-channels 10    # quick test
    python vizio_scraper.py --include-unresolved # keep channels with no stream
"""
import argparse
import asyncio
import gzip
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from playwright.async_api import async_playwright

BASE = "https://www.watchfreeplus.com"
LIST_URL = f"{BASE}/live-tv-channels"
GROUP_TITLE = "Fast Vizio"  # every channel gets this group-title
OUT_NAME = "Vizio"
CACHE_FILE = Path("channels_cache.json")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CHANNEL_RE = re.compile(r"/live-tv-channels/(\d+)/([^/?#]+)")

EXTRACT_JS = """
els => els.map(a => {
  let c = a;
  for (let i = 0; i < 5 && c; i++) {
    if (c.querySelector('h3') && c.querySelector('img')) break;
    c = c.parentElement;
  }
  const img = c ? c.querySelector('img') : null;
  const h3 = c ? c.querySelector('h3') : null;
  return {
    href: a.href,
    name: ((h3 && h3.textContent) || (img && img.alt) || '').trim(),
    logo: img ? (img.currentSrc || img.src || '') : ''
  };
})
"""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


async def accept_terms(page):
    """Dismiss the 'Privacy & Terms' modal if it shows up."""
    try:
        btn = page.get_by_text("Accept & Continue", exact=True).first
        if await btn.is_visible(timeout=2500):
            await btn.click()
            await page.wait_for_timeout(500)
    except Exception:
        pass


async def scrape_listing(ctx):
    page = await ctx.new_page()
    await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
    await accept_terms(page)

    last_count, stable = -1, 0
    for _ in range(200):
        await page.evaluate("window.scrollBy(0, Math.max(800, window.innerHeight))")
        await page.wait_for_timeout(700)
        count = await page.locator('a[href*="/live-tv-channels/"]').count()
        at_bottom = await page.evaluate(
            "window.innerHeight + window.scrollY >= document.body.scrollHeight - 5"
        )
        stable = stable + 1 if (count == last_count and at_bottom) else 0
        last_count = count
        if stable >= 6:
            break

    raw = await page.eval_on_selector_all(
        'a[href*="/live-tv-channels/"]', EXTRACT_JS
    )
    await page.close()

    channels = {}
    for item in raw:
        m = CHANNEL_RE.search(item["href"])
        if not m:
            continue
        cid, slug = m.group(1), m.group(2)
        if cid in channels:
            continue
        name = item["name"] or slug.replace("-", " ").title()
        channels[cid] = {
            "id": cid,
            "slug": slug,
            "name": name,
            "logo": item["logo"],
            "url": f"{BASE}/live-tv-channels/{cid}/{slug}",
        }
    log(f"Found {len(channels)} channels on listing page")
    return list(channels.values())


async def resolve_stream(ctx, ch, sem):
    """Open a channel page, press Watch Now, capture the first .m3u8 request."""
    async with sem:
        page = await ctx.new_page()
        found = []

        def on_response(resp):
            url = resp.url
            ctype = (resp.headers.get("content-type") or "").lower()
            if ".m3u8" in url.split("?")[0].lower() or "mpegurl" in ctype:
                found.append(url)

        page.on("response", on_response)
        try:
            await page.goto(ch["url"], wait_until="domcontentloaded", timeout=45000)
            await accept_terms(page)

            try:
                desc = await page.locator('meta[name="description"]').get_attribute(
                    "content", timeout=3000
                )
                if desc:
                    ch["description"] = desc.strip()
            except Exception:
                pass

            try:
                watch = page.get_by_text(re.compile(r"^\s*Watch Now\s*$", re.I)).first
                await watch.click(timeout=8000)
            except Exception:
                pass

            for _ in range(30):  # wait up to ~15s for a playlist request
                if found:
                    break
                await page.wait_for_timeout(500)
        except Exception as e:
            log(f"  ! {ch['name']}: {e}")
        finally:
            await page.close()

        if found:
            ch["stream"] = found[0]
            log(f"  ok {ch['name']}")
        else:
            log(f"  -- no stream captured for {ch['name']}")
        return ch


def load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def write_m3u(channels, path):
    lines = ["#EXTM3U"]
    for ch in channels:
        tvg_id = f"vizio.{ch['id']}"
        name = ch["name"].replace(",", " ")
        lines.append(
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" '
            f'tvg-logo="{ch.get("logo", "")}" group-title="{GROUP_TITLE}",{name}'
        )
        lines.append(ch["stream"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_xmltv(channels, path, hours=48, block_hours=6):
    """Simple XMLTV guide: rolling programme blocks per channel."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    fmt = "%Y%m%d%H%M%S +0000"
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="vizio-scraper">']
    for ch in channels:
        cid = quoteattr(f"vizio.{ch['id']}")
        out.append(f"  <channel id={cid}>")
        out.append(f"    <display-name>{escape(ch['name'])}</display-name>")
        if ch.get("logo"):
            out.append(f"    <icon src={quoteattr(ch['logo'])} />")
        out.append(f"    <url>{escape(ch['url'])}</url>")
        out.append("  </channel>")
    for ch in channels:
        cid = quoteattr(f"vizio.{ch['id']}")
        desc = escape(ch.get("description") or f"Live programming on {ch['name']}")
        for i in range(0, hours, block_hours):
            start = now + timedelta(hours=i)
            stop = start + timedelta(hours=block_hours)
            out.append(
                f'  <programme start="{start.strftime(fmt)}" '
                f'stop="{stop.strftime(fmt)}" channel={cid}>'
            )
            out.append(f'    <title lang="en">{escape(ch["name"])}</title>')
            out.append(f'    <desc lang="en">{desc}</desc>')
            out.append(f'    <category lang="en">{escape(GROUP_TITLE)}</category>')
            out.append("  </programme>")
    out.append("</tv>")
    with gzip.open(path, "wb") as f:
        f.write("\n".join(out).encode("utf-8"))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-channels", type=int, default=0, help="0 = all")
    ap.add_argument("--include-unresolved", action="store_true",
                    help="keep channels with no stream (links to the channel page)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = load_cache()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            timezone_id="America/Chicago",
        )
        channels = await scrape_listing(ctx)
        if args.max_channels:
            channels = channels[: args.max_channels]

        sem = asyncio.Semaphore(args.concurrency)
        channels = await asyncio.gather(*(resolve_stream(ctx, c, sem) for c in channels))
        await browser.close()

    # Fall back to last-known stream if this run failed to capture one
    for ch in channels:
        old = cache.get(ch["id"], {})
        if "stream" not in ch and old.get("stream"):
            ch["stream"] = old["stream"]
            ch["description"] = ch.get("description") or old.get("description")
            log(f"  (using cached stream for {ch['name']})")

    if args.include_unresolved:
        for ch in channels:
            ch.setdefault("stream", ch["url"])
    final = [c for c in channels if c.get("stream")]

    if not final:
        log("No streams captured - leaving existing files untouched.")
        sys.exit(1)

    CACHE_FILE.write_text(json.dumps({c["id"]: c for c in final}, indent=2))
    write_m3u(final, out_dir / f"{OUT_NAME}.m3u")
    write_xmltv(final, out_dir / f"{OUT_NAME}.xml.gz")
    log(f"Wrote {len(final)} channels -> {OUT_NAME}.m3u / {OUT_NAME}.xml.gz")


if __name__ == "__main__":
    asyncio.run(main())
