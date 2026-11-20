#!/usr/bin/env python3
"""Chrome Hearts restock watcher.

Checks each product in watchlist.json using the store's public product-variation
endpoint, and sends a phone alert (photo, price, sizes in stock) when something
comes back in stock, including when a single size comes back.

Run once:        python3 restock_bot.py
Run in a loop:   python3 restock_bot.py --loop 600
Test alerts:     python3 restock_bot.py --test-notify
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
WATCHLIST = HERE / "watchlist.json"            # products to watch (safe to share)
PRIVATE_SETTINGS = HERE / "settings.local.json"  # your ntfy topic, phone numbers (never uploaded)
STATE = HERE / "state.json"
ON_MAC = sys.platform == "darwin"
LOG = HERE / "restock_bot.log"

BASE = "https://www.chromehearts.com"
VARIATION = BASE + "/on/demandware.store/Sites-ChromeHearts-Site/en_US/Product-Variation"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
DELAY_BETWEEN_REQUESTS = 6  # seconds; the site rate-limits bursts


class RateLimited(Exception):
    pass


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with LOG.open("a") as f:
        f.write(line + "\n")


def _get(url, accept="text/html"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            raise RateLimited(f"HTTP {e.code}")
        raise


def fetch_variation(pid, dwvars=None):
    params = {"pid": pid, "quantity": "1"}
    for attr, value_id in (dwvars or {}).items():
        params[f"dwvar_{pid}_{attr}"] = value_id
    body = _get(VARIATION + "?" + urllib.parse.urlencode(params), "application/json")
    try:
        return json.loads(body)["product"]
    except (ValueError, KeyError):
        raise RateLimited("non-JSON response (probably a bot challenge)")


def fetch_page(url):
    text = _get(url).decode("utf-8", "replace")
    if "challenges.cloudflare.com" in text and "data-pid" not in text:
        raise RateLimited("bot challenge page")
    return text


def pid_from_url(url):
    m = re.search(r"/([A-Z0-9]+)\.html", url)
    if not m:
        raise ValueError(f"Can't find a product ID in {url}")
    return m.group(1)


def _attrs(product):
    return {a["id"].lower(): a for a in product.get("variationAttributes") or []}


def _in_stock_values(product, attr):
    a = _attrs(product).get(attr)
    if not a:
        return []
    return [v.get("displayValue") for v in a.get("values", []) if v.get("selectable")]


def _image(product):
    imgs = product.get("images") or {}
    for size in ("medium", "large", "small"):
        for img in imgs.get(size) or []:
            url = img.get("url", "")
            if url and "noimage" not in url:
                return urllib.parse.urljoin(BASE, url)
    return None


def check_item(item):
    """Return a dict describing the item's current stock."""
    pid = pid_from_url(item["url"])
    product = fetch_variation(pid)
    info = {
        "name": product.get("productName", pid),
        "price": ((product.get("price") or {}).get("sales") or {}).get("formatted", ""),
        "image": _image(product),
        "url": item["url"],
        "options": "",
    }
    wanted = item.get("options") or {}  # e.g. {"size": "M", "color": "Black"}

    if not wanted:
        info["available"] = bool(product.get("available"))
        info["sizes"] = _in_stock_values(product, "size")
        info["colors"] = _in_stock_values(product, "color")
        info["status"] = ", ".join(product.get("availability", {}).get("messages", []))
        return info

    # Map the display values you typed (e.g. "M") to the store's value ids (e.g. "MED").
    dwvars, label = {}, []
    attrs = _attrs(product)
    for attr, display in wanted.items():
        a = attrs.get(attr.lower())
        if not a:
            raise ValueError(f"{info['name']}: no option '{attr}' (has: {', '.join(attrs) or 'none'})")
        match = next((v for v in a.get("values", [])
                      if str(v.get("displayValue", "")).lower() == str(display).lower()
                      or str(v.get("id", "")).lower() == str(display).lower()), None)
        if not match:
            options = ", ".join(v.get("displayValue", "?") for v in a.get("values", []))
            raise ValueError(f"{info['name']}: no {attr} '{display}' (has: {options})")
        dwvars[a["id"]] = match["id"]
        label.append(f"{attr.capitalize()}: {match.get('displayValue', display)}")

    time.sleep(DELAY_BETWEEN_REQUESTS)
    variant = fetch_variation(pid, dwvars)
    info["available"] = bool(variant.get("available")) and bool(variant.get("readyToOrder", True))
    info["sizes"], info["colors"] = [], []
    info["options"] = " · ".join(label)
    info["image"] = _image(variant) or info["image"]  # photo of the exact color you picked
    info["status"] = ", ".join(variant.get("availability", {}).get("messages", []))
    return info


