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

# How many pages (~10-20 listings each) to scan per item. Steam returns listings
# roughly cheapest-first, and for flipping we only care about CHEAP frostbloom lots
# (near the floor), so a few pages is enough — and keeps us well under Steam's
# per-IP rate limit (scanning every page of every item trips it).
MAX_PAGES = int(os.environ.get("MAX_PAGES", "3"))
# Seconds to wait between HTTP requests to stay under Steam's rate limits.
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "5"))
# Per-target cap on how many seen ids we remember (keeps state.json small).
MAX_SEEN_PER_TARGET = 800

# Error alerting: how many consecutive failed runs before we ping Telegram
# (2 avoids crying wolf over a single transient network blip), and how often to
# remind while an error persists.
ERROR_THRESHOLD = int(os.environ.get("ERROR_THRESHOLD", "3"))
ERROR_REMINDER_HOURS = float(os.environ.get("ERROR_REMINDER_HOURS", "12"))
# Optional "still alive" heartbeat. 0 = off. E.g. 24 = one message per day even
# when there is nothing new — useful to also catch "the cron stopped running".
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "0"))

# Arbitrage mode: only lots claiming THIS gem trigger notifications, and each
# alert shows the lot's price vs the item's cheapest lot (the "floor"/default
# price). The play: a frostbloom lot listed near the floor can be bought and
# resold at the frostbloom premium.
# NOTE: Steam does NOT expose the real socketed gem — the gem name comes from the
# seller's (editable) description, so it is shown as an UNVERIFIED claim.
TRACK_GEM = os.environ.get("TRACK_GEM", "frostbloom").strip().lower()


class MonitorError(Exception):
    """A problem we want surfaced to Telegram (e.g. Steam returned garbage)."""

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

APPID = "570"  # Dota 2


# ---------------------------------------------------------------------------
# Target parsing
# ---------------------------------------------------------------------------
class Target:
    def __init__(self, url, label=None, **_ignored):
        self.url = url
        self.name = self._parse(url)              # market_hash_name (decoded)
        self.label = label or self.name
        # Plain item page (no gem filter) — we surface every listing.
        self.link = f"https://steamcommunity.com/market/listings/{APPID}/{urllib.parse.quote(self.name)}"
        if not self.name:
            raise ValueError(f"Could not parse item name from URL: {url}")

    @staticmethod
    def _parse(url):
        p = urllib.parse.urlparse(url)
        # path: /market/listings/570/<market_hash_name>
        m = re.search(r"/market/listings/\d+/([^/?#]+)", p.path)
        return urllib.parse.unquote(m.group(1)) if m else ""

    @property
    def key(self):
        return self.name

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
            targets.append(Target(item["url"], label=item.get("label")))
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
# A gem, when present, is written by the seller inside a Valve attribute string
# wrapped in ''...'' and mentioning "effect gem", e.g. ''Frostbloom Unusual Effect
# Gem'' or ''Unusual Effect Gem Frostbloom''. This is the ONLY gem text Steam
# exposes, and it is user-editable (hence "unverified").
GEM_CLAIM = re.compile(r"''\s*([^']*?effect gem[^']*?)\s*''")


def extract_gem(chunk_lower):
    """Return the seller-claimed gem name for a listing, or None."""
    for m in GEM_CLAIM.finditer(chunk_lower):
        name = re.sub(r"unusual effect gem|effect gem", "", m.group(1)).strip(" '\"-")
        if name:
            return name
    return None


# Prices live in the listing JSON as unPrice (base) + unFee (fee); the buyer pays
# their sum. Chunks are lowercased, so match 'unprice'/'unfee'.
UN_PRICE = re.compile(r'unprice\\+":(\d+)')
UN_FEE = re.compile(r'unfee\\+":(\d+)')


def listing_price(chunk_lower):
    """Buyer-facing price of a listing in cents, or None if not found."""
    p = UN_PRICE.search(chunk_lower)
    if not p:
        return None
    f = UN_FEE.search(chunk_lower)
    return int(p.group(1)) + (int(f.group(1)) if f else 0)


def fmt_price(cents):
    return f"${cents / 100:.2f}" if cents is not None else "?"


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


