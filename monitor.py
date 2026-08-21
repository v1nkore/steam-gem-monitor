#!/usr/bin/env python3
"""
Steam Dota 2 Unusual gem monitor.

For every target (a Steam Market listing URL with a ?filter=<gem> token) it scans
the current listings, detects which listings actually carry that effect gem, and
sends a Telegram message when a NEW matching listing appears.

Design notes
------------
- Steam's new market UI applies the `filter=` gem filter client-side, so we can't
  trust the server to pre-filter. Instead we read each listing's real gem from its
  embedded description text and match it ourselves.
- Everything runs over a plain HTTP GET (no login) so it can run on any free cron
  host (GitHub Actions, Cloudflare cron via a rewrite, a cheap VPS, etc.).
- State (which listing ids we've already seen) is a small JSON file. On the very
  first run for a target we "seed" the current listings WITHOUT notifying, so you
  only get pinged for future appearances.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Config / environment
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", ROOT / "config.json"))
STATE_FILE = Path(os.environ.get("STATE_FILE", ROOT / "state.json"))

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# If 1, never send Telegram messages — just print what would be sent.
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
# If 1, also notify about listings that already exist on the very first run.
NOTIFY_ON_FIRST = os.environ.get("NOTIFY_ON_FIRST", "0") == "1"

# How many pages (of ~10-20 listings) to scan per target before giving up.
MAX_PAGES = int(os.environ.get("MAX_PAGES", "8"))
# Seconds to wait between HTTP requests to stay under Steam's rate limits.
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "3"))
# Per-target cap on how many seen ids we remember (keeps state.json small).
MAX_SEEN_PER_TARGET = 800

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

APPID = "570"  # Dota 2


# ---------------------------------------------------------------------------
# Target parsing
# ---------------------------------------------------------------------------
class Target:
    def __init__(self, url, match=None, label=None):
        self.url = url
        name, gem = self._parse(url)
        self.name = name                    # market_hash_name (decoded)
        self.gem_token = gem                # the ?filter= token, e.g. "frostbloom"
        # What text we look for inside a listing's description (lowercased).
        self.match = (match or gem or "").replace("_", " ").replace("+", " ").lower().strip()
        self.label = label or name
        if not self.match:
            raise ValueError(f"Target has no gem filter/match: {url}")
        # The real gem always shows up inside a Valve attribute string wrapped in
        # ''...'' (double apostrophes), e.g. ''Frostbloom Unusual Effect Gem''.
        # Requiring that wrapper avoids false positives from stray text (menus,
        # related-item links) for gems whose name is a common word.
        self._matcher = re.compile(r"''[^']*" + re.escape(self.match) + r"[^']*''")

    def matches(self, chunk_lower):
        return bool(self._matcher.search(chunk_lower))

    @staticmethod
    def _parse(url):
        p = urllib.parse.urlparse(url)
        # path: /market/listings/570/<market_hash_name>
        m = re.search(r"/market/listings/\d+/([^/?#]+)", p.path)
        name = urllib.parse.unquote(m.group(1)) if m else ""
        qs = urllib.parse.parse_qs(p.query)
        gem = (qs.get("filter", [""])[0] or "").strip()
        return name, gem

    @property
    def key(self):
        return f"{self.name}|{self.gem_token or self.match}"

    def page_url(self, start, count=100):
        base = f"https://steamcommunity.com/market/listings/{APPID}/{urllib.parse.quote(self.name)}/render/"
        q = {
            "start": start,
            "count": count,
            "currency": 1,
            "country": "US",
            "language": "english",
        }
        return base + "?" + urllib.parse.urlencode(q)


def load_targets():
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    targets = []
    for item in cfg.get("targets", []):
        if isinstance(item, str):
            targets.append(Target(item))
        elif isinstance(item, dict):
            targets.append(Target(item["url"], item.get("match"), item.get("label")))
    return targets


# ---------------------------------------------------------------------------
# HTTP + parsing
# ---------------------------------------------------------------------------
def http_get(url, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:  # rate limited — back off
                time.sleep(10 * (attempt + 1))
                continue
            if 500 <= e.code < 600:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except Exception as e:  # network hiccup
            last_err = e
            time.sleep(3 * (attempt + 1))
    if last_err:
        raise last_err
    return ""


LISTING_SPLIT = re.compile(r'listingid\\+":\\+"')
LISTING_ID = re.compile(r"^(\d+)")
TOTAL_COUNT = re.compile(r'total_count\\+":(\d+)')


def parse_listings(html_text):
    """Return (total_count, [(listingid, chunk_lower), ...]) from a render page.

    The new SSR market page embeds a triple-escaped JSON hydration blob. We split
    it into per-listing chunks and keep each chunk's lowercased text so we can look
    for the effect-gem name inside its description.
    """
    tc_m = TOTAL_COUNT.search(html_text)
    total = int(tc_m.group(1)) if tc_m else None

    parts = LISTING_SPLIT.split(html_text)
    listings = []
    for chunk in parts[1:]:
        m = LISTING_ID.match(chunk)
        if not m:
            continue
        lid = m.group(1)
        # Cap chunk size so one listing can't swallow the next one's data.
        listings.append((lid, chunk[:12000].lower()))
    return total, listings


def scan_target(t):
    """Return the set of listing ids that currently carry the target gem."""
    matches = set()
    total = None
    start = 0
    pages = 0
    while pages < MAX_PAGES:
        html_text = http_get(t.page_url(start))
        page_total, listings = parse_listings(html_text)
        if page_total is not None:
            total = page_total
        if not listings:
            break
        for lid, chunk in listings:
            if t.matches(chunk):
                matches.add(lid)
        step = len(listings)
        start += step
        pages += 1
        if total is not None and start >= total:
            break
        time.sleep(REQUEST_DELAY)
    return matches, total


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"targets": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(text):
    if DRY_RUN or not TG_TOKEN or not TG_CHAT:
        print("[telegram:dry-run] " + text.replace("\n", " | "))
        return
    api = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }
    ).encode()
    req = urllib.request.Request(api, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def notify(t, new_ids):
    n = len(new_ids)
    text = (
        f"🔔 <b>Unusual с ценным самоцветом на ТП!</b>\n"
        f"Предмет: <b>{t.label}</b>\n"
        f"Самоцвет: <b>{t.gem_token or t.match}</b>\n"
        f"Новых листингов: <b>{n}</b>\n"
        f'<a href="{t.url}">Открыть на торговой площадке</a>'
    )
    send_telegram(text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    targets = load_targets()
    if not targets:
        print("No targets in config.json")
        return 0

    state = load_state()
    tstate = state.setdefault("targets", {})
    changed = False

    for t in targets:
        try:
            matches, total = scan_target(t)
        except Exception as e:
            print(f"[error] {t.label}: {e}")
            continue

        entry = tstate.get(t.key)
        if entry is None:
            # First time we see this target: seed silently (unless overridden).
            if NOTIFY_ON_FIRST and matches:
                notify(t, matches)
            tstate[t.key] = {"seen": sorted(matches), "seeded": True}
            changed = True
            print(f"[seed] {t.label}: {len(matches)} matching / {total} total listings")
            continue

        seen = set(entry.get("seen", []))
        new_ids = matches - seen
        if new_ids:
            print(f"[NEW] {t.label}: {len(new_ids)} new matching listing(s)")
            notify(t, new_ids)
        else:
            print(f"[ok] {t.label}: {len(matches)} matching / {total} total (no new)")

        # Remember everything we've ever matched (bounded), so we don't re-notify.
        merged = list(seen | matches)
        if len(merged) > MAX_SEEN_PER_TARGET:
            merged = merged[-MAX_SEEN_PER_TARGET:]
        entry["seen"] = sorted(merged)
        if new_ids:
            changed = True

    if changed:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