# ---------- new products ----------

def discover_categories(homepage_html):
    """Shop categories linked from the homepage menu, e.g. /scents, /socks."""
    skip = {"cart", "login", "contact", "account", "search", "wishlist"}
    found = re.findall(r'href="(?:https://www\.chromehearts\.com)?/([a-z0-9][a-z0-9-]*)"', homepage_html)
    return sorted({c for c in found if c not in skip})


def parse_tiles(page_html, category):
    """Product tiles on a category page -> {pid: {name, price, url, image, category}}."""
    products = {}
    for chunk in page_html.split('class="product productType')[1:]:
        meta = re.search(r'data-pid="([^"]+)"\s+data-name="([^"]*)"\s+data-price="([^"]*)"', chunk)
        if not meta:
            continue
        pid, name, price = meta.groups()
        link = re.search(r'href="([^"]+\.html)"', chunk)
        img = re.search(r'(https://[^"\s]+/img_products/[^"\s?]+\.(?:png|jpg|jpeg))', chunk)
        products[pid] = {
            "name": html.unescape(name),
            "price": html.unescape(price),
            "url": urllib.parse.urljoin(BASE, html.unescape(link.group(1))) if link else BASE,
            "image": img.group(1) + "?sw=768" if img else None,
            "category": category.replace("-", " ").title(),
        }
    return products


def scan_category(category):
    products, url, pages = {}, f"{BASE}/{category}", 0
    while url and pages < 20:
        if pages:
            time.sleep(DELAY_BETWEEN_REQUESTS)
        page = fetch_page(url)
        pages += 1
        products.update(parse_tiles(page, category))
        more = re.search(r'(/on/demandware\.store/[^"\s]*Search-UpdateGrid\?[^"\s]+)', page)
        url = urllib.parse.urljoin(BASE, html.unescape(more.group(1))) if more else None
    return products


def check_new_products(cfg, state):
    categories = cfg.get("categories") or []
    if not categories:
        categories = discover_categories(fetch_page(BASE + "/"))
        time.sleep(DELAY_BETWEEN_REQUESTS)

    first_run = "_seen_products" not in state
    seen = set(state.get("_seen_products", []))
    new = []
    for i, cat in enumerate(categories):
        if i:
            time.sleep(DELAY_BETWEEN_REQUESTS)
        try:
            found = scan_category(cat)
        except urllib.error.HTTPError as e:
            log(f"  ! category /{cat}: HTTP {e.code}")
            continue
        for pid, p in found.items():
            if pid not in seen:
                seen.add(pid)
                new.append(p)
        state["_seen_products"] = sorted(seen)  # save progress even if a later page is blocked

    if first_run:
        log(f"New-product watch started: remembering {len(seen)} current products "
            f"across {len(categories)} categories ({', '.join(categories)}).")
        return
    log(f"New-product scan: {len(seen)} products known, {len(new)} new.")

    for n, p in enumerate(new):
        if n >= 5:
            rest = new[5:]
            notify(f"NEW: {len(rest)} more products",
                   "\n".join(f"{q['name']} · {q['price']}" for q in rest[:15]),
                   rest[0]["url"], cfg, rest[0]["image"])
            break
        log(f"  >>> NEW PRODUCT: {p['name']} ({p['category']}, {p['price']})")
        lines = [p["price"], f"Category: {p['category']}"]
        try:  # look up sizes for the alert
            time.sleep(DELAY_BETWEEN_REQUESTS)
            info = check_item({"url": p["url"]})
            if info["sizes"]:
                lines.append(f"Sizes in stock: {', '.join(info['sizes'])}")
            if len(info["colors"]) > 1:
                lines.append(f"Colors: {', '.join(info['colors'])}")
            if not info["available"]:
                lines.append("Not available to buy yet")
            p["image"] = p["image"] or info["image"]
        except RateLimited:
            pass
        except Exception as e:
            log(f"  ! couldn't look up sizes for {p['name']}: {e}")
        notify(f"NEW: {p['name']}", "\n".join(l for l in lines if l), p["url"], cfg, p["image"])


