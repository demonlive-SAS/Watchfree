import base64
import gzip
import json
import os
import sys
import time
import zlib
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

import requests

# Configuration
API_URL = "https://api.lgchannels.com/api/v1.0/schedulelist"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PLAYLIST_NAME = "LG GB Playlist"
M3U_FILENAME = "lggb.m3u"
EPG_FILENAME = "lggb.xml.gz"

# Picks up the repo automatically when run in GitHub Actions
REPO = os.environ.get("GITHUB_REPOSITORY", "YOUR_USERNAME/YOUR_REPO")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
EPG_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{EPG_FILENAME}"

HEADERS = {
    "user-agent": USER_AGENT,
    "x-device-country": "GB",
    "x-device-language": "en",
    "accept": "application/json, text/plain, */*",
    "referer": "https://channel-lineup.lgchannels.com/",
    "origin": "https://channel-lineup.lgchannels.com",
}

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5


def fetch_data():
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(API_URL, headers=HEADERS, timeout=30)
            print(
                f"Attempt {attempt}: HTTP {response.status_code}, "
                f"content-type={response.headers.get('content-type')}, "
                f"body-length={len(response.content)}"
            )
            if response.status_code != 200:
                print(f"Non-200 body (first 300 chars): {response.text[:300]!r}")
                last_error = f"HTTP {response.status_code}"
            else:
                try:
                    return response.json()
                except json.JSONDecodeError:
                    # Some responses come back as base64(zlib(json))
                    try:
                        raw = zlib.decompress(base64.b64decode(response.text))
                        return json.loads(raw)
                    except Exception as e:
                        print(f"Unrecognised body (first 300 chars): {response.text[:300]!r}")
                        last_error = e
        except requests.RequestException as e:
            print(f"Request exception on attempt {attempt}: {e}")
            last_error = e

        if attempt < MAX_RETRIES:
            delay = RETRY_BACKOFF_SECONDS * attempt
            print(f"Retrying in {delay}s...")
            time.sleep(delay)

    print(f"Error fetching data after {MAX_RETRIES} attempts: {last_error}")
    return None


def to_xmltv_time(value):
    """Convert an ISO 8601 timestamp to XMLTV format: YYYYMMDDHHMMSS +0000."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")
    except ValueError:
        return ""


def generate_files(data):
    if not data or "categories" not in data:
        print("No 'categories' in API response.")
        return False

    m3u_lines = [f'#EXTM3U x-tvg-url="{EPG_URL}" playlist-name="{PLAYLIST_NAME}"',
                 f"#PLAYLIST:{PLAYLIST_NAME}"]
    xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<tv generator-info-name="LG GB Playlist">']
    seen = set()

    for category in data.get("categories", []):
        cat_name = category.get("categoryName") or "General"
        for chan in category.get("channels", []):
            chan_id = chan.get("channelId", "")
            if not chan_id or chan_id in seen:
                continue

            stream_url = (chan.get("mediaStaticUrl") or "").split("?")[0]
            if not stream_url:
                continue

            chan_name = chan.get("channelName") or "Unknown"
            programs = chan.get("programs") or []
            logo = programs[0].get("imageUrl", "") if programs else ""

            m3u_lines.append(
                f"#EXTINF:-1 tvg-id={quoteattr(chan_id)} tvg-name={quoteattr(chan_name)} "
                f"tvg-logo={quoteattr(logo)} group-title={quoteattr(cat_name)},{chan_name}"
            )
            m3u_lines.append(stream_url)

            xml_lines.append(
                f"  <channel id={quoteattr(chan_id)}>\n"
                f"    <display-name>{escape(chan_name)}</display-name>\n"
                f"  </channel>"
            )

            for prog in programs:
                start = to_xmltv_time(prog.get("startDateTime"))
                stop = to_xmltv_time(prog.get("endDateTime"))
                if not (start and stop):
                    continue
                title = escape(prog.get("programTitle") or "No Title")
                desc = escape(prog.get("description") or "")
                xml_lines.append(
                    f'  <programme start="{start}" stop="{stop}" channel={quoteattr(chan_id)}>\n'
                    f"    <title>{title}</title>\n"
                    f"    <desc>{desc}</desc>\n"
                    f"  </programme>"
                )

            seen.add(chan_id)

    xml_lines.append("</tv>")

    if not seen:
        print("No channels found; not overwriting existing files.")
        return False

    with open(M3U_FILENAME, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u_lines) + "\n")
    with gzip.open(EPG_FILENAME, "wt", encoding="utf-8") as f:
        f.write("\n".join(xml_lines) + "\n")

    print(f"Wrote {len(seen)} channels to {M3U_FILENAME} and {EPG_FILENAME}")
    return True


if __name__ == "__main__":
    fetched = fetch_data()
    if fetched is None or not generate_files(fetched):
        print("Aborting: leaving existing files untouched.")
        sys.exit(1)