def fetch_page(t, start, retries=3):
    """Fetch+parse one listings page, retrying when Steam returns an unreadable
    anti-bot/interstitial page (HTTP 200 but no listing data). This absorbs brief
    rate-limiting inside a run so it doesn't look like a hard failure.

    Returns (total_count, listings). A genuinely empty item returns (0, []). Only a
    persistently unreadable page returns (None, []).
    """
    delay = 6
    for attempt in range(retries):
        html_text = http_get(t.page_url(start))
        page_total, listings = parse_listings(html_text)
        if listings or page_total is not None:
            return page_total, listings
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    return None, []


def scan_target(t):
    """Scan all listing pages for an item.

    Returns (gem_lots, floor, total) where:
      - gem_lots: {listing_id: buyer_price_cents} for lots claiming TRACK_GEM,
      - floor: cheapest buyer price across ALL lots (the item's default price),
      - total: total listing count reported by Steam.
    """
    gem_lots = {}
    floor = None
    total = None
    start = 0
    pages = 0
    while pages < MAX_PAGES:
        page_total, listings = fetch_page(t, start)
        if page_total is not None:
            total = page_total
        if not listings:
            break
        for lid, chunk in listings:
            price = listing_price(chunk)
            if price is not None and (floor is None or price < floor):
                floor = price
            if extract_gem(chunk) == TRACK_GEM:
                gem_lots[lid] = price
        start += len(listings)
        pages += 1
        if total is not None and start >= total:
            break
        time.sleep(REQUEST_DELAY)
    return gem_lots, floor, total


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


def safe_send(text):
    """Send without ever crashing the run — used for error/status messages."""
    try:
        send_telegram(text)
        return True
    except Exception as e:
        print(f"[telegram-error] {e}")
        return False


def run_link():
    """Link to the current GitHub Actions run, if we're inside one."""
    srv = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    rid = os.environ.get("GITHUB_RUN_ID")
    if srv and repo and rid:
        return f"{srv}/{repo}/actions/runs/{rid}"
    return None


def send_error_alert(alerts):
    link = run_link()
    lines = ["⚠️ <b>Проблема в мониторинге</b>", ""]
    for a in alerts:
        tag = " (всё ещё не чинится)" if a.get("reminder") else ""
        lines.append(f"• <b>{a['label']}</b>{tag}: {a['reason']}")
    lines += ["", "Пока это не починится, новые лоты могут не отслеживаться."]
    if link:
        lines.append(f'<a href="{link}">Открыть логи запуска</a>')
    safe_send("\n".join(lines))


def send_recovery(labels):
    uniq = ", ".join(sorted(set(labels)))
    safe_send(f"✅ <b>Мониторинг восстановился</b>\nСнова читаю листинги нормально: {uniq}")


def send_heartbeat(n_targets, n_errors):
    status = "ошибок нет ✅" if n_errors == 0 else f"активных ошибок: {n_errors} ⚠️"
    safe_send(
        f"💓 <b>Мониторинг работает</b>\n"
        f"Отслеживаю предметов: {n_targets}\n{status}"
    )