# ---------- notifications ----------

def build_alert(info, new_sizes):
    lines = []
    if info["price"]:
        lines.append(info["price"])
    if info["options"]:
        lines.append(info["options"])
    if info["sizes"]:
        sizes = ", ".join(info["sizes"])
        lines.append(f"Sizes in stock: {sizes}")
        if new_sizes and set(new_sizes) != set(info["sizes"]):
            lines.append(f"Just restocked: {', '.join(new_sizes)}")
    if len(info["colors"]) > 1 or (info["colors"] and info["sizes"]):
        lines.append(f"Colors: {', '.join(info['colors'])}")
    if not info["sizes"] and not info["options"]:
        lines.append(info["status"] or "Back in stock")
    return f"RESTOCK: {info['name']}", "\n".join(lines)


def notify(title, message, url, cfg, image=None):
    # macOS banner
    if ON_MAC:
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False)

    # Phone push via ntfy.sh (free app, no account needed)
    topic = cfg.get("ntfy_topic")
    if topic:
        payload = {
            "topic": topic,
            "title": title,
            "message": message,
            "click": url,
            "priority": 5,
            "tags": ["rotating_light"],
            "actions": [{"action": "view", "label": "Open product", "url": url}],
        }
        if image:
            payload["attach"] = image
            payload["filename"] = "product.png"
        _post("https://ntfy.sh/", json.dumps(payload).encode(), {"Content-Type": "application/json"})

    # iMessage through the Messages app on this Mac (one number, or a list of numbers)
    recipients = cfg.get("imessage_to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    if recipients and ON_MAC:
        photo = _download_image(image) if image else None
        for to in recipients:
            send_imessage(to, f"{title}\n{message}\n{url}", photo)

    # Discord channel webhook
    hook = cfg.get("discord_webhook")
    if hook:
        embed = {"title": title, "description": message, "url": url}
        if image:
            embed["image"] = {"url": image}
        _post(hook, json.dumps({"embeds": [embed]}).encode(), {"Content-Type": "application/json"})


IMESSAGE_SCRIPT = '''
on run argv
    set theHandle to item 1 of argv
    set theText to item 2 of argv
    tell application "Messages"
        set theAccount to 1st account whose service type = iMessage
        set theTarget to participant theHandle of theAccount
        if (count of argv) > 2 then send (POSIX file (item 3 of argv)) to theTarget
        send theText to theTarget
    end tell
end run
'''
IMAGE_DIR = Path.home() / "Pictures" / "ChromeHeartsBot"  # Messages can reliably attach from here


def _download_image(url):
    try:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        path = IMAGE_DIR / f"alert-{datetime.now():%Y%m%d-%H%M%S}.png"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            path.write_bytes(r.read())
        for old in sorted(IMAGE_DIR.glob("alert-*.png"))[:-20]:  # keep the last 20
            old.unlink()
        return str(path)
    except Exception as e:
        log(f"  ! couldn't download product photo for iMessage: {e}")
        return None


def send_imessage(to, text, photo_path=None):
    args = ["osascript", "-e", IMESSAGE_SCRIPT, to, text]
    if photo_path:
        args.append(photo_path)
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  ! iMessage to {to} failed: {r.stderr.strip()}")


def _post(url, data, headers):
    try:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "restock-bot", **headers})
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:
        log(f"  ! notification to {urllib.parse.urlparse(url).netloc} failed: {e}")


# ---------- main ----------

def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def load_config():
    """watchlist.json + private settings (settings.local.json on the Mac, secrets in the cloud)."""
    cfg = load_json(WATCHLIST, None)
    if cfg is None:
        sys.exit(f"Missing {WATCHLIST}")
    cfg.update(load_json(PRIVATE_SETTINGS, {}))
    for key in ("ntfy_topic", "discord_webhook"):
        if os.environ.get(key.upper()):  # e.g. NTFY_TOPIC from a GitHub secret
            cfg[key] = os.environ[key.upper()]
    return cfg


def run_once():
    cfg = load_config()
    state = load_json(STATE, {})
    try:
        check_watchlist(cfg, state)
        if cfg.get("watch_new_products", True):
            time.sleep(DELAY_BETWEEN_REQUESTS)
            check_new_products(cfg, state)
    except RateLimited as e:
        log(f"Site is rate-limiting us ({e}); stopping this round, will retry next time.")
    finally:
        STATE.write_text(json.dumps(state, indent=2))


def check_watchlist(cfg, state):
    for i, item in enumerate(cfg.get("items", [])):
        if i:
            time.sleep(DELAY_BETWEEN_REQUESTS)
        key = item["url"] + json.dumps(item.get("options") or {}, sort_keys=True)
        try:
            info = check_item(item)
        except RateLimited:
            raise
        except Exception as e:
            log(f"  ! {item['url']}: {e}")
            continue

        prev = state.get(key)
        if isinstance(prev, bool):  # state from the older version of this script
            prev = {"available": prev, "sizes": None}

        sizes_txt = f" sizes: {', '.join(info['sizes'])}" if info["sizes"] else ""
        log(f"{'IN STOCK ' if info['available'] else 'sold out '} {info['name']} "
            f"{info['options']}{sizes_txt} ({info['status']})")

        if prev is not None and info["available"]:
            came_back = prev.get("available") is False
            old_sizes = prev.get("sizes")
            new_sizes = [s for s in info["sizes"] if old_sizes is not None and s not in old_sizes]
            if came_back or new_sizes:
                log(f"  >>> RESTOCK: {info['name']} {', '.join(new_sizes)}")
                title, message = build_alert(info, new_sizes)
                notify(title, message, info["url"], cfg, info["image"])

        state[key] = {"available": info["available"], "sizes": info["sizes"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="keep running, checking every N seconds")
    ap.add_argument("--test-notify", action="store_true",
                    help="send a sample alert for the last product in the watchlist and exit")
    args = ap.parse_args()

    if args.test_notify:
        cfg = load_config()
        items = cfg.get("items") or []
        if items:
            info = check_item(items[-1])
            title, message = build_alert(info, info["sizes"][-1:])
            notify("TEST " + title, message, info["url"], cfg, info["image"])
        else:
            notify("Restock bot test", "Notifications are working", BASE, cfg)
        phones = cfg.get("imessage_to") or []
        phones = [phones] if isinstance(phones, str) else phones
        targets = []
        if cfg.get("ntfy_topic"):
            targets.append(f"ntfy topic '{cfg['ntfy_topic']}'")
        if phones:
            targets.append("iMessage " + ", ".join(phones))
        where = " and ".join(targets) or "your Mac only"
        print(f"Test alert sent to {where}. Check your phone.")
        return
    while True:
        run_once()
        if not args.loop:
            break
        time.sleep(max(args.loop, 300))  # never faster than every 5 minutes


if __name__ == "__main__":
    main()