def notify(t, new_lots, floor):
    """new_lots: list of (listing_id, price_cents) for lots claiming TRACK_GEM."""
    n = len(new_lots)
    gem_title = TRACK_GEM.title()
    link = f"{t.link}?filter={urllib.parse.quote(TRACK_GEM)}"
    lines = [
        f"💎 <b>{t.label}</b>",
        f"Новых {gem_title}-лотов: <b>{n}</b>",
        f"Самый дешёвый лот предмета (пол): <b>{fmt_price(floor)}</b>",
        "",
    ]
    for _lid, price in sorted(new_lots, key=lambda x: (x[1] is None, x[1])):
        if price is None:
            lines.append(f"• {gem_title}: цена не определена")
        elif floor is not None and price <= floor:
            lines.append(f"• {gem_title} <b>{fmt_price(price)}</b> — 🔥 на уровне пола или дешевле — возможный флип!")
        elif floor is not None:
            diff = price - floor
            pct = diff / floor * 100 if floor else 0
            lines.append(f"• {gem_title} <b>{fmt_price(price)}</b> — дороже пола на {fmt_price(diff)} (+{pct:.0f}%)")
        else:
            lines.append(f"• {gem_title} <b>{fmt_price(price)}</b>")
    lines += ["", "⚠️ гем заявлен продавцом, не подтверждён — проверь инспектом в игре",
              f'<a href="{link}">Открыть {gem_title}-лоты</a>']
    safe_send("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    """Top-level wrapper: any unexpected crash is reported to Telegram, then the
    run is failed so it's also visible in GitHub Actions."""
    try:
        return _run()
    except Exception as e:
        link = run_link()
        msg = (
            "🛑 <b>Мониторинг упал с фатальной ошибкой</b>\n"
            f"<code>{type(e).__name__}: {str(e)[:400]}</code>"
        )
        if link:
            msg += f'\n<a href="{link}">Логи запуска</a>'
        safe_send(msg)
        raise


def _run():
    targets = load_targets()
    if not targets:
        print("No targets in config.json")
        return 0

    state = load_state()
    tstate = state.setdefault("targets", {})
    estate = state.setdefault("errors", {})  # fingerprint -> failure record
    changed = False
    now = time.time()

    alerts = []        # new/renewed errors to announce this run
    recovered = []     # labels that were failing and now work
    ok_target_keys = set()

    for i, t in enumerate(targets):
        if i:
            time.sleep(REQUEST_DELAY)  # pace between items too, not just pages
        # ---- scan, treating an unreadable page as an error worth reporting ----
        try:
            gem_lots, floor, total = scan_target(t)
            if total is None and floor is None:
                raise MonitorError(
                    "Steam вернул неожиданный ответ — не удалось прочитать листинги "
                    "(возможна блокировка IP или смена формата страницы)."
                )
        except Exception as e:
            fp = f"{t.key}::{type(e).__name__}"
            ent = estate.get(fp) or {"fails": 0, "since": now, "notified": False, "last_notified": 0}
            ent["fails"] += 1
            ent["target"] = t.key
            ent["label"] = t.label
            ent["reason"] = str(e)[:300]
            estate[fp] = ent
            changed = True
            reminder_due = now - ent.get("last_notified", 0) >= ERROR_REMINDER_HOURS * 3600
            if ent["fails"] >= ERROR_THRESHOLD and (not ent["notified"] or reminder_due):
                alerts.append({"label": t.label, "reason": ent["reason"], "reminder": ent["notified"]})
                ent["notified"] = True
                ent["last_notified"] = now
            print(f"[error] {t.label}: {e} (fails={ent['fails']})")
            continue

        ok_target_keys.add(t.key)

        # ---- only TRACK_GEM lots trigger alerts; show price vs floor ----
        current = set(gem_lots)
        entry = tstate.get(t.key)
        if entry is None:
            if NOTIFY_ON_FIRST and current:
                notify(t, [(lid, gem_lots[lid]) for lid in current], floor)
            tstate[t.key] = {"seen": sorted(current), "seeded": True}
            changed = True
            print(f"[seed] {t.label}: {len(current)} {TRACK_GEM} lots / floor {fmt_price(floor)}")
            continue

        seen = set(entry.get("seen", []))
        new_ids = current - seen
        if new_ids:
            print(f"[NEW] {t.label}: {len(new_ids)} new {TRACK_GEM} lot(s) / floor {fmt_price(floor)}")
            notify(t, [(lid, gem_lots[lid]) for lid in new_ids], floor)
        else:
            print(f"[ok] {t.label}: {len(current)} {TRACK_GEM} lots / floor {fmt_price(floor)} (no new)")

        merged = list(seen | current)
        if len(merged) > MAX_SEEN_PER_TARGET:
            merged = merged[-MAX_SEEN_PER_TARGET:]
        entry["seen"] = sorted(merged)
        if new_ids:
            changed = True

    # ---- clear errors for targets that scanned fine again ----
    for fp in list(estate.keys()):
        ent = estate[fp]
        if ent.get("target") in ok_target_keys:
            if ent.get("notified"):
                recovered.append(ent.get("label", ent["target"]))
            del estate[fp]
            changed = True

    if alerts:
        send_error_alert(alerts)
    if recovered:
        send_recovery(recovered)

    # ---- optional heartbeat ----
    if HEARTBEAT_HOURS > 0:
        last_hb = state.get("heartbeat", 0)
        if now - last_hb >= HEARTBEAT_HOURS * 3600:
            send_heartbeat(len(targets), len(estate))
            state["heartbeat"] = now
            changed = True

    if changed:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
